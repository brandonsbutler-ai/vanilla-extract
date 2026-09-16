r"""Tests for puretext.

Run: python3 -m unittest discover -s tests -v      (no pytest needed)

The fixtures are BUILT here rather than committed as binaries, so the suite
stays readable and there is nothing opaque in the repo. Building a docx by
hand is also the clearest possible statement of what the extractor expects.
"""

import io
import os
import sys
import unittest
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from puretext import UnsupportedFormat, extract, extract_archive  # noqa: E402
from puretext.formats import pdf, rtf                             # noqa: E402
from puretext.formats.markup import strip_html                    # noqa: E402
from puretext.formats.plain import decode_text                    # noqa: E402


def make_docx(paragraphs):
    """Minimal but real .docx: a zip with word/document.xml."""
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>' + body + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_pdf(text_ops, compress=True):
    """A PDF carrying one content stream, enough to exercise the tokenizer."""
    content = text_ops.encode("latin-1")
    if compress:
        stream = zlib.compress(content)
        filt = b"/Filter /FlateDecode "
    else:
        stream = content
        filt = b""
    return (b"%PDF-1.4\n1 0 obj\n<< " + filt +
            b"/Length " + str(len(stream)).encode() + b" >>\nstream\n" +
            stream + b"\nendstream\nendobj\n%%EOF\n")


class TestPDF(unittest.TestCase):
    def test_simple_tj(self):
        out = pdf.extract_pdf(make_pdf("BT (Hello world) Tj ET"))
        self.assertEqual(out, "Hello world")

    def test_tj_array_is_concatenated(self):
        out = pdf.extract_pdf(make_pdf("BT [(Hel) -20 (lo)] TJ ET"))
        self.assertEqual(out, "Hello")

    def test_balanced_parens_inside_string(self):
        """`(a (b) c)` is ONE string. A regex-based reader truncates here."""
        out = pdf.extract_pdf(make_pdf("BT (a (b) c) Tj ET"))
        self.assertEqual(out, "a (b) c")

    def test_escaped_close_paren(self):
        r"""`(a\)b)` is one string containing a literal ')'."""
        out = pdf.extract_pdf(make_pdf(r"BT (a\)b) Tj ET"))
        self.assertEqual(out, "a)b")

    def test_octal_escape(self):
        out = pdf.extract_pdf(make_pdf(r"BT (caf\351) Tj ET"))
        self.assertEqual(out, "caf\xe9")

    def test_hex_string(self):
        out = pdf.extract_pdf(make_pdf("BT <48656C6C6F> Tj ET"))
        self.assertEqual(out, "Hello")

    def test_odd_hex_nibble_is_padded(self):
        """The spec says a trailing odd nibble is padded with 0."""
        out = pdf.extract_pdf(make_pdf("BT <4A4> Tj ET"))
        self.assertEqual(out, "J@")

    def test_uncompressed_stream(self):
        out = pdf.extract_pdf(make_pdf("BT (plain) Tj ET", compress=False))
        self.assertEqual(out, "plain")

    def test_line_break_on_td(self):
        out = pdf.extract_pdf(make_pdf("BT (one) Tj 0 -12 Td (two) Tj ET"))
        self.assertEqual(out.split("\n"), ["one", "two"])

    def test_non_text_stream_ignored(self):
        """An image stream must not produce garbage text."""
        self.assertEqual(pdf.extract_pdf(make_pdf("q 100 0 0 100 0 0 cm Do Q")), "")

    def test_garbage_before_header(self):
        data = b"junk junk " + make_pdf("BT (found) Tj ET")
        self.assertEqual(pdf.extract_pdf(data), "found")

    def test_truncated_stream_does_not_raise(self):
        broken = make_pdf("BT (x) Tj ET")[:-30]
        pdf.extract_pdf(broken)          # must not raise


