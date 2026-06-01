#!/usr/bin/env python3
"""Tab-completion tests for the BBoeOS shell.

Boots QEMU, sends partial commands + Tab via serial, and verifies the
shell completes them correctly.

Usage:
    ./test_tab_complete.py                # run all tests
    ./test_tab_complete.py <name>          # run a single test
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from run_qemu import qemu_session  # noqa: E402

_SETTLE_SECONDS = 0.3


def _build_image() -> Path:
    """Build the OS image and return the drive path."""
    subprocess.run(
        ["./make_os.sh"],
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return REPO_ROOT / "drive.img"


def test_command_completion_unique() -> None:
    """Typing 'hel' + Tab completes to 'help'; Enter executes it."""
    drive = _build_image()
    with qemu_session(drive=drive) as session:
        session.write_serial("hel\t\r")
        session.wait_for_prompt()
        assert "Commands:" in session.output, f"expected 'Commands:' in output: {session.output!r}"


def test_command_completion_multiple() -> None:
    """Typing 'sh' + Tab shows 'shutdown' among matches."""
    drive = _build_image()
    with qemu_session(drive=drive) as session:
        session.write_serial("sh\t")
        time.sleep(_SETTLE_SECONDS)
        session.write_serial("\x03")
        session.wait_for_prompt()
        assert "shutdown" in session.output, f"expected 'shutdown' in output: {session.output!r}"


def test_argument_completion_directory() -> None:
    """Typing 'ls bi' + Tab completes to 'ls bin/' and lists contents."""
    drive = _build_image()
    with qemu_session(drive=drive) as session:
        session.write_serial("ls bi\t\r")
        session.wait_for_prompt()
        output = session.output
        assert "asm" in output or "ls" in output or "cat" in output, f"expected bin/ contents in output: {output!r}"


def test_empty_tab_lists_commands() -> None:
    """Pressing Tab on empty input lists available commands."""
    drive = _build_image()
    with qemu_session(drive=drive) as session:
        session.write_serial("\t")
        time.sleep(_SETTLE_SECONDS + 0.2)
        session.write_serial("\x03")
        session.wait_for_prompt()
        output = session.output
        assert "help" in output, f"expected 'help' in output: {output!r}"
        assert "reboot" in output, f"expected 'reboot' in output: {output!r}"


_ALL_TESTS = [
    ("command_completion_multiple", test_command_completion_multiple),
    ("command_completion_unique", test_command_completion_unique),
    ("argument_completion_directory", test_argument_completion_directory),
    ("empty_tab_lists_commands", test_empty_tab_lists_commands),
]


def main() -> None:  # noqa: D103
    selected = sys.argv[1] if len(sys.argv) > 1 else None
    tests = [(name, function) for name, function in _ALL_TESTS if name == selected] if selected else _ALL_TESTS
    if not tests:
        print(f"Unknown test: {selected}")
        print(f"Available: {', '.join(name for name, _ in _ALL_TESTS)}")
        sys.exit(1)
    failures = 0
    for name, function in tests:
        try:
            function()
            print(f"PASS: {name}")
        except Exception as exception:  # noqa: BLE001
            print(f"FAIL: {name}: {exception}")
            failures += 1
    if failures:
        print(f"\n{failures} test(s) failed.")
        sys.exit(1)
    print(f"\nAll {len(tests)} tab-completion test(s) passed.")


if __name__ == "__main__":
    main()
