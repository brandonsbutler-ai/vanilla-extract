"""Filesystem state of a document as found: the chain-of-custody half.

A content hash proves the bytes did not change. This records the circumstances
they were found in -- size, timestamps, permissions, ownership, link status --
which is the other half of "what was this file when it arrived".

Three things make this harder than calling os.stat, and getting them wrong
produces a datasheet that looks authoritative and is not:

1. "DATE CREATED" DOES NOT EXIST EVERYWHERE. Windows puts creation time in
   st_ctime; macOS and the BSDs expose st_birthtime; Linux has neither through
   os.stat -- st_ctime there is the INODE CHANGE time, which is a different
   fact and is usually later than creation. So creation is reported only when
   the platform actually supplies it, every record says where the value came
   from, and Linux gets a best-effort read from `stat --format=%W` rather than
   a relabelled st_ctime.

2. MOUNTED FOREIGN FILESYSTEMS SYNTHESIZE OWNERSHIP AND MODE. An NTFS or
   exFAT volume mounted on Linux commonly reports one uid and 0777 for every
   file, because those come from the mount options, not the files. Reporting
   them as the file's permissions is false. Such records are flagged.

3. ARCHIVE MEMBERS HAVE THEIR OWN METADATA and it is not the archive's. A ZIP
   entry carries its own timestamp, and often the original Unix mode, in the
   central directory. Those are read from the entry.

Standard library only: os, stat, and pwd/grp where they exist.
"""

import datetime
import os
import platform
import stat as statmod
import subprocess

try:                                   # Unix only
    import grp
    import pwd
except ImportError:                    # pragma: no cover - Windows
    grp = pwd = None

SYSTEM = platform.system()

# Filesystems that commonly report ownership and mode from mount options
# rather than from the files themselves.
_SYNTHETIC_FS = {
    "fuseblk", "ntfs", "ntfs3", "exfat", "vfat", "msdos", "cifs", "smb2",
    "smbfs", "9p", "iso9660", "udf", "hfs", "fat", "fat32",
}

_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(n):
    """Byte count as a short human string. 1 KB is 1024 B here."""
    value = float(n)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _iso(epoch):
    if epoch is None:
        return None
    return (datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))


# `autofs` is an automount trigger, not the filesystem that ends up mounted
# there; it must never be reported as the answer.
_PASSTHROUGH_FS = {"autofs"}


def _mount_table():
    """Mount point -> filesystem type, longest path first.

    Two subtleties, both of which produced a wrong answer before they were
    handled. A mount point can appear MORE THAN ONCE in /proc/mounts, and the
    later entry shadows the earlier -- a systemd autofs trigger and the real
    fuseblk volume both claim the same directory, and taking the first gave
    "autofs", which in turn made an NTFS volume look like it had trustworthy
    Unix ownership. And octal escapes appear in paths containing spaces.
    """
    seen = {}
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                point = (parts[1].replace("\\040", " ").replace("\\011", "\t")
                         .replace("\\012", "\n").replace("\\134", "\\"))
                fstype = parts[2]
                if fstype in _PASSTHROUGH_FS and point in seen:
                    continue           # never let a trigger shadow a real fs
                if fstype in _PASSTHROUGH_FS and point not in seen:
                    seen[point] = fstype     # provisional; a real fs may replace it
                    continue
                seen[point] = fstype         # last real mount wins
    except OSError:
        return []
    return sorted(seen.items(), key=lambda t: len(t[0]), reverse=True)


_MOUNTS = _mount_table()


_FS_CACHE = {}


