"""Entry point for the PACKAGED command line.

`vanilla_extract/__main__.py` uses relative imports, which is correct for a
module run as `python -m vanilla_extract` and impossible for a script
PyInstaller runs as a top-level module. The bundled binary died on its first
line with "attempted relative import with no known parent package".

It had been like that since the installer was written. The build script told
whoever ran it to "smoke-test it before shipping", and both platform
installers install the result -- but nothing ever ran the built binary, so the
instruction was the only thing standing between a broken installer and a
customer. It is a check now, in verify_e2e.

The module itself is left alone: rewriting a package's own imports to suit a
build tool would be the tail wagging the dog.
"""

import sys

from vanilla_extract.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
