#!/usr/bin/env python3
"""Smoke tests for SYS_IO_DUP and SYS_IO_DUP2.

Boots the OS in QEMU per case, drives the shell over the serial fifo,
and asserts the test program's output.  Mirrors test_shell_chain.py's
single-boot-per-case structure.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402


def _run(*, command: str) -> bytes:
    with qemu_session(monitor=False, snapshot=True) as session:
        pre = len(session.buffer)
        session.send_command(command)
        # Strip the echo line.
        full = bytes(session.buffer[pre:])
        crlf = full.find(b"\r\n")
        return full[crlf + 2 :] if crlf >= 0 else full


def test_dup2_closes_target_first() -> None:
    """dup2 over an existing fd closes the old contents and reuses the slot."""
    out = _run(command="fd_helpers dup2_close_target")
    assert b"dup2_close_ok" in out, f"got {out!r}"
    print("PASS: test_dup2_closes_target_first")


def test_dup2_self_is_noop() -> None:
    """dup2(N, N) returns N without side effects (Linux semantics)."""
    out = _run(command="fd_helpers dup2_self")
    assert b"dup2_self_ok" in out, f"got {out!r}"
    print("PASS: test_dup2_self_is_noop")


def test_dup_console_writes() -> None:
    """Verify dup(1) returns a usable fd and writes to the same console."""
    out = _run(command="fd_helpers dup_console")
    assert b"dup_ok" in out, f"fd_helpers dup_console must emit dup_ok; got {out!r}"
    print("PASS: test_dup_console_writes")


def test_dup_refuses_vga() -> None:
    """Dup of /dev/vga must fail with ERROR_INVALID (singleton-opener)."""
    out = _run(command="fd_helpers dup_vga")
    assert b"dup_vga_refused" in out, f"got {out!r}"
    print("PASS: test_dup_refuses_vga")


def main() -> int:
    """Build the OS image and run all dup smoke tests."""
    subprocess.run(["./make_os.sh"], check=True, cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    test_dup2_closes_target_first()
    test_dup2_self_is_noop()
    test_dup_console_writes()
    test_dup_refuses_vga()
    print("4 passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
