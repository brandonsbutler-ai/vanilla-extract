r"""CLI: `vanilla <path>` once installed, or `python3 -m vanilla_extract <path>`
from a source checkout. Both forms are shown below as the module form, which
works either way.

    python3 -m vanilla_extract report.pdf
    python3 -m vanilla_extract --json *.docx
    python3 -m vanilla_extract archive.zip

Batch a folder into a spreadsheet, with every unreadable file accounted for:

    python3 -m vanilla_extract --batch invoices/ --csv out.csv --exceptions skipped.csv
    python3 -m vanilla_extract --batch invoices/ --csv out.csv \
        --field 'invoice_no=Invoice\s*(?:Number|No\.?|#)\s*:?\s*([A-Z]+-[0-9]+)' \
        --field 'total=Total\s*:?\s*\$?([0-9,]+\.[0-9]{2})'

Exit status: 2 for a usage error (a bad option or --field, a path that does
not exist, a folder without --batch); 1 if any document -- or any member of an
archive -- could not be read or yielded no text, with the reason on stderr (and
as "reason"/"detail" in a --json record); 0 otherwise. So it composes in a
shell pipeline. --batch is the exception: its unreadable documents are a
reported result, and it exits 0.
"""

import argparse
import json
import os
import re
import sys
import zipfile

from . import extract_archive, extract_file, __version__
from .dispatch import reason_of
from .batch import Field, run as run_batch, write_csv
from .dispatch import zip_holds_document
from .fileinfo import DATASHEET_COLUMNS
from .recognize import infer_schema
from .report import write_report
from .provenance import Workspace, read_csv_rows


def _emit_text(label, text, show_headers):
    if show_headers:
        print(f"===== {label} =====")
    print(text)


