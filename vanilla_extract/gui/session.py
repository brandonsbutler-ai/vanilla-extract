"""What the window does, without the window.

Every decision the desktop application makes lives here: what a dropped path
turns into, what the options mean, what running produces, and what to say when
it goes wrong. None of it imports tkinter.

That split is not tidiness. The library's claim is that it has no dependencies,
and the desktop application has one; keeping the logic on this side means the
claim stays true for everything except the window itself, and it means the
behaviour can be tested without a display.
"""

import os
import time

from .. import __version__
from .. import batch, recognize, report
from ..provenance import Workspace

# The batch reader's own "not a document" list, imported rather than mirrored so
# the count this index shows and the set the run processes cannot drift apart.
from ..batch import SKIP_EXT as _SKIP


class DropError(Exception):
    """A dropped thing this application cannot work with."""


class Cancelled(Exception):
    """Raised out of a run when the caller asks it to stop. Not an error --
    it is how a Stop button reaches a batch already partway through."""


def folders_from_drop(payload):
    """Every folder a drag-and-drop payload refers to, in order.

    Several folders can be dropped at once, and files dropped alongside them
    resolve to their own folder. Duplicates collapse, because dropping a
    folder and one of the files inside it is one folder, not two.
    """
    out, seen = [], set()
    for raw in _split_payload(payload):
        folder = _folder_of(raw)
        if folder not in seen:
            seen.add(folder)
            out.append(folder)
    if not out:
        raise DropError("nothing was dropped")
    return out


def _folder_of(path):
    if not os.path.exists(path):
        raise DropError(f"{path} does not exist")
    full = os.path.abspath(path)
    return os.path.dirname(full) if os.path.isfile(full) else full


def _split_payload(payload):
    """Tk's drop payload, split into paths. See folder_from_drop."""
    if not payload or not str(payload).strip():
        raise DropError("nothing was dropped")
    if isinstance(payload, (list, tuple)):
        return [str(p) for p in payload if str(p).strip()]
    payload = str(payload)
    if os.path.exists(payload.strip()):
        return [payload.strip()]
    paths, current, depth = [], "", 0
    for ch in payload:
        if ch == "{":
            depth += 1
            if depth == 1:
                continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                paths.append(current)
                current = ""
                continue
        if ch == " " and depth == 0:
            if current:
                paths.append(current)
                current = ""
            continue
        current += ch
    if current:
        paths.append(current)
    return [p for p in paths if p]


def folder_from_drop(payload):
    """The folder a drag-and-drop payload refers to.

    Tk hands over a brace-quoted list when a path contains spaces, and hands
    over several paths when several things are dropped. A file is accepted and
    resolved to its parent, because dropping one invoice out of a folder is an
    obvious thing to do and refusing it would be pedantry.
    """
    if not payload or not payload.strip():
        raise DropError("nothing was dropped")
    # A path that exists as given wins. Tk braces anything containing a space,
    # so splitting on spaces is right for what Tk sends -- but this function is
    # also called with a plain path from the folder picker and from tests, and
    # splitting "/home/me/my docs" into two non-existent paths is a confusing
    # way to refuse a folder that is plainly there.
    if os.path.exists(payload.strip()):
        candidate = os.path.abspath(payload.strip())
        return (os.path.dirname(candidate) if os.path.isfile(candidate)
                else candidate)
    paths, current, depth = [], "", 0
    for ch in payload:
        if ch == "{":
            depth += 1
            if depth == 1:
                continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                paths.append(current)
                current = ""
                continue
        if ch == " " and depth == 0:
            if current:
                paths.append(current)
                current = ""
            continue
        current += ch
    if current:
        paths.append(current)

    paths = [p for p in paths if p]
    if not paths:
        raise DropError("nothing was dropped")
    first = paths[0]
    if not os.path.exists(first):
        raise DropError(f"{first} does not exist")
    if os.path.isfile(first):
        return os.path.dirname(os.path.abspath(first))
    return os.path.abspath(first)


