"""Office Open XML: .docx, .pptx, .xlsx.

All three are zip containers full of XML. The text lives in different places in
each, so the only shared machinery is "unzip, walk XML, collect the runs".

Uses zipfile + xml.etree.ElementTree from the standard library. Nothing else.
"""

import io
import re
import zipfile
import xml.etree.ElementTree as ET

# OOXML namespaces. Rather than register these and write qualified tag names,
# we match on the local part of the tag -- files in the wild disagree about
# prefixes far more often than they disagree about local names.
_WORD_PARA = "p"
_WORD_TEXT = "t"
_WORD_BREAK = ("br", "cr", "tab")


def _as_zip(data):
    """Accept bytes, a path, or a file object -- zipfile only accepts the last two."""
    if isinstance(data, (bytes, bytearray)):
        return io.BytesIO(data)
    return data


def _localname(tag):
    """'{http://...}p' -> 'p'. Tags without a namespace pass through."""
    return tag.rpartition("}")[2]


def _iter_text(elem):
    """Yield text from <w:t>/<a:t> nodes, and '\t'/'\n' for break elements.

    Walks in document order, which is what makes the output readable rather
    than a bag of words.
    """
    for node in elem.iter():
        name = _localname(node.tag)
        if name == _WORD_TEXT:
            if node.text:
                yield node.text
        elif name == "tab":
            yield "\t"
        elif name in ("br", "cr"):
            yield "\n"


def _paragraphs(root):
    """Text of each <w:p>/<a:p>, in order, skipping empties."""
    out = []
    for node in root.iter():
        if _localname(node.tag) == _WORD_PARA:
            text = "".join(_iter_text(node)).strip()
            if text:
                out.append(text)
    return out


def extract_docx(data):
    """Body text of a .docx, one paragraph per line.

    Headers, footers and footnotes live in separate parts; we read the ones
    that exist and put the body first, because that is what a caller means by
    "the text of this document".
    """
    lines = []
    with zipfile.ZipFile(_as_zip(data)) as zf:
        names = set(zf.namelist())
        ordered = ["word/document.xml"]
        ordered += sorted(n for n in names
                          if re.fullmatch(r"word/(header|footer|footnotes|endnotes)\d*\.xml", n))
        for name in ordered:
            if name not in names:
                continue
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError:
                continue
            lines.extend(_paragraphs(root))
    return "\n".join(lines)


def _slide_sort_key(name):
    """ppt/slides/slide10.xml must sort after slide9.xml, not before it."""
    m = re.search(r"(\d+)", name.rpartition("/")[2])
    return int(m.group(1)) if m else 0


def extract_pptx(data):
    """Slide text of a .pptx, with a marker line per slide.

    The marker is deliberate: a deck's text is almost useless without knowing
    which slide a line came from.
    """
    chunks = []
    with zipfile.ZipFile(_as_zip(data)) as zf:
        slides = [n for n in zf.namelist()
                  if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        for name in sorted(slides, key=_slide_sort_key):
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError:
                continue
            paras = _paragraphs(root)
            n = _slide_sort_key(name)
            chunks.append(f"--- slide {n} ---")
            chunks.extend(paras)
    return "\n".join(chunks)


def _shared_strings(zf):
    """xlsx stores repeated cell text once, in sharedStrings.xml, by index."""
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out = []
    for node in root:
        if _localname(node.tag) == "si":
            out.append("".join(_iter_text(node)))
    return out


def extract_xlsx(data):
    """Cell values of an .xlsx as tab-separated rows.

    Cells carrying t="s" hold an index into the shared-string table rather
    than a literal; resolving that is the whole trick to reading xlsx.
    """
    rows_out = []
    with zipfile.ZipFile(_as_zip(data)) as zf:
        shared = _shared_strings(zf)
        sheets = [n for n in zf.namelist()
                  if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
        for name in sorted(sheets, key=_slide_sort_key):
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError:
                continue
            for node in root.iter():
                if _localname(node.tag) != "row":
                    continue
                cells = []
                for cell in node:
                    if _localname(cell.tag) != "c":
                        continue
                    ctype = cell.get("t")
                    value = ""
                    for child in cell:
                        cname = _localname(child.tag)
                        if cname == "v":
                            value = child.text or ""
                        elif cname == "is":
                            value = "".join(_iter_text(child))
                    if ctype == "s" and value.isdigit():
                        idx = int(value)
                        value = shared[idx] if idx < len(shared) else ""
                    cells.append(value)
                if any(c.strip() for c in cells):
                    rows_out.append("\t".join(cells))
    return "\n".join(rows_out)


def extract_odt(data):
    """OpenDocument text. Different namespace, same shape as docx."""
    with zipfile.ZipFile(_as_zip(data)) as zf:
        try:
            root = ET.fromstring(zf.read("content.xml"))
        except (KeyError, ET.ParseError):
            return ""
    lines = []
    for node in root.iter():
        if _localname(node.tag) in ("p", "h"):
            text = "".join(node.itertext()).strip()
            if text:
                lines.append(text)
    return "\n".join(lines)
