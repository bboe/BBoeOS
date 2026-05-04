#!/usr/bin/env python3
"""Boot bboeos in QEMU, run shell commands, and capture the serial output.

Useful for manual smoke tests and future automated tests — hides the
fifo/timeout plumbing behind a simple API and CLI.

Module API:
    from run_qemu import run_commands
    output = run_commands(["arp 10.0.2.2"], with_net=True)

CLI:
    ./run_qemu.py uptime
    ./run_qemu.py --net "arp 10.0.2.2"
    ./run_qemu.py --timeout 15 "echo hello" "uptime"
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

BOOT_TIMEOUT = float(os.environ.get("BBOE_BOOT_TIMEOUT", "2"))
COMMAND_TIMEOUT = 4
DEFAULT_IMAGE = Path(__file__).resolve().parent.parent / "drive.img"
MONITOR_PROMPT = b"(qemu) "
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

    *memory* (e.g. ``"16M"``) appends ``-m <memory>`` to QEMU.  Defaults
    to ``"1"`` (1 MB) — the OS's minimum-RAM target — so most tests run
    against the same low-RAM configuration the kernel is sized for.
    Tests that need more (e.g. test_kernel_cc 's cc-on-host stages, or
    workloads that load large blobs into extended RAM) pass an explicit
    value.  An unset memory falls back to ``BBOE_QEMU_MEMORY`` if set,
    else ``"1"``.

    *machine* (e.g. ``"acpi=off"``) appends ``-machine <machine>``;
    falls back to ``BBOE_QEMU_MACHINE`` when unset, else no flag.  The
    env-var fallbacks let the self-review driver sweep configurations
    without per-script CLI plumbing.

    When *retry* is True (the default) and a TimeoutError occurs, the entire
    QEMU session is retried once with 50% more time for both boot and command
    timeouts.  A second timeout raises immediately.
    """
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


