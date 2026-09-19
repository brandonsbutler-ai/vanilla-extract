"""Workspaces: keep the original, keep every correction, and prove what changed.

Extraction work has an awkward property. The deliverable is a table a human has
almost certainly corrected by hand, and once corrected there is no way to tell
which values came out of the document and which came out of a person -- so a
disagreement three months later has no answer.

A workspace fixes that by keeping both, and never overwriting either:

    case01/
      manifest.json          run metadata, and a SHA-256 for every artefact
      originals/             the source documents as received
      extracted/             what the tool read, per document, immutable
      revisions/             each corrected table, appended, never replaced
      report.html            the review page

The design rule is append-only. A correction is a NEW revision that records
what it changed against the one before it; nothing edits `extracted/`, and
nothing replaces a previous revision. That is what makes the question "was this
value extracted or typed?" answerable rather than a matter of recollection.

Hashing is SHA-256 over the bytes. This proves integrity and detects drift --
it is not a signature, and the manifest is not tamper-proof against someone
with write access to the workspace. Said plainly here so nobody assumes more of
it than it does.
"""

import contextlib
import csv
import datetime
import hashlib
import json
import os
import shutil

from .batch import csv_safe

MANIFEST = "manifest.json"
# How often a held manifest is written out anyway, so a process killed outright
# loses at most this many documents from the record.
_FLUSH_EVERY = 500

_SCHEMA = 1


