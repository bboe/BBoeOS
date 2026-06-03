"""Row-major addressing for multidimensional array struct fields.

Task 3 lifts the struct-field multidim guard and routes ``g.field[i][j]``
(dot, struct value) and ``p->field[i][j]`` (arrow, struct pointer) through a
dedicated row-major (Horner) addressing path keyed on the FIELD's declared
dimensions.  Constant indices fold into a static displacement equal to
``field_offset + (row * inner_count + col) * element_size``; the terminal
load/store uses the field's innermost element size (so int / 4-byte elements
work, bypassing the bespoke member-index 1/2-byte gate).

These tests pin the emitted displacement and element-size shape for the
constant-index cases; the QEMU program proves end-to-end row-major values.
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


def test_arrow_int_constant_index_row_major_displacement(tmp_path: Path) -> None:
    """``p->cells[1][2]`` on ``struct{int cells[2][3];}`` lands at +20 via the pointer."""
    src = "struct grid { int cells[2][3]; };\nint main(void){ struct grid g; struct grid* p = &g; return p->cells[1][2]; }\n"
    out = tmp_path / "test.asm"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr
    asm = out.read_text()
    # field_offset 0 + (1*3 + 2)*4 = 20
    assert "+20]" in asm


def test_char_field_constant_index_byte_stride(tmp_path: Path) -> None:
    """``g.cells[1][2]`` on a char[2][3] field uses a 1-byte stride and `mov byte`."""
    src = "struct grid { char cells[2][3]; };\nint main(void){ struct grid g; g.cells[1][2] = 65; return g.cells[1][2]; }\n"
    out = tmp_path / "test.asm"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr
    asm = out.read_text()
    # field_offset 0 + (1*3 + 2)*1 = 5
    assert "+5]" in asm
    assert "mov byte" in asm


def test_dot_int_constant_index_row_major_displacement(tmp_path: Path) -> None:
    """``g.cells[1][2]`` on ``struct{int cells[2][3];}`` stores/loads at +20."""
    src = "struct grid { int cells[2][3]; };\nint main(void){ struct grid g; g.cells[1][2] = 7; return g.cells[1][2]; }\n"
    out = tmp_path / "test.asm"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr
    asm = out.read_text()
    # field_offset 0 + (1*3 + 2)*4 = 20
    assert "+20]" in asm


def test_field_offset_added_to_displacement(tmp_path: Path) -> None:
    """A preceding scalar field shifts the multidim field's base offset."""
    src = "struct grid { int head; int cells[2][3]; };\nint main(void){ struct grid g; g.cells[1][2] = 7; return g.cells[1][2]; }\n"
    out = tmp_path / "test.asm"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr
    asm = out.read_text()
    # field_offset 4 + (1*3 + 2)*4 = 24
    assert "+24]" in asm


def test_wrong_subscript_count_rejected(tmp_path: Path) -> None:
    """Subscripting a 2-D field with 3 indices is a compile error."""
    src = "struct grid { int cells[2][3]; };\nint main(void){ struct grid g; return g.cells[1][2][0]; }\n"
    result = _compile(src, tmp_path)
    assert result.returncode != 0
    assert "wrong number of subscripts" in result.stderr
