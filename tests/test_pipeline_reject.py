#!/usr/bin/env python3
"""Shell rejects pipeline syntax that v1 doesn't support."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402


def _run(*, command: str, timeout: float = 8.0) -> bytes:
    with qemu_session(monitor=False, snapshot=True, boot_timeout=10.0) as session:
        pre = len(session.buffer)
        session.write_serial(command + "\r")
        with contextlib.suppress(TimeoutError):
            session.wait_for_substring(b"$ ", start=pre, timeout=timeout)
        return bytes(session.buffer[pre:])


def test_double_pipe_rejected() -> None:
    """`a | b | c` is rejected at parse time."""
    out = _run(command="pipe_producer | pipe_consumer | pipe_consumer")
    assert b"only one |" in out or b"pipelines support" in out, f"expected double-pipe rejection message; got {out!r}"
    print("PASS: test_double_pipe_rejected")


def test_pipe_with_redirect_rejected() -> None:
    """`a > file | b` is rejected at parse time (v1 limitation)."""
    out = _run(command="pipe_producer > /tmp.txt | pipe_consumer")
    assert b"cannot combine" in out or b"redirect" in out, f"expected pipe+redirect rejection message; got {out!r}"
    print("PASS: test_pipe_with_redirect_rejected")


if __name__ == "__main__":
    test_double_pipe_rejected()
    test_pipe_with_redirect_rejected()
