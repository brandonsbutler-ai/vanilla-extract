#!/usr/bin/env python3
"""Build standalone executables for the host platform.

Two of them, and the second is the one that matters to most people:

    vanilla          the command line, one file, no Python needed
    Vanilla Extract  the DESKTOP APPLICATION -- double-click it, drop a folder
                     on it, no terminal and no install step at all

The application is the point. "pip install vanilla-extract[gui]" and then
"vanilla-gui" is two commands and a working Python, which is exactly the
audience this tool does not have: the people who most need a dependency-free
extractor are usually the ones who cannot install Python either. An icon they
can drop a folder onto removes every step between having the problem and
seeing the answer.

PyInstaller is a BUILD-time tool. It is not a runtime dependency and nothing
it adds changes the library's promise: `import vanilla_extract` still pulls in
nothing but the standard library. The desktop build additionally bundles Qt,
which is the window and nothing else.

    python3 -m pip install pyinstaller "PySide6-Essentials"
    python3 packaging/build_standalone.py          # both
    python3 packaging/build_standalone.py --cli    # just the command line
    python3 packaging/build_standalone.py --app    # just the application
"""

import os
import platform
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


WINDOWS = platform.system() == "Windows"
MACOS = platform.system() == "Darwin"
APP_NAME = "Vanilla Extract"


def _run(cmd):
    print("running:", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT).returncode


def _built(name):
    """The path PyInstaller produced, or None."""
    candidates = [os.path.join(ROOT, "dist", name)]
    if WINDOWS:
        candidates.insert(0, os.path.join(ROOT, "dist", name + ".exe"))
    if MACOS:
        candidates.insert(0, os.path.join(ROOT, "dist", name + ".app"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def build_app():
    """The desktop application: windowed, Qt bundled, no console."""
    try:
        import PySide6                                    # noqa: F401
    except ImportError:
        print("PySide6 not found; the application needs it. Install with:\n"
              '    python3 -m pip install "PySide6-Essentials"', file=sys.stderr)
        return 2
    cmd = [
        "pyinstaller",
        "--onefile",
        "--name", APP_NAME,
        # No console window. On Windows a console build flashes a black box
        # behind the application and leaves it open underneath; on macOS it
        # refuses to bundle. This is the flag that makes it an app rather than
        # a command that happens to draw a window.
        "--windowed",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build"),
        "--specpath", os.path.join(ROOT, "build"),
        # Qt ships far more than a window needs. Excluding what is provably
        # unused keeps the download to something a person will actually wait
        # for -- the difference is roughly 200 MB against 60.
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.Qt3DCore",
        "--exclude-module", "PySide6.QtMultimedia",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtCharts",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "numpy",
        # The project root has to be importable, or the launcher's absolute
        # `from vanilla_extract.gui...` cannot resolve during analysis.
        "--paths", ROOT,
        os.path.join(ROOT, "packaging", "app_entry.py"),
    ]
    code = _run(cmd)
    if code:
        return code
    built = _built(APP_NAME)
    if not built:
        print(f"expected dist/{APP_NAME} but it was not produced", file=sys.stderr)
        return 1
    size = os.path.getsize(built) / (1024 * 1024) if os.path.isfile(built) else 0
    print(f"\nbuilt {built}  ({size:.0f} MB)")
    if not WINDOWS and not MACOS:
        _write_desktop_entry(built)
    return 0


def _write_desktop_entry(binary):
    """A .desktop file, so Linux treats it as an application.

    Without one the build is a 57 MB file in a folder: double-clicking it in a
    file manager offers to open it in a text editor, and it appears in no menu
    and in no launcher. The entry is what turns the same binary into something
    a person can find by typing its name, and it declares the MIME types it
    accepts so a folder can be dropped onto its icon.
    """
    path = os.path.join(ROOT, "dist", "vanilla-extract.desktop")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "GenericName=Document text extractor\n"
            "Comment=Drop a folder of documents and get a reviewable table\n"
            f"Exec=\"{os.path.abspath(binary)}\" %f\n"
            "Terminal=false\n"
            "Categories=Office;Utility;\n"
            "MimeType=inode/directory;\n"
            "StartupNotify=true\n")
    print(f"wrote {path}")
    print("  install it for the current user with:")
    print("    cp dist/vanilla-extract.desktop ~/.local/share/applications/")
    return path


def build_cli():
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
        # The root has to be importable for the launcher's absolute import.
        "--paths", ROOT,
        os.path.join(ROOT, "packaging", "cli_entry.py"),
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


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if shutil.which("pyinstaller") is None:
        print("pyinstaller not found. Install it with:\n"
              "    python3 -m pip install pyinstaller", file=sys.stderr)
        return 2
    want_cli = "--app" not in argv
    want_app = "--cli" not in argv
    code = 0
    if want_cli:
        code = build_cli() or code
    if want_app:
        code = build_app() or code
    if not code:
        print("\nBoth are self-contained: the target machine needs no Python,\n"
              "no pip and no Qt. Put the application somewhere a person can\n"
              "double-click it and drop a folder on the window.")
    return code


if __name__ == "__main__":
    sys.exit(main())
