#!/usr/bin/env python3
"""Check a BUILT DISTRIBUTION, not the working tree.

    python3 packaging/verify_dist.py --dist dist/
    python3 packaging/verify_dist.py --dist dist/ --corpus /path/to/real/documents

verify_e2e.py proves the claims the README makes about the source in this checkout.
This proves the claims a STRANGER depends on: that the wheel and the sdist on the index
install into an empty environment, pull nothing with them, put working commands on the
PATH, and carry no file that should never have left this machine.

Every check runs against the archive or against a throwaway virtual environment built
from it. Nothing here imports the package from the checkout, because the checkout is
what we are trying not to trust.

Needs Python 3.11.4+ and the `build` and `twine` modules for checks 6 and 7; the
packages being tested still need neither. 3.11 is the floor the package declares and
is where tomllib arrived; the .4 is tarfile's `filter="data"`, used on the sdist in
check 6, which reached the 3.11 series in 3.11.4 (measured 2026-09-22 -- it is the one
API in this repository that wants more than a bare 3.11.0).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Files that must never appear inside a distribution, whatever else changes.
FORBIDDEN_MEMBERS = (
    re.compile(r"(^|/)__pycache__/"),
    re.compile(r"\.pyc$"),
    re.compile(r"(^|/)docs/"),          # working notes: cite other machines, gitignored on purpose
    re.compile(r"(^|/)_backup_"),
    re.compile(r"(^|/)\.git"),
    re.compile(r"(^|/)out/"),
    re.compile(r"\.bak$"),
)

# Text that must never appear inside any shipped file. These rules name NOTHING:
# a check that spells out the private strings it hunts for publishes them itself,
# which is the bug commit 358e2d2 fixed in the push guard and which this list hit
# on its own first run. So they are structural -- the SHAPE of a machine path, of
# a home directory, of a token -- and the names live only in the push guard, as
# digests. These stand alone when the guard is not beside us (an unpacked sdist
# carries no .githooks/).
_REAL_HOME = os.path.expanduser("~").rstrip("/")
_REAL_USER = os.path.basename(_REAL_HOME)

# A drive letter and at least one path segment. The guard's own rule requires TWO
# segments, so this is the half it cannot see.
#
# The separator is ONE backslash, or two. One is the path as a document, a batch
# file or a log line writes it. Two is the same path inside a source string
# literal, where the language ate the first one. An earlier version spelled that
# separator with four backslashes inside a RAW bytes literal, which compiles to
# a requirement for two LITERAL backslashes -- so every real, unescaped path
# went straight through, and the escaped form that did match was then handed to
# the documentation exemption below and dropped there. Measured 2026-09-21: a
# one-segment and a two-segment raw drive path both read NO MATCH.
#
# A segment may not BEGIN with a space or a hyphen. That is what keeps ordinary
# prose off this rule: a bare drive root followed by English is a sentence, not
# a path, and once spaces are allowed INSIDE a segment (a Program Files path
# needs them) the leading one is the only thing that tells the two apart.
_DRIVE_SEP = rb"\\\\?"
_DRIVE_SEG = rb"[A-Za-z0-9_.~][A-Za-z0-9_.~ -]*"
_DRIVE_PATH = re.compile(
    rb"(?<![A-Za-z0-9_\\])[A-Za-z]:" + _DRIVE_SEP + _DRIVE_SEG
    + rb"(?:" + _DRIVE_SEP + _DRIVE_SEG + rb")*")

# Documentation shapes: a manual has to be able to show what a path looks like.
# A Windows profile directory is NOT one of them and must not go back on this
# list: a manual shows a path-to-thing placeholder, and anything under a profile
# directory names the account that owns it. That one word was exempting the only
# form of this leak the over-escaped pattern above could still see.
_DOC_DRIVE_PATH = re.compile(rb"(?i)^[a-z]:" + _DRIVE_SEP
                             + rb"(work|path|to|projects?|you|temp|tmp|some|"
                               rb"example|program files|my)\b")

# Accounts that are a manual's stand-in rather than a person. Deliberately
# three, and deliberately NOT the push guard's much longer generic-user set.
# The guard is a PUSH gate, where a build or a distribution account is more
# often a CI image than somebody's machine, and a warning a developer reads is
# a proportionate answer. This is a PUBLISH gate: a jenkins workspace or a
# clients directory under such an account still discloses the customer whose
# name is the next path segment, and nothing is recallable after an upload.
_DOC_ACCOUNTS = frozenset(("me", "you", "user"))

# The account in a path, for either spelling. Used only to decide whether a
# machine-identity finding is documentation.
_ACCOUNT_IN_PATH = re.compile(
    r"^/?(?:run/)?(?:media|home|Users)/(?P<who>[^/]*)"
    r"|^[A-Za-z]:\\+Users\\+(?P<who_win>[^\\]*)")

# The most of a single member that is read before the rules are applied to it.
# Anything past this is NAMED by a check rather than passed over in silence.
MAX_SCAN_BYTES = 8 * 1024 * 1024

FORBIDDEN_TEXT = (
    ("this machine's home directory",
     re.compile(re.escape(_REAL_HOME).encode() + rb"/|/home/" + re.escape(_REAL_USER).encode() + rb"/"),
     False),
    ("a GitHub token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"), True),
    ("a PyPI token", re.compile(rb"pypi-[A-Za-z0-9_-]{20,}"), True),
)

# Files a source distribution must carry so that whoever downloads it can re-run the
# claims instead of believing them.
SDIST_MUST_CARRY = ("tests/", "verify_e2e.py", "README.md", "LICENSE", "pyproject.toml",
                    "VALIDATION.md")

results = []
_t0 = time.time()


def check(ok, title, detail=""):
    results.append({"ok": bool(ok), "title": title, "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {title}")
    if detail:
        for line in str(detail).splitlines():
            print(f"         {line}")
    return bool(ok)


def heading(text):
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


# Filled in by main() before anything else runs. run() puts it in the BASE
# environment of every subprocess rather than leaving each caller to remember.
SANDBOX = {}


def run(argv, cwd=None, env=None, timeout=900, check_rc=False):
    """Run a command with a clean, explicit environment. Never inherits DISPLAY.

    HOME and the XDG directories come from SANDBOX and are part of the base. A
    subprocess given no HOME does not fail and does not complain:
    `os.path.expanduser("~")` falls back to the passwd database, so `~`
    resolves to the REAL account and every tool writes exactly where it always
    writes. Measured 2026-09-21: pip populated the real pip cache in the middle
    of a verification run whose whole purpose was to prove it had not.
    """
    base = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    base.update(SANDBOX)
    if env:
        base.update(env)
    p = subprocess.run(argv, cwd=cwd, env=base, capture_output=True, text=True,
                       timeout=timeout)
    if check_rc and p.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {p.returncode}\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
    return p


def redact_paths(text):
    """Keep this machine's own directories out of the evidence file.

    Everything printed here reaches a CI transcript and the --json evidence. A
    traceback names the file its frames were raised in, and the path to that
    file begins at somebody's home directory.
    """
    for real, stand_in in ((_REAL_HOME, "~"), (ROOT, "<project>")):
        if real and len(real) > 3:
            text = text.replace(real, stand_in)
    return text


def project_metadata():
    import tomllib
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        data = tomllib.load(fh)
    name = data["project"]["name"]
    scripts = sorted(data["project"].get("scripts", {}))
    version = data["project"].get("version")
    if version is None:                       # dynamic: read it from the package
        attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        mod, _, _var = attr.rpartition(".")
        src = open(os.path.join(ROOT, mod.replace(".", os.sep), "__init__.py"),
                   encoding="utf-8").read()
        version = re.search(r'__version__\s*=\s*"([^"]+)"', src).group(1)
    return name, version, scripts


def _zip_kind(info):
    """What a zip entry IS. A wheel can carry a symlink like any other zip."""
    if info.filename.endswith("/") or info.is_dir():
        return "directory"
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        return "a symbolic link"
    if mode and mode != 0o100000:
        return "not a regular file"
    return "file"


def archive_entries(path):
    """(name, kind) for EVERY member, including the ones no check should trust.

    members() and every_file() used to filter to regular files and directories
    and then say nothing about the rest, so a symlink, a hard link or a device
    node inside an sdist was examined by NO check: not the name rules, not the
    content rules, not the count. Whatever is in the archive is listed here,
    and unsafe_entries() is what refuses it.
    """
    if path.endswith(".whl"):
        with zipfile.ZipFile(path) as z:
            return [(i.filename, _zip_kind(i)) for i in z.infolist()]
    out = []
    with tarfile.open(path) as t:
        for m in t.getmembers():
            if m.isfile():
                kind = "file"
            elif m.isdir():
                kind = "directory"
            elif m.issym():
                kind = "a symbolic link to " + (m.linkname or "?")
            elif m.islnk():
                kind = "a hard link to " + (m.linkname or "?")
            elif m.ischr() or m.isblk():
                kind = "a device node"
            elif m.isfifo():
                kind = "a named pipe"
            else:
                kind = "not a regular file"
            out.append((m.name, kind))
    return out


def unsafe_entries(entries):
    """(name, why) for every member a distribution must not contain at all.

    Three ways a member is unsafe whatever is inside it. It is not a plain file
    or a directory -- a symlink or a hard link names something that is not in
    the archive, which is how a private key ships without a single byte of it
    being there to scan. It is absolute. Or it walks up out of the directory it
    unpacks into: a member named with a leading parent reference wrote outside
    the unpack directory on 2026-09-21 and nothing looked at it on the way
    past. Pure; the tests drive it.
    """
    bad = []
    for name, kind in entries:
        if kind not in ("file", "directory"):
            bad.append((name, kind))
            continue
        norm = name.replace("\\", "/")
        if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
            bad.append((name, "an absolute path"))
        elif ".." in norm.split("/"):
            bad.append((name, "escapes the directory it unpacks into"))
    return bad


def members(path):
    return [n for n, _kind in archive_entries(path)]


def read_member(path, name):
    if path.endswith(".whl"):
        with zipfile.ZipFile(path) as z:
            return z.read(name)
    with tarfile.open(path) as t:
        fh = t.extractfile(name)
        return fh.read() if fh else b""


def every_file(path, unreadable=None):
    """Yield (member name, bytes) for every regular file in the archive.

    A member this cannot hand back is appended to `unreadable` with the reason,
    because a member no check can read must still be NAMED by one. Silence is
    what let the rest of an archive ride along unexamined.
    """
    if path.endswith(".whl"):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                kind = _zip_kind(info)
                if kind == "directory":
                    continue
                if kind != "file":
                    if unreadable is not None:
                        unreadable.append((info.filename, kind))
                    continue
                try:
                    blob = z.read(info)
                except Exception as exc:
                    if unreadable is not None:
                        unreadable.append((info.filename, f"{type(exc).__name__}: {exc}"))
                    continue
                yield info.filename, blob
        return
    with tarfile.open(path) as t:
        for m in t.getmembers():
            if m.isdir():
                continue
            if not m.isfile():
                if unreadable is not None:
                    unreadable.append((m.name, "not a regular file"))
                continue
            try:
                fh = t.extractfile(m)
                blob = fh.read() if fh else b""
            except Exception as exc:
                if unreadable is not None:
                    unreadable.append((m.name, f"{type(exc).__name__}: {exc}"))
                continue
            yield m.name, blob


def redacted(text):
    """Enough to recognise the value, never enough to use it."""
    return text if len(text) <= 12 else text[:6] + "*" * 8 + text[-4:]


def forbidden_members(names):
    """Members that must never be in a distribution. Pure; the tests drive it."""
    return [n for n in names if any(p.search(n) for p in FORBIDDEN_MEMBERS)]


def load_push_guard():
    """The pre-push guard, imported as a module.

    It already decides what must never be published -- machine identity,
    credentials, private names -- and it carries the generic-user model and the
    per-file allowlist that stop a documentation example reading as a leak. Two
    definitions of 'unpublishable' would drift apart, so there is one, and this
    is it. Returns None when it is not beside us, e.g. inside an unpacked sdist.
    """
    import importlib.machinery
    import importlib.util
    path = os.path.join(ROOT, ".githooks", "pre-push")
    if not os.path.exists(path):
        return None
    loader = importlib.machinery.SourceFileLoader("push_guard", path)
    spec = importlib.util.spec_from_loader("push_guard", loader)
    mod = importlib.util.module_from_spec(spec)
    try:
        loader.exec_module(mod)
    except Exception:
        return None
    return mod if hasattr(mod, "scan_text") else None


# The THREE exact paths whose content IS the README, and nothing else. The rule
# used to be "any member basenamed METADATA or PKG-INFO, at any depth", which
# handed an allowlist entry written for README.md to files nobody had reviewed,
# and reported a finding against a file that did not contain the string.
_SDIST_TOP = re.compile(r"^[A-Za-z0-9_.-]+-\d[\w.]*$")
_GENERATED_METADATA = (
    re.compile(r"^PKG-INFO$"),
    re.compile(r"^[A-Za-z0-9_.-]+\.egg-info/PKG-INFO$"),
    re.compile(r"^[A-Za-z0-9_.-]+\.dist-info/METADATA$"),
)


def archive_relative(member):
    """The member name with an sdist's `<name>-<version>/` wrapper removed."""
    parts = member.split("/")
    if parts and _SDIST_TOP.match(parts[0]):
        parts = parts[1:]
    return "/".join(parts)


