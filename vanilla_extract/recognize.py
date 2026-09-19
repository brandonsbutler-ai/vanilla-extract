r"""Field recognition: find the structure in a document without being told it.

Two problems, one after the other.

FIRST, per document: forms and invoices are label/value pairs, but they arrive
in several shapes once a PDF is flattened to text --

    Invoice Number: INV-1001          colon separated
    Invoice Number    INV-1001        column separated (runs of whitespace)
    Invoice Number
    INV-1001                          label on its own line, value beneath

All three are the same fact. `label_values()` reads all three.

SECOND, across a corpus: a client with 400 invoices does not want to write
regexes, and does not reliably know what fields their own documents contain.
`infer_schema()` collects labels across every document and ranks them by how
many documents carry them, so the common structure falls out of the corpus
rather than out of a guess. A label present in 380 of 400 files is a column;
one present in 3 is noise.

Everything here is the standard library. The typed detectors are deliberately
conservative -- a pattern that fires on the wrong thing costs a client more
than one that stays quiet, because a wrong value looks exactly like a right
one in a spreadsheet.
"""

import re
from collections import Counter, defaultdict

# --- typed value detectors -------------------------------------------------
# Ordered most-specific first; the first match wins when classifying a value.
#
# Each entry is (name, regex) or (name, regex, predicate). The predicate is
# applied to the matched TEXT and exists for one reason: a condition expressed
# as a lookahead is re-evaluated at every starting position, and one of these
# was costing O(n^2) -- 8.8 seconds on 64 KB of uppercase hyphenated text with
# no digit in it, which is a document anyone can hand you.
DETECTORS = [
    # The lengths are RFC 5321's (64-character local part, 255-character
    # domain) and they are here for speed as much as correctness: an unbounded
    # [A-Za-z0-9._%+-]+ before a literal @ walks the whole of any long run of
    # those characters, fails, and restarts one position later -- 1.2 seconds on
    # 64 KB of hyphenated text with no @ in it. Bounded, the same input is 4 ms,
    # and the two agree on every one of 30,000 compared strings.
    ("email", re.compile(
        r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}\b")),
    ("url", re.compile(r"\bhttps?://[^\s<>\"]+", re.IGNORECASE)),
    ("money", re.compile(r"(?<![\w.])[$£€]\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?(?![\w])")),
    ("date_iso", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("date_us", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    ("date_long", re.compile(
        r"\b(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},?\s+\d{4}\b")),
    ("phone_us", re.compile(r"(?<!\d)(?:\+1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")),
    ("percent", re.compile(r"(?<![\w.])\d{1,3}(?:\.\d+)?\s?%")),
    # Identifier-shaped: a hyphen-separated run of capitals and digits, with at
    # least one digit IN THE MATCH.
    #
    # That condition used to be the lookahead (?=[A-Z0-9-]*\d), which was wrong
    # twice over. It scanned through a double hyphen to find a digit the match
    # can never reach -- `AB-Z--0911` was classified an identifier and the value
    # returned was `AB-Z`, with no digit in it at all -- and because a lookahead
    # is retried at every start position it turned the search quadratic.
    ("identifier", re.compile(r"\b[A-Z0-9]{2,}(?:-[A-Z0-9]+)+\b"),
     lambda t: any(c.isdigit() for c in t)),
]

# Labels this module will not propose as columns: page furniture, not data.
_LABEL_STOPWORDS = {
    "page", "continued", "note", "notes", "comments", "description",
    "terms and conditions", "thank you", "sincerely", "regards",
}

_COLON_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /&'().#-]{1,40}?)\s*:\s*(.+?)\s*$")
_COLUMN_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /&'().#-]{1,40}?)\s{2,}(\S.*?)\s*$")
_LABELISH_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /&'().#-]{1,40}?)\s*:?\s*$")


def normalize_label(label):
    """Collapse a label to a stable key: lowercase, single-spaced, no trailing colon."""
    label = re.sub(r"\s+", " ", label).strip().rstrip(":").strip()
    return label.lower()


def _detector(entry):
    """(name, regex, predicate) from an entry written either way."""
    name, regex = entry[0], entry[1]
    return name, regex, (entry[2] if len(entry) > 2 else None)


def classify(value):
    """Name the kind of value this is, or 'text' when nothing matches."""
    for entry in DETECTORS:
        name, regex, ok = _detector(entry)
        for m in regex.finditer(value):
            if ok and not ok(m.group(0)):
                continue
            if len(m.group(0)) >= len(value.strip()) * 0.6:
                return name
            break
    return "text"


def find_values(text, kind):
    """Every value of one detected kind, in document order."""
    for entry in DETECTORS:
        name, regex, ok = _detector(entry)
        if name == kind:
            return [m.group(0) for m in regex.finditer(text)
                    if not ok or ok(m.group(0))]
    raise KeyError(f"no detector named {kind!r}")


def _kinds(lines):
    """Sort lines into finished pairs, label-shaped lines, value lines and
    breaks. A value line is one a label cannot be: it has a character no label
    has, or it reads as a typed value (money, date, identifier, ...)."""
    found, kinds = [], []
    for i, line in enumerate(lines):
        if not line.strip():
            kinds.append("break")
            continue
        m = _COLON_RE.match(line) or _COLUMN_RE.match(line)
        if m:
            found.append((i, m.group(1), m.group(2)))
            kinds.append("break")
        elif _LABELISH_RE.match(line) and classify(line.strip()) == "text":
            kinds.append("label")
        else:
            kinds.append("value")
    return found, kinds


def label_values(text, max_value_len=200, known=None):
    """Extract {normalized label: value} pairs from a document's text.

    Reads colon-separated, column-separated, and label-on-its-own-line forms.
    Earlier occurrences win, because a form's first statement of a field is
    almost always the real one and later repeats are summaries or footers.

    Label-on-its-own-line is the hard one, because a value can look exactly
    like a label: "Contoso Ltd" and "Net 15" are short capitalised lines, just
    as "Customer" and "Terms" are. One document cannot tell them apart; a
    corpus can, because other documents state the label plainly
    ("Customer: Fabrikam Inc") and the value does not recur. So:

      * a label-shaped line directly above a VALUE line (one no label could
        be) is that value's label -- this needs no outside knowledge;
      * a label-shaped line above another label-shaped line is paired only
        when `known` -- labels the corpus states clearly, from infer_schema(),
        or the chosen columns, from extract_fields() -- names the first and
        not the second. Without `known`, such a pair is left alone.

    Precision over recall, deliberately: an unpaired field is a blank cell a
    reviewer notices; a wrongly paired one is a plausible value nobody does.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    known = {normalize_label(k) for k in known} if known else set()
    found, kinds = _kinds(lines)

    def own_label(i):
        return _LABELISH_RE.match(lines[i]).group(1)

    used = set()                    # a line taken as a value is not a label
    for i, kind in enumerate(kinds):
        if kind != "label" or i in used or i + 1 >= len(kinds):
            continue
        if kinds[i + 1] == "value":
            found.append((i, own_label(i), lines[i + 1].strip()))
            used.add(i + 1)
        elif (kinds[i + 1] == "label" and normalize_label(own_label(i)) in known
              and normalize_label(own_label(i + 1)) not in known):
            found.append((i, own_label(i), lines[i + 1].strip()))
            used.add(i + 1)

    out = {}
    for _i, raw_label, value in sorted(found, key=lambda f: f[0]):
        value = value.strip()
        if not value or len(value) > max_value_len:
            continue
        key = normalize_label(raw_label)
        if not key or key in _LABEL_STOPWORDS or len(key) < 2:
            continue
        if key.isdigit():
            continue
        out.setdefault(key, value)
    return out


def _common_labels(texts, min_support):
    """Labels that at least `min_support` of the documents state CLEARLY --
    colon, column, or above a line no label could be.

    A label-shaped line that merely recurs is not enough. Section headings and
    table headers recur too ("Version", "Method", "Purpose"), and admitting
    them paired each with whatever line followed it: on one real delivery of
    technical documents that turned five proposed columns into thirteen. So a
    stacked label is read when the corpus has seen it stated plainly
    elsewhere, as a mixed folder of form layouts almost always does.
    """
    df = Counter()
    for text in texts:
        df.update(set(label_values(text)))
    return {label for label, n in df.items() if n / len(texts) >= min_support}


def infer_schema(texts, min_support=0.5, max_fields=25):
    """Propose columns from a corpus.

    `texts` is an iterable of document strings. Returns a list of dicts sorted
    by how many documents carry the label:

        {"label", "support", "ratio", "kind", "example"}

    `min_support` is the fraction of documents a label must appear in to be
    proposed. The default of 0.5 is deliberately forgiving -- a client's folder
    is usually two or three form types mixed together, and a field present in
    half the documents is still a column worth having.
    """
    texts = list(texts)
    if not texts:
        return []
    counts = Counter()
    kinds = defaultdict(Counter)
    examples = {}
    known = _common_labels(texts, min_support)
    for text in texts:
        pairs = label_values(text, known=known)
        for label, value in pairs.items():
            counts[label] += 1
            kinds[label][classify(value)] += 1
            examples.setdefault(label, value)

    total = len(texts)
    proposed = []
    for label, count in counts.most_common():
        ratio = count / total
        if ratio < min_support:
            continue
        proposed.append({
            "label": label,
            "support": count,
            "ratio": round(ratio, 3),
            "kind": kinds[label].most_common(1)[0][0],
            "example": examples[label],
        })
        if len(proposed) >= max_fields:
            break
    return proposed


def extract_fields(text, labels):
    """Pull the given labels out of one document. Missing labels give "".

    The labels double as `known` for label_values(), which is what lets a
    label stacked above a label-shaped value ("Customer" / "Contoso Ltd") be
    read at all."""
    pairs = label_values(text, known=labels)
    return {label: pairs.get(normalize_label(label), "") for label in labels}
