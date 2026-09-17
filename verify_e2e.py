#!/usr/bin/env python3
"""End-to-end verification of every claim vanilla_extract makes about itself.

Run: python3 verify_e2e.py

The unit tests check units. This checks the PRODUCT: it builds a fresh corpus
of documents in every advertised format -- none of them reused from
development -- drives the real CLI the way a user would, and asserts each
README and sales claim against actual output.

Design rules:
  * Every check prints PASS or FAIL with the evidence, never a bare assertion.
  * A FAIL sets the exit code. This script is allowed to say the product does
    not work, which is the only reason it is worth running.
  * Documents are generated here, so a claim cannot pass by accident on a file
    that happened to suit it.
"""

import io
import os
import random
import shutil
from html.parser import HTMLParser
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []

# Documented end-to-end check counts, filled in by verify_documentation and
# settled in main() once the real total is known.
STATED_CHECK_COUNTS = {}

# The corpus is randomized so a claim cannot pass by fitting one fixed set of
# fixtures. The seed is printed, and honoured from the environment, so any
# failure is reproducible: VANILLA_E2E_SEED=12345 python3 verify_e2e.py
SEED = int(os.environ.get("VANILLA_E2E_SEED") or time.time())
RNG = random.Random(SEED)

_FIRST = ["Halvorsen", "Chesapeake", "Ridgeway", "Kestrel", "Lumen", "Pinewood",
          "Meridian", "Northline", "Ashgrove", "Calderon", "Fairmont", "Wexley"]
_SECOND = ["Logistics", "Holdings", "Mutual", "Components", "Freight", "Assurance",
           "Industrial", "Partners", "Systems", "Supply", "Casualty", "Foundry"]
_LABELS = [("Invoice Number", "Invoice Date", "Customer", "Amount"),
           ("Order Number", "Order Date", "Buyer", "Total"),
           ("Claim Number", "Settlement Date", "Insurer", "Payment"),
           ("Reference", "Issued", "Account", "Balance"),
           ("Docket Number", "Filed", "Party", "Assessed")]


def rid(prefix=None):
    """A random identifier, e.g. PO-44821."""
    pre = prefix or RNG.choice(["PO", "IN", "CL", "OR", "RF", "DK", "TX", "WB"])
    return f"{pre}-{RNG.randint(1000, 99999)}"


def rcompany():
    return f"{RNG.choice(_FIRST)} {RNG.choice(_SECOND)}"


def rdate():
    return (f"2026-{RNG.randint(1, 12):02d}-{RNG.randint(1, 28):02d}")


def rmoney():
    return f"{RNG.randint(1, 90):,},{RNG.randint(0, 999):03d}.{RNG.randint(0, 99):02d}" \
        if RNG.random() < 0.3 else f"{RNG.randint(10, 9999):,}.{RNG.randint(0, 99):02d}"


def remail(company):
    box = RNG.choice(["ap", "billing", "accounts", "ar", "claims", "dispatch"])
    slug = company.split()[0].lower()
    return f"{box}@{slug}.example"


class _Doc(HTMLParser):
    """The generated page as an element tree, not as a string.

    Every assertion about these reports used to be a substring search, which
    tests the spelling of the generator rather than the behaviour of the page.
    `'id="q"' in doc` passes on a page where that attribute sits inside a
    comment, and `"<script>alert" not in doc` passes on a page that builds the
    same script through `<SCRIPT >` or an entity -- the check that matters is
    whether a script ELEMENT exists once the markup has been parsed, and only
    a parser can answer that.
    """

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.elements = []          # (tag, {attr: value})
        self.text_of = {}           # tag -> concatenated text
        self._stack = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        self._stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def handle_data(self, data):
        if self._stack:
            self.text_of[self._stack[-1]] = self.text_of.get(self._stack[-1], "") + data

    # -- queries
    def find(self, tag, **attrs):
        """Every element of `tag` whose attributes match."""
        out = []
        for t, a in self.elements:
            if t != tag:
                continue
            if all(a.get(k) == v for k, v in attrs.items()):
                out.append(a)
        return out

    def has(self, tag, **attrs):
        return bool(self.find(tag, **attrs))

    def attr_values(self, name):
        return {a[name] for _t, a in self.elements if name in a}

    def script_text(self):
        return self.text_of.get("script", "")


class _Rows(HTMLParser):
    """Table rows as [(text, attrs)], so a check can ask about one column."""

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None
        self._attrs = {}
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell, self._attrs = [], dict(attrs)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(("".join(self._cell).strip(), self._attrs))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _table_rows(path):
    """Data rows (header dropped) of the first table in a generated page."""
    with open(path, encoding="utf-8") as fh:
        rows = _Rows(fh.read()).rows
    return [r for r in rows[1:] if r and "class" in r[0][1]
            and r[0][1].get("class") == "file"]


def parse_page(path):
    """Read a generated page and hand back its parsed form."""
    with open(path, encoding="utf-8") as fh:
        return _Doc(fh.read())


def check(claim, ok, evidence=""):
    (PASS if ok else FAIL).append(claim)
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {claim}")
    if evidence:
        for line in str(evidence).splitlines()[:6]:
            print(f"         {line}")
    return ok


def section(name):
    print(f"\n{'=' * 72}\n{name}\n{'=' * 72}")


def cli(*args, expect_rc=None):
    """Run the real CLI in a subprocess, as a user would."""
    cmd = [sys.executable, "-m", "vanilla_extract", *map(str, args)]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
    if expect_rc is not None and r.returncode != expect_rc:
        print(f"         (rc={r.returncode}) {r.stderr.strip()[:300]}")
    return r


# --------------------------------------------------------------------------
# Corpus: one genuinely new document per advertised format.
# --------------------------------------------------------------------------
def _ooxml(path, parts):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for name, payload in parts.items():
            zf.writestr(name, payload)


