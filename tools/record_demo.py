#!/usr/bin/env python3
"""Record a VGA-framebuffer demo of bboeos as an animated GIF.

Drives QEMU through a fixed scenario (boot to shell basics to self-hosted
assembler to networking), grabbing PPM frames at 10 fps via the QEMU
HMP monitor, then encodes them with ffmpeg + gifsicle.

Output: docs/gifs/demo.gif (overwritten on every run).
"""

from __future__ import annotations

import argparse
import dataclasses
import random
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

DEFAULT_OUTPUT = ROOT / "docs" / "gifs" / "demo.gif"
DEMO_PROGRAM = """\
[bits 32]
org 08048000h
%include "constants.asm"
mov edi, msg
call FUNCTION_PRINT_STRING
jmp FUNCTION_EXIT
msg: db `hi from demo.asm\\n\\0`
"""
DRIVE_IMAGE = ROOT / "drive.img"
EDITOR_OPEN_SECONDS = 1.0
EDITOR_PRE_QUIT_SECONDS = 2.0
EDITOR_SAVE_PAUSE_SECONDS = 0.5
FINAL_PAUSE_SECONDS = 5.0
FRAME_RATE = 10
INTER_COMMAND_PAUSE_SECONDS = 2.0
PRE_TERMINATOR_SECONDS = 0.5
TYPE_DELAY_SECONDS = 0.06
TYPE_JITTER_SECONDS = 0.02


@dataclasses.dataclass(frozen=True, kw_only=True)
class EditorStep:
    r"""Open ``edit <filename>``, type *content*, save (Ctrl-S), quit (Ctrl-Q).

    Lines in *content* are separated by ``\n``; the editor accepts both
    ``\r`` and ``\n`` as line breaks.
    """

    content: str
    editor_open_seconds: float = EDITOR_OPEN_SECONDS
    filename: str
    pause_after: float = INTER_COMMAND_PAUSE_SECONDS
    pre_quit_seconds: float = EDITOR_PRE_QUIT_SECONDS
    save_pause_seconds: float = EDITOR_SAVE_PAUSE_SECONDS


@dataclasses.dataclass(frozen=True, kw_only=True)
class Scenario:
    """The complete demo: ordered sections of typed shell commands."""

    sections: list[Section]


@dataclasses.dataclass(frozen=True, kw_only=True)
class Section:
    """One labelled phase of the demo."""

    name: str
    steps: list[EditorStep | ShellStep]


@dataclasses.dataclass(frozen=True, kw_only=True)
class ShellStep:
    """Type *command* + Enter and (optionally) wait for the next shell prompt."""

    await_prompt: bool = True
    command: str
    pause_after: float = INTER_COMMAND_PAUSE_SECONDS


def _is_seabios_frame(*, path: Path) -> bool:
    """Detect SeaBIOS boot frames by the top-left character glyph.

    SeaBIOS row 0 always starts with 'S' ("SeaBIOS (version ...)") or 'B'
    ("Booting from Hard Disk...").  Both glyphs share three pixel signatures
    inside the (0,0) char cell that bboeos's row-0 starting characters
    ('W' from "Welcome", 'V' from "Version", '$' from the prompt, 'P', 'R',
    'Q', 'C', 'O', 'h', etc.) don't all hit at once: a black pixel at
    (3, 0), a white pixel at (3, 6), and a black pixel at (3, 8).  '$'
    has a top stem so (3, 0) is white; 'W'/'V' lack the central horizontal
    stroke so (3, 6) is black; 'h' / lowercase letters likewise miss
    (3, 6).  The combined three-pixel test catches both initial-boot and
    post-reboot SeaBIOS frames without snagging shell content.
    """
    return (
        _read_pixel_red(column=3, path=path, row=0) < 128  # noqa: PLR2004
        and _read_pixel_red(column=3, path=path, row=6) > 128  # noqa: PLR2004
        and _read_pixel_red(column=3, path=path, row=8) < 128  # noqa: PLR2004
    )


