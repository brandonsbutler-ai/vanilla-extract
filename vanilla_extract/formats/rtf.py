r"""RTF.

RTF is control words, groups and escapes rather than markup. The rules that
matter for pulling text out:

  * \word and \word123 are control words; most produce no text.
  * A control word's terminating space is part of the control word, not content.
  * \'hh is a hex-escaped byte.
  * Some groups are metadata (\fonttbl, \colortbl, \stylesheet, \info) and
    their whole contents must be dropped, not just their control words --
    otherwise font names end up in the output.
  * \* marks a destination the reader is allowed to ignore entirely.
"""

import re

_DROP_GROUPS = ("fonttbl", "colortbl", "stylesheet", "info", "listtable",
                "listoverridetable", "rsidtbl", "generator", "themedata",
                "latentstyles", "datastore", "pict")

_BREAKS = {"par": "\n", "line": "\n", "tab": "\t", "page": "\n\n",
           "sect": "\n\n", "cell": "\t", "row": "\n"}

_CONTROL = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}]+)")


def _start_dropping(dropping, depth):
    r"""Mark this group as discarded, at most once.

    `{\*\generator Riched20;}` triggers the drop TWICE -- once for `\*` and
    once for the `generator` control word -- but the single closing brace pops
    only one entry. The leftover entry then discarded the rest of the file, so
    every RTF written by Word, WordPad or RichEdit extracted as an empty
    string. One entry per group is the invariant.
    """
    if not (dropping and dropping[-1] == depth):
        dropping.append(depth)


def extract_rtf(fh):
    data = fh.read() if hasattr(fh, "read") else fh
    text = data.decode("latin-1", errors="replace")
    out = []
    depth = 0
    # Stack of group depths we are currently discarding.
    dropping = []
    for m in _CONTROL.finditer(text):
        word, _arg, hexbyte, escaped, brace, literal = m.groups()
        if brace == "{":
            depth += 1
            continue
        if brace == "}":
            if dropping and dropping[-1] == depth:
                dropping.pop()
            depth -= 1
            continue
        if word:
            if word in _DROP_GROUPS:
                _start_dropping(dropping, depth)
                continue
            if not dropping and word in _BREAKS:
                out.append(_BREAKS[word])
            continue
        if dropping:
            continue
        if hexbyte:
            out.append(bytes([int(hexbyte, 16)]).decode("latin-1", errors="replace"))
        elif escaped:
            if escaped == "*":
                # \* introduces an ignorable destination.
                _start_dropping(dropping, depth)
            elif escaped in ("{", "}", "\\"):
                out.append(escaped)
            elif escaped == "\n":
                out.append("\n")
        elif literal:
            out.append(literal)
    joined = "".join(out)
    # Strip trailing blanks before each newline. This was `re.sub(r"[ \t]+\n",
    # "\n", joined)`, which walks a run of blanks, fails to find the newline,
    # and restarts one position later: 1.9 seconds on 64 KB of spaces, which an
    # RTF file can contain. Splitting is linear and agrees with the pattern on
    # every one of 60,000 compared strings -- the last segment is left alone
    # because no newline follows it.
    _lines = joined.split("\n")
    joined = "\n".join([l.rstrip(" \t") for l in _lines[:-1]] + _lines[-1:])
    return re.sub(r"\n{3,}", "\n\n", joined).strip()
