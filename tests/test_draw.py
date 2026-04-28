#!/usr/bin/env python3
r"""Smoke + font-restoration test for the draw program.

Runs draw with a few keystrokes, exits, and renders one more command on
the post-exit text screen.  Captures a VGA screendump and checks the
framebuffer has substantial pixel content.

Two failure modes this guards against:

1. draw crashes (vga_fill_block #GP, vga_set_mode bug) -- shell prompt
   never returns and the test times out waiting for the post-ls-bin
   prompt.
2. The boot-time vga_font_load doesn't run, leaving char-gen slot
   0x4000 empty.  draw exits cleanly but the post-exit text mode (mode
   03h with SR03=05h) renders every glyph as blank: framebuffer is
   nearly all-zero except for the blinking hardware cursor.

Drives QEMU directly rather than going through run_commands.run_commands
because the prompt-counting scheme there gets confused by the extra
respawn prompt the shell prints after SYS_EXIT and the leftover \r left
in the input pipe by sending keystrokes to draw.

Usage:
    ./test_draw.py
"""

from __future__ import annotations

import argparse
import contextlib
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT = b"$ "

BOOT_TIMEOUT = 30.0
LS_TIMEOUT = 5.0

# A populated 80x25 text screen with welcome banner + uptime + ls bin
# clocks ~10-15k non-zero RGB bytes in a 720x400 PPM.  An empty-font
# screen would be under ~200 (just the hardware cursor).  500 is safely
# above the noise floor and well below any plausibly-rendered screen.
MIN_NONZERO_PIXEL_BYTES = 500


def _count_nonzero_pixel_bytes(*, screenshot_path: Path) -> int:
    """Return the number of non-zero RGB bytes in a P6 PPM file."""
    data = screenshot_path.read_bytes()
    end = 0
    for _ in range(3):  # P6 magic, dimensions, max-value
        end = data.index(b"\n", end) + 1
    return sum(1 for byte in data[end:] if byte)


