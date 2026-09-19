"""Batch extraction: a folder of documents in, one table out.

This is the shape the work actually takes. Nobody extracts one file; they have
a directory of invoices and they want a spreadsheet. Two outputs, always:

    results     one row per document that was read
    exceptions  one row per document that was NOT read, and why

The second table is the point. Encrypted PDFs, scanned pages with no text
layer, and files whose fonts carry no character map all look like empty
documents to most extraction tools, and they come through as blank rows that
nobody notices until the data is already in use. Naming them is the difference
between a delivery a client can trust and one they cannot.

Optional field extraction pulls named values out of each document with a
regex, so "give me invoice number and total from these 400 PDFs" is one
command rather than a script.
"""

import csv
import os
import re
import zipfile

from . import UnsupportedFormat, extract, extract_file
from . import fileinfo, recognize
from .dispatch import zip_holds_document
from .limits import ArchiveTooLarge, Budget, read_member

# One decompression budget per archive, so the 1 GB whole-archive cap that
# limits.py documents actually applies in batch mode. Without it only the
# per-member cap ran, and forty members each declaring 200 MB at exactly the
# allowed ratio decompressed to 8 GB.
_BUDGETS = {}


def _budget_for(archive):
    return _BUDGETS.setdefault(os.path.abspath(archive), Budget())
from .formats.pdf import EncryptedPDF, UndecodableText

# Media and binaries we do not attempt; listing them keeps the exceptions
# table meaningful rather than full of images.
#
# ARCHIVE EXTENSIONS ARE DELIBERATELY ABSENT. A readable .zip is recursed by
# _walk and never reaches here. An UNREADABLE one used to land in this list and
# disappear -- no result row, no exception row -- because is_zipfile() said no
# and the extension said skip. A corrupt archive is exactly the thing a caller
# needs told about, so it now falls through to extraction and is reported as
# an unsupported format instead.
# ONE list of "never a document", shared with the GUI pre-scan (session.Index)
# so the files it counts are exactly the files this walk processes. Two divergent
# copies drifted once and let a run walk .git/.venv, copy 36 GB and count past
# 100%. It covers images/audio/video/fonts AND compiled/code/database artifacts.
SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
            ".mp3", ".mp4", ".mov", ".avi", ".wav", ".woff", ".woff2", ".ttf",
            ".otf", ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin",
            ".o", ".a", ".class", ".jar", ".db", ".sqlite", ".lock")


class Field:
    """A named value to pull out of each document's text."""

    def __init__(self, name, pattern, flags=re.IGNORECASE):
        self.name = name
        self.regex = re.compile(pattern, flags)

    def find(self, text):
        """First match; the first capture group if the pattern has one.

        `m.group(1)` is None when group 1 sits in a branch that did not
        participate -- `(a)|b` matching "b" -- so the `or ""` matters: without
        it a perfectly reasonable alternation pattern raised AttributeError and
        killed the whole batch.
        """
        m = self.regex.search(text)
        if not m:
            return ""
        value = m.group(1) if m.groups() else m.group(0)
        return (value or "").strip()

    @classmethod
    def parse(cls, spec):
        """Parse a NAME=REGEX command-line spec."""
        name, sep, pattern = spec.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"field spec must be NAME=REGEX, got {spec!r}")
        try:
            return cls(name.strip(), pattern)
        except re.error as exc:
            # Say WHICH field and WHAT pattern arrived. The shell has usually
            # rewritten it on the way -- inside double quotes `\$` becomes `$` --
            # so the pattern Python received is the evidence, and a bare
            # "nothing to repeat at position 14" named neither.
            raise ValueError(
                f"--field {name.strip()}: {exc} in the pattern, as received: "
                f"{pattern}\n  (quote --field in single quotes, so the shell "
                f"leaves backslashes and $ alone)") from exc


# A ZIP may contain the same name twice. zf.getinfo(name) resolves through
# NameToInfo, which keeps only the LAST entry, so re-fetching a member by name
# silently returns the wrong bytes -- the earlier member is never extracted,
# never hashed and never reported. That is a standard archive-evasion trick and
# this is the untrusted-input path, so the ZipInfo OBJECT is carried through
# instead of the filename.


def _in_scope(name):
    """A candidate document: not a non-document extension. The SAME rule the
    GUI pre-scan applies, so what runs is exactly what was counted."""
    return not name.lower().endswith(SKIP_EXT)


