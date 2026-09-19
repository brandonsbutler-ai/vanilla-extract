r"""Tests for vanilla_extract.

Run: python3 -m unittest discover -s tests -v      (no pytest needed)

The fixtures are BUILT here rather than committed as binaries, so the suite
stays readable and there is nothing opaque in the repo. Building a docx by
hand is also the clearest possible statement of what the extractor expects.
"""

import io
import os
import sys
from html.parser import HTMLParser
import shutil
import tempfile
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


# Growth limits for the "never quadratic" tests. The input grows 8x (8k to 64k),
# so a linear cost grows about 8x and a quadratic one about 64x; 24 sits well
# clear of both. Measured on this thread's CPU clock, best of five: wall-clock
# ratios with a 3x limit per doubling failed 6 of 6 parallel runs on a loaded
# machine, where CPU time barely moves (worst linear case 9.6 idle, 8.9 loaded).
GROWTH_SMALL, GROWTH_BIG, GROWTH_LIMIT = 8000, 64000, 24.0


def cpu_growth(make, op, reps=5, small=GROWTH_SMALL, big=GROWTH_BIG):
    """(cost at `big` in CPU seconds, cost ratio big/small) of op(make(n)).

    The input is built outside the timed region, so only `op` is measured.
    `big` must stay 8x `small` for GROWTH_LIMIT to mean what it says.
    """
    import time

    def cost(n):
        data = make(n)
        best = float("inf")
        for _ in range(reps):
            start = time.thread_time()
            op(data)
            best = min(best, time.thread_time() - start)
        return best
    small_cost, big_cost = cost(small), cost(big)
    return big_cost, big_cost / max(small_cost, 1e-6)


class Page(HTMLParser):
    """A generated page as an element tree, so tests can ask what it IS.

    Every assertion here used to be a substring search over the file, which
    tests how the generator spells its output rather than what a browser would
    do with it. Three concrete ways that goes wrong, all of them live in this
    file's history:

      * `'<td class="file">a.pdf</td>' in doc` fails the moment an attribute is
        added or reordered, though nothing about the page has changed.
      * `'<script>alert(1)</script>' not in doc` passes on a page that builds
        the same element as `<SCRIPT >alert(1)</SCRIPT >`.
      * the report embeds the documents' own text, so a search over the whole
        file cannot tell markup from content -- a document CONTAINING the
        string `id="q"` would satisfy a check about the filter box.
    """

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.elements = []                 # (tag, {attr: value})
        self.text_parts = []               # text outside <script>/<style>
        self.script = []
        self._stack = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        self._stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag in self._stack:
            while self._stack and self._stack.pop() != tag:
                pass

    def handle_data(self, data):
        top = self._stack[-1] if self._stack else ""
        if top == "script":
            self.script.append(data)
        elif top != "style":
            self.text_parts.append(data)

    # -- queries
    def find(self, tag, **attrs):
        return [a for t, a in self.elements
                if t == tag and all(a.get(k) == v for k, v in attrs.items())]

    def has(self, tag, **attrs):
        return bool(self.find(tag, **attrs))

    def tags(self):
        return [t for t, _a in self.elements]

    def attr_values(self, name):
        return {a[name] for _t, a in self.elements if name in a}

    @property
    def text(self):
        """Visible text only -- not markup, not script, not CSS."""
        return " ".join(self.text_parts)

    @property
    def script_text(self):
        return "".join(self.script)


def page_of(path):
    with open(path, encoding="utf-8") as fh:
        return Page(fh.read())


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
        # the image is not read -- and it is NAMED, with its reason
        self.assertNotIn("c.png", texts)
        errors = {name: err for name, text, err in results if err}
        self.assertIn("image_no_text_layer", errors.get("c.png", ""))



class TestCountMatchesTheWork(unittest.TestCase):
    """The number shown before a run must be the number the run does.

    Brandon watched a run reach "35,600 of 23,700 files". The pre-scan counted
    an archive as ONE file while the walk expands it into every member inside,
    so the denominator was smaller than the work. Measured on two real trees
    before this was fixed: 2,210 counted against 3,919 processed, and 15,657
    against 20,148.
    """

    def _tree(self):
        import zipfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "loose.txt"), "w", encoding="utf-8") as fh:
            fh.write("a loose document\n")
        # an archive of real documents: the walk yields one unit PER MEMBER
        with zipfile.ZipFile(os.path.join(d, "bundle.zip"), "w") as z:
            for n in ("one.txt", "two.txt", "three.txt"):
                z.writestr(n, "content of " + n)
        # and an archive whose every member is not a document: each is still
        # a unit of work, because each becomes an exception row
        with zipfile.ZipFile(os.path.join(d, "compiled.zip"), "w") as z:
            z.writestr("mod.pyc", "\x00binary")
            z.writestr("lib.so", "\x00binary")
        return d

    def test_the_prescan_counts_exactly_what_the_walk_will_process(self):
        from vanilla_extract import batch
        from vanilla_extract.gui.session import Index
        d = self._tree()
        work = sum(1 for _ in batch._walk([d]))
        self.assertEqual(Index(d).count, work,
                         "the progress denominator disagrees with the work")

    def test_an_archive_counts_as_its_members_not_as_one_file(self):
        from vanilla_extract.gui.session import Index
        d = self._tree()
        # loose.txt + three members of bundle.zip + two of compiled.zip = 6
        self.assertEqual(Index(d).count, 6, Index(d).files)

    def test_the_denominator_is_never_smaller_than_the_work(self):
        """The 'past 100%' failure, stated as the invariant it violates."""
        from vanilla_extract import batch
        from vanilla_extract.gui.session import Index
        d = self._tree()
        self.assertGreaterEqual(Index(d).count, sum(1 for _ in batch._walk([d])))


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
        # the encrypted PDF and the image are both REPORTED, never dropped
        reasons = {os.path.basename(e["file"]): e["reason"] for e in exceptions}
        self.assertEqual(reasons, {"c.pdf": "encrypted", "d.png": "image_no_text_layer"})

    def test_unreadable_file_never_aborts_the_batch(self):
        """One bad document must not cost the caller the other 399.

        The unreadable file has a NON-skipped extension (.dat): a skipped
        extension is intentionally not a document and is dropped, not reported.
        """
        import tempfile
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            self._corpus(d)
            with open(os.path.join(d, "e.dat"), "wb") as fh:
                fh.write(b"\x00\x01\x02\x03\x00\x01")
            results, exceptions = run([d])
        self.assertEqual(len(results), 2)
        self.assertIn("unsupported_format",
                      {e["reason"] for e in exceptions})

    def test_tooling_directories_are_reported_but_never_walked(self):
        """A scan once walked .git/.venv, copied them, and counted past 100%.
        They are still not walked -- nothing inside becomes a row -- but each
        is now REPORTED with the number of files it holds, and a .so binary is
        named as not a document rather than silently dropped."""
        import tempfile
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "keep.txt"), "w").write("real document text")
            open(os.path.join(d, "lib.so"), "wb").write(b"\x7fELF binary")
            os.makedirs(os.path.join(d, ".git"))
            open(os.path.join(d, ".git", "config"), "w").write("[core]\n")
            os.makedirs(os.path.join(d, ".venv"))
            open(os.path.join(d, ".venv", "pkg.py"), "w").write("x = 1\n")
            open(os.path.join(d, ".venv", "pyvenv.cfg"), "w").write("home = x\n")
            results, exceptions = run([d])
        self.assertEqual([os.path.basename(r["file"]) for r in results], ["keep.txt"])
        got = {os.path.basename(e["file"]): (e["reason"], e.get("files_not_read"))
               for e in exceptions}
        self.assertEqual(got, {"lib.so": ("not_a_document", None),
                               ".git": ("excluded_directory", 1),
                               ".venv": ("excluded_directory", 2)})

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

    def test_a_bad_field_regex_names_the_field_and_shows_the_pattern(self):
        """`\\$` inside double quotes reaches Python as `$`, and the error said only
        "nothing to repeat at position 14" -- not which of several --field
        options it meant, nor what pattern the shell had actually handed over."""
        import contextlib
        import tempfile
        from vanilla_extract.__main__ import main
        pattern = r"Total\s*:?\s*$?([0-9,]+\.[0-9]{2})"   # what bash delivers
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(err):
            rc = main(["--batch", d, "--field", r"ok=Invoice\s*(\d+)",
                       "--field", "total=" + pattern])
        self.assertEqual(rc, 2)
        self.assertIn("--field total", err.getvalue())
        self.assertIn(pattern, err.getvalue())
        self.assertIn("nothing to repeat", err.getvalue())

    def test_a_docx_named_pdf_is_one_document_not_an_archive(self):
        """The walk used to decide "archive" from the extension before the
        content sniff could see word/document.xml: a DOCX saved as .pdf came
        back as three junk rows and seven exceptions for its XML parts."""
        import tempfile
        from vanilla_extract.batch import run
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "really_a_docx.pdf"), "wb") as fh:
                fh.write(make_docx(["Invoice Number: INV-77"]))
            results, exceptions = run([d])
        self.assertEqual([os.path.basename(r["file"]) for r in results],
                         ["really_a_docx.pdf"])
        self.assertIn("INV-77", results[0]["text"])
        self.assertEqual(exceptions, [])

    def test_a_docx_named_pdf_reads_as_a_document_on_the_command_line(self):
        import contextlib
        import tempfile
        from vanilla_extract.__main__ import main
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "really_a_docx.pdf")
            with open(path, "wb") as fh:
                fh.write(make_docx(["Invoice Number: INV-77"]))
            with contextlib.redirect_stdout(out):
                rc = main([path])
        self.assertEqual(rc, 0)
        # One document, so no banner: an archive walk would print one per part.
        self.assertEqual(out.getvalue().strip(), "Invoice Number: INV-77")

    def test_a_mistyped_batch_folder_fails_instead_of_writing_an_empty_table(self):
        import contextlib
        from vanilla_extract.__main__ import main
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        out = os.path.join(d, "results.csv")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main(["--batch", os.path.join(d, "invoces"), "--csv", out])
        self.assertEqual(rc, 2)
        self.assertIn("invoces: no such file or folder", err.getvalue())
        self.assertFalse(os.path.exists(out), "an empty spreadsheet was written")

    def test_write_csv_emits_header_even_when_empty(self):
        import tempfile
        from vanilla_extract.batch import write_csv
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.csv")
            written = write_csv([], path, ["file", "reason", "detail"])
            self.assertEqual(written, 0)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read().strip(), "file,reason,detail")


