"""Entry point for the PACKAGED desktop application.

Not the same as `python -m vanilla_extract.gui`, and it has to exist for two
reasons that only show up once something is bundled.

PyInstaller runs its entry script as a top-level module, not as part of a
package, so `from .qt_app import main` raises "attempted relative import with
no known parent package" the moment the binary starts. The import here is
absolute.

And PyInstaller finds dependencies by reading the source. The window imports
PySide6 inside a function so that a machine without Qt still gets a working
library and a clear message -- which means a static read of the package never
sees Qt at all, and the first build produced a 7 MB "Qt application" with no
Qt in it. Importing the modules here, at the top level, is what puts them in
the bundle.
"""

import sys

from PySide6 import QtCore, QtGui, QtWidgets           # noqa: F401

from vanilla_extract.gui.qt_app import main

if __name__ == "__main__":
    sys.exit(main())
