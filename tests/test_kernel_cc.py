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
# out_register attribute
# ---------------------------------------------------------------------------


def test_out_register_callee_no_spill() -> None:
    """out_register param is not spilled to a stack slot in the callee prologue."""
    asm = _kernel("""
        __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si")))) {
            *entry = 0;
            return 1;
        }
    """)
    # No sub sp instruction — out_register params don't occupy stack space.
    assert "sub sp" not in asm


def test_out_register_callee_deref_assign_emits_to_register() -> None:
    """*param = expr in callee emits to the named register, not a memory slot."""
    asm = _kernel("""
        __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si")))) {
            *entry = 5;
            return 1;
        }
    """)
    assert "mov si," in asm
    # No dereference write to a bp-relative address.
    assert "[bp-" not in asm


def test_out_register_caller_no_push() -> None:
    """Caller emits no push for an out_register argument — only call + capture."""
    asm = _kernel("""
        __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si"))));

        __attribute__((carry_return)) int do_alloc() {
            int* entry;
            if (fd_alloc(&entry)) {
                return 1;
            }
            return 0;
        }
    """)
    # The register capture happens right after the call, with no push before it.
    lines = [line.strip() for line in asm.splitlines()]
    call_idx = next(i for i, line in enumerate(lines) if line == "call fd_alloc")
    # No argument push immediately before the call.
    assert lines[call_idx - 1] != "push ax", "unexpected argument push before call fd_alloc"
    assert lines[call_idx + 1] == "mov [bp-2], si"


def test_out_register_caller_captures_register_into_local() -> None:
    """After the call, the named register is stored into the caller's local variable."""
    asm = _kernel("""
        __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si"))));

        void caller() {
            int* entry;
            fd_alloc(&entry);
        }
    """)
    assert "mov [bp-2], si" in asm


def test_out_register_prototype_registers_convention() -> None:
    """A function prototype with out_register is retained in the AST and registers the convention."""
    # If the prototype is silently dropped, generate_call won't know about out_register
    # and will try to push the &entry argument — causing an error or wrong code.
    ok, output = _compile(
        """
        __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si"))));

        void caller() {
            int* entry;
            fd_alloc(&entry);
        }
    """,
        target="kernel",
    )
    assert ok, f"Compilation failed:\n{output}"
    assert "mov [bp-2], si" in output


def test_out_register_carry_return_condition() -> None:
    """carry_return + out_register: correct CF-based branch and register capture."""
    asm = _kernel("""
        __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si"))));

        __attribute__((carry_return)) int wrapper() {
            int* entry;
            if (fd_alloc(&entry)) {
                return 1;
            }
            return 0;
        }
    """)
    lines = [line.strip() for line in asm.splitlines()]
    call_idx = next(i for i, line in enumerate(lines) if line == "call fd_alloc")
    # Capture happens before the branch.
    assert lines[call_idx + 1] == "mov [bp-2], si"
    assert any(line.startswith(("jc", "jnc")) for line in lines[call_idx + 2 : call_idx + 5])


