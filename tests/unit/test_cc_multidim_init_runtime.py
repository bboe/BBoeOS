"""Codegen for multidimensional array initializers (local + global, zero-fill).

Tasks 2 & 3: ``int m[2][3] = {{1,2,3},{4,5,6}}`` (nested) and the flat form
``= {1,2,3,4,5,6}`` now lay down row-major contiguous storage.  Globals emit a
single ``db`` / ``dw`` / ``dd`` directive carrying every flattened element, with
a ``times (total-count)*stride db 0`` tail when the initializer is partial.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CC = REPO_ROOT / "cc.py"


def _compile(source: str, tmp_path: Path, /) -> subprocess.CompletedProcess[str]:
    src = tmp_path / "test.c"
    out = tmp_path / "test.asm"
    src.write_text(source)
    return subprocess.run(
        ["python3", str(CC), "--bits", "32", str(src), str(out)],
        capture_output=True,
        check=False,
        cwd=str(REPO_ROOT),
        text=True,
    )


def test_global_char_nested_init(tmp_path: Path) -> None:
    """``char c[2][2] = {{1,2},{3,4}}`` emits ``db 1, 2, 3, 4``."""
    result = _compile("char c[2][2] = {{1,2},{3,4}};\nint main(void){ return c[0][0]; }\n", tmp_path)
    assert result.returncode == 0, result.stderr
    asm = (tmp_path / "test.asm").read_text()
    assert "db 1, 2, 3, 4" in asm


def test_global_flat_init(tmp_path: Path) -> None:
    """The flat form ``= {1,2,3,4,5,6}`` emits the same row-major run as nested."""
    result = _compile("int g[2][3] = {1,2,3,4,5,6};\nint main(void){ return g[0][0]; }\n", tmp_path)
    assert result.returncode == 0, result.stderr
    asm = (tmp_path / "test.asm").read_text()
    assert "dd 1, 2, 3, 4, 5, 6" in asm


def test_global_nested_init(tmp_path: Path) -> None:
    """``int g[2][3] = {{1,2,3},{4,5,6}}`` emits ``dd 1, 2, 3, 4, 5, 6``."""
    result = _compile("int g[2][3] = {{1,2,3},{4,5,6}};\nint main(void){ return g[0][0]; }\n", tmp_path)
    assert result.returncode == 0, result.stderr
    asm = (tmp_path / "test.asm").read_text()
    assert "dd 1, 2, 3, 4, 5, 6" in asm


def test_global_partial_init_zero_fills(tmp_path: Path) -> None:
    """A partial ``int g[2][3] = {1,2,3}`` emits the run plus a zero-fill tail."""
    result = _compile("int g[2][3] = {1,2,3};\nint main(void){ return g[0][0]; }\n", tmp_path)
    assert result.returncode == 0, result.stderr
    asm = (tmp_path / "test.asm").read_text()
    assert "dd 1, 2, 3" in asm
    assert "times (6-3)*4 db 0" in asm


def test_too_many_initializers_rejected(tmp_path: Path) -> None:
    """More elements than the array holds is a clean compile error."""
    result = _compile("int g[2][2] = {1,2,3,4,5};\nint main(void){ return 0; }\n", tmp_path)
    assert result.returncode != 0
    assert "too many initializers" in result.stderr.lower()
