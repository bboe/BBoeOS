#!/usr/bin/env python3
"""Record a clip of bboeos booting, running Doom, quitting, and rebooting.

Boots QEMU against the existing drive.img (assumes you've already
run `./tools/install_doom.sh`), captures the VGA framebuffer via
the QEMU monitor's ``screendump`` command, and produces a single
clip covering: welcome banner + shell prompt → ``doom`` typed →
title sequence + demo loop → menu-driven quit → shell prompt
returning → ``reboot`` typed → welcome banner again.  Bookending
on the welcome banner makes the start/end states match visually.

The shell + Doom segments capture at different resolutions (text
mode 720x400 vs mode 13h 320x200); frames are scaled-and-padded
to a common size so the output video has stable dimensions
throughout.

Quit sequence is sent through QEMU's ``sendkey`` (PS/2 scancodes)
rather than serial bytes — Doom's input loop reads BBKEY events
from the per-fd PS/2 ring, so serial input wouldn't reach it.

Two output formats:

  ``--format=webm`` (default): VP9 at 30 fps — ~few MB for the
  full clip, plays inline in modern browsers / GitHub.

  ``--format=gif``: 5 fps, gifsicle -O3 --lossy=80.  Bigger than
  the WebM but renders inline anywhere markdown does (e.g. README
  image embeds without an HTML video tag).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "tools"))

from record_demo import (  # noqa: E402
    _ppm_dimensions,  # noqa: PLC2701
    _renumber_frames,  # noqa: PLC2701
    capture_loop,
    type_at_human_pace,
)
from run_qemu import qemu_session  # noqa: E402

# Wall-clock budget per phase.  Total ~30s end-to-end; tweak in one
# place rather than scattering sleeps through main().
INITIAL_LINGER_SECONDS = 2.5  # welcome banner visible before typing "doom"
GAMEPLAY_SECONDS = 12  # title + demo loop visible after `doom\r` lands
QUIT_KEY_DELAY = 0.5  # gap between menu keystrokes so Doom can re-render
POST_DOOM_LINGER_SECONDS = 2.0  # post-Doom shell prompt visible before typing "reboot"
WELCOME_LINGER_SECONDS = 2.5  # post-reboot welcome banner visible before stopping
WELCOME_NEEDLE = b"Welcome to BBoeOS!"
CAPTURE_DIR = REPO / "_demo_capture_doom"
DRIVE = REPO / "drive.img"

# WebM: VP9 compresses both text and graphics frames well; 30 fps
# captures the full motion of the Doom demo, original capture size is
# preserved (text mode 720x400 dominates after _normalize_to_max_size
# scales the 320x200 Doom frames up + pads them).
WEBM_FRAMERATE = 30
WEBM_OUTPUT = REPO / "docs" / "gifs" / "doom.webm"

# GIF: 5 fps over the full clip; downscale to 480x270 so the
# README-embedded gif stays around 1-2 MB even with the wider text-mode
# frames in the boot + shell-return segments.  640x400 + gifsicle -O3
# was ~10 MB because Doom's dithered framebuffer compresses poorly.
GIF_FRAMERATE = 5
GIF_WIDTH = 480
GIF_HEIGHT = 270
GIF_OUTPUT = REPO / "docs" / "gifs" / "doom.gif"


def _drop_frames_in_window(*, end: float, frame_directory: Path, start: float) -> int:
    """Delete frames whose mtime falls in (*start*, *end*); return drop count.

    Used to excise the SeaBIOS / early-boot interlude during the
    reboot that bookends the recording.  Both BIOS and text mode
    render at 720x400 so we can't filter by dimension; we instead
    rely on the serial-output markers main() captured around the
    reboot to wall-clock the BIOS window and drop the corresponding
    frames here.
    """
    dropped = 0
    for frame in frame_directory.glob("frame_*.ppm"):
        mtime = frame.stat().st_mtime
        if start < mtime < end:
            frame.unlink()
            dropped += 1
    return dropped


def _encode_doom_gif(*, capture_framerate: int, frame_directory: Path) -> None:
    """Ffmpeg palettegen + paletteuse + gifsicle, downscaling to GIF_WIDTHxGIF_HEIGHT.

    record_demo.encode_gif emits the gif at the source resolution;
    a 30-second mixed boot + Doom + shell clip at the post-normalize
    text-mode size (720x400-ish) ends up too large for the
    README-embedded gif (>5 MB).  This downscales during paletteuse
    so the gif stays in the 1-2 MB range.

    *capture_framerate* tells ffmpeg the real cadence of the input
    PPMs; the ``fps=GIF_FRAMERATE`` filter then drops frames so the
    output gif plays back at real time even when the capture ran
    faster (e.g. 30 fps for the WebM share-pass).  Without that,
    a 30-fps capture decoded with ``-framerate 5`` would stretch
    a 30-second clip to 3 minutes of playback.
    """
    if _normalize_to_max_size(frame_directory=frame_directory) == 0:
        message = f"no frames found in {frame_directory}"
        raise RuntimeError(message)
    palette = frame_directory / "palette.png"
    intermediate = frame_directory / "intermediate.gif"
    pattern = frame_directory / "frame_%05d.ppm"
    fps_drop = f"fps={GIF_FRAMERATE}"
    scale = f"scale={GIF_WIDTH}:{GIF_HEIGHT}:flags=neighbor"
    GIF_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(capture_framerate),
            "-i",
            str(pattern),
            "-vf",
            f"{fps_drop},{scale},palettegen=stats_mode=full",
            str(palette),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(capture_framerate),
            "-i",
            str(pattern),
            "-i",
            str(palette),
            "-lavfi",
            f"{fps_drop},{scale} [a]; [a][1:v] paletteuse=dither=bayer:bayer_scale=5",
            str(intermediate),
        ],
        check=True,
    )
    subprocess.run(
        [
            "gifsicle",
            "-O3",
            "--lossy=80",
            "-o",
            str(GIF_OUTPUT),
            str(intermediate),
        ],
        check=True,
    )


def _encode_doom_webm(*, frame_directory: Path) -> None:
    """Ffmpeg → VP9 WebM at the post-normalize source resolution.

    VP9 with -crf 40 (still very watchable for low-res content),
    -b:v 0 (let CRF drive bitrate), -row-mt + -tile-columns for
    parallel encode.  Both the text-mode boot/shell segments and
    the Doom gameplay compress well; expect a few MB for the full
    ~30 s clip.  Bump CRF higher for an even smaller file at the
    cost of muddier sprites.
    """
    if _normalize_to_max_size(frame_directory=frame_directory) == 0:
        message = f"no frames found in {frame_directory}"
        raise RuntimeError(message)
    WEBM_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pattern = frame_directory / "frame_%05d.ppm"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(WEBM_FRAMERATE),
            "-i",
            str(pattern),
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "0",
            "-crf",
            "40",
            "-row-mt",
            "1",
            "-tile-columns",
            "2",
            "-pix_fmt",
            "yuv420p",
            str(WEBM_OUTPUT),
        ],
        check=True,
    )


def _normalize_to_max_size(*, frame_directory: Path) -> int:
    """Scale-and-pad every frame to the largest captured (width, height).

    The recording crosses several VGA modes (text mode / Doom's
    mode 13h), each of which screendump emits at its native
    resolution.  ffmpeg's image2 demuxer rejects mid-stream
    dimension changes, so we pre-pass each frame through
    ``ffmpeg -vf scale=...:flags=neighbor,pad=...:black`` to land
    on the same target before encode.  Aspect ratio is preserved
    (Doom's mode-13h picture stays square-pixeled inside black
    bars), and nearest-neighbor scaling preserves the chunky
    pixel-art look.  Returns the number of normalized frames.
    """
    frames = sorted(frame_directory.glob("frame_*.ppm"))
    if not frames:
        return 0
    max_width = max_height = 0
    dimensions: list[tuple[int, int]] = []
    for frame in frames:
        size = _ppm_dimensions(path=frame)
        dimensions.append(size)
        max_width = max(max_width, size[0])
        max_height = max(max_height, size[1])
    fit_expr = f"min({max_width}/iw\\,{max_height}/ih)"
    scale_pad = (
        f"scale=trunc(iw*{fit_expr}/2)*2:trunc(ih*{fit_expr}/2)*2:flags=neighbor,"
        f"pad={max_width}:{max_height}:({max_width}-iw)/2:({max_height}-ih)/2:black"
    )
    for frame, size in zip(frames, dimensions, strict=True):
        if size == (max_width, max_height):
            continue
        scaled = frame.with_suffix(".scaled.ppm")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(frame), "-vf", scale_pad, str(scaled)],
            check=True,
            capture_output=True,
        )
        scaled.replace(frame)
    _renumber_frames(frame_directory=frame_directory, kept=frames)
    return len(frames)


def main() -> int:
    """Record + encode the Doom clip in the requested format(s)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format",
        choices=("gif", "webm", "both"),
        default="webm",
        help="output format (default: webm)",
    )
    arguments = parser.parse_args()

    if not DRIVE.is_file():
        print(f"record_doom: {DRIVE} missing — run ./tools/install_doom.sh first", file=sys.stderr)
        return 1
    needed_tools = ["ffmpeg"]
    if arguments.format in {"gif", "both"}:
        needed_tools.append("gifsicle")
    for tool in needed_tools:
        if shutil.which(tool) is None:
            print(f"record_doom: {tool} not on PATH; install it (apt: {tool}; brew: {tool})", file=sys.stderr)
            return 1

    # Capture at the higher of the two requested framerates so a single
    # capture run feeds either encode.  WebM resamples cleanly to 30
    # fps; the GIF encode reads the same PPMs and lets ffmpeg drop
    # frames to its 5 fps target.
    capture_framerate = max(
        WEBM_FRAMERATE if arguments.format in {"webm", "both"} else 0,
        GIF_FRAMERATE if arguments.format in {"gif", "both"} else 0,
    )

    if CAPTURE_DIR.exists():
        shutil.rmtree(CAPTURE_DIR)
    CAPTURE_DIR.mkdir(parents=True)

    stop = threading.Event()
    # BIOS-window markers — set right around the reboot so post-processing
    # can drop the SeaBIOS frames.  Initialised so a teardown before that
    # point still leaves _drop_frames_in_window with a valid (empty) range.
    bios_window_start = 0.0
    bios_window_end = 0.0
    with qemu_session(drive=DRIVE, memory="64", monitor=True, snapshot=True) as session:
        # Start capture before anything else so the boot sequence (BIOS
        # splash, kernel banner, driver init prints) appears at the
        # head of the clip.
        capture = threading.Thread(
            target=capture_loop,
            kwargs={
                "frame_directory": CAPTURE_DIR,
                "framerate": capture_framerate,
                "session": session,
                "stop": stop,
            },
            daemon=True,
        )
        capture.start()
        try:
            session.wait_for_substring(b"$ ", timeout=15.0)
            # Linger on the welcome banner so the viewer can read it
            # before any input arrives.
            time.sleep(INITIAL_LINGER_SECONDS)
            doom_buffer_offset = len(session.buffer)
            # Type "doom" character-by-character (with the standard
            # human-feel pacing from record_demo) so the command
            # appears on screen rather than blinking past in a single
            # frame.  type_at_human_pace's pre_terminator pause keeps
            # the typed line on screen briefly before \r executes it.
            type_at_human_pace(text="doom", writer=session.write_serial)
            # Title screen + demo loop play.
            time.sleep(GAMEPLAY_SECONDS)
            # Quit menu via PS/2 sendkey (Doom reads BBKEY events from
            # the per-fd PS/2 ring; serial input goes through the cooked
            # ASCII path, which doesn't reach DG_GetKey).  Esc opens the
            # main menu (cursor on "New Game"), Up wraps to the bottom
            # entry "Quit Game", Enter brings up the "Press Y to quit"
            # confirmation, Y exits cleanly back to the shell.
            for key in ("esc", "up", "ret", "y"):
                session.sendkey(key)
                time.sleep(QUIT_KEY_DELAY)
            # Wait for the post-Doom shell prompt so the text-mode
            # return is captured, then linger so the viewer registers
            # the clean exit before the next command starts typing.
            session.wait_for_substring(b"$ ", start=doom_buffer_offset, timeout=10.0)
            time.sleep(POST_DOOM_LINGER_SECONDS)
            # Reboot — bookend the recording on the welcome banner
            # (matches the opening frame; visually closes the loop).
            # Same human-pace typing so "reboot" is readable on screen
            # before the BIOS takes over.  Bracket the BIOS-takes-over
            # interlude with wall-clock timestamps so we can drop the
            # SeaBIOS / "Booting from..." frames in post-processing —
            # they render at 720x400 (same as our text mode) so we
            # can't filter them by dimension.
            reboot_buffer_offset = len(session.buffer)
            type_at_human_pace(text="reboot", writer=session.write_serial)
            bios_window_start = time.time()
            session.wait_for_substring(WELCOME_NEEDLE, start=reboot_buffer_offset, timeout=15.0)
            bios_window_end = time.time()
            time.sleep(WELCOME_LINGER_SECONDS)
        finally:
            stop.set()
            capture.join(timeout=5.0)

    # Drop the BIOS interlude before any encode pass touches the frames.
    # _normalize_to_max_size renumbers what's left, so the gap closes
    # cleanly and ffmpeg sees a continuous sequence.
    _drop_frames_in_window(
        end=bios_window_end,
        frame_directory=CAPTURE_DIR,
        start=bios_window_start,
    )

    if arguments.format in {"webm", "both"}:
        _encode_doom_webm(frame_directory=CAPTURE_DIR)
        print(f"wrote {WEBM_OUTPUT} ({WEBM_OUTPUT.stat().st_size:,} bytes)")
    if arguments.format in {"gif", "both"}:
        _encode_doom_gif(capture_framerate=capture_framerate, frame_directory=CAPTURE_DIR)
        print(f"wrote {GIF_OUTPUT} ({GIF_OUTPUT.stat().st_size:,} bytes)")
    shutil.rmtree(CAPTURE_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