def _drain(*, buffer: bytearray, file_descriptor: int, timeout: float) -> None:
    """Pull anything available on the serial fifo into the buffer."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([file_descriptor], [], [], 0.05)
        if not ready:
            continue
        try:
            buffer.extend(os.read(file_descriptor, 4096))
        except BlockingIOError:
            return


def _take_screenshot(*, monitor_path: Path, output_path: Path) -> None:
    """Connect to the HMP monitor, send screendump, wait for completion."""
    deadline = time.monotonic() + 5.0
    with contextlib.closing(socket.socket(socket.AF_UNIX)) as sock:
        sock.settimeout(1.0)
        sock.connect(str(monitor_path))
        buffer = bytearray()
        while b"(qemu) " not in buffer:
            if time.monotonic() > deadline:
                return
            with contextlib.suppress(socket.timeout):
                buffer.extend(sock.recv(4096))
        sock.sendall(f"screendump {output_path}\n".encode())
        prompt_count = buffer.count(b"(qemu) ")
        while buffer.count(b"(qemu) ") <= prompt_count:
            if time.monotonic() > deadline:
                return
            with contextlib.suppress(socket.timeout):
                buffer.extend(sock.recv(4096))


def _wait_for_substring(
    *,
    buffer: bytearray,
    file_descriptor: int,
    needle: bytes,
    process: subprocess.Popen,
    start: int,
    timeout: float,
) -> None:
    """Drain the fifo until `needle` appears in buffer at index >= start."""
    deadline = time.monotonic() + timeout
    while needle not in buffer[start:]:
        if time.monotonic() > deadline:
            tail = bytes(buffer[-300:])
            message = f"never saw {needle!r}; tail={tail!r}"
            raise TimeoutError(message)
        if process.poll() is not None:
            message = f"qemu exited with {process.returncode}"
            raise RuntimeError(message)
        ready, _, _ = select.select([file_descriptor], [], [], 0.1)
        if not ready:
            continue
        try:
            buffer.extend(os.read(file_descriptor, 4096))
        except BlockingIOError:
            continue


def _build_os(*, image_path: Path) -> None:
    """Run make_os.sh into image_path; abort the test if the build fails."""
    result = subprocess.run(
        ["./make_os.sh", str(image_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        message = "make_os.sh failed"
        raise RuntimeError(message)


def _run_draw_session(
    *,
    image_path: Path,
    monitor_path: Path,
    screenshot_path: Path,
    serial_base: Path,
) -> bytearray:
    """Boot QEMU, drive draw + ls bin, screendump, return serial buffer."""
    qemu_arguments = [
        "qemu-system-i386",
        "-chardev",
        f"pipe,id=s,path={serial_base}",
        "-display",
        "none",
        "-drive",
        f"file={image_path},format=raw,snapshot=on",
        "-monitor",
        f"unix:{monitor_path},server,nowait",
        "-serial",
        "chardev:s",
    ]
    process = subprocess.Popen(qemu_arguments)
    output_fd = os.open(f"{serial_base}.out", os.O_RDONLY | os.O_NONBLOCK)
    input_path = Path(f"{serial_base}.in")
    buffer = bytearray()
    try:
        _wait_for_substring(
            buffer=buffer,
            file_descriptor=output_fd,
            needle=PROMPT,
            process=process,
            start=0,
            timeout=BOOT_TIMEOUT,
        )
        # Sequence: draw, then 5 keystrokes (q exits), then ls bin to
        # populate the post-exit screen.  Sending in stages with short
        # pauses sidesteps the shell-respawn-prints-extra-prompt race
        # that confuses run_commands' prompt counter.
        input_path.write_text("draw\r", encoding="utf-8")
        time.sleep(0.5)
        _drain(buffer=buffer, file_descriptor=output_fd, timeout=0.3)
        for character in "wasdq":
            input_path.write_text(character, encoding="utf-8")
            time.sleep(0.1)
            _drain(buffer=buffer, file_descriptor=output_fd, timeout=0.05)
        time.sleep(0.5)
        _drain(buffer=buffer, file_descriptor=output_fd, timeout=0.3)
        ls_start = len(buffer)
        input_path.write_text("ls bin\r", encoding="utf-8")
        _wait_for_substring(
            buffer=buffer,
            file_descriptor=output_fd,
            needle=b"uptime*",  # last filename alphabetically
            process=process,
            start=ls_start,
            timeout=LS_TIMEOUT,
        )
        time.sleep(0.3)  # let trailing prompt render before screendump
        _take_screenshot(monitor_path=monitor_path, output_path=screenshot_path)
    finally:
        os.close(output_fd)
        if process.poll() is None:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
    return buffer


def main() -> int:
    """Boot, exercise draw, screendump, verify pixel content."""
    argparse.ArgumentParser(description=__doc__).parse_args()
    os.chdir(REPO_ROOT)

    with tempfile.TemporaryDirectory(prefix="test_draw_") as temporary_path:
        temporary_directory = Path(temporary_path)
        image_path = temporary_directory / "drive.img"
        serial_base = temporary_directory / "ser"
        monitor_path = temporary_directory / "monitor"
        screenshot_path = temporary_directory / "post_draw.ppm"
        os.mkfifo(f"{serial_base}.in")
        os.mkfifo(f"{serial_base}.out")

        try:
            _build_os(image_path=image_path)
            buffer = _run_draw_session(
                image_path=image_path,
                monitor_path=monitor_path,
                screenshot_path=screenshot_path,
                serial_base=serial_base,
            )
        except (OSError, RuntimeError, TimeoutError) as error:
            print(f"FAIL: {error}")
            return 1

        if not screenshot_path.exists():
            print("FAIL: monitor screendump didn't produce a file")
            return 1
        nonzero = _count_nonzero_pixel_bytes(screenshot_path=screenshot_path)
        if nonzero < MIN_NONZERO_PIXEL_BYTES:
            print(
                f"FAIL: post-draw screen has only {nonzero} non-zero RGB bytes "
                f"(need >= {MIN_NONZERO_PIXEL_BYTES}); font slot 0x4000 likely empty"
            )
            return 1
        if b"EXC" in buffer:
            print(f"FAIL: CPU exception observed in serial output: tail={bytes(buffer[-300:])!r}")
            return 1
        print(f"PASS: draw exited cleanly, post-draw text rendered ({nonzero} non-zero RGB bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