def _repo_relative(member):
    """Map an archive member onto the repository path the allowlist is keyed to.

    METADATA and PKG-INFO are not files anybody wrote: the packaging tools
    build them, and the long description inside them IS README.md. Judging them
    under their own names would re-flag every example the README is already
    allowed to contain -- three documentation paths, in this project's case --
    so they are judged as the file they were copied from.

    Only at the three paths where the tools actually generate them, though, and
    the finding is still REPORTED against the real member name. Which file a
    finding is IN and which file the allowlist judges it as are two questions,
    and one string was answering both.
    """
    rel = archive_relative(member)
    if any(p.match(rel) for p in _GENERATED_METADATA):
        return "README.md"
    return rel


def _scannable_views(blob, member, unreadable=None):
    """Every byte view of one file that a rule could match inside.

    The RAW BYTES always. A NUL byte says nothing about whether the rest of a
    file is readable, and the rule this replaces -- skip the file the moment
    one appears in the first 8 KiB, in both passes -- made one NUL enough to
    hide a file from every content rule at any size. Reproduced 2026-09-21: a
    token, a home path and a drive path shipped undetected inside a PNG, a
    UTF-16 .ini and a plain .txt with one stray NUL.

    Where there ARE NULs the file is additionally decoded as UTF-16 in both
    byte orders and re-encoded, because that is what a Windows text file is:
    its bytes interleave NULs and so match no ASCII pattern at all.
    """
    views = []
    if len(blob) > MAX_SCAN_BYTES:
        if unreadable is not None:
            unreadable.append((member, f"only the first {MAX_SCAN_BYTES} of "
                                       f"{len(blob)} bytes were scanned"))
        blob = blob[:MAX_SCAN_BYTES]
    views.append(blob)
    if b"\x00" in blob:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                views.append(blob.decode(enc, "replace").encode("utf-8", "replace"))
            except (UnicodeError, LookupError) as exc:
                if unreadable is not None:
                    unreadable.append((member, f"{enc}: {type(exc).__name__}"))
    return views


