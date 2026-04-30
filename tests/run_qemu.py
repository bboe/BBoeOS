#!/usr/bin/env python3
"""Boot bboeos in QEMU, run shell commands, and capture the serial output.

Useful for manual smoke tests and future automated tests — hides the
fifo/timeout plumbing behind a simple API and CLI.

Module API:
    from run_qemu import run_commands
    output = run_commands(["netinit"], with_net=True)

CLI:
    ./run_qemu.py netinit
    ./run_qemu.py --net netinit
    ./run_qemu.py --timeout 15 "echo hello" "uptime"
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BOOT_TIMEOUT = float(os.environ.get("BBOE_BOOT_TIMEOUT", "2"))
COMMAND_TIMEOUT = 4
DEFAULT_IMAGE = Path(__file__).resolve().parent.parent / "drive.img"
PROMPT = b"$ "
SERIAL_BASENAME = "ser"


@dataclasses.dataclass
class QemuResult:
    """Timing and output from a QEMU session."""

    boot_time: float
    command_times: list[float]
    output: str


def run_commands(
    commands: list[str],
    *,
    boot_timeout: float = BOOT_TIMEOUT,
    command_timeout: float = COMMAND_TIMEOUT,
    drive: Path = DEFAULT_IMAGE,
    floppy: bool = False,
    machine: str | None = None,
    memory: str | None = None,
    pcap: Path | None = None,
    retry: bool = True,
    snapshot: bool = False,
    with_net: bool = False,
) -> QemuResult:
    """Boot QEMU, run each command, return a :class:`QemuResult`.

    QEMU is always killed when this returns (normal or error path). The shell
    prompt ('$ ') is used as the synchronisation marker: the function returns
    after it has seen the prompt once per command.

    *floppy* attaches the drive image as the primary floppy (``if=floppy``)
    instead of the default IDE/HDD attachment — boots route through
    INT 13h's floppy path in the BIOS and through ``fdc_*`` post-flip,
    which is the harder path to keep working as the kernel evolves.

    *memory* (e.g. ``"16M"``) appends ``-m <memory>`` to QEMU; *machine*
    (e.g. ``"acpi=off"``) appends ``-machine <machine>``.  Both fall back
    to ``BBOE_QEMU_MEMORY`` / ``BBOE_QEMU_MACHINE`` env vars when unset,
    so the self-review driver can sweep configurations without per-script
    CLI plumbing.  Defaults are still ``None`` (no flag) — existing callers
    keep QEMU's defaults.

    When *retry* is True (the default) and a TimeoutError occurs, the entire
    QEMU session is retried once with 50% more time for both boot and command
    timeouts.  A second timeout raises immediately.
    """
    if memory is None:
        memory = os.environ.get("BBOE_QEMU_MEMORY") or None
    if machine is None:
        machine = os.environ.get("BBOE_QEMU_MACHINE") or None
    try:
        return _run_commands_once(
            commands,
            boot_timeout=boot_timeout,
            command_timeout=command_timeout,
            drive=drive,
            floppy=floppy,
            machine=machine,
            memory=memory,
            pcap=pcap,
            snapshot=snapshot,
            with_net=with_net,
        )
    except TimeoutError:
        if not retry:
            raise
        return _run_commands_once(
            commands,
            boot_timeout=boot_timeout * 1.5,
            command_timeout=command_timeout * 1.5,
            drive=drive,
            floppy=floppy,
            machine=machine,
            memory=memory,
            pcap=pcap,
            snapshot=snapshot,
            with_net=with_net,
        )


_PROMPT_SETTLE_SECONDS = 0.05


def _drain_until_idle(*, buffer: bytearray, file_descriptor: int, settle_seconds: float) -> None:
    """Append any pending bytes to `buffer` until `settle_seconds` of silence."""
    deadline = time.monotonic() + settle_seconds
    while time.monotonic() < deadline:
        ready, _, _ = select.select([file_descriptor], [], [], settle_seconds)
        if not ready:
            return
        try:
            chunk = os.read(file_descriptor, 4096)
        except BlockingIOError:
            return
        if not chunk:
            return
        buffer.extend(chunk)
        deadline = time.monotonic() + settle_seconds


def _run_commands_once(
    commands: list[str],
    *,
    boot_timeout: float,
    command_timeout: float,
    drive: Path,
    floppy: bool,
    machine: str | None,
    memory: str | None,
    pcap: Path | None,
    snapshot: bool,
    with_net: bool,
) -> QemuResult:
    """Single-attempt implementation of run_commands."""
    with tempfile.TemporaryDirectory(prefix="run_qemu_") as temp_dir:
        temporary_directory = Path(temp_dir)
        serial_base = temporary_directory / SERIAL_BASENAME
        os.mkfifo(f"{serial_base}.in")
        os.mkfifo(f"{serial_base}.out")

        drive_spec = f"file={drive},format=raw"
        if floppy:
            drive_spec += ",if=floppy"
        if snapshot:
            drive_spec += ",snapshot=on"
        qemu_args = [
            "qemu-system-i386",
            "-chardev",
            f"pipe,id=s,path={serial_base}",
            "-display",
            "none",
            "-drive",
            drive_spec,
            "-monitor",
            "none",
            "-serial",
            "chardev:s",
        ]
        if memory is not None:
            qemu_args += ["-m", memory]
        if machine is not None:
            qemu_args += ["-machine", machine]
        if with_net:
            qemu_args += [
                "-netdev",
                "user,id=net0",
                "-device",
                "ne2k_isa,netdev=net0,irq=3,iobase=0x300",
            ]
            if pcap is not None:
                qemu_args += ["-object", f"filter-dump,id=f0,netdev=net0,file={pcap}"]

        qemu: subprocess.Popen | None = None
        output_fd: int | None = None
        buffer = bytearray()
        command_times: list[float] = []
        try:
            qemu = subprocess.Popen(qemu_args)
            output_fd = os.open(f"{serial_base}.out", os.O_RDONLY | os.O_NONBLOCK)

            boot_start = time.monotonic()
            _wait_for_prompt(
                buffer=buffer,
                file_descriptor=output_fd,
                process=qemu,
                timeout=boot_timeout,
            )
            boot_time = time.monotonic() - boot_start

            input_path = Path(f"{serial_base}.in")
            for command in commands:
                command_start = time.monotonic()
                input_path.write_text(command + "\r", encoding="utf-8")
                _wait_for_prompt(
                    buffer=buffer,
                    file_descriptor=output_fd,
                    process=qemu,
                    timeout=command_timeout,
                )
                command_times.append(time.monotonic() - command_start)
        finally:
            if output_fd is not None:
                os.close(output_fd)
            if qemu is not None:
                _terminate(process=qemu)
        return QemuResult(
            boot_time=boot_time,
            command_times=command_times,
            output=buffer.decode(errors="replace"),
        )


def _terminate(*, process: subprocess.Popen) -> None:
    """Kill the QEMU process and wait for it to exit."""
    if process.poll() is not None:
        return
    process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)


def _wait_for_prompt(
    *,
    buffer: bytearray,
    file_descriptor: int,
    process: subprocess.Popen,
    timeout: float,
) -> None:
    """Drain the output fifo into `buffer` until `PROMPT` appears, then settle.

    After the first PROMPT match, keep draining for a short window so
    back-to-back prompts (e.g. shell consuming a stray carriage return as
    an empty command after a program that itself swallowed an inline
    byte) all land in the buffer before this returns.  Without the
    settle, the next command's prompt_start sits between the two prompts
    and the spurious one falsely satisfies the next wait — masking
    whether the actual command produced any output.
    """
    prompt_start = len(buffer)
    deadline = time.monotonic() + timeout
    while PROMPT not in buffer[prompt_start:]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            message = f"timed out waiting for shell prompt after {timeout}s"
            raise TimeoutError(message)
        if process.poll() is not None:
            message = f"qemu exited with {process.returncode} before prompt appeared"
            raise RuntimeError(message)
        ready, _, _ = select.select([file_descriptor], [], [], min(remaining, 0.1))
        if not ready:
            continue
        try:
            chunk = os.read(file_descriptor, 4096)
        except BlockingIOError:
            continue
        if not chunk:
            time.sleep(0.01)
            continue
        buffer.extend(chunk)
    _drain_until_idle(buffer=buffer, file_descriptor=file_descriptor, settle_seconds=_PROMPT_SETTLE_SECONDS)


def main() -> int:
    """CLI entry point: run each positional argument as a shell command."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("commands", nargs="+", help="shell command to run")
    parser.add_argument(
        "--boot-timeout",
        type=float,
        default=BOOT_TIMEOUT,
        help=f"seconds to wait for initial boot prompt (default: {BOOT_TIMEOUT})",
    )
    parser.add_argument(
        "--drive",
        type=Path,
        default=DEFAULT_IMAGE,
        help=f"path to drive image (default: {DEFAULT_IMAGE})",
    )
    parser.add_argument(
        "--floppy",
        action="store_true",
        help="attach drive as primary floppy (if=floppy) instead of IDE",
    )
    parser.add_argument(
        "--machine",
        type=str,
        default=None,
        help="value for -machine (e.g. 'acpi=off'); falls back to $BBOE_QEMU_MACHINE",
    )
    parser.add_argument(
        "--memory",
        type=str,
        default=None,
        help="value for -m (e.g. '32M'); falls back to $BBOE_QEMU_MEMORY",
    )
    parser.add_argument(
        "--net",
        action="store_true",
        help="attach NE2000 NIC (user-mode networking)",
    )
    parser.add_argument(
        "--pcap",
        type=Path,
        default=None,
        help="capture NIC traffic to this pcap file (requires --net)",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="discard drive writes on exit (no persistence across runs)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=COMMAND_TIMEOUT,
        help=f"per-command timeout in seconds (default: {COMMAND_TIMEOUT})",
    )
    arguments = parser.parse_args()
    result = run_commands(
        arguments.commands,
        boot_timeout=arguments.boot_timeout,
        command_timeout=arguments.timeout,
        drive=arguments.drive,
        floppy=arguments.floppy,
        machine=arguments.machine,
        memory=arguments.memory,
        pcap=arguments.pcap,
        snapshot=arguments.snapshot,
        with_net=arguments.net,
    )
    sys.stdout.write(result.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
