"""puretext -- pull plain text out of documents using only the Python standard library.

    from puretext import extract_file
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

from .dispatch import UnsupportedFormat, extract, sniff

__version__ = "0.2.0"
__all__ = ["extract", "extract_file", "extract_archive", "sniff",
           "UnsupportedFormat", "__version__"]

# Skip these inside archives rather than trying to sniff them.
_BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
               ".mp3", ".mp4", ".mov", ".avi", ".zip", ".gz", ".tar",
               ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf")


def extract_file(path):
    """Extract text from a file on disk."""
    with open(path, "rb") as fh:
        data = fh.read()
    return extract(data, os.path.basename(path))


def extract_archive(path, max_members=500):
    """Extract text from every readable document inside a .zip.

    Yields (member_name, text_or_None, error_or_None) so a caller can report
    what was skipped instead of quietly returning less than the archive held.

    `max_members` is a guard, not a limit on ambition: a zip bomb with a
    million entries should not hang the caller.
    """
    with zipfile.ZipFile(path) as zf:
        for i, info in enumerate(zf.infolist()):
            if i >= max_members:
                yield ("<truncated>", None,
                       f"stopped after {max_members} members")
                return
            if info.is_dir():
                continue
            name = info.filename
            if name.lower().endswith(_BINARY_EXT):
                continue
            try:
                data = zf.read(info)
            except (zipfile.BadZipFile, RuntimeError) as exc:
                yield (name, None, f"unreadable: {exc}")
                continue
            try:
                yield (name, extract(data, os.path.basename(name)), None)
            except UnsupportedFormat as exc:
                yield (name, None, str(exc))
            except Exception as exc:                  # noqa: BLE001
                # One malformed member must not abort the whole archive.
                yield (name, None, f"{type(exc).__name__}: {exc}")