def _documentation_account(who):
    """True when the account named in a path is a stand-in, not a person."""
    who = (who or "").strip("\"'")
    if not who:
        return True
    if any(c in who for c in "$%*~"):            # a shell expansion or a glob
        return True
    if "..." in who or set(who) <= {".", "_", "-"}:   # a redaction
        return True
    return who.lower() in _DOC_ACCOUNTS


def blocks_publication(finding, guard):
    """Whether one push-guard finding is fatal to a RELEASE.

    The guard's own answer is its severity, and for machine identity that
    answer is tuned for a push: it downgrades to a warning whenever the account
    name is one of two dozen generic ones. That is the right model for a gate a
    developer reads and the wrong one for a gate that uploads. The account name
    is not the disclosure -- the path around it is, and the segments after a
    generic account name are as often a customer as anything else. So a
    machine-identity finding blocks a publication whatever the account is
    called, unless the account is one of the three the manuals actually use.
    """
    block = getattr(guard, "BLOCK", "BLOCK")
    if getattr(finding, "severity", block) == block:
        return True
    if getattr(finding, "category", "") != "machine identity":
        return False
    m = _ACCOUNT_IN_PATH.search(getattr(finding, "text", "") or "")
    who = (m.group("who") or m.group("who_win") or "") if m else ""
    return not _documentation_account(who)


def forbidden_text_hits(path, guard=None, unscanned=None):
    """(member, why, matched text) for every file carrying something unpublishable.

    Two sources, both applied. The push guard is the shared definition and
    brings the allowlist with it. The patterns above are this project's own
    last word. They also reach a shape the guard's rule does not: a drive
    letter followed by a single name, where the guard requires two path
    segments. A published file cannot be unpublished, so the last gate is the
    belt AND the braces.

    `unscanned`, if given, collects (member, why) for everything that could not
    be read in full, so that "every file read, nothing matched" is a statement
    a check can stand behind rather than a description of what was skipped.

    Note what this file must not do: name those strings in its own prose. The
    first version of this docstring spelled one out and the guard caught it in
    the built sdist -- a check that publishes what it looks for is the bug that
    commit 358e2d2 fixed in the push guard.
    """
    guard = guard if guard is not None else load_push_guard()
    hits, seen_pairs = [], set()

    def add(member, why, text):
        key = (member, text)
        if key not in seen_pairs:
            seen_pairs.add(key)
            hits.append((member, why, text))

    allow = []
    if guard is not None:
        allow_file = os.path.join(ROOT, ".githooks", "push-guard-allow.txt")
        if os.path.exists(allow_file):
            allow, _errors = guard.parse_allowlist(
                open(allow_file, encoding="utf-8").read(), allow_file)

    for member, blob in every_file(path, unreadable=unscanned):
        judged_as = _repo_relative(member)
        for view in _scannable_views(blob, member, unscanned):
            for what, pat, secret in FORBIDDEN_TEXT:
                m = pat.search(view)
                if m:
                    text = m.group(0).decode("utf-8", "replace")[:60]
                    # A credential must not be reprinted into a log, an evidence
                    # file or a CI transcript by the very check that found it.
                    add(member, what, redacted(text) if secret else text)
            for m in _DRIVE_PATH.finditer(view):
                found = m.group(0).rstrip(b" ")
                if _DOC_DRIVE_PATH.match(found):
                    continue                    # a manual showing a path's shape
                add(member, "an absolute drive-letter path",
                    found.decode("utf-8", "replace")[:60])
            if guard is None:
                continue
            out, seen = [], {}
            guard.scan_text(view.decode("utf-8", "replace"), judged_as,
                            "the built distribution", out, seen)
            for f in out:
                if not blocks_publication(f, guard):
                    continue
                if any(rule.matches(f) for rule in allow):
                    continue
                # Matched on the name the allowlist is keyed to, above.
                # Reported against the file it is actually in.
                f.path = member
                add(member, f.why, f.shown())
    return hits


def missing_from_sdist(names):
    """Which of the files a source distribution must carry are absent.

    Exact, never a substring. `want in name` said yes to a module called
    `latests.py` for `tests/`, so a distribution could satisfy every line of
    SDIST_MUST_CARRY and carry none of it.
    """
    rels = [r for r in (archive_relative(n) for n in names) if r]
    missing = []
    for want in SDIST_MUST_CARRY:
        if want.endswith("/"):
            ok = any(r == want.rstrip("/") or r.startswith(want) for r in rels)
        else:
            ok = any(r == want for r in rels)
        if not ok:
            missing.append(want)
    return missing


def licence_members(names):
    """Members that ARE a licence file, not members whose name ends in one.

    `n.endswith("LICENSE")` was satisfied by a file called NOTLICENSE.
    """
    out = []
    for n in names:
        base = n.rsplit("/", 1)[-1]
        if base in ("LICENSE", "LICENCE", "COPYING") or \
                base.startswith(("LICENSE.", "LICENCE.")):
            out.append(n)
    return out


