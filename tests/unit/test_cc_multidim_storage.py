"""Contiguous row-major storage for multidimensional arrays (Stage 3b).

Verifies that programs declaring multidimensional arrays COMPILE (returncode 0)
after the storage guard is removed in Task 3.  Subscript access is a later task,
so these tests only declare and sizeof-check the arrays — no m[i][j] reads or
writes.

sizeof values may be wrong until Task 4 fixes the sizeof codegen; these tests
do NOT assert the sizeof value, only that compilation succeeds.
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


def test_multidim_3d_local_storage_compiles(tmp_path: Path) -> None:
    """A 3-D local array declaration compiles without error."""
    result = _compile("int main(void){ int cube[2][3][4]; return sizeof(cube); }\n", tmp_path)
    assert result.returncode == 0, result.stderr


def test_multidim_char_local_storage_compiles(tmp_path: Path) -> None:
    """A local char multidim array declaration compiles without error."""
    result = _compile("int main(void){ char grid[4][8]; return sizeof(grid); }\n", tmp_path)
    assert result.returncode == 0, result.stderr


def test_multidim_global_storage_compiles(tmp_path: Path) -> None:
    """A file-scope int multidim array declaration compiles without error."""
    result = _compile("int g[2][3];\nint main(void){ return sizeof(g); }\n", tmp_path)
    assert result.returncode == 0, result.stderr


def test_multidim_initializer_rejected(tmp_path: Path) -> None:
    """A multidim array with a brace initializer is rejected with a clear error."""
    result = _compile("int main(void){ int m[2][3] = {0}; return 0; }\n", tmp_path)
    assert result.returncode != 0
    assert "initializer" in result.stderr.lower()


def test_multidim_local_storage_compiles(tmp_path: Path) -> None:
    """A function-local int multidim array declaration compiles without error."""
    result = _compile("int main(void){ int m[2][3]; return sizeof(m); }\n", tmp_path)
    assert result.returncode == 0, result.stderr


def test_multidim_unsigned_short_global_storage_compiles(tmp_path: Path) -> None:
    """A file-scope unsigned short multidim array declaration compiles without error."""
    result = _compile("unsigned short table[3][4];\nint main(void){ return sizeof(table); }\n", tmp_path)
    assert result.returncode == 0, result.stderr
