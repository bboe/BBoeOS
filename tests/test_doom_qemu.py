#!/usr/bin/env python3
"""On-OS Doom bootstrap smoke test.

Builds bin/doom via tools/build_doom.py, drops it on a fresh ext2
disk image (no WAD provided), boots QEMU, runs 'doom' from the shell,
and asserts the engine reaches the IWAD lookup before exiting.

This is end-to-end coverage for the libc + compiler-rt + bboeos
backend boot path: libc malloc grows the heap to ~600 KB, fopen
round-trips a .default.cfg write, the engine's main init sequence
runs, and the program exits cleanly when the WAD isn't found.

Doesn't run the engine main loop (no WAD on disk), so DG_DrawFrame /
DG_GetKey / DG_GetTicksMs aren't exercised — those land in
follow-up tests once WAD provisioning is in place.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOOM_BINARY = REPO / "build" / "doom" / "doom"

sys.path.insert(0, str(REPO / "tests"))

from run_qemu import run_commands  # noqa: E402

# Each pattern is searched independently in the full output.  Sorted
# alphabetically rather than in emit order — the engine prints them
# sequentially but the test treats them as a set.
EXPECTED = [
    r"\[bboeos doom\] DG_Init",
    r"Doom Generic",
    r"IWAD file 'doom1\.wad' not found",
    r"Z_Init: Init zone memory allocation daemon",
    r"zone memory: 0x[0-9a-f]+, 600000 allocated for zone",
]


def _build_doom() -> None:
    """Run tools/build_doom.py to produce build/doom/doom."""
    subprocess.check_call([sys.executable, str(REPO / "tools" / "build_doom.py")])


def _build_image_and_install() -> None:
    """Build a fresh ext2 drive image and add bin/doom to it."""
    subprocess.check_call(["./make_os.sh", "--ext2"], cwd=REPO)
    subprocess.check_call(
        ["./add_file.py", "-x", "-d", "bin", str(DOOM_BINARY)],
        cwd=REPO,
    )


def main() -> None:
    """Build doom + image, run via run_commands, verify bootstrap markers."""
    _build_doom()
    _build_image_and_install()
    result = run_commands(["doom"], memory="64", command_timeout=30)
    output = result.output.replace("\r", "")
    for pattern in EXPECTED:
        if not re.search(pattern, output):
            print(f"MISSING: {pattern}")
            print("--- output ---")
            print(output)
            sys.exit(1)
    print("doom bootstrap smoke test pass")


if __name__ == "__main__":
    main()