def ungated_requirements(metadata_text):
    """Requires-Dist lines that are NOT behind an optional extra.

    `--no-index` proves that on this platform, today, nothing had to be
    fetched. It cannot prove what the metadata ASKS FOR. A requirement marked
    for one operating system installs nothing here and everything on a user's
    machine, so a project whose central claim is that it has no dependencies
    would certify clean while shipping one. Both checks stay: one is about the
    resolver, this one is about the promise.
    """
    out = []
    for line in metadata_text.splitlines():
        if not line.lower().startswith("requires-dist:"):
            continue
        body = line.split(":", 1)[1].strip()
        marker = body.split(";", 1)[1] if ";" in body else ""
        if re.search(r"extra\s*==", marker):
            continue
        out.append(body)
    return out


def normalised_name(text):
    """The form a distribution filename spells a project name in (PEP 503/427)."""
    return re.sub(r"[-_.]+", "_", text).lower()


def wheel_filename_version(basename):
    """The version a WHEEL FILENAME declares, or None if it is not a wheel name.

    PEP 427: name-version(-build)?-python-abi-platform.whl. Nothing checked
    this. Only METADATA was compared against the project's own version, so a
    wheel whose FILENAME named a different release carried METADATA that
    matched and passed every check -- and the filename is the only version a
    user sees on the index before the download.
    """
    if not basename.endswith(".whl"):
        return None
    parts = basename[:-len(".whl")].split("-")
    if len(parts) < 5:
        return None
    return parts[1]


def sdist_filename_version(basename):
    """The version an sdist FILENAME declares, or None if it is not one."""
    if not basename.endswith(".tar.gz"):
        return None
    head, _, ver = basename[:-len(".tar.gz")].rpartition("-")
    return ver if head else None


def select_artifacts(files, name, version):
    """The ONE wheel and the ONE sdist this release is made of.

    Returns (chosen, errors). Exact, and singular. The old rule took any
    filename CONTAINING the version and starting with the first six characters
    of the package name, then sorted the survivors and took the last: a
    four-component version and a post-release both answered to the release
    being verified, and one of them was picked in silence. Choosing between two
    candidates is not this program's decision to make, so two is an error and
    the caller must refuse.
    """
    want = normalised_name(name)
    hits = {"wheel": [], "sdist": []}
    for f in sorted(files):
        if f.endswith(".whl"):
            parts = f[:-len(".whl")].split("-")
            if len(parts) >= 5 and normalised_name(parts[0]) == want \
                    and parts[1] == version:
                hits["wheel"].append(f)
        elif f.endswith(".tar.gz"):
            head, _, ver = f[:-len(".tar.gz")].rpartition("-")
            if head and normalised_name(head) == want and ver == version:
                hits["sdist"].append(f)
    chosen, errors = {}, []
    for kind in ("wheel", "sdist"):
        if not hits[kind]:
            errors.append(f"no {kind} named for {name} {version}")
        elif len(hits[kind]) > 1:
            errors.append(f"{len(hits[kind])} files could be the {kind} for "
                          f"{name} {version}: " + ", ".join(hits[kind])
                          + " -- which one is the release?")
        else:
            chosen[kind] = hits[kind][0]
    return chosen, errors


# ---------------------------------------------------------------- check 1
def check_archive_hygiene(art, name, version):
    heading("CHECK 1: the archive carries what it should and nothing it should not")
    guard = load_push_guard()
    check(guard is not None,
          "the published-content rules come from the push guard, not a second copy",
          ".githooks/pre-push loaded; its allowlist applies, its generic-account "
          "downgrade does not -- see blocks_publication()"
          if guard else "guard not found beside this script -- using the built-in patterns")
    for path in (art["wheel"], art["sdist"]):
        base = os.path.basename(path)
        entries = archive_entries(path)
        names = [n for n, _kind in entries]
        bad = forbidden_members(names)
        check(not bad, f"{base}: no build litter, notes or backups",
              "found: " + ", ".join(bad[:8]) if bad else f"{len(names)} members, all expected")

        unsafe = unsafe_entries(entries)
        check(not unsafe,
              f"{base}: every member is a plain file or directory, inside the archive",
              "\n".join(f"{n}: {why}" for n, why in unsafe[:8]) if unsafe
              else f"{len(names)} members, no link, device node, absolute path or "
                   "parent reference")

        unscanned = []
        hits = forbidden_text_hits(path, guard, unscanned=unscanned)
        check(not hits, f"{base}: no machine path, home directory or credential in any file",
              "\n".join(f"{m}: {what} -- {text!r}" for m, what, text in hits[:8])
              if hits else f"{len(names)} members, read whole and scanned as bytes, "
                           "nothing matched")
        check(not unscanned, f"{base}: every member was read and scanned in full",
              "\n".join(f"{n}: {why}" for n, why in unscanned[:8]) if unscanned
              else "nothing was skipped, truncated or unreadable")

    # The version a user sees must be the version we think we built -- and
    # there are two of those. METADATA is what pip records once the thing is
    # installed. The FILENAME is what a person reads on the index BEFORE
    # downloading anything, and nothing here used to look at it at all.
    fn = wheel_filename_version(os.path.basename(art["wheel"]))
    check(fn == version, "the wheel's FILENAME names the version being released",
          f"the filename says {fn!r}, this build is {version}")
    fn = sdist_filename_version(os.path.basename(art["sdist"]))
    check(fn == version, "the sdist's FILENAME names the version being released",
          f"the filename says {fn!r}, this build is {version}")

    meta = read_member(
        art["wheel"], f"{name.replace('-', '_')}-{version}.dist-info/METADATA").decode()
    found = re.search(r"^Version: (.+)$", meta, re.M)
    declared = found.group(1).strip() if found else None
    check(declared == version, "the wheel's METADATA version is the version being released",
          f"METADATA says {declared}, this build is {version}")

    ungated = ungated_requirements(meta)
    check(not ungated, "the metadata requires no third-party package outside an extra",
          "\n".join(ungated[:8]) if ungated
          else "no Requires-Dist line that is not gated behind an extra")

    lic = licence_members(members(art["wheel"]))
    check(lic, "the wheel carries the licence file", ", ".join(lic) or "no LICENSE in the wheel")

    sdist_names = members(art["sdist"])
    missing = missing_from_sdist(sdist_names)
    check(not missing,
          "the sdist carries the tests and the claim checker, so the claims can be re-run",
          "missing: " + ", ".join(missing) if missing
          else "tests/, verify_e2e.py and the documentation are all present")

    wheel_tests = [n for n in members(art["wheel"])
                   if n == "tests" or n.startswith("tests/") or "/tests/" in n]
    check(not wheel_tests, "the wheel does NOT install the test suite into site-packages",
          ", ".join(wheel_tests[:5]) if wheel_tests else "no test files in the wheel")