def test_out_register_nasm_assembles() -> None:
    """Generated out_register caller code assembles cleanly with nasm."""
    with tempfile.TemporaryDirectory(prefix="test_out_reg_") as work:
        work_path = Path(work)
        src = work_path / "t.c"
        asm_out = work_path / "t.asm"
        binary = work_path / "t.bin"
        src.write_text(
            textwrap.dedent("""
            __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si"))));

            __attribute__((carry_return)) int do_alloc() {
                int* entry;
                if (fd_alloc(&entry)) {
                    return 1;
                }
                return 0;
            }
        """)
        )
        result = subprocess.run(
            ["python3", str(CC), "--target", "kernel", str(src), str(asm_out)],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(f"cc.py failed:\n{result.stderr}")
        # Append a stub for the external asm function so nasm can resolve the call.
        with asm_out.open("a") as fh:
            fh.write("\nfd_alloc:\n        clc\n        ret\n")
        nasm_result = subprocess.run(
            ["nasm", "-f", "bin", str(asm_out), "-o", str(binary)],
            capture_output=True,
            check=False,
            text=True,
        )
        if nasm_result.returncode != 0:
            pytest.fail(f"nasm failed:\n{nasm_result.stderr}\n--- asm ---\n{asm_out.read_text()}")


# ---------------------------------------------------------------------------
# si_local optimization: use SI register directly for struct member access
# ---------------------------------------------------------------------------


def test_out_register_si_used_directly_for_member_access() -> None:
    """SI-cached pointer uses [si+offset] directly for struct writes.

    When no call intervenes between the out_register capture and the member
    writes, the code uses SI as the base register directly instead of the
    ``mov bx, [bp-N]; [bx+offset]`` round-trip.
    """
    asm = _kernel("""
        struct point { int x; int y; };

        __attribute__((carry_return)) int make_point(struct point* out __attribute__((out_register("si"))));

        void caller() {
            struct point* p;
            if (make_point(&p)) {
                p->x = 1;
                p->y = 2;
            }
        }
    """)
    # Both field writes should reference SI directly.
    assert "[si]" in asm or "[si+" in asm, f"expected [si] or [si+N] in member writes\n{asm}"
    assert "mov bx, [bp-" not in asm, f"unexpected BX reload for SI-cached pointer\n{asm}"


def test_out_register_si_cleared_across_call() -> None:
    """If a second call intervenes after the capture, the optimization falls back to BX."""
    asm = _kernel("""
        struct point { int x; int y; };

        __attribute__((carry_return)) int make_point(struct point* out __attribute__((out_register("si"))));
        void other_func();

        void caller() {
            struct point* p;
            if (make_point(&p)) {
                other_func();
                p->x = 1;
            }
        }
    """)
    # After other_func(), SI is no longer trusted — fallback to BX.
    assert "mov bx, [bp-" in asm, f"expected BX reload after intervening call\n{asm}"


# ---------------------------------------------------------------------------
# struct array initializers
# ---------------------------------------------------------------------------


def test_struct_array_initializer_emits_fields() -> None:
    """A struct array with a partial initializer emits per-field directives."""
    asm = _kernel("""
        struct point { uint16_t x; uint16_t y; };
        struct point points[4] = {
            {1, 2},
            {3, 4},
        };
        void f() {}
    """)
    assert "_g_points: dw 1" in asm
    assert "dw 2" in asm
    assert "dw 3" in asm
    assert "dw 4" in asm
    assert "times (4-2)*4 db 0" in asm


def test_struct_array_initializer_unspecified_fields_zero() -> None:
    """Unspecified trailing fields in a struct initializer are zero-filled."""
    asm = _kernel("""
        struct entry { uint8_t type; uint8_t flags; uint16_t value; };
        struct entry table[2] = {
            {1},
        };
        void f() {}
    """)
    assert "_g_table: db 1" in asm
    assert "db 0" in asm
    assert "dw 0" in asm


# ---------------------------------------------------------------------------
# uint16_t: fixed 2-byte type across both --bits modes
# ---------------------------------------------------------------------------


def test_uint16_t_size_is_always_two_bytes_16bit() -> None:
    """sizeof(uint16_t) == 2 in --bits 16 mode."""
    asm = _kernel("int f() { return sizeof(uint16_t); }", bits=16)
    assert "mov ax, 2" in asm, f"expected sizeof(uint16_t)==2 in 16-bit mode\n{asm}"


def test_uint16_t_size_is_always_two_bytes_32bit() -> None:
    """sizeof(uint16_t) == 2 in --bits 32 mode (not widened to 4)."""
    asm = _kernel("int f() { return sizeof(uint16_t); }", bits=32)
    assert "mov eax, 2" in asm, f"expected sizeof(uint16_t)==2 in 32-bit mode\n{asm}"


def test_uint32_t_size_is_always_four_bytes_16bit() -> None:
    """sizeof(uint32_t) == 4 in --bits 16 mode."""
    asm = _kernel("int f() { return sizeof(uint32_t); }", bits=16)
    assert "mov ax, 4" in asm, f"expected sizeof(uint32_t)==4 in 16-bit mode\n{asm}"


def test_uint32_t_size_is_always_four_bytes_32bit() -> None:
    """sizeof(uint32_t) == 4 in --bits 32 mode (not widened to 8 for future 64-bit)."""
    asm = _kernel("int f() { return sizeof(uint32_t); }", bits=32)
    assert "mov eax, 4" in asm, f"expected sizeof(uint32_t)==4 in 32-bit mode\n{asm}"


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
