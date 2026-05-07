#!/usr/bin/env python3
"""End-to-end Doom OPL3 music smoke test.

Boots Doom under QEMU with `-device sb16,audiodev=a -audiodev wav,id=a,...`,
runs Doom for ~25 s, and asserts:

  1. The captured serial log contains ``"[bboeos doom] OPL music enabled"``
     — proving ``BBoe_MusicInit`` (in tools/doom/i_sound_bboeos.c) called
     the upstream chocolate-doom ``music_opl_module.Init`` and got a
     non-zero return, which means ``OPL_Init`` opened ``/dev/midi`` and
     the kernel reported OPL3 presence.
  2. The captured WAV is non-silent (RMS above a low threshold) and at
     least 20 s long, matching the existing sound test — proving the
     engine ran for the full window.

The serial-log marker is the discriminator that distinguishes "music
plays" from "music doesn't play".  We can't acoustically detect OPL3
output in the captured WAV: QEMU 8.x's SB16 device emulates only the
DSP (PCM SFX), not the OPL FM synthesizer.  Writes to OPL ports
0x388/0x389 are accepted but produce no audio in the audiodev wav
output.  QEMU does ship an ``adlib`` device with OPL2 audio synthesis,
but it can't be attached alongside ``sb16`` (port-region collision in
QEMU 8.2's hw/audio).  See docs/superpowers/specs/2026-05-04-doom-music-opl3-design.md
for the design and tradeoffs.

The marker-line approach is honest about that limitation: if music is
broken at the BBoe_MusicInit / OPL_Init layer (kernel /dev/midi missing,
g_opl3_present = 0, chocolate-doom's I_OPL_InitMusic returning false),
the marker is replaced by ``"[bboeos doom] OPL music unavailable"``
and the test fails.  This catches the realistic regressions: kernel
device wiring breaks, the chocolate-doom build links broken, the OPL
probe fails.

Gated on wads/doom1.wad presence, matching test_doom_sound_qemu.py —
the test inherently needs the engine running through real music init
to produce the marker line and a non-empty WAV.
"""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOOM_BINARY = REPO / "build" / "doom" / "doom"
WAD_FILE = REPO / "wads" / "doom1.wad"

sys.path.insert(0, str(REPO / "tests"))

from run_qemu import qemu_session  # noqa: E402

MUSIC_ENABLED_MARKER = "[bboeos doom] OPL music enabled"
MUSIC_UNAVAILABLE_MARKER = "[bboeos doom] OPL music unavailable"


def _build_doom() -> None:
    """Build the doom binary via tools/build_doom.py."""
    subprocess.check_call([sys.executable, str(REPO / "tools" / "build_doom.py")])


def _install_doom_and_wad() -> None:
    """Build a fresh ext2 image and add bin/doom + doom1.wad."""
    subprocess.check_call(
        ["./make_os.sh", "--ext2", "--sectors=20480"],
        cwd=REPO,
    )
    subprocess.check_call(
        ["./add_file.py", "-x", "-d", "bin", str(DOOM_BINARY)],
        cwd=REPO,
    )
    subprocess.check_call(
        ["./add_file.py", "--image", "drive.img", str(WAD_FILE)],
        cwd=REPO,
    )


def _measure_wav(*, wav_path: Path) -> dict:
    """Return duration_s + RMS for the captured WAV.

    Parses the raw bytes manually rather than via the stdlib ``wave``
    module: QEMU only writes the chunk-size fields when its audio
    backend shuts down cleanly, and our ``qemu_session`` SIGTERMs the
    process at the end of the test, so the on-disk RIFF/data sizes
    stay zero.  We trust the rest of the header (channel count + rate
    + width = 16-bit) and compute duration from the file length.
    """
    raw = wav_path.read_bytes()
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        message = f"{wav_path}: missing RIFF/WAVE header"
        raise SystemExit(message)
    channels = struct.unpack("<H", raw[22:24])[0]
    rate = struct.unpack("<I", raw[24:28])[0]
    bits_per_sample = struct.unpack("<H", raw[34:36])[0]
    if bits_per_sample != 16:
        message = f"unexpected sample width: {bits_per_sample} bits"
        raise SystemExit(message)
    bytes_per_frame = channels * 2
    sample_bytes = raw[44:]
    frame_count = len(sample_bytes) // bytes_per_frame
    if frame_count == 0:
        return {"duration": 0.0, "rms": 0.0}
    samples = list(struct.unpack("<" + "h" * (frame_count * channels), sample_bytes[: frame_count * bytes_per_frame]))
    if channels == 2:
        samples = [(samples[i] + samples[i + 1]) / 2 for i in range(0, len(samples), 2)]
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    return {"duration": frame_count / rate, "rms": rms}


def _test_music_with_wad() -> None:
    """Run doom under QEMU+SB16 and verify music init via a serial-log marker."""
    if not WAD_FILE.is_file():
        print(f"SKIP doom-music test: {WAD_FILE} not present (run tools/fetch_wad.sh)")
        return
    _install_doom_and_wad()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_handle:
        wav_path = Path(temp_handle.name)
    # QEMU's wav backend writes a WAV header at start and refuses to
    # overwrite an existing file; drop the empty placeholder mkstemp
    # left behind so QEMU sees a clean creation path.
    wav_path.unlink()
    try:
        with qemu_session(
            memory="64",
            extra_qemu_args=[
                "-audiodev",
                f"wav,id=a,path={wav_path}",
                "-device",
                "sb16,audiodev=a",
            ],
        ) as session:
            session.write_serial("doom\r")
            # Drain serial for the same window as the sound test.
            # The engine prints the music init marker early in boot
            # (before the title screen), so a 25 s window is plenty.
            session.drain_serial(seconds=25)
            output = session.output
        metrics = _measure_wav(wav_path=wav_path)
        print(f"doom music metrics: {metrics}")
        if MUSIC_UNAVAILABLE_MARKER in output:
            sys.exit(
                f"FAIL: saw {MUSIC_UNAVAILABLE_MARKER!r} in serial output — "
                f"music subsystem reported failure (check kernel /dev/midi "
                f"and OPL3 probe path)",
            )
        if MUSIC_ENABLED_MARKER not in output:
            tail = output[-600:] if output else "<empty>"
            sys.exit(
                f"FAIL: never saw {MUSIC_ENABLED_MARKER!r} in serial output; "
                f"the music init marker is missing (engine may have crashed "
                f"before init or the marker print was removed). tail={tail!r}",
            )
        if metrics["duration"] < 20.0:
            sys.exit(f"FAIL: duration {metrics['duration']:.2f} s < 20 s")
        if metrics["rms"] < 100.0:
            sys.exit(f"FAIL: RMS {metrics['rms']:.0f} too low — likely silent")
        print("doom music test pass")
    finally:
        if wav_path.exists():
            wav_path.unlink()


def main() -> None:
    """Build doom, run the SB16 capture test focused on music init."""
    _build_doom()
    _test_music_with_wad()


if __name__ == "__main__":
    main()