class TestSessionCancel(unittest.TestCase):
    """A run must be stoppable partway through -- the GUI had no way to, and a
    scan that walked the wrong tree could not be halted. session.Session.run
    polls should_cancel() per document and raises Cancelled to stop the batch."""

    def _corpus(self, d, n):
        for i in range(n):
            with open(os.path.join(d, f"doc{i}.txt"), "w", encoding="utf-8") as fh:
                fh.write(f"document number {i} with some ordinary text")

    def test_a_run_stops_when_asked_and_runs_fully_when_not(self):
        import tempfile
        from vanilla_extract.gui.session import Session, Cancelled
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "docs"); os.makedirs(src)
            self._corpus(src, 12)
            seen = []
            sess = Session(out_dir=os.path.join(d, "o1"))
            sess.load(src)
            with self.assertRaises(Cancelled):
                sess.run(on_progress=lambda n, t, name: seen.append(n),
                         should_cancel=lambda: len(seen) >= 4)
            self.assertLess(len(seen), 12, "cancel did not stop the run early")

            full = []
            sess2 = Session(out_dir=os.path.join(d, "o2"))
            sess2.load(src)
            sess2.run(on_progress=lambda n, t, name: full.append(n))
            self.assertEqual(len(full), 12, "an uncancelled run must process all")


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

    # A layout that puts every label on its own line, as fpdf2 and many
    # flattened forms render it. Values like "INV-1001", "Contoso Ltd" and
    # "Net 15" look like labels themselves, so the rule "the next line must
    # not look like a label" dropped three of six fields: recall 0.577 on them.
    STACKED = ("INVOICE\nInvoice Number\nINV-1001\nInvoice Date\n06/13/2026\n"
               "Customer\nContoso Ltd\nTerms\nNet 15\nTotal\n$188,900.99\n"
               "Contact\nap1@contoso.example\n")

    WANT = {"invoice number": "INV-1001", "invoice date": "06/13/2026",
            "customer": "Contoso Ltd", "terms": "Net 15",
            "total": "$188,900.99", "contact": "ap1@contoso.example"}

    def test_stacked_labels_pair_when_the_labels_are_known(self):
        from vanilla_extract.recognize import label_values
        self.assertEqual(label_values(self.STACKED, known=list(self.WANT)), self.WANT)

    def test_without_knowledge_a_label_shaped_value_is_left_unpaired(self):
        """One document cannot tell "Customer / Contoso Ltd" from two headings."""
        from vanilla_extract.recognize import label_values
        pairs = label_values(self.STACKED)
        self.assertNotIn("customer", pairs)
        self.assertEqual(pairs["invoice number"], "INV-1001")     # typed: unambiguous

    def test_the_corpus_supplies_the_knowledge(self):
        """Other documents state the label plainly -- that is how infer_schema
        learns which stacked line is the label. The folder that exposed this
        mixed a colon layout with two stacked ones."""
        from vanilla_extract.recognize import extract_fields, infer_schema
        plain = ("Invoice Number: INV-7\nInvoice Date: 2026-01-02\nCustomer: Fabrikam Inc\n"
                 "Terms: Net 45\nTotal: $5.00\nContact: ap@fabrikam.example\n")
        docs = [plain, plain.replace("Fabrikam Inc", "Tailspin Toys"), self.STACKED]
        labels = [f["label"] for f in infer_schema(docs)]
        self.assertEqual(sorted(labels), sorted(self.WANT))
        self.assertEqual(extract_fields(self.STACKED, labels), self.WANT)

    def test_a_colon_label_takes_the_next_line_even_when_it_has_wide_spaces(self):
        """`Overall risk:` above `CRITICAL  (3 confirmed exploitable)` was lost:
        the double space made the value line read as a pair of its own, and a
        junk `critical` column survived instead."""
        from vanilla_extract.recognize import label_values
        pairs = label_values("Executive Summary\nOverall risk:\n"
                             "CRITICAL  (3 confirmed exploitable)\n"
                             "Severity breakdown: CRITICAL 5, HIGH 1\n")
        self.assertEqual(pairs.get("overall risk"), "CRITICAL  (3 confirmed exploitable)")
        self.assertNotIn("critical", pairs)
        self.assertEqual(pairs.get("severity breakdown"), "CRITICAL 5, HIGH 1")

    def test_a_label_without_a_colon_does_not_take_a_column_line(self):
        from vanilla_extract.recognize import label_values
        pairs = label_values("Summary\nTerms    Net 30\n")
        self.assertEqual(pairs, {"terms": "Net 30"})

    def test_columns_are_ordered_by_support_then_first_appearance(self):
        """The order the README documents, so it cannot drift silently."""
        from vanilla_extract.recognize import infer_schema
        docs = ["Zeta: 1\nAlpha: 2\n", "Alpha: 3\nZeta: 4\nMid: 5\n",
                "Mid: 6\nAlpha: 7\n", "Beta: 8\nZeta: 9\n"]
        self.assertEqual([f["label"] for f in infer_schema(docs)],
                         ["zeta", "alpha", "mid"])

    def test_page_furniture_and_http_verbs_are_not_columns(self):
        """A footer ("Acme Corp  |  Confidential" on every page) and an API
        reference's GET / POST lines were proposed as fields. A value that is
        the same in nine documents of ten is boilerplate, not a field."""
        from vanilla_extract.recognize import infer_schema
        docs = [f"Invoice Number: INV-{i}\nAcme Corp  |  Confidential\n"
                f"GET\n/api/v{i}/items\nPOST\n/api/v{i}/scope\n" for i in range(10)]
        self.assertEqual([f["label"] for f in infer_schema(docs)], ["invoice number"])

    def test_a_constant_value_in_a_handful_of_documents_is_still_a_field(self):
        """Too few documents to call anything boilerplate: the README's own
        four-invoice example must keep every column."""
        from vanilla_extract.recognize import infer_schema
        docs = [f"Invoice Number: INV-{i}\nTerms: Net 30\n" for i in range(4)]
        self.assertEqual(sorted(f["label"] for f in infer_schema(docs)),
                         ["invoice number", "terms"])

    def test_a_label_shaped_line_that_merely_recurs_is_not_known(self):
        """Headings recur too. Without a plain statement of the label anywhere,
        the stacked pairs stay unread: a blank cell, not a wrong value."""
        from vanilla_extract.recognize import infer_schema
        docs = [self.STACKED.replace("Contoso Ltd", co)
                for co in ("Contoso Ltd", "Fabrikam Inc", "Tailspin Toys")]
        self.assertNotIn("customer", [f["label"] for f in infer_schema(docs)])

    def test_a_run_of_headings_is_not_paired(self):
        from vanilla_extract.recognize import label_values
        text = "ACME CORP\nBilling Dept\nNorth Wing\nSuite B\n"
        self.assertEqual(label_values(text), {})
        self.assertEqual(label_values(text, known=["acme corp", "billing dept",
                                                   "north wing", "suite b"]), {})

    def test_a_typed_value_is_never_taken_as_a_label(self):
        from vanilla_extract.recognize import label_values
        self.assertNotIn("inv-1001", label_values("Invoice Number\nINV-1001\n$5.00\n"))


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
        """Read from the page's TEXT, not its source.

        The report embeds every document's extracted text, so a search over
        the file cannot tell a heading from a document that happens to contain
        the word.
        """
        page = Page(self._render())
        self.assertIn("encrypted", page.text)
        self.assertIn("needs a password", page.text)

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
        page = Page(doc)
        # The report writes exactly one script of its own. A value that became
        # markup would add another, or land inside that one.
        self.assertEqual(page.tags().count("script"), 1,
                         "the value became a script element")
        self.assertNotIn("alert(1)", page.script_text,
                         "the value reached the page's own script")
        self.assertIn("alert(1)", page.text,
                      "the value was dropped rather than escaped")

    def test_a_huge_document_does_not_make_a_huge_page(self):
        """One 21.6-million-character JSON file made a 155 MB report that took
        ten seconds to open. The source view is a preview, bounded per
        document, and it says how much it left out and where the rest is."""
        import json
        import re as _re
        from vanilla_extract.report import PREVIEW_LIMIT, write_report
        big = "x" * (PREVIEW_LIMIT * 5)
        rows = [{"file": "/data/scan.json", "characters": len(big), "text": big},
                {"file": "/data/small.txt", "characters": 5, "text": "hello"}]
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "r.html")
        write_report(rows, [], path)
        self.assertLess(os.path.getsize(path), PREVIEW_LIMIT * 2)
        script = page_of(path).script_text
        docs = json.loads(_re.search(r"const DOCS = (.*?);\n", script).group(1))
        self.assertLessEqual(len(docs[0]["text"]), PREVIEW_LIMIT + 400)
        self.assertIn(f"{len(big) - PREVIEW_LIMIT:,} more characters", docs[0]["text"])
        self.assertIn("/data/scan.json", docs[0]["text"])
        self.assertEqual(docs[1]["text"], "hello")

    def test_file_column_is_not_editable(self):
        """The filename identifies the row; editing it would break provenance."""
        page = Page(self._render())
        cells = page.find("td", **{"class": "file"})
        self.assertTrue(cells, "no filename cells rendered")
        for cell in cells:
            self.assertNotIn("contenteditable", cell)


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


