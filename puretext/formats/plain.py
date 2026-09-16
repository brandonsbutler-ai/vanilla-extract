"""Plain and semi-structured text: .txt, .md, .log, .csv, .tsv, .json.

These need no parsing to read, but they do need encoding detection, because
"just decode as utf-8" fails on anything a Windows tool wrote. We try a short
ladder of encodings and honour a BOM when one is present.
"""

import csv
import io
import json

_LADDER = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def decode_text(data):
    """Decode bytes using the first encoding in the ladder that succeeds.

    latin-1 is last and cannot fail, so this always returns a string.
    """
    for enc in _LADDER:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def extract_text(fh):
    data = fh.read() if hasattr(fh, "read") else fh
    return decode_text(data)


def extract_csv(fh):
    """Rows as tab-separated lines, with the delimiter sniffed rather than assumed."""
    data = fh.read() if hasattr(fh, "read") else fh
    text = decode_text(data)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error:
        # "field larger than field limit" on a legitimate file -- a base64 blob
        # or a long notes column. The raw text is still useful; losing the file
        # is not an acceptable alternative.
        return text
    return "\n".join("\t".join(cell.strip() for cell in row) for row in rows if row)


def _walk_json(node, path, out):
    if isinstance(node, dict):
        for key, value in node.items():
            _walk_json(value, f"{path}.{key}" if path else str(key), out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _walk_json(value, f"{path}[{i}]", out)
    elif node is not None:
        out.append(f"{path}: {node}" if path else str(node))


def extract_json(fh):
    """Flatten JSON to `path: value` lines, which is what makes it greppable."""
    data = fh.read() if hasattr(fh, "read") else fh
    text = decode_text(data)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return text          # not valid JSON; the raw text is still useful
    out = []
    try:
        _walk_json(parsed, "", out)
    except RecursionError:
        return text          # nested past the interpreter's limit
    return "\n".join(out)