class TestOOXML(unittest.TestCase):
    def test_docx_paragraph_order(self):
        data = make_docx(["First line", "Second line", "Third"])
        self.assertEqual(extract(data, "a.docx"),
                         "First line\nSecond line\nThird")

    def test_docx_skips_empty_paragraphs(self):
        self.assertEqual(extract(make_docx(["only", "", "  "]), "a.docx"), "only")

    def test_misnamed_docx_still_reads(self):
        """Routing is by content, so a .docx called .txt still works."""
        self.assertEqual(extract(make_docx(["body"]), "notes.txt"), "body")


class TestMarkup(unittest.TestCase):
    def test_script_and_style_dropped(self):
        html = ("<html><head><style>p{color:red}</style></head><body>"
                "<script>var x=1;</script><p>Visible</p></body></html>")
        self.assertEqual(strip_html(html), "Visible")

    def test_block_elements_break_lines(self):
        self.assertEqual(strip_html("<p>one</p><p>two</p>").split("\n"),
                         ["one", "two"])

    def test_entities_decoded(self):
        self.assertEqual(strip_html("<p>a &amp; b &lt; c</p>"), "a & b < c")


class TestRTF(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(rtf.extract_rtf(rb"{\rtf1\ansi Hello world}"),
                         "Hello world")

    def test_font_table_is_not_output(self):
        r"""\fonttbl contents are metadata; font names must not leak into text."""
        doc = rb"{\rtf1{\fonttbl{\f0\froman Times New Roman;}}\f0 Body text}"
        out = rtf.extract_rtf(doc)
        self.assertNotIn("Times New Roman", out)
        self.assertIn("Body text", out)

    def test_par_becomes_newline(self):
        self.assertEqual(rtf.extract_rtf(rb"{\rtf1 a\par b}").split("\n"),
                         ["a", "b"])

    def test_hex_escape(self):
        self.assertEqual(rtf.extract_rtf(rb"{\rtf1 caf\'e9}"), "caf\xe9")


class TestPlain(unittest.TestCase):
    def test_utf8_bom_stripped(self):
        self.assertEqual(decode_text(b"\xef\xbb\xbfhi"), "hi")

    def test_cp1252_fallback(self):
        """0x92 is a smart quote in cp1252 and invalid in utf-8."""
        self.assertEqual(decode_text(b"it\x92s"), "it’s")

    def test_csv_delimiter_sniffed(self):
        out = extract(b"a;b;c\n1;2;3\n", "x.csv")
        self.assertEqual(out, "a\tb\tc\n1\t2\t3")

    def test_json_flattened_to_paths(self):
        out = extract(b'{"user":{"name":"Bo"},"tags":["x","y"]}', "x.json")
        self.assertIn("user.name: Bo", out)
        self.assertIn("tags[0]: x", out)


class TestDispatch(unittest.TestCase):
    def test_binary_is_rejected(self):
        with self.assertRaises(UnsupportedFormat):
            extract(b"\x00\x01\x02\x03binary\x00", "mystery.bin")

    def test_eml_detected_by_headers(self):
        raw = (b"From: a@b.c\r\nTo: d@e.f\r\nSubject: Hi\r\n"
               b"Content-Type: text/plain\r\n\r\nBody here\r\n")
        out = extract(raw, "msg.eml")
        self.assertIn("Subject: Hi", out)
        self.assertIn("Body here", out)


class TestArchive(unittest.TestCase):
    def test_zip_members_extracted_and_errors_reported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bundle.zip")
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("a.txt", "alpha")
                zf.writestr("b.docx", make_docx(["beta"]))
                zf.writestr("c.png", b"\x89PNG\r\n\x1a\n binary")
            results = list(extract_archive(path))
        texts = {name: text for name, text, err in results if text is not None}
        self.assertEqual(texts.get("a.txt"), "alpha")
        self.assertEqual(texts.get("b.docx"), "beta")
        # the image is skipped by extension, not reported as an error
        self.assertNotIn("c.png", texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
