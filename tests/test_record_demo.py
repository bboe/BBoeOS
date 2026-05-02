#!/usr/bin/env python3
"""Unit tests for tools/record_demo.py pure helpers."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import record_demo  # noqa: E402


def test_default_scenario_assembler_section_starts_with_editor() -> None:
    """The assembler section should open the editor on demo.asm before assembling/running."""
    scenario = record_demo.default_scenario()
    assembler = next(section for section in scenario.sections if section.name == "assembler")
    assert isinstance(assembler.steps[0], record_demo.EditorStep)
    assert assembler.steps[0].filename == "src/demo.asm"
    assert "FUNCTION_PRINT_STRING" in assembler.steps[0].content
    follow_ups = [step.command for step in assembler.steps[1:]]
    assert follow_ups == ["asm src/demo.asm demo", "demo"]


def test_default_scenario_final_step_reboots_without_awaiting_prompt() -> None:
    """The trailing 'reboot' step should not wait for a prompt and should hold for FINAL_PAUSE_SECONDS.

    Skipping ``wait_for_prompt`` matters because ``reboot`` triple-faults
    the CPU and the next prompt comes from a fresh boot, which we want
    to bleed into the GIF's start so the loop is seamless.
    """
    scenario = record_demo.default_scenario()
    final_step = scenario.sections[-1].steps[-1]
    assert final_step.command == "reboot"
    assert final_step.await_prompt is False
    assert final_step.pause_after == record_demo.FINAL_PAUSE_SECONDS


def test_default_scenario_has_five_sections() -> None:
    """The default scenario should contain the five named demo sections in order."""
    scenario = record_demo.default_scenario()
    section_names = [section.name for section in scenario.sections]
    assert section_names == ["boot", "shell", "assembler", "networking", "reboot"]


def test_default_scenario_steps_are_nonempty() -> None:
    """Every non-boot section should have at least one step."""
    scenario = record_demo.default_scenario()
    for section in scenario.sections:
        if section.name == "boot":
            assert section.steps == []
        else:
            assert section.steps, f"section {section.name} has no steps"


def test_gifsicle_argv_optimises_in_place() -> None:
    """Gifsicle should be invoked with -O3 and the explicit output path."""
    argv = record_demo.gifsicle_argv(
        intermediate=Path("/tmp/out.gif"),
        output=Path("/dest/demo.gif"),
    )
    assert argv == ["gifsicle", "-O3", "-o", "/dest/demo.gif", "/tmp/out.gif"]


def test_palettegen_argv_uses_expected_filter() -> None:
    """The palettegen argv should drive ffmpeg with -framerate, -i, -vf, output."""
    argv = record_demo.palettegen_argv(
        frame_pattern=Path("/tmp/frame_%05d.ppm"),
        framerate=10,
        palette=Path("/tmp/palette.png"),
    )
    assert argv[0] == "ffmpeg"
    assert "-framerate" in argv
    assert "10" in argv
    assert "-i" in argv
    assert "/tmp/frame_%05d.ppm" in argv
    assert "palettegen" in " ".join(argv)
    assert argv[-1] == "/tmp/palette.png"


def test_paletteuse_argv_consumes_two_inputs_and_lavfi() -> None:
    """The paletteuse argv should pass two -i inputs and a -lavfi paletteuse filter."""
    argv = record_demo.paletteuse_argv(
        frame_pattern=Path("/tmp/frame_%05d.ppm"),
        framerate=10,
        intermediate=Path("/tmp/out.gif"),
        palette=Path("/tmp/palette.png"),
    )
    assert argv.count("-i") == 2
    assert "/tmp/frame_%05d.ppm" in argv
    assert "/tmp/palette.png" in argv
    assert "-lavfi" in argv
    assert "paletteuse" in " ".join(argv)
    assert argv[-1] == "/tmp/out.gif"


def test_type_at_human_pace_jitter_is_within_window() -> None:
    """Per-character sleep durations stay inside delay +- jitter."""
    sleeps: list[float] = []

    record_demo.type_at_human_pace(
        delay_seconds=0.06,
        jitter_seconds=0.02,
        pre_terminator_seconds=0.0,
        sleep=sleeps.append,
        text="x" * 200,
        writer=lambda _text: None,
    )
    assert all(0.04 <= sleep_value <= 0.08 for sleep_value in sleeps)


def test_type_at_human_pace_skips_pre_terminator_sleep_when_zero() -> None:
    """No pre-terminator sleep is issued when pre_terminator_seconds is zero."""
    sleeps: list[float] = []

    record_demo.type_at_human_pace(
        delay_seconds=0.05,
        jitter_seconds=0.0,
        pre_terminator_seconds=0.0,
        sleep=sleeps.append,
        text="ab",
        writer=lambda _text: None,
    )
    assert sleeps == [0.05, 0.05]


def test_type_at_human_pace_sleeps_between_characters() -> None:
    """One sleep per typed character, plus a final pre-terminator sleep."""
    sleeps: list[float] = []

    record_demo.type_at_human_pace(
        delay_seconds=0.05,
        jitter_seconds=0.0,
        pre_terminator_seconds=0.4,
        sleep=sleeps.append,
        text="ab",
        writer=lambda _text: None,
    )
    assert sleeps == [0.05, 0.05, 0.4]


def test_type_at_human_pace_terminator_is_configurable() -> None:
    """An explicit *terminator* replaces the default carriage return."""
    received: list[str] = []

    record_demo.type_at_human_pace(
        sleep=lambda _seconds: None,
        terminator="\n",
        text="x",
        writer=received.append,
    )
    assert received == ["x", "\n"]


def test_type_at_human_pace_writes_each_character_then_carriage_return() -> None:
    """Each character of *text* is sent through *writer*, followed by a CR."""
    received: list[str] = []

    record_demo.type_at_human_pace(
        sleep=lambda _seconds: None,
        text="ls",
        writer=received.append,
    )
    assert received == ["l", "s", "\r"]


def test_type_editor_program_does_not_pause_before_newlines() -> None:
    """The editor path should suppress the pre-terminator pause: only delay sleeps."""
    sleeps: list[float] = []

    record_demo.type_editor_program(
        content="ab\ncd\n",
        delay_seconds=0.05,
        jitter_seconds=0.0,
        sleep=sleeps.append,
        writer=lambda _text: None,
    )
    assert sleeps == [0.05, 0.05, 0.05, 0.05]


def test_type_editor_program_terminates_each_line_with_newline() -> None:
    r"""Each line of editor content is followed by a single ``\n``, no trailing CR."""
    received: list[str] = []

    record_demo.type_editor_program(
        content="ab\ncd\n",
        sleep=lambda _seconds: None,
        writer=received.append,
    )
    assert received == ["a", "b", "\n", "c", "d", "\n"]
