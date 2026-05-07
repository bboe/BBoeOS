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

  ``--format=mp4`` (default): H.264 + AAC at 30 fps — ~few MB for
  the full clip, plays inline in every modern browser (incl. older
  Firefox builds that flake on VP9), GitHub markdown previews, and
  HTML5 ``<video>`` embeds.

  ``--format=gif``: 5 fps, gifsicle -O3 --lossy=80.  Bigger than
  the MP4 but renders inline anywhere markdown does (e.g. README
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
AUDIO_FILENAME = "audio.wav"
AUDIO_TAIL_PAD_SECONDS = 60  # apad bound: longer than any plausible recording
CAPTURE_DIR = REPO / "_demo_capture_doom"
DOOM_AUDIO_READY_NEEDLE = b"[bboeos doom] OPL music"
DRIVE = REPO / "drive.img"
GAMEPLAY_SECONDS = 20  # title + demo loop visible after audio init completes
INITIAL_LINGER_SECONDS = 2.5  # welcome banner visible before typing "doom"
POST_DOOM_LINGER_SECONDS = 2.0  # post-Doom shell prompt visible before typing "reboot"
QUIT_KEY_DELAY = 0.5  # gap between menu keystrokes so Doom can re-render
WELCOME_LINGER_SECONDS = 2.5  # post-reboot welcome banner visible before stopping
WELCOME_NEEDLE = b"Welcome to BBoeOS!"

# MP4: H.264 + AAC for maximum browser compatibility.  30 fps captures
# the full motion of the Doom demo; original capture size is preserved
# (text mode 720x400 dominates after _normalize_to_max_size scales the
# 320x200 Doom frames up + pads them).
MP4_FRAMERATE = 30
MP4_OUTPUT = REPO / "docs" / "gifs" / "doom.mp4"

# GIF: 5 fps over the full clip; downscale to 480x270 so the
# README-embedded gif stays around 1-2 MB even with the wider text-mode
# frames in the boot + shell-return segments.  640x400 + gifsicle -O3
# was ~10 MB because Doom's dithered framebuffer compresses poorly.
GIF_FRAMERATE = 5
GIF_HEIGHT = 270
GIF_OUTPUT = REPO / "docs" / "gifs" / "doom.gif"
GIF_WIDTH = 480


def _audio_qemu_args(*, audio_path: Path) -> list[str]:
    """Build the QEMU args that route SB16 output to a WAV file.

    Mirrors the pattern used in tests/test_doom_sound_qemu.py — QEMU's
    ``wav`` audiodev writes a 16-bit stereo PCM file (44.1 kHz by
    default).  Two known quirks of this backend:

    1. The RIFF data-size field stays zero because qemu_session
       SIGTERMs QEMU before the backend can finalize the header.
       ffmpeg's wav demuxer reads to EOF anyway; one ``Packet
       corrupt`` warning at the trailing chunk is harmless.

    2. The audiodev only writes samples while SB16 is actively
       producing audio — silent stretches (boot, shell, post-Doom,
       reboot) are simply absent from the file.  The WAV's t=0 is
       therefore "first SB16 sample," not "QEMU launched."
       ``_encode_doom_mp4`` compensates with an ``adelay`` filter
       sized from a wall-clock timestamp snapped right after the
       ``doom`` command lands at the shell.
    """
    return [
        "-audiodev",
        f"wav,id=record,path={audio_path}",
        "-device",
        "sb16,audiodev=record",
    ]


def _build_audio_filter(*, audio_input_index: int, audio_offset_seconds: float) -> str:
    """Build the ffmpeg audio filter that aligns the WAV with the video timeline.

    QEMU's ``wav`` audiodev only writes samples while SB16 is producing
    audio; silent stretches (boot, shell, post-Doom, reboot) are simply
    absent from the WAV, so its t=0 corresponds to "first SB16 sample"
    rather than "QEMU launched".  The resulting WAV is shorter than the
    video and starts late in the recording's timeline.

    We compensate by prepending ``adelay`` (silence padding equal to
    *audio_offset_seconds*) and appending ``apad`` with a bounded
    ``pad_dur`` so the audio stream extends past any plausible video
    duration without running forever.  The caller pairs this with
    ``-shortest`` so the muxer truncates the apad'd audio to the
    actual video duration.  An unbounded apad caused ffmpeg's filter
    queue to grow without limit during smoke testing — pad_dur is the
    safety belt.

    Returns the filter graph stage; output stream is labeled ``[a]``.
    """
    offset_ms = max(0, int(audio_offset_seconds * 1000))
    return f"[{audio_input_index}:a]adelay={offset_ms}|{offset_ms}:all=1,apad=pad_dur={AUDIO_TAIL_PAD_SECONDS}[a]"


def _build_trim_concat_filter(
    *,
    audio_index: int | None,
    bios_end_seconds: float,
    bios_start_seconds: float,
    video_index: int,
) -> tuple[str, str, str | None] | None:
    """Build the ffmpeg trim/concat filter prefix that excises the BIOS window.

    Splits the input video stream at *video_index* (and, if *audio_index*
    is given, the audio stream at that index) into two halves bracketing
    the wall-clock window ``[bios_start_seconds, bios_end_seconds]``,
    then concats the surviving halves so the output stream has the
    interlude removed.  ffmpeg keeps the two streams aligned across
    the concat boundary.

    Returns ``(filter_prefix, video_label, audio_label)`` where the
    labels (``"[v]"`` / ``"[a]"``) are the names ffmpeg will produce
    for the trimmed streams.  Callers ``-map`` those labels.

    Returns ``None`` when *bios_start_seconds* is non-positive — the
    sentinel state (recording aborted before the reboot phase, so the
    markers never updated past their 0.0 initial values).  Callers
    fall back to a straight encode in that case.
    """
    if bios_start_seconds <= 0:
        return None
    video_chain = (
        f"[{video_index}:v]trim=0:{bios_start_seconds},setpts=PTS-STARTPTS[v1];"
        f"[{video_index}:v]trim={bios_end_seconds},setpts=PTS-STARTPTS[v2];"
        f"[v1][v2]concat=n=2:v=1:a=0[v]"
    )
    if audio_index is None:
        return video_chain, "[v]", None
    audio_chain = (
        f"[{audio_index}:a]atrim=0:{bios_start_seconds},asetpts=PTS-STARTPTS[a1];"
        f"[{audio_index}:a]atrim={bios_end_seconds},asetpts=PTS-STARTPTS[a2];"
        f"[a1][a2]concat=n=2:v=0:a=1[a]"
    )
    return f"{video_chain};{audio_chain}", "[v]", "[a]"


def _encode_doom_gif(
    *,
    bios_end_seconds: float,
    bios_start_seconds: float,
    capture_framerate: int,
    frame_directory: Path,
) -> None:
    """Ffmpeg palettegen + paletteuse + gifsicle, downscaling to GIF_WIDTHxGIF_HEIGHT.

    Audio is GIF-incompatible, so the format is video-only.  The
    BIOS-window cut is expressed via the same trim/concat helper as
    the WebM path; both palettegen and paletteuse apply it before
    fps drop + scale + palette work, so the palette is built from
    the same frames the final GIF shows.

    *capture_framerate* tells ffmpeg the real cadence of the input
    PPMs; the ``fps=GIF_FRAMERATE`` filter then drops frames so the
    output gif plays back at real time even when the capture ran
    faster (e.g. 30 fps for the WebM share-pass).  Without that, a
    30-fps capture decoded with ``-framerate 5`` would stretch a
    30-second clip to 3 minutes of playback.
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
    trim = _build_trim_concat_filter(
        audio_index=None,
        bios_end_seconds=bios_end_seconds,
        bios_start_seconds=bios_start_seconds,
        video_index=0,
    )
    if trim is None:
        # No BIOS window: keep the original two-pass commands (cheaper
        # filter graph, identical output when there's no cut to make).
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
    else:
        prefix, video_label, _ = trim
        # palettegen pass: trim → concat → fps/scale → palettegen.
        palettegen_filter = f"{prefix};{video_label}{fps_drop},{scale},palettegen=stats_mode=full[p]"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-framerate",
                str(capture_framerate),
                "-i",
                str(pattern),
                "-filter_complex",
                palettegen_filter,
                "-map",
                "[p]",
                str(palette),
            ],
            check=True,
        )
        # paletteuse pass: trim → concat → fps/scale, then [palette] applied.
        paletteuse_filter = f"{prefix};{video_label}{fps_drop},{scale}[scaled];[scaled][1:v]paletteuse=dither=bayer:bayer_scale=5[g]"
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
                "-filter_complex",
                paletteuse_filter,
                "-map",
                "[g]",
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


def _encode_doom_mp4(
    *,
    audio_offset_seconds: float,
    audio_path: Path,
    bios_end_seconds: float,
    bios_start_seconds: float,
    frame_directory: Path,
) -> None:
    """Ffmpeg → H.264 MP4 with optional AAC audio and BIOS-window cut.

    H.264 + AAC for the broadest browser support (WebM/VP9 flakes in
    older Firefox builds and isn't auto-rendered in GitHub markdown
    previews).  ``-crf 23 -preset medium`` is libx264's standard
    visually-lossless setting; ``-movflags +faststart`` moves the moov
    atom to the head so the file plays before the full byte range is
    downloaded (matters for HTML5 ``<video>`` streaming).  When
    *audio_path* exists and has a non-empty data section, the WAV is
    muxed in as AAC 128 kbps and shifted onto the video timeline by
    *audio_offset_seconds* via adelay (silence pad at the head) + apad
    (silence pad at the tail) + ``-shortest`` (truncate to video
    duration).  When the BIOS-window markers are set, the
    ``trim``/``concat`` filter splices the SeaBIOS interlude out of
    the video stream; the audio doesn't need its own cut because the
    BIOS window falls after Doom has quit (no audio content there).
    """
    if _normalize_to_max_size(frame_directory=frame_directory) == 0:
        message = f"no frames found in {frame_directory}"
        raise RuntimeError(message)
    MP4_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pattern = frame_directory / "frame_%05d.ppm"
    has_audio = audio_path.is_file() and audio_path.stat().st_size > 44  # noqa: PLR2004
    if not has_audio:
        print(
            f"record_doom: {audio_path} missing or empty — encoding silent MP4",
            file=sys.stderr,
        )
    inputs = ["-framerate", str(MP4_FRAMERATE), "-i", str(pattern)]
    if has_audio:
        inputs += ["-i", str(audio_path)]
    trim = _build_trim_concat_filter(
        audio_index=None,
        bios_end_seconds=bios_end_seconds,
        bios_start_seconds=bios_start_seconds,
        video_index=0,
    )
    filter_parts: list[str] = []
    video_map = "0:v"
    if trim is not None:
        video_prefix, video_label, _ = trim
        filter_parts.append(video_prefix)
        video_map = video_label
    audio_map: str | None = None
    if has_audio:
        filter_parts.append(
            _build_audio_filter(
                audio_input_index=1,
                audio_offset_seconds=audio_offset_seconds,
            ),
        )
        audio_map = "[a]"
    argv: list[str] = ["ffmpeg", "-y", *inputs]
    if filter_parts:
        argv += ["-filter_complex", ";".join(filter_parts)]
    argv += ["-map", video_map]
    if audio_map is not None:
        argv += ["-map", audio_map]
    argv += [
        "-c:v",
        "libx264",
        "-crf",
        "23",
        "-preset",
        "medium",
        "-movflags",
        "+faststart",
        "-pix_fmt",
        "yuv420p",
    ]
    if has_audio:
        argv += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
    argv.append(str(MP4_OUTPUT))
    subprocess.run(argv, check=True)


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


def main() -> int:  # noqa: PLR0915
    """Record + encode the Doom clip in the requested format(s)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format",
        choices=("gif", "mp4", "both"),
        default="mp4",
        help="output format (default: mp4)",
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
    # capture run feeds either encode.  MP4 resamples cleanly to 30
    # fps; the GIF encode reads the same PPMs and lets ffmpeg drop
    # frames to its 5 fps target.
    capture_framerate = max(
        MP4_FRAMERATE if arguments.format in {"mp4", "both"} else 0,
        GIF_FRAMERATE if arguments.format in {"gif", "both"} else 0,
    )

    if CAPTURE_DIR.exists():
        shutil.rmtree(CAPTURE_DIR)
    CAPTURE_DIR.mkdir(parents=True)
    audio_path = CAPTURE_DIR / AUDIO_FILENAME

    stop = threading.Event()
    # BIOS-window markers — set right around the reboot so post-processing
    # can excise the SeaBIOS interlude.  Initialised so a teardown before
    # that point leaves the encoders with the sentinel "no cut" state.
    bios_window_start = 0.0
    bios_window_end = 0.0
    with qemu_session(
        drive=DRIVE,
        extra_qemu_args=_audio_qemu_args(audio_path=audio_path),
        memory="64",
        monitor=True,
        snapshot=True,
    ) as session:
        # Snap the capture-start wall clock so the BIOS-window markers
        # below translate cleanly into ffmpeg seconds.  time.time()
        # (not monotonic) because bios_window_* are time.time() too.
        t0 = time.time()
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
            # Wait for Doom's audio-init marker before snapping
            # t_audio_start.  ``[bboeos doom] OPL music`` prints
            # right after the SB16 fd opens and OPL3 detection runs
            # — within a fraction of a second of the first WAV
            # sample being written, so the adelay we hand ffmpeg
            # tracks the actual audio start across QEMU builds with
            # different load times (custom OPL3-patched QEMU is
            # noticeably slower than stock).
            session.wait_for_substring(
                DOOM_AUDIO_READY_NEEDLE,
                start=doom_buffer_offset,
                timeout=20.0,
            )
            t_audio_start = time.time()
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

    if arguments.format in {"mp4", "both"}:
        _encode_doom_mp4(
            audio_offset_seconds=t_audio_start - t0,
            audio_path=audio_path,
            bios_end_seconds=bios_window_end - t0,
            bios_start_seconds=bios_window_start - t0,
            frame_directory=CAPTURE_DIR,
        )
        print(f"wrote {MP4_OUTPUT} ({MP4_OUTPUT.stat().st_size:,} bytes)")
    if arguments.format in {"gif", "both"}:
        _encode_doom_gif(
            bios_end_seconds=bios_window_end - t0,
            bios_start_seconds=bios_window_start - t0,
            capture_framerate=capture_framerate,
            frame_directory=CAPTURE_DIR,
        )
        print(f"wrote {GIF_OUTPUT} ({GIF_OUTPUT.stat().st_size:,} bytes)")
    shutil.rmtree(CAPTURE_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
