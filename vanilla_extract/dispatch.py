"""Pick the right extractor for a file.

Routing is by CONTENT first, extension second. That ordering is deliberate:
in any real corpus a meaningful fraction of files are misnamed -- a .doc that
is really RTF, a .xls that is really CSV, a .txt that is really a PDF. Trusting
the extension is the single most common reason a batch extraction job silently
produces empty output for part of its input.
"""

import io
import zipfile

from .formats import mail, markup, ooxml, pdf, plain, rtf


class UnsupportedFormat(Exception):
    """Raised when neither the content nor the extension identifies a handler."""


class NoTextFound(Exception):
    """A document was read and nothing came out of it.

    `reason` is one of empty_file, truncated_or_corrupt, limit_exceeded or
    no_text_found -- the same names the batch exceptions table uses -- and
    `detail` says which, in words. Raised by extract_file(require_text=True).
    """

    def __init__(self, reason, detail):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


# Extension -> handler, used only when the magic bytes are inconclusive.
_BY_EXT = {
    ".pdf": pdf.extract_pdf,
    ".docx": ooxml.extract_docx,
    ".pptx": ooxml.extract_pptx,
    ".xlsx": ooxml.extract_xlsx,
    ".odt": ooxml.extract_odt,
    ".eml": mail.extract_eml,
    ".mbox": mail.extract_mbox,
    ".rtf": rtf.extract_rtf,
    ".html": markup.extract_html,
    ".htm": markup.extract_html,
    ".xml": markup.extract_xml,
    ".csv": plain.extract_csv,
    ".tsv": plain.extract_csv,
    ".json": plain.extract_json,
    ".txt": plain.extract_text,
    ".md": plain.extract_text,
    ".log": plain.extract_text,
}

# Handlers whose format always starts with a fixed signature. If the signature
# checks above did not fire, the file is not one of these no matter what it is
# named, so the extension must not route to them.
_NEEDS_SIGNATURE = frozenset((
    pdf.extract_pdf, rtf.extract_rtf,
    ooxml.extract_docx, ooxml.extract_pptx, ooxml.extract_xlsx, ooxml.extract_odt,
))

# Members of an OOXML zip that identify which OOXML flavour it is.
_ZIP_MARKERS = (
    ("word/document.xml", ooxml.extract_docx),
    ("ppt/presentation.xml", ooxml.extract_pptx),
    ("xl/workbook.xml", ooxml.extract_xlsx),
    ("content.xml", ooxml.extract_odt),
)


