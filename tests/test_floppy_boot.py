#!/usr/bin/env python3
"""Verify the OS boots and runs a representative command from a floppy.

The default test matrix attaches `drive.img` as an IDE/HDD device
(`format=raw`).  All disk reads then go through `ata_read_sector`,
and the floppy code path (`fdc_init` reset → SENSE_INT x 4 → SPECIFY,
plus `fdc_motor_start`'s timer-driven spin-up wait, plus
`fdc_read_sector`'s DMA + IRQ 6 sequence) is exercised only by
the `if=floppy` invocation.  That path was silently broken for
some time before this test landed because nothing covered it; this
test makes the next regression loud.

Boot from floppy and run two commands:

  - ``date``   — exercises bbfs/ext2 read_sector → fdc_read_sector
                 (the program load) plus rtc_read_epoch (CMOS reads,
                 unrelated to FDC but a useful liveness check)
  - ``ls``     — exercises a directory walk on the floppy filesystem

Pass condition: both commands return their shell prompt within the
default per-command timeout, and ``date``'s output contains a
plausible YYYY-MM-DD prefix.

Usage:
    tests/test_floppy_boot.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import run_commands  # noqa: E402

DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\b")


def main() -> int:
    """Build the OS image, boot from floppy, run date + ls."""
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    result = run_commands(["date", "ls"], floppy=True, snapshot=True)
    output = result.output

    failures: list[str] = []
    if DATE_RE.search(output) is None:
        failures.append("date output not found (expected YYYY-MM-DD HH:MM:SS)")
    if "bin/" not in output:
        failures.append("ls output missing 'bin/' entry")
    if output.count("$ ") < 2:
        failures.append(f"expected at least 2 shell prompts, got {output.count('$ ')}")

    if failures:
        print("FAIL  test_floppy_boot")
        for failure in failures:
            print(f"  - {failure}")
        print("--- captured serial output ---")
        print(output)
        return 1

    print("PASS  test_floppy_boot — boot, date, ls all worked from floppy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
