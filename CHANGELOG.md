# Changelog

What changed in each release, in terms of what it means for somebody using it.
The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
version numbers are [semantic](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-09-22

The first release, and the first packaged for PyPI, as `vanilla-extract`.
Everything before it was development in this repository under an earlier
working name, with nothing published and no version anybody could install.

### Added

- Text extraction from PDF, the OOXML formats (.docx, .xlsx, .pptx), RTF,
  e-mail, HTML and XML, and plain text, using nothing outside the Python
  standard library. Nothing is installed alongside it, and nothing has to be
  approved before it can be.
- `vanilla <file>` prints the text; `--json` writes one record per file; a zip
  is read as what it contains, every document inside it and inside any archive
  nested in it.
- Batch mode: a folder in, a results table and an exceptions table out. One
  document that cannot be read never aborts the run, and every input is
  accounted for in one table or the other.
- Field recognition. Name a pattern with `--field name=regex` and each capture
  becomes a column, on every row, whether or not that document had it; or let
  `--recognize` propose the labels that appear across enough of the corpus to
  be worth a column.
- A self-contained HTML review report: each document's text beside the row it
  produced, editable in place, re-exportable as CSV. Corrections survive a
  reload, and the page asks before losing them.
- A file-state datasheet -- size, timestamps, permissions, owner and group,
  links, filesystem -- recording each file as it was found, as CSV or as a
  searchable page.
- A workspace that keeps the originals, the extractions and every correction
  together, each hashed with SHA-256. `--import-csv` files a corrected table as
  a new revision and records what it changed; `--verify` re-hashes everything
  and reports anything that moved since capture.
- A desktop window (`vanilla-gui`, from the `[gui]` extra): drop a folder onto
  it, pick what to do, press Run. It is a native window rather than a local web
  page because a browser hands over copies of the files without their owner,
  permissions or timestamps -- which is most of what the datasheet exists to
  record.
- A single-file build for a machine with no Python on it, for Linux and
  Windows, plus a Windows installer that needs no administrator rights.
- One contract for failures: a reason code for every document that produced no
  text, a `--json` record for those too, and exit 2 for a usage error rather
  than a traceback.
- `VALIDATION.md` for a customer or a reviewer -- what the tool does, how it is
  checked, what the checks measured and what they do not prove -- with a script
  that renders it to a PDF.
- `verify_e2e.py`, which generates a fresh corpus in every supported format,
  drives the real command line as a user would, and asserts each claim in the
  README against what came back. It prints PASS or FAIL per claim and exits
  non-zero on any failure, so it is able to say the product does not work.
- The source distribution carries the tests, that claim checker and
  `VALIDATION.md`, so the claims can be re-run by whoever downloads it rather
  than taken on trust. The README says how.
- `packaging/verify_dist.py` checks a built wheel and source distribution
  rather than the checkout: that each installs into an empty environment, pulls
  nothing in with it, puts a working `vanilla` on PATH, and carries no file
  that should never have left this machine.

### Changed

- The minimum Python is 3.11, not 3.9. The package declared `>=3.9` and the
  README repeated it, but nothing here had ever been run below 3.11, and the
  project's own checkers cannot start there: `verify_e2e.py`,
  `packaging/verify_dist.py` and one unit test import `tomllib`, which arrived
  in 3.11. Raised 2026-09-22 rather than ship a compatibility claim that had
  never been tested and could not be checked.

### Fixed

Defects found before this release, nearly all of them by running the tool over
real documents rather than by reasoning about it:

- A PDF written by LibreOffice returns words rather than control characters.
  Subset fonts renumber the glyphs, so reading the text means resolving the
  PDF's indirect objects, including the ones packed inside compressed object
  streams.
- An encrypted or undecodable PDF says so and fails, instead of returning
  mojibake that looks like a successful extraction.
- A document that comes back almost entirely empty is reported as empty, with
  the reason, instead of being returned as a blank row or a blank result.
- A zip is treated as an archive or as a document by what is inside it, not by
  its name, and a misnamed document is read for what it is.
- Hostile input is bounded rather than trusted: a zip that expands beyond its
  budget is refused, nested archives count toward that budget, and a PDF
  content stream is capped whether it arrives compressed or stored.
- Three patterns that the documentation described as linear were quadratic, as
  was the marker that splits duplicate members. All four are fixed, and growth
  is measured against a limit on every run.
- An output path that cannot be written is refused before a single document is
  read, with exit 2, rather than after the work is done.
- A `--batch` folder that does not exist fails, instead of writing an empty
  table that looks like a corpus with nothing in it.
- Folders that are symlinks, or that cannot be listed, are reported rather than
  skipped in silence.
- Spreadsheet output survives a spreadsheet: every cell stays within what one
  will hold and is marked where it was cut, a negative amount stays a number
  instead of being read as a formula, and a CSV that Excel wrote in
  Windows-1252 is read back correctly.
- The README's own `--field` example failed when pasted into a shell, because
  double quotes let bash rewrite `\$` before the pattern arrived. The examples
  are now run through bash on every verification run, exactly as written.
- The documentation's figures are checked against what the tools produce, after
  a stale count sat in three places while a different number was the truth.

Three more, found on 2026-09-22 by somebody outside the project who installed
the built wheel into a clean environment and followed the README:

- **A PDF made by TeX comes back with its words apart.** pdfTeX, XeTeX and
  LuaTeX draw no space character at all -- a word break is a positioning
  number inside the text-showing operator -- and those numbers were being
  discarded. A stock pdfTeX document extracted to 31,988 characters with zero
  spaces in them, exit 0, no warning: 0.179 token recall against `pdftotext`,
  now 0.999. Word breaks are read from the displacement, and a kern inside a
  word still joins. Re-scored over a corpus of 402 real PDFs before and after,
  no other file's output changed by a single character.
- **`--batch` without `--csv` prints a summary, as `--help` always said it
  would.** It printed one JSON object per document instead, full text
  included -- megabytes into a terminal for a folder of 400 documents. `--csv`
  writes the data, `--json` asks for the machine-readable form.
- **Re-importing an unmodified table files nothing.** The workspace recorded
  the delivered CSV's own text columns as corrections, so a byte-for-byte
  round trip of `--csv` output looked like two edits per document and a real
  correction was buried among them. The revision and the delivered file are
  now written by the same code from the same columns.