def _utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk=1 << 20):
    """Hash a file without reading it entirely into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _safe_member(name, content_hash=None):
    """A readable, unique, escape-proof filename for an archived document.

    When `content_hash` is supplied the name becomes CONTENT-addressed, so
    re-capturing the same label with different bytes lands in a different
    file rather than overwriting the first. That is what keeps the
    append-only guarantee true when a folder is re-scanned after its
    documents have changed.

    Source labels are arbitrary: absolute paths, '..' segments, archive members
    with embedded separators. Flattening the whole path is safe but produces
    unreadable names, and a plain basename collides the moment two folders both
    hold invoice.pdf. So: the basename, plus a short digest of the full label
    for uniqueness.
    """
    cleaned = name.replace("\\", "/").replace("!", "/")
    parts = [p for p in cleaned.split("/") if p not in ("", ".", "..")]
    base = parts[-1] if parts else "document"
    # batch labels the second of two same-named archive members "dup.txt#2".
    # Sanitised as-is that became "dup.txt_2" -- an extension nothing opens --
    # so the number moves onto the stem: "dup_2".
    # (Split, not a regex: `(.+)#(\d+)` backtracks quadratically on a long
    # name with no '#', which verify_e2e measures and refuses.)
    head, sep, tail = base.rpartition("#")
    number = ""
    if sep and head and tail.isdigit():
        base, number = head, f"_{tail}"
    base = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in base).strip()
    base = base or "document"
    digest = (content_hash
              or hashlib.sha256(name.encode("utf-8")).hexdigest())[:8]
    stem, dot, ext = base.rpartition(".")
    if dot and len(ext) <= 8:
        return f"{stem[:120]}{number}_{digest}.{ext}"
    return f"{base[:120]}{number}_{digest}"


class Workspace:
    """A directory that holds originals, extractions and revisions together."""

    def __init__(self, root):
        # Set before anything else: load() and _write() consult them.
        self._held = None          # the manifest, while a run holds it
        self._unwritten = 0
        self.root = os.path.abspath(root)
        self.originals = os.path.join(self.root, "originals")
        self.extracted = os.path.join(self.root, "extracted")
        self.revisions = os.path.join(self.root, "revisions")
        self.manifest_path = os.path.join(self.root, MANIFEST)

    # -- lifecycle ---------------------------------------------------------
    def create(self, tool_version, source_paths):
        for d in (self.root, self.originals, self.extracted, self.revisions):
            os.makedirs(d, exist_ok=True)
        manifest = {
            "schema": _SCHEMA,
            "tool": f"vanilla_extract {tool_version}",
            "created": _utc(),
            "sources": [os.path.abspath(p) for p in source_paths],
            "documents": [],
            "revisions": [],
            "note": ("SHA-256 proves integrity and detects drift. It is not a "
                     "signature; a workspace is not tamper-proof against "
                     "someone who can write to it."),
        }
        self._write(manifest)
        return manifest

    def load(self):
        if self._held is not None:
            return self._held
        with open(self.manifest_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _write(self, manifest):
        if self._held is not None:
            self._held = manifest
            self._unwritten += 1
            if self._unwritten < _FLUSH_EVERY:
                return
            self._unwritten = 0
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        os.replace(tmp, self.manifest_path)      # atomic; never a half manifest

    @contextlib.contextmanager
    def deferred(self):
        """Hold the manifest in memory for the length of a run.

        capture() re-read and re-wrote the whole manifest per document, so a
        run cost the square of its corpus: 2,000 documents took 65 s, nearly
        all of it re-serialising a list that had just been serialised. Inside
        this block the manifest is kept in memory and written every
        _FLUSH_EVERY documents, and again when the block ends -- including when
        it ends in an exception, so a cancelled run still records what it
        captured.
        """
        self._held = self.load()
        self._unwritten = 0
        try:
            yield self
        finally:
            manifest, self._held = self._held, None
            self._unwritten = 0
            self._write(manifest)

    def exists(self):
        return os.path.isfile(self.manifest_path)

    # -- capture -----------------------------------------------------------
    def capture(self, label, text, source_path=None, source_bytes=None,
                copy_original=True, file_state=None):
        """Record one document: its original, what was read, and both hashes.

        `file_state` is the filesystem state as found (fileinfo.stat_record).
        Content hashes prove the bytes; this records the circumstances -- who
        owned it, when it was last modified, what the permissions were. Both
        halves are needed to answer "what was this file when it arrived".
        """
        manifest = self.load()

        # Hash the content BEFORE choosing a filename. Naming by label alone
        # meant a second capture of the same label overwrote the first, so
        # re-running a batch over an updated folder silently rewrote history
        # and then failed the workspace's own --verify.
        payload = text.encode("utf-8")
        if source_path and os.path.isfile(source_path):
            content_hash = sha256_file(source_path)
        elif source_bytes is not None:
            content_hash = sha256_bytes(source_bytes)
        else:
            content_hash = sha256_bytes(payload)
        member = _safe_member(label, content_hash)

        existing = self._already_captured(manifest, label, content_hash)
        if existing is not None:
            return existing          # same label, same bytes: nothing new

        original_rel, original_hash = None, None
        if source_path and os.path.isfile(source_path):
            original_hash = content_hash
            if copy_original:
                dest = os.path.join(self.originals, member)
                shutil.copy2(source_path, dest)
                original_rel = os.path.relpath(dest, self.root)
        elif source_bytes is not None:
            original_hash = content_hash
            if copy_original:
                dest = os.path.join(self.originals, member)
                with open(dest, "wb") as fh:
                    fh.write(source_bytes)
                original_rel = os.path.relpath(dest, self.root)

        ext_path = os.path.join(self.extracted, member + ".txt")
        with open(ext_path, "wb") as fh:
            fh.write(payload)

        entry = {
            "label": label,
            "original": original_rel,
            "original_sha256": original_hash,
            "extracted": os.path.relpath(ext_path, self.root),
            "extracted_sha256": sha256_bytes(payload),
            "characters": len(text),
            "captured": _utc(),
            "state_when_found": file_state,
        }
        manifest["documents"].append(entry)
        self._write(manifest)
        return entry

    @staticmethod
    def _already_captured(manifest, label, content_hash):
        """The existing entry for this exact label and these bytes, or None."""
        for doc in manifest["documents"]:
            if doc["label"] == label and doc.get("original_sha256") == content_hash:
                return doc
        return None

    # -- revisions ---------------------------------------------------------
    def add_revision(self, rows, columns, note="", key="file", skip_if_unchanged=False):
        """Append a corrected table and record its diff against the previous one.

        Never replaces anything. Revision N+1 states what it changed relative to
        revision N, so the chain from extraction to delivery stays readable.

        With `skip_if_unchanged`, a table identical to the previous revision is
        not filed and None is returned: re-importing the same CSV used to add
        an empty revision to the record every time.
        """
        manifest = self.load()
        previous = self._previous_rows(manifest, key)
        changes = _diff_rows(previous, rows, columns, key) if previous is not None else []
        if skip_if_unchanged and previous is not None and not changes:
            return None
        number = len(manifest["revisions"]) + 1
        name = f"revision_{number:03d}.csv"
        path = os.path.join(self.revisions, name)

        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: csv_safe(v) for k, v in row.items()})

        entry = {
            "revision": number,
            "file": os.path.relpath(path, self.root),
            "sha256": sha256_file(path),
            "rows": len(rows),
            "created": _utc(),
            "note": note,
            "changes_from_previous": changes,
            "change_count": len(changes),
        }
        manifest["revisions"].append(entry)
        self._write(manifest)
        return entry

    def _previous_rows(self, manifest, key):
        if not manifest["revisions"]:
            return None
        last = manifest["revisions"][-1]["file"]
        path = os.path.join(self.root, last)
        if not os.path.isfile(path):
            return None
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def verify(self):
        """Re-hash every artefact. Returns a list of problems, empty when intact."""
        manifest = self.load()
        problems = []
        for doc in manifest["documents"]:
            for kind in ("original", "extracted"):
                rel = doc.get(kind)
                if not rel:
                    continue
                path = os.path.join(self.root, rel)
                if not os.path.isfile(path):
                    problems.append(f"missing {kind}: {rel}")
                    continue
                if sha256_file(path) != doc.get(f"{kind}_sha256"):
                    problems.append(f"{kind} changed since capture: {rel}")
        for rev in manifest["revisions"]:
            path = os.path.join(self.root, rev["file"])
            if not os.path.isfile(path):
                problems.append(f"missing revision: {rev['file']}")
            elif sha256_file(path) != rev["sha256"]:
                problems.append(f"revision changed after the fact: {rev['file']}")
        return problems


def _diff_rows(old_rows, new_rows, columns, key):
    """Cell-level differences between two tables, matched on `key`."""
    old_by_key = {r.get(key): r for r in old_rows}
    new_by_key = {r.get(key): r for r in new_rows}
    changes = []
    for k, new in new_by_key.items():
        old = old_by_key.get(k)
        if old is None:
            changes.append({"file": k, "column": "*", "from": None, "to": "(added)"})
            continue
        for col in columns:
            if col == key:
                continue
            before = (old.get(col) or "").strip()
            after = str(new.get(col) or "").strip()
            if before != after:
                changes.append({"file": k, "column": col,
                                "from": before, "to": after})
    for k in old_by_key:
        if k not in new_by_key:
            changes.append({"file": k, "column": "*", "from": "(present)",
                            "to": "(removed)"})
    return changes


def read_csv_rows(path):
    """(rows, columns, encoding) of a corrected table.

    UTF-8 first (with or without the BOM Excel writes for "CSV UTF-8"), then
    Windows-1252 -- what Excel's plain "CSV" save produces on Windows, and
    what raised a raw UnicodeDecodeError here before. The encoding used is
    returned so the caller can say which it was.
    """
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=encoding) as fh:
                reader = csv.DictReader(fh)
                return list(reader), list(reader.fieldnames or []), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path}: neither UTF-8 nor Windows-1252 text; save it "
                     f"from the spreadsheet as CSV UTF-8 and import that")
