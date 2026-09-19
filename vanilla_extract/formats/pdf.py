r"""PDF text extraction with no third-party library.

A PDF is a graph of objects; the visible text lives inside content streams,
usually Flate-compressed, expressed as PostScript-ish operators. So the job is
three steps:

    1. find the streams                      (byte scanning)
    2. decompress them                       (zlib, stdlib)
    3. pull the arguments of the text-showing operators  (Tj TJ ' ")

Step 3 needs a real tokenizer rather than a regex, because a PDF literal
string may contain balanced parentheses and backslash escapes -- `(a (b) c)`
is ONE string, and `(a\)b)` is one string containing a close paren. A regex
gets both wrong, which is the usual reason a hand-rolled PDF reader returns
truncated text.

KNOWN LIMITS -- stated because a caller needs to know when to distrust output:
  * No font/CMap decoding. Text drawn with a subset or CID-keyed font can come
    back as the wrong characters. Simple fonts with WinAnsi/Standard encoding
    (what most text-generating tools emit) read correctly.
  * No OCR: a scanned page contains an image, not text, and yields nothing.
  * Layout is approximated, not reconstructed. Multi-column pages interleave.
"""

import re
import zlib

from . import pdfcmap
from .. import limits
from ..limits import StreamTooLarge, bounded_inflate


# Thresholds for the coverage guard below. Both must trip: measured across 53
# commercial PDFs, where the drop ratio alone flags 14 sound documents and a
# per-page floor alone flags the smallest legitimate one.
_COVERAGE_DROP_RATIO = 0.95
_COVERAGE_CHARS_PER_PAGE = 150


class UndecodableText(Exception):
    """Text was found but its characters cannot be recovered.

    Happens with CID-keyed fonts using Identity-H encoding and NO /ToUnicode
    map: the content stream holds glyph IDs private to an embedded font
    program, so recovering characters means parsing the CFF/TrueType cmap
    inside that font -- well beyond this library's scope.

    Raised rather than returning the bytes, because the alternative is
    emitting hundreds of kilobytes of mojibake into a caller's dataset. A
    loud failure is recoverable; silent corruption is not.
    """


class EncryptedPDF(Exception):
    """The PDF's streams are encrypted, so its text cannot be read.

    Raised rather than returning "" because a silent empty result in a batch
    job looks like an empty document, and the caller acts on that. Commercial
    PDFs are routinely encrypted with an EMPTY user password purely to set
    permission flags -- readable in any viewer, still encrypted on disk.
    Decrypting them needs RC4 and AES, and AES is not in the standard library,
    so it is out of scope for this tool by design.
    """

# Operators that draw text. Tj and TJ take the string(s) before them;
# ' and " also imply a line break first.
_SHOW_ONE = b"Tj"
_SHOW_ARRAY = b"TJ"
_SHOW_NEXTLINE = (b"'", b'"')

# Operators that move the cursor to a new line, i.e. where we emit "\n".
_NEWLINE_OPS = (b"Td", b"TD", b"T*", b"ET")

_STREAM_RE = re.compile(rb"stream\r?\n?", re.DOTALL)


def _iter_streams(data):
    """Yield the raw bytes of every `stream ... endstream`, with its dict.

    Scans rather than parsing the xref table on purpose: a damaged or
    linearized xref is common in the wild, and the streams are still there.
    """
    pos = 0
    while True:
        m = _STREAM_RE.search(data, pos)
        if not m:
            return
        # The object dictionary sits immediately before the `stream` keyword.
        dict_start = data.rfind(b"<<", max(0, m.start() - 4096), m.start())
        header = data[dict_start:m.start()] if dict_start != -1 else b""
        end = data.find(b"endstream", m.end())
        if end == -1:
            return
        yield header, m.end(), end          # offsets: the caller sizes before it copies
        pos = end + len(b"endstream")