def _run_workspace_op(args):
    """--verify / --import-csv against an existing workspace."""
    ws = Workspace(args.workspace)
    if not ws.exists():
        print(f"vanilla: no workspace at {args.workspace}", file=sys.stderr)
        return 2

    if args.verify:
        problems = ws.verify()
        manifest = ws.load()
        print(f"{len(manifest['documents'])} documents, "
              f"{len(manifest['revisions'])} revisions")
        if problems:
            print("INTEGRITY FAILURES:", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            return 1
        print("all artefacts match their recorded hashes")

    if args.import_csv:
        try:
            rows, columns, encoding = read_csv_rows(args.import_csv)
        except FileNotFoundError:
            print(f"vanilla: {args.import_csv}: no such file", file=sys.stderr)
            return 2
        except (OSError, ValueError) as exc:
            print(f"vanilla: cannot read {args.import_csv}: {exc}", file=sys.stderr)
            return 2
        if encoding != "utf-8-sig":
            print(f"vanilla: {args.import_csv} is not UTF-8; read it as "
                  f"{encoding} (Windows-1252, Excel's plain CSV)", file=sys.stderr)
        entry = ws.add_revision(rows, columns,
                                note=f"imported from {os.path.basename(args.import_csv)}",
                                skip_if_unchanged=True)
        if entry is None:
            print(f"no cell changed from revision {len(ws.load()['revisions'])}; "
                  f"nothing filed")
            return 0
        print(f"revision {entry['revision']}: {entry['rows']} rows, "
              f"{entry['change_count']} cell(s) changed from the previous revision")
        for change in entry["changes_from_previous"][:20]:
            print(f"    {os.path.basename(str(change['file']))}: {change['column']}: "
                  f"{change['from']!r} -> {change['to']!r}")
        if entry["change_count"] > 20:
            print(f"    ... and {entry['change_count'] - 20} more")
    return 0


def _run_batch(args):
    """--batch: a folder in, a results table and an exceptions table out."""
    try:
        fields = [Field.parse(spec) for spec in args.field]
    except (ValueError, re.error) as exc:
        print(f"vanilla: {exc}", file=sys.stderr)
        return 2
    # A mistyped folder is a mistake in the command, not an unreadable
    # document: it used to produce an empty spreadsheet and exit 0.
    missing = [p for p in args.paths if not os.path.exists(p)]
    if missing:
        for p in missing:
            print(f"vanilla: {p}: no such file or folder", file=sys.stderr)
        return 2

    ws = None
    if args.workspace:
        ws = Workspace(args.workspace)
        if not ws.exists():
            ws.create(__version__, args.paths)

    auto_labels = []
    if args.recognize:
        # First pass: read the corpus and let the common structure emerge.
        seen, _ = run_batch(args.paths, include_text=True)
        schema = infer_schema((r["text"] for r in seen),
                              min_support=args.min_support)
        auto_labels = [f["label"] for f in schema]
        if schema:
            print("vanilla: discovered fields --", file=sys.stderr)
            for f in schema:
                print(f"    {f['label']:<24} {f['support']} docs "
                      f"({f['ratio']:.0%})  {f['kind']:<10} e.g. {f['example'][:40]}",
                      file=sys.stderr)
        else:
            print("vanilla: no field common to enough documents; "
                  "try --min-support 0.3", file=sys.stderr)

    keep_text = (not args.no_text) or bool(args.report)
    want_meta = bool(args.datasheet) or ws is not None
    batch_out = run_batch(args.paths, fields=fields,
                          include_text=keep_text,
                          auto_labels=auto_labels,
                          workspace=ws,
                          copy_originals=not args.no_copy_originals,
                          collect_metadata=want_meta)
    if want_meta:
        results, exceptions, datasheet = batch_out
    else:
        results, exceptions = batch_out
        datasheet = []

    if args.datasheet:
        present = [c for c in DATASHEET_COLUMNS
                   if any(c in row for row in datasheet)]
        if args.datasheet.lower().endswith((".html", ".htm")):
            from .report import write_datasheet
            write_datasheet(datasheet, args.datasheet, columns=present)
        else:
            write_csv(datasheet, args.datasheet, present)
        print(f"datasheet -> {args.datasheet}  ({len(datasheet)} files)",
              file=sys.stderr)
        unreliable = sum(1 for r in datasheet
                         if r.get("ownership_reliable") is False)
        if unreliable:
            print(f"vanilla: {unreliable} file(s) sit on a filesystem that reports "
                  f"ownership and permissions from mount options rather than from "
                  f"the files; those columns are flagged not reliable",
                  file=sys.stderr)

    columns = ["file", "characters"] + auto_labels + [f.name for f in fields]
    if args.report:
        write_report(results, exceptions, args.report, columns=columns)
        print(f"report -> {args.report}", file=sys.stderr)

    if args.csv:
        csv_columns = list(columns)
        if not args.no_text:
            csv_columns.append("text")
        write_csv(results, args.csv, csv_columns)
        print(f"{len(results)} documents -> {args.csv}", file=sys.stderr)
    else:
        for row in results:
            print(json.dumps(row))

    if args.exceptions:
        write_csv(exceptions, args.exceptions, ["file", "reason", "detail"])
        print(f"{len(exceptions)} skipped -> {args.exceptions}", file=sys.stderr)
    elif exceptions:
        # Never let these vanish just because no path was given.
        print(f"vanilla: {len(exceptions)} file(s) could not be read:",
              file=sys.stderr)
        for row in exceptions:
            print(f"  {row['file']}: {row['reason']}", file=sys.stderr)

    if ws is not None:
        entry = ws.add_revision(results, columns, note="as extracted")
        print(f"workspace {args.workspace}: {len(results)} originals and "
              f"extractions archived, revision {entry['revision']} written",
              file=sys.stderr)

    # Exceptions are a reported result, not a failure of the run.
    return 0


def build_parser():
    """The argument parser, as a value.

    Separate from main() so a test can read the real options rather
    than scrape --help: six flags once shipped documented nowhere but
    the help text, and scraping would not have caught it either.
    """

    parser = argparse.ArgumentParser(
        prog="vanilla",
        description="Extract plain text from documents using only the Python "
                    "standard library.")
    # Optional, because --workspace --verify / --import-csv operate on an
    # existing workspace and have no input files to name.
    parser.add_argument("paths", nargs="*", metavar="FILE")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON object per file instead of text")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="omit the ===== filename ===== banners")
    parser.add_argument("--version", action="version",
                        version=f"vanilla-extract {__version__}")
    parser.add_argument("--batch", action="store_true",
                        help="walk the given paths and emit a table instead of text")
    parser.add_argument("--csv", metavar="PATH",
                        help="with --batch: write results here (default: stdout summary)")
    parser.add_argument("--exceptions", metavar="PATH",
                        help="with --batch: write the unreadable-files table here")
    parser.add_argument("--field", action="append", default=[], metavar="NAME=REGEX",
                        help="with --batch: pull a named value out of each document "
                             "(repeatable). The first capture group wins if present.")
    parser.add_argument("--no-text", action="store_true",
                        help="with --batch: omit the full text column")
    parser.add_argument("--recognize", action="store_true",
                        help="with --batch: discover the fields from the documents "
                             "themselves instead of being given regexes")
    parser.add_argument("--min-support", type=float, default=0.5, metavar="F",
                        help="with --recognize: fraction of documents a label must "
                             "appear in to become a column (default 0.5)")
    parser.add_argument("--workspace", metavar="DIR",
                        help="keep originals, extractions and every correction "
                             "together in DIR, each hashed (SHA-256)")
    parser.add_argument("--no-copy-originals", action="store_true",
                        help="with --workspace: hash the originals but do not "
                             "copy them (for corpora too large to duplicate)")
    parser.add_argument("--import-csv", metavar="PATH",
                        help="with --workspace: file a corrected table as a new "
                             "revision, recording what it changed")
    parser.add_argument("--verify", action="store_true",
                        help="with --workspace: re-hash every artefact and "
                             "report anything that changed since capture")
    parser.add_argument("--datasheet", metavar="PATH",
                        help="with --batch: write a searchable table of each file's "
                             "state as found -- size, timestamps, permissions, "
                             "owner, links, filesystem (CSV, or .html for a "
                             "searchable page)")
    parser.add_argument("--report", metavar="PATH",
                        help="with --batch: write a self-contained HTML report with "
                             "per-document preview, in-place editing and CSV re-export")
    
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.workspace and (args.import_csv or args.verify):
        return _run_workspace_op(args)
    if not args.paths:
        parser.error("a FILE is required unless using --workspace with "
                     "--verify or --import-csv")
    if args.batch:
        return _run_batch(args)

    show_headers = len(args.paths) > 1 and not args.quiet
    failed = False          # a document could not be read, or yielded no text
    usage = False           # the command named something that is not a file

    def fail(path, reason, detail, member=None):
        """One failure, named the way the batch exceptions table names it --
        on stderr, and as a record in --json output."""
        where = f"{path}!{member}" if member is not None else path
        print(f"vanilla: {where}: {reason}: {detail}", file=sys.stderr)
        if args.json:
            record = {"file": path, "chars": 0, "text": "",
                      "reason": reason, "detail": detail}
            if member is not None:
                record["member"] = member
            print(json.dumps(record))

    for path in args.paths:
        if not os.path.exists(path):
            print(f"vanilla: {path}: no such file", file=sys.stderr)
            usage = True
            continue
        if os.path.isdir(path):
            print(f"vanilla: {path}: is a folder; use --batch to read a folder",
                  file=sys.stderr)
            usage = True
            continue
        try:
            if zipfile.is_zipfile(path) and not zip_holds_document(path):
                for name, text, error in extract_archive(path, require_text=True):
                    if error:
                        reason, _sep, detail = error.partition(": ")
                        fail(path, reason, detail, member=name)
                        failed = True
                        continue
                    if args.json:
                        print(json.dumps({"file": path, "member": name,
                                          "chars": len(text), "text": text}))
                    else:
                        _emit_text(f"{path}!{name}", text, not args.quiet)
                continue

            text = extract_file(path, require_text=True)
            if args.json:
                print(json.dumps({"file": path, "chars": len(text),
                                  "text": text}))
            else:
                _emit_text(path, text, show_headers)
        except Exception as exc:                      # noqa: BLE001
            # Read and empty (NoTextFound), unsupported, encrypted, damaged:
            # every one named with its reason code, never a blank result.
            fail(path, *reason_of(exc))
            failed = True

    return 2 if usage else (1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
