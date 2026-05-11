#!/usr/bin/env python3
"""Console scrollback smoke test.

Boots BBoeOS, runs enough commands to scroll a known marker line off
the visible screen, sends Shift+PgUp via the QEMU monitor, dumps the
VGA text framebuffer with `pmemsave 0xb8000 4000 ...`, and verifies
the marker reappears.  Then sends a normal key and verifies the
scrollback view collapses (marker goes away, prompt comes back).

Run standalone:
    tests/test_scrollback.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import QemuSession, qemu_session  # noqa: E402

MARKER = "ZZSCROLLMARK"
NUM_FILLER = 30

# pmemsave resolves paths relative to QEMU's cwd (the repo root).
# Using an absolute path trips QEMU's HMP expression parser (the
# leading slash is interpreted as division), so we use a short
# relative name and translate it to the absolute repo-root path for
# reading.
_FB_DUMP_RELPATH = "fb_scrollback_dump.bin"
_FB_DUMP_ABSPATH = REPO_ROOT / _FB_DUMP_RELPATH


def framebuffer_text(*, session: QemuSession) -> str:
    """Return the visible VGA text as a single string (rows joined with newlines).

    Drains the serial fifo briefly first so any pending PS/2 IRQs fired
    by a preceding sendkey() have been processed by the guest before we
    snapshot the framebuffer.  Without the drain, pmemsave can race the
    IRQ handler and read stale VGA memory.
    """
    session.drain_serial(seconds=0.15)
    _FB_DUMP_ABSPATH.unlink(missing_ok=True)
    session.monitor_send(f"pmemsave 0xb8000 4000 {_FB_DUMP_RELPATH}")
    raw = _FB_DUMP_ABSPATH.read_bytes()
    _FB_DUMP_ABSPATH.unlink(missing_ok=True)
    rows = []
    for row_index in range(25):
        chars = bytearray()
        for col_index in range(80):
            byte = raw[(row_index * 80 + col_index) * 2]
            chars.append(byte if 0x20 <= byte <= 0x7E else 0x20)
        rows.append(chars.rstrip().decode("ascii"))
    return "\n".join(rows)


def test_shift_pgup_reveals_scrolled_off_line() -> None:
    """Shift+PgUp reveals a previously-scrolled-off marker line; a cooked-emit key collapses scrollback."""
    with qemu_session(monitor=True, snapshot=True) as session:
        # Print the marker, then enough commands to scroll it off.
        session.send_command(f"echo {MARKER}")
        for _ in range(NUM_FILLER):
            session.send_command("echo filler")
        # Confirm marker is NOT on the visible screen anymore.
        live = framebuffer_text(session=session)
        if MARKER in live:
            message = f"marker should have scrolled off; live screen:\n{live}"
            raise AssertionError(message)
        # Shift+PgUp until the marker shows.  Each press scrolls 24
        # rows; 30 filler commands fill ~42 rows of history (the marker
        # is at the very beginning), so 2 presses (each capped to the
        # remaining valid rows) reaches it.
        session.sendkey("shift-pgup")
        session.sendkey("shift-pgup")
        scrolled = framebuffer_text(session=session)
        if MARKER not in scrolled:
            message = f"Shift+PgUp should reveal the marker; got:\n{scrolled}"
            raise AssertionError(message)
        # Type a regular key — cooked emit auto-exits scrollback.
        session.sendkey("a")
        session.sendkey("backspace")  # erase the 'a' so prompt is clean
        session.drain_serial(seconds=0.3)
        live_again = framebuffer_text(session=session)
        if MARKER in live_again:
            message = f"cooked-emit key should collapse scrollback; got:\n{live_again}"
            raise AssertionError(message)
    print("PASS: test_shift_pgup_reveals_scrolled_off_line")


def main() -> int:
    """Build the OS image and run the scrollback smoke test."""
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    test_shift_pgup_reveals_scrolled_off_line()
    print("1 passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