def _walk(paths, recurse_archives=True):
    """Yield (label, loader) for every candidate document under `paths`.

    SKIP_EXT files are dropped at BOTH the file level and inside archives, and
    ON DISK a directory whose name starts with a dot (.git, .venv, caches) is
    pruned as well, so a scanned tree's version control and virtualenv are never
    walked, copied or counted. This rule matches session.Index exactly.

    The dot-directory prune is the on-disk walk only. A `.git/config` carried
    INSIDE an archive still comes through -- it has no SKIP_EXT extension, so it
    reaches extraction and is reported as an unsupported format. That is a row
    in the report rather than a silent drop, which is the behaviour this module
    promises; pruning it too would be a change of product behaviour, not a fix.
    """
    def _archive(full):
        try:
            with zipfile.ZipFile(full) as zf:
                members = [i for i in zf.infolist() if not i.is_dir()]
        except (zipfile.BadZipFile, OSError, RuntimeError):
            yield full, None
            return
        for i, info in enumerate(members):
            if not _in_scope(info.filename):
                continue
            dup = sum(1 for m in members[:i] if m.filename == info.filename)
            suffix = f"#{dup + 1}" if dup else ""
            yield f"{full}!{info.filename}{suffix}", (full, info)

    for path in paths:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in sorted(files):
                    if not _in_scope(name):
                        continue
                    full = os.path.join(root, name)
                    # Recurse into archives found by walking, too. Previously
                    # only archives named on the command line were opened, so a
                    # bundle.zip inside a scanned folder produced no result row
                    # AND no exception row -- it simply disappeared, which is
                    # the one thing this module promises never to do.
                    # Content decides, not the name: a DOCX saved as .pdf is
                    # a document, a plain archive saved as .docx is an archive.
                    if (recurse_archives and zipfile.is_zipfile(full)
                            and not zip_holds_document(full)):
                        yield from _archive(full)
                    else:
                        yield full, None
        elif (zipfile.is_zipfile(path) and recurse_archives
                and not zip_holds_document(path)):
            # is_zipfile only validates the end-of-central-directory record, so a
            # damaged central directory passes it and raises inside _archive --
            # which yields (path, None) rather than killing the batch, the one
            # thing this module promises never to happen.
            yield from _archive(path)
        elif _in_scope(path):
            yield path, None


def _load(label, source):
    """Return (text, source_path, source_bytes) -- the extras let a workspace
    archive the original alongside what was read from it."""
    if source is None:
        return extract_file(label), label, None
    archive, info = source
    with zipfile.ZipFile(archive) as zf:
        data = read_member(zf, info, _budget_for(archive))
    return (extract(data, os.path.basename(info.filename.replace("\\", "/"))),
            None, data)


def _archive_failure(workspace, label, meta, source, reason):
    """Record the ORIGINAL of a document we could not read.

    provenance.py describes a workspace as holding "the source documents as
    received", and --verify reports that every artefact matches its hash. Both
    were true only of the documents that READ successfully: encrypted PDFs,
    unsupported formats and scans with no text layer were skipped entirely, so
    the files most likely to be disputed were the ones missing from the record.
    """
    if workspace is None:
        return
    src_path = label if source is None else None
    src_bytes = None
    if source is not None:
        try:
            archive, info = source
            with zipfile.ZipFile(archive) as zf:
                src_bytes = read_member(zf, info, _budget_for(archive))
        except Exception:                 # noqa: BLE001 - best effort only
            src_bytes = None
    try:
        workspace.capture(label, "", source_path=src_path,
                          source_bytes=src_bytes, file_state=meta)
    except Exception:                     # noqa: BLE001
        pass                              # never let archiving break the batch


def _record(sheet, meta, characters, result):
    """Append one datasheet row, if metadata collection is on."""
    if meta is None:
        return
    meta = dict(meta)
    meta["characters_extracted"] = characters
    meta["read_result"] = meta.get("read_result") or result
    sheet.append(meta)


