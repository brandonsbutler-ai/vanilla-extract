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
                dropping.append(depth)
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
                dropping.append(depth)
            elif escaped in ("{", "}", "\\"):
                out.append(escaped)
            elif escaped == "\n":
                out.append("\n")
        elif literal:
            out.append(literal)
    joined = "".join(out)
    joined = re.sub(r"[ \t]+\n", "\n", joined)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()
