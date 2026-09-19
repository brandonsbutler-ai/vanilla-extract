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
import io
import os
import re
import zipfile

from . import UnsupportedFormat, extract, extract_file
from . import dispatch, fileinfo, limits, recognize
from .dispatch import explain_empty, skip_reason, zip_holds_document
from .limits import (MAX_ARCHIVE_DEPTH, ArchiveTooLarge, Budget, StreamTooLarge,
                     read_member)

# One decompression budget per archive, so the 1 GB whole-archive cap that
# limits.py documents actually applies in batch mode. Without it only the
# per-member cap ran, and forty members each declaring 200 MB at exactly the
# allowed ratio decompressed to 8 GB.
_BUDGETS = {}


def _budget_for(archive):
    return _BUDGETS.setdefault(os.path.abspath(archive), Budget())
from .formats.pdf import EncryptedPDF, UndecodableText

# Media and binaries: a name on this list is a CLAIM that the file is not a
# document. dispatch.skip_reason() checks the bytes before believing it, and a
# file that really is media is REPORTED (image_no_text_layer, not_a_document)
# rather than dropped -- it used to appear in no table at all, with exit 0.
# ONE list, shared with the GUI pre-scan (session.Index), so what it counts is
# what this walk processes.
SKIP_EXT = dispatch.SKIP_EXT

# Directories that are tooling, not a delivery. They are not walked -- a scan
# once copied 36 GB of .git and .venv -- but each is REPORTED, with the number
# of files it holds, the way a survey states what it excluded.
_NOISE_DIRS = {".git": "version control", ".hg": "version control",
               ".svn": "version control", "__pycache__": "Python bytecode cache",
               ".tox": "tool cache", ".mypy_cache": "tool cache",
               ".pytest_cache": "tool cache", ".ruff_cache": "tool cache"}


# Every reason a row in the exceptions table can carry, and what it means.
# The README names each one; a test holds the two together.
REASONS = {
    "encrypted": "a PDF encrypted with the standard security handler",
    "undecodable_fonts": "text drawn in fonts with no usable character map",
    "unsupported_format": "neither content nor name identifies a reader",
    "empty_file": "0 bytes",
    "truncated_or_corrupt": "cut off before its end, or XML that is not well-formed",
    "no_text_found": "read, and no text in it -- for a PDF of images, a scan",
    "limit_exceeded": "a safety limit: entity expansion, a PDF stream, archive depth or size",
    "archive_bomb": "an archive member past the size or compression-ratio limit",
    "unreadable": "the file could not be opened or read",
    "error": "an unexpected failure, named with its exception",
    "image_no_text_layer": "an image: pixels, not text (no OCR here)",
    "not_a_document": "media, fonts, compiled code or a database",
    "hidden_directory": "a dot-folder, not walked; name it to read it",
    "excluded_directory": "tooling (`.git`, `__pycache__`, a virtualenv, a cache), not walked",
    "symlink_not_followed": "a symlinked folder, not followed",
    "unreadable_directory": "a folder that could not be listed",
    "field_extraction_failed": "the row was kept; a field pattern failed on it",
}


class Skip:
    """An input the walk accounts for without reading it."""

    def __init__(self, reason, detail, member=None, files=None):
        self.reason, self.detail = reason, detail
        self.member = member            # the Member, for one inside an archive
        self.files = files              # for a directory: files it holds


class Member(tuple):
    """(zf, info, top): an archive member. `zf` is its archive, ALREADY OPEN --
    the walk opens each archive once and holds it while the run reads the
    member, because re-opening a ZipFile re-parses its whole central directory
    and doing that per member made 8,000 members take 197 s. `top` is the
    archive on disk, whose one budget every nested read spends from."""

    def __new__(cls, zf, info, top):
        return super().__new__(cls, (zf, info, top))


