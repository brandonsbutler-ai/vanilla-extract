# vanilla-extract

**Pull plain text out of documents using nothing but the Python standard library.**

No `pip install`. No wheels, no native extensions, no C toolchain. Drop the package in and it
runs — which is the entire point: in a locked-down environment, adding a dependency needs an
approval that takes longer than the job.

The examples use the `vanilla` command that an install provides. In a drop-in copy with nothing
installed, the same thing is `python3 -m vanilla_extract`, run from the folder that holds the
`vanilla_extract` package -- see [Install it](#install-it).

```bash
vanilla contract.pdf
vanilla --json invoices/*.docx > invoices.jsonl
vanilla archive.zip          # reads every document inside
```

```python
from vanilla_extract import extract_file
text = extract_file("statement.pdf")
```

**A folder of documents into a spreadsheet**, which is the shape this work usually takes:

```bash
vanilla --batch invoices/ --csv results.csv --exceptions skipped.csv \
    --field 'invoice_no=Invoice\s*(?:Number|No\.?|#)\s*:?\s*([A-Z]+-[0-9]+)' \
    --field 'total=Total\s*:?\s*\$?([0-9,]+\.[0-9]{2})'
```

Patterns match **case-insensitively** (`re.IGNORECASE`), so `[A-Z]` matches lowercase letters too
and a bare `Invoice\s*#?\s*([A-Z0-9-]+)` happily captures the word "Invoice" from the line under an
`INVOICE` heading. Anchor a pattern on the words around the value, as above, and use `(?-i:...)`
for a part that must match case exactly.

Quote each `--field` in **single** quotes. Inside double quotes bash rewrites `\$` to `$` before the
pattern arrives, and the regex no longer means what was typed. A pattern that will not compile is
refused with the field's name and the pattern exactly as received, and exit status 2.

**No CSV cell is longer than 32,767 characters**, which is as much as Excel holds in one cell. A
document's full text can run to millions of characters, and a cell that long is cut off by Excel
and refused by Python's own `csv` reader at its default settings. A longer value is cut, the cell
ends with `[... truncated: N characters in full]`, and a `text_truncated` column (True/False) follows
`text` so no cut is silent. The `characters` column always counts the full text; the full text
itself is in the document, and with `--workspace` in `extracted/`. `--no-text` leaves the column out.

**Or let it find the fields itself**, which is the point when the client has 400 documents and no
idea what regex to write:

```bash
vanilla --batch invoices/ --recognize --report review.html --csv results.csv
```

```
vanilla: discovered fields --
    invoice number           4 docs (100%)  identifier e.g. INV-1001
    invoice date             4 docs (100%)  date_iso   e.g. 2026-03-14
    customer                 4 docs (100%)  text       e.g. Northwind Traders
    terms                    4 docs (100%)  text       e.g. Net 30
    total                    4 docs (100%)  money      e.g. $1,299.00
    contact                  4 docs (100%)  email      e.g. ap@northwind.example
```

`--recognize` reads label/value pairs out of each document -- colon-separated, column-separated,
and label-on-its-own-line, since a flattened PDF renders forms all three ways -- then ranks labels
by how many documents carry them. A label in 380 of 400 files is a column; one in 3 is noise.
Each column is typed by a conservative detector (money, dates, email, phone, percent, identifier),
because a pattern that fires on the wrong thing costs more than one that stays quiet.

Label-on-its-own-line is the ambiguous form: `Customer` above `Contoso Ltd` is two short
capitalised lines, and one document cannot say which is the label. The corpus can. A label that
other documents state plainly (`Customer: Fabrikam Inc`) is read above a value that looks like a
label; a line that merely recurs, as headings do, is not treated as one. On the synthetic invoice
corpus in the validation run, that took recall on stacked layouts from 0.58 to 1.00 per field
(0.795 to 1.000 overall) with precision unchanged at 1.000, and it proposed no new columns on
three real deliveries of technical documents.

Two kinds of line are never proposed as columns. A label whose value is the same in nine of ten
documents carrying it (once at least five do) is boilerplate -- a page footer such as
`Acme Corp  |  Confidential`, a template placeholder -- because a column that says the same
thing on every row tells the reader nothing; the cost is that a genuinely constant field, such as
`Terms: Net 30` across one vendor's invoices, is not proposed either, and `--field` still reads
it. And HTTP method names (`GET`, `POST`, ...), which an API reference sets on a line of their own
above a path, are not field labels.

`--report` writes a **single self-contained HTML file**: every extracted value editable in place,
the source text of each document one click away so a suspect value can be checked against the page
it came from, the exceptions table given equal billing, and a button that exports the corrected
table back out as CSV. No server, no external assets, nothing uploaded -- so a client can review a
delivery of their own sensitive documents without it leaving their machine.

The source view is a preview: up to 100,000 characters of each document, about forty pages, which
is enough to check a value against its page. A longer text is cut and the view says how many
characters it left out and where the full text is. Embedding everything once turned one very large
JSON file into a 155 MB report that took over ten seconds to open; the same delivery now makes a
4 MB report that opens in under a second.

**Export is the save.** A page opened from disk cannot write itself, so corrections are not stored
in the HTML file. They live in the browser until exported: where the browser allows it, a copy is
kept in its local storage, keyed to that one report so another report never inherits it, and
reloading or closing the tab with unexported corrections asks first.

One row per document, one column per field, and **a second table naming every file that could not
be read and why**. That second table is the point: encrypted PDFs, scans with no
text layer, and files whose fonts carry no character map all look like empty documents to most
extraction tools and arrive as blank rows nobody notices until the data is already in use. A bad
document never aborts the batch, and it never silently becomes an empty row either.

**Every input is accounted for**: rows + exceptions = inputs. A file named like an image or a binary
is checked by its bytes first, so a PDF renamed `.jpg` is read; a real image is listed as
`image_no_text_layer`, other media and binaries as `not_a_document`. Tooling directories (`.git`,
`__pycache__`, virtualenvs, tool caches) are not walked, but each is listed as
`excluded_directory` with the number of files it holds; any other hidden directory is listed as
`hidden_directory` -- name it on the command line to read it. A zip inside a zip is opened, up to
eight levels deep; past that it is listed as `limit_exceeded`.

## Formats

| | |
|---|---|
| **PDF** | content-stream parsing, Flate decompression, `/ToUnicode` CMap decoding |
| **Office** | DOCX, PPTX, XLSX, ODT |
| **Email** | EML, MBOX — headers, body, attachment names |
| **Markup** | HTML (scripts and styles dropped), XML |
| **Data** | CSV, TSV (delimiter sniffed), JSON (flattened to `path: value`) |
| **Batch** | folder or archive in; results table, exceptions table, HTML review report out |
| **Recognition** | label/value pairs, typed values, and schema inferred across a corpus |
| **Datasheet** | size, timestamps, rwx permissions, owner, links, filesystem -- searchable |
| **Text** | TXT, MD, LOG, with encoding detection |
| **Archives** | ZIP containing any of the above, including ZIPs nested inside ZIPs |

Routing is by **signature first, extension second, and the extension is not allowed to
contradict a missing signature.** In any real corpus a meaningful fraction of files are misnamed
— a `.doc` that is really RTF, a `.txt` that is really a PDF — and trusting the extension is the
usual reason a batch job silently produces nothing for part of its input.

Precisely, in this order: magic bytes; then the extension, unless it names a format that always
begins with a signature (PDF, RTF, the OOXML family) and that signature is absent; then, as a
last resort, plain text if the bytes contain no NUL. That third clause matters. A text export
saved as `report.pdf` used to reach the PDF reader and come back `no_text_found` — a reason this
README describes as almost always a scan with no text layer, so it sent the reader looking for
OCR for a file whose text was sitting in plain bytes.

## Measured results

Claims about extraction quality are worth nothing without numbers, so `benchmark.py` scores
output against [poppler](https://poppler.freedesktop.org/)'s `pdftotext` — a mature C++
implementation with full font handling — by token recall: the fraction of the reference's words
that vanilla_extract also produced, counted as a multiset.

pdftotext is the reference, not the target. It is allowed to win. The point is knowing by how much.

**Corpus A — 210 tool-generated PDFs** (fpdf2, LibreOffice Writer):

```
token recall : mean 1.000   median 1.000   min 1.000
>= 0.99      : 210/210 (100.0%)
```

**Corpus B — 86 commercial PDFs** (Adobe PDF Library, Mac Quartz; illustrated, multi-column):

```
files measured : 52
token recall   : mean 0.890   median 0.913   min 0.409   max 0.961
>= 0.99        : 0/52  (0.0%)
>= 0.95        : 4/52  (7.7%)
>= 0.90        : 35/52 (67.3%)
>= 0.50        : 51/52 (98.1%)

encrypted, refused with a clear error          : 33   (not scored)
unmapped CID fonts, refused with a clear error : 1    (not scored)
```

Thirty-four of the 86 are not scored because the tool refuses them rather than guessing, and a
refusal is not a recall figure. The single unmapped-font refusal used to be scored, at 0.007 --
it came back as 2,549 characters of mojibake out of 401,034, with exit code 0. That is the blank
row this tool exists to prevent, and it took an audit of the benchmark's own worst entry to find
it. Excluding it is why the mean moved from 0.873 to 0.890; extraction did not improve.

Read that honestly: **excellent on PDFs produced by tools, good-but-not-perfect on complex
commercial ones, and never quite matching poppler on the hard ones.** If you need the last few
percent on illustrated multi-column layouts, use poppler. If you need no dependencies, use this.

Reproduce with `python3 benchmark.py /path/to/pdfs`.

## It fails loudly

The worst behavior a text extractor can have is returning something that *looks* like a result.
An empty string reads as an empty document; mojibake poisons a dataset silently. Both get a
specific exception instead:

- **`EncryptedPDF`** — the file uses the standard security handler. Commercial PDFs are routinely
  encrypted with an *empty user password* purely to set permission flags: readable in any viewer,
  still encrypted on disk. 33 of the 86 files in corpus B are like this. Decrypting needs RC4 and
  AES, and AES is not in the standard library, so it is out of scope by design.
- **`UndecodableText`** — text was drawn, but every run decoded to glyph IDs rather than
  characters. Raised only when *nothing* survives; a page mixing an unmapped decorative heading
  with readable body text keeps the body.
- **`UnsupportedFormat`** — neither content nor extension identifies a handler.
- **`NoTextFound`** — the document was read and nothing came out. `extract_file()` still returns
  `""` for such a file by default, because an empty `.txt` is a legitimate answer; call
  `extract_file(path, require_text=True)` to get this exception instead, with `.reason` set to
  `empty_file`, `truncated_or_corrupt`, `limit_exceeded` or `no_text_found` and `.detail` saying
  which in words. `explain_empty(data, filename)` answers the same question for bytes in hand.

On the command line, `vanilla file.pdf` and `vanilla --json file.pdf` exit **1** when a file -- or
any member of an archive -- produced no text, with the reason on stderr (and as `reason` and
`detail` fields in the JSON record); **2** for a usage error, such as a `--field` pattern that
will not compile; **0** otherwise. `--batch` is different on purpose: an unreadable document is a
row in the exceptions table, a reported result rather than a failed run, and it exits 0.

## Known limits

- **No font-program parsing.** CID-keyed fonts using Identity-H with no `/ToUnicode` map cannot be
  decoded — recovering characters means reading the CFF/TrueType cmap inside the embedded font.
  Affected runs are dropped rather than emitted as noise. One file in corpus B (Mac Quartz,
  Identity-H throughout) returns almost nothing for this reason and scores 0.007.
- **No OCR.** A scanned page holds an image, not text.
- **Layout is approximated, not reconstructed.** Multi-column pages interleave. This is the main
  source of the gap against `pdftotext -layout` on corpus B.
- **Fonts are keyed by resource name across the document**, not per page-resource dictionary. A
  file reusing `/F1` for two different fonts on different pages can decode one of them wrong.
  Handling it properly means walking the page tree; the benchmark is how I know the trade is
  acceptable for tool-generated files.
- **Encryption is detected, never bypassed.** This library will not help you read a document you
  do not have the password for.

## How the PDF reader works

A PDF is a graph of objects; visible text lives in content streams, usually Flate-compressed,
written as PostScript-ish operators. Three steps: find the streams, decompress them, and pull
the arguments of the text-showing operators (`Tj`, `TJ`, `'`, `"`).

Two pieces are where a naive implementation goes wrong:

**The string reader is a tokenizer, not a regex.** A PDF literal string can contain balanced
parentheses and backslash escapes — `(a (b) c)` is one string, and `(a\)b)` is one string
containing a close paren. A regex gets both wrong, which is the usual reason a hand-rolled PDF
reader returns truncated text.

**`/ToUnicode` CMaps are parsed.** Word and LibreOffice embed a *subset* of each font and
renumber the glyphs 1, 2, 3… in order of first appearance. The content stream then draws
`\x01\x02\x03`, which is meaningless without the font's CMap. Reading it requires indexing the
PDF's indirect objects — including the ones packed inside compressed object streams (`/ObjStm`),
which is where PDF 1.5+ puts them — and resolving `12 0 R` references. Without this, every
LibreOffice PDF returns control characters instead of words.

## Install it

vanilla-extract is not published on PyPI. Every route below starts from a clone of
<https://github.com/brandonsbutler-ai/vanilla-extract> or pip's git URL form.

**Nothing installed at all** -- the air-gapped route. Fetch the code where there is a network,
carry the folder across, and run the package where it sits. Python 3.9 or later is the only
requirement; there is nothing to resolve because there is nothing to resolve:

```bash
git clone https://github.com/brandonsbutler-ai/vanilla-extract
# carry the vanilla-extract folder across; then, from inside it:
python3 -m vanilla_extract contract.pdf
python3 -m vanilla_extract --batch invoices/ --csv results.csv --report review.html
```

`python3 -m vanilla_extract` takes every option `vanilla` does. It is the form to use wherever this
README says `vanilla` and nothing has been installed.

**With pip, for a `vanilla` command on PATH:**

```bash
pip install .                    # from inside a clone
pip install "vanilla-extract @ git+https://github.com/brandonsbutler-ai/vanilla-extract"
```

The package has no runtime dependencies, but pip still has to *build* it, with setuptools 61 or
later, and by default it downloads setuptools to do so. On a machine with no network that
download fails. Either use the route above, or install into an environment that already has
setuptools and tell pip not to fetch it: `pip install --no-build-isolation .`

**Linux, without pip:**

```bash
./packaging/install-linux.sh                    # per-user, ~/.local/bin
PREFIX=/usr/local sudo ./packaging/install-linux.sh
```

**A single executable, for machines with no Python at all:**

```bash
pip install pyinstaller
python3 packaging/build_standalone.py           # -> dist/vanilla(.exe)
```

**Windows installer:** build the executable as above, then `iscc packaging\vanilla-extract.iss`.
It produces a per-user installer that needs no administrator rights and puts `vanilla` on PATH --
which matters, because the people who most need a dependency-free extractor are usually the same
people who cannot install Python or pip on their work machine.

PyInstaller and Inno Setup are *build-time* tools. Neither ships inside the application, and the
runtime dependency list stays empty.

## The datasheet: file state as found

A content hash proves the bytes did not change. The datasheet records the circumstances they
arrived in.

```bash
vanilla --batch invoices/ --datasheet state.csv --csv out.csv     # CSV
vanilla --batch invoices/ --datasheet state.html --csv out.csv    # searchable page
```

Per file: size (human and in bytes), modified, accessed, created, inode-change time, rwx
permissions, octal mode, setuid/setgid/sticky, owner and group by name and id, symlink status and
target, hard-link count, inode, filesystem type, and -- for members found inside a ZIP -- the
entry's own timestamp, mode, compressed size, ratio and CRC32. Plus the extraction outcome, so a
file that could not be read still appears with its state and the reason.

The `.html` form is searchable and every column sorts, numerically where that is the right order
(sorting `size` lexically puts "9 KB" after "10 MB").

**Three things this gets right that a naive `os.stat` dump does not:**

- **Creation time is not universally available.** Windows records it in `st_ctime`; macOS and the
  BSDs expose `st_birthtime`; on Linux `os.stat` has neither, and `st_ctime` there is the
  *inode-change* time -- a different fact, usually later. So creation is reported only when the
  platform really has it, Linux gets a best-effort `statx` read, every row states **where the
  value came from**, and inode-change time is reported under its own name rather than relabelled.
- **Mounted foreign filesystems synthesize ownership and mode.** An NTFS or exFAT volume mounted
  on Linux typically reports one uid and `0777` for every file, because those come from the mount
  options. Such rows are marked `ownership_reliable = False` and the HTML datasheet explains why,
  rather than presenting a mount default as the file's permissions.
- **Archive members carry their own metadata**, which is not the archive's -- a member can predate
  the containing ZIP by years. Those values are read from the central directory.

With `--workspace`, each document's full state is also written into `manifest.json` as
`state_when_found`.

## Keeping the original: workspaces

Extraction deliverables get corrected by hand, and once corrected there is no way to tell which
values came out of the document and which came out of a person. A workspace answers that:

```bash
vanilla --batch invoices/ --recognize --workspace case01/ --report review.html --csv out.csv
# ... client corrects values in review.html and exports corrected.csv ...
vanilla --workspace case01/ --import-csv corrected.csv
vanilla --workspace case01/ --verify
```

```
case01/
  manifest.json      run metadata and a SHA-256 for every artefact
  originals/         the source documents as received
  extracted/         what the tool read, per document, immutable
  revisions/         each corrected table, appended, never replaced
```

Filing a correction prints exactly what moved:

```
revision 2: 4 rows, 2 cell(s) changed from the previous revision
    INV-1001.txt: customer: 'Northwind Traders' -> 'Northwind Traders LLC'
    INV-1002.txt: total: '$433.00' -> '$433.50'
```

`--import-csv` reads UTF-8 (with or without a BOM) and, failing that, Windows-1252 -- what Excel's
plain "CSV" save writes -- and says so when it falls back. A file that is missing or unreadable is
an error with exit status 2, not a traceback.

`--verify` re-hashes every artefact and exits non-zero if anything changed since capture.

**What the hashes do and do not prove:** SHA-256 establishes integrity and detects drift. It is
not a signature, and a workspace is not tamper-proof against someone who can write to it. Said
plainly so nobody assumes more of it than it does.

## Performance

Measured on the commercial corpus, single-threaded, no tuning:

| Size | Pages | Time | Extracted |
|---|---|---|---|
| 68 MB | 321 | 6.1 s | 1,296,816 chars |
| 42 MB | 338 | 4.4 s | 1,444,783 chars |
| 31 MB | 261 | 2.9 s | 1,205,266 chars |
| 83 MB | 578 | 0.03 s | encrypted -- refused immediately |

Throughput over 14 unencrypted PDFs of 5 MB and up: **median 101 pages per second**, fastest 179.
The slowest file measured is an image-heavy adventure module at **42 pages/s** over four runs.

So the honest figure is **40 pages per second or better, typically around 100** -- **measured on
an otherwise idle machine**, which is the caveat that matters. Throughput is wall-clock, so it
measures the machine as much as the code: the same files that run at 42-76 pages/s idle drop to
28-35 at a load average of 4.

`verify_e2e.py` therefore **reports** throughput with the load average that produced it rather
than asserting a floor. An earlier version did assert one, and the suite went red on a busy box --
which says nothing about whether the product is correct, and a verification suite that cries wolf
stops being read. Only a catastrophic regression is gated, at an order of magnitude below the idle
figures, where no amount of ordinary load explains it. An earlier draft of this file claimed "50-70", which managed to both
understate the typical case and overstate the floor -- the number now follows the measurement.

A file is read fully into memory, so peak usage tracks document size; that is fine into the
hundreds of megabytes and is the limit to know about. One PDF content stream is inflated with a
64 MB ceiling, and peak memory runs to roughly twice that during processing.

## Hostile input

The whole job is reading files supplied by someone else, so the input is untrusted by definition.
These were tested, not assumed:

| Attack | Result |
|---|---|
| **ZIP decompression bomb** | **Refused.** A 199 KB `.docx` declaring a 200 MB member expanded fully before this was fixed. Members are now vetted against their *declared* size and compression ratio before a byte is read, with a running budget for the archive as a whole. Refusal costs 0.000 s because nothing is allocated. |
| **XXE (external entity)** | Not exploitable -- `xml.etree` does not resolve external entities. Verified against `file:///etc/passwd`. |
| **Billion laughs** | Bounded. expat stops expanding a few levels in, capping the damage near 300 KB of text. Amplification, not denial of service. A document whose XML trips the limit is reported as `limit_exceeded`, not as a scan. |
| **Catastrophic backtracking** | **Three patterns were quadratic; all three are fixed.** A run of uppercase hyphenated text with no digit took 8.8 s at 64 KB, a run of email-legal characters with no `@` took 1.2 s, and a run of spaces with no newline took 1.9 s inside the RTF cleanup. Each walked the whole run, failed, and restarted one character later. The earlier claim that all of them were linear was tested against inputs that contained a digit, an `@` and a newline -- so every pattern completed and nothing backtracked. Every regex in the package is now measured by doubling the input and comparing the times; the check fails above 3x per doubling, and no exponential case has been found. |
| **CSV formula injection** | **Neutralized.** Extracted text went verbatim into a CSV the client opens in Excel, so a document containing `=cmd\|' /C calc'!A0` was a path to code execution on the reviewer's machine. Cells beginning `= + - @` tab or CR are now prefixed, in the Python writer and in the report's in-browser export -- except a cell that is, in full, a plain negative number, amount or percentage (`-$251.00`, `-4.5%`), which stays a number the spreadsheet can sum. `-2+3+cmd\|' /C calc'!A0` is still prefixed. |
| **XSS via the report** | **Fixed.** `json.dumps` does not escape `</script>`, so a document containing it closed the report's data block and the rest parsed as markup -- executing attacker script in a browser holding the client's entire delivery. `<`, `>`, `&`, U+2028 and U+2029 are now escaped in the embedded JSON. |
| **PDF decompression bomb** | **Refused.** A 597 KB PDF expanded one Flate stream to 616 MB. Inflation is now bounded; peak stays near 128 MB. A stream that reaches the 64 MB bound raises `StreamTooLarge` and the document is reported as `limit_exceeded`. It used to be truncated instead -- the first 64 MB kept, exit 0 -- and a 1 MB bomb took 12.5 s, spent tokenizing 64 MB of inflated padding; refused, it takes 0.3 s. |
| **Path traversal** | A source label containing `../` or an absolute path cannot escape a workspace; archived names are flattened to a basename plus a digest. |

Limits are generous on purpose -- a real 300-page report is large and legitimate. The point is to
refuse the absurd, not the merely big: a 20,000-paragraph document at a 29:1 ratio still extracts
in 0.02 s.

## For a customer or a reviewer

[**VALIDATION.md**](VALIDATION.md) is the single document to read: what the tool does, how it is
checked, what the checks measured, the defects they found, and a section on what the numbers do
**not** prove. `python3 generate_validation_pdf.py` renders it to a PDF for anyone who would
rather have one.

## The desktop application

**Self-contained: one file, nothing to install.** No Python, no pip, no Qt on
the machine it runs on: copy the file across, double-click it, drop a folder on
the window.

**There is no prebuilt download yet** -- no release carries the application --
so it is built from a clone, on the platform it is for (PyInstaller does not
cross-compile), and the one file it produces is what gets copied across:

```bash
# to BUILD it (on the platform you are shipping to)
python3 -m pip install pyinstaller "PySide6-Essentials"
python3 packaging/build_standalone.py

#   dist/Vanilla Extract            the application  (~57 MB, one file)
#   dist/vanilla                    the command line (~8 MB, one file)
#   dist/vanilla-extract.desktop    Linux menu entry
```

On Linux, copy the `.desktop` file into `~/.local/share/applications/` and it
appears in the launcher and accepts a folder dropped onto its icon. Without it
the build is a large file in a folder that a file manager offers to open in a
text editor.

If you would rather run it from a Python you already have:

```bash
pip install ".[gui]"             # from inside a clone
pip install "vanilla-extract[gui] @ git+https://github.com/brandonsbutler-ai/vanilla-extract"
vanilla-gui
```

Or, with PySide6 already installed and nothing else, `python3 -m vanilla_extract.gui` from inside
a clone.

Drop a folder onto the window — or several, one at a time; each drop adds to the
job and the summary shows what came from where. Pick what to do with it, press
Run, and the review report opens in your browser.

**The window is the only part of this project with a dependency.** Installed
without `[gui]`, the package is a library and a command line that import
nothing outside the standard library, and that is checked on every run of the
verifier. `[gui]` adds PySide6 for the window, and nothing else uses it.

It is a native window rather than a local web page for one reason: a drop has
to carry the real path. A browser hands over copies of the files, without the
owner, permissions, inode or creation time — which is most of what the
file-state datasheet exists to record, so a browser-based drop would quietly
make that feature untrue.

## Options

Every option the command accepts. `--help` prints the same list.

| Option | What it does |
|---|---|
| `--json` | emit one JSON object per file instead of text |
| `--quiet, -q` | omit the ===== filename ===== banners |
| `--version` | show program's version number and exit |
| `--batch` | walk the given paths and emit a table instead of text |
| `--csv PATH` | with --batch: write results here (default: stdout summary) |
| `--exceptions PATH` | with --batch: write the unreadable-files table here |
| `--field NAME=REGEX` | with --batch: pull a named value out of each document (repeatable). The first capture group wins if present. |
| `--no-text` | with --batch: omit the full text column |
| `--recognize` | with --batch: discover the fields from the documents themselves instead of being given regexes |
| `--min-support F` | with --recognize: fraction of documents a label must appear in to become a column (default 0.5) |
| `--workspace DIR` | keep originals, extractions and every correction together in DIR, each hashed (SHA-256) |
| `--no-copy-originals` | with --workspace: hash the originals but do not copy them (for corpora too large to duplicate) |
| `--import-csv PATH` | with --workspace: file a corrected table as a new revision, recording what it changed |
| `--verify` | with --workspace: re-hash every artefact and report anything that changed since capture |
| `--datasheet PATH` | with --batch: write a searchable table of each file's state as found -- size, timestamps, permissions, owner, links, filesystem (CSV, or .html for a searchable page) |
| `--report PATH` | with --batch: write a self-contained HTML report with per-document preview, in-place editing and CSV re- export |

## Verification

Two layers, both runnable:

```bash
python3 -m unittest discover -s tests -v     # 160 unit tests
python3 verify_e2e.py                        # 188 end-to-end claim checks
```

`verify_e2e.py` exists because unit tests check units, not promises. It generates a fresh corpus
in every advertised format -- none of it reused from development -- drives the real CLI as a user
would, and asserts each claim in this README against actual output. It prints PASS or FAIL per
claim with the evidence, and exits non-zero on any failure, so it is able to say the product does
not work. That is the only reason it is worth running.

It has already done so twice. It caught a stated throughput of "50-70 pages/second" that one
image-heavy file missed at 42, and it caught an over-strict check of its own. The performance
figures here are whatever it last measured, not what would read best.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

160 tests, no pytest required. Fixtures are built in code rather than committed as binaries, so
there is nothing opaque in the repo. The suite covers the cases that actually break extractors:
balanced parens inside PDF strings, escaped close-parens, octal escapes, odd hex nibbles,
RTF `\fonttbl` contents leaking into output, cp1252 fallback, and misnamed files -- plus the
batch guarantees: one bad document never aborts the run, an empty document is reported rather
than returned as a blank row, and every requested field column exists on every row.

The review page's behaviour on reload, close and export is checked in a real headless Chromium
through Playwright, a development tool only. Where Playwright is not installed those tests report
as skipped rather than disappearing, so the count above is the same on every machine.

## License

MIT — see [LICENSE](LICENSE).
