#!/usr/bin/env python3
"""Build a single-file vanilla_extract executable for the host platform.

Produces dist/vanilla (Linux) or dist/vanilla.exe (Windows) -- one file, no
Python installation required on the target machine. That matters because the
people who most need a dependency-free extractor are usually the ones who
cannot install Python either.

PyInstaller is a BUILD-time tool. It is not a runtime dependency and nothing
it adds changes the library's no-dependencies promise: `import vanilla_extract` still
pulls in nothing but the standard library.

    python3 -m pip install pyinstaller
    python3 packaging/build_standalone.py
"""

import os
import platform
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    if shutil.which("pyinstaller") is None:
        print("pyinstaller not found. Install it with:\n"
              "    python3 -m pip install pyinstaller", file=sys.stderr)
        return 2

    cmd = [
        "pyinstaller",
        "--onefile",
        # The binary is `vanilla` everywhere else -- the pyproject console
        # script, install-linux.sh, and the Inno Setup AppExeName. A blanket
        # rename substituted this to the MODULE name, so the Linux installer
        # silently fell back to the launcher and the Windows installer failed
        # on a missing source file.
        "--name", "vanilla",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build"),
        "--specpath", os.path.join(ROOT, "build"),
        "--console",
        # Nothing to bundle beyond the package itself; no data files, no hooks.
        os.path.join(ROOT, "vanilla_extract", "__main__.py"),
    ]
    print("running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    name = "vanilla.exe" if platform.system() == "Windows" else "vanilla"
    built = os.path.join(ROOT, "dist", name)
    if not os.path.isfile(built):
        print(f"expected {built} but it was not produced", file=sys.stderr)
        return 1

    size = os.path.getsize(built) / (1024 * 1024)
    print(f"\nbuilt {built}  ({size:.1f} MB)")
    print("Smoke-test it before shipping:")
    print(f"    {built} --version")
    return 0


if __name__ == "__main__":
    sys.exit(main())