def _ppm_dimensions(*, path: Path) -> tuple[int, int]:
    """Return (width, height) from a binary PPM header."""
    with path.open("rb") as handle:
        magic = handle.readline().strip()
        if magic != b"P6":
            message = f"{path} is not a P6 PPM (got {magic!r})"
            raise RuntimeError(message)
        # Skip comment lines, then read the WIDTH HEIGHT line.
        while True:
            line = handle.readline()
            if not line:
                message = f"{path} is truncated"
                raise RuntimeError(message)
            if line.startswith(b"#"):
                continue
            parts = line.split()
            if len(parts) >= 2:  # noqa: PLR2004
                return int(parts[0]), int(parts[1])


def _read_pixel_red(*, column: int, path: Path, row: int) -> int:
    """Return the R byte of the pixel at (*column*, *row*) in a binary PPM."""
    with path.open("rb") as handle:
        handle.readline()  # P6
        line = handle.readline()
        while line.startswith(b"#"):
            line = handle.readline()
        width, _ = (int(value) for value in line.split())
        handle.readline()  # max-value
        handle.seek(handle.tell() + (row * width + column) * 3)
        return handle.read(1)[0]


def _record(*, keep_frames: bool, output: Path) -> int:
    """Run the demo end-to-end and produce *output*."""
    from run_qemu import qemu_session  # noqa: PLC0415

    scenario = default_scenario()
    capture_root = ROOT / "_demo_capture"
    if capture_root.exists():
        shutil.rmtree(capture_root)
    capture_root.mkdir(parents=True)
    stop = threading.Event()
    with qemu_session(
        monitor=True,
        snapshot=True,
        wait_for_boot=False,
        with_net=True,
    ) as session:
        capture = threading.Thread(
            target=capture_loop,
            kwargs={
                "frame_directory": capture_root,
                "framerate": FRAME_RATE,
                "session": session,
                "stop": stop,
            },
            daemon=True,
        )
        capture.start()
        try:
            session.wait_for_substring(b"$ ", timeout=10.0)
            for section in scenario.sections:
                for step in section.steps:
                    _run_step(session=session, step=step)
        finally:
            stop.set()
            capture.join(timeout=5.0)
    encode_gif(frame_directory=capture_root, framerate=FRAME_RATE, output=output)
    print(f"wrote {output} ({output.stat().st_size:,} bytes)")
    if keep_frames:
        print(f"frames retained in {capture_root}")
    else:
        shutil.rmtree(capture_root)
    return 0


def _renumber_frames(*, frame_directory: Path, kept: list[Path]) -> None:
    """Renumber *kept* frames consecutively from zero in *frame_directory*."""
    for frame in kept:
        frame.rename(frame.with_suffix(".keep"))
    for index, frame in enumerate(sorted(frame_directory.glob("frame_*.keep"))):
        frame.rename(frame_directory / f"frame_{index:05d}.ppm")


def _run_step(*, session: Any, step: EditorStep | ShellStep) -> None:  # noqa: ANN401
    """Execute one demo *step* against *session*."""
    if isinstance(step, ShellStep):
        type_at_human_pace(text=step.command, writer=session.write_serial)
        if step.await_prompt:
            session.wait_for_prompt(timeout=10.0)
    else:
        type_at_human_pace(
            text=f"edit {step.filename}",
            writer=session.write_serial,
        )
        time.sleep(step.editor_open_seconds)
        type_editor_program(content=step.content, writer=session.write_serial)
        session.write_serial("\x13")  # Ctrl-S: save
        time.sleep(step.save_pause_seconds)
        time.sleep(step.pre_quit_seconds)
        session.write_serial("\x11")  # Ctrl-Q: quit (clean because just saved)
        session.wait_for_prompt(timeout=10.0)
    time.sleep(step.pause_after)