class TestDesktopSession(unittest.TestCase):
    """The window's decisions, driven with no window attached.

    All of this lives in a module that imports nothing outside the standard
    library, which is why it can be tested here at all -- the toolkit is in
    qt_app.py and nothing in this file touches it.
    """

    def _tree(self, layout):
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        for name, count in layout.items():
            folder = os.path.join(root, name)
            os.makedirs(folder, exist_ok=True)
            for i in range(count):
                with open(os.path.join(folder, f"{name}{i}.txt"), "w",
                          encoding="utf-8") as fh:
                    fh.write(f"Invoice Number: {name.upper()}-{i}\n"
                             f"Invoice Date: 2026-08-0{i + 1}\n"
                             f"Customer: Northwind\nTotal: ${i + 1}.00\n")
        return root

    def test_several_folders_become_one_job(self):
        """Drops ADD. Four folders dropped one at a time are one run."""
        from vanilla_extract.gui.session import Session
        root = self._tree({"august": 3, "september": 4})
        s = Session(out_dir=os.path.join(root, "out"))
        s.load([os.path.join(root, "august")])
        index = s.load([os.path.join(root, "september")], add=True)
        self.assertEqual(len(index.folders), 2)
        self.assertEqual(index.count, 7)
        self.assertIn("2 folders", index.summary())
        self.assertEqual(sorted(index.per_folder.values()), [3, 4])

    def test_a_parent_folder_supersedes_one_already_loaded(self):
        """Dropping a folder and then its parent must not read it twice."""
        from vanilla_extract.gui.session import Session
        root = self._tree({"august": 3, "september": 4})
        s = Session(out_dir=os.path.join(root, "out"))
        s.load([os.path.join(root, "august")])
        index = s.load([root], add=True)
        self.assertEqual(index.folders, [os.path.abspath(root)])
        self.assertEqual(index.count, 7, "files were counted twice")

    def test_a_child_of_a_loaded_folder_adds_nothing(self):
        from vanilla_extract.gui.session import Session
        root = self._tree({"august": 3})
        s = Session(out_dir=os.path.join(root, "out"))
        s.load([root])
        index = s.load([os.path.join(root, "august")], add=True)
        self.assertEqual(index.folders, [os.path.abspath(root)])

    def test_a_dropped_file_resolves_to_its_folder(self):
        from vanilla_extract.gui.session import folders_from_drop
        root = self._tree({"august": 2})
        one = os.path.join(root, "august", "august0.txt")
        self.assertEqual(folders_from_drop([one]),
                         [os.path.abspath(os.path.join(root, "august"))])

    def test_a_tk_payload_with_spaces_in_the_path_survives(self):
        """Tk braces a path containing a space; two braced paths are two."""
        from vanilla_extract.gui.session import folders_from_drop
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        first = os.path.join(root, "my invoices")
        second = os.path.join(root, "more invoices")
        os.makedirs(first)
        os.makedirs(second)
        got = folders_from_drop("{%s} {%s}" % (first, second))
        self.assertEqual(got, [os.path.abspath(first), os.path.abspath(second)])

    def test_output_never_lands_inside_the_folder_being_read(self):
        """Otherwise the next run reads its own report."""
        from vanilla_extract.gui.session import Session
        root = self._tree({"august": 2})
        folder = os.path.join(root, "august")
        s = Session()
        s.load([folder])
        out = os.path.abspath(s.output_dir())
        self.assertFalse(out.startswith(os.path.abspath(folder) + os.sep))

    def test_a_fast_run_does_not_report_zero_seconds(self):
        """"0.0s" reads like a failure."""
        from vanilla_extract.gui.session import Result
        r = Result("/x", "/y")
        r.seconds = 0.021
        self.assertIn("ms", r.duration())
        r.seconds = 4.2
        self.assertIn("seconds", r.duration())
        r.seconds = 200
        self.assertIn("minutes", r.duration())


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

    def _forged_size_bomb(self, real_mb=200):
        """A docx whose header LIES: it declares file_size=100 for a member
        whose deflate stream really inflates to `real_mb` MB. The honest-size
        check waves it through, so only a bounded read stops it."""
        import struct
        payload = b"A" * (real_mb * 1024 * 1024)
        FAKE = 100
        def member(name, data, off):
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            comp = c.compress(data) + c.flush()
            crc = zlib.crc32(data) & 0xffffffff
            nm = name.encode()
            lfh = struct.pack("<IHHHHHIIIHH", 0x04034b50, 20, 0, 8, 0, 0,
                              crc, len(comp), FAKE, len(nm), 0) + nm
            cd = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014b50, 20, 20, 0, 8, 0,
                             0, crc, len(comp), FAKE, len(nm), 0, 0, 0, 0, 0,
                             off) + nm
            return lfh + comp, cd
        ct = b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
        buf, cds, off = io.BytesIO(), [], 0
        for name, data in (("word/document.xml", payload),
                           ("[Content_Types].xml", ct)):
            body, cd = member(name, data, off)
            buf.write(body); cds.append(cd); off += len(body)
        cd_bytes = b"".join(cds); cd_off = off
        buf.write(cd_bytes)
        buf.write(struct.pack("<IHHHHIIH", 0x06054b50, 0, 0, len(cds),
                              len(cds), len(cd_bytes), cd_off, 0))
        return buf.getvalue()

    def test_a_forged_member_size_cannot_exhaust_memory(self):
        # A member can forge a tiny declared size to slip past the declared-size
        # check while its real stream inflates to 200 MB. Both the fixed and the
        # unbounded reader raise (a CRC mismatch), so the exception type proves
        # nothing -- the DIFFERENCE is memory. Run the extraction under a hard
        # address-space cap well below the bomb: the unbounded read dies with
        # MemoryError, the bounded read refuses cleanly under the cap.
        import subprocess
        import tempfile
        import textwrap
        fd, path = tempfile.mkstemp(suffix=".docx")
        os.write(fd, self._forged_size_bomb(200))
        os.close(fd)
        self.addCleanup(os.remove, path)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = textwrap.dedent(f"""
            import resource, sys
            cap = 250 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
            sys.path.insert(0, {repo!r})
            from vanilla_extract.formats import ooxml
            try:
                ooxml.extract_docx(open({path!r}, "rb"))
                print("RETURNED")
            except MemoryError:
                print("MEMORYERROR")
            except Exception as e:
                print("REFUSED", type(e).__name__)
        """)
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr)
        self.assertNotIn("MemoryError", out,
                         "the forged-size bomb exhausted memory under the cap")
        self.assertNotIn("MEMORYERROR", out,
                         "the forged-size bomb exhausted memory under the cap")
        self.assertIn("REFUSED", r.stdout,
                      "the forged-size bomb was not refused")

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

        Growing the input 8x grows a linear pattern's cost about 8x and a
        quadratic one's about 64x, so the ratio is the assertion, on CPU time
        (see cpu_growth). The absolute times are left out of it.
        """
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
                slowest, ratio = cpu_growth(make, regex.search)
                if slowest < 0.002:
                    continue          # too fast to time; nothing to conclude
                self.assertLess(
                    ratio, GROWTH_LIMIT,
                    f"{name} grows {ratio:.1f}x for 8x the input on {label!r} "
                    f"({slowest * 1000:.0f} ms at 64k) -- linear is ~8x")

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
        from vanilla_extract.formats.rtf import extract_rtf
        make = lambda n: (r"{\rtf1\ansi " + " " * n + "text}").encode()   # noqa: E731
        self.assertEqual(extract_rtf(make(100)), "text")
        # 16k -> 128k: at 64k the whole cleanup takes under half a millisecond,
        # too little to measure a ratio from. Asserted, not skipped -- a guard
        # that skips when things are fast never runs.
        slowest, ratio = cpu_growth(make, extract_rtf, small=16000, big=128000)
        self.assertGreater(slowest, 0.0005, "too fast to measure growth")
        self.assertLess(ratio, GROWTH_LIMIT,
                        f"whitespace cleanup grows {ratio:.1f}x for 8x the input")

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
        page = Page(doc)
        self.assertEqual(page.find("img"), [],
                         "the document supplied an element that survived parsing")
        self.assertLessEqual(page.tags().count("script"), 2,
                             "the document opened a script block")

    def test_pdf_object_index_is_not_quadratic(self):
        """#4 -- objects with no `endobj` sliced to end-of-file, n^2 in time and memory."""
        from vanilla_extract.formats import pdfcmap
        # Growth on CPU time rather than a wall-clock budget: n objects of
        # ~20 bytes, so GROWTH_SMALL..GROWTH_BIG bytes is 8x the objects.
        make = lambda n: b"%PDF-1.4\n" + b"".join(      # noqa: E731
            f"{i} 0 obj\n<</X 1>>\n".encode() for i in range(n // 8))
        _slowest, ratio = cpu_growth(make, pdfcmap.build_object_index, reps=3)
        self.assertLess(ratio, GROWTH_LIMIT, f"object index grows {ratio:.1f}x for 8x the objects")

    def test_pdf_flate_stream_is_capped(self):
        """#5 -- the primary format was the one decompression path with no ceiling."""
        import zlib
        from vanilla_extract.formats import pdf
        from vanilla_extract.limits import MAX_PDF_STREAM_BYTES
        stream = zlib.compress(b"A" * (MAX_PDF_STREAM_BYTES * 4))
        doc = (b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode /Length "
               + str(len(stream)).encode() + b" >>\nstream\n" + stream
               + b"\nendstream\nendobj\n%%EOF\n")
        # must stop at the cap, not exhaust memory -- and since the cap was
        # hit, it is refused rather than returned truncated
        from vanilla_extract.limits import StreamTooLarge
        with self.assertRaises(StreamTooLarge):
            pdf.extract_pdf(doc)

    def test_a_pdf_decompression_bomb_is_refused_and_reported(self):
        """The README said "Refused". It was truncated silently: a 1 MB file
        came back as its first 64 MB, took 12.5 s -- the time goes on walking
        64 MB of inflated whitespace byte by byte -- and exited 0."""
        import time
        import zlib
        from unittest import mock
        from vanilla_extract import limits
        from vanilla_extract.batch import run
        cap = 1024 * 1024
        stream = zlib.compress(b"BT (Hello bomb) Tj ET\n" + b" " * (cap * 3))
        doc = (b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode /Length "
               + str(len(stream)).encode() + b" >>\nstream\n" + stream
               + b"\nendstream\nendobj\n%%EOF\n")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "bomb.pdf"), "wb") as fh:
            fh.write(doc)
        with mock.patch.object(limits, "MAX_PDF_STREAM_BYTES", cap):
            start = time.thread_time()     # CPU, so a loaded machine cannot fail it
            results, exceptions = run([d])
            took = time.thread_time() - start
        self.assertEqual(results, [])
        self.assertEqual([e["reason"] for e in exceptions], ["limit_exceeded"])
        self.assertIn("MB", exceptions[0]["detail"])
        self.assertLess(took, 1.0)

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

    def test_no_csv_cell_exceeds_what_a_spreadsheet_holds(self):
        """A 21.6-million-character text cell: Excel stops at 32,767 characters
        per cell, and Python's own csv reader refused the file at its default
        131,072-character field limit."""
        import csv as _csv
        from vanilla_extract.batch import write_csv
        big = "word " * 20000                                   # 100,000 chars
        rows = [{"file": "big.json", "characters": len(big), "text": big},
                {"file": "small.txt", "characters": 5, "text": "hello"}]
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "o.csv")
        write_csv(rows, path, ["file", "characters", "text"])
        with open(path, encoding="utf-8", newline="") as fh:
            got = list(_csv.DictReader(fh))              # default field limit
        self.assertLessEqual(max(len(v) for r in got for v in r.values()), 32767)
        self.assertEqual([r["text_truncated"] for r in got], ["True", "False"])
        self.assertIn("truncated", got[0]["text"][-60:])
        self.assertTrue(big.startswith(got[0]["text"][:30000]))
        self.assertEqual(got[1]["text"], "hello")

    def test_negative_money_and_percent_stay_numbers(self):
        """The guard prefixed every cell starting '-', so -$251.00 and -4.5%
        reached Excel as text: a column that will not sum."""
        from vanilla_extract.batch import csv_safe
        for value in ("-$251.00", "-4.5%", "-12", "-1,204.55", "-£3.10", "-0.5"):
            self.assertEqual(csv_safe(value), value, value)

    def test_formula_payloads_starting_with_a_minus_are_still_neutralized(self):
        from vanilla_extract.batch import csv_safe
        for value in ("-2+3+cmd|' /C calc'!A0", "-1+1", "-SUM(A1)", "- 5",
                      "-$251.00+cmd|' /C calc'!A0", "=1+1", "+1", "@SUM(1)",
                      "-4.5%\n=cmd", "\t-1"):
            self.assertTrue(csv_safe(value).startswith("'"), value)

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
        """Two mounts can claim one path; the later real one is the answer.

        The shadowed mount point is DISCOVERED from /proc/mounts, not named.
        It used to be a literal path on one developer's machine, which made the
        assertion vacuous everywhere else: the comprehension came back empty and
        assertNotIn passed without testing anything. Most Linux boxes have at
        least one such point (/proc/sys/fs/binfmt_misc is autofs-triggered), and
        where none exists the test skips out loud rather than passing quietly.
        """
        from vanilla_extract import fileinfo
        table = fileinfo._mount_table()
        points = [m for m, _ in table]
        self.assertEqual(len(points), len(set(points)), "duplicate mount points")

        raw = {}
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 3:
                        raw.setdefault(parts[1].replace("\\040", " "),
                                       []).append(parts[2])
        except OSError:
            self.skipTest("/proc/mounts unavailable")
        shadowed = [pt for pt, kinds in raw.items()
                    if any(k in fileinfo._PASSTHROUGH_FS for k in kinds)
                    and any(k not in fileinfo._PASSTHROUGH_FS for k in kinds)]
        if not shadowed:
            self.skipTest("no mount point on this machine is claimed by both a "
                          "passthrough trigger and a real filesystem")
        resolved = dict(table)
        for point in shadowed:
            self.assertNotIn(resolved.get(point), fileinfo._PASSTHROUGH_FS,
                             f"{point} resolved to a trigger, not the real fs")

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
        page = Page(doc)
        remote = [a for _t, a in page.elements
                  if str(a.get("src", "")).startswith("http")
                  or str(a.get("href", "")).startswith("http")]
        self.assertEqual(remote, [], "an element loads a remote URL")
        self.assertGreaterEqual(len(page.attr_values("data-col")), 3)
        self.assertTrue(page.has("input", id="q"))
        self.assertIn("parseFloat", page.script_text)

    def test_a_column_empty_for_every_file_is_not_shown(self):
        """29 fields exist because a FILE can have 29 properties, not this set.

        A corpus with no archives and no symlinks carries four permanently
        blank columns, which on a narrow screen is four columns of horizontal
        scrolling between the reader and the data.
        """
        import tempfile
        from vanilla_extract.report import write_datasheet
        rows = [{"name": "a.txt", "size": "1 B", "symlink_target": "",
                 "compressed_bytes": None},
                {"name": "b.txt", "size": "2 B", "symlink_target": "",
                 "compressed_bytes": None}]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "s.html")
            write_datasheet(rows, out,
                            columns=["name", "size", "symlink_target",
                                     "compressed_bytes"])
            page = page_of(out)
        shown = page.attr_values("data-col")
        self.assertEqual(shown, {"name", "size"})
        # and the page says what it left out, rather than quietly dropping it
        self.assertIn("symlink target", page.text)
        self.assertIn("compressed bytes", page.text)

    def test_a_column_with_any_value_is_kept(self):
        """One file having a value is enough; the column is about the set."""
        import tempfile
        from vanilla_extract.report import write_datasheet
        rows = [{"name": "a.txt", "compressed_bytes": ""},
                {"name": "b.txt", "compressed_bytes": "120"}]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "s.html")
            write_datasheet(rows, out, columns=["name", "compressed_bytes"])
            page = page_of(out)
        self.assertIn("compressed_bytes", page.attr_values("data-col"))

    def test_datasheet_values_are_escaped(self):
        import tempfile
        from vanilla_extract.report import write_datasheet
        rows = [{"name": "<script>alert(1)</script>", "size": "1 B"}]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "s.html")
            write_datasheet(rows, out, columns=["name", "size"])
            doc = open(out, encoding="utf-8").read()
        page = Page(doc)
        self.assertNotIn("alert(1)", page.script_text,
                         "the value reached the page's own script")
        self.assertIn("alert(1)", page.text,
                      "the value was dropped rather than escaped")

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
        # (located by the handler, not the first "export" in the page -- the
        # header now tells the reader to export, which is prose, not script)
        export_js = doc.split("getElementById('export')")[1][:500]
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

    def test_the_windows_installer_takes_its_path_entry_back_out(self):
        """The installer appended {app} to the user's PATH and the uninstaller
        left it there, with no ChangesEnvironment to tell Explorer either way.
        Inno Setup cannot run here, so this reads the script's sections; the
        compiled behaviour is unverified on this machine."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        iss = open(os.path.join(root, "packaging", "vanilla-extract.iss"),
                   encoding="utf-8").read()
        sections, current = {}, None
        for line in iss.splitlines():
            if line.startswith("[") and line.rstrip().endswith("]"):
                current = line.strip()[1:-1]
                sections[current] = []
            elif current:
                sections[current].append(line.strip())
        self.assertIn("ChangesEnvironment=yes", sections["Setup"])
        code = "\n".join(sections["Code"])
        self.assertIn("procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);", code)
        self.assertIn("usPostUninstall", code)
        self.assertIn("RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path'", code)

    def test_birthtime_support_is_cached_per_filesystem(self):
        """#10 -- one subprocess per file dominated a large scan."""
        from vanilla_extract import fileinfo
        self.assertTrue(hasattr(fileinfo, "_BIRTHTIME_SUPPORT"))


