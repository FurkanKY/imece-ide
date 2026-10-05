"""`python -m collab_runtime` entry point (offline collaboration CLI)."""

import sys

from collab_runtime.cli import main

if __name__ == "__main__":
    sys.exit(main())
