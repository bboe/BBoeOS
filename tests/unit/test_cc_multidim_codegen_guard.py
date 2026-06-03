"""Codegen rejects multidimensional arrays with a clean error (pre-Stage-4).

The parser now accepts ``int m[2][3]`` (records ``ArrayDecl.dimensions``), but
the addressing codegen that lays out / indexes true multidimensional arrays is
a later stage.  Until it lands, a multidimensional declarator reaching codegen
must raise a clear ``CompileError`` rather than silently miscompiling as a
single-dimension ``int m[2]`` (the legacy ``size`` field carries only the outer
dimension).
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


def test_empty_bracket_param_still_compiles(tmp_path: Path) -> None:
    """Guard must not regress the ordinary ``int argv[]`` decay-to-pointer param."""
    source = "int first(int values[]) { return values[0]; }\nint main(void) { return 0; }\n"
    result = _compile(source, tmp_path)
    assert result.returncode == 0, result.stderr


def test_multidim_global_now_compiles(tmp_path: Path) -> None:
    """A file-scope ``int m[2][3];`` now compiles — storage guard removed in Task 3."""
    result = _compile("int m[2][3];\nint main(void) { return 0; }\n", tmp_path)
    assert result.returncode == 0, result.stderr


def test_multidim_local_now_compiles(tmp_path: Path) -> None:
    """A function-local ``int m[2][3];`` now compiles — storage guard removed in Task 3."""
    result = _compile("int main(void) { int m[2][3]; return 0; }\n", tmp_path)
    assert result.returncode == 0, result.stderr


def test_multidim_param_rejected_cleanly(tmp_path: Path) -> None:
    """A function with an ``int m[2][3]`` parameter fails to compile cleanly."""
    source = "int sum(int m[2][3]) { return m[0][0]; }\nint main(void) { return 0; }\n"
    result = _compile(source, tmp_path)
    assert result.returncode != 0
    assert "multidimensional" in result.stderr.lower()


def test_multidim_struct_field_rejected_cleanly(tmp_path: Path) -> None:
    """A struct with an ``int m[2][3];`` field fails to compile with a clean error."""
    source = "struct s { int m[2][3]; };\nstruct s value;\nint main(void) { return 0; }\n"
    result = _compile(source, tmp_path)
    assert result.returncode != 0
    assert "multidimensional" in result.stderr.lower()


def test_single_dimension_still_compiles(tmp_path: Path) -> None:
    """Guard must not regress ordinary single-dimension arrays."""
    result = _compile("int main(void) { int a[4]; a[0] = 1; return a[0]; }\n", tmp_path)
    assert result.returncode == 0, result.stderr


def test_single_dimension_struct_field_still_compiles(tmp_path: Path) -> None:
    """Guard must not regress ordinary single-dimension struct array fields."""
    source = "struct s { char buffer[15]; };\nstruct s value;\nint main(void) { value.buffer[0] = 65; return value.buffer[0]; }\n"
    result = _compile(source, tmp_path)
    assert result.returncode == 0, result.stderr
