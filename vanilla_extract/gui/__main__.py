"""`python -m vanilla_extract.gui` opens the desktop window."""
import sys

from .qt_app import main

if __name__ == "__main__":
    sys.exit(main())