def _decompress(header, raw):
    """Return usable stream bytes, or None if this stream is not for us.

    Images, fonts and metadata also live in streams; we let them fail the
    decode or fall out later when no text operators are found.
    """
    if b"/FlateDecode" in header:
        # Bounded: a PDF stream declares no output size, so the only way to
        # refuse a decompression bomb is to inflate with a ceiling. A truncated
        # or junk-terminated stream still yields whatever decompressed before
        # the error, rather than costing the whole page.
        data, truncated = bounded_inflate(raw)
        if data is None:
            return None
        if truncated:
            # Refused, not truncated. Keeping what fit looked like a result:
            # a 1 MB bomb came back as 64 MB of mostly whitespace, exit 0,
            # after 12.5 s of tokenizing it.
            raise StreamTooLarge(
                f"a content stream inflates past the "
                f"{len(data) // (1024 * 1024)} MB per-stream limit: a "
                f"decompression bomb rather than a document")
        return data
    if b"/Filter" in header:
        # DCTDecode (JPEG), CCITTFax, JBIG2, LZW and friends: not text we can
        # reach without a codec. Skip rather than emit garbage.
        return None
    return raw


def _read_literal_string(buf, i):
    r"""Read a `(...)` string starting at buf[i] == '('. Returns (bytes, next_i).

    Tracks paren depth and honours backslash escapes, including the \ooo
    octal form. This is the part a regex cannot do.
    """
    assert buf[i:i + 1] == b"("
    i += 1
    depth = 1
    out = bytearray()
    while i < len(buf):
        c = buf[i:i + 1]
        if c == b"\\":
            nxt = buf[i + 1:i + 2]
            simple = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
                      b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
            if nxt in simple:
                out += simple[nxt]
                i += 2
            elif nxt.isdigit():
                octal = b""
                j = i + 1
                while j < len(buf) and len(octal) < 3 and buf[j:j + 1].isdigit():
                    octal += buf[j:j + 1]
                    j += 1
                try:
                    out.append(int(octal, 8) & 0xFF)
                except ValueError:
                    pass
                i = j
            elif nxt in (b"\n", b"\r"):
                # Backslash-newline is a line continuation: emit nothing.
                i += 2
            else:
                out += nxt
                i += 2
            continue
        if c == b"(":
            depth += 1
            out += c
        elif c == b")":
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
            out += c
        else:
            out += c
        i += 1
    return bytes(out), i


def _read_hex_string(buf, i):
    """Read a `<48656C>` hex string. Returns (bytes, next_i)."""
    assert buf[i:i + 1] == b"<"
    end = buf.find(b">", i + 1)
    if end == -1:
        return b"", len(buf)
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", buf[i + 1:end])
    if len(digits) % 2:
        digits += b"0"          # spec says pad a trailing odd nibble with 0
    try:
        return bytes.fromhex(digits.decode("ascii")), end + 1
    except ValueError:
        return b"", end + 1


def _is_garbage(text, max_control_ratio=0.2):
    """True when a decoded run is glyph IDs rather than characters.

    Applied per STRING, not per document. A page often mixes a decorative
    heading in an unmapped font with body text that decodes perfectly; judging
    the whole document would throw the body away to avoid the heading.
    """
    if not text:
        return False
    control = sum(1 for c in text if not c.isprintable() and c not in "\n\r\t ")
    return (control / len(text)) > max_control_ratio


def _decode_string(raw, cmap=None):
    """Bytes of a PDF string -> str, through a /ToUnicode CMap when there is one.

    Without a CMap, latin-1 is the pragmatic choice: it maps WinAnsiEncoding's
    low range correctly and never raises, so a surprising byte degrades to a
    wrong character instead of losing the whole page.
    """
    if cmap:
        mapped = pdfcmap.decode_with_cmap(raw, cmap)
        if mapped is not None:
            return mapped
    return raw.decode("latin-1", errors="replace")