def _zip(container):
    return zipfile.ZipFile(container if isinstance(container, str)
                           else io.BytesIO(container))


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
    """Not a SKIP_EXT name. Kept for callers that only have a name; the walk
    itself checks the bytes before trusting one (see dispatch.skip_reason)."""
    return not name.lower().endswith(SKIP_EXT)


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _head(path, size=1024):
    with open(path, "rb") as fh:
        return fh.read(size)


def _excluded(root, name):
    """(reason, what) for a directory the walk does not enter, or None."""
    if name in _NOISE_DIRS:
        return "excluded_directory", _NOISE_DIRS[name]
    if os.path.isfile(os.path.join(root, name, "pyvenv.cfg")):
        return "excluded_directory", "Python virtual environment"
    if name.startswith("."):
        return "hidden_directory", "hidden directory"
    return None


def _walk(paths, recurse_archives=True):
    """Yield (label, source) for EVERY input under `paths`.

    `source` is None for a file on disk, a Member for a document inside an
    archive, or a Skip for an input that is accounted for without being read:
    media and binaries (checked by their bytes, not their names), directories
    that are tooling rather than a delivery, hidden directories, and archives
    nested past MAX_ARCHIVE_DEPTH. Nothing is dropped: every yield becomes a
    result row or an exception row, and the GUI pre-scan counts these same
    yields, so its number is the run's number.

    A hidden directory (.from_client/) is reported rather than read, because a
    dot-name is how tooling hides itself; naming it on the command line reads it.
    """
    def _archive(container, prefix, top, depth, spent=None):
        # One budget per archive on disk, across every nesting level: members
        # yielded and their declared uncompressed bytes. A 23 KB zip nesting 16
        # copies five deep stood for 1,048,576 documents; past the budget the
        # rest is ONE limit_exceeded row, not a million.
        spent = spent if spent is not None else {"members": 0, "bytes": 0, "stop": False}
        try:
            zf = _zip(container)
        except (zipfile.BadZipFile, OSError, RuntimeError):
            yield prefix, None
            return
        with zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            seen = {}
            for info in members:
                if spent["stop"]:
                    return
                if (spent["members"] >= limits.MAX_ARCHIVE_MEMBERS or
                        spent["bytes"] + info.file_size > limits.MAX_ARCHIVE_WALK_BYTES):
                    spent["stop"] = True
                    yield top, Skip(
                        "limit_exceeded",
                        f"the archive holds more than it may: {spent['members']:,} "
                        f"members read (limit {limits.MAX_ARCHIVE_MEMBERS:,}), "
                        f"{spent['bytes'] // (1024 * 1024):,} MB declared across all "
                        f"levels (limit {limits.MAX_ARCHIVE_WALK_BYTES // (1024 * 1024):,} MB); "
                        f"the rest, from {prefix}!{info.filename} on, was not read")
                    return
                spent["bytes"] += info.file_size
                seen[info.filename] = seen.get(info.filename, 0) + 1
                suffix = f"#{seen[info.filename]}" if seen[info.filename] > 1 else ""
                label = f"{prefix}!{info.filename}{suffix}"
                member = Member(zf, info, top)
                try:
                    with zf.open(info) as fh:
                        head = fh.read(1024)
                except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
                    head = b""      # encrypted or damaged: extraction reports it
                if not head.startswith(b"PK"):
                    spent["members"] += 1
                    why = skip_reason(info.filename, head)
                    yield label, (Skip(*why, member=member) if why else member)
                    continue
                # A zip signature: an office document, or an archive to open.
                try:
                    data = read_member(zf, info, _budget_for(top))
                except ArchiveTooLarge as exc:
                    spent["members"] += 1
                    yield label, Skip("archive_bomb", str(exc), member=member)
                    continue
                except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                    spent["members"] += 1
                    yield label, Skip("unreadable", f"{type(exc).__name__}: {exc}",
                                      member=member)
                    continue
                if zip_holds_document(data) or not zipfile.is_zipfile(io.BytesIO(data)):
                    # A peek, not the read: extraction reads it again and spends
                    # then, so the archive budget must not be charged twice.
                    _budget_for(top).remaining += len(data)
                    spent["members"] += 1
                    why = skip_reason(info.filename, head, lambda: data)
                    yield label, (Skip(*why, member=member) if why else member)
                elif depth + 1 > MAX_ARCHIVE_DEPTH:
                    spent["members"] += 1
                    yield label, Skip("limit_exceeded",
                                      f"archive nested more than {MAX_ARCHIVE_DEPTH} "
                                      f"deep; not opened", member=member)
                else:
                    # A nested zip is a member entry too. Counting only leaves
                    # let an archive of nothing but nested empty zips open
                    # every one of them: 28 KB at depth 6 ran for 286 s.
                    spent["members"] += 1
                    yield from _archive(data, label, top, depth + 1, spent)

    def _file(full):
        if full.lower().endswith(SKIP_EXT):
            try:
                why = skip_reason(full, _head(full), lambda: full)
            except OSError:
                why = None          # unreadable: extraction names the error
            if why:
                yield full, Skip(*why)
                return
        # Content decides, not the name: a DOCX saved as .pdf is a document, a
        # plain archive saved as .docx is an archive. is_zipfile only validates
        # the end-of-central-directory record, so a damaged central directory
        # passes it and raises inside _archive -- which yields (full, None)
        # rather than killing the batch.
        if (recurse_archives and zipfile.is_zipfile(full)
                and not zip_holds_document(full)):
            yield from _archive(full, full, full, 0)
        else:
            yield full, None

    for path in paths:
        if os.path.isdir(path):
            # os.walk skips a folder it cannot list unless told what to do with
            # the error, and never enters a symlinked folder; both used to leave
            # no trace. Symlinks stay unfollowed (a link back up the tree is a
            # loop) but each is reported, as is every folder that would not list.
            unlisted = []
            for root, dirs, files in os.walk(path, onerror=unlisted.append):
                while unlisted:
                    err = unlisted.pop(0)
                    yield err.filename, Skip(
                        "unreadable_directory",
                        f"the folder could not be listed ({err.strerror}); nothing "
                        f"in it was read")
                kept = []
                for d in sorted(dirs):
                    full = os.path.join(root, d)
                    if os.path.islink(full):
                        yield full, Skip(
                            "symlink_not_followed",
                            f"a link to the folder {os.path.realpath(full)}; not "
                            f"followed, since a link can loop back up the tree -- "
                            f"name the target on the command line to read it")
                        continue
                    why = _excluded(root, d)
                    if why is None:
                        kept.append(d)
                        continue
                    full = os.path.join(root, d)
                    n = sum(len(f) for _r, _d, f in os.walk(full))
                    tail = ("name it on the command line to read it"
                            if why[0] == "hidden_directory" else "not walked")
                    yield full, Skip(why[0], f"{why[1]}: {n} file(s) not read; {tail}",
                                     files=n)
                dirs[:] = kept
                for name in sorted(files):
                    yield from _file(os.path.join(root, name))
            for err in unlisted:          # the top folder itself, or the last one
                yield err.filename, Skip(
                    "unreadable_directory",
                    f"the folder could not be listed ({err.strerror}); nothing "
                    f"in it was read")
        else:
            yield from _file(path)