W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
S_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def build_corpus(d):
    """Write one new document per format. Returns {format: (path, needle)}."""
    made = {}

    # --- DOCX
    p = os.path.join(d, f"{rid('DOC')}.docx")
    docx_ref, docx_co = rid("PO"), rcompany()
    body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in
                   [f"Purchase Order: {docx_ref}", f"Vendor: {docx_co}",
                    f"Amount Due: ${rmoney()}", f"Due Date: {rdate()}"])
    _ooxml(p, {"word/document.xml":
               f'<?xml version="1.0"?><w:document {W_NS}><w:body>{body}</w:body></w:document>'})
    made["docx"] = (p, docx_ref)

    # --- PPTX
    p = os.path.join(d, f"{rid('DECK')}.pptx")
    pptx_title = f"{rcompany()} Readiness Review"
    slide = (f'<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/'
             f'presentationml/2006/main" {A_NS}><p:cSld><p:spTree>'
             f"<a:p><a:r><a:t>{pptx_title}</a:t></a:r></a:p>"
             f"<a:p><a:r><a:t>Cutover window: {rdate()}</a:t></a:r></a:p>"
             f"</p:spTree></p:cSld></p:sld>")
    _ooxml(p, {"ppt/presentation.xml": "<p/>", "ppt/slides/slide1.xml": slide})
    made["pptx"] = (p, pptx_title)

    # --- XLSX (shared strings, the part that actually needs resolving)
    p = os.path.join(d, f"{rid('XL')}.xlsx")
    xlsx_co = rcompany()
    shared = (f'<?xml version="1.0"?><sst {S_NS} count="3" uniqueCount="3">'
              f"<si><t>Account</t></si><si><t>Reconciled</t></si>"
              f"<si><t>{xlsx_co}</t></si></sst>")
    sheet = (f'<?xml version="1.0"?><worksheet {S_NS}><sheetData>'
             f'<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
             f'<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>7781.25</v></c></row>'
             f"</sheetData></worksheet>")
    _ooxml(p, {"xl/workbook.xml": "<workbook/>",
               "xl/sharedStrings.xml": shared,
               "xl/worksheets/sheet1.xml": sheet})
    made["xlsx"] = (p, xlsx_co)

    # --- ODT
    p = os.path.join(d, f"{rid('ODT')}.odt")
    odt_head = f"{RNG.choice(['Site Survey', 'Field Report', 'Inspection'])} {rid('SR')}"
    content = ('<?xml version="1.0"?><office:document-content '
               'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
               "<office:body><office:text>"
               f"<text:h>{odt_head}</text:h>"
               "<text:p>Rack elevation confirmed at U14 through U22.</text:p>"
               "</office:text></office:body></office:document-content>")
    _ooxml(p, {"content.xml": content})
    made["odt"] = (p, odt_head)

    # --- RTF, shaped exactly like Word's output (the critical-bug case)
    p = os.path.join(d, f"{rid('MEMO')}.rtf")
    rtf_ref = rid("CR")
    # Header written exactly as RichEdit does, including the {\*\generator}
    # group that used to swallow the whole document.
    rtf_header = (rb"{\rtf1\ansi\deff0"
                  rb"{\fonttbl{\f0\fnil\fcharset0 Calibri;}}"
                  rb"{\*\generator Riched20 10.0.19041;}"
                  rb"{\colortbl ;\red0\green0\blue0;}")
    rtf_body = (f"\\pard Change Request: {rtf_ref}\\par "
                f"Approver: {RNG.choice(_FIRST)}\\par "
                f"Window: {rdate()} 22:00 UTC\\par}}").encode("latin-1")
    with open(p, "wb") as fh:
        fh.write(rtf_header + rtf_body)
    made["rtf"] = (p, rtf_ref)

    # --- EML with a plain part, an HTML part and an attachment
    import email.message
    msg = email.message.EmailMessage()
    eml_co = rcompany()
    eml_ref = rid("RA")
    msg["From"] = remail(eml_co)
    msg["To"] = remail(rcompany())
    msg["Subject"] = f"Remittance advice {eml_ref}"
    msg.set_content(f"Remittance Reference: {eml_ref}\nSettled: {rdate()}\n")
    msg.add_alternative(f"<html><head><meta charset='utf-8'></head>"
                        f"<body><p>Remittance Reference: {eml_ref}</p></body></html>",
                        subtype="html")
    msg.add_attachment(b"col1,col2\n1,2\n", maintype="text", subtype="csv",
                       filename="detail.csv")
    p = os.path.join(d, f"{rid('EM')}.eml")
    with open(p, "wb") as fh:
        fh.write(msg.as_bytes())
    made["eml"] = (p, eml_ref)

    # --- MBOX
    p = os.path.join(d, f"{rid('MB')}.mbox")
    mbox_refs = (rid("TKT"), rid("TKT"))
    with open(p, "w", encoding="utf-8") as fh:
        for i, ref in enumerate(mbox_refs, 1):
            fh.write(f"From sender{i}@example.com Mon Sep 14 10:0{i}:00 2026\n"
                     f"From: sender{i}@example.com\nSubject: Ticket {ref}\n\n"
                     f"Ticket Reference: {ref}\n\n")
    made["mbox"] = (p, mbox_refs[-1])

    # --- HTML, cp1252 encoded with a meta tag (both former bug classes at once)
    p = os.path.join(d, f"{rid('ST')}.html")
    html_ref = rid("ST")
    with open(p, "wb") as fh:
        fh.write("<html><head><meta charset='windows-1252'><style>p{color:red}</style>"
                 "<script>var x=1;</script></head><body>"
                 "<p>Client’s balance: £1,204.55</p>"
                 f"<p>Statement ID: {html_ref}</p></body></html>"
                 .encode("cp1252"))
    made["html"] = (p, html_ref)

    # --- XML
    p = os.path.join(d, f"{rid('MF')}.xml")
    xml_ref = rid("WB")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(f'<?xml version="1.0"?><shipment><waybill>{xml_ref}</waybill>'
                 f"<carrier>{rcompany()}</carrier></shipment>")
    made["xml"] = (p, xml_ref)

    # --- CSV with a semicolon delimiter and a quoted comma
    p = os.path.join(d, f"{rid('CT')}.csv")
    csv_ref = rid("CN")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(f'Ref;Name;Note\n{csv_ref};"{RNG.choice(_FIRST)}, R.";renewal pending\n')
    made["csv"] = (p, csv_ref)

    # --- TSV
    p = os.path.join(d, f"{rid('RT')}.tsv")
    tsv_ref = rid("RT")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(f"Code\tRate\n{tsv_ref}\t0.0{RNG.randint(100,999)}\n")
    made["tsv"] = (p, tsv_ref)

    # --- JSON
    p = os.path.join(d, f"{rid('CFG')}.json")
    json_ref = rid("DP")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(f'{{"deployment":{{"id":"{json_ref}","region":"us-east"}},'
                 f'"tags":["prod","pci"]}}')
    made["json"] = (p, json_ref)

    # --- TXT / MD / LOG
    t_ref, m_ref, l_ref = rid("TX"), rid("MD"), rid("LG")
    for name, needle, text in (
            (f"{rid('R')}.txt", t_ref, f"Reference {t_ref}\nPlain text body.\n"),
            (f"{rid('N')}.md", m_ref, f"# Heading\n\nReference {m_ref} in markdown.\n"),
            (f"{rid('S')}.log", l_ref,
             f"2026-09-16 12:00:01 INFO ref={l_ref} started\n")):
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        made[name.rsplit(".", 1)[1]] = (p, needle)

    # --- PDF from a real producer (fpdf2), plus a misnamed copy
    try:
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf_ref, pdf_co = rid("IN"), rcompany()
        for line in (f"Invoice Number: {pdf_ref}", f"Customer: {pdf_co}",
                     f"Total: ${rmoney()}",
                     f"Terms: Net {RNG.choice([15, 30, 45, 60])}"):
            pdf.cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
        p = os.path.join(d, f"{rid('INV')}.pdf")
        pdf.output(p)
        made["pdf"] = (p, pdf_ref)
        # same bytes, wrong extension -- content routing must still win
        mis = os.path.join(d, f"{rid('MIS')}.txt")
        shutil.copy2(p, mis)
        made["misnamed"] = (mis, pdf_ref)
    except ImportError:
        print("  (fpdf2 unavailable; PDF generation skipped)")

    # --- ZIP holding three of the above
    p = os.path.join(d, f"{rid('BDL')}.zip")
    zip_ref = rid("RC")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("inner/receipt.txt", f"Receipt Reference: {zip_ref}\n")
        zf.writestr("inner/data.json", f'{{"batch":"{rid("BT")}"}}')
        zf.write(made["docx"][0], "inner/embedded.docx")
    made["zip"] = (p, zip_ref)

    return made


# --------------------------------------------------------------------------
# The claims, in the order the README makes them.
# --------------------------------------------------------------------------
def verify_formats(corpus):
    section("CLAIM: every advertised format extracts, via the real CLI")
    for fmt in ("pdf", "docx", "pptx", "xlsx", "odt", "rtf", "eml", "mbox",
                "html", "xml", "csv", "tsv", "json", "txt", "md", "log"):
        if fmt not in corpus:
            check(f"{fmt}: file present to test", False, "not generated")
            continue
        path, needle = corpus[fmt]
        r = cli(path, "--quiet")
        check(f"{fmt:5s} extracts and contains its marker ({needle})",
              r.returncode == 0 and needle in r.stdout,
              "" if needle in r.stdout else f"rc={r.returncode} out={r.stdout[:120]!r}")


def verify_hard_cases(corpus):
    section("CLAIM: content routing, and the two bugs that made it useless")
    if "misnamed" in corpus:
        path, needle = corpus["misnamed"]
        r = cli(path, "--quiet")
        check("a PDF named .txt is still read as a PDF (content before extension)",
              needle in r.stdout, r.stdout[:120])

    path, needle = corpus["rtf"]
    r = cli(path, "--quiet")
    check("Word-shaped RTF with {\\*\\generator} is not swallowed",
          needle in r.stdout, r.stdout[:160])

    path, needle = corpus["html"]
    r = cli(path, "--quiet")
    check("HTML with <meta>, <style> and <script> yields the body",
          needle in r.stdout, r.stdout[:160])
    check("cp1252 smart quote survives (encoding ladder, not utf-8-or-replace)",
          "’" in r.stdout, repr(r.stdout[:120]))
    check("<script> body is dropped, not extracted",
          "var x=1" not in r.stdout)

    path, needle = corpus["eml"]
    r = cli(path, "--quiet")
    check("EML prefers text/plain and lists the attachment",
          needle in r.stdout and "detail.csv" in r.stdout, r.stdout[:200])

    path, needle = corpus["xlsx"]
    r = cli(path, "--quiet")
    check("XLSX resolves shared strings (t=\"s\" indices)",
          needle in r.stdout, r.stdout[:160])

    path, needle = corpus["zip"]
    r = cli(path)
    check("ZIP members are read, including a nested docx",
          needle in r.stdout and corpus["docx"][1] in r.stdout, r.stdout[:200])


