#!/usr/bin/env python3
"""PS/2 keyboard driver smoke test.

Boots BBoeOS, waits for the shell prompt, then injects keys via the
QEMU monitor's `sendkey` command.  Captures the serial console and
checks that typed characters reach the shell — this is the only path
that actually exercises the native PS/2 driver (the other tests feed
input through the serial fifo and skip the keyboard layer entirely).

Run standalone:
    tests/test_ps2.py
"""

from __future__ import annotations

import argparse
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BOOT_TIMEOUT = 15
DRAIN_AFTER_KEYS = 2.0
DRIVE = Path(__file__).resolve().parent.parent / "drive.img"
MONITOR_APPEAR_TIMEOUT = 5
PROMPT = b"$ "


def _wait_path(*, path: Path, timeout: float) -> None:
    """Block until `path` exists, or raise RuntimeError on timeout."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            message = f"{path} never appeared within {timeout}s"
            raise RuntimeError(message)
        time.sleep(0.05)


def _drain(*, buffer: bytearray, file_descriptor: int, seconds: float) -> None:
    """Read any bytes available from `file_descriptor` into `buffer`."""
    cutoff = time.monotonic() + seconds
    while time.monotonic() < cutoff:
        ready, _, _ = select.select([file_descriptor], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(file_descriptor, 4096)
        except BlockingIOError:
            continue
        if chunk:
            buffer.extend(chunk)


def _wait_for_prompt(*, buffer: bytearray, file_descriptor: int) -> None:
    """Drain output until the shell prompt appears or boot times out."""
    deadline = time.monotonic() + BOOT_TIMEOUT
    while PROMPT not in buffer:
        if time.monotonic() > deadline:
            message = f"no shell prompt within {BOOT_TIMEOUT}s; output={bytes(buffer)!r}"
            raise RuntimeError(message)
        _drain(buffer=buffer, file_descriptor=file_descriptor, seconds=0.1)


def _send_keys(*, monitor_path: Path, keys: list[str]) -> None:
    """Send each `sendkey <name>` command to the QEMU monitor."""
    monitor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    monitor.connect(str(monitor_path))
    time.sleep(0.1)
    monitor.recv(4096)  # drain banner
    for key in keys:
        monitor.sendall(f"sendkey {key}\n".encode())
        time.sleep(0.05)
    monitor.close()


def _launch_qemu(*, floppy: bool, serial_base: Path, monitor_path: Path) -> subprocess.Popen:
    """Start QEMU with a serial fifo and a unix-socket monitor."""
    return subprocess.Popen([
        "qemu-system-i386",
        "-drive",
        f"file={DRIVE},format=raw,snapshot=on" + (",if=floppy" if floppy else ""),
        "-chardev",
        f"pipe,id=s,path={serial_base}",
        "-serial",
        "chardev:s",
        "-display",
        "none",
        "-monitor",
        f"unix:{monitor_path},server,nowait",
    ])


def inject_keys(*, expected: bytes, floppy: bool, keys: list[str]) -> None:
    """Boot BBoeOS, send `keys` via the monitor, expect `expected` on serial."""
    with tempfile.TemporaryDirectory(prefix="test_ps2_") as temp_dir:
        serial_base = Path(temp_dir) / "ser"
        monitor_path = Path(temp_dir) / "mon"
        os.mkfifo(f"{serial_base}.in")
        os.mkfifo(f"{serial_base}.out")

        qemu = _launch_qemu(floppy=floppy, serial_base=serial_base, monitor_path=monitor_path)
        output_fd: int | None = None
        try:
            _wait_path(path=monitor_path, timeout=MONITOR_APPEAR_TIMEOUT)
            output_fd = os.open(f"{serial_base}.out", os.O_RDONLY | os.O_NONBLOCK)
            buffer = bytearray()
            _wait_for_prompt(buffer=buffer, file_descriptor=output_fd)
            pre_len = len(buffer)
            _send_keys(monitor_path=monitor_path, keys=keys)
            _drain(buffer=buffer, file_descriptor=output_fd, seconds=DRAIN_AFTER_KEYS)
            new_output = bytes(buffer[pre_len:])
            if expected not in new_output:
                message = f"expected {expected!r} in output, got {new_output!r}"
                raise AssertionError(message)
            print(f"PASS: {' '.join(keys)} -> {expected!r}")
        finally:
            if output_fd is not None:
                os.close(output_fd)
            qemu.kill()
            qemu.wait(timeout=5)


def main() -> int:
    """Run the PS/2 smoke cases: unshifted and shifted letter injection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--floppy",
        action="store_true",
        help="boot QEMU with the drive attached as a floppy (if=floppy)",
    )
    arguments = parser.parse_args()
    inject_keys(
        expected=b"hi\r\n",
        floppy=arguments.floppy,
        keys=["e", "c", "h", "o", "spc", "h", "i", "ret"],
    )
    inject_keys(
        expected=b"Hi\r\n",
        floppy=arguments.floppy,
        keys=["e", "c", "h", "o", "spc", "shift-h", "i", "ret"],
    )
    print("2 passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