def _zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR binary pixels"


class TestEveryInputIsAccountedFor(unittest.TestCase):
    """inputs == rows + exceptions (+ reported exclusions), with nothing silent.

    Found by a reconcile of a real delivery: an image-named file (even a real
    PDF renamed .jpg), a dot-folder such as .from_client/, and a zip inside a
    zip each appeared in no table at all, with exit status 0.
    """

    def _tree(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)

        def put(rel, data):
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data if isinstance(data, bytes) else data.encode())
        put("a.txt", "Invoice Number: INV-1\n")
        put("photo.png", PNG)
        put("invoice.jpg", make_pdf("BT (Invoice Number: INV-2) Tj ET"))
        put("lib.so", b"\x7fELF\x00binary")
        put(".from_client/inv.txt", "Invoice Number: INV-3\n")
        put(".git/config", "[core]\n")
        put(".git/objects/ab/cdef", b"\x00blob")
        put("pkg/__pycache__/m.cpython-312.pyc", b"\x00pyc")
        put("env/pyvenv.cfg", "home = /usr/bin\n")
        put("env/lib/site.py", "x = 1\n")
        inner = _zip_bytes({"deep.txt": "Invoice Number: INV-4\n"})
        put("bundle.zip", _zip_bytes({"one.txt": "Invoice Number: INV-5\n",
                                      "pic.png": PNG, "inner.zip": inner}))
        return d

    def test_rows_plus_exceptions_account_for_every_input(self):
        from vanilla_extract.batch import run
        d = self._tree()
        results, exceptions = run([d])
        # The inputs: every file on disk, an archive standing for its members.
        # bundle.zip holds one.txt, pic.png and inner.zip; inner.zip holds one.
        on_disk = sum(len(files) for _r, _d, files in os.walk(d))
        inputs = on_disk - 1 + 2 + 1
        per_file = [e for e in exceptions if "files_not_read" not in e]
        excluded = sum(e["files_not_read"] for e in exceptions
                       if "files_not_read" in e)
        self.assertEqual(len(results) + len(per_file) + excluded, inputs,
                         (sorted(r["file"] for r in results), exceptions))

    def test_what_each_input_became(self):
        from vanilla_extract.batch import run
        d = self._tree()
        results, exceptions = run([d])
        rel = lambda p: os.path.relpath(p, d)          # noqa: E731
        read = sorted(rel(r["file"]) for r in results)
        self.assertEqual(read, ["a.txt", "bundle.zip!inner.zip!deep.txt",
                                "bundle.zip!one.txt", "invoice.jpg"])
        reasons = {rel(e["file"]): e["reason"] for e in exceptions}
        self.assertEqual(reasons, {
            "photo.png": "image_no_text_layer",
            "bundle.zip!pic.png": "image_no_text_layer",
            "lib.so": "not_a_document",
            ".from_client": "hidden_directory",
            ".git": "excluded_directory",
            os.path.join("pkg", "__pycache__"): "excluded_directory",
            "env": "excluded_directory",
        })

    def test_symlinked_and_unreadable_folders_are_reported(self):
        """os.walk does not follow a symlinked folder and silently skips one it
        cannot list, so both used to vanish from every table."""
        if not hasattr(os, "symlink") or os.name != "posix" or os.geteuid() == 0:
            self.skipTest("needs POSIX permissions and a non-root user")
        from vanilla_extract.batch import run
        d = self._tree()
        elsewhere = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, elsewhere, True)
        with open(os.path.join(elsewhere, "linked.txt"), "w") as fh:
            fh.write("Invoice Number: INV-6\n")
        os.symlink(elsewhere, os.path.join(d, "linked"))
        locked = os.path.join(d, "locked")
        os.makedirs(locked)
        with open(os.path.join(locked, "secret.txt"), "w") as fh:
            fh.write("Invoice Number: INV-7\n")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        results, exceptions = run([d])
        reasons = {os.path.relpath(e["file"], d): e["reason"] for e in exceptions}
        self.assertEqual(reasons.get("linked"), "symlink_not_followed")
        self.assertEqual(reasons.get("locked"), "unreadable_directory")
        self.assertNotIn("linked.txt", {os.path.basename(r["file"]) for r in results})
        # The reconcile still closes: what the walk can see, the archive
        # standing for its members, and each unentered folder as one input.
        visible = sum(len(files) for _r, _d, files in os.walk(d))
        inputs = visible - 1 + 2 + 1 + 2
        per_file = [e for e in exceptions if "files_not_read" not in e]
        excluded = sum(e["files_not_read"] for e in exceptions if "files_not_read" in e)
        self.assertEqual(len(results) + len(per_file) + excluded, inputs)

    def test_the_gui_prescan_still_counts_exactly_the_work(self):
        from vanilla_extract import batch
        from vanilla_extract.gui.session import Index
        d = self._tree()
        self.assertEqual(Index(d).count, sum(1 for _ in batch._walk([d])))

    def test_nesting_past_the_limit_is_reported_not_dropped(self):
        from vanilla_extract.batch import run
        from vanilla_extract.limits import MAX_ARCHIVE_DEPTH
        data = _zip_bytes({"deep.txt": "Invoice Number: INV-DEEP\n"})
        for level in range(MAX_ARCHIVE_DEPTH + 2):
            data = _zip_bytes({f"level{level}.zip": data})
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "nested.zip"), "wb") as fh:
            fh.write(data)
        results, exceptions = run([d])
        self.assertEqual(results, [])
        self.assertEqual([e["reason"] for e in exceptions], ["limit_exceeded"])

    @staticmethod
    def _fan(fanout, depth, leaf=b"hello " * 10):
        """The reviewer's construction: every level holds `fanout` copies of the
        level below, so a few KB stand for fanout**depth documents."""
        cur = _zip_bytes({"leaf.txt": leaf})
        for level in range(depth):
            cur = _zip_bytes({f"l{level}_{i}.zip": cur for i in range(fanout)})
        return cur

    def _one(self, name, data):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(data)
        return d

    def test_a_fan_out_archive_stops_at_the_member_budget_with_one_row(self):
        """A 23 KB zip (fanout 16, depth 5) became 1,048,576 rows in 138 s."""
        from unittest import mock
        from vanilla_extract import limits
        from vanilla_extract.batch import run
        d = self._one("fan.zip", self._fan(4, 3))                 # 64 leaves
        with mock.patch.object(limits, "MAX_ARCHIVE_MEMBERS", 20):
            results, exceptions = run([d])
        self.assertLessEqual(len(results), 20)
        self.assertEqual([e["reason"] for e in exceptions], ["limit_exceeded"])
        self.assertIn("20", exceptions[0]["detail"])

    def test_the_member_budget_counts_uncompressed_bytes_across_levels(self):
        from unittest import mock
        from vanilla_extract import limits
        from vanilla_extract.batch import run
        leaf = os.urandom(5000).hex().encode()        # 10 KB, compresses ~2:1
        d = self._one("fan.zip", self._fan(4, 2, leaf=leaf))            # 16 x 10 KB
        with mock.patch.object(limits, "MAX_ARCHIVE_WALK_BYTES", 50_000):
            results, exceptions = run([d])
        self.assertLess(len(results), 16)
        self.assertEqual([e["reason"] for e in exceptions], ["limit_exceeded"])

    def test_the_reviewers_fan_out_finishes_bounded(self):
        """fanout 16 depth 4 -- 65,536 leaves in 17 KB -- at the real limit."""
        import time
        from vanilla_extract.batch import run
        from vanilla_extract.limits import MAX_ARCHIVE_MEMBERS
        d = self._one("fan.zip", self._fan(16, 4))
        start = time.thread_time()
        results, exceptions = run([d], include_text=False)
        self.assertLessEqual(len(results), MAX_ARCHIVE_MEMBERS)
        self.assertEqual([e["reason"] for e in exceptions], ["limit_exceeded"])
        self.assertLess(time.thread_time() - start, 60)

    def test_an_archive_is_opened_once_not_once_per_member(self):
        """Re-opening the ZipFile for every member parses the whole central
        directory each time: 8,000 members took 197 s."""
        from unittest import mock
        from vanilla_extract.batch import run
        d = self._one("wide.zip", _zip_bytes({f"f{i}.txt": "x" for i in range(300)}))
        with mock.patch.object(zipfile, "ZipFile", wraps=zipfile.ZipFile) as opened:
            results, _ = run([d])
        self.assertEqual(len(results), 300)
        self.assertLessEqual(opened.call_count, 5)

    def test_single_file_mode_reads_a_nested_zip_and_names_what_it_skips(self):
        path = os.path.join(self._tree(), "bundle.zip")
        got = {name: (text, err) for name, text, err in extract_archive(path)}
        self.assertEqual(got["inner.zip!deep.txt"][0].strip(), "Invoice Number: INV-4")
        self.assertIsNone(got["pic.png"][0])
        self.assertIn("image_no_text_layer", got["pic.png"][1])
        self.assertEqual(sorted(got), ["inner.zip!deep.txt", "one.txt", "pic.png"])

    def test_the_cli_says_what_it_skipped_inside_an_archive(self):
        import contextlib
        from vanilla_extract.__main__ import main
        path = os.path.join(self._tree(), "bundle.zip")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main([path])
        self.assertIn("INV-4", out.getvalue())
        self.assertIn("pic.png: image_no_text_layer", err.getvalue())


