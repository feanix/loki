"""PyInstaller entry point for the console exe Loki-cli.exe: the command line (loki.cli), no window."""

import sys

from loki.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