# ---------------------------------------------------------------- check 2
def make_venv(where):
    run([sys.executable, "-m", "venv", where], check_rc=True, timeout=300)
    return os.path.join(where, "bin", "python")


def check_clean_install(art, name, version, work):
    """Install from the FILE with the index switched off, so a hidden dependency
    cannot be quietly satisfied from the network."""
    heading("CHECK 2: it installs into an empty environment and brings nothing with it")
    envs = {}
    for kind in ("wheel", "sdist"):
        vdir = os.path.join(work, f"venv-{kind}")
        py = make_venv(vdir)
        extra = {}
        if kind == "sdist":
            # A source install needs its build backend; that is a BUILD dependency,
            # so it is fetched deliberately and named in the output.
            extra = {"allow_index": True}
            p = run([py, "-m", "pip", "install", "--no-cache-dir", art[kind]], timeout=900)
        else:
            p = run([py, "-m", "pip", "install", "--no-index", "--no-cache-dir", art[kind]],
                    timeout=900)
        ok = p.returncode == 0
        check(ok, f"the {kind} installs into a fresh virtual environment"
                  + ("" if kind == "sdist" else " with --no-index"),
              (p.stdout + p.stderr)[-500:] if not ok else
              f"installed {name} {version}" + (" (build backend fetched, as expected)" if extra else ""))
        if not ok:
            continue
        envs[kind] = py

        listing = run([py, "-m", "pip", "list", "--format=json"], timeout=300)
        installed = {d["name"].lower().replace("_", "-"): d["version"]
                     for d in json.loads(listing.stdout or "[]")}
        allowed = {"pip", "setuptools", "wheel", name.lower().replace("_", "-")}
        extras = sorted(set(installed) - allowed)
        check(not extras, f"the {kind} pulled in no third-party package",
              "unexpected: " + ", ".join(extras) if extras
              else "environment holds " + ", ".join(sorted(installed)))

        check(installed.get(name.lower().replace("_", "-")) == version,
              f"the {kind} reports the right version once installed",
              f"pip says {installed.get(name.lower().replace('-', '-'))}, expected {version}")
    return envs


# ---------------------------------------------------------------- check 3
def check_console_scripts(py, scripts, version, work):
    heading("CHECK 3: every command the package advertises exists and answers")
    bindir = os.path.join(os.path.dirname(py))
    sandbox = os.path.join(work, "home-scripts")
    os.makedirs(sandbox, exist_ok=True)
    env = {"HOME": sandbox}
    for script in scripts:
        exe = os.path.join(bindir, script)
        if not check(os.path.exists(exe), f"{script}: installed on the PATH", exe):
            continue
        p = run([exe, "--version"], env=env, timeout=120)
        gui = "gui" in script
        if gui:
            # No PySide6 here on purpose: the window is an extra. Failing is correct;
            # failing with a traceback at the user is not.
            trace = "Traceback (most recent call last)" in (p.stderr + p.stdout)
            check(not trace, f"{script}: without the GUI extra, says so instead of a traceback",
                  (p.stderr or p.stdout).strip().splitlines()[-1][:140] if (p.stderr or p.stdout)
                  else f"exit {p.returncode}, no output")
        else:
            out = (p.stdout + p.stderr).strip()
            check(p.returncode == 0 and version in out,
                  f"{script} --version prints {version}", out[:140] or f"exit {p.returncode}")
            h = run([exe, "--help"], env=env, timeout=120)
            check(h.returncode == 0, f"{script} --help exits 0", f"exit {h.returncode}")


# ---------------------------------------------------------------- check 4
XXE_DOCUMENT = """<?xml version="1.0"?>
<!DOCTYPE root [<!ENTITY stolen SYSTEM "file:///etc/hostname">]>
<root><p>&stolen;</p><p>MARKER-XXE-BODY</p></root>
"""

INVOICE_TEXT = """ACME SUPPLY CO
Invoice Number: INV-90699
Total: $6,107.76
MARKER-INVOICE
"""

NOTE_HTML = """<html><body><h1>Meeting note</h1><p>MARKER-HTML</p></body></html>
"""


def check_real_work(py, work, version):
    heading("CHECK 4: the installed command does real work on documents it has never seen")
    bindir = os.path.dirname(py)
    sandbox = os.path.join(work, "home-work")
    os.makedirs(sandbox, exist_ok=True)
    env = {"HOME": sandbox}
    vanilla = os.path.join(bindir, "vanilla")

    docs = os.path.join(work, "fixture-docs")
    os.makedirs(docs, exist_ok=True)
    open(os.path.join(docs, "invoice.txt"), "w").write(INVOICE_TEXT)
    open(os.path.join(docs, "note.html"), "w").write(NOTE_HTML)
    open(os.path.join(docs, "hostile.xml"), "w").write(XXE_DOCUMENT)

    p = run([vanilla, os.path.join(docs, "invoice.txt")], cwd=work, env=env, timeout=300)
    check(p.returncode == 0 and "MARKER-INVOICE" in p.stdout,
          "extracts the text of a document and prints it",
          (p.stdout + p.stderr).strip()[-200:])

    out_csv = os.path.join(work, "batch.csv")
    p = run([vanilla, "--batch", docs, "--csv", out_csv,
             "--field", r"invoice_no=Invoice\s*Number\s*:?\s*([A-Z]+-[0-9]+)"],
            cwd=work, env=env, timeout=600)
    ok = p.returncode == 0 and os.path.exists(out_csv)
    body = open(out_csv).read() if os.path.exists(out_csv) else ""
    check(ok, "a batch over a folder writes the CSV it was asked for",
          f"{len(body.splitlines())} lines" if ok else (p.stdout + p.stderr)[-300:])
    check("INV-90699" in body, "the named field is pulled out of the document",
          "found INV-90699 in the CSV" if "INV-90699" in body else body[:200])

    # Hostile input, re-tested against the INSTALLED package: an external entity
    # must not be resolved. The claim is the library's, not this checkout's.
    try:
        host = open("/etc/hostname").read().strip()
    except OSError:
        host = ""
    p = run([vanilla, os.path.join(docs, "hostile.xml")], cwd=work, env=env, timeout=300)
    leaked = bool(host) and host in (p.stdout + p.stderr)
    check(not leaked, "an XML external entity does not read a file off the host",
          "THE HOST FILE WAS READ" if leaked else "no host file content in the output")


# ---------------------------------------------------------------- check 5
# Paths this product writes to when it is allowed to. Hashed by CONTENT, so a
# rewrite that keeps the size and restores the timestamp is still caught.
SHELF_PATHS = ("~/.local/share/vanilla-extract", "~/.vanilla-extract")

