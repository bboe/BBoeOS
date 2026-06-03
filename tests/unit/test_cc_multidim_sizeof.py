"""sizeof correctness for multidimensional and non-int-element arrays (Task 4).

Verifies that:
- ``sizeof`` of a multidimensional global or local array returns the full
  product of all dimensions times the element size.
- ``sizeof`` of a single-dimension ``unsigned short`` array uses a 2-byte
  element stride rather than the previous ``int_size`` (4-byte) stride.
- ``sizeof`` of ``int`` and ``char`` single-dimension arrays is unchanged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CC = REPO_ROOT / "cc.py"


def _compile_to_asm(source: str, tmp_path: Path, /) -> str:
    """Compile *source* to 32-bit asm and return the emitted text."""
    source_path = tmp_path / "test.c"
    output_path = tmp_path / "test.asm"
    source_path.write_text(source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", str(source_path), str(output_path)],
        capture_output=True,
        check=False,
        cwd=str(REPO_ROOT),
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return output_path.read_text()


def test_char_array_sizeof_unchanged(tmp_path: Path) -> None:
    """Sizeof of a global char array is element count * 1."""
    asm = _compile_to_asm("char c[10];\nint main(void){ return sizeof(c); }\n", tmp_path)
    assert "mov eax, 10" in asm


def test_int_array_sizeof_unchanged(tmp_path: Path) -> None:
    """Sizeof of a global int array is element count * 4."""
    asm = _compile_to_asm("int a[10];\nint main(void){ return sizeof(a); }\n", tmp_path)
    assert "mov eax, 40" in asm


def test_local_char_array_sizeof_unchanged(tmp_path: Path) -> None:
    """Sizeof of a local char array is element count * 1."""
    asm = _compile_to_asm("int main(void){ char c[10]; return sizeof(c); }\n", tmp_path)
    assert "mov eax, 10" in asm


def test_local_int_array_sizeof_unchanged(tmp_path: Path) -> None:
    """Sizeof of a local int array is element count * 4."""
    asm = _compile_to_asm("int main(void){ int a[10]; return sizeof(a); }\n", tmp_path)
    assert "mov eax, 40" in asm


def test_local_unsigned_short_array_sizeof_uses_two_byte_stride(tmp_path: Path) -> None:
    """Sizeof of a local unsigned short array uses a 2-byte stride, not 4."""
    asm = _compile_to_asm(
        "int main(void){ unsigned short t[3]; return sizeof(t); }\n",
        tmp_path,
    )
    assert "mov eax, 6" in asm  # was 12 before the fix (3 * int_size=4 instead of 3 * 2)


def test_local_unsigned_short_initializer_array_sizeof_uses_two_byte_stride(
    tmp_path: Path,
) -> None:
    """Sizeof of a local unsigned short array with initializer uses a 2-byte stride."""
    asm = _compile_to_asm(
        "int main(void){ unsigned short t[] = {1, 2, 3}; return sizeof(t); }\n",
        tmp_path,
    )
    assert "mov eax, 6" in asm  # was 12 before the fix (3 * int_size=4 instead of 3 * 2)


def test_multidim_global_sizeof_is_product(tmp_path: Path) -> None:
    """Sizeof of a multidim global array is the full dimension product * element size."""
    asm = _compile_to_asm("int g[2][3];\nint main(void){ return sizeof(g); }\n", tmp_path)
    assert "mov eax, 24" in asm  # 2*3*4


def test_multidim_local_sizeof_is_product(tmp_path: Path) -> None:
    """Sizeof of a multidim local array is the full dimension product * element size."""
    asm = _compile_to_asm("int main(void){ int m[2][3]; return sizeof(m); }\n", tmp_path)
    assert "mov eax, 24" in asm  # 2*3*4


def test_unsigned_short_array_sizeof_uses_two_byte_stride(tmp_path: Path) -> None:
    """Sizeof of a global unsigned short array uses a 2-byte stride, not 4."""
    asm = _compile_to_asm(
        "unsigned short s[10];\nint main(void){ return sizeof(s); }\n",
        tmp_path,
    )
    assert "mov eax, 20" in asm  # was 40 before the fix (the bug)
