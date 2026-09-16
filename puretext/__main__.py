"""CLI: python3 -m puretext <path> [...]

    python3 -m puretext report.pdf
    python3 -m puretext --json *.docx
    python3 -m puretext archive.zip
    find . -name '*.pdf' | xargs python3 -m puretext --quiet > all.txt

Exit status is 1 if any input failed, so it composes in a shell pipeline.
"""

import argparse
import json
import os
import sys
import zipfile

from . import UnsupportedFormat, extract_archive, extract_file, __version__


def _emit_text(label, text, show_headers):
    if show_headers:
        print(f"===== {label} =====")
    print(text)


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
    args = parser.parse_args(argv)

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