def _sniff_zip(data):
    """Tell the OOXML flavours apart by what is inside the container."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return None
    for marker, handler in _ZIP_MARKERS:
        if marker in names:
            return handler
    return None


# Names that mean media or a binary rather than a document. ONE list, shared
# by the batch walk, the archive reader and the GUI pre-scan, so what is
# counted is what is processed. A name is only a claim, though: skip_reason()
# checks the bytes before believing it.
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp")
_KIND_BY_EXT = (
    (_IMAGE_EXT, "image"),
    ((".svg",), "vector image"),
    ((".mp3", ".mp4", ".mov", ".avi", ".wav"), "audio or video"),
    ((".woff", ".woff2", ".ttf", ".otf"), "font"),
    ((".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a",
      ".class", ".jar"), "compiled code or binary"),
    ((".db", ".sqlite", ".lock"), "database or lock file"),
)
SKIP_EXT = tuple(ext for exts, _kind in _KIND_BY_EXT for ext in exts)


def skip_reason(name, head, whole=None):
    """(reason, detail) for a file that is not read, or None to read it.

    Only a SKIP_EXT name can be skipped, and only when its bytes agree: a PDF,
    RTF or office document renamed .jpg carries its signature and is read like
    any other document. `head` is the first KB; `whole` is a zero-argument
    callable giving a path or the bytes, consulted only for a zip signature,
    because telling an office document from a .jar needs its member list.
    """
    lower = name.lower()
    if not lower.endswith(SKIP_EXT):
        return None
    if head.startswith((b"%PDF", rb"{\rtf")) or b"%PDF-" in head[:1024]:
        return None
    if head.startswith(b"PK") and whole is not None and zip_holds_document(whole()):
        return None
    kind = next(k for exts, k in _KIND_BY_EXT if lower.endswith(exts))
    if kind == "image":
        return ("image_no_text_layer",
                "an image holds pixels, not text; reading it would need OCR, "
                "which this tool does not do")
    return ("not_a_document", f"{kind}; not a document format this tool reads")


def zip_holds_document(source):
    """True when a zip -- a path or its bytes -- is an office document.

    Decided by what is INSIDE the container, never by its name, so a DOCX saved
    as .pdf is read as one document rather than walked as an archive of XML
    parts, and a plain archive saved as .docx is opened as the archive it is.
    """
    try:
        target = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
        with zipfile.ZipFile(target) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    return any(marker in names for marker, _handler in _ZIP_MARKERS)


def _looks_like_email(head):
    """An RFC 822 message starts with headers; check for the common ones."""
    first = head[:2048].lower()
    return any(f in first for f in (b"from:", b"subject:", b"message-id:",
                                    b"received:", b"mime-version:"))


def sniff(data, filename=""):
    """Return the handler for this content, or raise UnsupportedFormat.

    Content checks run in order of how distinctive the signature is.
    """
    head = data[:4096]

    if data[:4] == b"%PDF" or b"%PDF-" in head[:1024]:
        return pdf.extract_pdf
    if data[:2] == b"PK":
        handler = _sniff_zip(data)
        if handler:
            return handler
        # A zip we do not recognize: an archive, handled a level up.
        raise UnsupportedFormat("zip archive without a known document layout")
    if data[:5] == rb"{\rtf":
        return rtf.extract_rtf
    if head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        return markup.extract_html
    if head.lstrip()[:5] == b"<?xml":
        return markup.extract_xml
    if data[:5] == b"From " :
        return mail.extract_mbox
    if _looks_like_email(head):
        return mail.extract_eml

    # Content was inconclusive; fall back to the extension -- except where the
    # extension names a format that CANNOT be inconclusive.
    #
    # PDF, RTF and the OOXML family all begin with a fixed signature. Reaching
    # this point means that signature is absent, so the file is not that format
    # whatever it is called, and handing it to that reader produces a wrong
    # diagnosis rather than an error: a text export saved as report.pdf came
    # back "no_text_found", which the documentation describes as almost always
    # a scan with no text layer. It sent the reader looking for OCR for a file
    # whose text was sitting there in plain bytes.
    lower = filename.lower()
    for ext, handler in _BY_EXT.items():
        if lower.endswith(ext):
            if handler in _NEEDS_SIGNATURE:
                break          # the name is wrong about the format; keep going
            return handler

    # Last resort: if it decodes as text without NUL bytes, treat it as text.
    if b"\x00" not in head:
        return plain.extract_text
    raise UnsupportedFormat(f"unrecognized format: {filename or 'input'}")


def explain_empty(data, filename=""):
    """(reason, detail) for a document whose extraction came back empty.

    Four different failures used to share one label, "often a scan with no
    text layer": a 0-byte file, a PDF cut off mid-stream, an office document
    whose XML tripped the parser's entity-expansion limit, and a real scan.
    Only the last one needs OCR.
    """
    if not data:
        return ("empty_file", "the file is 0 bytes")
    try:
        handler = sniff(data, filename)
    except UnsupportedFormat:
        handler = None
    if handler is pdf.extract_pdf:
        return pdf.why_empty(data)
    if handler in (ooxml.extract_docx, ooxml.extract_pptx,
                   ooxml.extract_xlsx, ooxml.extract_odt):
        # The readers skip a part that will not parse, so a document whose
        # every part failed comes back empty. Say why the parts failed.
        import xml.etree.ElementTree as ET
        from .limits import Budget, read_member
        budget = Budget()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if not info.filename.endswith(".xml"):
                    continue
                try:
                    ET.fromstring(read_member(zf, info, budget))
                except ET.ParseError as exc:
                    if "amplification" in str(exc):
                        return ("limit_exceeded",
                                f"{info.filename} expands XML entities past the "
                                f"parser's safety limit (a 'billion laughs' "
                                f"construction); it was refused, not read")
                    return ("truncated_or_corrupt",
                            f"{info.filename} is not well-formed XML ({exc})")
    return ("no_text_found", "the document was read but holds no extractable text")


def extract(data, filename=""):
    """Extract text from one document's bytes."""
    handler = sniff(data, filename)
    return handler(data)