class QemuSession:
    """A live QEMU process driven by run_qemu.

    Wraps the serial fifo, optional unix-socket monitor, and the captured
    output buffer.  Use via :func:`qemu_session` (context-managed) so the
    process is reliably torn down.

    Attributes:
        process: the running ``qemu-system-i386`` subprocess.
        buffer: bytes received on the serial fifo so far.
        monitor_path: path to the unix-socket monitor (None if disabled).
        boot_time: seconds from process start to the first shell prompt.
        command_times: per-:meth:`send_command` round-trip durations.

    Methods that take a *timeout* raise :class:`TimeoutError` if the
    deadline elapses without the expected event.  *send_command* and
    *wait_for_prompt* both look for the shell's ``"$ "`` prompt, so they
    are safe to call only after the OS has booted (or after a previous
    command finished).

    """

    def __init__(
        self,
        *,
        monitor_path: Path | None,
        process: subprocess.Popen,
        serial_input: Path,
        serial_output_fd: int,
    ) -> None:
        """Bind a running QEMU process and its IO endpoints to a session."""
        self.process = process
        self.buffer = bytearray()
        self.monitor_path = monitor_path
        self._serial_input = serial_input
        self._serial_output_fd = serial_output_fd
        self.boot_time: float = 0.0
        self.command_times: list[float] = []

    def drain_serial(self, *, seconds: float) -> None:
        """Pull available serial bytes into :attr:`buffer` for *seconds*.

        Used for timing-sensitive sequences (e.g. test_draw's keystrokes)
        where we don't want to wait for a specific marker.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self._serial_output_fd], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(self._serial_output_fd, 4096)
            except BlockingIOError:
                return
            if chunk:
                self.buffer.extend(chunk)

    def monitor_send(self, command: str, *, timeout: float = 5.0) -> None:
        """Send a single HMP command via the monitor unix socket.

        Drains the monitor's ``"(qemu) "`` prompt before sending and waits
        for one more prompt afterwards so ``screendump`` and ``sendkey``
        return only after QEMU has actually processed the command.  The
        socket is opened and closed per-call; the monitor is a low-rate
        side channel.
        """
        if self.monitor_path is None:
            message = "monitor=False; pass monitor=True to qemu_session() first"
            raise RuntimeError(message)
        with contextlib.closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as monitor:
            monitor.settimeout(timeout)
            monitor.connect(str(self.monitor_path))
            buffer = bytearray()
            deadline = time.monotonic() + timeout
            while MONITOR_PROMPT not in buffer:
                if time.monotonic() > deadline:
                    message = f"monitor banner didn't appear within {timeout}s"
                    raise TimeoutError(message)
                with contextlib.suppress(socket.timeout):
                    chunk = monitor.recv(4096)
                    if chunk:
                        buffer.extend(chunk)
            prompt_count = buffer.count(MONITOR_PROMPT)
            monitor.sendall((command + "\n").encode())
            while buffer.count(MONITOR_PROMPT) <= prompt_count:
                if time.monotonic() > deadline:
                    message = f"monitor command {command!r} didn't echo prompt within {timeout}s"
                    raise TimeoutError(message)
                with contextlib.suppress(socket.timeout):
                    chunk = monitor.recv(4096)
                    if chunk:
                        buffer.extend(chunk)

    @property
    def output(self) -> str:
        """Captured serial output decoded as UTF-8 (lossy)."""
        return self.buffer.decode(errors="replace")

    def screendump(self, output_path: Path) -> None:
        """Take a VGA screendump via the monitor and write it to *output_path* (PPM)."""
        self.monitor_send(f"screendump {output_path}")

    def send_command(self, command: str, *, timeout: float = COMMAND_TIMEOUT) -> None:
        r"""Type *command* + ``\r`` and wait for the next prompt.  Records timing."""
        start = time.monotonic()
        self.write_serial(command + "\r")
        self.wait_for_prompt(timeout=timeout)
        self.command_times.append(time.monotonic() - start)

    def sendkey(self, key: str) -> None:
        """Send one ``sendkey <key>`` (PS/2-translated) via the monitor."""
        self.monitor_send(f"sendkey {key}")

    def wait_for_prompt(self, *, timeout: float = COMMAND_TIMEOUT) -> None:
        r"""Wait until a shell ``"$ "`` prompt appears past the current end of :attr:`buffer`.

        After matching, drains for a short settle window so back-to-back
        prompts (e.g. shell consuming a stray ``\r`` as an empty command
        after a program that itself swallowed an inline byte) all land in
        the buffer before this returns.  Without the settle, the next
        ``send_command``'s prompt_start sits between the two prompts and
        the spurious one falsely satisfies the next wait — masking
        whether the actual command produced any output.
        """
        prompt_start = len(self.buffer)
        self.wait_for_substring(PROMPT, start=prompt_start, timeout=timeout)
        _drain_until_idle(
            buffer=self.buffer,
            file_descriptor=self._serial_output_fd,
            settle_seconds=_PROMPT_SETTLE_SECONDS,
        )

    def wait_for_substring(self, needle: bytes, *, start: int = 0, timeout: float) -> None:
        """Drain the fifo into :attr:`buffer` until *needle* appears at index >= *start*."""
        deadline = time.monotonic() + timeout
        while needle not in self.buffer[start:]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                tail = bytes(self.buffer[-300:])
                message = f"never saw {needle!r} within {timeout}s; tail={tail!r}"
                raise TimeoutError(message)
            if self.process.poll() is not None:
                message = f"qemu exited with {self.process.returncode} before {needle!r} appeared"
                raise RuntimeError(message)
            ready, _, _ = select.select([self._serial_output_fd], [], [], min(remaining, 0.1))
            if not ready:
                continue
            try:
                chunk = os.read(self._serial_output_fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                time.sleep(0.01)
                continue
            self.buffer.extend(chunk)

    def write_serial(self, data: str) -> None:
        """Write raw bytes to the serial input fifo (no waiting)."""
        self._serial_input.write_text(data, encoding="utf-8")


def _build_qemu_args(
    *,
    drive: Path,
    extra_qemu_args: list[str],
    floppy: bool,
    machine: str | None,
    memory: str,
    monitor_path: Path | None,
    pcap: Path | None,
    serial_base: Path,
    snapshot: bool,
    with_net: bool,
) -> list[str]:
    """Compose the ``qemu-system-i386`` argv from a session's parameters."""
    drive_spec = f"file={drive},format=raw"
    if floppy:
        drive_spec += ",if=floppy"
    if snapshot:
        drive_spec += ",snapshot=on"
    monitor_arg = f"unix:{monitor_path},server,nowait" if monitor_path is not None else "none"
    qemu_args = [
        "qemu-system-i386",
        "-chardev",
        f"pipe,id=s,path={serial_base}",
        "-display",
        "none",
        "-drive",
        drive_spec,
        "-m",
        memory,
        "-monitor",
        monitor_arg,
        "-serial",
        "chardev:s",
    ]
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
    qemu_args += extra_qemu_args
    return qemu_args


