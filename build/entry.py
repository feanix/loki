"""PyInstaller entry point: same as `python -m loki`."""

import sys

from loki.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
