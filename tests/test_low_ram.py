#!/usr/bin/env python3
"""Smoke-test the OS booting under QEMU's smallest configuration.

The kernel's reserved region (image + stack + NIC bufs +
program_scratch + boot PD + first kernel PT) is sized to fit under
the VGA aperture at 0xA0000 so the OS can boot with only conventional
memory present.  This test pins the contract: if a future change pushes
the reserved region across the VGA hole or makes the system depend on
extended RAM during early bring-up, this run will fail.

Usage:
    tests/test_low_ram.py
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
    """Build the OS image, boot under -m 1, run date + ls."""
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    result = run_commands(["date", "ls"], memory="1", snapshot=True)
    output = result.output

    failures: list[str] = []
    if DATE_RE.search(output) is None:
        failures.append("date output not found (expected YYYY-MM-DD HH:MM:SS)")
    if "bin/" not in output:
        failures.append("ls output missing 'bin/' entry")
    if output.count("$ ") < 2:
        failures.append(f"expected at least 2 shell prompts, got {output.count('$ ')}")

    if failures:
        print("FAIL  test_low_ram")
        for failure in failures:
            print(f"  - {failure}")
        print("--- captured serial output ---")
        print(output)
        return 1

    print("PASS  test_low_ram — boot, date, ls all worked under -m 1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