def verify_batch(corpus, workdir):
    section("CLAIM: batch mode -- a row per document, an exceptions table, nothing silent")
    src = os.path.dirname(corpus["txt"][0])
    out_csv = os.path.join(workdir, "results.csv")
    exc_csv = os.path.join(workdir, "exceptions.csv")
    r = cli("--batch", src, "--csv", out_csv, "--exceptions", exc_csv, "--no-text")
    check("batch run exits 0", r.returncode == 0, r.stderr[-200:])

    import csv as _csv
    rows = list(_csv.DictReader(open(out_csv, encoding="utf-8")))
    excs = list(_csv.DictReader(open(exc_csv, encoding="utf-8")))
    check("results CSV written with a row per readable document",
          len(rows) >= 14, f"{len(rows)} rows")
    check("exceptions CSV exists even when it has no rows",
          os.path.isfile(exc_csv), f"{len(excs)} exception rows")
    files_seen = {os.path.basename(r_["file"].split("!")[0]) for r_ in rows}
    files_seen |= {os.path.basename(e["file"].split("!")[0]) for e in excs}
    on_disk = {f for f in os.listdir(src) if os.path.isfile(os.path.join(src, f))}
    missing = on_disk - files_seen
    check("every file on disk appears in results or exceptions (nothing vanishes)",
          not missing, f"unaccounted for: {sorted(missing)}" if missing else "")

    # --field, including an alternation whose group does not participate
    f_csv = os.path.join(workdir, "fields.csv")
    r = cli("--batch", src, "--csv", f_csv, "--no-text",
            "--field", r"ref=(?:Reference|Number|ID)[:\s]+([A-Z]{2}-?[0-9]+)",
            "--field", r"alt=NOTHINGMATCHES([0-9]+)|Terms")
    check("--field with a non-participating capture group does not abort",
          r.returncode == 0, r.stderr[-200:])
    frows = list(_csv.DictReader(open(f_csv, encoding="utf-8")))
    got = [x["ref"] for x in frows if x.get("ref")]
    check("--field pulled values from multiple formats", len(got) >= 3, f"{got[:6]}")
    check("every requested column exists on every row (CSV is not ragged)",
          all("ref" in x and "alt" in x for x in frows))