def _extract_content(buf, cmaps=None, tally=None):
    """Pull text out of one decompressed content stream, in drawing order.

    `cmaps` maps font resource names to /ToUnicode tables. When the active
    font has one, string bytes are glyph codes and must be translated through
    it; without one, the bytes are an 8-bit encoding and decode directly.
    """
    cmaps = cmaps or {}
    tally = tally if tally is not None else [0, 0]   # [kept chars, dropped chars]
    out = []
    pending = []          # strings collected since the last operator
    active = None         # CMap of the font selected by the last Tf
    last_name = None      # most recent /Name, which Tf consumes
    i = 0
    n = len(buf)
    while i < n:
        c = buf[i:i + 1]
        if c == b"(":
            s, i = _read_literal_string(buf, i)
            decoded = _decode_string(s, active)
            if _is_garbage(decoded):
                tally[1] += len(decoded)
            else:
                tally[0] += len(decoded)
                pending.append(decoded)
            continue
        if c == b"<" and buf[i + 1:i + 2] != b"<":
            s, i = _read_hex_string(buf, i)
            decoded = _decode_string(s, active)
            if _is_garbage(decoded):
                tally[1] += len(decoded)
            else:
                tally[0] += len(decoded)
                pending.append(decoded)
            continue
        if c == b"/":
            # A name object: remember it in case the next operator is Tf.
            j = i + 1
            while j < n and (buf[j:j + 1].isalnum() or buf[j:j + 1] in b"+-._#"):
                j += 1
            last_name = buf[i + 1:j].decode("latin-1")
            i = j
            continue
        if c.isalpha() or c in (b"'", b'"'):
            j = i
            while j < n and (buf[j:j + 1].isalpha() or buf[j:j + 1] in (b"*", b"'", b'"')):
                j += 1
            op = buf[i:j]
            if op == b"Tf":
                # `/F2 11 Tf` selects font F2; its CMap governs every string
                # drawn until the next Tf.
                if last_name is not None:
                    active = cmaps.get(last_name) or cmaps.get("*")
                pending = []
                i = j
                continue
            if op in (_SHOW_ONE, _SHOW_ARRAY) or op in _SHOW_NEXTLINE:
                if op in _SHOW_NEXTLINE and out and not out[-1].endswith("\n"):
                    out.append("\n")
                out.append("".join(pending))
                pending = []
            elif op in _NEWLINE_OPS:
                pending = []
                if out and not out[-1].endswith("\n"):
                    out.append("\n")
            else:
                pending = []
            i = j if j > i else i + 1
            continue
        i += 1
    return "".join(out)


def _is_plausible_text(text, sample=20000, max_control_ratio=0.05):
    """Heuristic guard against emitting mojibake.

    Correctly decoded prose contains essentially no control characters. Glyph
    IDs decoded as if they were characters contain a great many. Sampling the
    head keeps this cheap on large documents.
    """
    head = text[:sample]
    if not head:
        return True
    control = sum(1 for c in head
                  if not c.isprintable() and c not in "\n\r\t ")
    return (control / len(head)) <= max_control_ratio


def _is_encrypted(data):
    """True when the trailer names an /Encrypt dictionary.

    Checked against the trailer rather than the whole file so that the literal
    string "/Encrypt" appearing inside a content stream cannot cause a false
    positive on a perfectly readable document.
    """
    tail = data[-4096:] if len(data) > 4096 else data
    if re.search(rb"/Encrypt\s+\d+\s+\d+\s+R", tail):
        return True
    # Cross-reference streams put the trailer dict at the front of the xref
    # object, which may sit anywhere; fall back to a bounded global check.
    return bool(re.search(rb"trailer.{0,2048}?/Encrypt", data, re.DOTALL))


def why_empty(data):
    """(reason, detail) for a PDF that yielded no text.

    Only a PDF whose pages hold images is a likely scan. One that stops before
    its end-of-file marker was cut off in transit or damaged, and pointing its
    reader at OCR would send them the wrong way.
    """
    if b"%%EOF" not in data[-1024:]:
        return ("truncated_or_corrupt",
                "the PDF stops before its end-of-file marker: it was cut off or "
                "damaged, so its text could not be reached")
    if re.search(rb"/Subtype\s*/Image", data):
        return ("no_text_found",
                "the pages hold images but no text layer (a scan); reading it "
                "would need OCR, which this tool does not do")
    return ("no_text_found", "the PDF is intact but draws no text")


