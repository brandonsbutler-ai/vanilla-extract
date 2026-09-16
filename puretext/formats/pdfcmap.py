r"""/ToUnicode CMap support: makes subset-font PDFs readable.

The problem this solves. A PDF produced by Word or LibreOffice usually embeds
a SUBSET of each font, and renumbers the glyphs 1, 2, 3... in order of first
appearance. The content stream then draws `\x01\x02\x03`, which means nothing
on its own -- decoding those bytes as text yields control characters, not
words. The translation table is a /ToUnicode CMap attached to the font.

So reading such a file needs three things this module provides:

  1. an index of the PDF's indirect objects, including the ones packed inside
     compressed object streams (/ObjStm), which is where PDF 1.5+ puts them;
  2. a resolver for `12 0 R` style references;
  3. a parser for the CMap's beginbfchar/beginbfrange tables.

SIMPLIFICATION, stated plainly: fonts are keyed by their resource NAME (/F1,
/F2) collected across the whole document rather than per page-resource
dictionary. A file that reuses /F1 for two different fonts on different pages
can therefore decode one of them wrong. Handling that properly means walking
the page tree, which is a much larger job for a case that is rare in
tool-generated PDFs. The benchmark is how we know the trade is acceptable.
"""

import re
import zlib

_OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")

# A single PDF object holding more than this is not a font or a CMap.
MAX_OBJECT_BYTES = 32 * 1024 * 1024
_REF_RE = re.compile(rb"(\d+)\s+(\d+)\s+R\b")
_TOUNICODE_RE = re.compile(rb"/ToUnicode\s+(\d+)\s+(\d+)\s+R")
_FONT_RES_RE = re.compile(rb"/([A-Za-z0-9#+._-]+)\s+(\d+)\s+(\d+)\s+R")


def _stream_bytes(obj):
    """Raw stream payload of an object, decompressed when Flate-encoded."""
    m = re.search(rb"stream\r?\n?", obj)
    if not m:
        return None
    end = obj.find(b"endstream", m.end())
    raw = obj[m.end():end if end != -1 else len(obj)]
    if b"/FlateDecode" in obj[:m.start()]:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(raw)
            except zlib.error:
                return None
    return raw


def build_object_index(data):
    """Map object number -> object bytes, including objects inside /ObjStm.

    Scans for `N G obj` rather than trusting the xref table, then expands any
    compressed object streams it finds. A later definition of the same object
    number wins, which approximates incremental-update semantics.
    """
    index = {}
    # Bound each object by the NEXT object header as well as by `endobj`.
    # Without the bound, an object missing its `endobj` slices to end-of-file,
    # which is quadratic in both time and memory: 531 KB of crafted headers
    # took 3 seconds, and 77 KB pushed resident memory to 168 MB.
    starts = [(int(m.group(1)), m.end(), m.start()) for m in _OBJ_RE.finditer(data)]
    for i, (num, body_start, _hdr_start) in enumerate(starts):
        next_hdr = starts[i + 1][2] if i + 1 < len(starts) else len(data)
        end = data.find(b"endobj", body_start, next_hdr)
        stop = end if end != -1 else next_hdr
        if stop - body_start > MAX_OBJECT_BYTES:
            continue                  # not a real object; skip rather than slice
        index[num] = data[body_start:stop]

    # Expand compressed object streams.
    for num, obj in list(index.items()):
        if b"/ObjStm" not in obj:
            continue
        body = _stream_bytes(obj)
        if not body:
            continue
        n_match = re.search(rb"/N\s+(\d+)", obj)
        first_match = re.search(rb"/First\s+(\d+)", obj)
        if not (n_match and first_match):
            continue
        count = int(n_match.group(1))
        first = int(first_match.group(1))
        header = body[:first].split()
        # header is pairs: objnum offset objnum offset ...
        for i in range(0, min(len(header) - 1, count * 2), 2):
            try:
                onum = int(header[i])
                off = int(header[i + 1])
            except ValueError:
                continue
            start = first + off
            nxt = len(body)
            if i + 3 < len(header):
                try:
                    nxt = first + int(header[i + 3])
                except ValueError:
                    pass
            if onum not in index:
                index[onum] = body[start:nxt]
    return index


def _balanced_dict(buf, start):
    """Return the bytes inside the `<< >>` beginning at or after buf[start].

    Counts nesting rather than stopping at the first `>>`, which a non-greedy
    regex cannot do -- and font resource dicts nest constantly.
    """
    i = buf.find(b"<<", start)
    if i == -1:
        return None
    depth = 0
    j = i
    while j < len(buf) - 1:
        pair = buf[j:j + 2]
        if pair == b"<<":
            depth += 1
            j += 2
            continue
        if pair == b">>":
            depth -= 1
            j += 2
            if depth == 0:
                return buf[i + 2:j - 2]
            continue
        j += 1
    return None


