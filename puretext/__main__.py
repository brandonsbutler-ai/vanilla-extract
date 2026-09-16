r"""CLI: python3 -m puretext <path> [...]

    python3 -m puretext report.pdf
    python3 -m puretext --json *.docx
    python3 -m puretext archive.zip

Batch a folder into a spreadsheet, with every unreadable file accounted for:

    python3 -m puretext --batch invoices/ --csv out.csv --exceptions skipped.csv
    python3 -m puretext --batch invoices/ --csv out.csv \
        --field "invoice_no=Invoice\s*#?\s*([A-Z0-9-]+)" \
        --field "total=Total\s*:?\s*\$?([0-9,]+\.[0-9]{2})"

Exit status is 1 if any input failed, so it composes in a shell pipeline.
"""

import argparse
import json
import os
import re
import sys
import zipfile

from . import UnsupportedFormat, extract_archive, extract_file, __version__
from .batch import Field, run as run_batch, write_csv


def _emit_text(label, text, show_headers):
    if show_headers:
        print(f"===== {label} =====")
    print(text)


def _run_batch(args):
    """--batch: a folder in, a results table and an exceptions table out."""
    try:
        fields = [Field.parse(spec) for spec in args.field]
    except (ValueError, re.error) as exc:
        print(f"puretext: {exc}", file=sys.stderr)
        return 2

    results, exceptions = run_batch(args.paths, fields=fields,
                                    include_text=not args.no_text)

    if args.csv:
        columns = ["file", "characters"] + [f.name for f in fields]
        if not args.no_text:
            columns.append("text")
        write_csv(results, args.csv, columns)
        print(f"{len(results)} documents -> {args.csv}", file=sys.stderr)
    else:
        for row in results:
            print(json.dumps(row))

    if args.exceptions:
        write_csv(exceptions, args.exceptions, ["file", "reason", "detail"])
        print(f"{len(exceptions)} skipped -> {args.exceptions}", file=sys.stderr)
    elif exceptions:
        # Never let these vanish just because no path was given.
        print(f"puretext: {len(exceptions)} file(s) could not be read:",
              file=sys.stderr)
        for row in exceptions:
            print(f"  {row['file']}: {row['reason']}", file=sys.stderr)

    # Exceptions are a reported result, not a failure of the run.
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="puretext",
        description="Extract plain text from documents using only the Python "
                    "standard library.")
    parser.add_argument("paths", nargs="+", metavar="FILE")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON object per file instead of text")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="omit the ===== filename ===== banners")
    parser.add_argument("--version", action="version",
                        version=f"puretext {__version__}")
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
    args = parser.parse_args(argv)

    if args.batch:
        return _run_batch(args)

    show_headers = len(args.paths) > 1 and not args.quiet
    failed = False

    for path in args.paths:
        if not os.path.exists(path):
            print(f"puretext: {path}: no such file", file=sys.stderr)
            failed = True
            continue
        try:
            if zipfile.is_zipfile(path) and not path.lower().endswith(
                    (".docx", ".pptx", ".xlsx", ".odt")):
                for name, text, error in extract_archive(path):
                    if error:
                        print(f"puretext: {path}!{name}: {error}",
                              file=sys.stderr)
                        continue
                    if args.json:
                        print(json.dumps({"file": path, "member": name,
                                          "chars": len(text), "text": text}))
                    else:
                        _emit_text(f"{path}!{name}", text, show_headers or True)
                continue

            text = extract_file(path)
            if args.json:
                print(json.dumps({"file": path, "chars": len(text),
                                  "text": text}))
            else:
                _emit_text(path, text, show_headers)
        except UnsupportedFormat as exc:
            print(f"puretext: {path}: {exc}", file=sys.stderr)
            failed = True
        except Exception as exc:                      # noqa: BLE001
            print(f"puretext: {path}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