# Where an install or a run of a Python package actually writes, outside its
# own sandbox. These are fingerprinted; the rest of the home directory is not.
#
# The first version of this check fingerprinted the WHOLE home minus a list of
# declared-volatile paths, and it went red on its first independent run for
# sixteen files under a scratch directory another process on this machine
# created and deleted while the run was in flight -- nothing to do with the
# distribution. Measured 2026-09-21. A gate that fails for reasons unrelated to
# what it gates gets switched off, and then it guards nothing; a narrower check
# that is always true when it is green is worth more than a broad one that is
# not. The exemption list also had to grow every time a desktop program was
# opened, which is a list nobody can finish.
#
# What is kept is what the failures this check exists to catch actually touched:
# pip wrote into the real pip cache when HOME was not sandboxed (measured
# 2026-09-21), and a one-line `--version` check once registered a folder on the
# real shelf (2026-09-19). Both land here.
WATCHED_HOME = (
    "~/.cache/pip",                     # where an unsandboxed pip install lands
    "~/.local/bin",                     # where a console script would be written
    "~/.local/lib",                     # where a --user install would land
    "~/.pypirc",                        # a publishing tool must not touch this
    "~/.netrc",
)


def watched_fingerprint():
    """Mode, size and modification time of every entry under the watched paths.

    Metadata rather than content: a write that leaves mode, size and
    modification time all identical is not one anything here makes by accident.
    Returns (manifest, roots_present).
    """
    seen, present = {}, []
    for spec in WATCHED_HOME:
        root = os.path.expanduser(spec)
        if not os.path.exists(root):
            seen[spec] = "absent"
            continue
        present.append(spec)
        if os.path.isfile(root):
            try:
                st = os.lstat(root)
                seen[spec] = f"{st.st_mode}:{st.st_size}:{st.st_mtime_ns}"
            except OSError as exc:
                seen[spec] = f"unreadable:{exc.errno}"
            continue
        for base, dirs, files in os.walk(root, followlinks=False,
                                         onerror=lambda e: None):
            dirs.sort()
            for nm in sorted(files) + dirs:
                path = os.path.join(base, nm)
                rel = spec + "/" + os.path.relpath(path, root)
                try:
                    st = os.lstat(path)
                except OSError as exc:
                    seen[rel] = f"unreadable:{exc.errno}"
                    continue
                seen[rel] = ("dir:%s" % st.st_mode if nm in dirs
                             else f"{st.st_mode}:{st.st_size}:{st.st_mtime_ns}")
    return seen, present


def shelf_fingerprint():
    """Hash what this product writes, so a test run that touched it is visible."""
    out = {}
    for spec in SHELF_PATHS:
        path = os.path.expanduser(spec)
        try:
            if os.path.isdir(path):
                h = hashlib.sha256()
                for base, dirs, files in os.walk(path):
                    dirs.sort()
                    for nm in sorted(files):
                        p = os.path.join(base, nm)
                        h.update(os.path.relpath(p, path).encode("utf-8", "replace"))
                        with open(p, "rb") as fh:
                            for chunk in iter(lambda: fh.read(1 << 20), b""):
                                h.update(chunk)
                out[spec] = h.hexdigest()
            else:
                with open(path, "rb") as fh:
                    out[spec] = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            out[spec] = "absent"
    return out


def check_shelf_untouched(before, before_home=None):
    heading("CHECK 5: nothing written outside the sandbox")
    after = shelf_fingerprint()
    changed = [p for p in before if before[p] != after.get(p)]
    check(not changed, "this product's own state on disk is what it was before this run",
          "CHANGED: " + ", ".join(changed) if changed
          else "\n".join(f"{p}: {v[:12]}" for p, v in before.items()))

    if before_home is None:
        check(False, "the places an install or a run writes were sampled before the run",
              "no `before` sample was taken, so nothing can be compared")
        return
    was, present = before_home
    now, _present2 = watched_fingerprint()
    added = sorted(set(now) - set(was))
    removed = sorted(set(was) - set(now))
    modified = sorted(p for p in was if p in now and was[p] != now[p])
    total = len(added) + len(removed) + len(modified)
    listing = "\n".join(["  + " + p for p in added[:8]]
                        + ["  - " + p for p in removed[:8]]
                        + ["  ~ " + p for p in modified[:8]])
    watched = ", ".join(WATCHED_HOME)
    check(not total,
          "nothing was written where an install or a run of this package would write",
          (f"{len(now)} entries under {len(present)} existing watched paths, all "
           f"identical.\n  watched: {watched}")
          if not total else
          f"{len(added)} added, {len(removed)} removed, {len(modified)} changed\n"
          + listing + f"\n  watched: {watched}")


# ---------------------------------------------------------------- check 6
def check_sdist_rebuilds(art, work, name, version):
    heading("CHECK 6: the sdist is self-sufficient -- a wheel built FROM it matches")
    unpack = os.path.join(work, "sdist-unpacked")
    os.makedirs(unpack, exist_ok=True)
    with tarfile.open(art["sdist"]) as t:
        # filter="data" on the one archive this check exists to DISTRUST.
        # Without it extractall honours absolute member names, parent
        # references and links: a member named with a leading parent
        # reference wrote OUTSIDE the unpack directory on 2026-09-21.
        t.extractall(unpack, filter="data")
    top = os.path.join(unpack, f"{name.replace('-', '_')}-{version}")
    if not os.path.isdir(top):
        top = os.path.join(unpack, os.listdir(unpack)[0])
    outdir = os.path.join(work, "rebuilt")
    p = run([sys.executable, "-m", "build", "--wheel", "--outdir", outdir], cwd=top, timeout=900)
    if not check(p.returncode == 0, "a wheel builds from the unpacked sdist alone",
                 (p.stdout + p.stderr)[-400:] if p.returncode else "built"):
        return top
    rebuilt = [f for f in os.listdir(outdir) if f.endswith(".whl")]
    original = {n for n in members(art["wheel"]) if not n.endswith(("RECORD", "METADATA"))}
    remade = {n for n in members(os.path.join(outdir, rebuilt[0]))
              if not n.endswith(("RECORD", "METADATA"))}
    check(original == remade, "it contains exactly the files the released wheel contains",
          "only in released: " + ", ".join(sorted(original - remade)[:5]) +
          " | only in rebuilt: " + ", ".join(sorted(remade - original)[:5])
          if original != remade else f"{len(original)} members, identical")
    return top


# ---------------------------------------------------------------- check 7
def check_twine(art):
    heading("CHECK 7: the index will accept it and render the README")
    p = run([sys.executable, "-m", "twine", "check", art["wheel"], art["sdist"]], timeout=300)
    check(p.returncode == 0, "twine check passes on both files",
          (p.stdout + p.stderr).strip()[-400:])


