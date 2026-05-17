#!/usr/bin/env python3
"""Tests for tools/ccar.py (cc.py archive packer)."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CCAR = REPO_ROOT / "tools" / "ccar.py"


def _write_ccobj(
    path: Path,
    /,
    *,
    symbols: dict[str, dict],
    text_bytes: bytes = b"\xc3",
) -> None:
    """Materialize a minimal hand-crafted `.ccobj` at ``path``."""
    payload = {
        "extern": [],
        "relocations": [],
        "sections": {
            "text": {
                "align": 16,
                "bytes": base64.b64encode(text_bytes).decode("ascii"),
            },
        },
        "source": str(path),
        "symbols": symbols,
        "version": 1,
    }
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)


def test_ccar_rejects_input_outside_output_directory(tmp_path: Path) -> None:
    """Ccar requires inputs to live in the same directory as ``--output``."""
    other_directory = tmp_path / "elsewhere"
    other_directory.mkdir()
    object_payload = other_directory / "errno.ccobj"
    _write_ccobj(
        object_payload,
        symbols={"errno": {"section": "text", "offset": 0, "binding": "global"}},
    )
    output = tmp_path / "lib.ccar"
    result = subprocess.run(
        [sys.executable, str(CCAR), "--output", str(output), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "directory" in result.stderr.lower()
    assert not output.exists()


def test_ccar_writes_manifest_with_provides(tmp_path: Path) -> None:
    """Ccar writes a JSON manifest with each member's provides list."""
    a = tmp_path / "errno.ccobj"
    b = tmp_path / "die.ccobj"
    _write_ccobj(
        a,
        symbols={"errno": {"section": "text", "offset": 0, "binding": "global"}},
    )
    _write_ccobj(
        b,
        symbols={
            "die": {"section": "text", "offset": 0, "binding": "global"},
            "_die_helper": {"section": "text", "offset": 4, "binding": "local"},
        },
    )
    output = tmp_path / "lib.ccar"
    subprocess.run(
        [sys.executable, str(CCAR), "--output", str(output), str(a), str(b)],
        check=True,
    )

    with output.open(encoding="utf-8") as file:
        manifest = json.load(file)
    assert manifest["version"] == 1
    members = {entry["file"]: entry for entry in manifest["members"]}
    assert set(members) == {"errno.ccobj", "die.ccobj"}
    assert members["errno.ccobj"]["provides"] == ["errno"]
    # `_die_helper` is local, so it must NOT appear in `provides`.
    assert members["die.ccobj"]["provides"] == ["die"]
