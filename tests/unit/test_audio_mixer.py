"""Pytest unit tests for the SFX mixer used by tools/doom/i_sound_bboeos.c.

Compiles tools/doom/audio_mixer.c against the host system clang as a
shared library and exercises the mixer through its small public API
(mixer_reset, mixer_start_voice, mixer_stop_voice, mixer_voice_active,
mixer_render).  Same shape as tests/unit/test_libbboeos.py.

Run with: ``pytest tests/unit/test_audio_mixer.py``
"""

from __future__ import annotations

import ctypes
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIXER_SOURCE = REPO_ROOT / "tools" / "doom" / "audio_mixer.c"


def _build_shared_library() -> ctypes.CDLL:
    """Compile audio_mixer.c into a host shared library and load it."""
    temp_directory = tempfile.mkdtemp()
    library_path = Path(temp_directory) / "audio_mixer.so"
    subprocess.check_call([
        "clang",
        "-O2",
        "-Wall",
        "-Werror",
        "-shared",
        "-fPIC",
        f"-I{MIXER_SOURCE.parent}",
        str(MIXER_SOURCE),
        "-o",
        str(library_path),
    ])
    library = ctypes.CDLL(str(library_path))
    library.mixer_reset.argtypes = []
    library.mixer_reset.restype = None
    library.mixer_start_voice.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.mixer_start_voice.restype = ctypes.c_int
    library.mixer_stop_voice.argtypes = [ctypes.c_int]
    library.mixer_stop_voice.restype = None
    library.mixer_voice_active.argtypes = [ctypes.c_int]
    library.mixer_voice_active.restype = ctypes.c_int
    library.mixer_render.argtypes = [
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_int,
    ]
    library.mixer_render.restype = None
    return library


_LIBRARY = _build_shared_library()

# Keep ctypes arrays alive across mixer_render calls — the C side
# stores raw pointers into voice->samples; if Python frees the array
# before render reads from it, we read freed memory.
_KEEPALIVE: list = []


def _render(*, length: int) -> list[int]:
    out = (ctypes.c_uint8 * length)()
    _LIBRARY.mixer_render(out, length)
    return list(out)


def _start(*, samples: list[int], voice: int, volume: int = 255) -> None:
    array = (ctypes.c_uint8 * len(samples))(*samples)
    _KEEPALIVE.append(array)
    assert _LIBRARY.mixer_start_voice(voice, array, len(samples), volume) == 1


def test_invalid_voice_index_rejected() -> None:
    """mixer_start_voice rejects out-of-range voice indices."""
    _LIBRARY.mixer_reset()
    array = (ctypes.c_uint8 * 1)(128)
    assert _LIBRARY.mixer_start_voice(8, array, 1, 255) == 0
    assert _LIBRARY.mixer_start_voice(-1, array, 1, 255) == 0


def test_silence_when_no_voices() -> None:
    """Empty mixer renders the 8-bit unsigned silence midpoint (128)."""
    _LIBRARY.mixer_reset()
    assert _render(length=64) == [128] * 64


def test_single_voice_passthrough_at_full_volume() -> None:
    """One voice at volume 255 renders sample bytes through unchanged."""
    _LIBRARY.mixer_reset()
    _start(samples=[10, 20, 30, 40, 50, 60, 70, 80], voice=0)
    assert _render(length=8) == [10, 20, 30, 40, 50, 60, 70, 80]


def test_stop_voice_silences_immediately() -> None:
    """mixer_stop_voice halts the voice; subsequent renders see silence."""
    _LIBRARY.mixer_reset()
    _start(samples=[200, 200, 200, 200], voice=0)
    _LIBRARY.mixer_stop_voice(0)
    assert _LIBRARY.mixer_voice_active(0) == 0
    assert _render(length=4) == [128, 128, 128, 128]


def test_two_voices_sum_clamps_at_255() -> None:
    """Two voices each at 200 (above midpoint) saturate output to 255."""
    _LIBRARY.mixer_reset()
    _start(samples=[200, 200, 200, 200], voice=0)
    _start(samples=[200, 200, 200, 200], voice=1)
    rendered = _render(length=4)
    assert all(value == 255 for value in rendered), rendered


def test_voice_finishes_and_deactivates() -> None:
    """Voice with finite-length samples deactivates once the stream is consumed."""
    _LIBRARY.mixer_reset()
    _start(samples=[200, 200], voice=0)
    _render(length=2)
    assert _LIBRARY.mixer_voice_active(0) == 0
    assert _render(length=2) == [128, 128]


def test_volume_attenuates_voice() -> None:
    """Half-volume attenuates the deviation from midpoint by half."""
    # +100 above midpoint at half volume → +50 above midpoint = ~178.
    # Allow ±2 for integer rounding.
    _LIBRARY.mixer_reset()
    _start(samples=[228], voice=0, volume=128)
    rendered = _render(length=1)
    assert 176 <= rendered[0] <= 180, rendered