def _laughs_docx():
    """A billion-laughs document.xml: ten levels of tenfold entity expansion."""
    ents = "".join(f'<!ENTITY l{i} "{("&l%d;" % (i - 1)) * 10}">' for i in range(1, 10))
    xml = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY l0 "lol">' + ents + ']>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
           '2006/main"><w:body><w:p><w:r><w:t>&l9;</w:t></w:r></w:p></w:body></w:document>')
    return _zip_bytes({"[Content_Types].xml": "<Types/>", "word/document.xml": xml})


SCAN_PDF = (b"%PDF-1.4\n1 0 obj\n<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
            b"/Filter /DCTDecode /Length 3 >>\nstream\nabc\nendstream\nendobj\n"
            b"trailer\n<< /Root 1 0 R >>\n%%EOF\n")


class TestWhyADocumentCameBackEmpty(unittest.TestCase):
    """Four different failures were all labelled "often a scan with no text
    layer", which sends the reader looking for OCR for a file that was empty,
    cut off, or refused by a safety limit."""

    def _run(self, files):
        from vanilla_extract.batch import run
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for name, data in files.items():
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(data)
        _, exceptions = run([d])
        return {os.path.basename(e["file"]): (e["reason"], e["detail"]) for e in exceptions}

    def test_each_empty_result_names_its_own_cause(self):
        whole = make_pdf("BT (Invoice Number: INV-9 and a good deal more text) Tj ET")
        got = self._run({
            "zero.pdf": b"",
            "cut.pdf": whole[:whole.index(b"stream") + 20],
            "laughs.docx": _laughs_docx(),
            "scan.pdf": SCAN_PDF,
            "blank.txt": b"   \n\n  ",
        })
        self.assertEqual({k: v[0] for k, v in got.items()}, {
            "zero.pdf": "empty_file",
            "cut.pdf": "truncated_or_corrupt",
            "laughs.docx": "limit_exceeded",
            "scan.pdf": "no_text_found",
            "blank.txt": "no_text_found",
        })
        self.assertIn("OCR", got["scan.pdf"][1])
        for name in ("zero.pdf", "cut.pdf", "laughs.docx", "blank.txt"):
            self.assertNotIn("scan", got[name][1], name)
            self.assertNotIn("OCR", got[name][1], name)


