"""Unit tests for ports/doom/record.py's pure helpers.

The recording itself needs QEMU + ffmpeg + a Doom WAD and isn't suited
to pytest; these tests cover the BIOS-window filter-chain builder,
which is a pure-string function and easy to lock down against
regressions (a silent regression to "no trim" would mean the BIOS
splash sneaks back into every release clip).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "ports" / "doom"))

from record import (  # noqa: E402
    _build_audio_filter,  # noqa: PLC2701
    _build_trim_concat_filter,  # noqa: PLC2701
)


def test_audio_filter_bounded_apad_prevents_runaway() -> None:
    """The apad stage must be bounded — unbounded apad starves ffmpeg's filter queue."""
    result = _build_audio_filter(audio_input_index=1, audio_offset_seconds=10.0)
    assert "apad=pad_dur=" in result


def test_audio_filter_clamps_negative_offset_to_zero() -> None:
    """Negative offsets (shouldn't happen in practice) clamp to 0."""
    result = _build_audio_filter(audio_input_index=1, audio_offset_seconds=-5.0)
    assert "adelay=0|0:all=1" in result


def test_audio_filter_emits_offset_in_milliseconds() -> None:
    """A 10s offset becomes adelay=10000 (ms) on each channel."""
    result = _build_audio_filter(audio_input_index=1, audio_offset_seconds=10.0)
    assert "[1:a]adelay=10000|10000:all=1" in result
    assert result.endswith("[a]")


def test_audio_filter_rounds_fractional_seconds_to_ms() -> None:
    """2.5 s → 2500 ms; the helper truncates rather than rounds."""
    result = _build_audio_filter(audio_input_index=1, audio_offset_seconds=2.5)
    assert "adelay=2500|2500" in result


def test_audio_filter_uses_named_input_index() -> None:
    """The audio_input_index parameter threads into the input selector."""
    result = _build_audio_filter(audio_input_index=3, audio_offset_seconds=1.0)
    assert result.startswith("[3:a]")


def test_filter_segments_are_semicolon_separated() -> None:
    """The prefix must be a valid filter graph: stages joined by ';'."""
    result = _build_trim_concat_filter(
        audio_index=1,
        bios_end_seconds=2.0,
        bios_start_seconds=1.0,
        video_index=0,
    )
    assert result is not None
    prefix, _, _ = result
    # Six stages: 2x trim, 1x concat, 2x atrim, 1x aconcat.
    assert prefix.count(";") == 5
    assert not prefix.strip().endswith(";")


def test_returns_none_when_bios_start_negative() -> None:
    """A negative offset (recording aborted pre-reboot) also skips the trim."""
    assert (
        _build_trim_concat_filter(
            audio_index=None,
            bios_end_seconds=2.0,
            bios_start_seconds=-1.0,
            video_index=0,
        )
        is None
    )


def test_returns_none_when_bios_window_unset() -> None:
    """If both bios markers are zero (initial-value state), no trim happens."""
    assert (
        _build_trim_concat_filter(
            audio_index=None,
            bios_end_seconds=0.0,
            bios_start_seconds=0.0,
            video_index=0,
        )
        is None
    )


def test_video_only_filter_shape() -> None:
    """audio_index=None emits only the video trim/concat chain."""
    result = _build_trim_concat_filter(
        audio_index=None,
        bios_end_seconds=20.5,
        bios_start_seconds=15.0,
        video_index=0,
    )
    assert result is not None
    prefix, video_label, audio_label = result
    assert "[0:v]trim=0:15.0" in prefix
    assert "[0:v]trim=20.5" in prefix
    assert "[v1][v2]concat=n=2:v=1:a=0[v]" in prefix
    assert "atrim" not in prefix
    assert video_label == "[v]"
    assert audio_label is None


def test_video_plus_audio_filter_shape() -> None:
    """audio_index=1 emits parallel video and audio trim/concat chains."""
    result = _build_trim_concat_filter(
        audio_index=1,
        bios_end_seconds=20.5,
        bios_start_seconds=15.0,
        video_index=0,
    )
    assert result is not None
    prefix, video_label, audio_label = result
    assert "[0:v]trim=0:15.0" in prefix
    assert "[0:v]trim=20.5" in prefix
    assert "[v1][v2]concat=n=2:v=1:a=0[v]" in prefix
    assert "[1:a]atrim=0:15.0" in prefix
    assert "[1:a]atrim=20.5" in prefix
    assert "[a1][a2]concat=n=2:v=0:a=1[a]" in prefix
    assert video_label == "[v]"
    assert audio_label == "[a]"
