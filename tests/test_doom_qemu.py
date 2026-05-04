#!/usr/bin/env python3
"""On-OS Doom smoke tests.

Two test stages:

  1. ``_test_bootstrap_no_wad`` — always runs.  Boots Doom on an ext2
     image with no WAD provided; asserts the engine reaches IWAD
     lookup and exits cleanly.  Covers libc + compiler-rt + the
     bboeos DG_* backend's init path.

  2. ``_test_main_loop_with_wad`` — runs only when wads/doom1.wad is
     present (fetched via tools/fetch_wad.sh, gitignored).  Boots
     Doom on a 10 MB ext2 image with the WAD and bin/doom installed;
     asserts the engine completes init and the main loop ticks at
     least 30 frames.  Covers the runtime DG_DrawFrame / DG_GetTicksMs
     / DG_SleepMs path end-to-end.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOOM_BINARY = REPO / "build" / "doom" / "doom"
WAD_FILE = REPO / "wads" / "doom1.wad"

sys.path.insert(0, str(REPO / "tests"))

from run_qemu import qemu_session, run_commands  # noqa: E402

# Stage 1: bootstrap (no WAD).  Each pattern is searched independently
# in the full output and matched as a set.
BOOTSTRAP_EXPECTED = [
    r"\[bboeos doom\] DG_Init",
    r"Doom Generic",
    r"IWAD file 'doom1\.wad' not found",
    r"Z_Init: Init zone memory allocation daemon",
    r"zone memory: 0x[0-9a-f]+, 600000 allocated for zone",
]

# Stage 2: main loop (with WAD).  Bootstrap markers + engine init +
# graphics setup + DG_DrawFrame frame counter.
MAIN_LOOP_EXPECTED = [
    r"\[bboeos doom\] DG_Init",
    r"\[bboeos doom\] VGA mapped at 0xb8000000",
    r"\[bboeos doom\] frame 1",
    r"\[bboeos doom\] frame 30",
    r" adding doom1\.wad",
    r"DOOM Shareware",
    r"HU_Init: Setting up heads up display",
    r"I_Init: Setting up machine state",
    r"I_InitGraphics: Auto-scaling factor: 1",
    r"P_Init: Init Playloop state",
    r"R_Init: Init DOOM refresh daemon",
    r"S_Init: Setting up sound",
]


def _assert_markers(*, expected: list[str], label: str, output: str) -> None:
    """Fail the test if any expected pattern is missing from the output."""
    missing = [pattern for pattern in expected if not re.search(pattern, output)]
    if missing:
        for pattern in missing:
            print(f"MISSING [{label}]: {pattern}")
        print("--- output ---")
        print(output)
        sys.exit(1)


def _build_doom() -> None:
    """Run tools/build_doom.py to produce build/doom/doom."""
    subprocess.check_call([sys.executable, str(REPO / "tools" / "build_doom.py")])


def _install_doom_and_wad() -> None:
    """Build a 10 MB ext2 image with bin/doom + doom1.wad at the root."""
    _install_doom_only(sectors="20480")
    subprocess.check_call(
        ["./add_file.py", "--image", "drive.img", str(WAD_FILE)],
        cwd=REPO,
    )


def _install_doom_only(*, sectors: str = "2880") -> None:
    """Build a fresh ext2 drive image and add bin/doom (no WAD)."""
    subprocess.check_call(["./make_os.sh", "--ext2", f"--sectors={sectors}"], cwd=REPO)
    subprocess.check_call(
        ["./add_file.py", "-x", "-d", "bin", str(DOOM_BINARY)],
        cwd=REPO,
    )


def _test_bootstrap_no_wad() -> None:
    """Run doom on an image with no WAD; expect IWAD-not-found path."""
    _install_doom_only()
    result = run_commands(["doom"], memory="64", command_timeout=30)
    _assert_markers(expected=BOOTSTRAP_EXPECTED, label="bootstrap", output=result.output.replace("\r", ""))
    print("doom bootstrap (no WAD) pass")


def _test_main_loop_with_wad() -> None:
    """Run doom with WAD; expect init complete + main loop ticking.

    Doom's main loop never exits (DG_GetKey is a stub so no quit key
    arrives), so we boot QEMU directly via qemu_session, send `doom`,
    drain serial for a fixed window, then assert markers against the
    full captured buffer.  Skipped when wads/doom1.wad is missing.
    """
    if not WAD_FILE.is_file():
        print(f"SKIP main-loop test: {WAD_FILE} not present (run tools/fetch_wad.sh)")
        return
    _install_doom_and_wad()
    with qemu_session(memory="64") as session:
        # Type the command + enter, then let the engine run for 20 s
        # so we see the bootstrap prints + at least frame 30.
        session.write_serial("doom\r")
        session.drain_serial(seconds=20)
    output = session.buffer.decode(errors="replace").replace("\r", "")
    _assert_markers(expected=MAIN_LOOP_EXPECTED, label="main_loop", output=output)
    print("doom main-loop (with WAD) pass")


def main() -> None:
    """Build doom, run both stages."""
    _build_doom()
    _test_bootstrap_no_wad()
    _test_main_loop_with_wad()


if __name__ == "__main__":
    main()