class TestEmptyOutsideBatch(unittest.TestCase):
    """`vanilla file.pdf`, `--json` and extract_file() returned "" with exit 0
    for an empty, truncated or scanned PDF -- the blank result the batch path
    already refused to produce."""

    def _file(self, name, data):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def _cli(self, *argv):
        import contextlib
        from vanilla_extract.__main__ import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_the_cli_says_why_and_exits_1(self):
        rc, out, err = self._cli(self._file("zero.pdf", b""))
        self.assertEqual(rc, 1)
        self.assertIn("empty_file", err)
        self.assertEqual(out.strip(), "")

    def test_json_carries_the_reason(self):
        import json
        rc, out, _err = self._cli("--json", self._file("scan.pdf", SCAN_PDF))
        self.assertEqual(rc, 1)
        record = json.loads(out)
        self.assertEqual((record["text"], record["reason"]), ("", "no_text_found"))
        self.assertIn("OCR", record["detail"])

    def test_an_empty_member_of_an_archive_is_reported_too(self):
        path = self._file("a.zip", _zip_bytes({"blank.txt": "  \n", "ok.txt": "text"}))
        rc, out, err = self._cli(path)
        self.assertEqual(rc, 1)
        self.assertIn("blank.txt: no_text_found", err)
        self.assertIn("text", out)

    def test_a_readable_file_still_exits_0(self):
        rc, out, err = self._cli(self._file("ok.txt", b"Invoice Number: INV-1\n"))
        self.assertEqual((rc, err), (0, ""))

    def test_extract_file_keeps_returning_a_string_unless_asked(self):
        from vanilla_extract import NoTextFound, extract_file
        whole = make_pdf("BT (Invoice Number: INV-9 and more) Tj ET")
        path = self._file("cut.pdf", whole[:whole.index(b"stream") + 20])
        self.assertEqual(extract_file(path), "")
        with self.assertRaises(NoTextFound) as caught:
            extract_file(path, require_text=True)
        self.assertEqual(caught.exception.reason, "truncated_or_corrupt")