def _load(label, source):
    """Return (text, source_path, source_bytes) -- the extras let a workspace
    archive the original alongside what was read from it."""
    if source is None:
        return extract_file(label), label, None
    zf, info, top = source
    data = read_member(zf, info, _budget_for(top))
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
            zf, info, top = source
            src_bytes = read_member(zf, info, _budget_for(top))
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
            member = source.member if isinstance(source, Skip) else source
            if collect_metadata or workspace is not None:
                try:
                    if member is None:
                        meta = fileinfo.stat_record(label)
                    else:
                        _container, info, top = member
                        meta = fileinfo.zip_member_record(info, top)
                        meta["path"] = label    # names the nesting, if any
                except OSError as exc:
                    meta = {"path": label, "name": os.path.basename(label),
                            "read_result": f"stat failed: {exc}"}
            if isinstance(source, Skip):
                # Accounted for, not read: media, tooling, hidden, too deep.
                _record(datasheet, meta, None, source.reason)
                row = {"file": label, "reason": source.reason,
                       "detail": source.detail}
                if source.files is not None:
                    row["files_not_read"] = source.files
                else:
                    _archive_failure(workspace, label, meta, member, source.reason)
                exceptions.append(row)
                continue
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
            except StreamTooLarge as exc:
                _record(datasheet, meta, None, "limit_exceeded")
                _archive_failure(workspace, label, meta, source, "limit_exceeded")
                exceptions.append({"file": label, "reason": "limit_exceeded",
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
                # Read, and nothing came out. A caller needs to see this, not a
                # blank row -- and needs the RIGHT reason: an empty file, a
                # truncated one and a scan need three different next steps.
                try:
                    data = src_bytes if src_bytes is not None else _read(label)
                    reason, detail = explain_empty(data, os.path.basename(label))
                except (OSError, zipfile.BadZipFile, ArchiveTooLarge) as exc:
                    reason, detail = ("no_text_found", f"holds no extractable text; "
                                      f"re-reading it to say why failed: {exc}")
                _record(datasheet, meta, 0, reason)
                _archive_failure(workspace, label, meta, source, reason)
                exceptions.append({"file": label, "reason": reason, "detail": detail})
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

# A plain negative number, amount or percentage is data, not a formula, and
# prefixing it turned -$251.00 and -4.5% into text a spreadsheet will not sum.
# The match is a FULL match on a strict shape, so anything with an operator,
# a letter, a space or a line break after the minus is still neutralized.
# report.py's export applies the same pattern in the browser.
# [0-9], not \d: Python's \d also matches other scripts' digits, JavaScript's
# does not, and the two guards must agree on every cell.
_NEGATIVE_NUMBER = re.compile(r"-[$£€]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?%?")


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
    if text.startswith(_FORMULA_LEAD) and not _NEGATIVE_NUMBER.fullmatch(text):
        return "'" + text
    return text


# Excel's documented maximum characters in one cell. A document's full text
# can be millions of characters -- 21.6 million from one JSON scan file -- and
# such a cell is cut off by Excel and refused outright by Python's own csv
# reader at its default 131,072-character field limit. So no CSV cell this
# writes is longer: a longer value is cut, the cut is marked in the cell, and
# a `text_truncated` column says which rows lost text. The full text stays in
# the document itself and, with --workspace, in extracted/.
CSV_CELL_LIMIT = 32767


def _fit_cell(text):
    """(value, truncated) with len(value) <= CSV_CELL_LIMIT."""
    if len(text) <= CSV_CELL_LIMIT:
        return text, False
    note = f" [... truncated: {len(text):,} characters in full]"
    return text[:CSV_CELL_LIMIT - len(note)] + note, True


def write_csv(rows, path, columns=None):
    """Write rows to CSV. Returns the number of data rows written.

    No cell exceeds CSV_CELL_LIMIT. When a `text` column is written, a
    `text_truncated` column follows it (True/False), so a cut is never silent.
    """
    columns = list(columns or (rows[0].keys() if rows else ["file"]))
    if "text" in columns and "text_truncated" not in columns:
        columns.insert(columns.index("text") + 1, "text_truncated")
    if not rows:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=columns).writeheader()
        return 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {}
            for k, v in row.items():
                out[k], cut = _fit_cell(csv_safe(v))
                if k == "text":
                    out["text_truncated"] = cut
            if "text_truncated" in columns and "text" not in row:
                out["text_truncated"] = False
            writer.writerow(out)
    return len(rows)
