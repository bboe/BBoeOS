"""Subscript access for contiguous multidimensional arrays ``m[i][j]``.

Tasks 5/6 give every 2+-subscript access a single uniform parser shape and
a type-driven codegen dispatch: contiguous multidim arrays get row-major
(Horner) addressing; arrays of pointers reconstruct the legacy deref shape
for byte-identical output (proved by the function-size / place byte gates).

These tests prove the new path compiles and emits the correct row-major
displacement for a constant-index case, plus that the array-of-pointers
path still compiles.
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


def test_array_of_pointers_two_subscript_still_compiles(tmp_path: Path) -> None:
    """An array-of-pointers ``grid[i][j]`` still compiles (legacy deref path)."""
    src = "int r0[3]; int r1[3]; int* grid[2];\nint main(void){ grid[0]=r0; grid[1]=r1; grid[1][2]=9; return grid[1][2]; }\n"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr


def test_contiguous_char_two_dim_byte_store(tmp_path: Path) -> None:
    """A char[2][3] uses a 1-byte element stride and `mov byte`."""
    src = "int main(void){ char g[2][3]; g[1][2]=65; return g[1][2]; }\n"
    out = tmp_path / "test.asm"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr
    asm = out.read_text()
    # byte offset (1*3 + 2)*1 = 5
    assert "+5]" in asm
    assert "mov byte" in asm


def test_contiguous_three_dim_compiles(tmp_path: Path) -> None:
    """A 3-D contiguous array subscript ``m[i][j][k]`` compiles."""
    result = _compile("int main(void){ int m[2][2][2]; m[1][1][1]=5; return m[1][1][1]; }\n", tmp_path)
    assert result.returncode == 0, result.stderr


def test_contiguous_two_dim_compiles(tmp_path: Path) -> None:
    """A 2-D contiguous array subscript ``m[i][j]`` compiles."""
    result = _compile("int main(void){ int m[2][3]; m[1][2]=7; return m[1][2]; }\n", tmp_path)
    assert result.returncode == 0, result.stderr


def test_contiguous_two_dim_constant_index_row_major_displacement(tmp_path: Path) -> None:
    """``int m[2][3]; m[1][2]`` lands at byte offset (1*3+2)*4 = 20."""
    src = "int main(void){ int m[2][3]; m[1][2]=7; return m[1][2]; }\n"
    out = tmp_path / "test.asm"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr
    asm = out.read_text()
    assert "+20]" in asm


def test_three_dim_constant_index_row_major_displacement(tmp_path: Path) -> None:
    """``int c[2][3][4]; c[1][2][3]`` lands at ((1*3+2)*4+3)*4 = 92."""
    src = "int main(void){ int c[2][3][4]; c[1][2][3]=1; return c[1][2][3]; }\n"
    out = tmp_path / "test.asm"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr
    asm = out.read_text()
    assert "+92]" in asm


def test_wrong_subscript_count_rejected(tmp_path: Path) -> None:
    """Subscripting a 2-D array with 3 indices is a compile error."""
    src = "int main(void){ int m[2][3]; return m[1][2][0]; }\n"
    result = _compile(src, tmp_path)
    assert result.returncode != 0
    assert "wrong number of subscripts" in result.stderr
