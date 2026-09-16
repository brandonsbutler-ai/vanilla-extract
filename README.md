# puretext

**Pull plain text out of documents using nothing but the Python standard library.**

No `pip install`. No wheels, no native extensions, no C toolchain. Drop the package in and it
runs — which is the entire point: in a locked-down environment, adding a dependency needs an
approval that takes longer than the job.

```bash
python3 -m puretext contract.pdf
python3 -m puretext --json invoices/*.docx > invoices.jsonl
python3 -m puretext archive.zip          # reads every document inside
```

```python
from puretext import extract_file
text = extract_file("statement.pdf")
```

**A folder of documents into a spreadsheet**, which is the shape this work usually takes:

```bash
python3 -m puretext --batch invoices/ --csv results.csv --exceptions skipped.csv \
    --field "invoice_no=Invoice\s*#?\s*([A-Z0-9-]+)" \
    --field "total=Total\s*:?\s*\$?([0-9,]+\.[0-9]{2})"
```

**Or let it find the fields itself**, which is the point when the client has 400 documents and no
idea what regex to write:

```bash
python3 -m puretext --batch invoices/ --recognize --report review.html --csv results.csv
```

```
puretext: discovered fields --
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

`--report` writes a **single self-contained HTML file**: every extracted value editable in place,
the source text of each document one click away so a suspect value can be checked against the page
it came from, the exceptions table given equal billing, and a button that exports the corrected
table back out as CSV. No server, no external assets, nothing uploaded -- so a client can review a
delivery of their own sensitive documents without it leaving their machine.

One row per document, one column per field, and **a second table naming every file that could not
be read and why**. That second table is the point: encrypted PDFs, scans with no
text layer, and files whose fonts carry no character map all look like empty documents to most
extraction tools and arrive as blank rows nobody notices until the data is already in use. A bad
document never aborts the batch, and it never silently becomes an empty row either.

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
| **Text** | TXT, MD, LOG, with encoding detection |
| **Archives** | ZIP containing any of the above |

Routing is by **content first, extension second**. In any real corpus a meaningful fraction of
files are misnamed — a `.doc` that is really RTF, a `.txt` that is really a PDF — and trusting
the extension is the usual reason a batch job silently produces nothing for part of its input.

## Measured results

Claims about extraction quality are worth nothing without numbers, so `benchmark.py` scores
output against [poppler](https://poppler.freedesktop.org/)'s `pdftotext` — a mature C++
implementation with full font handling — by token recall: the fraction of the reference's words
that puretext also produced, counted as a multiset.

pdftotext is the reference, not the target. It is allowed to win. The point is knowing by how much.

**Corpus A — 209 tool-generated PDFs** (fpdf2, LibreOffice Writer):

```
token recall : mean 1.000   median 1.000   min 1.000
>= 0.99      : 209/209 (100.0%)
```

**Corpus B — 86 commercial PDFs** (Adobe PDF Library, Mac Quartz; illustrated, multi-column):

```
files measured : 53
token recall   : mean 0.873   median 0.912   min 0.007   max 0.961
>= 0.95        : 4/53  (7.5%)
>= 0.90        : 35/53 (66.0%)
>= 0.50        : 51/53 (96.2%)

encrypted, refused with a clear error: 33   (reported separately, not scored)
```

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

**From source** -- nothing to resolve, because there is nothing to resolve:

```bash
pip install .            # gives you a `puretext` command
```

**Linux, without pip:**

```bash
./packaging/install-linux.sh                    # per-user, ~/.local/bin
PREFIX=/usr/local sudo ./packaging/install-linux.sh
```

**A single executable, for machines with no Python at all:**

```bash
pip install pyinstaller
python3 packaging/build_standalone.py           # -> dist/puretext(.exe)
```

**Windows installer:** build the executable as above, then `iscc packaging\puretext.iss`.
It produces a per-user installer that needs no administrator rights and puts `puretext` on PATH --
which matters, because the people who most need a dependency-free extractor are usually the same
people who cannot install Python or pip on their work machine.

PyInstaller and Inno Setup are *build-time* tools. Neither ships inside the application, and the
runtime dependency list stays empty.

## Keeping the original: workspaces

Extraction deliverables get corrected by hand, and once corrected there is no way to tell which
values came out of the document and which came out of a person. A workspace answers that:

```bash
puretext --batch invoices/ --recognize --workspace case01/ --report review.html --csv out.csv
# ... client corrects values in review.html and exports corrected.csv ...
puretext --workspace case01/ --import-csv corrected.csv
puretext --workspace case01/ --verify
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

Roughly 50-70 pages per second. A file is read fully into memory, so peak usage tracks the
document size; that is fine for the hundreds-of-megabytes range and is the limit to know about.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

58 tests, no pytest required. Fixtures are built in code rather than committed as binaries, so
there is nothing opaque in the repo. The suite covers the cases that actually break extractors:
balanced parens inside PDF strings, escaped close-parens, octal escapes, odd hex nibbles,
RTF `\fonttbl` contents leaking into output, cp1252 fallback, and misnamed files -- plus the
batch guarantees: one bad document never aborts the run, an empty document is reported rather
than returned as a blank row, and every requested field column exists on every row.

## License

MIT — see [LICENSE](LICENSE).
