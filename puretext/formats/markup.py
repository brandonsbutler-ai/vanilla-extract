"""HTML and XML.

html.parser handles the tag soup; the judgment is in which elements contribute
text. <script> and <style> bodies are code, not content, and block elements
need to produce line breaks or the output becomes one unreadable run.
"""

from html.parser import HTMLParser
import re
import xml.etree.ElementTree as ET

from .plain import decode_text

# Containers whose CONTENT is code or metadata rather than text. Depth-tracked,
# so they must have closing tags.
_SKIP = {"script", "style", "head", "noscript"}

# Void elements: no closing tag exists, so they must never be depth-tracked.
# Counting <meta> as a skip container latched the counter open and discarded
# every byte after it -- which is to say, every real HTML page and every
# HTML-only email body extracted as an empty string.
_VOID = {"meta", "link", "br", "img", "hr", "input", "source", "track",
         "area", "base", "col", "embed", "param", "wbr"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
          "section", "article", "header", "footer", "blockquote", "pre",
          "table", "ul", "ol", "td", "th"}


class _TextHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            # No end tag will ever arrive; only <br> contributes a break.
            if tag in _BLOCK:
                self.parts.append("\n")
            return
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        # <meta />, <br /> -- self-closing form. Never change skip depth.
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)

    def text(self):
        joined = "".join(self.parts)
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r" *\n *", "\n", joined)
        # A </p><p> pair emits two breaks; one line between blocks is what a
        # reader expects, so collapse any run of them to a single newline.
        return re.sub(r"\n+", "\n", joined).strip()


def strip_html(text):
    """Visible text of an HTML string."""
    parser = _TextHTMLParser()
    parser.feed(text)
    parser.close()
    return parser.text()


def extract_html(fh):
    data = fh.read() if hasattr(fh, "read") else fh
    # Use the encoding ladder rather than utf-8-or-replacement: a cp1252 page
    # (the normal output of Windows authoring tools) otherwise turns every
    # smart quote and accent into U+FFFD.
    return strip_html(decode_text(data))


def extract_xml(fh):
    """Text nodes of an XML document, one per line.

    Falls back to the HTML stripper when the XML is malformed, which is the
    common case for files that claim to be XML.
    """
    data = fh.read() if hasattr(fh, "read") else fh
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return strip_html(decode_text(data))
    lines = []
    for node in root.iter():
        if node.text and node.text.strip():
            lines.append(node.text.strip())
        if node.tail and node.tail.strip():
            lines.append(node.tail.strip())
    return "\n".join(lines)