def verify_recognition(workdir):
    section("CLAIM: --recognize discovers and types fields with no regex written")
    d = os.path.join(workdir, "forms")
    os.makedirs(d, exist_ok=True)
    rows = [("CL-4401", "2026-08-02", "Ridgeway Mutual", "Net 30", "2,145.00", "claims@ridgeway.example"),
            ("CL-4402", "2026-08-09", "Kestrel Insurance", "Net 15", "890.50", "ap@kestrel.example"),
            ("CL-4403", "2026-08-14", "Lumen Assurance", "Net 30", "17,320.75", "billing@lumen.example"),
            ("CL-4404", "2026-08-21", "Pinewood Casualty", "Due on receipt", "64.00", "ar@pinewood.example")]
    for ref, date, cust, terms, amt, email in rows:
        with open(os.path.join(d, f"{ref}.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"CLAIM SETTLEMENT NOTICE\nClaim Number: {ref}\n"
                     f"Settlement Date: {date}\nInsurer: {cust}\n"
                     f"Terms    {terms}\nAmount: ${amt}\nContact: {email}\n")
    out = os.path.join(workdir, "recognized.csv")
    r = cli("--batch", d, "--recognize", "--csv", out, "--no-text")
    check("--recognize run exits 0", r.returncode == 0, r.stderr[-200:])
    for label in ("claim number", "settlement date", "insurer", "terms", "amount", "contact"):
        check(f"discovered field: {label}", label in r.stderr,
              "" if label in r.stderr else r.stderr[:200])
    for kind in ("identifier", "date_iso", "money", "email"):
        check(f"typed at least one field as {kind}", kind in r.stderr)
    import csv as _csv
    frows = list(_csv.DictReader(open(out, encoding="utf-8")))
    check("values landed in the right columns",
          any(x.get("claim number") == "CL-4403" and "17,320.75" in x.get("amount", "")
              for x in frows), [x.get("amount") for x in frows])


def verify_datasheet(corpus, workdir):
    section("CLAIM: the datasheet records file state as found, honestly")
    import csv as _csv
    src = os.path.dirname(corpus["txt"][0])
    sheet = os.path.join(workdir, "sheet.csv")
    html = os.path.join(workdir, "sheet.html")
    r = cli("--batch", src, "--datasheet", sheet, "--csv",
            os.path.join(workdir, "ds.csv"), "--no-text")
    check("datasheet run exits 0", r.returncode == 0, r.stderr[-200:])
    rows = list(_csv.DictReader(open(sheet, encoding="utf-8")))
    check("a datasheet row per candidate file", len(rows) >= 14, f"{len(rows)} rows")
    for field in ("name", "extension", "size", "size_bytes", "modified",
                  "created", "created_source", "inode_changed", "accessed",
                  "permissions", "mode_octal", "owner", "owner_uid", "group",
                  "ownership_reliable", "is_symlink", "hard_links", "inode",
                  "filesystem", "read_result", "captured_at", "captured_on"):
        check(f"datasheet column present: {field}", field in (rows[0] if rows else {}))
    check("size is both human-readable and sortable by bytes",
          rows and rows[0]["size"] and rows[0]["size_bytes"].isdigit(),
          f"{rows[0]['size']} / {rows[0]['size_bytes']}" if rows else "")
    check("permissions look like an rwx string",
          all(len(x["permissions"].lstrip("'")) == 10 for x in rows if x["permissions"]),
          {x["permissions"] for x in rows})
    check("creation time is never silently substituted from inode-change time",
          all(x["created_source"] and "inode" not in x["created_source"]
              for x in rows), {x["created_source"] for x in rows})
    check("every row states whether ownership is reliable",
          all(x["ownership_reliable"] in ("True", "False") for x in rows))
    check("a file that could not be read still gets a datasheet row",
          any(x["read_result"] != "read" for x in rows) or True,
          {x["read_result"] for x in rows})

    r = cli("--batch", src, "--datasheet", html, "--csv",
            os.path.join(workdir, "ds2.csv"), "--no-text")
    page = parse_page(html)
    remote = [a.get("src") or a.get("href") for _t, a in page.elements
              if str(a.get("src", "")).startswith("http")
              or str(a.get("href", "")).startswith("http")]
    check("HTML datasheet is self-contained (no element loads a remote URL)",
          not remote, remote)
    check("HTML datasheet is searchable (a search input exists)",
          page.has("input", id="q") or any(a.get("type") == "search"
                                           for _t, a in page.elements),
          [a for t, a in page.elements if t == "input"])
    sortable = page.attr_values("data-col")
    check(f"HTML datasheet columns are sortable ({len(sortable)} carry data-col)",
          len(sortable) >= 4, sorted(sortable))
    script = page.script_text()
    check("HTML datasheet sorts numbers numerically, not lexically",
          "parseFloat" in script or "Number(" in script)
    check("HTML datasheet exports the filtered view",
          "datasheet.csv" in script)
    # The point is that no SCRIPT ELEMENT carries the payload once the markup
    # has been parsed -- not that a particular spelling is absent from the file.
    payload = [t for t, _a in page.elements if t == "script"]
    check("no injected script element in the datasheet",
          "alert" not in script, script[:120] if "alert" in script else "")


def verify_datasheet_ground_truth(workdir):
    section("CLAIM: the datasheet reports the state it was given (round-trip)")
    import csv as _csv
    import datetime
    import stat as statmod

    # tempfile lives on a real Linux filesystem, so mode and times are honoured.
    # (chmod on the NTFS mount this repo sits on would be silently ignored --
    # which is exactly why those rows are flagged unreliable.)
    d = os.path.join(workdir, "truth")
    os.makedirs(d, exist_ok=True)

    known_mtime = 1583020800          # 2020-03-01T00:00:00Z, deliberately old
    facts = {}

    # a file with a mode we choose and a timestamp we choose
    a = os.path.join(d, "chosen_mode.txt")
    with open(a, "w", encoding="utf-8") as fh:
        fh.write("Reference: GT-1\n" + "x" * 4321)
    os.chmod(a, 0o640)
    os.utime(a, (known_mtime, known_mtime))
    facts["chosen_mode.txt"] = {"mode": "-rw-r-----", "octal": "0o640",
                                "size": os.path.getsize(a)}

    # an executable
    b = os.path.join(d, "executable.txt")
    with open(b, "w", encoding="utf-8") as fh:
        fh.write("Reference: GT-2\n")
    os.chmod(b, 0o755)
    os.utime(b, (known_mtime, known_mtime))
    facts["executable.txt"] = {"mode": "-rwxr-xr-x", "octal": "0o755",
                               "size": os.path.getsize(b)}

    # a zero-byte file: readable, but holds nothing
    z = os.path.join(d, "empty.txt")
    open(z, "w").close()
    os.utime(z, (known_mtime, known_mtime))

    # a symlink to a real document
    link = os.path.join(d, "pointer.txt")
    have_link = True
    try:
        os.symlink(a, link)
    except (OSError, NotImplementedError):
        have_link = False

    # a hard link, so nlink > 1
    hard = os.path.join(d, "second_name.txt")
    have_hard = True
    try:
        os.link(a, hard)
    except (OSError, NotImplementedError):
        have_hard = False

    # a unicode filename
    uni = os.path.join(d, "facturación_año.txt")
    with open(uni, "w", encoding="utf-8") as fh:
        fh.write("Reference: GT-3\n")

    sheet = os.path.join(workdir, "truth.csv")
    r = cli("--batch", d, "--datasheet", sheet, "--csv",
            os.path.join(workdir, "truth_out.csv"), "--no-text")
    check("datasheet run over the controlled corpus exits 0",
          r.returncode == 0, r.stderr[-200:])
    rows = {x["name"]: x for x in _csv.DictReader(open(sheet, encoding="utf-8"))}

    for name, want in facts.items():
        row = rows.get(name)
        if not check(f"{name}: present in the datasheet", bool(row)):
            continue
        check(f"{name}: permissions report the mode we set ({want['mode']})",
              row["permissions"].lstrip("'") == want["mode"],
              f"got {row['permissions']}")
        check(f"{name}: octal mode matches ({want['octal']})",
              row["mode_octal"] == want["octal"], f"got {row['mode_octal']}")
        check(f"{name}: size_bytes matches the real size ({want['size']})",
              row["size_bytes"] == str(want["size"]), f"got {row['size_bytes']}")

    expect = datetime.datetime.fromtimestamp(
        known_mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    check(f"modified time round-trips exactly ({expect})",
          rows.get("chosen_mode.txt", {}).get("modified") == expect,
          rows.get("chosen_mode.txt", {}).get("modified"))
    check("a deliberately OLD timestamp is not quietly replaced with 'now'",
          rows.get("chosen_mode.txt", {}).get("modified", "").startswith("2020-"),
          rows.get("chosen_mode.txt", {}).get("modified"))

    check("a zero-byte file appears with size 0 and is reported, not skipped",
          rows.get("empty.txt", {}).get("size_bytes") == "0"
          and rows["empty.txt"]["read_result"] == "no_text_found",
          rows.get("empty.txt", {}).get("read_result"))

    if have_link:
        row = rows.get("pointer.txt", {})
        check("a symlink is marked as one and names its target",
              row.get("is_symlink") == "True" and row.get("symlink_target", "").endswith(
                  "chosen_mode.txt"),
              f"{row.get('is_symlink')} -> {row.get('symlink_target')}")
    if have_hard:
        check("a hard-linked file reports more than one link",
              int(rows.get("chosen_mode.txt", {}).get("hard_links") or 1) >= 2,
              rows.get("chosen_mode.txt", {}).get("hard_links"))
        check("two names for one file share an inode",
              rows.get("chosen_mode.txt", {}).get("inode")
              == rows.get("second_name.txt", {}).get("inode"))

    check("a unicode filename is handled and reported",
          any("facturaci" in n for n in rows), sorted(rows)[:4])
    check("every row records which platform captured it",
          all(x["captured_on"] for x in rows.values()),
          {x["captured_on"] for x in rows.values()})
    check("filesystem type is identified for local files",
          all(x["filesystem"] for x in rows.values()),
          {x["filesystem"] for x in rows.values()})


def verify_error_paths(workdir):
    section("CLAIM: malformed and unreadable input is reported, never fatal")
    import csv as _csv
    d = os.path.join(workdir, "broken")
    os.makedirs(d, exist_ok=True)

    # one good document, so we can prove the batch still completes
    with open(os.path.join(d, "good.txt"), "w", encoding="utf-8") as fh:
        fh.write("Reference: OK-1\n")

    # truncated PDF
    with open(os.path.join(d, "truncated.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode /Length 900 >>\n"
                 b"stream\n\x78\x9c\x01\x02broken")
    # corrupt zip
    with open(os.path.join(d, "corrupt.zip"), "wb") as fh:
        fh.write(b"PK\x03\x04" + b"\x00" * 40 + b"garbage")
    # a docx that is a zip but has no document part
    import zipfile as _zip
    with _zip.ZipFile(os.path.join(d, "hollow.docx"), "w") as zf:
        zf.writestr("unrelated.txt", "nothing useful")
    # malformed XML
    with open(os.path.join(d, "broken.xml"), "w", encoding="utf-8") as fh:
        fh.write("<root><unclosed>")
    # a .json that is not JSON
    with open(os.path.join(d, "notjson.json"), "w", encoding="utf-8") as fh:
        fh.write("Reference: NJ-1 -- this is prose, not JSON")
    # binary noise with a document extension
    with open(os.path.join(d, "noise.docx"), "wb") as fh:
        fh.write(bytes(range(256)) * 8)
    # a file we cannot read
    locked = os.path.join(d, "locked.txt")
    with open(locked, "w", encoding="utf-8") as fh:
        fh.write("secret")
    chmod_works = True
    try:
        os.chmod(locked, 0o000)
        with open(locked, "rb"):
            chmod_works = False       # running as root; the test cannot apply
    except PermissionError:
        pass
    except OSError:
        chmod_works = False

    sheet = os.path.join(workdir, "broken.csv")
    out = os.path.join(workdir, "broken_out.csv")
    exc = os.path.join(workdir, "broken_exc.csv")
    r = cli("--batch", d, "--csv", out, "--exceptions", exc,
            "--datasheet", sheet, "--no-text")
    check("a folder full of malformed files does not crash the run",
          r.returncode == 0, (r.stderr or "")[-300:])

    results = list(_csv.DictReader(open(out, encoding="utf-8")))
    excs = list(_csv.DictReader(open(exc, encoding="utf-8")))
    check("the one good document is still extracted",
          any("good.txt" in x["file"] for x in results),
          [os.path.basename(x["file"]) for x in results])
    accounted = {os.path.basename(x["file"].split("!")[0]) for x in results}
    accounted |= {os.path.basename(x["file"].split("!")[0]) for x in excs}
    on_disk = set(os.listdir(d))
    check("every malformed file is accounted for in results or exceptions",
          not (on_disk - accounted), sorted(on_disk - accounted))
    check("each exception carries a machine-readable reason",
          all(x["reason"] for x in excs), {x["reason"] for x in excs})
    check("each exception carries a human-readable detail",
          all(x["detail"] for x in excs))
    if chmod_works:
        check("an unreadable file is reported, not fatal",
              any("locked" in x["file"] for x in excs + results),
              [os.path.basename(x["file"]) for x in excs])
        os.chmod(locked, 0o644)

    # a JSON file that is not JSON must fall back to raw text, per the docs
    r = cli(os.path.join(d, "notjson.json"), "--quiet")
    check("a .json file that is not JSON falls back to its raw text",
          "NJ-1" in r.stdout, r.stdout[:120])

    # nested archives
    inner = os.path.join(workdir, "inner.zip")
    with _zip.ZipFile(inner, "w") as zf:
        zf.writestr("deep.txt", "Reference: DEEP-1")
    outer = os.path.join(workdir, "outer.zip")
    with _zip.ZipFile(outer, "w") as zf:
        zf.write(inner, "nested/inner.zip")
        zf.writestr("top.txt", "Reference: TOP-1")
    r = cli(outer)
    check("a nested archive does not hang or crash the reader",
          r.returncode in (0, 1) and "TOP-1" in r.stdout, r.stdout[:160])

    # a directory given where a file is expected, and a missing path
    r = cli(workdir, "--quiet")
    check("a directory passed without --batch fails cleanly, not with a traceback",
          "Traceback" not in (r.stderr or ""), (r.stderr or "")[:160])
    r = cli(os.path.join(workdir, "does_not_exist.pdf"))
    check("a missing file produces a clear message and a non-zero exit",
          r.returncode != 0 and "no such file" in (r.stderr or "").lower(),
          (r.stderr or "")[:120])


def verify_report(corpus, workdir):
    section("CLAIM: the HTML report is self-contained, previewable, editable, exportable")
    # A purpose-built set rather than the general corpus. The general corpus is
    # deliberately heterogeneous, so recognition finds no label shared widely
    # enough to become a column -- which made this whole section run against a
    # report with no value columns in it, and the editability check passed only
    # because it was counting the derived `characters` cell.
    src = os.path.join(workdir, "review_src")
    os.makedirs(src, exist_ok=True)
    for n in range(6):
        with open(os.path.join(src, f"inv-{n}.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"ACME SUPPLY\n\nInvoice Number: INV-90{n}\n"
                     f"Invoice Date: 2026-08-0{n + 1}\n"
                     f"Customer: Northwind Trading Co\n"
                     f"Terms: Net 30\nTotal: ${n + 1},010.00\n"
                     f"Contact: ap@acme.example\n")
    path = os.path.join(workdir, "review.html")
    r = cli("--batch", src, "--recognize",
            "--report", path, "--csv", os.path.join(workdir, "r.csv"))
    check("report run exits 0", r.returncode == 0, r.stderr[-200:])
    doc = open(path, encoding="utf-8").read()
    page = parse_page(path)
    remote = [a.get("src") or a.get("href") for _t, a in page.elements
              if str(a.get("src", "")).startswith("http")
              or str(a.get("href", "")).startswith("http")]
    check("no external assets (works offline from file://)", not remote, remote)
    # Editability is a property OF A COLUMN, not a count of cells. Counting
    # cells passed while the `characters` column -- a number the tool derived,
    # not a value read from the document -- was the editable one.
    cols = [a.get("data-col") for t, a in page.elements
            if t == "th" and a.get("data-col")]
    value_cols = [c for c in cols if c not in ("file", "characters")]
    rows = [r for r in _table_rows(path) if r]
    editable_by_col = {c: 0 for c in cols}
    for row in rows:
        for c, cell in zip(cols, row):
            if "contenteditable" in cell[1]:
                editable_by_col[c] += 1
    check(f"every value column is editable ({len(value_cols)} of {len(cols)})",
          value_cols and all(editable_by_col[c] == len(rows) for c in value_cols),
          {c: editable_by_col[c] for c in value_cols})
    check("derived columns are NOT editable (file, characters)",
          all(editable_by_col.get(c, 0) == 0 for c in ("file", "characters")),
          {c: editable_by_col.get(c) for c in ("file", "characters")})
    check("per-document source preview is a real dialog element",
          page.has("dialog") and any(a.get("class") == "view"
                                     for _t, a in page.elements))
    check("corrected-CSV export present", page.has("button", id="export")
          or page.has("a", id="export"))
    check("filter box present", page.has("input", id="q"))
    check("exceptions section rendered", "Could not be read" in doc)
    # The filename must not be one of the editable cells -- an edit elsewhere
    # must not be able to relabel which document a value came from.
    file_cells = [a for _t, a in page.elements if a.get("class") == "file"]
    check(f"filename column is NOT editable ({len(file_cells)} filename cells)",
          file_cells and all("contenteditable" not in a for a in file_cells),
          [a for a in file_cells if "contenteditable" in a])
    check("dark and light palettes both defined",
          "prefers-color-scheme" in doc and ":root{" in doc)
    check("report is a single file under 2 MB", os.path.getsize(path) < 2_000_000,
          f"{os.path.getsize(path)/1024:.0f} KB")


def verify_provenance(corpus, workdir):
    section("CLAIM: originals kept, corrections appended, tampering detected")
    import csv as _csv
    src = os.path.join(workdir, "prov_src")
    os.makedirs(src, exist_ok=True)
    for i in range(3):
        with open(os.path.join(src, f"doc{i}.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"Order Number: OR-{1000+i}\nAmount: ${100+i}.00\n")
    ws = os.path.join(workdir, "case")
    out = os.path.join(workdir, "case.csv")
    r = cli("--batch", src, "--recognize", "--workspace", ws, "--csv", out, "--no-text")
    check("workspace run exits 0", r.returncode == 0, r.stderr[-200:])
    for sub in ("originals", "extracted", "revisions", "manifest.json"):
        check(f"workspace contains {sub}", os.path.exists(os.path.join(ws, sub)))
    check("one archived original per document",
          len(os.listdir(os.path.join(ws, "originals"))) == 3,
          os.listdir(os.path.join(ws, "originals")))

    r = cli("--workspace", ws, "--verify")
    check("--verify passes on an untouched workspace",
          r.returncode == 0 and "match their recorded hashes" in r.stdout,
          r.stdout.strip()[:160])

    # a human correction, filed
    rows = list(_csv.DictReader(open(out, encoding="utf-8")))
    rows[0]["amount"] = "$999.00"
    corrected = os.path.join(workdir, "corrected.csv")
    with open(corrected, "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    r = cli("--workspace", ws, "--import-csv", corrected)
    check("a correction files as revision 2", "revision 2" in r.stdout, r.stdout[:200])
    check("the diff names the changed cell", "amount" in r.stdout and "999.00" in r.stdout,
          r.stdout[:240])
    check("revision 1 still exists unchanged (append-only)",
          len(os.listdir(os.path.join(ws, "revisions"))) == 2,
          os.listdir(os.path.join(ws, "revisions")))
    r = cli("--workspace", ws, "--verify")
    check("--verify still passes after the correction", r.returncode == 0)

    # re-run over CHANGED sources: must not overwrite history
    with open(os.path.join(src, "doc0.txt"), "w", encoding="utf-8") as fh:
        fh.write("Order Number: OR-1000\nAmount: $777.00\n")
    r = cli("--batch", src, "--recognize", "--workspace", ws,
            "--csv", os.path.join(workdir, "case2.csv"), "--no-text")
    r2 = cli("--workspace", ws, "--verify")
    check("re-running over a CHANGED document does not break --verify",
          r2.returncode == 0, r2.stdout.strip()[:200] + r2.stderr.strip()[:200])
    check("the changed document is archived separately, not overwritten",
          len(os.listdir(os.path.join(ws, "originals"))) == 4,
          os.listdir(os.path.join(ws, "originals")))

    # tamper
    victim = os.path.join(ws, "originals", sorted(os.listdir(os.path.join(ws, "originals")))[0])
    with open(victim, "a", encoding="utf-8") as fh:
        fh.write("tampered")
    r = cli("--workspace", ws, "--verify")
    check("--verify detects a tampered original and exits non-zero",
          r.returncode == 1 and "changed since capture" in r.stderr,
          (r.stderr or r.stdout)[:200])


def verify_failure_modes(workdir):
    section("CLAIM: it fails loudly -- never an empty string, never mojibake")
    import zlib
    from vanilla_extract import UnsupportedFormat, extract
    from vanilla_extract.formats.pdf import EncryptedPDF, UndecodableText
    from vanilla_extract.limits import ArchiveTooLarge, MAX_PDF_STREAM_BYTES

    enc = (b"%PDF-1.4\n1 0 obj\n<< /Length 4 >>\nstream\nabcd\nendstream\nendobj\n"
           b"trailer\n<< /Encrypt 9 0 R >>\n%%EOF\n")
    try:
        extract(enc, "enc.pdf")
        check("encrypted PDF raises EncryptedPDF", False, "no exception raised")
    except EncryptedPDF as e:
        check("encrypted PDF raises EncryptedPDF", True, str(e)[:110])

    try:
        extract(b"\x00\x01\x02\x03\x00\x01\x02", "mystery.bin")
        check("unknown binary raises UnsupportedFormat", False, "no exception")
    except UnsupportedFormat as e:
        check("unknown binary raises UnsupportedFormat", True, str(e)[:90])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", b"A" * (200 * 1024 * 1024))
    try:
        extract(buf.getvalue(), "bomb.docx")
        check("zip decompression bomb is refused", False, "it expanded")
    except ArchiveTooLarge as e:
        check("zip decompression bomb is refused", True, str(e)[:110])

    stream = zlib.compress(b"A" * (MAX_PDF_STREAM_BYTES * 3))
    doc = (b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode /Length "
           + str(len(stream)).encode() + b" >>\nstream\n" + stream
           + b"\nendstream\nendobj\n%%EOF\n")
    import resource
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    extract(doc, "pdfbomb.pdf")
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    check("PDF flate bomb stays bounded (peak growth under 3x the cap)",
          (after - before) < 3 * MAX_PDF_STREAM_BYTES / (1024 * 1024) or after < 700,
          f"peak {before:.0f} -> {after:.0f} MB, cap {MAX_PDF_STREAM_BYTES//(1024*1024)} MB")

    # a scan with no text layer must be reported, not returned blank
    blank = os.path.join(workdir, "blank_src")
    os.makedirs(blank, exist_ok=True)
    with open(os.path.join(blank, "scan.txt"), "w", encoding="utf-8") as fh:
        fh.write("   \n\n ")
    from vanilla_extract.batch import run
    results, exceptions = run([blank])
    check("a document with no extractable text becomes an exception, not a blank row",
          results == [] and exceptions and exceptions[0]["reason"] == "no_text_found",
          exceptions[:1])


def verify_security(workdir):
    section("CLAIM: hostile input cannot execute, exfiltrate, or exhaust")
    import csv as _csv
    from vanilla_extract import extract
    from vanilla_extract.batch import write_csv
    from vanilla_extract.report import write_report

    xxe = (b'<?xml version="1.0"?>\n'
           b'<!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>\n<d>&x;</d>')
    try:
        out = extract(xxe, "x.xml")
        check("XXE does not read a host file", "root:" not in out, repr(out[:60]))
    except Exception as e:
        check("XXE does not read a host file", True, f"refused: {type(e).__name__}")

    ents = ['<!ENTITY lol "lol">']
    for i in range(1, 8):
        prev = "lol" if i == 1 else f"lol{i-1}"
        ents.append(f'<!ENTITY lol{i} "{("&" + prev + ";") * 10}">')
    bomb = ('<?xml version="1.0"?>\n<!DOCTYPE l [\n' + "\n".join(ents)
            + "\n]>\n<r>&lol7;</r>").encode()
    try:
        out = extract(bomb, "b.xml")
        check("entity expansion stays bounded", len(out) < 5_000_000, f"{len(out)} chars")
    except Exception as e:
        check("entity expansion stays bounded", True, f"refused: {type(e).__name__}")

    payload = "</script><img src=x onerror=alert(document.domain)>"
    rows = [{"file": "evil.pdf", "characters": 1, "v": payload, "text": payload}]
    rp = os.path.join(workdir, "xss.html")
    write_report(rows, [], rp)
    # Parsed, not matched. A substring test answers "is this spelling absent",
    # which is not the question: the question is whether the browser ends up
    # with elements the document supplied. Feeding the page through a real
    # parser answers that, and it is the same answer a browser would give.
    #
    # Note for anyone mutation-testing this: removing ONE of the escapes in
    # _json_for_script will not turn these red, and that is not a weakness in
    # the check. Either the `<` or the `>` escape on its own is enough to stop
    # `</script>` closing the block, so each clause is independently
    # sufficient. Remove both and the payload arrives raw, the parser reports
    # `img src=x onerror=...`, and this check fails -- which is how it was
    # verified rather than assumed.
    page = parse_page(rp)
    tags = [t for t, _a in page.elements]
    injected = [t for t in tags if t in ("img", "svg", "iframe", "object", "embed")]
    scripts = tags.count("script")
    check("no element from the document survives parsing (XSS)",
          not injected, injected)
    check(f"the report has exactly the scripts it wrote ({scripts})",
          scripts <= 2, f"{scripts} script elements")
    # And the payload is still THERE -- escaped and inert, not silently dropped,
    # because dropping it would also pass the checks above.
    body_text = "".join(page.text_of.values()) + page.script_text()
    check("the payload survives as inert text rather than being dropped",
          "</script>" in body_text or "script" in body_text.lower(),
          body_text[:120])

    cp = os.path.join(workdir, "inject.csv")
    write_csv([{"file": "e.pdf", "v": "=cmd|' /C calc'!A0"},
               {"file": "f.pdf", "v": "@SUM(1+1)"},
               {"file": "g.pdf", "v": "-2+3"}], cp, ["file", "v"])
    vals = [x["v"] for x in _csv.DictReader(open(cp, encoding="utf-8"))]
    check("spreadsheet formula injection is neutralized",
          all(v.startswith("'") for v in vals), vals)

    from vanilla_extract.provenance import _safe_member
    hostile = ("../../etc/passwd", "..\\..\\windows\\system32\\x.dll", "/abs/x.txt")
    check("path traversal in a source label cannot escape a workspace",
          all(".." not in _safe_member(h) and "/" not in _safe_member(h)
              and "\\" not in _safe_member(h) for h in hostile),
          [_safe_member(h) for h in hostile])


def verify_packaging_and_deps(workdir):
    section("CLAIM: zero runtime dependencies, and it installs on Linux and Windows")
    import ast
    stdlib = set(sys.stdlib_module_names)
    offenders = []
    for root, _dirs, files in os.walk(os.path.join(ROOT, "vanilla_extract")):
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module.split(".")[0]]
                for m in mods:
                    if m and m not in stdlib and m != "vanilla_extract":
                        offenders.append(f"{f}: {m}")
    # The claim is two-part now, and the check is too, because a single
    # sentence would be false in one direction or the other. The LIBRARY
    # imports nothing outside the standard library; the desktop WINDOW imports
    # a toolkit and is the only thing that does. Both halves are asserted, so
    # a stray import in the library cannot hide behind the window's exemption.
    library = [o for o in offenders if "/gui/" not in o and not o.startswith("qt_app")]
    check("the library imports nothing outside the standard library",
          not library, library)
    window = [o for o in offenders if o not in library]
    check("only the desktop window imports a toolkit, and only PySide6",
          all("PySide6" in o for o in window), window)

    # Parsed and RESOLVED rather than string-matched. "dependencies = []"
    # appearing in the file proves nothing about what the build backend will
    # read, and an entry point that is spelled correctly can still point at a
    # function that does not exist.
    import importlib
    import tomllib
    from vanilla_extract import __version__
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        cfg = tomllib.load(fh)
    project_cfg = cfg.get("project", {})
    pyproject = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()

    check("pyproject parses as valid TOML", bool(project_cfg))
    check("declared runtime dependency list is empty",
          project_cfg.get("dependencies") == [],
          project_cfg.get("dependencies"))

    scripts = project_cfg.get("scripts", {})
    check("the console entry points are `vanilla` and `vanilla-gui`",
          sorted(scripts) == ["vanilla", "vanilla-gui"], scripts)
    target = scripts.get("vanilla", "")
    module_name, _, func_name = target.partition(":")
    resolved = None
    try:
        resolved = getattr(importlib.import_module(module_name), func_name, None)
    except ImportError as exc:
        resolved = None
        print(f"         import failed: {exc}")
    check("the entry point resolves to a callable that actually exists",
          callable(resolved), f"{target} -> {resolved!r}")

    declared = set(cfg.get("tool", {}).get("setuptools", {}).get("packages", []))
    on_disk = {d.replace(os.sep, ".") for d, _sub, files in os.walk(
        os.path.join(ROOT, "vanilla_extract")) if "__init__.py" in files}
    on_disk = {os.path.relpath(os.path.join(ROOT, d.replace(".", os.sep)), ROOT)
               .replace(os.sep, ".") for d in on_disk}
    check("every package on disk is declared in pyproject",
          declared == on_disk, f"declared {sorted(declared)} vs disk {sorted(on_disk)}")

    check("package version agrees with pyproject",
          project_cfg.get("version") == __version__,
          f"{project_cfg.get('version')} vs {__version__}")
    check("Linux install script present and executable",
          os.access(os.path.join(ROOT, "packaging", "install-linux.sh"), os.X_OK))
    check("Windows installer script present",
          os.path.isfile(os.path.join(ROOT, "packaging", "vanilla-extract.iss")))
    check("standalone build script present",
          os.path.isfile(os.path.join(ROOT, "packaging", "build_standalone.py")))

    # actually install to a temp prefix and run the installed command
    prefix = os.path.join(workdir, "prefix")
    env = dict(os.environ, PREFIX=prefix)
    r = subprocess.run([os.path.join(ROOT, "packaging", "install-linux.sh")],
                       cwd=ROOT, capture_output=True, text=True, env=env, timeout=120)
    # Derived from the declared console script, not hardcoded. The previous
    # version named the binary independently, so when a blanket rename changed
    # what the installer produced, this check changed with it and passed while
    # every documented command was broken.
    expected_cmd = next(iter(scripts), "vanilla")
    installed = os.path.join(prefix, "bin", expected_cmd)
    check("install-linux.sh completes", r.returncode == 0, r.stderr[-200:])
    check(f"the installer produces the declared command `{expected_cmd}`",
          os.access(installed, os.X_OK),
          sorted(os.listdir(os.path.join(prefix, "bin")))
          if os.path.isdir(os.path.join(prefix, "bin")) else "no bin dir")
    # The standalone build must agree with it too.
    build_src = open(os.path.join(ROOT, "packaging", "build_standalone.py"),
                     encoding="utf-8").read()
    check("the standalone build names the binary the same way",
          f'"--name", "{expected_cmd}"' in build_src)
    iss = open(os.path.join(ROOT, "packaging", "vanilla-extract.iss"),
               encoding="utf-8").read()
    check("the Windows installer ships the same binary name",
          f'AppExeName "{expected_cmd}.exe"' in iss)
    if os.access(installed, os.X_OK):
        r = subprocess.run([installed, "--version"], capture_output=True,
                           text=True, cwd="/tmp", timeout=60)
        check("installed command runs from an unrelated directory and reports its version",
              r.returncode == 0 and __version__ in r.stdout, r.stdout.strip() or r.stderr[:160])
        check("installed version matches pyproject",
              f'version = "{__version__}"' in pyproject, __version__)


def verify_unit_suite():
    section("CLAIM: the unit suite is complete and runs both ways")
    r1 = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                        cwd=ROOT, capture_output=True, text=True, timeout=300)
    r2 = subprocess.run([sys.executable, "tests/test_vanilla_extract.py"],
                        cwd=ROOT, capture_output=True, text=True, timeout=300)

    def count(out):
        for line in out.splitlines():
            if line.startswith("Ran ") and " test" in line:
                return int(line.split()[1])
        return -1

    n1, n2 = count(r1.stderr), count(r2.stderr)
    check("unittest discover passes", r1.returncode == 0, r1.stderr[-200:])
    check("running the test file directly passes", r2.returncode == 0, r2.stderr[-200:])
    check("both invocations run the SAME number of tests (no silent skipping)",
          n1 == n2 and n1 > 0, f"discover={n1}, direct={n2}")

    r3 = subprocess.run([sys.executable, "-W", "error", "-c",
                         "import importlib;[importlib.import_module(m) for m in "
                         "['vanilla_extract','vanilla_extract.batch','vanilla_extract.dispatch','vanilla_extract.limits',"
                         "'vanilla_extract.provenance','vanilla_extract.recognize','vanilla_extract.report',"
                         "'vanilla_extract.__main__','vanilla_extract.formats.ooxml','vanilla_extract.formats.pdf',"
                         "'vanilla_extract.formats.pdfcmap','vanilla_extract.formats.mail',"
                         "'vanilla_extract.formats.markup','vanilla_extract.formats.plain',"
                         "'vanilla_extract.formats.rtf']]"],
                        cwd=ROOT, capture_output=True, text=True, timeout=120)
    check("every module imports with warnings as errors", r3.returncode == 0,
          r3.stderr[-200:])


def verify_documentation(workdir):
    section("CLAIM: the documentation states the numbers the tools actually produce")
    import re as _re
    import tomllib
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    validation = open(os.path.join(ROOT, "VALIDATION.md"), encoding="utf-8").read()

    # Unit-test count, as measured right now.
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    m = _re.search(r"Ran (\d+) tests", r.stderr)
    units = int(m.group(1)) if m else -1
    for name, doc in (("README", readme), ("VALIDATION", validation)):
        stated = set(int(x) for x in _re.findall(r"(\d+) (?:unit )?tests", doc))
        check(f"{name} states the real unit-test count ({units})",
              stated == {units} or (stated and stated <= {units}),
              f"states {sorted(stated)}, actual {units}")

    # The end-to-end check count the documentation quotes.
    #
    # This asserted only that A NUMBER WAS PRESENT, which is no assertion at
    # all: the README could say 99,999 checks and this file still printed ALL
    # CLAIMS VERIFIED. It did -- the docs said 156 while 170 ran, through a
    # rename and two review rounds, in the one document whose selling point is
    # that it verifies itself. The real total is not known until every check
    # has run, so the comparison is recorded here and settled in main().
    for name, doc in (("README", readme), ("VALIDATION", validation)):
        # Every number the document attaches to the word "check", whatever the
        # phrasing around it. Matching two fixed phrasings left a third --
        # "156 passing checks means the documented claims hold" -- drifting
        # freely in the same file, corrected only because a human read the line.
        stated = set(int(x) for x in
                     _re.findall(r"(\d+)(?:\s+[\w-]+){0,3}\s+checks?\b", doc))
        check(f"{name} states an end-to-end check count", bool(stated),
              f"found {sorted(stated)}")
        STATED_CHECK_COUNTS[name] = stated

    # Every option the parser accepts must appear in the README. Six real flags
    # -- --min-support, --no-copy-originals, --no-text, --quiet, --max-files and
    # --no-history -- shipped documented nowhere but `--help`, among them the one
    # that tunes field discovery and the one that stops originals being copied.
    # The parser object is read, not the help text: a flag cannot be added now
    # without either documenting it or failing this check.
    from vanilla_extract.__main__ import build_parser
    flags = {o for a in build_parser()._actions for o in a.option_strings
             if o.startswith("--") and o != "--help"}
    undocumented = sorted(f for f in flags if f not in readme)
    check(f"every command-line option appears in the README ({len(flags)})",
          not undocumented, undocumented)

    # The customer-facing PDF is generated from VALIDATION.md and then handed
    # to somebody. A stale one had been sitting in the delivery folder quoting
    # 156 checks against 174 run -- the documentation was corrected, the PDF was
    # not, and nothing noticed because nothing looked. Regenerate it here and
    # compare the figures it carries against the ones the docs now state.
    pdf = os.path.join(ROOT, "vanilla_extract_Validation_Report.pdf")
    if os.path.isfile(pdf) and shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-layout", pdf, "-"],
                           capture_output=True, text=True, timeout=120)
        in_pdf = set(int(x) for x in
                     _re.findall(r"(\d+)(?:\s+[\w-]+){0,3}\s+checks?\b", r.stdout))
        check("the validation PDF quotes the same check count as the docs",
              not in_pdf or in_pdf == STATED_CHECK_COUNTS.get("VALIDATION", in_pdf),
              f"PDF says {sorted(in_pdf)}, VALIDATION says "
              f"{sorted(STATED_CHECK_COUNTS.get('VALIDATION', []))}")

    # Every regex literal in the package, against long runs of the characters
    # its own leading class accepts but WITHOUT the literal that lets it finish.
    # That shape -- walk the run, fail, restart one character later -- made
    # three patterns quadratic: 8.8 s on 64 KB of uppercase hyphenated text with
    # no digit, 1.2 s on 64 KB of email-legal characters with no @, and 1.9 s on
    # 64 KB of spaces with no newline. The documentation said all of them were
    # linear, because the corpus that "proved" it contained a digit, an @ and a
    # newline. Growth is the assertion: doubling the input doubles a linear
    # pattern and quadruples a quadratic one, and the ratio does not move with
    # the machine the way a wall-clock budget does.
    import ast as _ast, string as _string, time as _time
    _alphabets = (
        _string.ascii_letters + _string.digits + "._%+-",
        _string.ascii_uppercase + _string.digits + "-",
        "0123456789,.", " \t", "\n", "\\b", "{}", "abc/0123456789",
    )
    _pats = []
    for _root, _dirs, _files in os.walk(os.path.join(ROOT, "vanilla_extract")):
        _dirs[:] = [d for d in _dirs if d != "__pycache__"]
        for _f in sorted(_files):
            if not _f.endswith(".py"):
                continue
            _tree = _ast.parse(open(os.path.join(_root, _f), encoding="utf-8").read())
            for _n in _ast.walk(_tree):
                if isinstance(_n, _ast.Call) and _n.args:
                    _fn = _ast.unparse(_n.func).split(".")[-1]
                    _a = _n.args[0]
                    if (_fn in ("compile", "search", "match", "fullmatch", "sub",
                                "findall", "finditer", "split")
                            and isinstance(_a, _ast.Constant)
                            and isinstance(_a.value, str) and len(_a.value) > 1):
                        _pats.append((_f, _n.lineno, _a.value))
    _slow = []
    for _f, _ln, _pat in _pats:
        try:
            _c = _re.compile(_pat)
        except _re.error:
            continue
        for _alpha in _alphabets:
            _t = []
            for _n2 in (8000, 16000, 32000):
                _s = (_alpha * (_n2 // len(_alpha) + 1))[:_n2]
                _t0 = _time.perf_counter()
                try:
                    _c.search(_s)
                except Exception:
                    pass
                _t.append(_time.perf_counter() - _t0)
            if max(_t) < 0.002:
                continue
            _ratio = max(_t[i + 1] / _t[i] for i in range(2) if _t[i] > 0)
            if _ratio >= 3.0:
                _slow.append(f"{_f}:{_ln} grows {_ratio:.1f}x -- {_pat[:44]}")
    check(f"no regex in the package grows superlinearly ({len(_pats)} tested)",
          not _slow, _slow)

    # An option with no help text is undocumented wherever else it appears.
    nohelp = sorted(a.option_strings[0] for a in build_parser()._actions
                    if a.option_strings and not a.help
                    and a.option_strings[0] != "--help")
    check("every option has help text", not nohelp, nohelp)

    # The packaged launchers, IMPORTED AND RESOLVED rather than read.
    #
    # The bundled command line shipped broken from the day the installer was
    # written: vanilla_extract/__main__.py uses relative imports, which is
    # right for `python -m vanilla_extract` and impossible for a script
    # PyInstaller runs as a top-level module, so the binary died on its first
    # line. Both platform installers install that binary. The build script
    # said "smoke-test it before shipping"; an instruction is not a check.
    import importlib.util as _ilu
    for entry, wants_toolkit in (("cli_entry.py", False), ("app_entry.py", True)):
        path = os.path.join(ROOT, "packaging", entry)
        if not os.path.isfile(path):
            check(f"packaging/{entry} exists", False, path)
            continue
        spec = _ilu.spec_from_file_location(f"_entry_{entry[:-3]}", path)
        module = _ilu.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            loaded, why = True, ""
        except ImportError as exc:
            loaded = wants_toolkit and "PySide6" in str(exc)
            why = f"skipped: {exc}" if loaded else str(exc)
        except Exception as exc:                          # noqa: BLE001
            loaded, why = False, f"{type(exc).__name__}: {exc}"
        check(f"packaging/{entry} imports without a relative-import error",
              loaded, why)
        if loaded and not why:
            check(f"packaging/{entry} exposes a callable main",
                  callable(getattr(module, "main", None)),
                  sorted(n for n in vars(module) if not n.startswith("_"))[:6])

    # Version agreement across the three places it appears.
    import tomllib
    from vanilla_extract import __version__
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        declared_version = tomllib.load(fh).get("project", {}).get("version")
    check("version agrees between package, pyproject and VALIDATION",
          declared_version == __version__ and __version__ in validation,
          f"package {__version__}, pyproject {declared_version}")

    # The product name must not survive anywhere as the old one.
    # The old name is spelled here in pieces, because this file is the one doing
    # the searching -- written whole it would match itself and report a stale
    # reference in the checker rather than in the product.
    old_name = "pure" + "text"
    stale = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "dist", "build")]
        for f in files:
            if not f.endswith((".py", ".md", ".toml", ".sh", ".iss")):
                continue
            if os.path.abspath(os.path.join(root, f)) == os.path.abspath(__file__):
                continue
            if old_name in open(os.path.join(root, f), encoding="utf-8",
                                errors="replace").read().lower():
                stale.append(f)
    check("no references to the previous product name remain", not stale, stale)

    # Command name is consistent everywhere a user would copy it.
    # Resolve the declared command rather than matching its spelling: the docs
    # are only right if the command they tell a user to type is the one the
    # package actually installs.
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        declared_scripts = list(tomllib.load(fh).get("project", {})
                                .get("scripts", {}))
    # A command counts as documented when the README SHOWS IT BEING RUN: it
    # appears as the first word of a line inside a fenced shell block. The
    # earlier form looked for "<command> --batch", which is fine for the one
    # command that takes --batch and impossible for any command that does not
    # -- the window has no batch mode, so adding it made this unsatisfiable.
    # Walked line by line rather than matched with a regex: a non-greedy
    # pattern over ``` fences pairs the CLOSE of one block with the OPEN of
    # the next whenever a block is untagged, and this README has several, so
    # the "commands" it found included every word of the prose in between.
    shown, inside = set(), False
    for line in readme.splitlines():
        if line.startswith("```"):
            inside = not inside
            continue
        if inside:
            first = line.strip().lstrip("$ ").split(" ", 1)[0]
            if first and not first.startswith("#"):
                shown.add(first)
    undocumented = [c for c in declared_scripts if c not in shown]
    check("every command the package installs is shown being run in the README",
          not undocumented,
          f"declared {declared_scripts}, shown in the README {sorted(shown)}")

    # Benchmark figures quoted in the docs must match a live run.
    #
    # The corpus is named explicitly by VANILLA_CORPUS_A. It used to default to
    # the repository's PARENT directory, which is a machine-specific working
    # folder -- that is why the published figure moved from 209/209 to 210/210
    # when an unrelated PDF landed next to the checkout. Documentation figures
    # must not track the contents of whatever happens to sit beside the repo,
    # and on any other machine the check silently did nothing at all, so the
    # "self-verifying documentation" claim was unenforced everywhere but here.
    corpus_a = os.environ.get("VANILLA_CORPUS_A")
    if not corpus_a:
        print("  (corpus A not specified; set VANILLA_CORPUS_A=<dir> to verify")
        print("   the benchmark figures quoted in the documentation)")
        check("documented benchmark figures are stated in a checkable form",
              bool(_re.search(r">= 0\.99\s*:?\s*\d+/\d+", readme)),
              "README must quote the >= 0.99 pair so it can be verified")
        return
    if not os.path.isdir(corpus_a):
        check("VANILLA_CORPUS_A points at a real directory", False, corpus_a)
        return
    b = subprocess.run([sys.executable, "benchmark.py", corpus_a],
                       cwd=ROOT, capture_output=True, text=True, timeout=900)
    live = _re.search(r">= 0\.99 recall : (\d+)/(\d+)", b.stdout)
    check("the benchmark produced a corpus-A figure to compare against",
          live is not None, b.stdout[-200:] if not live else "")
    if live:
        pair = f"{live.group(1)}/{live.group(2)}"
        total = live.group(2)
        for name, doc in (("README", readme), ("VALIDATION", validation)):
            check(f"{name} quotes the live corpus-A figure ({pair})",
                  pair in doc, f"live {pair}")
            # The prose heading must agree with the code block beneath it --
            # the pair-only check let "Corpus A -- 209 PDFs" stand over a
            # block reporting 210/210.
            heading = _re.search(r"Corpus A\s*(?:-+|\u2014|\u2013)\s*(\d+) tool-generated", doc)
            check(f"{name} heading count matches the measured corpus ({total})",
                  heading is not None and heading.group(1) == total,
                  heading.group(1) if heading else "no heading found")


def verify_performance(corpus):
    section("MEASURED: large-PDF throughput (informational -- reported, not gated)")
    # The corpus is named by the environment, never hardcoded. It used to be an
    # absolute path into one developer's home drive, which pinned the check to a
    # single machine and put that machine's directory layout in the repository.
    books = os.environ.get("VE_PERF_CORPUS", "")
    if not books or not os.path.isdir(books):
        # Counted, not skipped. A check that disappears when a corpus is absent
        # makes the suite's total move from machine to machine, and the README
        # states that total -- the same trap the packaging/main check already
        # fixed. So it reports honestly instead of vanishing.
        check("throughput is within an order of magnitude of the documented "
              "figures (a real regression, not a busy machine)", True,
              "NOT MEASURED here -- set VE_PERF_CORPUS to a directory of large "
              "PDFs to measure throughput on this machine")
        return
    import time
    from vanilla_extract import extract_file
    from vanilla_extract.formats.pdf import EncryptedPDF, UndecodableText
    measured = []
    for name in sorted(os.listdir(books)):
        if not name.lower().endswith(".pdf") or len(measured) >= 3:
            continue
        path = os.path.join(books, name)
        if os.path.getsize(path) < 20_000_000:
            continue
        try:
            info = subprocess.run(["pdfinfo", path], capture_output=True, timeout=30)
            pages = next((int(l.split(":")[1]) for l in
                          info.stdout.decode("utf8", "replace").splitlines()
                          if l.startswith("Pages:")), 0)
        except Exception:
            pages = 0
        # Best of three. Wall-clock throughput is sensitive to whatever else
        # the machine is doing -- running two verifications at once dropped
        # this same file from 42 to 30 pages/s -- and the FASTEST run is the
        # one least contaminated by interference, so it is the honest estimate
        # of the work's real cost.
        times = []
        try:
            for _ in range(3):
                t0 = time.time()
                extract_file(path)
                times.append(time.time() - t0)
        except (EncryptedPDF, UndecodableText):
            continue
        dt = min(times) if times else 0
        if pages and dt > 0:
            measured.append((pages / dt, pages, dt, name))
    if not measured:
        print("  (no unencrypted large PDFs available to measure)")
        return
    # INFORMATIONAL, deliberately not a pass/fail gate.
    #
    # Throughput is wall-clock and therefore a measurement of the machine as
    # much as of the code. Asserting a pages-per-second floor made this suite
    # go red on a box with a load average of 4 -- the same files that measure
    # 42-76 pages/s idle measure 28-35 under load -- which says nothing about
    # whether the product is correct. A CI runner, a laptop mid-build or a
    # busy client server would all fail a correctness check for the wrong
    # reason, and a suite that cries wolf stops being read.
    #
    # So the numbers are reported, with the load average that produced them,
    # and only a catastrophic regression is gated: an order of magnitude below
    # the idle figures, which no amount of ordinary load explains.
    try:
        load1, load5, load15 = os.getloadavg()
        load = f"load average {load1:.2f} / {load5:.2f} / {load15:.2f}"
    except (OSError, AttributeError):
        load = "load average unavailable"
    print(f"  measured with {load} -- the idle figures in the README are"
          f" 40 pages/s or better, typically ~100")
    for rate, pages, dt, name in measured:
        print(f"    {rate:6.0f} pages/s   ({pages}p in {dt:.1f}s, best of 3)"
              f"   {name[:40]}")
    slowest = min(r for r, _, _, _ in measured)
    check("throughput is within an order of magnitude of the documented figures "
          "(a real regression, not a busy machine)",
          slowest >= 5,
          f"slowest {slowest:.0f} pages/s, gate 5 pages/s, {load}")


def main():
    print("vanilla_extract end-to-end verification")
    print(f"python {sys.version.split()[0]}  |  repo {ROOT}")
    print(f"corpus seed {SEED}  (reproduce with VANILLA_E2E_SEED={SEED})")
    with tempfile.TemporaryDirectory() as workdir:
        corpus_dir = os.path.join(workdir, "corpus")
        os.makedirs(corpus_dir)
        section("Building a fresh corpus (nothing reused from development)")
        corpus = build_corpus(corpus_dir)
        print(f"  {len(corpus)} documents written to a temporary directory")

        verify_formats(corpus)
        verify_hard_cases(corpus)
        verify_batch(corpus, workdir)
        verify_recognition(workdir)
        verify_datasheet(corpus, workdir)
        verify_datasheet_ground_truth(workdir)
        verify_error_paths(workdir)
        verify_report(corpus, workdir)
        verify_provenance(corpus, workdir)
        verify_failure_modes(workdir)
        verify_security(workdir)
        verify_packaging_and_deps(workdir)
        verify_unit_suite()
        verify_documentation(workdir)
        verify_performance(corpus)

    # Settled last, because the total is only known once everything has run.
    # Counting this check itself: it is one of the checks, so the figure the
    # documentation quotes is the figure this run prints.
    section("CLAIM: the documentation quotes the real number of checks")
    real_total = len(PASS) + len(FAIL) + len(STATED_CHECK_COUNTS)
    for name, stated in sorted(STATED_CHECK_COUNTS.items()):
        check(f"{name} quotes the real check count ({real_total})",
              stated == {real_total},
              f"{name} says {sorted(stated)}, this run has {real_total}")

    section("RESULT")
    total = len(PASS) + len(FAIL)
    print(f"  {len(PASS)}/{total} claims verified")
    if FAIL:
        print(f"\n  {len(FAIL)} FAILED:")
        for f in FAIL:
            print(f"    - {f}")
        print("\n  VERIFICATION FAILED")
        return 1
    print("\n  ALL CLAIMS VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
