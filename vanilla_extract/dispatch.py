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


def extract(data, filename=""):
    """Extract text from one document's bytes."""
    handler = sniff(data, filename)
    return handler(data)