def capture_loop(
    *,
    frame_directory: Path,
    framerate: int,
    session: Any,  # noqa: ANN401
    stop: threading.Event,
) -> None:
    """Dump PPM frames into *frame_directory* at *framerate* until *stop* is set.

    Sole user of ``session.screendump()`` (and therefore the QEMU
    monitor) while the demo is running; the main thread only writes
    serial bytes.  Signals on the *stop* event so the main thread can
    wake the loop ahead of its next scheduled frame.
    """
    interval = 1.0 / framerate
    index = 0
    next_deadline = time.monotonic()
    while not stop.is_set():
        path = frame_directory / f"frame_{index:05d}.ppm"
        try:
            session.screendump(path)
        except (TimeoutError, RuntimeError):
            return
        index += 1
        next_deadline += interval
        sleep_for = next_deadline - time.monotonic()
        if sleep_for > 0:
            stop.wait(timeout=sleep_for)


def default_scenario() -> Scenario:
    """Build the four-section demo scenario described in the design doc.

    Returns:
        The :class:`Scenario` driven by :func:`main` for an end-to-end run.

    """
    return Scenario(
        sections=[
            Section(name="boot", steps=[]),
            Section(
                name="shell",
                steps=[
                    ShellStep(command="help"),
                    ShellStep(command="ls"),
                    ShellStep(command="date"),
                    ShellStep(command="echo hello world"),
                ],
            ),
            Section(
                name="assembler",
                steps=[
                    EditorStep(content=DEMO_PROGRAM, filename="src/demo.asm"),
                    ShellStep(command="asm src/demo.asm demo"),
                    ShellStep(command="demo"),
                ],
            ),
            Section(
                name="networking",
                steps=[
                    ShellStep(command="arp 10.0.2.2"),
                    ShellStep(command="dns bboeos.bryceboe.com"),
                    ShellStep(command="ping bryceboe.com"),
                ],
            ),
            Section(
                name="reboot",
                steps=[
                    ShellStep(
                        await_prompt=False,
                        command="reboot",
                        pause_after=FINAL_PAUSE_SECONDS,
                    ),
                ],
            ),
        ],
    )


def encode_gif(
    *,
    frame_directory: Path,
    framerate: int,
    output: Path,
) -> None:
    """Two-pass ffmpeg + gifsicle encode of ``frame_directory/frame_*.ppm``."""
    if shutil.which("ffmpeg") is None:
        message = "ffmpeg not found on PATH"
        raise RuntimeError(message)
    if shutil.which("gifsicle") is None:
        message = "gifsicle not found on PATH"
        raise RuntimeError(message)
    if normalize_frames(frame_directory=frame_directory) == 0:
        message = f"no frames found in {frame_directory}"
        raise RuntimeError(message)
    trim_seabios_frames(frame_directory=frame_directory)
    output.parent.mkdir(parents=True, exist_ok=True)
    palette = frame_directory / "palette.png"
    intermediate = frame_directory / "intermediate.gif"
    pattern = frame_directory / "frame_%05d.ppm"
    subprocess.run(
        palettegen_argv(frame_pattern=pattern, framerate=framerate, palette=palette),
        check=True,
    )
    subprocess.run(
        paletteuse_argv(
            frame_pattern=pattern,
            framerate=framerate,
            intermediate=intermediate,
            palette=palette,
        ),
        check=True,
    )
    subprocess.run(
        gifsicle_argv(intermediate=intermediate, output=output),
        check=True,
    )


def gifsicle_argv(*, intermediate: Path, output: Path) -> list[str]:
    """Build the gifsicle argv for the optimisation pass."""
    return ["gifsicle", "-O3", "-o", str(output), str(intermediate)]


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"path of the final GIF (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--keep-frames",
        action="store_true",
        help="leave captured PPMs in the temp dir for inspection",
    )
    arguments = parser.parse_args()
    if not DRIVE_IMAGE.exists():
        subprocess.run(["./make_os.sh"], check=True, cwd=str(ROOT))
    return _record(keep_frames=arguments.keep_frames, output=arguments.output)


