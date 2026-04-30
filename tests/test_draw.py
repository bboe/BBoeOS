#!/usr/bin/env python3
r"""Smoke + font-restoration test for the draw program.

Runs draw with a few keystrokes, exits, and renders one more command on
the post-exit text screen.  Captures a VGA screendump and checks the
framebuffer has substantial pixel content.

Two failure modes this guards against:

1. draw crashes (vga_fill_block #GP, vga_set_mode bug) -- shell prompt
   never returns and the test times out waiting for the post-ls-bin
   prompt.
2. The boot-time vga_font_load doesn't run, leaving char-gen slot
   0x4000 empty.  draw exits cleanly but the post-exit text mode (mode
   03h with SR03=05h) renders every glyph as blank: framebuffer is
   nearly all-zero except for the blinking hardware cursor.

Drives QEMU through run_qemu.qemu_session because draw sends keystrokes
mid-program (not commands the shell prompts for) and needs the HMP
monitor to take a screendump — both of which run_commands' simple
"send a command, wait for prompt" loop can't express directly.

Usage:
    ./test_draw.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402

LS_TIMEOUT = 5.0

# A populated 80x25 text screen with welcome banner + uptime + ls bin
# clocks ~10-15k non-zero RGB bytes in a 720x400 PPM.  An empty-font
# screen would be under ~200 (just the hardware cursor).  500 is safely
# above the noise floor and well below any plausibly-rendered screen.
MIN_NONZERO_PIXEL_BYTES = 500


def _count_nonzero_pixel_bytes(*, screenshot_path: Path) -> int:
    """Return the number of non-zero RGB bytes in a P6 PPM file."""
    data = screenshot_path.read_bytes()
    end = 0
    for _ in range(3):  # P6 magic, dimensions, max-value
        end = data.index(b"\n", end) + 1
    return sum(1 for byte in data[end:] if byte)


def _build_os(*, image_path: Path) -> None:
    """Run make_os.sh into image_path; abort the test if the build fails."""
    result = subprocess.run(
        ["./make_os.sh", str(image_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        message = "make_os.sh failed"
        raise RuntimeError(message)


def _run_draw_session(
    *,
    floppy: bool,
    image_path: Path,
    screenshot_path: Path,
) -> bytearray:
    """Boot QEMU, drive draw + ls bin, screendump, return serial buffer."""
    with qemu_session(
        drive=image_path,
        floppy=floppy,
        monitor=True,
        snapshot=True,
    ) as session:
        # Sequence: draw, then 5 keystrokes (q exits), then ls bin to
        # populate the post-exit screen.  Sending in stages with short
        # pauses sidesteps the shell-respawn-prints-extra-prompt race
        # that confuses run_commands' prompt counter.
        session.write_serial("draw\r")
        time.sleep(0.5)
        session.drain_serial(seconds=0.3)
        for character in "wasdq":
            session.write_serial(character)
            time.sleep(0.1)
            session.drain_serial(seconds=0.05)
        time.sleep(0.5)
        session.drain_serial(seconds=0.3)
        ls_start = len(session.buffer)
        session.write_serial("ls bin\r")
        session.wait_for_substring(
            b"uptime*",  # last filename alphabetically
            start=ls_start,
            timeout=LS_TIMEOUT,
        )
        time.sleep(0.3)  # let trailing prompt render before screendump
        session.screendump(screenshot_path)
        return session.buffer


def main() -> int:
    """Boot, exercise draw, screendump, verify pixel content."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--floppy",
        action="store_true",
        help="boot QEMU with the drive attached as a floppy (if=floppy)",
    )
    arguments = parser.parse_args()
    os.chdir(REPO_ROOT)

    with tempfile.TemporaryDirectory(prefix="test_draw_") as temporary_path:
        temporary_directory = Path(temporary_path)
        image_path = temporary_directory / "drive.img"
        screenshot_path = temporary_directory / "post_draw.ppm"

        try:
            _build_os(image_path=image_path)
            buffer = _run_draw_session(
                floppy=arguments.floppy,
                image_path=image_path,
                screenshot_path=screenshot_path,
            )
        except (OSError, RuntimeError, TimeoutError) as error:
            print(f"FAIL: {error}")
            return 1

        if not screenshot_path.exists():
            print("FAIL: monitor screendump didn't produce a file")
            return 1
        nonzero = _count_nonzero_pixel_bytes(screenshot_path=screenshot_path)
        if nonzero < MIN_NONZERO_PIXEL_BYTES:
            print(
                f"FAIL: post-draw screen has only {nonzero} non-zero RGB bytes "
                f"(need >= {MIN_NONZERO_PIXEL_BYTES}); font slot 0x4000 likely empty"
            )
            return 1
        if b"EXC" in buffer:
            print(f"FAIL: CPU exception observed in serial output: tail={bytes(buffer[-300:])!r}")
            return 1
        print(f"PASS: draw exited cleanly, post-draw text rendered ({nonzero} non-zero RGB bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