class TestImportCsvInput(unittest.TestCase):
    """--import-csv ended in a raw traceback for Excel's plain "CSV" save,
    which is Windows-1252, and for a mistyped path."""

    def _workspace(self):
        from vanilla_extract.batch import run
        from vanilla_extract.provenance import Workspace
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        src = os.path.join(d, "src")
        os.makedirs(src)
        with open(os.path.join(src, "Rechnung_Müller.txt"), "w", encoding="utf-8") as fh:
            fh.write("Kunde: Müller GmbH\n")
        ws = Workspace(os.path.join(d, "ws"))
        ws.create("test", [src])
        results, _ = run([src], workspace=ws)
        ws.add_revision(results, ["file", "characters"], note="as extracted")
        return d, ws, results[0]["file"]

    def _cli(self, *argv):
        import contextlib
        from vanilla_extract.__main__ import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_a_windows_1252_csv_is_read_and_the_encoding_named(self):
        d, ws, label = self._workspace()
        path = os.path.join(d, "excel.csv")
        with open(path, "wb") as fh:
            fh.write(f"file,characters\r\n{label},999\r\n".encode("cp1252"))
        rc, out, err = self._cli("--workspace", ws.root, "--import-csv", path)
        self.assertEqual(rc, 0, err)
        self.assertIn("cp1252", out + err)
        self.assertIn("1 cell(s) changed", out)       # matched by the ü path

    def test_a_missing_csv_is_a_clear_error(self):
        d, ws, _label = self._workspace()
        rc, out, err = self._cli("--workspace", ws.root, "--import-csv",
                                 os.path.join(d, "nope.csv"))
        self.assertEqual(rc, 2)
        self.assertIn("nope.csv", err)
        self.assertNotIn("Traceback", err)


class TestSmallThingsFromTheJourney(unittest.TestCase):
    """The cheap, clearly-right nits from the end-to-end review."""

    def _cli(self, *argv):
        import contextlib
        from vanilla_extract.__main__ import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_a_folder_without_batch_says_to_use_batch(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        rc, _out, err = self._cli(d)
        self.assertEqual(rc, 1)
        self.assertIn("is a folder", err)
        self.assertIn("--batch", err)
        self.assertNotIn("IsADirectoryError", err)

    def test_reimporting_an_unchanged_table_files_no_empty_revision(self):
        from vanilla_extract.batch import run, write_csv
        from vanilla_extract.provenance import Workspace
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        src = os.path.join(d, "src")
        os.makedirs(src)
        with open(os.path.join(src, "a.txt"), "w") as fh:
            fh.write("Total: $5.00\n")
        ws = Workspace(os.path.join(d, "ws"))
        ws.create("test", [src])
        results, _ = run([src], workspace=ws)
        ws.add_revision(results, ["file", "characters"], note="as extracted")
        table = os.path.join(d, "same.csv")
        write_csv(results, table, ["file", "characters"])
        rc, out, err = self._cli("--workspace", ws.root, "--import-csv", table)
        self.assertEqual(rc, 0, err)
        self.assertIn("nothing filed", out)
        self.assertEqual(len(ws.load()["revisions"]), 1)

    def test_a_duplicate_member_keeps_its_extension(self):
        from vanilla_extract.provenance import _safe_member
        name = _safe_member("/x/bundle.zip!dup.txt#2")
        self.assertTrue(name.startswith("dup_2_"), name)
        self.assertTrue(name.endswith(".txt"), name)

    def test_a_columns_example_is_a_value_of_the_columns_type(self):
        from vanilla_extract.recognize import infer_schema
        docs = ["Invoice Date: 2026-07-06\n"] + [f"Invoice Date: 07/0{i}/2026\n" for i in range(1, 5)]
        field, = infer_schema(docs)
        self.assertEqual(field["kind"], "date_us")
        self.assertEqual(field["example"], "07/01/2026")

    def test_the_linux_uninstaller_leaves_no_empty_folders_behind(self):
        if shutil.which("bash") is None:
            self.skipTest("no bash")
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prefix = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, prefix, True)
        os.makedirs(os.path.join(prefix, "share"))        # something already there
        before = sorted(os.listdir(prefix))
        env = dict(os.environ, PREFIX=prefix)
        for script in ("install-linux.sh", "uninstall-linux.sh"):
            r = subprocess.run(["bash", os.path.join(root, "packaging", script)],
                               env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(prefix)), before)


