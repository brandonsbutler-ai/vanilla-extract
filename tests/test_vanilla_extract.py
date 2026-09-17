r"""Tests for vanilla_extract.

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

from vanilla_extract import UnsupportedFormat, extract, extract_archive  # noqa: E402
from vanilla_extract.formats import pdf, rtf                             # noqa: E402
from vanilla_extract.formats.markup import strip_html                    # noqa: E402
from vanilla_extract.formats.plain import decode_text                    # noqa: E402


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



class TestBatch(unittest.TestCase):
    """Batch mode: the shape the work actually takes."""

    def _corpus(self, d):
        """A folder holding one readable file, one encrypted PDF, one image."""
        with open(os.path.join(d, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("Invoice #A-1001\nTotal: $42.50\n")
        with open(os.path.join(d, "b.docx"), "wb") as fh:
            fh.write(make_docx(["Invoice #B-2002", "Total: $7.00"]))
        # A PDF whose trailer declares encryption.
        body = make_pdf("BT (hidden) Tj ET")
        body = body.replace(b"%%EOF", b"trailer\n<< /Encrypt 9 0 R >>\n%%EOF")
        with open(os.path.join(d, "c.pdf"), "wb") as fh:
            fh.write(body)
        with open(os.path.join(d, "d.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n not really an image")

    def test_results_and_exceptions_are_separate(self):
        import tempfile
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            self._corpus(d)
            results, exceptions = run([d])
        names = sorted(os.path.basename(r["file"]) for r in results)
        self.assertEqual(names, ["a.txt", "b.docx"])
        # the image is skipped silently; the encrypted PDF is REPORTED
        reasons = {os.path.basename(e["file"]): e["reason"] for e in exceptions}
        self.assertEqual(reasons, {"c.pdf": "encrypted"})

    def test_unreadable_file_never_aborts_the_batch(self):
        """One bad document must not cost the caller the other 399."""
        import tempfile
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            self._corpus(d)
            with open(os.path.join(d, "e.bin"), "wb") as fh:
                fh.write(b"\x00\x01\x02\x03\x00\x01")
            results, exceptions = run([d])
        self.assertEqual(len(results), 2)
        self.assertIn("unsupported_format",
                      {e["reason"] for e in exceptions})

    def test_empty_document_is_an_exception_not_a_blank_row(self):
        """A scan with no text layer must be named, not returned as empty."""
        import tempfile
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "blank.txt"), "w", encoding="utf-8") as fh:
                fh.write("   \n\n  ")
            results, exceptions = run([d])
        self.assertEqual(results, [])
        self.assertEqual(exceptions[0]["reason"], "no_text_found")

    def test_field_extraction_uses_first_capture_group(self):
        import tempfile
        from vanilla_extract.batch import Field, run
        with tempfile.TemporaryDirectory() as d:
            self._corpus(d)
            fields = [Field.parse(r"invoice=Invoice\s*#([A-Z0-9-]+)"),
                      Field.parse(r"total=Total:\s*\$([0-9.]+)")]
            results, _ = run([d], fields=fields)
        by_name = {os.path.basename(r["file"]): r for r in results}
        self.assertEqual(by_name["a.txt"]["invoice"], "A-1001")
        self.assertEqual(by_name["a.txt"]["total"], "42.50")
        self.assertEqual(by_name["b.docx"]["invoice"], "B-2002")

    def test_missing_field_is_empty_not_absent(self):
        """A column must exist for every field on every row, or the CSV is ragged."""
        import tempfile
        from vanilla_extract.batch import Field, run
        with tempfile.TemporaryDirectory() as d:
            self._corpus(d)
            results, _ = run([d], fields=[Field.parse("nope=ZZZNOMATCHZZZ")])
        self.assertTrue(all("nope" in r for r in results))
        self.assertTrue(all(r["nope"] == "" for r in results))

    def test_bad_field_spec_is_rejected(self):
        from vanilla_extract.batch import Field
        with self.assertRaises(ValueError):
            Field.parse("no-equals-sign")

    def test_write_csv_emits_header_even_when_empty(self):
        import tempfile
        from vanilla_extract.batch import write_csv
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.csv")
            written = write_csv([], path, ["file", "reason", "detail"])
            self.assertEqual(written, 0)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read().strip(), "file,reason,detail")


class TestRecognize(unittest.TestCase):
    DOC = ("ACME SUPPLY CO\n"
           "Invoice Number: INV-1001\n"
           "Invoice Date: 2026-03-14\n"
           "Terms    Net 30\n"
           "Total: $1,299.00\n"
           "Contact: ap@northwind.example\n")

    def test_colon_form(self):
        from vanilla_extract.recognize import label_values
        self.assertEqual(label_values(self.DOC)["invoice number"], "INV-1001")

    def test_column_form(self):
        """Two-or-more spaces is the other way a flattened PDF renders a form."""
        from vanilla_extract.recognize import label_values
        self.assertEqual(label_values(self.DOC)["terms"], "Net 30")

    def test_label_on_its_own_line(self):
        from vanilla_extract.recognize import label_values
        pairs = label_values("Ship To\n42 Rue Morgue\n")
        self.assertEqual(pairs.get("ship to"), "42 Rue Morgue")

    def test_first_occurrence_wins(self):
        """A footer repeat must not overwrite the form's own statement."""
        from vanilla_extract.recognize import label_values
        pairs = label_values("Total: $10.00\nsome body text\nTotal: $99.99\n")
        self.assertEqual(pairs["total"], "$10.00")

    def test_labels_are_normalized(self):
        from vanilla_extract.recognize import normalize_label
        self.assertEqual(normalize_label("  Invoice   Number :"), "invoice number")

    def test_classify_types(self):
        from vanilla_extract.recognize import classify
        self.assertEqual(classify("$1,299.00"), "money")
        self.assertEqual(classify("2026-03-14"), "date_iso")
        self.assertEqual(classify("ap@northwind.example"), "email")
        self.assertEqual(classify("INV-1001"), "identifier")
        self.assertEqual(classify("Net 30"), "text")

    def test_infer_schema_ranks_by_support(self):
        from vanilla_extract.recognize import infer_schema
        common = "Invoice Number: A-1\nTotal: $5.00\n"
        rare = "Invoice Number: A-2\nTotal: $6.00\nRush Fee: $2.00\n"
        schema = infer_schema([common, common, common, rare])
        labels = [f["label"] for f in schema]
        self.assertIn("invoice number", labels)
        self.assertIn("total", labels)
        # present in 1 of 4 documents, below the 0.5 default
        self.assertNotIn("rush fee", labels)

    def test_infer_schema_honours_min_support(self):
        from vanilla_extract.recognize import infer_schema
        # Two form types mixed in one folder, each label in half the corpus.
        docs = ["Order Number: 1\n", "Order Number: 2\n",
                "Claim Number: 3\n", "Claim Number: 4\n"]
        self.assertEqual(len(infer_schema(docs, min_support=0.9)), 0)
        self.assertEqual(len(infer_schema(docs, min_support=0.5)), 2)

    def test_single_character_labels_are_rejected_as_noise(self):
        """`A: 1` is a list marker or an artefact far more often than a field."""
        from vanilla_extract.recognize import infer_schema
        self.assertEqual(infer_schema(["A: 1\n", "A: 2\n"], min_support=0.5), [])

    def test_extract_fields_fills_missing_with_blank(self):
        from vanilla_extract.recognize import extract_fields
        got = extract_fields(self.DOC, ["invoice number", "purchase order"])
        self.assertEqual(got["invoice number"], "INV-1001")
        self.assertEqual(got["purchase order"], "")

    def test_empty_corpus(self):
        from vanilla_extract.recognize import infer_schema
        self.assertEqual(infer_schema([]), [])


