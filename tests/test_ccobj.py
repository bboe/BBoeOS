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
