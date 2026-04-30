#!/usr/bin/env python3
"""PS/2 keyboard driver smoke test.

Boots BBoeOS, waits for the shell prompt, then injects keys via the
QEMU monitor's `sendkey` command.  Captures the serial console and
checks that typed characters reach the shell — this is the only path
that actually exercises the native PS/2 driver (the other tests feed
input through the serial fifo and skip the keyboard layer entirely).

Run standalone:
    tests/test_ps2.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402

DRAIN_AFTER_KEYS = 2.0


def inject_keys(*, expected: bytes, floppy: bool, keys: list[str]) -> None:
    """Boot BBoeOS, send `keys` via the monitor, expect `expected` on serial."""
    with qemu_session(floppy=floppy, monitor=True, snapshot=True) as session:
        pre_len = len(session.buffer)
        for key in keys:
            session.sendkey(key)
        session.drain_serial(seconds=DRAIN_AFTER_KEYS)
        new_output = bytes(session.buffer[pre_len:])
        if expected not in new_output:
            message = f"expected {expected!r} in output, got {new_output!r}"
            raise AssertionError(message)
    print(f"PASS: {' '.join(keys)} -> {expected!r}")


def main() -> int:
    """Run the PS/2 smoke cases: unshifted and shifted letter injection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--floppy",
        action="store_true",
        help="boot QEMU with the drive attached as a floppy (if=floppy)",
    )
    arguments = parser.parse_args()
    inject_keys(
        expected=b"hi\r\n",
        floppy=arguments.floppy,
        keys=["e", "c", "h", "o", "spc", "h", "i", "ret"],
    )
    inject_keys(
        expected=b"Hi\r\n",
        floppy=arguments.floppy,
        keys=["e", "c", "h", "o", "spc", "shift-h", "i", "ret"],
    )
    print("2 passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