class Index:
    """What is in one or more folders, read without extracting any document.

    It counts UNITS OF WORK, which is not the same as files on disk: an archive
    becomes every member the run will read. That costs an archive open per zip
    -- roughly 2.7 seconds on a 20,000-unit tree against 1.0 before -- and it
    buys the only property that matters here, which is that the number the
    window shows is the number the run does. The cheap version was faster and
    wrong: it reported 15,657 for a tree the run then took 20,148 units to
    finish, and the progress bar sailed past 100%.

    No document is extracted. Archive central directories are read; contents
    are not.
    """

    def __init__(self, folders):
        if isinstance(folders, str):
            folders = [folders]
        self.folders = list(folders)
        self.files = []
        self.skipped = []
        self.by_extension = {}
        self.total_bytes = 0
        self.per_folder = {}
        for folder in self.folders:
            before = len(self.files)
            self._walk(folder)
            self.per_folder[folder] = len(self.files) - before

    @property
    def folder(self):
        """The first folder, for callers that only ever pass one."""
        return self.folders[0] if self.folders else None

    def _walk(self, folder):
        # UNITS OF WORK, asked of the same walk the run uses. An archive is not
        # one unit: batch expands it into every member it will read, so counting
        # the container is how a progress bar reaches "35,600 of 23,700 files".
        # Measured before this was fixed: 2,210 counted against 3,919 walked on
        # one tree, 15,657 against 20,148 on another. Two rules that agree by
        # luck are the same bug the shared SKIP_EXT already fixed once.
        for label, _loader in batch._walk([folder]):
            self.files.append(label)
            member = label.split("!", 1)[-1]
            ext = os.path.splitext(member)[1].lower()
            self.by_extension[ext or "(none)"] = \
                self.by_extension.get(ext or "(none)", 0) + 1

        # A second, cheap pass for what the window SAYS about the folder: how
        # many bytes are there, and how much of it is not a document. The walk
        # cannot answer that -- it does not yield what it refuses.
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in sorted(names):
                path = os.path.join(root, name)
                if os.path.splitext(name)[1].lower() in _SKIP:
                    self.skipped.append(path)
                    continue
                try:
                    self.total_bytes += os.path.getsize(path)
                except OSError:
                    self.skipped.append(path)

    @property
    def count(self):
        return len(self.files)

    def summary(self):
        """One line for the window: what was found, in plain words."""
        where = ("" if len(self.folders) < 2
                 else f"{len(self.folders)} folders · ")
        if not self.files:
            return where + "no readable files"
        kinds = sorted(self.by_extension.items(), key=lambda kv: -kv[1])
        listed = ", ".join(f"{n} {e.lstrip('.') or 'no extension'}"
                           for e, n in kinds[:4])
        if len(kinds) > 4:
            listed += f", and {len(kinds) - 4} other kinds"
        note = f"  ({len(self.skipped)} not documents)" if self.skipped else ""
        return f"{where}{self.count} files · {listed}{note}"


class Options:
    """The choices on the window, with the defaults the tool recommends."""

    def __init__(self):
        self.recognize = True        # discover the fields from the documents
        self.datasheet = True        # record each file's state as found
        self.workspace = True        # keep originals and hashes
        self.include_text = True     # carry the full text into the CSV

    def as_dict(self):
        return {k: v for k, v in vars(self).items()}


class Result:
    """What a run produced, and where it put it."""

    def __init__(self, folder, out_dir):
        self.folder = folder
        self.out_dir = out_dir
        self.rows = []
        self.exceptions = []
        self.fields = []
        self.report_path = None
        self.datasheet_path = None
        self.csv_path = None
        self.workspace_path = None
        self.seconds = 0.0

    @property
    def read(self):
        return len(self.rows)

    @property
    def unreadable(self):
        return len(self.exceptions)

    def headline(self):
        if not self.rows and not self.exceptions:
            return "nothing to read in that folder"
        parts = [f"{self.read} read"]
        if self.exceptions:
            parts.append(f"{self.unreadable} could not be read")
        if self.fields:
            parts.append(f"{len(self.fields)} fields discovered")
        return " · ".join(parts) + " · " + self.duration()

    def duration(self):
        """How long it took, in units a person reads without converting.

        "0.0s" is what a fast run used to say, which reads like a failure.
        """
        if self.seconds < 1:
            return f"{self.seconds * 1000:.0f} ms"
        if self.seconds < 90:
            return f"{self.seconds:.1f} seconds"
        return f"{self.seconds / 60:.1f} minutes"


