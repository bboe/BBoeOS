#!/usr/bin/env python3
"""Tests for cc/ccobj.py and cc.py pack-ccobj / --object modes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "data" / "ccobj"


def test_flat_mode_unchanged_by_object_plumbing(tmp_path: Path) -> None:
    """Flat mode is unchanged by object-mode plumbing.

    The default (flat) mode keeps emitting ``org`` + ``_program_end`` +
    ``_bss_end`` after the ``--object`` plumbing lands.  Regression guard.
    """
    src = tmp_path / "hello.c"
    src.write_text("int main() { return 0; }\n")
    asm = tmp_path / "hello.asm"
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", str(src), str(asm)],
        check=True,
    )

    body = asm.read_text()
    assert "org 08048000h" in body
    assert "_program_end:" in body
    assert "_bss_end equ" in body
    assert '%include "ccobj_markers.inc"' not in body


def test_object_mode_emits_section_directives(tmp_path: Path) -> None:
    """Object mode emits section directives and global declarations.

    Checks that ``--object`` emits ``section .text`` (not ``org``),
    ``global main``, ``%include "ccobj_markers.inc"``, and suppresses the
    flat-mode ``_program_end``/``_bss_end`` trailer.
    """
    src = tmp_path / "hello.c"
    src.write_text("int main() { return 0; }\n")
    asm = tmp_path / "hello.asm"
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", "--object", str(src), str(asm)],
        check=True,
    )

    body = asm.read_text()
    assert '%include "ccobj_markers.inc"' in body
    assert "section .text" in body
    assert "global main" in body
    assert "org 08048000h" not in body
    assert "_program_end:" not in body
    assert "_bss_end equ" not in body


def test_pack_ccobj_basic_fixture(tmp_path: Path) -> None:
    """Fixture .asm exercises every CCREL_* macro.

    The produced .ccobj must list every global symbol and every relocation.
    """
    output = tmp_path / "fixture_basic.ccobj"
    subprocess.run(
        [
            sys.executable,
            str(CC),
            "pack-ccobj",
            str(FIXTURE_DIR / "fixture_basic.bin"),
            str(FIXTURE_DIR / "fixture_basic.lst"),
            str(output),
        ],
        check=True,
    )

    data = json.loads(output.read_text())

    assert data["version"] == 1
    assert set(data["symbols"]) == {"main", "helper", "format_string", "counter", "scratch"}
    assert data["symbols"]["main"] == {"section": "text", "offset": 0, "binding": "global"}
    assert data["symbols"]["helper"]["section"] == "text"
    assert data["symbols"]["format_string"]["section"] == "rodata"
    assert data["symbols"]["counter"]["section"] == "data"
    assert data["symbols"]["scratch"]["section"] == "bss"

    assert set(data["extern"]) == {"die", "_exit", "errno"}

    reloc_summary = [(reloc["symbol"], reloc["type"], reloc["section"]) for reloc in data["relocations"]]
    assert ("die", "rel32", "text") in reloc_summary
    assert ("_exit", "rel32", "text") in reloc_summary
    assert reloc_summary.count(("errno", "abs32", "text")) == 2

    assert data["sections"]["text"]["align"] >= 4
    assert data["sections"]["bss"]["size"] == 64  # 16 dwords


if __name__ == "__main__":
    sys.exit(0 if test_pack_ccobj_basic_fixture(Path("/tmp/test_ccobj_run")) is None else 1)