def filesystem_of(path):
    """Filesystem type for a path, or None when it cannot be determined.

    Cached per directory: a batch of 10,000 files in one folder should not walk
    the mount table 10,000 times.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    key = os.path.dirname(real)
    if key in _FS_CACHE:
        return _FS_CACHE[key]
    answer = None
    for mount, fstype in _MOUNTS:
        if real == mount or real.startswith(mount.rstrip("/") + "/"):
            answer = fstype
            break
    _FS_CACHE[key] = answer
    return answer


def _birthtime(path, st):
    """(epoch, source) for creation time, or (None, reason) when unavailable."""
    if hasattr(st, "st_birthtime"):
        return st.st_birthtime, "st_birthtime"
    if SYSTEM == "Windows":
        return st.st_ctime, "st_ctime (creation on Windows)"
    # Linux: os.stat cannot reach it. statx can, on filesystems that store it.
    try:
        out = subprocess.run(["stat", "--format=%W", path],
                             capture_output=True, timeout=5, text=True)
        raw = out.stdout.strip()
        if out.returncode == 0 and raw.isdigit() and int(raw) > 0:
            return int(raw), "statx birth time via stat(1)"
    except (OSError, subprocess.SubprocessError):
        pass
    return None, "unavailable on this platform and filesystem"


def _owner(st, synthetic):
    """(uid, name, gid, group) with names resolved where possible."""
    uid = getattr(st, "st_uid", None)
    gid = getattr(st, "st_gid", None)
    name = group = None
    if pwd is not None and uid is not None:
        try:
            name = pwd.getpwuid(uid).pw_name
        except (KeyError, OverflowError):
            name = None
    if grp is not None and gid is not None:
        try:
            group = grp.getgrgid(gid).gr_name
        except (KeyError, OverflowError):
            group = None
    if synthetic:
        # Present but not meaningful; say so rather than imply otherwise.
        name = f"{name} (from mount options)" if name else None
        group = f"{group} (from mount options)" if group else None
    return uid, name, gid, group


def stat_record(path):
    """Everything worth recording about a file as found on disk.

    Never raises for a readable path; fields that cannot be determined are
    None and carry a sibling field saying why.
    """
    st = os.lstat(path)
    is_link = statmod.S_ISLNK(st.st_mode)
    target = os.readlink(path) if is_link else None
    if is_link:
        try:
            st = os.stat(path)         # report the file, note the link
        except OSError:
            pass

    fstype = filesystem_of(path)
    synthetic = bool(fstype and fstype.lower() in _SYNTHETIC_FS)
    created, created_source = _birthtime(path, st)
    uid, owner, gid, group = _owner(st, synthetic)
    mode = st.st_mode

    return {
        "path": os.path.abspath(path),
        "name": os.path.basename(path),
        "extension": os.path.splitext(path)[1].lower().lstrip("."),
        "size_bytes": st.st_size,
        "size": human_size(st.st_size),
        "modified": _iso(st.st_mtime),
        "accessed": _iso(st.st_atime),
        "created": _iso(created),
        "created_source": created_source,
        "inode_changed": _iso(st.st_ctime),
        "permissions": statmod.filemode(mode),
        "mode_octal": oct(mode & 0o7777),
        "setuid": bool(mode & statmod.S_ISUID),
        "setgid": bool(mode & statmod.S_ISGID),
        "sticky": bool(mode & statmod.S_ISVTX),
        "owner_uid": uid,
        "owner": owner,
        "group_gid": gid,
        "group": group,
        "is_symlink": is_link,
        "symlink_target": target,
        "hard_links": getattr(st, "st_nlink", None),
        "inode": getattr(st, "st_ino", None),
        "filesystem": fstype,
        "ownership_reliable": not synthetic,
        "source": "filesystem",
        "captured_at": _iso(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        "captured_on": SYSTEM,
    }


def zip_member_record(info, archive_path):
    """State of a document that was found inside a ZIP archive.

    The entry's own timestamp and mode come from the central directory, not
    from the archive file -- a member can predate the archive by years.
    """
    try:
        modified = datetime.datetime(*info.date_time).strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        modified = None

    # The high 16 bits of external_attr hold the Unix mode when the archive was
    # produced on a Unix system; zero means the producer did not record one.
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if unix_mode and not statmod.S_IFMT(unix_mode):
        # ZIP stores permission bits without file-type bits, so filemode()
        # renders a leading "?". A member is a regular file (directories are
        # reported separately by is_dir), so say so.
        unix_mode |= statmod.S_IFREG
    perms = statmod.filemode(unix_mode) if unix_mode else None

    return {
        "path": f"{os.path.abspath(archive_path)}!{info.filename}",
        "name": os.path.basename(info.filename.replace("\\", "/")),
        "extension": os.path.splitext(info.filename)[1].lower().lstrip("."),
        "size_bytes": info.file_size,
        "size": human_size(info.file_size),
        "modified": modified,
        "accessed": None,
        "created": None,
        "created_source": "not recorded in ZIP format",
        "inode_changed": None,
        "permissions": perms,
        "mode_octal": oct(unix_mode & 0o7777) if unix_mode else None,
        "setuid": False,
        "setgid": False,
        "sticky": False,
        "owner_uid": None,
        "owner": None,
        "group_gid": None,
        "group": None,
        "is_symlink": False,
        "symlink_target": None,
        "hard_links": None,
        "inode": None,
        "filesystem": f"zip member of {os.path.basename(archive_path)}",
        "ownership_reliable": False,
        "compressed_bytes": info.compress_size,
        "compression_ratio": (round(info.file_size / info.compress_size, 1)
                              if info.compress_size else None),
        "crc32": f"{info.CRC:08x}",
        "source": "zip-central-directory",
        "captured_at": _iso(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        "captured_on": SYSTEM,
    }


# Column order for the datasheet, chosen so the useful fields come first.
DATASHEET_COLUMNS = [
    "name", "extension", "size", "size_bytes", "modified", "created",
    "created_source", "inode_changed", "accessed", "permissions", "mode_octal",
    "owner", "owner_uid", "group", "group_gid", "ownership_reliable",
    "is_symlink", "symlink_target", "hard_links", "inode", "filesystem",
    "compressed_bytes", "compression_ratio", "crc32", "sha256",
    "characters_extracted", "read_result", "path", "captured_at", "captured_on",
]