class Session:
    """One folder, its options, and the run that turns it into output."""

    def __init__(self, out_dir=None):
        self.folders = []
        self.index = None
        self.options = Options()
        self.out_dir = out_dir
        self.result = None

    @property
    def folder(self):
        """The first folder. Kept so single-folder callers read naturally."""
        return self.folders[0] if self.folders else None

    def load(self, folders, add=False):
        """Point the session at one or more folders. Returns the Index.

        `add` keeps what is already loaded, so several drops build one job --
        which is the difference between "open a folder" and "collect the work
        from four places and run it once".
        """
        if isinstance(folders, str):
            folders = [folders]
        incoming = []
        for folder in folders:
            if not os.path.isdir(folder):
                raise DropError(f"{folder} is not a folder")
            incoming.append(os.path.abspath(folder))
        kept = self.folders if add else []
        # A folder already covered by one being kept adds nothing but a second
        # pass over the same files.
        merged = list(kept)
        for folder in incoming:
            if folder in merged:
                continue
            if any(folder.startswith(k.rstrip(os.sep) + os.sep) for k in merged):
                continue
            merged = [k for k in merged
                      if not k.startswith(folder.rstrip(os.sep) + os.sep)]
            merged.append(folder)
        self.folders = merged
        self.index = Index(self.folders)
        self.result = None
        return self.index

    def output_dir(self):
        """Where results go: beside the folder, in a dated subdirectory.

        Never inside the source folder -- writing a report into the directory
        being read means the next run reads its own output.
        """
        if self.out_dir:
            return self.out_dir
        first = self.folders[0]
        parent = os.path.dirname(first.rstrip(os.sep)) or "."
        base = os.path.basename(first.rstrip(os.sep)) or "extract"
        if len(self.folders) > 1:
            base = f"{base}-and-{len(self.folders) - 1}-more"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return os.path.join(parent, f"{base}-extracted-{stamp}")

    def run(self, on_progress=None, on_stage=None, should_cancel=None):
        """Do the work. Returns a Result.

        `on_progress(done, total, name)` is called per document and
        `on_stage(text)` when the run moves between phases. Both are optional
        so this can be driven from a test with no window attached.

        `should_cancel()` is polled once per document; when it returns true the
        run stops at the next document boundary by raising Cancelled, which
        `batch.run` propagates (a callback that raises stops the batch). Partial
        output is left in the dated directory for the caller to discard.
        """
        if not self.folders:
            raise DropError("no folder loaded")
        started = time.time()
        out = self.output_dir()
        os.makedirs(out, exist_ok=True)
        result = Result(self.folder, out)
        total = max(self.index.count, 1)

        auto_labels = []
        if self.options.recognize:
            if on_stage:
                on_stage("reading a sample to discover the fields")
            auto_labels = self._discover(on_stage)
            result.fields = list(auto_labels)

        workspace = None
        if self.options.workspace:
            result.workspace_path = os.path.join(out, "workspace")
            workspace = Workspace(result.workspace_path)
            if not workspace.exists():
                workspace.create(__version__, list(self.folders))

        if on_stage:
            on_stage("extracting")
        done = [0]

        def progress(label, ok):
            if should_cancel and should_cancel():
                raise Cancelled()
            done[0] += 1
            if on_progress:
                on_progress(done[0], total, os.path.basename(str(label)))

        rows, exceptions, meta = batch.run(
            list(self.folders),
            auto_labels=auto_labels,
            include_text=self.options.include_text,
            workspace=workspace,
            collect_metadata=True,
            on_document=progress)
        result.rows, result.exceptions = rows, exceptions

        if on_stage:
            on_stage("writing the report")
        columns = ["file", "characters"] + list(auto_labels)
        if self.options.include_text:
            columns.append("text")
        result.report_path = os.path.join(out, "review.html")
        report.write_report(rows, exceptions, result.report_path, columns=columns)

        result.csv_path = os.path.join(out, "results.csv")
        batch.write_csv(rows, result.csv_path, columns=columns)

        if self.options.datasheet and meta:
            result.datasheet_path = os.path.join(out, "file-state.html")
            report.write_datasheet(meta, result.datasheet_path)

        if workspace is not None:
            workspace.add_revision(rows, columns, note="first extraction")

        result.seconds = time.time() - started
        self.result = result
        return result

    def _discover(self, on_stage=None):
        """Field labels shared across the corpus, from a sample of it.

        A sample, not the whole folder: discovery only needs enough documents
        to see which labels recur, and reading 4,000 invoices twice to find out
        that six labels are common is a minute of somebody's life for nothing.
        """
        sample = self.index.files[:60]
        texts = []
        for path in sample:
            try:
                with open(path, "rb") as fh:
                    data = fh.read(400_000)
                from ..dispatch import extract
                texts.append(extract(data, path))
            except Exception:             # noqa: BLE001
                continue                  # a document that will not read is
                                          # the batch's problem to report, not
                                          # discovery's problem to solve
        if not texts:
            return []
        return [f["label"] for f in recognize.infer_schema(texts)]