@contextlib.contextmanager
def qemu_session(
    *,
    boot_timeout: float = BOOT_TIMEOUT,
    drive: Path = DEFAULT_IMAGE,
    extra_qemu_args: list[str] | None = None,
    floppy: bool = False,
    machine: str | None = None,
    memory: str | None = None,
    monitor: bool = False,
    pcap: Path | None = None,
    snapshot: bool = False,
    wait_for_boot: bool = True,
    with_net: bool = False,
) -> Iterator[QemuSession]:
    """Launch QEMU, yield a :class:`QemuSession`, kill on exit.

    *monitor* attaches a unix-socket HMP monitor; the path is stored on
    the session and used by :meth:`QemuSession.monitor_send`,
    :meth:`QemuSession.screendump`, and :meth:`QemuSession.sendkey`.
    Without it, those methods raise.

    *wait_for_boot* (default True) drains the serial fifo until the
    shell's first ``"$ "`` prompt appears within *boot_timeout*; the
    elapsed time is recorded on the session as ``boot_time``.  Set
    False if a caller wants to interact with QEMU before the OS boots
    (e.g. injecting keys at the BIOS prompt).

    *memory* falls back to ``BBOE_QEMU_MEMORY`` when unset, else
    ``"1"`` (1 MB) — see :func:`run_commands` for rationale.
    *machine* falls back to ``BBOE_QEMU_MACHINE`` when unset, else
    no flag.

    Yields:
        A :class:`QemuSession` wrapping the running QEMU process and its
        serial / monitor endpoints.  The process is killed when the
        ``with`` block exits.

    """
    if memory is None:
        memory = os.environ.get("BBOE_QEMU_MEMORY") or "1"
    if machine is None:
        machine = os.environ.get("BBOE_QEMU_MACHINE") or None
    if extra_qemu_args is None:
        extra_qemu_args = []
    with tempfile.TemporaryDirectory(prefix="run_qemu_") as temp_dir:
        temporary_directory = Path(temp_dir)
        serial_base = temporary_directory / SERIAL_BASENAME
        os.mkfifo(f"{serial_base}.in")
        os.mkfifo(f"{serial_base}.out")
        monitor_path = temporary_directory / "monitor" if monitor else None
        qemu_args = _build_qemu_args(
            drive=drive,
            extra_qemu_args=extra_qemu_args,
            floppy=floppy,
            machine=machine,
            memory=memory,
            monitor_path=monitor_path,
            pcap=pcap,
            serial_base=serial_base,
            snapshot=snapshot,
            with_net=with_net,
        )
        process: subprocess.Popen | None = None
        output_fd: int | None = None
        try:
            process = subprocess.Popen(qemu_args)
            if monitor_path is not None:
                _wait_path(path=monitor_path, timeout=5.0)
            output_fd = os.open(f"{serial_base}.out", os.O_RDONLY | os.O_NONBLOCK)
            session = QemuSession(
                monitor_path=monitor_path,
                process=process,
                serial_input=Path(f"{serial_base}.in"),
                serial_output_fd=output_fd,
            )
            if wait_for_boot:
                boot_start = time.monotonic()
                session.wait_for_substring(PROMPT, timeout=boot_timeout)
                session.boot_time = time.monotonic() - boot_start
            yield session
        finally:
            if output_fd is not None:
                os.close(output_fd)
            if process is not None:
                _terminate(process=process)


def _wait_path(*, path: Path, timeout: float) -> None:
    """Block until *path* exists, or raise :class:`RuntimeError` on timeout."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            message = f"{path} never appeared within {timeout}s"
            raise RuntimeError(message)
        time.sleep(0.05)


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
    with qemu_session(
        boot_timeout=boot_timeout,
        drive=drive,
        floppy=floppy,
        machine=machine,
        memory=memory,
        pcap=pcap,
        snapshot=snapshot,
        with_net=with_net,
    ) as session:
        for command in commands:
            session.send_command(command, timeout=command_timeout)
        return QemuResult(
            boot_time=session.boot_time,
            command_times=list(session.command_times),
            output=session.output,
        )


def _terminate(*, process: subprocess.Popen) -> None:
    """Kill the QEMU process and wait for it to exit."""
    if process.poll() is not None:
        return
    process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)


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
        help="value for -m (e.g. '32M'); falls back to $BBOE_QEMU_MEMORY, then '1' (1 MB)",
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