class TestReport(unittest.TestCase):
    ROWS = [{"file": "a.pdf", "characters": 12, "total": "$5.00", "text": "Total: $5.00"}]
    EXCS = [{"file": "b.pdf", "reason": "encrypted", "detail": "needs a password"}]

    def _render(self, **kw):
        import tempfile
        from vanilla_extract.report import write_report
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.html")
            write_report(self.ROWS, self.EXCS, path, **kw)
            with open(path, encoding="utf-8") as fh:
                return fh.read()

    def test_report_is_self_contained(self):
        """No external assets: it must work from a file:// URL, offline."""
        doc = self._render()
        for needle in ("src=\"http", "href=\"http", "cdn."):
            self.assertNotIn(needle, doc)

    def test_exceptions_are_shown_not_hidden(self):
        doc = self._render()
        self.assertIn("encrypted", doc)
        self.assertIn("needs a password", doc)

    def test_values_are_escaped(self):
        """A document containing markup must not be able to inject it."""
        import tempfile
        from vanilla_extract.report import write_report
        rows = [{"file": "x.pdf", "characters": 1,
                 "total": "<script>alert(1)</script>", "text": "t"}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.html")
            write_report(rows, [], path)
            doc = open(path, encoding="utf-8").read()
        self.assertNotIn("<script>alert(1)</script>", doc)
        self.assertIn("&lt;script&gt;", doc)

    def test_file_column_is_not_editable(self):
        """The filename identifies the row; editing it would break provenance."""
        doc = self._render()
        self.assertIn('<td class="file">a.pdf</td>', doc)


class TestProvenance(unittest.TestCase):
    """Originals kept, corrections appended, and both provable afterwards."""

    def _ws(self, d):
        from vanilla_extract.provenance import Workspace
        ws = Workspace(os.path.join(d, "case"))
        ws.create("0.2.0", [d])
        return ws

    def test_capture_records_both_hashes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.txt")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write("Total: $5.00")
            ws = self._ws(d)
            entry = ws.capture("a.txt", "Total: $5.00", source_path=src)
            self.assertEqual(len(entry["original_sha256"]), 64)
            self.assertEqual(len(entry["extracted_sha256"]), 64)
            self.assertTrue(os.path.isfile(os.path.join(ws.root, entry["original"])))
            self.assertTrue(os.path.isfile(os.path.join(ws.root, entry["extracted"])))

    def test_revisions_are_appended_never_replaced(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws = self._ws(d)
            cols = ["file", "total"]
            ws.add_revision([{"file": "a.pdf", "total": "$5.00"}], cols)
            ws.add_revision([{"file": "a.pdf", "total": "$6.00"}], cols)
            manifest = ws.load()
            self.assertEqual([r["revision"] for r in manifest["revisions"]], [1, 2])
            # the first revision's file still exists and still says $5.00
            first = os.path.join(ws.root, manifest["revisions"][0]["file"])
            self.assertIn("$5.00", open(first, encoding="utf-8").read())

    def test_diff_names_the_changed_cell(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws = self._ws(d)
            cols = ["file", "total", "customer"]
            ws.add_revision([{"file": "a.pdf", "total": "$5.00", "customer": "X"}], cols)
            entry = ws.add_revision(
                [{"file": "a.pdf", "total": "$6.00", "customer": "X"}], cols)
            self.assertEqual(entry["change_count"], 1)
            change = entry["changes_from_previous"][0]
            self.assertEqual((change["column"], change["from"], change["to"]),
                             ("total", "$5.00", "$6.00"))

    def test_verify_detects_a_modified_original(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.txt")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write("original")
            ws = self._ws(d)
            entry = ws.capture("a.txt", "original", source_path=src)
            self.assertEqual(ws.verify(), [])
            with open(os.path.join(ws.root, entry["original"]), "a",
                      encoding="utf-8") as fh:
                fh.write("tampered")
            problems = ws.verify()
            self.assertTrue(any("changed since capture" in p for p in problems))

    def test_archive_names_are_readable_and_collision_proof(self):
        from vanilla_extract.provenance import _safe_member
        a = _safe_member("/clients/acme/invoice.pdf")
        b = _safe_member("/clients/beta/invoice.pdf")
        self.assertTrue(a.startswith("invoice_") and a.endswith(".pdf"))
        self.assertNotEqual(a, b)

    def test_traversal_is_neutralized(self):
        from vanilla_extract.provenance import _safe_member
        for hostile in ("../../etc/passwd", "..\\..\\windows\\system32\\x.dll",
                        "/absolute/path/x.txt"):
            member = _safe_member(hostile)
            self.assertNotIn("..", member)
            self.assertNotIn("/", member)
            self.assertNotIn("\\", member)

    def test_manifest_write_is_atomic(self):
        """A crash mid-write must never leave a half-parsed manifest."""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws = self._ws(d)
            ws.capture("a.txt", "text")
            with open(ws.manifest_path, encoding="utf-8") as fh:
                json.load(fh)          # parses => the write completed or did not happen
            self.assertFalse(os.path.exists(ws.manifest_path + ".tmp"))


class TestHostileInput(unittest.TestCase):
    """This library reads files supplied by strangers. These are the attacks."""

    def _zip(self, name, payload):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr(name, payload)
        return buf.getvalue()

    def test_decompression_bomb_is_refused_before_allocating(self):
        from vanilla_extract.limits import ArchiveTooLarge
        bomb = self._zip("word/document.xml", b"A" * (200 * 1024 * 1024))
        with self.assertRaises(ArchiveTooLarge):
            extract(bomb, "bomb.docx")

    def test_a_genuinely_large_document_still_works(self):
        """The limit must refuse the absurd, not the merely big."""
        body = "".join(f"<w:p><w:r><w:t>Paragraph {i} of ordinary text.</w:t>"
                       f"</w:r></w:p>" for i in range(20000))
        xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
               'openxmlformats.org/wordprocessingml/2006/main"><w:body>'
               + body + "</w:body></w:document>")
        out = extract(self._zip("word/document.xml", xml), "big.docx")
        self.assertGreater(len(out), 500000)

    def test_archive_budget_stops_many_medium_members(self):
        """Members that are individually fine must not add up without limit."""
        from vanilla_extract.limits import ArchiveTooLarge, Budget, check_member

        class FakeInfo:
            filename = "x"
            file_size = 200 * 1024 * 1024
            compress_size = 100 * 1024 * 1024

        budget = Budget()
        for _ in range(5):
            check_member(FakeInfo(), budget)
        with self.assertRaises(ArchiveTooLarge):
            for _ in range(5):
                check_member(FakeInfo(), budget)

    def test_xxe_external_entity_is_not_resolved(self):
        """A document must not be able to read a file off the host."""
        xxe = (b'<?xml version="1.0"?>\n'
               b'<!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>\n'
               b"<d>&x;</d>")
        try:
            out = extract(xxe, "x.xml")
        except Exception:
            return          # refusing outright is also a correct outcome
        self.assertNotIn("root:", out)

    def test_entity_expansion_stays_bounded(self):
        """Billion laughs: expat caps expansion, so this documents the ceiling."""
        ents = ['<!ENTITY lol "lol">']
        for i in range(1, 8):
            prev = "lol" if i == 1 else f"lol{i-1}"
            ents.append(f'<!ENTITY lol{i} "{("&" + prev + ";") * 10}">')
        bomb = ('<?xml version="1.0"?>\n<!DOCTYPE lolz [\n' + "\n".join(ents)
                + "\n]>\n<root>&lol7;</root>").encode()
        try:
            out = extract(bomb, "b.xml")
        except Exception:
            return
        self.assertLess(len(out), 5 * 1024 * 1024)

    def test_recognizers_do_not_go_quadratic_on_a_near_miss(self):
        """Growth, not wall-clock: a quadratic pattern passes any loose budget.

        The old version of this test allowed 5 seconds for four inputs and one
        of those inputs was `("AB-" * 4000) + "1"` -- which ENDS IN A DIGIT, so
        the identifier lookahead succeeded and the match completed immediately.
        The shape that broke it is the same run with NO digit, which took 8.8
        seconds on 64 KB, and a run of email-legal characters with no `@`, which
        took 1.2 seconds. Neither was in the corpus.

        Doubling the input doubles a linear pattern's time and quadruples a
        quadratic one, so the ratio is the assertion. The absolute times are
        left out of it: those move with the machine, the ratio does not.
        """
        import time
        from vanilla_extract.recognize import DETECTORS, _detector
        shapes = {
            "uppercase hyphen run, no digit": lambda n: "AB" + "-AB" * (n // 3),
            "email-legal run, no at sign":    lambda n: "a.b-c_d" * (n // 7),
            "digits and commas, no currency": lambda n: "1," * (n // 2),
            "spaces":                         lambda n: " " * n,
            "dots":                           lambda n: "." * n,
        }
        for label, make in shapes.items():
            for name, regex, _ok in map(_detector, DETECTORS):
                times = []
                for n in (8000, 16000, 32000):
                    text = make(n)
                    start = time.perf_counter()
                    regex.search(text)
                    times.append(time.perf_counter() - start)
                slowest = max(times)
                if slowest < 0.002:
                    continue          # too fast to time; nothing to conclude
                ratio = max(times[i + 1] / times[i]
                            for i in range(len(times) - 1) if times[i] > 0)
                self.assertLess(
                    ratio, 3.0,
                    f"{name} grows {ratio:.1f}x per doubling on {label!r} "
                    f"({slowest * 1000:.0f} ms at 32k) -- linear is ~2x")

    def _pdf_with_unmapped_text(self, pages, decodable_runs, body="readable",
                                glyph_runs=60):
        """A PDF whose text is drawn in a font with no /ToUnicode map.

        `decodable_runs` ordinary strings are added so the all-or-nothing guard
        does NOT fire -- that is the whole point: a document can come back
        overwhelmingly empty while a handful of runs decode.
        """
        import zlib
        objs, offsets, parts = [], {}, [b"%PDF-1.5\n"]
        kids = " ".join(f"{4 + i} 0 R" for i in range(pages))
        def add(num, body):
            offsets[num] = sum(len(x) for x in parts)
            parts.append(f"{num} 0 obj\n{body}\nendobj\n".encode("latin-1"))
        add(1, "<< /Type /Catalog /Pages 2 0 R >>")
        add(2, f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>")
        add(3, "<< /Type /Font /Subtype /Type0 /BaseFont /AAAAAA+Sub "
               "/Encoding /Identity-H >>")
        num = 4
        for page in range(pages):
            # Glyph IDs: hex strings that decode to control characters.
            # Kept inside the control range on purpose: past U+0020 the code
            # points decode to printable characters, _is_garbage keeps them,
            # and a fixture meant to be 95% dropped quietly becomes 83%.
            glyphs = "".join(f"<{i % 31 + 1:04x}>" for i in range(1, glyph_runs))
            content = f"BT /F1 12 Tf 72 720 Td [{glyphs}] TJ ET\n"
            if page < decodable_runs:
                content += f"BT /F1 12 Tf 72 700 Td ({body}) Tj ET\n"
            z = zlib.compress(content.encode("latin-1"))
            offsets[num] = sum(len(x) for x in parts)
            parts.append(f"{num} 0 obj\n<< /Length {len(z)} /Filter /FlateDecode >>\n"
                         "stream\n".encode("latin-1") + z + b"\nendstream\nendobj\n")
            num += 1
            offsets[num] = sum(len(x) for x in parts)
            parts.append(f"{num} 0 obj\n<< /Type /Page /Parent 2 0 R "
                         f"/MediaBox [0 0 612 792] /Resources << /Font "
                         f"<< /F1 3 0 R >> >> /Contents {num - 1} 0 R >>\n"
                         "endobj\n".encode("latin-1"))
            num += 1
        start = sum(len(x) for x in parts)
        parts.append(f"xref\n0 {num}\n0000000000 65535 f \n".encode("latin-1"))
        for n in range(1, num):
            parts.append(b"%010d 00000 n \n" % offsets.get(n, 0))
        parts.append(f"trailer\n<< /Size {num} /Root 1 0 R >>\n"
                     f"startxref\n{start}\n%%EOF\n".encode("latin-1"))
        return b"".join(parts)

    def _coverage_signals(self, doc):
        """(drop ratio, characters per page) actually produced by a fixture.

        A fixture is a claim about the input. Measuring it here is what stops a
        test asserting that one clause matters while a different clause is
        doing the rejecting.
        """
        import collections
        from vanilla_extract.formats import pdf as pdf_mod
        counts = collections.Counter()
        real = pdf_mod._is_garbage
        def counting(text, *a, **kw):
            bad = real(text, *a, **kw)
            counts["dropped" if bad else "kept"] += len(text)
            return bad
        pdf_mod._is_garbage = counting
        try:
            try:
                out = pdf_mod.extract_pdf(doc)
            except pdf_mod.UndecodableText:
                out = None
        finally:
            pdf_mod._is_garbage = real
        total = counts["kept"] + counts["dropped"]
        return counts["dropped"] / max(total, 1), counts, out

    def test_a_mostly_undecodable_document_is_refused_not_returned(self):
        """A 68-page book came back as 2,549 characters of mojibake, rc 0.

        The guard only fired when EVERY run decoded to glyph IDs. A few runs
        decoding was enough to silence it, so the document became an ordinary
        results row -- the blank row nobody notices, which is the failure this
        tool exists to prevent.
        """
        from vanilla_extract.formats.pdf import extract_pdf, UndecodableText
        # Readable text on every page, but only a few characters of it, so the
        # result is NOT empty and the all-or-nothing guard cannot fire.
        doc = self._pdf_with_unmapped_text(pages=40, decodable_runs=40, body="ok")
        ratio, counts, _ = self._coverage_signals(doc)
        self.assertGreater(counts["kept"], 0,
                           "fixture decodes nothing, so the older guard fires "
                           "and this test would prove nothing about the new one")
        self.assertGreater(ratio, 0.95, f"fixture drop ratio is only {ratio:.3f}")
        with self.assertRaises(UndecodableText) as caught:
            extract_pdf(doc)
        message = str(caught.exception)
        self.assertIn("per page", message)
        self.assertRegex(message, r"9\d\.\d% of the text decoded to glyph IDs")

    def test_a_heavily_dropped_but_readable_document_is_not_refused(self):
        """The drop ratio alone is not evidence of anything.

        Illustrated books draw most of their text in fonts with no map and
        still read fine: one scored 0.915 recall against pdftotext having
        dropped 94.9 per cent of its characters. Refusing on the ratio alone
        would have thrown away fourteen sound documents out of fifty-three.

        This document drops well over 95 per cent and carries a full page of
        readable text on every page, so only the per-page clause can save it.
        """
        from vanilla_extract.formats.pdf import extract_pdf
        doc = self._pdf_with_unmapped_text(pages=12, decodable_runs=12,
                                           body="ordinary sentence " * 12,
                                           glyph_runs=4000)
        ratio, _counts, _out = self._coverage_signals(doc)
        self.assertGreater(ratio, 0.95,
                           f"fixture drop ratio is only {ratio:.3f}, so the "
                           f"drop clause rejects it and the per-page clause "
                           f"is not what this test is measuring")
        text = extract_pdf(doc)
        self.assertIn("ordinary sentence", text)
        self.assertGreater(len(text) / 12, 150,
                           "fixture does not clear the per-page threshold")

    def test_a_thin_but_sound_document_is_not_refused(self):
        """A short document is not a broken one.

        The corpus the E2E suite generates contains an 85-character
        single-page invoice; a per-page floor on its own would refuse it.
        """
        from vanilla_extract.formats.pdf import extract_pdf
        doc = self._pdf_with_unmapped_text(pages=1, decodable_runs=1)
        text = extract_pdf(doc.replace(b"/Encoding /Identity-H ", b""))
        self.assertIn("readable", text)

    def test_a_name_cannot_override_a_missing_signature(self):
        """A text export saved as report.pdf came back `no_text_found`.

        That reason is documented as almost always meaning a scan with no text
        layer, so the reader was sent looking for OCR for a file whose text was
        sitting in plain bytes. PDF, RTF and OOXML all begin with a fixed
        signature; if it is absent the file is not that format, whatever it is
        called, and the extension must not route to that reader.
        """
        from vanilla_extract.dispatch import sniff, extract
        from vanilla_extract.formats import plain, pdf, rtf as rtf_mod, ooxml
        for data, name in ((b"Invoice: T1\nTotal: $8.00\n", "report.pdf"),
                           (b"a,b\n1,2\n", "sheet.pdf"),
                           (b"plain words", "note.rtf"),
                           (b"not a zip at all", "deck.docx")):
            self.assertIs(sniff(data, name), plain.extract_text,
                          f"{name} with no signature routed to the wrong reader")
        self.assertEqual(extract(b"Invoice: T1\n", "report.pdf").strip(),
                         "Invoice: T1")

    def test_a_real_signature_still_wins_over_the_name(self):
        """The fix must not stop a correctly named file from being read."""
        from vanilla_extract.dispatch import sniff
        from vanilla_extract.formats import pdf, rtf as rtf_mod, plain, markup
        self.assertIs(sniff(b"%PDF-1.4\n%x\n", "x.pdf"), pdf.extract_pdf)
        self.assertIs(sniff(rb"{\rtf1 hi}", "x.rtf"), rtf_mod.extract_rtf)
        self.assertIs(sniff(b"a,b\n1,2\n", "x.csv"), plain.extract_csv)
        self.assertIs(sniff(b'{"a": 1}', "x.json"), plain.extract_json)
        self.assertIs(sniff(b"<html><p>h</p></html>", "x.txt"), markup.extract_html)

    def test_rtf_whitespace_cleanup_is_linear(self):
        """`[ \\t]+\\n` restarted at every blank: 1.9 s on 64 KB of spaces.

        An RTF file can contain a long run of blanks with no newline after it,
        and the cleanup pass walked the whole run, failed, and began again one
        character later.
        """
        import time
        from vanilla_extract.formats.rtf import extract_rtf
        times = []
        for n in (16000, 32000, 64000):
            doc = (r"{\rtf1\ansi " + " " * n + "text}").encode()
            start = time.perf_counter()
            self.assertEqual(extract_rtf(doc), "text")
            times.append(time.perf_counter() - start)
        if max(times) > 0.002:
            ratio = max(times[i + 1] / times[i] for i in range(2) if times[i] > 0)
            self.assertLess(ratio, 3.0,
                            f"whitespace cleanup grows {ratio:.1f}x per doubling")

    def test_an_identifier_must_contain_a_digit(self):
        """`AB-Z--0911` returned `AB-Z`, which has no digit in it.

        The requirement was written as the lookahead (?=[A-Z0-9-]*\\d), whose
        character class includes the hyphen -- so it scanned past a double
        hyphen to a digit the match itself can never reach, because each
        `-[A-Z0-9]+` needs a character after its hyphen.
        """
        from vanilla_extract.recognize import classify, find_values
        for value in ("AB-Z--0911", "REF-NO--2026", "ALL-CAPS--7"):
            self.assertEqual(classify(value), "text",
                             f"{value!r} classified as an identifier")
        self.assertEqual(
            find_values("see INV-4471 and REF-NO--2026 and PO-77", "identifier"),
            ["INV-4471", "PO-77"])
        for value in ("INV-4471", "PO-2026-118", "X1-Y2"):
            self.assertEqual(classify(value), "identifier")


class TestReviewRegressions(unittest.TestCase):
    """One test per finding from the 2026-09-16 review. None of these may return."""

    def test_rtf_generator_group_does_not_swallow_the_document(self):
        r"""#1 -- `{\*\generator}` double-pushed the drop stack. Word emits it always."""
        from vanilla_extract.formats import rtf
        doc = rb"{\rtf1\ansi{\*\generator Riched20 10.0.19041;}\pard Body text here\par}"
        self.assertEqual(rtf.extract_rtf(doc), "Body text here")

    def test_rtf_starred_listtable_does_not_swallow_the_document(self):
        from vanilla_extract.formats import rtf
        self.assertEqual(rtf.extract_rtf(rb"{\rtf1{\*\listtable{\list x}}Body}"), "Body")

    def test_html_meta_does_not_latch_the_skip_counter(self):
        """#2 -- a void element in the skip set discarded every real web page."""
        doc = ("<html><head><meta charset='utf-8'><title>t</title></head>"
               "<body><p>Visible body</p></body></html>")
        self.assertEqual(strip_html(doc), "Visible body")

    def test_html_link_between_paragraphs_keeps_both(self):
        self.assertEqual(strip_html("<p>a</p><link rel=x><p>b</p>").split("\n"), ["a", "b"])

    def test_report_json_cannot_break_out_of_the_script_block(self):
        """#3 -- `</script>` in a document's text was an XSS into the client's browser."""
        import tempfile
        from vanilla_extract.report import write_report
        payload = "</script><img src=x onerror=alert(1)>"
        rows = [{"file": "evil.pdf", "characters": 1, "v": payload, "text": payload}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.html")
            write_report(rows, [], path)
            doc = open(path, encoding="utf-8").read()
        self.assertNotIn("</script><img", doc)
        self.assertNotIn("<img src=x", doc)

    def test_pdf_object_index_is_not_quadratic(self):
        """#4 -- objects with no `endobj` sliced to end-of-file, n^2 in time and memory."""
        import time
        from vanilla_extract.formats import pdfcmap
        crafted = b"%PDF-1.4\n" + b"".join(
            f"{i} 0 obj\n<</X 1>>\n".encode() for i in range(20000))
        start = time.time()
        pdfcmap.build_object_index(crafted)
        self.assertLess(time.time() - start, 3.0)

    def test_pdf_flate_stream_is_capped(self):
        """#5 -- the primary format was the one decompression path with no ceiling."""
        import zlib
        from vanilla_extract.formats import pdf
        from vanilla_extract.limits import MAX_PDF_STREAM_BYTES
        stream = zlib.compress(b"A" * (MAX_PDF_STREAM_BYTES * 4))
        doc = (b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode /Length "
               + str(len(stream)).encode() + b" >>\nstream\n" + stream
               + b"\nendstream\nendobj\n%%EOF\n")
        pdf.extract_pdf(doc)          # must return, not exhaust memory

    def test_odd_length_hex_does_not_disable_every_font(self):
        """#6 -- one malformed CMap entry made a whole readable PDF 'undecodable'."""
        from vanilla_extract.formats import pdfcmap
        parsed = pdfcmap._parse_cmap(
            b"begincmap\n1 beginbfchar\n<041> <0042>\nendbfchar\nendcmap")
        self.assertTrue(parsed)

    def test_field_with_non_participating_group_does_not_abort_the_batch(self):
        """#7 -- m.group(1) is None for `(a)|b`, and .strip() killed the whole run."""
        import tempfile
        from vanilla_extract.batch import Field, run
        with tempfile.TemporaryDirectory() as d:
            for name in ("a.txt", "b.txt"):
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write("TOTAL DUE\n")
            field = Field.parse(r"total=Total:\s*\$([0-9.]+)|TOTAL DUE")
            results, _ = run([d], fields=[field])
        self.assertEqual(len(results), 2)

    def test_csv_formula_injection_is_neutralized(self):
        """#8 -- untrusted text went straight into a spreadsheet the client opens."""
        import csv as _csv
        import tempfile
        from vanilla_extract.batch import write_csv
        rows = [{"file": "evil.pdf", "v": "=cmd|' /C calc'!A0"},
                {"file": "b.pdf", "v": "@SUM(1+1)"}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "o.csv")
            write_csv(rows, path, ["file", "v"])
            values = [r["v"] for r in _csv.DictReader(open(path, encoding="utf-8"))]
        self.assertTrue(all(v.startswith("'") for v in values))

    def test_csv_with_an_enormous_field_still_returns_something(self):
        """#9 -- csv.Error escaped and lost the file entirely."""
        blob = "x" * 200000
        out = extract(f"a,b\n1,{blob}\n".encode(), "big.csv")
        self.assertTrue(out)

    def test_html_uses_the_encoding_ladder(self):
        """#10 -- cp1252 smart quotes became U+FFFD on every Windows-authored page."""
        out = strip_html_bytes(b"<p>it\x92s here</p>")
        self.assertIn("\u2019", out)

    def test_zip_inside_a_scanned_folder_is_not_silently_dropped(self):
        """#12 -- an archive found by walking produced no row and no exception."""
        import tempfile
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write("plain")
            with zipfile.ZipFile(os.path.join(d, "bundle.zip"), "w") as zf:
                zf.writestr("inner.txt", "inside the archive")
            results, exceptions = run([d])
        texts = " ".join(r.get("text", "") for r in results)
        self.assertIn("inside the archive", texts)

    def test_recapture_after_a_document_changes_keeps_both(self):
        """#13 -- the second capture overwrote the first and broke --verify."""
        import tempfile
        from vanilla_extract.provenance import Workspace
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.txt")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write("one")
            ws = Workspace(os.path.join(d, "case"))
            ws.create("0.2.0", [d])
            ws.capture("a.txt", "one", source_path=src)
            with open(src, "w", encoding="utf-8") as fh:
                fh.write("two")
            ws.capture("a.txt", "two", source_path=src)
            manifest = ws.load()
            self.assertEqual(len(manifest["documents"]), 2)
            self.assertEqual(len({x["original"] for x in manifest["documents"]}), 2)
            self.assertEqual(ws.verify(), [])
            ws.capture("a.txt", "two", source_path=src)      # idempotent
            self.assertEqual(len(ws.load()["documents"]), 2)

    def test_deeply_nested_json_falls_back_to_raw_text(self):
        """#14 -- RecursionError escaped instead of the documented fallback."""
        out = extract(b"[" * 3000, "deep.json")
        self.assertTrue(out)


def strip_html_bytes(data):
    from vanilla_extract.formats.markup import extract_html
    return extract_html(data)



class TestFileInfo(unittest.TestCase):
    """File state as found -- the chain-of-custody half of provenance."""

    def test_records_the_fields_a_datasheet_needs(self):
        import tempfile
        from vanilla_extract.fileinfo import stat_record
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("hello")
            r = stat_record(p)
        for field in ("name", "extension", "size", "size_bytes", "modified",
                      "accessed", "permissions", "mode_octal", "owner_uid",
                      "is_symlink", "hard_links", "inode", "filesystem",
                      "captured_at", "captured_on"):
            self.assertIn(field, r, field)
        self.assertEqual(r["size_bytes"], 5)
        self.assertEqual(r["extension"], "txt")
        self.assertTrue(r["permissions"].startswith("-"))

    def test_creation_time_is_never_faked_from_ctime(self):
        """ctime is inode-change time on Linux. Substituting it would be a lie."""
        import tempfile
        from vanilla_extract.fileinfo import stat_record
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("x")
            r = stat_record(p)
        # Either a real creation time with a stated source, or empty with a reason.
        self.assertTrue(r["created_source"])
        if r["created"] is None:
            self.assertIn("unavailable", r["created_source"])
        else:
            self.assertNotIn("inode", r["created_source"])
        self.assertIn("inode_changed", r)      # reported separately, correctly named

    def test_synthetic_ownership_is_flagged_not_asserted(self):
        """An NTFS/exFAT mount reports mount options, not file attributes."""
        from vanilla_extract import fileinfo
        real = fileinfo.filesystem_of
        try:
            fileinfo.filesystem_of = lambda p: "fuseblk"
            fileinfo._FS_CACHE.clear()
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "a.txt")
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write("x")
                r = fileinfo.stat_record(p)
            self.assertFalse(r["ownership_reliable"])
            if r["owner"]:
                self.assertIn("mount options", r["owner"])
        finally:
            fileinfo.filesystem_of = real
            fileinfo._FS_CACHE.clear()

    def test_autofs_never_shadows_the_real_filesystem(self):
        """Two mounts can claim one path; the later real one is the answer."""
        from vanilla_extract import fileinfo
        table = fileinfo._mount_table()
        points = [m for m, _ in table]
        self.assertEqual(len(points), len(set(points)), "duplicate mount points")
        self.assertNotIn("autofs", [f for m, f in table
                                    if m == "/mnt/volume"])

    def test_symlink_is_reported_with_its_target(self):
        import tempfile
        from vanilla_extract.fileinfo import stat_record
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "real.txt")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("x")
            link = os.path.join(d, "link.txt")
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            r = stat_record(link)
        self.assertTrue(r["is_symlink"])
        self.assertEqual(r["symlink_target"], target)

    def test_zip_member_metadata_comes_from_the_entry(self):
        """A member's timestamp can predate the archive by years."""
        import tempfile
        from vanilla_extract.fileinfo import zip_member_record
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.zip")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                info = zipfile.ZipInfo("old/doc.txt", date_time=(2011, 3, 4, 5, 6, 7))
                info.external_attr = 0o644 << 16
                zf.writestr(info, "content here")
                got = zf.getinfo("old/doc.txt")
                r = zip_member_record(got, path)
        self.assertTrue(r["modified"].startswith("2011-03-04"))
        self.assertEqual(r["permissions"], "-rw-r--r--")   # no leading '?'
        self.assertEqual(r["source"], "zip-central-directory")
        self.assertFalse(r["ownership_reliable"])
        self.assertTrue(r["crc32"])

    def test_human_size(self):
        from vanilla_extract.fileinfo import human_size
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(999), "999 B")
        self.assertEqual(human_size(1024), "1.0 KB")
        self.assertEqual(human_size(5 * 1024 * 1024), "5.0 MB")


class TestDatasheet(unittest.TestCase):
    def test_batch_returns_a_row_per_candidate_including_failures(self):
        import tempfile
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "ok.txt"), "w", encoding="utf-8") as fh:
                fh.write("Ref: A-1")
            body = make_pdf("BT (x) Tj ET").replace(
                b"%%EOF", b"trailer\n<< /Encrypt 9 0 R >>\n%%EOF")
            with open(os.path.join(d, "locked.pdf"), "wb") as fh:
                fh.write(body)
            results, exceptions, sheet = run([d], collect_metadata=True)
        names = {r["name"]: r["read_result"] for r in sheet}
        self.assertEqual(names.get("ok.txt"), "read")
        self.assertEqual(names.get("locked.pdf"), "encrypted")
        self.assertEqual(len(sheet), 2)

    def test_html_datasheet_is_self_contained_and_sortable(self):
        import tempfile
        from vanilla_extract.fileinfo import stat_record
        from vanilla_extract.report import write_datasheet
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("x")
            rows = [stat_record(p)]
            out = os.path.join(d, "sheet.html")
            write_datasheet(rows, out, columns=["name", "size", "permissions"])
            doc = open(out, encoding="utf-8").read()
        self.assertNotIn('src="http', doc)
        self.assertIn("data-col=", doc)
        self.assertIn('id="q"', doc)
        self.assertIn("parseFloat", doc)

    def test_datasheet_values_are_escaped(self):
        import tempfile
        from vanilla_extract.report import write_datasheet
        rows = [{"name": "<script>alert(1)</script>", "size": "1 B"}]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "s.html")
            write_datasheet(rows, out, columns=["name", "size"])
            doc = open(out, encoding="utf-8").read()
        self.assertNotIn("<script>alert(1)</script>", doc)
        self.assertIn("&lt;script&gt;", doc)

    def test_workspace_records_state_when_found(self):
        import tempfile
        from vanilla_extract.batch import run
        from vanilla_extract.provenance import Workspace
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            os.makedirs(src)
            with open(os.path.join(src, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write("Ref: A-1")
            ws = Workspace(os.path.join(d, "case"))
            ws.create("0.2.0", [src])
            run([src], workspace=ws)
            doc = ws.load()["documents"][0]
        self.assertIsNotNone(doc.get("state_when_found"))
        self.assertIn("permissions", doc["state_when_found"])
        self.assertIn("modified", doc["state_when_found"])


class TestCorruptArchiveRegression(unittest.TestCase):
    """A corrupt archive fell between is_zipfile() and the skip list."""

    def test_corrupt_zip_is_reported_not_silently_skipped(self):
        import tempfile
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "good.txt"), "w", encoding="utf-8") as fh:
                fh.write("Ref: OK-1")
            with open(os.path.join(d, "corrupt.zip"), "wb") as fh:
                fh.write(b"PK\x03\x04" + b"\x00" * 40 + b"garbage")
            results, exceptions = run([d])
        seen = {os.path.basename(r["file"].split("!")[0]) for r in results}
        seen |= {os.path.basename(e["file"].split("!")[0]) for e in exceptions}
        self.assertIn("corrupt.zip", seen)
        self.assertEqual(len(results), 1)

    def test_valid_zip_is_still_recursed_not_reported(self):
        """The fix must not turn working archives into exceptions."""
        import tempfile
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            with zipfile.ZipFile(os.path.join(d, "valid.zip"), "w") as zf:
                zf.writestr("in.txt", "Ref: IN-1")
            results, exceptions = run([d])
        self.assertEqual(exceptions, [])
        self.assertTrue(any("in.txt" in r["file"] for r in results))


class TestPostRenameReviewRegressions(unittest.TestCase):
    """One test per finding from the post-rename review, 2026-09-16."""

    def test_duplicate_zip_names_do_not_hide_a_member(self):
        """#2 -- getinfo(name) keeps only the LAST entry, so the first vanished."""
        import tempfile
        import warnings
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dup.zip")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with zipfile.ZipFile(path, "w") as zf:
                    zf.writestr("doc.txt", "FIRST member content")
                    zf.writestr("doc.txt", "SECOND member content")
            results, _ = run([path])
        texts = " ".join(r["text"] for r in results)
        self.assertIn("FIRST member content", texts)
        self.assertIn("SECOND member content", texts)
        self.assertEqual(len(results), 2)

    def test_corrupt_archive_named_directly_does_not_abort(self):
        """#3 -- is_zipfile only checks the EOCD; ZipFile then raised in _walk."""
        import tempfile
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bad.zip")
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("a.txt", "hello")
            raw = bytearray(open(path, "rb").read())
            i = raw.find(b"PK\x01\x02")
            raw[i:i + 4] = b"PK\x09\x09"        # corrupt the central directory
            with open(path, "wb") as fh:
                fh.write(raw)
            results, exceptions = run([path])      # must not raise
        self.assertEqual(results, [])
        self.assertTrue(exceptions)

    def test_report_export_includes_a_header_row(self):
        """#1 -- without it, --import-csv ate the first document as field names."""
        import tempfile
        from vanilla_extract.report import write_report
        rows = [{"file": "a.pdf", "characters": 1, "total": "$5.00", "text": "t"}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.html")
            write_report(rows, [], path)
            doc = open(path, encoding="utf-8").read()
        self.assertIn('id="resultshead"', doc)
        self.assertIn('data-col="file"', doc)
        # the export must gather the header, not just the tbody
        export_js = doc.split("export")[1][:500]
        self.assertIn("resultshead", export_js)

    def test_workspace_keeps_the_original_of_an_unreadable_document(self):
        """#5 -- the files most likely to be disputed were the ones omitted."""
        import tempfile
        from vanilla_extract.batch import run
        from vanilla_extract.provenance import Workspace
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            os.makedirs(src)
            body = make_pdf("BT (x) Tj ET").replace(
                b"%%EOF", b"trailer\n<< /Encrypt 9 0 R >>\n%%EOF")
            with open(os.path.join(src, "locked.pdf"), "wb") as fh:
                fh.write(body)
            ws = Workspace(os.path.join(d, "case"))
            ws.create("0.2.0", [src])
            run([src], workspace=ws)
            docs = ws.load()["documents"]
        self.assertEqual(len(docs), 1)
        self.assertIsNotNone(docs[0]["original_sha256"])

    def test_archive_budget_applies_in_batch_mode(self):
        """#4 -- only the per-member cap ran; the whole-archive total did not."""
        from vanilla_extract import batch
        self.assertTrue(hasattr(batch, "_budget_for"))
        a = batch._budget_for("/tmp/one.zip")
        self.assertIs(a, batch._budget_for("/tmp/one.zip"))     # shared per archive
        self.assertIsNot(a, batch._budget_for("/tmp/two.zip"))

    def test_zip_members_are_not_blamed_on_a_foreign_mount(self):
        """#9 -- the banner asserted something false about where files live."""
        import tempfile
        from vanilla_extract.fileinfo import zip_member_record
        from vanilla_extract.report import write_datasheet
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.zip")
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("inner.txt", "x")
                info = zf.getinfo("inner.txt")
            rows = [zip_member_record(info, path)]
            out = os.path.join(d, "s.html")
            write_datasheet(rows, out, columns=["name", "size"])
            doc = open(out, encoding="utf-8").read()
        self.assertIn("inside ZIP archives", doc)
        self.assertNotIn("mount options", doc)

    def test_installers_agree_on_the_command_name(self):
        """#6/#7 -- a blanket rename broke both, and the check agreed with them."""
        import tomllib
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
            command = next(iter(tomllib.load(fh)["project"]["scripts"]))
        build = open(os.path.join(root, "packaging", "build_standalone.py"),
                     encoding="utf-8").read()
        install = open(os.path.join(root, "packaging", "install-linux.sh"),
                       encoding="utf-8").read()
        iss = open(os.path.join(root, "packaging", "vanilla-extract.iss"),
                   encoding="utf-8").read()
        self.assertIn(f'"--name", "{command}"', build)
        self.assertIn(f"$BINDIR/{command}", install)
        self.assertIn(f'AppExeName "{command}.exe"', iss)

    def test_birthtime_support_is_cached_per_filesystem(self):
        """#10 -- one subprocess per file dominated a large scan."""
        from vanilla_extract import fileinfo
        self.assertTrue(hasattr(fileinfo, "_BIRTHTIME_SUPPORT"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