def normalize_frames(*, frame_directory: Path) -> int:
    """Drop frames whose dimensions don't match the modal size, renumber the rest.

    QEMU captures the BIOS splash at 640x480 before the OS switches to
    720x400 text mode; ffmpeg's image2 demuxer rejects mid-stream
    dimension changes.  Returns the number of kept frames.
    """
    frames = sorted(frame_directory.glob("frame_*.ppm"))
    if not frames:
        return 0
    counts: dict[tuple[int, int], int] = {}
    dimensions: list[tuple[int, int]] = []
    for frame in frames:
        size = _ppm_dimensions(path=frame)
        dimensions.append(size)
        counts[size] = counts.get(size, 0) + 1
    target = max(counts, key=counts.__getitem__)
    kept = [frame for frame, size in zip(frames, dimensions, strict=True) if size == target]
    dropped = [frame for frame, size in zip(frames, dimensions, strict=True) if size != target]
    for frame in dropped:
        frame.unlink()
    _renumber_frames(frame_directory=frame_directory, kept=kept)
    return len(kept)


def palettegen_argv(
    *,
    frame_pattern: Path,
    framerate: int,
    palette: Path,
) -> list[str]:
    """Build the ffmpeg argv for the first pass (palette generation)."""
    return [
        "ffmpeg",
        "-y",
        "-framerate",
        str(framerate),
        "-i",
        str(frame_pattern),
        "-vf",
        "palettegen",
        str(palette),
    ]


def paletteuse_argv(
    *,
    frame_pattern: Path,
    framerate: int,
    intermediate: Path,
    palette: Path,
) -> list[str]:
    """Build the ffmpeg argv for the second pass (palette application)."""
    return [
        "ffmpeg",
        "-y",
        "-framerate",
        str(framerate),
        "-i",
        str(frame_pattern),
        "-i",
        str(palette),
        "-lavfi",
        "paletteuse",
        str(intermediate),
    ]


def trim_seabios_frames(*, frame_directory: Path) -> int:
    """Drop SeaBIOS boot frames captured during initial boot and post-reboot.

    Returns the number of dropped frames.  Renumbers the survivors so
    ffmpeg's image2 demuxer sees a contiguous frame_NNNNN.ppm sequence.
    """
    frames = sorted(frame_directory.glob("frame_*.ppm"))
    kept = [frame for frame in frames if not _is_seabios_frame(path=frame)]
    dropped_count = len(frames) - len(kept)
    for frame in frames:
        if frame not in kept:
            frame.unlink()
    _renumber_frames(frame_directory=frame_directory, kept=kept)
    return dropped_count


def type_at_human_pace(
    *,
    delay_seconds: float = TYPE_DELAY_SECONDS,
    jitter_seconds: float = TYPE_JITTER_SECONDS,
    pre_terminator_seconds: float = PRE_TERMINATOR_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    terminator: str = "\r",
    text: str,
    writer: Callable[[str], None],
) -> None:
    r"""Send each character of *text* through *writer* with human-feel pacing.

    Sleeps *delay_seconds* +/- uniform *jitter_seconds* after every typed
    character.  When *terminator* is non-empty, sleeps an extra
    *pre_terminator_seconds* before sending it so the viewer can read
    what was typed before the line executes (or, in the editor, before
    the newline commits the line).  Pass ``terminator=""`` or
    ``pre_terminator_seconds=0`` to suppress.  Sleeps go through *sleep*
    for testability.
    """
    for character in text:
        writer(character)
        wait = delay_seconds
        if jitter_seconds > 0:
            wait += random.uniform(-jitter_seconds, jitter_seconds)
        sleep(wait)
    if terminator:
        if pre_terminator_seconds > 0:
            sleep(pre_terminator_seconds)
        writer(terminator)


def type_editor_program(
    *,
    content: str,
    delay_seconds: float = TYPE_DELAY_SECONDS,
    jitter_seconds: float = TYPE_JITTER_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    writer: Callable[[str], None],
) -> None:
    r"""Type *content* into the editor: each line at human pace, then ``\n``.

    Skipped trailing blank line (from a content string ending in ``\n``)
    is not re-typed.  Lines flow into the editor without the
    pre-terminator pause that shell commands use — newlines inside a
    program don't need that "press Enter" beat.
    """
    for line in content.splitlines():
        type_at_human_pace(
            delay_seconds=delay_seconds,
            jitter_seconds=jitter_seconds,
            pre_terminator_seconds=0.0,
            sleep=sleep,
            terminator="\n",
            text=line,
            writer=writer,
        )


if __name__ == "__main__":
    sys.exit(main())