def _parse_cmap(text):
    """Parse a /ToUnicode CMap into {code: character}.

    Handles both table forms:
        <01> <0042>                       in beginbfchar
        <01> <03> <0042>                  in beginbfrange (contiguous)
        <01> <03> [<0042> <0043> <0044>]  in beginbfrange (explicit list)
    """
    mapping = {}

    def to_str(hexstr):
        """A bfchar target may be several UTF-16BE code units (a ligature).

        Pad BEFORE converting: an odd number of hex digits raises ValueError
        out of bytes.fromhex, which escaped _parse_cmap, was swallowed upstream
        as "no CMaps", and made every subset font in the document decode to
        glyph IDs -- turning one malformed entry into a whole unreadable PDF.
        """
        if len(hexstr) % 2:
            hexstr += "0"
        raw = bytes.fromhex(hexstr)
        if len(raw) % 2:
            raw += b"\x00"
        try:
            return raw.decode("utf-16-be", errors="replace")
        except UnicodeDecodeError:
            return ""

    for block in re.findall(rb"beginbfchar(.*?)endbfchar", text, re.DOTALL):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            mapping[int(src, 16)] = to_str(dst.decode("ascii"))

    for block in re.findall(rb"beginbfrange(.*?)endbfrange", text, re.DOTALL):
        # Explicit-list form first, so its <..> pairs are not re-read below.
        for lo, hi, items in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.DOTALL):
            start = int(lo, 16)
            targets = re.findall(rb"<([0-9A-Fa-f]+)>", items)
            for offset, dst in enumerate(targets):
                mapping[start + offset] = to_str(dst.decode("ascii"))
        stripped = re.sub(rb"<[0-9A-Fa-f]+>\s*<[0-9A-Fa-f]+>\s*\[.*?\]", b"",
                          block, flags=re.DOTALL)
        for lo, hi, dst in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", stripped):
            start, stop = int(lo, 16), int(hi, 16)
            hexdigits = dst.decode("ascii")
            if len(hexdigits) % 2:
                hexdigits += "0"
            base = bytes.fromhex(hexdigits)
            if len(base) % 2:
                base += b"\x00"
            base_cp = int.from_bytes(base[-2:], "big")
            prefix = base[:-2]
            if stop - start > 65535:            # malformed; refuse to loop forever
                continue
            for offset in range(stop - start + 1):
                unit = (base_cp + offset).to_bytes(2, "big")
                mapping[start + offset] = (prefix + unit).decode(
                    "utf-16-be", errors="replace")
    return mapping


def font_cmaps(data):
    """Map font resource name (str, no slash) -> {code: char}.

    Returns {} when the document has no /ToUnicode tables, which is the signal
    to fall back to byte decoding.
    """
    index = build_object_index(data)
    if not index:
        return {}

    # objnum of a font -> its CMap
    by_objnum = {}
    for num, obj in index.items():
        m = _TOUNICODE_RE.search(obj)
        if not m:
            continue
        target = int(m.group(1))
        stream = index.get(target)
        if stream is None:
            continue
        body = _stream_bytes(stream)
        if not body:
            continue
        parsed = _parse_cmap(body)
        if parsed:
            by_objnum[num] = parsed

    if not by_objnum:
        return {}

    # resource name -> CMap, via every /Font resource dictionary in the file.
    # A /Font entry appears in two forms and both occur in the wild:
    #     /Font << /F1 245 0 R /F2 250 0 R >>      inline
    #     /Font 266 0 R                            indirect
    # LibreOffice writes the indirect form and shares one resource object
    # across every page, so handling only the inline form finds nothing.
    by_name = {}

    def harvest(dict_bytes):
        for name, onum, _gen in _FONT_RES_RE.findall(dict_bytes):
            cmap = by_objnum.get(int(onum))
            if cmap:
                by_name.setdefault(name.decode("latin-1"), cmap)

    for obj in index.values():
        for fm in re.finditer(rb"/Font\b", obj):
            after = obj[fm.end():fm.end() + 64]
            ref = re.match(rb"\s*(\d+)\s+(\d+)\s+R\b", after)
            if ref:
                target = index.get(int(ref.group(1)))
                if target:
                    harvest(target)
                continue
            if re.match(rb"\s*<<", after):
                body = _balanced_dict(obj, fm.end())
                if body:
                    harvest(body)
    # A font referenced without a resource dict we could find: expose it by
    # object number so a single-font document still decodes.
    if not by_name and len(by_objnum) == 1:
        by_name["*"] = next(iter(by_objnum.values()))
    return by_name


def decode_with_cmap(raw, cmap):
    """Decode string bytes through a CMap.

    Subset CMaps are keyed on one-byte codes in the files this targets; a
    two-byte CID font is detected by every single-byte lookup failing.
    """
    if not cmap:
        return None
    single = "".join(cmap.get(b, "�") for b in raw)
    if single.count("�") <= len(raw) * 0.25:
        return single
    if len(raw) % 2 == 0:
        pairs = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
        double = "".join(cmap.get(p, "�") for p in pairs)
        if double.count("�") <= len(pairs) * 0.25:
            return double
    return None
