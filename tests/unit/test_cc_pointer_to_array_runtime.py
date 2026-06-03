"""Subscript access and decay for pointer-to-array ``int (*p)[3]``.

A pointer-to-array ``int (*p)[3]`` holds an address; ``p[i][j]`` loads that
pointer VALUE (not p's address) and adds a row-major offset over the pointee
dims: stride_i = 4*3 = 12, stride_j = 4.  ``p[1][2]`` therefore lands at
12*1 + 4*2 = 20 bytes past the loaded base.  The same lowering backs a
multidimensional array PARAMETER ``int m[][3]``, which decays to ``int (*)[3]``.

These asm-shape tests pin the constant-index displacement, the pointer-value
load (not lea-of-slot), and the array→address decay on assignment.
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


def test_global_pointer_to_array_constant_offset(tmp_path: Path) -> None:
    """``int (*p)[3]; ... = p[1][2]`` reads at offset 12*1 + 4*2 = 20."""
    src = "int (*p)[3];\nint main(void){ return p[1][2]; }\n"
    out = tmp_path / "test.asm"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr
    asm = out.read_text()
    assert "+20]" in asm


def test_local_pointer_to_array_decay_and_read(tmp_path: Path) -> None:
    """``int (*p)[3] = g;`` decays the array to its base address, then p[1][1] reads."""
    src = "int g[2][3];\nint main(void){ int (*p)[3]; p = g; return p[1][1]; }\n"
    out = tmp_path / "test.asm"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr
    asm = out.read_text()
    # p[1][1] offset = 12*1 + 4*1 = 16
    assert "+16]" in asm


def test_multidim_param_decays_to_pointer_to_array(tmp_path: Path) -> None:
    """A multidim parameter ``int m[][3]`` compiles and addresses m[1][2] at +20."""
    src = "int sum(int m[][3]){ return m[1][2]; }\nint main(void){ return 0; }\n"
    out = tmp_path / "test.asm"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr
    asm = out.read_text()
    assert "+20]" in asm


def test_partial_subscript_rejected(tmp_path: Path) -> None:
    """A partial subscript ``p[i]`` of a pointer-to-array is rejected cleanly."""
    src = "int (*p)[3];\nint main(void){ int *q; q = p[0]; return q[0]; }\n"
    result = _compile(src, tmp_path)
    assert result.returncode != 0
    assert "partial subscript" in result.stderr.lower()


def test_pointer_to_array_sizeof(tmp_path: Path) -> None:
    """``sizeof(p)`` == pointer width (4); ``sizeof(*p)`` == pointee array (12)."""
    src = "int (*p)[3];\nint main(void){ return sizeof(p) + sizeof(*p); }\n"
    out = tmp_path / "test.asm"
    result = _compile(src, tmp_path)
    assert result.returncode == 0, result.stderr
    asm = out.read_text()
    # 4 + 12 = 16 folded into the return immediate.
    assert "16" in asm
