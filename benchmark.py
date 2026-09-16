#!/usr/bin/env python3
"""Measure vanilla_extract's PDF output against poppler's pdftotext.

Why this exists: "it extracts text" is not a claim anyone should take on
faith. This scores every PDF in a directory by token overlap against
pdftotext -- a mature C++ implementation with full font handling -- and prints
the distribution. The README quotes these numbers and nothing else.

pdftotext is the reference, not the target: it is allowed to win. The point is
to know BY HOW MUCH, and to find the files where vanilla_extract returns nothing.

    python3 benchmark.py /path/to/pdfs
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vanilla_extract import extract_file            # noqa: E402
from vanilla_extract.formats.pdf import EncryptedPDF, UndecodableText  # noqa: E402

_WORD = re.compile(r"[A-Za-z0-9]+")


def tokens(text):
    return [w.lower() for w in _WORD.findall(text)]


def reference(path):
    """pdftotext output, or None if poppler is not installed."""
    try:
        out = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", errors="replace")


def recall(ours, theirs):
    """Fraction of the reference's tokens that we also produced.

    Multiset-aware: producing a word once when the reference has it three
    times counts as one third, not as a match.
    """
    ref = tokens(theirs)
    if not ref:
        return None
    mine = {}
    for t in tokens(ours):
        mine[t] = mine.get(t, 0) + 1
    hit = 0
    for t in ref:
        if mine.get(t, 0) > 0:
            mine[t] -= 1
            hit += 1
    return hit / len(ref)


def main(directory):
    paths = sorted(os.path.join(directory, f) for f in os.listdir(directory)
                   if f.lower().endswith(".pdf"))
    if not paths:
        print(f"no PDFs in {directory}")
        return 1

    scores, empties, skipped, encrypted, undecodable = [], [], 0, [], []
    for path in paths:
        ref = reference(path)
        if ref is None:
            skipped += 1
            continue
        try:
            ours = extract_file(path)
        except EncryptedPDF:
            encrypted.append(os.path.basename(path))
            continue
        except UndecodableText:
            undecodable.append(os.path.basename(path))
            continue
        except Exception as exc:                      # noqa: BLE001
            empties.append((os.path.basename(path), f"{type(exc).__name__}"))
            continue
        score = recall(ours, ref)
        if score is None:
            skipped += 1
            continue
        if not ours.strip():
            empties.append((os.path.basename(path), "empty output"))
        scores.append((score, os.path.basename(path)))

    if not scores:
        print("nothing measurable (is pdftotext installed?)")
        return 1

    scores.sort()
    values = [s for s, _ in scores]
    n = len(values)
    mean = sum(values) / n
    median = values[n // 2]
    print(f"files measured : {n}" + (f"  ({skipped} skipped)" if skipped else ""))
    print(f"token recall   : mean {mean:.3f}   median {median:.3f}"
          f"   min {values[0]:.3f}   max {values[-1]:.3f}")
    for threshold in (0.99, 0.95, 0.90, 0.50):
        count = sum(1 for v in values if v >= threshold)
        print(f"  >= {threshold:.2f} recall : {count}/{n} ({100 * count / n:.1f}%)")
    if encrypted:
        print(f"\nencrypted, refused with a clear error: {len(encrypted)}"
              f"  (reported separately -- not scored)")
    if undecodable:
        print(f"unmapped CID fonts, refused with a clear error: {len(undecodable)}"
              f"  (reported separately -- not scored)")
    if empties:
        print(f"\nfiles returning nothing usable: {len(empties)}")
        for name, why in empties[:10]:
            print(f"  {name}: {why}")
    print("\nworst 5:")
    for score, name in scores[:5]:
        print(f"  {score:.3f}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
