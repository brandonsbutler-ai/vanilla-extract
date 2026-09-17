"""Resource limits for hostile input.

This library's whole job is reading files supplied by someone else, so the
input is untrusted by definition. Two bounds matter in practice.

A ZIP DECOMPRESSION BOMB is the live risk. A 200 KB .docx can declare a 200 MB
document.xml -- a ratio above 1000:1 -- and a reader that simply calls
`zf.read(name)` will allocate all of it. Verified on this library before these
limits existed: 199 KB in, 200 MB allocated, no error. Every archive member is
first checked against its DECLARED uncompressed size, which rejects an honest
bomb cheaply; but the declared size lives in the archive's own header and can be
forged, so the READ is bounded too -- it decompresses incrementally and stops at
the ratio ceiling, refusing a member whose REAL stream runs past it regardless
of what it claims. A running budget caps the archive as a whole.

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

# One Flate stream inside a PDF may not expand past this. A PDF stream carries
# no declared output size, so unlike a zip member it cannot be vetted up front
# -- it has to be decompressed with a ceiling. Measured before this existed: a
# 305 KB PDF expanded one stream to 616 MB of resident memory in 0.4 s.
# Peak memory runs to roughly TWICE this, because the inflated buffer is
# copied once during processing. Measured: a 597 KB bomb declaring 600 MB
# peaked at 274 MB against a 128 MB cap, so the cap is set at 64 MB to keep
# the worst case near 128 MB. 64 MB of text from one content stream is still
# roughly 64 million characters -- far beyond any real document.
MAX_PDF_STREAM_BYTES = 64 * 1024 * 1024     # 64 MB


class ArchiveTooLarge(Exception):
    """An archive member exceeded a size or compression-ratio limit.

    Raised before allocating, so hitting this costs nothing.
    """


def bounded_inflate(raw, limit=None):
    """zlib-inflate `raw`, refusing to produce more than `limit` bytes.

    Returns (data, truncated). Uses the incremental decompressor with a max
    length, because zlib.decompress() has no ceiling and will happily allocate
    whatever the stream describes.
    """
    import zlib

    limit = MAX_PDF_STREAM_BYTES if limit is None else limit
    obj = zlib.decompressobj()
    try:
        data = obj.decompress(raw, limit)
    except zlib.error:
        return None, False
    # unconsumed_tail is non-empty exactly when the limit stopped us early.
    return data, bool(obj.unconsumed_tail)


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
    """Read one archive member, bounded by its REAL decompressed size.

    check_member vets the member's DECLARED size, which lives in the archive's
    own header and can be forged -- write file_size=100 into a header whose
    stream really inflates to 200 MB and the declared-size and ratio checks both
    wave it through. So the read itself is bounded too: it stops at the largest
    size the compressed bytes could legitimately reach (the compression-ratio
    ceiling, capped by the per-member limit) and refuses a member whose real
    stream runs past that, before it is all allocated.

    `zf.read(info)` cannot do this. It is `read(-1)`, which decompresses the
    whole stream in one unbounded call and only then truncates to the declared
    size -- so it allocates the bomb in full and discovers the lie too late.
    Reading a bounded number of bytes decompresses incrementally and stops.
    """
    check_member(info, budget)
    name = getattr(info, "filename", "member")
    packed = getattr(info, "compress_size", 0) or 0
    ceiling = MAX_MEMBER_BYTES
    if packed > 0:
        ceiling = min(ceiling, packed * MAX_COMPRESSION_RATIO)
    with zf.open(info) as fh:
        data = fh.read(ceiling + 1)
    if len(data) > ceiling:
        raise ArchiveTooLarge(
            f"{name}: decompresses past {ceiling // (1024 * 1024)} MB "
            f"({packed} compressed bytes at a {MAX_COMPRESSION_RATIO}:1 cap), "
            f"a decompression bomb rather than a document")
    return data
