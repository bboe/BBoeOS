#!/usr/bin/env python3
"""Pipeline regression: consumer exits while producer is parked on a full pipe.

`pipe_spam | pipe_drain` writes 16 KB through a 4 KB pipe ring, so
pipe_spam blocks inside `kernel_yield_write` after the first batch.
pipe_drain reads one byte and `exit(9)`s, which:
  1. wakes pipe_spam (slot_b) via `pipe_decrement_reader` →
     `pipe_wake_writer` during slot_c's `fd_close` loop in
     `child_terminate`, then
  2. cooperatively schedules slot_b again via
     `kernel_yield(STATE_EXITED)`.

A prior bug left slot_b's EBP register holding the kernel address of
slot_c's `.sys_exit` syscall dispatcher (syscall_handler's
`mov ebp, [.table + eax*4]; jmp ebp` dispatch clobbers EBP, and
`kernel_yield` did not preserve callee-saved registers across the
context switch).  `fd_write_pipe`'s `mov esp, ebp; pop ebp; ret`
epilogue then jumped through that kernel code address and the CPU
faulted on an instruction-fetch at an unmapped user-virt address —
visible as `EXC0E EIP=<garbage> CR2=<same> ERR=00000000` on the
serial console.

This test asserts the prompt comes back without any `EXC0E` line.
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


def test_pipeline_early_close() -> None:
    """`pipe_spam | pipe_drain` must return to the prompt without an EXC0E."""
    out = _run(command="pipe_spam | pipe_drain")
    assert b"EXC0E" not in out, f"unexpected EXC0E in output: {out!r}"
    assert b"$ " in out, f"prompt did not return; got {out!r}"
    print("PASS: test_pipeline_early_close")


if __name__ == "__main__":
    test_pipeline_early_close()
