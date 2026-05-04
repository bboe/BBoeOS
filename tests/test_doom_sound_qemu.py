#!/usr/bin/env python3
"""End-to-end Doom sound smoke test.

Boots Doom under QEMU with `-device sb16,audiodev=a -audiodev wav,id=a,...`,
plays for ~30 s, then opens the captured WAV and asserts:

  1. Duration >= 25 s (proves the engine ran for the full window).
  2. RMS energy above a threshold (proves output is not silence).
  3. Zero-crossings nonzero (proves output is not stuck at a DC level).

Gated on wads/doom1.wad presence, matching the main-loop half of
tests/test_doom_qemu.py — the test inherently needs the engine running
through real SFX playback to produce a non-empty WAV.
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
    """Return duration_s + RMS + zero_crossings for the captured WAV.

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
        return {"duration": 0.0, "rms": 0.0, "zero_crossings": 0}
    samples = list(struct.unpack("<" + "h" * (frame_count * channels), sample_bytes[: frame_count * bytes_per_frame]))
    if channels == 2:
        samples = [(samples[i] + samples[i + 1]) / 2 for i in range(0, len(samples), 2)]
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    zero_crossings = sum(1 for i in range(1, len(samples)) if (samples[i - 1] >= 0) != (samples[i] >= 0))
    return {
        "duration": frame_count / rate,
        "rms": rms,
        "zero_crossings": zero_crossings,
    }


def _test_sound_with_wad() -> None:
    """Run doom under QEMU+SB16, capture WAV, check it has real audio."""
    if not WAD_FILE.is_file():
        print(f"SKIP doom-sound test: {WAD_FILE} not present (run tools/fetch_wad.sh)")
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
            session.drain_serial(seconds=30)
        # qemu_session's __exit__ kills QEMU, which flushes the WAV file.
        metrics = _measure_wav(wav_path=wav_path)
        print(f"doom sound metrics: {metrics}")
        if metrics["duration"] < 25.0:
            sys.exit(f"FAIL: duration {metrics['duration']:.2f} s < 25 s")
        if metrics["rms"] < 100.0:
            sys.exit(f"FAIL: RMS {metrics['rms']:.0f} too low — likely silent")
        if metrics["zero_crossings"] < 100:
            sys.exit(f"FAIL: zero crossings {metrics['zero_crossings']} too few — likely DC level")
        print("doom sound test pass")
    finally:
        if wav_path.exists():
            wav_path.unlink()


def main() -> None:
    """Build doom, run the SB16 capture test."""
    _build_doom()
    _test_sound_with_wad()


if __name__ == "__main__":
    main()
