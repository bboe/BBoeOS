#!/usr/bin/env python3
"""Shell command-history smoke test.

Boots BBoeOS, runs two commands, then sends Up + Enter via the serial
fifo and asserts the recalled command's output appears.  Validates both
the CSI parser and the ring storage.

Run standalone:
    tests/test_shell_history.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402


def assert_in(*, expected: bytes, haystack: bytes, label: str) -> None:
    """Raise AssertionError if *expected* is not found in *haystack*."""
    if expected not in haystack:
        message = f"{label}: expected {expected!r} in {haystack!r}"
        raise AssertionError(message)


def test_up_recalls_previous_command() -> None:
    """Up arrow after two commands recalls the most recent, then the older."""
    with qemu_session(monitor=False, snapshot=True) as session:
        session.send_command("echo first")
        session.send_command("echo second")
        # Send Up + Enter via serial fifo.  Cooked stream sees ESC [ A
        # exactly as the PS/2 driver would emit on Up press.
        pre_length = len(session.buffer)
        session.write_serial("\x1b[A\r")
        session.wait_for_prompt()
        between = bytes(session.buffer[pre_length:])
        assert_in(expected=b"echo second", haystack=between, label="recalled line should be re-printed by line editor")
        assert_in(expected=b"second\r\n", haystack=between, label="recalled command should produce its output")
        # Up + Up + Enter recalls the older entry.
        pre_length = len(session.buffer)
        session.write_serial("\x1b[A\x1b[A\r")
        session.wait_for_prompt()
        between = bytes(session.buffer[pre_length:])
        assert_in(expected=b"first\r\n", haystack=between, label="Up Up should recall the older command")
    print("PASS: test_up_recalls_previous_command")


def test_down_at_live_line_is_noop() -> None:
    """Down arrow at the live (empty) line is a no-op and does not recall anything."""
    with qemu_session(monitor=False, snapshot=True) as session:
        session.send_command("echo only")
        pre_length = len(session.buffer)
        # Down at an empty live line is a no-op; Enter then runs an
        # empty command which the shell silently re-prompts on.
        session.write_serial("\x1b[B\r")
        session.wait_for_prompt()
        between = bytes(session.buffer[pre_length:])
        if b"only" in between:
            message = f"Down should not recall anything; got {between!r}"
            raise AssertionError(message)
    print("PASS: test_down_at_live_line_is_noop")


def test_down_restores_partial_line() -> None:
    """Down past newest entry restores the partial line typed before Up."""
    with qemu_session(monitor=False, snapshot=True) as session:
        session.send_command("echo committed")
        # Type a partial line, browse Up, browse back Down to the live
        # line, then complete it with " typed" + Enter.  Expect the
        # full "echo partial typed" output.
        pre_length = len(session.buffer)
        session.write_serial("echo partial")
        session.write_serial("\x1b[A")  # up — should snapshot "echo partial"
        session.write_serial("\x1b[B")  # down — should restore "echo partial"
        session.write_serial(" typed\r")
        session.wait_for_prompt()
        between = bytes(session.buffer[pre_length:])
        assert_in(expected=b"partial typed\r\n", haystack=between, label="Down past newest should restore the saved partial line")
    print("PASS: test_down_restores_partial_line")


def main() -> int:
    """Build the OS image and run all history smoke tests."""
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    test_up_recalls_previous_command()
    test_down_at_live_line_is_noop()
    test_down_restores_partial_line()
    print("3 passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
