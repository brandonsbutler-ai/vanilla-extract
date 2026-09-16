"""Resource limits for hostile input.

This library's whole job is reading files supplied by someone else, so the
input is untrusted by definition. Two bounds matter in practice.

A ZIP DECOMPRESSION BOMB is the live risk. A 200 KB .docx can declare a 200 MB
document.xml -- a ratio above 1000:1 -- and a reader that simply calls
`zf.read(name)` will allocate all of it. Verified on this library before these
limits existed: 199 KB in, 200 MB allocated, no error. So every archive member
is checked against its DECLARED uncompressed size before a byte is read, and a
running budget caps the archive as a whole.

XML ENTITY EXPANSION ("billion laughs") was measured and found already bounded:
the expat parser behind xml.etree stops expanding a few levels in, capping the
damage around 300 KB of text from a tiny file. That is amplification, not
denial of service, so it is documented rather than re-implemented. External
entities are not resolved by xml.etree at all, so XXE does not apply -- also
verified, not assumed.

The defaults are generous on purpose. A real 300-page scanned report is large
and legitimate; the point is to refuse the absurd, not the merely big.
"""

# One member of an archive may not exceed this once decompressed.
MAX_MEMBER_BYTES = 256 * 1024 * 1024        # 256 MB

# Nor may all the members read from a single archive, together.
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024      # 1 GB

# A member whose declared size is this many times its compressed size is a
# bomb rather than a document. Text compresses well, so the threshold is high.
MAX_COMPRESSION_RATIO = 200


class ArchiveTooLarge(Exception):
    """An archive member exceeded a size or compression-ratio limit.

    Raised before allocating, so hitting this costs nothing.
    """


class Budget:
    """A running total for one archive, so many small members cannot add up."""

    def __init__(self, total=MAX_ARCHIVE_BYTES):
        self.remaining = total

    def spend(self, size, what="member"):
        if size > self.remaining:
            raise ArchiveTooLarge(
                f"archive exceeds the {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB "
                f"total limit at {what}")
        self.remaining -= size


def check_member(info, budget=None):
    """Vet a zipfile.ZipInfo before reading it. Raises ArchiveTooLarge."""
    size = getattr(info, "file_size", 0) or 0
    packed = getattr(info, "compress_size", 0) or 0
    name = getattr(info, "filename", "member")

    if size > MAX_MEMBER_BYTES:
        raise ArchiveTooLarge(
            f"{name}: declares {size // (1024 * 1024)} MB uncompressed, over the "
            f"{MAX_MEMBER_BYTES // (1024 * 1024)} MB per-member limit")

    if packed > 0 and size / packed > MAX_COMPRESSION_RATIO:
        raise ArchiveTooLarge(
            f"{name}: compression ratio {size / packed:.0f}:1 exceeds "
            f"{MAX_COMPRESSION_RATIO}:1, which is a decompression bomb rather "
            f"than a document")

    if budget is not None:
        budget.spend(size, name)
    return size


def read_member(zf, info, budget=None):
    """Read one archive member, but only after it passes check_member()."""
    check_member(info, budget)
    return zf.read(info)