def _chromium():
    """A headless Chromium through Playwright, or None where it is not installed.

    The report is a page, and what it does on reload, close and export is only
    answered by a browser. Playwright is a development tool, never a runtime
    dependency; without it these tests skip rather than vanish, so the count
    the documentation quotes is the same on every machine.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None
    try:
        pw = sync_playwright().start()
        return pw, pw.chromium.launch()
    except Exception:                     # noqa: BLE001 - no browser binary
        return None, None


class TestReviewPageInABrowser(unittest.TestCase):
    """review.html in a real browser: corrections must not vanish silently.

    The page said "Edits stay in this file" while a reload or a closed tab threw
    every correction away without a word -- a file:// page cannot write itself.
    """

    ROWS = [{"file": "/data/a.pdf", "characters": 12, "total": "$5.00", "text": "Total: $5.00"},
            {"file": "/data/b.pdf", "characters": 12, "total": "$7.00", "text": "Total: $7.00"}]

    @classmethod
    def setUpClass(cls):
        cls.pw, cls.browser = _chromium()

    @classmethod
    def tearDownClass(cls):
        if cls.browser:
            cls.browser.close()
            cls.pw.stop()

    def setUp(self):
        if not self.browser:
            self.skipTest("Playwright with Chromium is not installed")
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ctx = self.browser.new_context(accept_downloads=True)
        self.addCleanup(self.ctx.close)

    def _report(self, rows=None, name="review.html"):
        from vanilla_extract.report import write_report
        path = os.path.join(self.dir, name)
        write_report(rows or self.ROWS, [], path,
                     columns=["file", "characters", "total"])
        return "file://" + path

    def _edit(self, page, value, row=0):
        cell = page.locator("#results tr").nth(row).locator("td[contenteditable]").first
        cell.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(value)
        return cell

    def _export(self, page):
        import csv as _csv
        with page.expect_download() as dl:
            page.click("#export")
        with open(dl.value.path(), encoding="utf-8") as fh:
            return list(_csv.DictReader(fh))

    def test_the_header_does_not_claim_edits_are_saved_in_the_file(self):
        from vanilla_extract.report import write_report
        path = os.path.join(self.dir, "r.html")
        write_report(self.ROWS, [], path)
        text = page_of(path).text
        self.assertNotIn("Edits stay in this file", text)
        self.assertIn("live in this tab until you export them or close the tab", text)
        self.assertIn("nothing is saved to disk or uploaded", text)

    def test_two_reports_of_the_same_data_in_the_same_minute_use_different_keys(self):
        import re as _re
        from vanilla_extract.report import write_report
        keys = []
        for name in ("a.html", "b.html"):
            path = os.path.join(self.dir, name)
            write_report(self.ROWS, [], path)
            keys.append(_re.search(r"STORE_KEY = '([^']+)'", page_of(path).script_text).group(1))
        self.assertNotEqual(keys[0], keys[1])

    def test_another_tab_cannot_read_the_corrections(self):
        """Owner decision: per-tab sessionStorage, not localStorage, which any
        file:// page in the browser could read and which outlives a restart."""
        page = self.ctx.new_page()
        url = self._report()
        page.goto(url)
        self._edit(page, "$9.99")
        other = self.ctx.new_page()
        other.goto(url)                       # the same report, a new tab
        cell = other.locator("#results tr").first.locator("td[contenteditable]").first
        self.assertEqual(cell.inner_text().strip(), "$5.00")
        stranger = self.ctx.new_page()
        stranger.goto(self._report(name="other.html"))
        leaked = stranger.evaluate(
            "JSON.stringify(Object.assign({}, localStorage)) + JSON.stringify(Object.assign({}, sessionStorage))")
        self.assertNotIn("9.99", leaked)
        self.assertEqual(page.evaluate("localStorage.length"), 0)

    def test_the_store_is_emptied_by_a_successful_export(self):
        page = self.ctx.new_page()
        page.goto(self._report())
        self._edit(page, "$9.99")
        self.assertGreater(page.evaluate("sessionStorage.length"), 0)
        self._export(page)
        self.assertEqual(page.evaluate("sessionStorage.length"), 0)

    def test_leaving_with_unexported_edits_asks_first(self):
        page = self.ctx.new_page()
        page.goto(self._report())
        self._edit(page, "$9.99")
        seen = []
        page.on("dialog", lambda d: (seen.append(d.type), d.dismiss()))
        try:
            page.reload(timeout=3000)
        except Exception:                 # noqa: BLE001 - dismissed = stays put
            pass
        self.assertEqual(seen, ["beforeunload"])

    def test_no_prompt_once_the_edits_are_exported(self):
        page = self.ctx.new_page()
        page.goto(self._report())
        self._edit(page, "$9.99")
        self._export(page)
        seen = []
        page.on("dialog", lambda d: (seen.append(d.type), d.accept()))
        page.reload()
        self.assertEqual(seen, [])

    def test_edits_survive_a_reload(self):
        page = self.ctx.new_page()
        page.goto(self._report())
        self._edit(page, "$9.99")
        page.on("dialog", lambda d: d.accept())
        page.reload()
        cell = page.locator("#results tr").first.locator("td[contenteditable]").first
        self.assertEqual(cell.inner_text().strip(), "$9.99")
        self.assertIn("edited", cell.get_attribute("class") or "")

    def test_export_carries_exactly_the_one_changed_cell(self):
        page = self.ctx.new_page()
        page.goto(self._report())
        self._edit(page, "$9.99", row=1)
        rows = self._export(page)
        changed = [(r["file"], k) for r, orig in zip(rows, self.ROWS)
                   for k in ("file", "characters", "total")
                   if r[k] != str(orig[k])]
        self.assertEqual(changed, [("/data/b.pdf", "total")])
        self.assertEqual(rows[1]["total"], "$9.99")

    def test_another_report_at_the_same_path_does_not_inherit_the_edits(self):
        """Every file:// page shares one origin, so the store must be keyed."""
        page = self.ctx.new_page()
        url = self._report()
        page.goto(url)
        self._edit(page, "$9.99")
        page.on("dialog", lambda d: d.accept())
        other = [dict(r, total="$1.00") for r in self.ROWS]
        self._report(rows=other)          # same file name, different report
        page.reload()
        cell = page.locator("#results tr").first.locator("td[contenteditable]").first
        self.assertEqual(cell.inner_text().strip(), "$1.00")

    def test_export_keeps_a_negative_amount_a_number(self):
        page = self.ctx.new_page()
        page.goto(self._report())
        self._edit(page, "-$251.00")
        self._edit(page, "-2+3+cmd|' /C calc'!A0", row=1)
        rows = self._export(page)
        self.assertEqual([r["total"] for r in rows],
                         ["-$251.00", "'-2+3+cmd|' /C calc'!A0"])

    def _layout(self, width, rows=None, columns=None):
        from vanilla_extract.report import write_report
        path = os.path.join(self.dir, f"layout{width}.html")
        long = "a long extracted value that runs on for a good many words " * 2
        rows = rows or [dict({"file": f"/data/evidence_package.zip!0{i}_System_Security_Plan.pdf",
                              "characters": 1000}, **{f"field {c}": long for c in range(6)})
                        for i in range(3)] + [{"file": "/data/no_fields_here.pdf", "characters": 9}]
        write_report(rows, [], path, columns=["file", "characters"] + [f"field {c}" for c in range(6)])
        page = self.ctx.new_page()
        page.set_viewport_size({"width": width, "height": 900})
        page.goto("file://" + path)
        return page

    def test_file_names_stay_readable_at_1400_and_on_a_phone(self):
        """They wrapped a letter or two per line once the value columns took
        the width."""
        for width in (1400, 390):
            page = self._layout(width)
            narrowest = page.evaluate(
                "Math.min(...[...document.querySelectorAll('td.file')]"
                ".map(td => td.getBoundingClientRect().width))")
            self.assertGreaterEqual(narrowest, 120, width)

    def test_the_no_fields_badge_fits_inside_its_cell(self):
        for width in (1400, 390):
            page = self._layout(width)
            badge, cell = page.evaluate(
                "(() => { const b = document.querySelector('.nofields');"
                " return [b.getBoundingClientRect().right,"
                " b.closest('td').getBoundingClientRect().right]; })()")
            self.assertLessEqual(badge, cell, width)

    def test_the_datasheet_uses_the_width_and_keeps_dates_on_one_line(self):
        from vanilla_extract.fileinfo import stat_record
        from vanilla_extract.report import write_datasheet
        doc = os.path.join(self.dir, "a_document_with_a_fairly_long_name.pdf")
        with open(doc, "w") as fh:
            fh.write("x")
        path = os.path.join(self.dir, "sheet.html")
        write_datasheet([stat_record(doc)] * 3, path)
        page = self.ctx.new_page()
        page.set_viewport_size({"width": 1920, "height": 900})
        page.goto("file://" + path)
        wrap = page.evaluate("document.querySelector('.tablewrap').getBoundingClientRect().width")
        self.assertGreater(wrap, 1800)
        lines = page.evaluate("""[...document.querySelectorAll('#sheet thead th')]
          .map((th, i) => [th.dataset.col, i])
          .filter(([c]) => ['modified', 'accessed', 'inode_changed', 'captured_at'].includes(c))
          .map(([c, i]) => { const td = document.querySelector('#sheet tbody tr').children[i];
                 const r = document.createRange(); r.selectNodeContents(td);
                 return [c, new Set([...r.getClientRects()].map(x => Math.round(x.top))).size]; })""")
        self.assertTrue(lines)
        self.assertEqual({c: n for c, n in lines}, {c: 1 for c, _n in lines})

    def test_the_page_works_when_storage_throws(self):
        """Private windows and locked-down browsers refuse localStorage."""
        self.ctx.add_init_script(
            "for (const k of ['localStorage', 'sessionStorage']) "
            "Object.defineProperty(window, k, "
            "{get(){ throw new DOMException('denied', 'SecurityError'); }});")
        page = self.ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(self._report())
        self._edit(page, "$9.99")
        rows = self._export(page)
        self.assertEqual(rows[0]["total"], "$9.99")
        self.assertEqual(errors, [])
        self._edit(page, "$8.88")
        seen = []
        page.on("dialog", lambda d: (seen.append(d.type), d.dismiss()))
        try:
            page.reload(timeout=3000)
        except Exception:                 # noqa: BLE001
            pass
        self.assertEqual(seen, ["beforeunload"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
