"""vanilla-extract -- pull plain text out of documents using only the Python standard library.

    from vanilla_extract import extract_file
    text = extract_file("contract.pdf")

Supports PDF, DOCX, PPTX, XLSX, ODT, RTF, EML, MBOX, HTML, XML, CSV, TSV,
JSON and plain text, plus ZIP archives containing any of the above.

No pip install. No wheels, no native extensions, no C toolchain -- which is
the whole point: it runs in locked-down environments where adding a dependency
needs an approval that takes longer than the job.
"""

import io
import os
import zipfile

from .dispatch import (NoTextFound, UnsupportedFormat, explain_empty, extract,
                       skip_reason, sniff, zip_holds_document)
from .limits import MAX_ARCHIVE_DEPTH, ArchiveTooLarge, Budget, read_member

__version__ = "0.2.0"
__all__ = ["extract", "extract_file", "extract_archive", "sniff", "explain_empty",
           "UnsupportedFormat", "NoTextFound", "ArchiveTooLarge", "__version__"]

def extract_file(path, require_text=False):
    """Extract text from a file on disk.

    Returns a string, and for a document with no text that string is empty --
    unchanged, because an empty .txt is a legitimate answer. Pass
    `require_text=True` to have an empty result raise NoTextFound instead,
    carrying the reason (empty_file, truncated_or_corrupt, limit_exceeded or
    no_text_found); `explain_empty(data, filename)` gives the same answer for
    bytes already in hand.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    text = extract(data, os.path.basename(path))
    if require_text and not text.strip():
        raise NoTextFound(*explain_empty(data, os.path.basename(path)))
    return text


def extract_archive(path, max_members=500):
    """Extract text from every readable document inside a .zip.

    Yields (member_name, text_or_None, error_or_None) so a caller can report
    what was skipped instead of quietly returning less than the archive held.
    Every member yields exactly once: a zip inside the zip is opened and its
    members named `inner.zip!member` (to MAX_ARCHIVE_DEPTH, on one budget), and
    a member that is media or a binary is named with its reason -- both used
    to be dropped without a word.

    `max_members` is a guard, not a limit on ambition: a zip bomb with a
    million entries should not hang the caller.
    """
    seen = [0]
    with zipfile.ZipFile(path) as zf:
        yield from _members(zf, "", Budget(), seen, max_members, 0)


def _members(zf, prefix, budget, seen, max_members, depth):
    """extract_archive's walk of one (possibly nested) archive. Returns True
    once the member guard stops it, so an enclosing archive stops too."""
    for info in zf.infolist():
        if seen[0] >= max_members:
            yield ("<truncated>", None, f"stopped after {max_members} members")
            return True
        seen[0] += 1
        if info.is_dir():
            continue
        name = prefix + info.filename
        try:
            with zf.open(info) as fh:
                head = fh.read(1024)
        except (zipfile.BadZipFile, RuntimeError, OSError, ValueError):
            head = b""
        if not head.startswith(b"PK"):
            why = skip_reason(info.filename, head)
            if why:
                yield (name, None, f"{why[0]}: {why[1]}")
                continue
        try:
            data = read_member(zf, info, budget)
        except ArchiveTooLarge as exc:
            yield (name, None, str(exc))
            continue
        except (zipfile.BadZipFile, RuntimeError) as exc:
            yield (name, None, f"unreadable: {exc}")
            continue
        if (data[:2] == b"PK" and not zip_holds_document(data)
                and zipfile.is_zipfile(io.BytesIO(data))):
            if depth + 1 > MAX_ARCHIVE_DEPTH:
                yield (name, None, f"limit_exceeded: archive nested more than "
                                   f"{MAX_ARCHIVE_DEPTH} deep; not opened")
                continue
            with zipfile.ZipFile(io.BytesIO(data)) as inner:
                stopped = yield from _members(inner, name + "!", budget, seen,
                                              max_members, depth + 1)
            if stopped:
                return True
            continue
        why = skip_reason(info.filename, head, lambda: data)
        if why:
            yield (name, None, f"{why[0]}: {why[1]}")
            continue
        try:
            yield (name, extract(data, os.path.basename(name)), None)
        except UnsupportedFormat as exc:
            yield (name, None, str(exc))
        except Exception as exc:                  # noqa: BLE001
            # One malformed member must not abort the whole archive.
            yield (name, None, f"{type(exc).__name__}: {exc}")
    return False