# ---------------------------------------------------------------- check 8
def check_claims_from_sdist(py, top, work):
    heading("CHECK 8: the claims re-run from what a stranger downloads")
    if not os.path.isdir(os.path.join(top, "tests")):
        check(False, "the sdist contains a test suite to run", "no tests/ directory in the sdist")
        return
    sandbox = os.path.join(work, "home-claims")
    os.makedirs(sandbox, exist_ok=True)
    env = {"HOME": sandbox, "CODECOBBLER_HOME": sandbox,
           "PATH": os.path.dirname(py) + ":/usr/bin:/bin"}
    p = run([py, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=top, env=env, timeout=1800)
    tail = (p.stderr or p.stdout).strip().splitlines()
    check(p.returncode == 0, "the test suite in the sdist passes against the installed package",
          " / ".join(tail[-2:])[:200])
    if os.path.exists(os.path.join(top, "verify_e2e.py")):
        p = run([py, "verify_e2e.py"], cwd=top, env=env, timeout=1800)
        line = [l for l in (p.stdout or "").splitlines() if "claims verified" in l]
        check(p.returncode == 0, "the claim checker in the sdist passes",
              (line[-1].strip() if line else (p.stdout + p.stderr)[-300:]))
    else:
        check(False, "the sdist carries verify_e2e.py so the claims travel with the code",
              "not present -- MANIFEST.in does not include it")


# ---------------------------------------------------------------- optional corpus
CORPUS_SUFFIXES = (".pdf", ".docx", ".pptx", ".xlsx", ".odt", ".rtf", ".md",
                   ".html", ".htm", ".txt", ".csv", ".eml", ".msg", ".xml")


def corpus_documents(corpus, limit):
    """Real documents from a real tree. Named by extension, never by where they came
    from: this file ships in a public repository and the corpus does not."""
    found = []
    for base, dirs, files in os.walk(corpus):
        dirs[:] = [d for d in dirs
                   if d not in (".claude", "site-packages", ".git", "node_modules")]
        if base.count(os.sep) - corpus.rstrip(os.sep).count(os.sep) > 2:
            dirs[:] = []
            continue
        for f in sorted(files):
            if f.lower().endswith(CORPUS_SUFFIXES):
                found.append(os.path.join(base, f))
                if len(found) >= limit:
                    return found
    return found


def stage_corpus(docs, dest):
    """A folder of links to the documents, because --batch takes a FOLDER.

    Why this exists, measured against the installed 0.2.0 wheel on 2026-09-21:
    `--csv` and `--exceptions` are batch-only. Both the README option table and
    `vanilla --help` say "with --batch:" for each of them, and outside --batch
    they are not read at all -- the run prints the extracted text, writes
    neither file, exits 0, and says nothing on stderr about the flags it
    ignored. So handing the tool a list of file paths and a `--csv` cannot
    produce a CSV, and a corpus check built that way is measuring nothing.
    The tool has to be given a folder.

    The corpus is READ-ONLY and is never written to: what is created here are
    links INSIDE the throwaway work directory that point out at it. Each link
    carries an index prefix because two documents in different corpus
    subdirectories may share a basename, and a silent collision would shrink
    the denominator every check below is measured against.

    Returns (staged paths, documents that could not be staged).
    """
    os.makedirs(dest, exist_ok=True)
    staged, failed = [], []
    for i, src in enumerate(docs):
        link = os.path.join(dest, f"{i:05d}_{os.path.basename(src)}")
        try:
            os.symlink(src, link)                     # measured: --batch walks these
        except OSError:
            try:
                shutil.copy2(src, link)               # a filesystem without links
            except OSError:
                failed.append(src)
                continue
        staged.append(link)
    return staged, failed


def csv_records(path):
    """The data rows of a CSV, header excluded. [] for a file that is empty.

    Counted with a CSV reader and never with line counts: the results table
    carries each document's full text in a quoted column, so one record spans
    many lines. Measured 2026-09-21: a 394-record corpus CSV is 114,822 lines
    long, so a line count would overstate the result by 290x.
    """
    import csv as _csv
    try:                                              # one document's text can
        _csv.field_size_limit(sys.maxsize)            # exceed the 128K default
    except (OverflowError, ValueError):
        pass
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        rows = list(_csv.reader(fh))
    return rows[1:] if rows else []


def check_corpus(py, corpus, work, max_files):
    heading(f"MEASURED: a real document set ({os.path.basename(corpus.rstrip('/'))})")
    bindir = os.path.dirname(py)
    sandbox = os.path.join(work, "home-corpus")
    os.makedirs(sandbox, exist_ok=True)
    env = {"HOME": sandbox}
    docs = corpus_documents(corpus, max_files)
    kinds = {}
    for d in docs:
        kinds[os.path.splitext(d)[1].lower()] = kinds.get(os.path.splitext(d)[1].lower(), 0) + 1
    total_bytes = sum(os.path.getsize(d) for d in docs if os.path.exists(d))
    if not check(docs, "the corpus holds documents this tool claims to read",
                 ", ".join(f"{n} {ext}" for ext, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
                 + f"  ({total_bytes/1e6:.0f} MB)"):
        return False

    stage = os.path.join(work, "corpus-stage")
    staged, unstageable = stage_corpus(docs, stage)
    if not check(staged and not unstageable,
                 "every corpus document is staged for the batch walk",
                 f"{len(staged)} staged" if not unstageable
                 else f"{len(staged)} staged, {len(unstageable)} could not be: "
                      + ", ".join(os.path.basename(u) for u in unstageable[:5])):
        return False
    expected = len(staged)

    out_csv = os.path.join(work, "corpus.csv")
    out_exc = os.path.join(work, "corpus-skipped.csv")
    t0 = time.time()
    p = run([os.path.join(bindir, "vanilla"), "--batch", stage,
             "--csv", out_csv, "--exceptions", out_exc, "--quiet"],
            cwd=work, env=env, timeout=3600)
    took = time.time() - t0

    # Exit 0, exactly -- not "0 or 1". The README's exit table says --batch is
    # different on purpose: an unreadable document is A ROW IN THE EXCEPTIONS
    # TABLE, "a reported result rather than a failed run, and it exits 0". A
    # completed batch therefore has one correct status and 1 means the run did
    # not do what was asked. The rule here used to accept `in (0, 1)`, which is
    # how a 135s run that exited 1 and wrote no file at all passed this whole
    # section on 2026-09-21. Measured over this corpus the same day: exit 0.
    ok = check(p.returncode == 0, "the installed command survives a real document set",
               f"exit {p.returncode} in {took:.0f}s"
               + (("\n" + (p.stdout + p.stderr)[-600:]) if p.returncode else ""))

    # No `if os.path.exists(...)` guarding what follows. A missing output IS the
    # failure this section exists to catch, and a check that only runs when the
    # file is there can never report its absence -- it launders "nothing
    # happened" into a PASS.
    have_csv = check(os.path.exists(out_csv),
                     "the batch wrote the results table it was asked for",
                     f"{os.path.getsize(out_csv)/1e6:.1f} MB" if os.path.exists(out_csv)
                     else f"NOTHING AT {out_csv}\n" + (p.stdout + p.stderr)[-400:])
    have_exc = check(os.path.exists(out_exc),
                     "the batch wrote the exceptions table it was asked for",
                     out_exc if os.path.exists(out_exc)
                     else f"NOTHING AT {out_exc}\n" + (p.stdout + p.stderr)[-400:])
    if not (have_csv and have_exc):
        return False

    extracted = csv_records(out_csv)
    skipped = csv_records(out_exc)
    accounted = len(extracted) + len(skipped)
    check(accounted == expected,
          "every staged document is accounted for: extracted, or named as an exception",
          f"{len(extracted)} extracted + {len(skipped)} skipped = {accounted}, "
          f"staged {expected}")

    check(len(extracted) > 0, "text came out",
          f"{len(extracted)} documents extracted in {took:.0f}s "
          f"({total_bytes/1e6/max(took, 1):.1f} MB/s), "
          f"{os.path.getsize(out_csv)/1e6:.1f} MB of results")

    # The exceptions table earns its place only if each row says WHY. Measured
    # reason codes over this corpus: encrypted, no_text_found, undecodable_fonts.
    reasons = {}
    unexplained = 0
    for r in skipped:
        code = r[1].strip() if len(r) > 1 and r[1].strip() else ""
        if not code:
            unexplained += 1
            continue
        reasons[code] = reasons.get(code, 0) + 1
    check(not unexplained, "every document that did not extract said why, by reason code",
          (f"{unexplained} of {len(skipped)} exception rows carry no reason code\n"
           if unexplained else "")
          + ("\n".join(f"{n:4d}  {why}" for why, n in
                       sorted(reasons.items(), key=lambda kv: -kv[1])[:10])
             or "nothing was skipped"))
    return ok


def attempt(what, fn, *a, **kw):
    """Run one section. A crash is a FAILED CHECK, never a silent exit.

    A post-release wheel used to be selected by a substring match and then die
    on a KeyError. Nothing caught it, and because the evidence file was written
    only at the end, the run left no evidence file and no RESULT section at all
    -- the one shape of failure that looks exactly like nothing having happened.
    """
    try:
        return fn(*a, **kw)
    except Exception:
        check(False, f"{what} ran to completion without an error",
              redact_paths(traceback.format_exc(limit=4).strip())[-900:])
        return None


def finish(args, name, version, art):
    """The RESULT section and the evidence file, whatever happened above."""
    heading("RESULT")
    failed = [r for r in results if not r["ok"]]
    print(f"  {len(results) - len(failed)}/{len(results)} checks passed "
          f"in {time.time() - _t0:.0f}s")
    if failed:
        print(f"\n  {len(failed)} FAILED:")
        for r in failed:
            print(f"    - {r['title']}")
    print()
    verified = bool(results) and not failed
    print("  DISTRIBUTION VERIFIED" if verified else "  DISTRIBUTION NOT FIT TO PUBLISH")
    if args.json:
        try:
            with open(args.json, "w") as fh:
                json.dump({"package": name, "version": version,
                           "artifacts": {k: os.path.basename(v) for k, v in art.items()},
                           "passed": len(results) - len(failed), "total": len(results),
                           "results": results}, fh, indent=2)
            print(f"  evidence: {args.json}")
        except OSError as exc:
            print(f"  evidence NOT written to {args.json}: {exc}")
    return 0 if verified else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"),
                    help="directory holding the built wheel and sdist")
    ap.add_argument("--corpus", help="optional: a real tree of documents to extract as a stress test")
    ap.add_argument("--max-files", type=int, default=400,
                    help="how many documents from the corpus to put through")
    ap.add_argument("--json", help="write the results to this path")
    ap.add_argument("--keep", action="store_true", help="keep the temporary venvs")
    args = ap.parse_args()


    name, version, scripts = project_metadata()
    print("=" * 72)
    print(f"VERIFYING THE DISTRIBUTION: {name} {version}")
    print("=" * 72)

    try:
        files = os.listdir(args.dist)
    except OSError as exc:
        files = []
        check(False, "the directory of built files can be read", str(exc))
    chosen, errors = select_artifacts(files, name, version)
    art = {k: os.path.join(args.dist, v) for k, v in chosen.items()}
    if errors:
        # Refuse. Picking one of two candidates, or carrying on without one, is
        # how a build nobody meant to publish gets certified.
        check(False, "exactly one wheel and one sdist are named for this release",
              "\n".join(errors))
        return finish(args, name, version, art)

    for kind in ("wheel", "sdist"):
        blob = open(art[kind], "rb").read()
        print(f"  {os.path.basename(art[kind])}")
        print(f"    {len(blob)/1024:.0f} KB  sha256 {hashlib.sha256(blob).hexdigest()}")

    before_shelf = shelf_fingerprint()
    before_home = watched_fingerprint()
    work = tempfile.mkdtemp(prefix="verify-dist-")
    # ONE sandbox, made before anything runs and handed to every subprocess
    # through run()'s base environment.
    sandbox = os.path.join(work, "home")
    for sub in ("", "cache", "share", "config", "state"):
        os.makedirs(os.path.join(sandbox, sub), exist_ok=True)
    SANDBOX.update({"HOME": sandbox,
                    "XDG_CACHE_HOME": os.path.join(sandbox, "cache"),
                    "XDG_DATA_HOME": os.path.join(sandbox, "share"),
                    "XDG_CONFIG_HOME": os.path.join(sandbox, "config"),
                    "XDG_STATE_HOME": os.path.join(sandbox, "state")})
    try:
        attempt("the archive check", check_archive_hygiene, art, name, version)
        envs = attempt("the clean-install check", check_clean_install,
                       art, name, version, work) or {}
        py = envs.get("wheel") or envs.get("sdist")
        if py:
            attempt("the console-script check", check_console_scripts,
                    py, scripts, version, work)
            attempt("the real-work check", check_real_work, py, work, version)
        top = attempt("the sdist-rebuild check", check_sdist_rebuilds,
                      art, work, name, version)
        attempt("the index check", check_twine, art)
        if py and top:
            attempt("the claims-from-the-sdist check", check_claims_from_sdist,
                    py, top, work)
        if args.corpus and py:
            attempt("the corpus check", check_corpus, py, args.corpus, work,
                    args.max_files)
        # LAST. It used to run before the three longest checks, so whatever
        # they wrote outside the sandbox was never compared against anything.
        attempt("the sandbox check", check_shelf_untouched, before_shelf, before_home)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"\n  kept: {work}")
        code = finish(args, name, version, art)
    return code


if __name__ == "__main__":
    sys.exit(main())
