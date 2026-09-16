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
DETECTORS = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("url", re.compile(r"\bhttps?://[^\s<>\"]+", re.IGNORECASE)),
    ("money", re.compile(r"(?<![\w.])[$£€]\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?(?![\w])")),
    ("date_iso", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("date_us", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    ("date_long", re.compile(
        r"\b(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},?\s+\d{4}\b")),
    ("phone_us", re.compile(r"(?<!\d)(?:\+1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")),
    ("percent", re.compile(r"(?<![\w.])\d{1,3}(?:\.\d+)?\s?%")),
    # Identifier-shaped: at least one digit and one separator or capital run.
    ("identifier", re.compile(r"\b(?=[A-Z0-9-]*\d)[A-Z0-9]{2,}(?:-[A-Z0-9]+)+\b")),
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


def classify(value):
    """Name the kind of value this is, or 'text' when nothing matches."""
    for name, regex in DETECTORS:
        m = regex.search(value)
        if m and len(m.group(0)) >= len(value.strip()) * 0.6:
            return name
    return "text"


def find_values(text, kind):
    """Every value of one detected kind, in document order."""
    for name, regex in DETECTORS:
        if name == kind:
            return [m.group(0) for m in regex.finditer(text)]
    raise KeyError(f"no detector named {kind!r}")


def label_values(text, max_value_len=200):
    """Extract {normalized label: value} pairs from a document's text.

    Reads colon-separated, column-separated, and label-on-its-own-line forms.
    Earlier occurrences win, because a form's first statement of a field is
    almost always the real one and later repeats are summaries or footers.
    """
    out = {}
    lines = [ln.rstrip() for ln in text.splitlines()]

    def record(raw_label, value):
        value = value.strip()
        if not value or len(value) > max_value_len:
            return
        key = normalize_label(raw_label)
        if not key or key in _LABEL_STOPWORDS or len(key) < 2:
            return
        if key.isdigit():
            return
        out.setdefault(key, value)

    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = _COLON_RE.match(line)
        if m:
            record(m.group(1), m.group(2))
            continue
        m = _COLUMN_RE.match(line)
        if m:
            record(m.group(1), m.group(2))
            continue
        # Label alone on a line, value on the next non-blank line.
        m = _LABELISH_RE.match(line)
        if m and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            # Only if the next line does not itself look like a label.
            if nxt and not _COLON_RE.match(nxt) and not _LABELISH_RE.match(nxt):
                record(m.group(1), nxt)
    return out


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
    for text in texts:
        pairs = label_values(text)
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
    """Pull the given labels out of one document. Missing labels give ""."""
    pairs = label_values(text)
    return {label: pairs.get(normalize_label(label), "") for label in labels}