def extract_pdf(fh):
    """Text of a PDF, page order preserved as far as stream order allows."""
    data = fh.read() if hasattr(fh, "read") else fh
    if not data.startswith(b"%PDF"):
        # Some files carry junk before the header; find it rather than refuse.
        idx = data.find(b"%PDF")
        if idx > 0:
            data = data[idx:]
    if _is_encrypted(data):
        raise EncryptedPDF(
            "encrypted PDF (standard security handler); text is not readable "
            "without decryption, which this library does not implement")
    # The Flate cap, applied to a stream stored uncompressed. Without it a
    # 250 MB PDF riding in a 5 MB zip was indexed and tokenized byte by byte:
    # 38 s and 537 MB for one document. Checked by offsets, before anything is
    # copied or indexed, so a refusal costs one pass of find().
    for header, start, end in _iter_streams(data):
        if b"/Filter" not in header and end - start > limits.MAX_PDF_STREAM_BYTES:
            raise StreamTooLarge(
                f"an uncompressed content stream of {(end - start) // (1024 * 1024)} MB "
                f"is past the {limits.MAX_PDF_STREAM_BYTES // (1024 * 1024)} MB "
                f"per-stream limit")
    try:
        cmaps = pdfcmap.font_cmaps(data)
    except Exception:                      # noqa: BLE001
        # A malformed font table must not cost us the document's text.
        cmaps = {}
    pages = []
    saw_text_ops = False
    tally = [0, 0]            # [characters kept, characters dropped as glyph IDs]
    streams = 0
    for header, start, end in _iter_streams(data):
        body = _decompress(header, data[start:end])
        if not body:
            continue
        if b"Tj" not in body and b"TJ" not in body:
            continue          # not a text-bearing content stream
        saw_text_ops = True
        streams += 1
        text = _extract_content(body, cmaps, tally)
        if text.strip():
            pages.append(text)
    joined = "\n".join(pages)
    # Collapse the runs of blank lines that line-positioning operators create.
    result = re.sub(r"\n{3,}", "\n\n", joined).strip()
    if saw_text_ops and not result:
        # Text was drawn, but every run decoded to glyph IDs.
        raise UndecodableText(
            "PDF text uses fonts with no /ToUnicode map (typically CID-keyed "
            "Identity-H); characters cannot be recovered without parsing the "
            "embedded font program")

    # All-or-nothing was not enough. A 68-page book came back with 2,549
    # characters of mostly mojibake and exit code 0, because a handful of runs
    # decoded and the check above only fires when NONE do. That is the blank
    # row nobody notices, which is the thing this tool exists to prevent.
    #
    # Neither signal works alone. Dropping 95% of the characters is normal in
    # illustrated books -- one scored 0.915 recall against pdftotext having
    # dropped 94.9% -- so the drop ratio on its own flags 14 healthy documents.
    # A thin page is normal too: the invoice the test corpus generates is 85
    # characters on one page. Together they are specific: across 53 commercial
    # PDFs the pair fires once, on the document that is actually broken, and
    # the next-nearest file is eight times clear of the threshold.
    kept, dropped = tally
    total = kept + dropped
    if saw_text_ops and total and streams:
        drop_ratio = dropped / total
        per_page = len(result) / streams
        if drop_ratio >= _COVERAGE_DROP_RATIO and per_page < _COVERAGE_CHARS_PER_PAGE:
            raise UndecodableText(
                f"only {len(result):,} characters recovered from {streams:,} "
                f"text-bearing pages ({per_page:.0f} per page); "
                f"{drop_ratio * 100:.1f}% of the text decoded to glyph IDs "
                f"rather than characters. The fonts carry no usable "
                f"/ToUnicode map, so what little came back is not "
                f"trustworthy either")
    return result