def run(paths, fields=None, include_text=True, max_text=None, auto_labels=None,
        workspace=None, copy_originals=True, collect_metadata=False,
        on_document=None):
    """Extract every document under `paths`.

    Returns (results, exceptions) as lists of dicts. Nothing raises: a failure
    on one document becomes a row in `exceptions` so the batch completes.

    `on_document(label, ok)` is called after each document, if given. It exists
    so a caller with a progress bar does not have to re-implement the walk --
    which is the part that knows about archives, skipped extensions and member
    names, and is exactly the part a second implementation would get wrong.
    A callback that raises stops the run; nothing else does.

    `auto_labels` are labels discovered by recognize.infer_schema(); each
    becomes a column filled from the document's own label/value pairs.

    `workspace` is a provenance.Workspace; when given, every document's
    original and extracted text are archived with their hashes as they are read.
    """
    fields = fields or []
    auto_labels = auto_labels or []
    datasheet = []
    _BUDGETS.clear()
    results, exceptions = [], []

    for label, source in _walk(paths):
        # Whether this document produced a row or an exception, decided by
        # what the body appended rather than by a flag each of the six exits
        # would have to remember to set.
        _failed_before = len(exceptions)
        try:
            meta = None
            if collect_metadata or workspace is not None:
                try:
                    if source is None:
                        meta = fileinfo.stat_record(label)
                    else:
                        archive, info = source
                        meta = fileinfo.zip_member_record(info, archive)
                except OSError as exc:
                    meta = {"path": label, "name": os.path.basename(label),
                            "read_result": f"stat failed: {exc}"}
            try:
                text, src_path, src_bytes = _load(label, source)
            except EncryptedPDF as exc:
                _record(datasheet, meta, None, "encrypted")
                _archive_failure(workspace, label, meta, source, "encrypted")
                exceptions.append({"file": label, "reason": "encrypted",
                                   "detail": str(exc)})
                continue
            except UndecodableText as exc:
                _record(datasheet, meta, None, "undecodable_fonts")
                _archive_failure(workspace, label, meta, source, "undecodable_fonts")
                exceptions.append({"file": label, "reason": "undecodable_fonts",
                                   "detail": str(exc)})
                continue
            except UnsupportedFormat as exc:
                _record(datasheet, meta, None, "unsupported_format")
                _archive_failure(workspace, label, meta, source, "unsupported_format")
                exceptions.append({"file": label, "reason": "unsupported_format",
                                   "detail": str(exc)})
                continue
            except ArchiveTooLarge as exc:
                _record(datasheet, meta, None, "archive_bomb")
                _archive_failure(workspace, label, meta, source, "archive_bomb")
                exceptions.append({"file": label, "reason": "archive_bomb",
                                   "detail": str(exc)})
                continue
            except (OSError, zipfile.BadZipFile) as exc:
                _record(datasheet, meta, None, "unreadable")
                _archive_failure(workspace, label, meta, source, "unreadable")
                exceptions.append({"file": label, "reason": "unreadable",
                                   "detail": f"{type(exc).__name__}: {exc}"})
                continue
            except Exception as exc:                      # noqa: BLE001
                _record(datasheet, meta, None, "error")
                _archive_failure(workspace, label, meta, source, "error")
                exceptions.append({"file": label, "reason": "error",
                                   "detail": f"{type(exc).__name__}: {exc}"})
                continue

            if not text.strip():
                # Read fine, contained nothing. Almost always a scan with no text
                # layer -- a caller needs to see this, not a blank row.
                _record(datasheet, meta, 0, "no_text_found")
                _archive_failure(workspace, label, meta, source, "no_text_found")
                exceptions.append({"file": label, "reason": "no_text_found",
                                   "detail": "document read successfully but holds "
                                             "no extractable text (often a scan "
                                             "with no text layer)"})
                continue

            if workspace is not None:
                workspace.capture(label, text, source_path=src_path,
                                  source_bytes=src_bytes,
                                  copy_original=copy_originals,
                                  file_state=meta)

            _record(datasheet, meta, len(text), "read")
            row = {"file": label, "characters": len(text)}
            try:
                if auto_labels:
                    row.update(recognize.extract_fields(text, auto_labels))
                for field in fields:
                    row[field.name] = field.find(text)
            except Exception as exc:                  # noqa: BLE001
                # A field pattern that misbehaves on one document must not cost the
                # caller the other 399. The row is kept with blank fields and the
                # failure is reported.
                for name in list(auto_labels) + [f.name for f in fields]:
                    row.setdefault(name, "")
                exceptions.append({"file": label, "reason": "field_extraction_failed",
                                   "detail": f"{type(exc).__name__}: {exc}"})
            if include_text:
                row["text"] = text[:max_text] if max_text else text
            results.append(row)

        finally:
            if on_document is not None:
                on_document(label, len(exceptions) == _failed_before)
    if collect_metadata:
        return results, exceptions, datasheet
    return results, exceptions


_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    r"""Neutralize spreadsheet formula injection in an extracted value.

    Excel, LibreOffice and Sheets treat a cell beginning `=`, `+`, `-`, `@`,
    tab or CR as a formula. The documented workflow here is "untrusted
    documents in, a spreadsheet the client opens out", so a document
    containing `=cmd|' /C calc'!A0` is a live path to code execution on the
    reviewer's machine. Prefixing an apostrophe is the standard defence: the
    value still reads correctly, it just is not evaluated.
    """
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_LEAD) else text


def write_csv(rows, path, columns=None):
    """Write rows to CSV. Returns the number of data rows written."""
    if not rows:
        columns = columns or ["file"]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=columns).writeheader()
        return 0
    columns = columns or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: csv_safe(v) for k, v in row.items()})
    return len(rows)
