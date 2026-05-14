#!/usr/bin/env python3
"""Pipeline children can receive argv tails.

Exercises the four-arg SYS_SYS_PIPELINE2 ABI: the shell tokenises each
pipeline side into its own ``char **`` argv array and hands both to the
kernel, which walks them under the shell's PD, copies the strings into
per-side argv scratch, and writes a Linux SysV i386 startup frame
(argc / argv / NULL / empty envp) onto each child's user stack before
iretd.  pipe_producer takes a `bulk` arg (16 KB) and an `early` arg
(1 byte + exit 7) — both exercised here through the shell pipeline so
a regression in the arg-plumbing surfaces as a wrong byte count or a
wrong wait status.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402


def _run(*, command: str, timeout: float = 15.0) -> bytes:
    with qemu_session(monitor=False, snapshot=True, boot_timeout=10.0) as session:
        pre = len(session.buffer)
        session.write_serial(command + "\r")
        with contextlib.suppress(TimeoutError):
            session.wait_for_substring(b"$ ", start=pre, timeout=timeout)
        return bytes(session.buffer[pre:])


def test_pipeline_bulk_args() -> None:
    """`pipe_producer bulk | pipe_consumer` prints 16384 (full ring)."""
    out = _run(command="pipe_producer bulk | pipe_consumer")
    assert b"16384" in out, f"expected '16384' in output; got {out!r}"
    print("PASS: test_pipeline_bulk_args")


def test_pipeline_early_args() -> None:
    """`pipe_producer early | pipe_consumer` prints 1 (one byte read)."""
    out = _run(command="pipe_producer early | pipe_consumer")
    assert b"\r\n1\r\n" in out or b"\n1\n" in out, f"expected solitary '1' line in output; got {out!r}"
    print("PASS: test_pipeline_early_args")


if __name__ == "__main__":
    test_pipeline_bulk_args()
    test_pipeline_early_args()
