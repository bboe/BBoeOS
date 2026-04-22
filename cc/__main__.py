"""``python -m cc`` entry point — delegates to :func:`cc.cli.main`."""

from __future__ import annotations

import sys

from cc.cli import main

if __name__ == "__main__":
    sys.exit(main())
