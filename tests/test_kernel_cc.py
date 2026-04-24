"""Pytest tests for cc.py --target kernel (PR 1).

Verifies structural correctness of kernel-mode output:
  - ``org 0600h``, ``_program_end:``, BSS trailer, ``%include "constants.asm"``
    are absent
  - ``0B055h`` stage-2 sentinel is absent
  - ``main`` definition raises CompileError
  - syscall builtins (write, exit, parse_ip …) raise CompileError
  - die() raises CompileError
  - ``--target user`` output is byte-for-byte identical to the default
    for all existing user programs
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
INCLUDE_DIR = REPO_ROOT / "src" / "include"


def _compile(source_text: str, *, target: str = "user", bits: int = 16) -> tuple[bool, str]:
    """Run cc.py on *source_text*; return (success, output_or_stderr)."""
    text = textwrap.dedent(source_text)
    with tempfile.TemporaryDirectory(prefix="test_kernel_") as work:
        work_path = Path(work)
        src = work_path / "test.c"
        out = work_path / "test.asm"
        src.write_text(text)
        result = subprocess.run(
            ["python3", str(CC), "--bits", str(bits), "--target", target, str(src), str(out)],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
        if result.returncode != 0:
            return False, result.stderr
        return True, out.read_text()


def _kernel(source_text: str, bits: int = 16) -> str:
    """Compile *source_text* in kernel mode; fail the test on error."""
    ok, output = _compile(source_text, target="kernel", bits=bits)
    if not ok:
        pytest.fail(f"cc.py --target kernel failed:\n{output}")
    return output


def _kernel_error(source_text: str, bits: int = 16) -> str:
    """Compile in kernel mode expecting failure; return the error message."""
    ok, output = _compile(source_text, target="kernel", bits=bits)
    if ok:
        pytest.fail(f"Expected CompileError but compilation succeeded:\n{output}")
    return output


# ---------------------------------------------------------------------------
# Structural: user-only tokens must be absent
# ---------------------------------------------------------------------------


def test_kernel_no_org() -> None:
    """Kernel output must not contain 'org 0600h'."""
    asm = _kernel("void hello() {}")
    assert "org 0600h" not in asm, f"'org 0600h' found in kernel output\n{asm}"


def test_kernel_no_constants_include() -> None:
    r"""Kernel output must not contain '%include "constants.asm"'."""
    asm = _kernel("void hello() {}")
    assert '%include "constants.asm"' not in asm, f"'%include \"constants.asm\"' found in kernel output\n{asm}"


def test_kernel_no_program_end() -> None:
    """Kernel output must not contain '_program_end:'."""
    asm = _kernel("void hello() {}")
    assert "_program_end:" not in asm, f"'_program_end:' found in kernel output\n{asm}"


def test_kernel_no_bss_trailer() -> None:
    """Kernel output must not contain the 0B055h stage-2 BSS sentinel."""
    asm = _kernel("void hello() {}")
    assert "0B055h" not in asm, f"'0B055h' BSS trailer found in kernel output\n{asm}"


def test_kernel_no_function_exit() -> None:
    """Kernel output must not contain 'jmp FUNCTION_EXIT'."""
    asm = _kernel("void hello() {}")
    assert "jmp FUNCTION_EXIT" not in asm, f"'jmp FUNCTION_EXIT' found in kernel output\n{asm}"


# ---------------------------------------------------------------------------
# Error: forbidden constructs raise CompileError
# ---------------------------------------------------------------------------


def test_kernel_rejects_main() -> None:
    """Defining 'main' in kernel mode raises CompileError."""
    error = _kernel_error("int main() { return 0; }")
    assert "main" in error, f"Expected error mentioning 'main'\n{error}"


def test_kernel_rejects_write() -> None:
    """Calling write() in kernel mode raises CompileError."""
    error = _kernel_error("""
        void send(int fd, char *buf, int n) {
            write(fd, buf, n);
        }
    """)
    assert "kernel" in error.lower() or "write" in error.lower(), f"Expected error mentioning kernel/write\n{error}"


def test_kernel_rejects_exit() -> None:
    """Calling exit() in kernel mode raises CompileError."""
    error = _kernel_error("""
        void quit() {
            exit();
        }
    """)
    assert "kernel" in error.lower() or "exit" in error.lower(), f"Expected error mentioning kernel/exit\n{error}"


def test_kernel_rejects_die() -> None:
    """Calling die() in kernel mode raises CompileError."""
    error = _kernel_error("""
        void panic() {
            die("oops");
        }
    """)
    assert "kernel" in error.lower() or "die" in error.lower(), f"Expected error mentioning kernel/die\n{error}"


def test_kernel_rejects_open() -> None:
    """Calling open() in kernel mode raises CompileError."""
    error = _kernel_error("""
        int get_fd(char *path) {
            return open(path, 0, 0);
        }
    """)
    assert "kernel" in error.lower() or "open" in error.lower(), f"Expected error mentioning kernel/open\n{error}"


# ---------------------------------------------------------------------------
# Positive: kernel-mode source compiles and assembles correctly
# ---------------------------------------------------------------------------


def test_kernel_function_emits_label() -> None:
    """A kernel-mode function emits its label and ret normally."""
    asm = _kernel("""
        int add(int a, int b) {
            return a + b;
        }
    """)
    assert "add:" in asm, f"Expected 'add:' label in kernel output\n{asm}"
    assert "ret" in asm, f"Expected 'ret' in kernel output\n{asm}"


def test_kernel_global_bss_without_program_end() -> None:
    """Global variables in kernel mode emit BSS EQUs relative to _program_end placeholder."""
    asm = _kernel("""
        int counter;
        void increment() {
            counter += 1;
        }
    """)
    assert "_g_counter" in asm, f"Expected '_g_counter' BSS EQU\n{asm}"
    assert "_program_end:" not in asm, f"'_program_end:' must not appear in kernel output\n{asm}"


def test_kernel_source_order_preserved() -> None:
    """Kernel mode emits functions in source order (no main-first reordering)."""
    asm = _kernel("""
        void first() {}
        void second() {}
        void third() {}
    """)
    pos_first = asm.find("first:")
    pos_second = asm.find("second:")
    pos_third = asm.find("third:")
    assert pos_first < pos_second < pos_third, f"Functions not in source order\n{asm}"


def test_kernel_compiles_and_assembles() -> None:
    """A realistic kernel-mode snippet compiles and assembles with nasm."""
    source = """
        int counter;
        int add(int a, int b) {
            return a + b;
        }
        void inc() {
            counter += 1;
        }
    """
    text = textwrap.dedent(source)
    with tempfile.TemporaryDirectory(prefix="test_kernel_asm_") as work:
        work_path = Path(work)
        src = work_path / "test.c"
        asm_out = work_path / "test.asm"
        binary = work_path / "test.bin"
        src.write_text(text)

        cc_result = subprocess.run(
            ["python3", str(CC), "--target", "kernel", str(src), str(asm_out)],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
        if cc_result.returncode != 0:
            pytest.fail(f"cc.py --target kernel failed:\n{cc_result.stderr}")

        nasm_result = subprocess.run(
            ["nasm", "-f", "bin", str(asm_out), "-o", str(binary)],
            capture_output=True,
            check=False,
            text=True,
        )
        if nasm_result.returncode != 0:
            pytest.fail(f"nasm failed:\n{nasm_result.stderr}\n--- asm ---\n{asm_out.read_text()}")


# ---------------------------------------------------------------------------
# Regression: --target user output is byte-for-byte identical to default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_path", sorted((REPO_ROOT / "src" / "c").glob("*.c")))
def test_user_target_identical_to_default(source_path: Path) -> None:
    """--target user output is byte-for-byte identical to the default (no --target)."""
    with tempfile.TemporaryDirectory(prefix="test_kernel_reg_") as work:
        work_path = Path(work)
        asm_default = work_path / f"{source_path.stem}_default.asm"
        asm_user = work_path / f"{source_path.stem}_user.asm"

        for out, extra in [(asm_default, []), (asm_user, ["--target", "user"])]:
            result = subprocess.run(
                ["python3", str(CC), *extra, str(source_path), str(out)],
                capture_output=True,
                check=False,
                cwd=str(REPO_ROOT),
                text=True,
            )
            if result.returncode != 0:
                pytest.fail(f"cc.py failed for {source_path.name}:\n{result.stderr}")

        default_text = asm_default.read_text()
        user_text = asm_user.read_text()
        assert default_text == user_text, (
            f"--target user differs from default for {source_path.name}\n"
            f"--- default ---\n{default_text[:500]}\n"
            f"--- user ---\n{user_text[:500]}"
        )
