#!/usr/bin/env python3
"""Thin shim so ``python cc.py src.c out.asm`` still works.

The real compiler lives in the :mod:`cc` package; this file exists so
``make_os.sh`` and the test suite can keep invoking ``cc.py`` directly.
"""

from __future__ import annotations

import sys

from cc.cli import main

if __name__ == "__main__":
    sys.exit(main())
