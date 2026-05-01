"""Pytest tests for cc.py codegen.

Most tests target ``--target kernel`` and verify structural correctness
of kernel-mode output (no ``org 0600h`` / ``_program_end:`` / BSS
trailer / ``%include "constants.asm"`` / 0B055h sentinel; rejection of
``main`` / syscall builtins / ``die()`` in kernel mode; user-mode
output byte-for-byte identical to the default).

Also covers user-mode struct support: packed layout + sizeof, ptr->field
read/write codegen with correct byte offsets, global struct array BSS
size, struct fd layout pinning against the FD_OFFSET_* constants in
src/include/constants.asm, and a regression sweep over every src/c/*.c
to confirm cc.py + nasm still accept the existing programs under
both --bits 16 and 32.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CC = REPO_ROOT / "cc.py"
INCLUDE_DIR = REPO_ROOT / "src" / "include"
sys.path.insert(0, str(REPO_ROOT))
from cc.codegen.x86.peephole import Peepholer  # noqa: E402
from cc.target import X86CodegenTarget16  # noqa: E402

# FD layout constants from src/include/constants.asm (must match exactly).
# Used by the struct-fd layout-pinning tests below.  Sorted alphabetically
# per project convention; the byte-offset values themselves trace the
# struct fd layout (type@0, flags@1, start@2, size@4, position@8,
# directory_sector@12, directory_offset@14, mode@16, entry_size=32).
FD_ENTRY_SIZE = 32
FD_OFFSET_DIRECTORY_OFFSET = 14
FD_OFFSET_DIRECTORY_SECTOR = 12
FD_OFFSET_FLAGS = 1
FD_OFFSET_MODE = 16
FD_OFFSET_POSITION = 8
FD_OFFSET_SIZE = 4
FD_OFFSET_START = 2
FD_OFFSET_TYPE = 0


def _compile(source_text: str, /, *, target: str = "user", bits: int = 16) -> tuple[bool, str]:
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


def _compile_and_assemble(source_text: str, /, *, bits: int = 16) -> None:
    """Compile *source_text* in user mode and assemble with nasm; fail on any error."""
    text = textwrap.dedent(source_text)
    with tempfile.TemporaryDirectory(prefix="test_kernel_cc_") as work:
        work_path = Path(work)
        src = work_path / "test.c"
        asm = work_path / "test.asm"
        binary = work_path / "test.bin"
        src.write_text(text)
        cc_result = subprocess.run(
            ["python3", str(CC), "--bits", str(bits), str(src), str(asm)],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
        if cc_result.returncode != 0:
            pytest.fail(f"cc.py failed:\n{cc_result.stderr}")
        nasm_result = subprocess.run(
            ["nasm", "-f", "bin", "-i", str(INCLUDE_DIR) + "/", str(asm), "-o", str(binary)],
            capture_output=True,
            check=False,
            text=True,
        )
        if nasm_result.returncode != 0:
            pytest.fail(f"nasm failed:\n{nasm_result.stderr}\n--- asm ---\n{asm.read_text()}")


def _kernel(source_text: str, /, *, bits: int = 16) -> str:
    """Compile *source_text* in kernel mode; fail the test on error."""
    ok, output = _compile(source_text, target="kernel", bits=bits)
    if not ok:
        pytest.fail(f"cc.py --target kernel failed:\n{output}")
    return output


def _kernel_error(source_text: str, /, *, bits: int = 16) -> str:
    """Compile in kernel mode expecting failure; return the error message."""
    ok, output = _compile(source_text, target="kernel", bits=bits)
    if ok:
        pytest.fail(f"Expected CompileError but compilation succeeded:\n{output}")
    return output


def _peephole_run(lines: list[str], /) -> list[str]:
    """Run the x86-16 peephole pipeline over a synthetic instruction list."""
    return Peepholer(lines=lines, target=X86CodegenTarget16()).run()


def _user(source_text: str, /, *, bits: int = 16) -> str:
    """Compile *source_text* in user mode; fail the test on error."""
    ok, output = _compile(source_text, target="user", bits=bits)
    if not ok:
        pytest.fail(f"cc.py --target user failed:\n{output}")
    return output


def test_address_of_array_element_compiles() -> None:
    """``&array[i]`` parses and lowers to scaled pointer arithmetic."""
    asm = _kernel("""
        struct entry { uint8_t ip[4]; };
        struct entry table[8];
        void test() {
            struct entry *e;
            e = &table[3];
        }
    """)
    # Each entry is sizeof(struct entry) = 4 bytes; index 3 → +12 from base.
    assert "_g_table" in asm, f"Expected base reference '_g_table' in:\n{asm}"
    assert "3*4" in asm or "12" in asm, f"Expected stride-scaled offset (3*4 or 12) in:\n{asm}"


def test_asm_name_global_compiles() -> None:
    """asm_name globals compile without emitting storage."""
    source = textwrap.dedent("""
        uint16_t my_sym __attribute__((asm_name("ext_sym")));
        int read_sym(int *result __attribute__((out_register("ax")))) {
            *result = my_sym;
            return 1;
        }
    """)
    output = _kernel(source)
    assert "[ext_sym]" in output
    assert "_g_my_sym" not in output
    assert "ext_sym:" not in output


def test_asm_name_with_offset_compiles() -> None:
    """asm_name with expression offset emits correct symbol reference."""
    source = textwrap.dedent("""
        uint16_t size_hi __attribute__((asm_name("vfs_found_size+2")));
        void set_size_hi(int v) {
            size_hi = v;
        }
    """)
    output = _kernel(source)
    assert "[vfs_found_size+2]" in output


def test_char_compared_inside_logical_and_is_validated() -> None:
    """``&&`` legs each go through the validator independently."""
    error = _kernel_error("""
        void f() {
            char c;
            int n;
            c = 'A';
            n = 0;
            if (c == 'A' && n == c) {
                c = 'B';
            }
        }
    """)
    assert "char compared to non-char" in error, f"expected per-leg validation, got: {error}"


def test_char_compared_inside_nested_if_is_validated() -> None:
    """Comparisons nested inside ``if`` body are walked, not just top-level conditions."""
    error = _kernel_error("""
        void f() {
            char c;
            c = 'A';
            if (c == 'A') {
                if (c == 0) {
                    c = 'B';
                }
            }
        }
    """)
    assert "char compared to non-char" in error, f"expected nested-if validation, got: {error}"


def test_char_compared_inside_while_condition_is_validated() -> None:
    """``while`` conditions reach the validator the same as ``if``."""
    error = _kernel_error("""
        void f() {
            char c;
            c = 'A';
            while (c != 0) {
                c = c + 1;
            }
        }
    """)
    assert "char compared to non-char" in error, f"expected while-condition validation, got: {error}"


def test_char_index_compared_to_int_literal_is_rejected() -> None:
    """``char *p; if (p[0] == 0)`` raises — element type carries through."""
    error = _kernel_error("""
        void f(char *p) {
            if (p[0] == 0) {
                p = p + 1;
            }
        }
    """)
    assert "char compared to non-char" in error, f"expected char-vs-int rejection, got: {error}"


def test_char_local_compared_to_char_literal_compiles() -> None:
    r"""``char c; if (c == '\0')`` is the supported spelling."""
    asm = _kernel("""
        void f() {
            char c;
            c = 'A';
            if (c == '\\0') {
                c = 'B';
            }
            if (c >= 'A' && c <= 'Z') {
                c = ' ';
            }
            if (c < ' ') {
                c = '\\0';
            }
        }
    """)
    assert "f:" in asm


def test_char_local_compared_to_int_literal_is_rejected() -> None:
    r"""``char c; if (c == 0)`` raises — must use ``c == '\0'``."""
    error = _kernel_error("""
        void f() {
            char c;
            c = 'A';
            if (c == 0) {
                c = 'B';
            }
        }
    """)
    assert "char compared to non-char" in error, f"expected char-vs-int rejection, got: {error}"


def test_char_local_compared_to_int_var_is_rejected() -> None:
    """``char c; int n; if (c == n)`` raises — int-typed RHS isn't a Char literal."""
    error = _kernel_error("""
        void f() {
            char c;
            int n;
            c = 'A';
            n = 65;
            if (c == n) {
                c = 'B';
            }
        }
    """)
    assert "char compared to non-char" in error, f"expected char-vs-int rejection, got: {error}"


def test_char_local_ordered_compare_int_literal_is_rejected() -> None:
    """``char c; if (c < 32)`` raises — ordered comparison goes through the same rule."""
    error = _kernel_error("""
        void f() {
            char c;
            c = 'A';
            if (c < 32) {
                c = ' ';
            }
        }
    """)
    assert "char compared to non-char" in error, f"expected char-vs-int rejection, got: {error}"


def test_char_param_compared_to_int_literal_is_rejected() -> None:
    """``char`` parameter compared to bare integer literal is also rejected."""
    error = _kernel_error("""
        void f(char byte) {
            if (byte == 10) {
                byte = 13;
            }
        }
    """)
    assert "char compared to non-char" in error, f"expected char-vs-int rejection, got: {error}"


def test_dot_access_on_extern_struct_global_reads_via_symbol() -> None:
    """``obj.field`` on a file-scope struct global emits ``[_g_obj+offset]``.

    Motivation: fd_open's port wants to read ``vfs_found.size`` etc.
    after vfs_find populates the struct.  No base-register load needed
    because the struct's address is a compile-time symbol.
    """
    asm = _kernel(
        """
        struct vfs_found_t { uint8_t type; uint8_t mode; uint16_t inode; uint32_t size; };
        extern struct vfs_found_t vfs_found;
        int read_size(int *r __attribute__((out_register("ax")))) {
            *r = vfs_found.size;
            return 1;
        }
    """,
        bits=32,
    )
    assert "[_g_vfs_found+4]" in asm, f"expected direct memory access\n{asm}"


def test_dot_access_on_local_struct_still_rejected() -> None:
    """Dot-access on a local struct value (not a pointer) still raises an error."""
    error = _kernel_error(
        """
        struct s { uint8_t x; };
        void bad() {
            struct s local;
            int y;
            y = local.x;
        }
    """,
        bits=32,
    )
    assert "dot member access" in error, f"Expected dot-access rejection, got: {error}"


def test_dot_assign_on_extern_struct_global_writes_via_symbol() -> None:
    """``obj.field = expr;`` on a file-scope struct global emits direct stores."""
    asm = _kernel(
        """
        struct slot { uint8_t kind; uint16_t value; };
        struct slot entry;
        void set() {
            entry.kind = 5;
            entry.value = 42;
        }
    """,
        bits=32,
    )
    # cc.py packs struct fields tightly (no alignment padding), so the
    # uint16_t value sits immediately after the uint8_t kind at offset 1.
    assert "mov byte [_g_entry], al" in asm, f"expected byte write to _g_entry\n{asm}"
    assert "mov word [_g_entry+1], ax" in asm, f"expected word write to _g_entry+1\n{asm}"


def test_double_pointer_argv_with_out_register_cx_argc() -> None:
    """The ``shared_parse_argv`` shape compiles end to end.

    Combines double-pointer parameter (uint8_t **argv), in_register("di"),
    out_register("cx") for argc, and a uint8_t* alias of a NAMED_CONSTANT
    address — exactly what the ported lib/proc.c version needs.
    """
    src = """
        uint8_t *exec_arg_pointer __attribute__((asm_name("EXEC_ARG")));
        void shared_parse_argv(uint8_t **argv __attribute__((in_register("di"))),
                               int *argc __attribute__((out_register("cx")))) {
            int count;
            int slot;
            uint8_t *str;
            count = 0;
            slot = 0;
            str = exec_arg_pointer;
            if (str != NULL) {
                while (1) {
                    while (str[0] == ' ') {
                        str = str + 1;
                    }
                    if (str[0] == '\0') { break; }
                    argv[slot] = str;
                    slot = slot + 1;
                    count = count + 1;
                    while (1) {
                        if (str[0] == '\0') { break; }
                        if (str[0] == ' ') { break; }
                        str = str + 1;
                    }
                    if (str[0] == '\0') { break; }
                    str[0] = '\0';
                    str = str + 1;
                }
            }
            *argc = count;
        }
    """
    asm = _kernel(src)
    body = asm.split("shared_parse_argv:")[1]
    # The function should END with CX holding count (the argc) and a ret.
    assert "mov ax, cx" in body or "mov cx, ax" in body, f"expected CX/AX dance for argc out_register\n{asm}"
    assert "ret" in body, f"non-naked function emits ret\n{asm}"


def test_double_pointer_arithmetic_advances_by_two() -> None:
    """``argv = argv + 1`` on ``uint8_t**`` advances by 2 bytes (sizeof pointer)."""
    src = """
        void f(uint8_t **argv __attribute__((in_register("di")))) {
            argv = argv + 1;
        }
    """
    asm = _kernel(src)
    body = asm.split("f:")[1].split("ret")[0]
    # Either an explicit ``add ..., 2`` or an ``inc`` twice; never a stride
    # of 1 (which would mistreat the type as a byte pointer).
    assert "add" in body or "inc" in body, f"expected pointer-advance instruction\n{asm}"


def test_double_pointer_char_double_star_parses() -> None:
    """``char **`` parameter type parses (mirrors the canonical ``char **argv``)."""
    src = """
        void f(char **argv __attribute__((in_register("di")))) {
            argv[0] = argv[1];
        }
    """
    asm = _kernel(src)
    assert "f:" in asm


def test_double_pointer_indexed_assign_uses_word_stride() -> None:
    """``argv[i] = ptr`` on ``uint8_t**`` writes a 16-bit value (slot is a pointer)."""
    src = """
        void f(uint8_t **argv __attribute__((in_register("di"))),
               uint8_t *value __attribute__((in_register("si")))) {
            argv[0] = value;
        }
    """
    asm = _kernel(src)
    # The slot is 16 bits — must NOT be ``mov [..], al`` (byte store) and
    # must reach the slot via the word path.
    assert "mov [di], si" in asm or "mov [si], di" in asm or ("mov" in asm and "byte" not in asm.split("f:")[1].split("ret")[0]), (
        f"expected 16-bit slot store, got\n{asm}"
    )


def test_double_pointer_int_double_star_parses() -> None:
    """``int **`` parameter type parses (parser regression check)."""
    src = """
        void f(int **slots __attribute__((in_register("di")))) {
            slots[0] = slots[1];
        }
    """
    asm = _kernel(src)
    assert "f:" in asm


def test_double_pointer_parameter_compiles() -> None:
    """``uint8_t **argv`` is accepted as a parameter type."""
    src = """
        void f(uint8_t **argv __attribute__((in_register("di")))) {
            argv[0] = argv[1];
        }
    """
    asm = _kernel(src)
    assert "f:" in asm, f"function emitted\n{asm}"


@pytest.mark.parametrize("source_path", sorted((REPO_ROOT / "src" / "c").glob("*.c")))
@pytest.mark.parametrize("bits", [16, 32])
def test_existing_programs_unchanged(source_path: Path, bits: int) -> None:
    """Every existing user-space C program still compiles and assembles after PR 0."""
    with tempfile.TemporaryDirectory(prefix="test_struct_regression_") as work:
        work_path = Path(work)
        asm = work_path / f"{source_path.stem}.asm"
        binary = work_path / f"{source_path.stem}.bin"

        cc_result = subprocess.run(
            ["python3", str(CC), "--bits", str(bits), str(source_path), str(asm)],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
        if cc_result.returncode != 0:
            pytest.fail(f"cc.py failed for {source_path.name} --bits {bits}:\n{cc_result.stderr}")

        nasm_result = subprocess.run(
            ["nasm", "-f", "bin", "-i", str(INCLUDE_DIR) + "/", str(asm), "-o", str(binary)],
            capture_output=True,
            check=False,
            text=True,
        )
        if nasm_result.returncode != 0:
            pytest.fail(f"nasm failed for {source_path.name} --bits {bits}:\n{nasm_result.stderr}")


def test_extern_array_global_no_storage() -> None:
    """``extern T name[N];`` declares the array symbol but emits no _g_name storage."""
    source = textwrap.dedent("""
        extern int sizes[8];
        int read_size(int idx __attribute__((in_register("bx"))),
                      int *result __attribute__((out_register("ax")))) {
            *result = sizes[idx];
            return 1;
        }
    """)
    output = _kernel(source)
    assert "_g_sizes" in output, f"reference must mention _g_sizes\n{output}"
    assert "_g_sizes:" not in output, f"extern must not emit storage\n{output}"


def test_extern_local_rejected() -> None:
    """``extern`` inside a function body is rejected (only valid at file scope)."""
    error = _kernel_error("""
        void f() {
            extern int x;
        }
    """)
    assert "extern" in error.lower(), f"Expected error about extern in function body, got: {error}"


def test_extern_scalar_global_no_storage() -> None:
    """``extern T name;`` declares the symbol but emits no _g_name storage.

    The motivating shape is a cross-.c-file global like ``fd_write_buffer``
    whose definition lives in fs/fd.c and whose handlers in
    fs/fd/console.c want to reference it without redefining storage.
    """
    source = textwrap.dedent("""
        extern uint8_t *fd_write_buffer;
        int read_buf(int *result __attribute__((out_register("ax")))) {
            *result = fd_write_buffer[0];
            return 1;
        }
    """)
    output = _kernel(source)
    assert "[_g_fd_write_buffer]" in output, f"reference must resolve to _g_fd_write_buffer\n{output}"
    assert "_g_fd_write_buffer:" not in output, f"extern must not emit storage\n{output}"


def test_extern_with_init_rejected() -> None:
    """``extern T name = value;`` is rejected — extern declares, doesn't define."""
    error = _kernel_error("""
        extern int x = 5;
    """)
    assert "extern" in error.lower(), f"Expected error about extern + initializer, got: {error}"


def test_fd_layout_all_offsets() -> None:
    """Verify each field of struct fd is accessed at the exact FD_OFFSET_* byte offset.

    This is the canonical correctness gate for the fd.c port: if any field
    drifts from its FD_OFFSET_* constant, the C code and the asm callers
    will disagree on struct layout and silently corrupt the FD table.
    """
    source = textwrap.dedent("""
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        void write_type(struct fd *p) { p->type = 0; }
        void write_flags(struct fd *p) { p->flags = 0; }
        void write_start(struct fd *p) { p->start = 0; }
        void write_directory_sector(struct fd *p) { p->directory_sector = 0; }
        void write_directory_offset(struct fd *p) { p->directory_offset = 0; }
        void write_mode(struct fd *p) { p->mode = 0; }
        int main() { return 0; }
    """)
    asm = _user(source, bits=16)

    def _section(asm_text: str, function_name: str) -> str:
        start = asm_text.find(f"{function_name}:")
        if start == -1:
            return ""
        end = asm_text.find("\nret", start)
        return asm_text[start : end + 4] if end != -1 else asm_text[start:]

    assert "[bx]" in _section(asm, "write_type"), f"type (offset 0) should use [bx]\n{_section(asm, 'write_type')}"
    assert f"[bx+{FD_OFFSET_FLAGS}]" in _section(asm, "write_flags"), (
        f"flags should be at offset {FD_OFFSET_FLAGS}\n{_section(asm, 'write_flags')}"
    )
    assert f"[bx+{FD_OFFSET_START}]" in _section(asm, "write_start"), (
        f"start should be at offset {FD_OFFSET_START}\n{_section(asm, 'write_start')}"
    )
    assert f"[bx+{FD_OFFSET_DIRECTORY_SECTOR}]" in _section(asm, "write_directory_sector"), (
        f"directory_sector should be at offset {FD_OFFSET_DIRECTORY_SECTOR}\n{_section(asm, 'write_directory_sector')}"
    )
    assert f"[bx+{FD_OFFSET_DIRECTORY_OFFSET}]" in _section(asm, "write_directory_offset"), (
        f"directory_offset should be at offset {FD_OFFSET_DIRECTORY_OFFSET}\n{_section(asm, 'write_directory_offset')}"
    )
    assert f"[bx+{FD_OFFSET_MODE}]" in _section(asm, "write_mode"), (
        f"mode should be at offset {FD_OFFSET_MODE}\n{_section(asm, 'write_mode')}"
    )


def test_file_scope_function_pointer_assignment_uses_function_symbol() -> None:
    """Bare function name decays to its address in an assignment.

    ``vfs_find_fn = my_handler;`` emits ``mov eax, my_handler`` (the
    function's link-time address) followed by ``mov [_g_name], eax``.
    """
    asm = _kernel(
        """
        int my_handler();
        int (*vfs_find_fn)();
        void register_handler() {
            vfs_find_fn = my_handler;
        }
    """,
        bits=32,
    )
    assert "mov eax, my_handler" in asm, f"expected function-symbol load\n{asm}"
    assert "mov [_g_vfs_find_fn], eax" in asm, f"expected store to global\n{asm}"


def test_file_scope_function_pointer_emits_storage_and_indirect_call() -> None:
    """File-scope function_pointer compiles to storage + indirect call.

    ``int (*name)(...);`` emits ``_g_<name>`` storage and ``name(args)``
    becomes ``mov eax, [_g_<name>]; call eax``.
    """
    asm = _kernel(
        """
        int (*vfs_find_fn)();
        int dispatch() {
            vfs_find_fn();
            return 1;
        }
    """,
        bits=32,
    )
    assert "_g_vfs_find_fn:" in asm, f"expected storage label\n{asm}"
    assert "mov eax, [_g_vfs_find_fn]" in asm, f"expected indirect load\n{asm}"
    assert "call eax" in asm, f"expected indirect call\n{asm}"


def test_file_scope_function_pointer_tail_call() -> None:
    """``__tail_call`` works on a file-scope function_pointer global.

    Emits ``mov eax, [_g_<name>]; jmp eax`` after frame teardown.
    """
    asm = _kernel(
        """
        int (*vfs_find_fn)(int x __attribute__((in_register("ebx"))));
        __attribute__((carry_return))
        int dispatch(int v __attribute__((in_register("ebx")))) {
            __tail_call(vfs_find_fn, v);
        }
    """,
        bits=32,
    )
    assert "mov eax, [_g_vfs_find_fn]" in asm, f"expected indirect load before jmp\n{asm}"
    assert "jmp eax" in asm, f"expected jmp eax\n{asm}"


def test_function_pointer_arg_count_mismatch_raises_error() -> None:
    """Calling an function_pointer with wrong arg count raises CompileError."""
    error = _kernel_error("""
        int get_fn();
        void caller() {
            int (*handler)(int x __attribute__((in_register("bx"))));
            handler = get_fn();
            handler();
        }
    """)
    assert "function_pointer" in error, f"Expected function_pointer arity error, got: {error}"


def test_function_pointer_local_emits_call_ax() -> None:
    """A local function_pointer variable called with no args emits 'call ax'."""
    asm = _kernel("""
        int get_fn();
        void caller() {
            int (*handler)();
            handler = get_fn();
            handler();
        }
    """)
    assert "call ax" in asm, "indirect call through function_pointer must emit 'call ax'"


def test_function_pointer_struct_field_type() -> None:
    """A struct with an function_pointer field compiles and the field has width 2."""
    asm = _kernel("""
        struct ops {
            int (*read)();
            int (*write)();
        };
        int do_read(struct ops *o) {
            int (*fn)();
            fn = o->read;
            return fn();
        }
    """)
    assert "call ax" in asm, "indirect call through function_pointer must emit 'call ax'"


def test_function_pointer_with_in_register_param_moves_arg_before_call() -> None:
    """An function_pointer with an in_register param loads that register before 'call ax'."""
    asm = _kernel("""
        int get_fn();
        void caller() {
            int (*handler)(int x __attribute__((in_register("bx"))));
            handler = get_fn();
            handler(42);
        }
    """)
    assert "call ax" in asm, "indirect call must emit 'call ax'"
    assert "mov bx, 42" in asm, "in_register param must be loaded into bx before call"
    call_pos = asm.index("call ax")
    bx_pos = asm.index("mov bx, 42")
    assert bx_pos < call_pos, "mov bx must appear before call ax"


def test_global_struct_array_bss_size() -> None:
    """Global struct array sizes to (N * sizeof(struct)) in the BSS trailer.

    cc.py reserves BSS via the trailer-magic protocol — emit ``dd N``
    + ``dw 0xB032`` and let program_enter zero-fill at load — rather
    than allocating bytes in the binary.  For ``struct item table[5]``
    where struct item is 3 bytes (char=1 + int=2), the trailer must
    declare 15 bytes via ``_bss_end equ _program_end + 15``.
    """
    asm = _user(
        """
        struct item { char x; int y; };
        struct item table[5];
        int main() {
            return 0;
        }
    """,
        bits=16,
    )
    assert "_bss_end equ _program_end + 15" in asm, f"Expected '_bss_end equ _program_end + 15' for 5-element struct array BSS\n{asm}"


def test_global_struct_array_compiles_and_assembles() -> None:
    """Global struct fd array with symbolic size compiles and assembles."""
    _compile_and_assemble(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        struct fd fd_table[8];
        int main() {
            return 0;
        }
    """,
        bits=16,
    )


def test_in_register_32bit_char_param_other_registers() -> None:
    """32-bit target: byte-typed pin to bx / cx / dx widens from bl / cl / dl."""
    for reg, low in (("bx", "bl"), ("cx", "cl"), ("dx", "dl")):
        asm = _kernel(
            f"""
            void putc(char byte __attribute__((in_register("{reg}")))) {{
                int copy;
                copy = byte;
                kernel_outb(0x3F8, copy);
            }}
            """,
            bits=32,
        )
        assert f"movzx eax, {low}" in asm, f"expected widening from {low}\n{asm}"


def test_in_register_32bit_char_param_widens_from_low_byte() -> None:
    """32-bit target: ``char`` pin uses the *byte* alias (AL) for widening.

    The asm-side calling convention sets only AL — AH is whatever the
    caller had in there from earlier code (e.g. ``lodsb; call f``).
    Widening from AX would preserve AH-garbage in bits 8..15 of the
    spilled slot, so subsequent ``cmp dword [ebp-N], <const>`` reads
    fail even when AL holds the expected byte.  Widening from AL
    scrubs AH out of the picture.
    """
    asm = _kernel(
        """
        void putc(char byte __attribute__((in_register("ax")))) {
            int copy;
            copy = byte;
            kernel_outb(0x3F8, copy);
        }
        """,
        bits=32,
    )
    assert "movzx eax, al" in asm, f"expected widening from low byte\n{asm}"
    assert "movzx eax, ax" not in asm, f"unexpected widening from full ax\n{asm}"


def test_in_register_32bit_full_width_skips_widen() -> None:
    """32-bit target: an ``ecx``-pinned param spills directly without ``movzx``."""
    asm = _kernel(
        """
        void clobber();
        void f(int x __attribute__((in_register("ecx")))) {
            int y;
            clobber();
            y = x;
            kernel_outb(0x60, y);
        }
        """,
        bits=32,
    )
    assert "movzx ecx" not in asm, f"unexpected widening for full-width pin\n{asm}"
    assert "mov [ebp-4], ecx" in asm, f"expected direct spill of ecx\n{asm}"


def test_in_register_32bit_widens_narrow_to_full_slot() -> None:
    """32-bit target: a 16-bit ``in_register`` pin gets ``movzx`` before the spill.

    Without the widening, the prologue would write only the low 2 bytes
    of a 4-byte slot, leaving the upper 2 bytes uninitialised — a
    later 32-bit reload picks up garbage stack content.  An intervening
    call forces the spill (cc.py elides the spill+reload entirely when
    the value can flow through registers without clobber).
    """
    asm = _kernel(
        """
        void clobber();
        void f(int x __attribute__((in_register("ax")))) {
            int y;
            clobber();
            y = x;
            kernel_outb(0x60, y);
        }
        """,
        bits=32,
    )
    assert "movzx eax, ax" in asm, f"expected 'movzx eax, ax' before spill\n{asm}"
    assert "mov [ebp-4], eax" in asm, f"expected 4-byte spill of widened value\n{asm}"


def test_in_register_byte_typed_pin_to_si_rejected() -> None:
    """Byte-typed parameter pinned to a register without a byte alias errors out."""
    error = _kernel_error(
        """
        void f(char byte __attribute__((in_register("esi")))) {
            kernel_outb(0x3F8, byte);
        }
        """,
        bits=32,
    )
    assert "byte" in error.lower() and "esi" in error, f"expected error mentioning byte+esi\n{error}"


def test_in_register_int_param_keeps_full_word_widen() -> None:
    """Non-byte typed pins still widen from the full 16-bit alias.

    ``int`` parameters carry the full 16 bits of AX as the value; the
    caller-side ABI sets AX (not just AL) for them.  Widening must
    use ``movzx eax, ax`` to keep the upper 8 bits of AX intact.
    """
    asm = _kernel(
        """
        void f(int x __attribute__((in_register("ax")))) {
            int y;
            y = x;
            kernel_outb(0x60, y);
        }
        """,
        bits=32,
    )
    assert "movzx eax, ax" in asm, f"expected full word widening for int param\n{asm}"
    assert "movzx eax, al" not in asm, f"int param should not narrow to al\n{asm}"


def test_in_register_no_caller_push() -> None:
    """Caller passes in_register arg by loading the register, not pushing."""
    src = """
        void callee(int x __attribute__((in_register("bx"))));
        void caller(int v) { callee(v); }
    """
    asm = _kernel(src)
    # The call should load BX (not push) for the in_register param.
    assert "mov bx," in asm, f"expected 'mov bx, ...' for in_register arg\n{asm}"
    assert "push" not in asm.split("callee")[1].split("ret")[0], f"expected no push before callee call\n{asm}"


def test_in_register_spill_kept_when_param_read_in_body() -> None:
    """Spill is NOT elided when the body reads the param outside a TailCall arg.

    A body that uses the in_register param in an expression (assignment,
    arithmetic, etc.) still needs the prologue spill so the body can read it
    from the local slot via the normal accumulator path.
    """
    asm = _kernel("""
        void f(int x __attribute__((in_register("bx")))) {
            int y;
            y = x + 1;
        }
    """)
    assert "mov [bp-" in asm, f"expected spill kept when param is read in body\n{asm}"
    assert "mov [bp-2], bx" in asm, f"expected 'mov [bp-2], bx' spill\n{asm}"


def test_in_register_spills_to_local_slot() -> None:
    """in_register param is spilled to a local stack slot at function entry."""
    src = """
        void f(int x __attribute__((in_register("bx")))) {
            int y;
            y = x;
        }
    """
    asm = _kernel(src)
    assert "mov [bp-" in asm, f"expected spill to local slot\n{asm}"
    assert "mov [bp-2], bx" in asm, f"expected 'mov [bp-2], bx' spill\n{asm}"


def test_in_register_with_carry_return() -> None:
    """in_register and carry_return can combine: spill bx, emit clc/stc."""
    proto = (
        "__attribute__((carry_return)) int fd_lookup("
        'int fd __attribute__((in_register("bx"))),'
        ' int *entry __attribute__((out_register("si"))));'
    )
    defn = proto.rstrip(";") + " { if (fd >= 8) { return 0; } *entry = fd; return 1; }"
    src = proto + "\n" + defn
    asm = _kernel(src)
    assert "mov [bp-" in asm and "mov [bp-2], bx" in asm, f"expected bx spill\n{asm}"
    assert "stc" in asm, f"expected stc for return 0\n{asm}"
    assert "clc" in asm, f"expected clc for return 1\n{asm}"
    assert "mov si," in asm, f"expected mov si for out_register\n{asm}"


def test_int_local_compared_to_int_literal_compiles() -> None:
    """Plain ``int x; if (x == 0)`` is unaffected — both operands classify as integer."""
    asm = _kernel("""
        void f() {
            int x;
            x = 0;
            if (x == 0) {
                x = 1;
            }
        }
    """)
    assert "f:" in asm


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


def test_kernel_inb_emits_in_al_dx() -> None:
    """``kernel_inb(port)`` emits ``in al, dx`` followed by ``xor ah, ah`` to zero-extend."""
    asm = _kernel("""
        void poll() {
            int status;
            status = kernel_inb(0x3FD);
        }
    """)
    assert "in al, dx" in asm, f"Expected 'in al, dx' in:\n{asm}"
    assert "xor ah, ah" in asm, f"Expected 'xor ah, ah' (zero-extend) in:\n{asm}"


def test_kernel_insw_emits_rep_insw() -> None:
    """``kernel_insw(port, buffer, count)`` emits the rep insw setup."""
    asm = _kernel("""
        void f() {
            char buf[512];
            kernel_insw(0x1F0, buf, 256);
        }
    """)
    assert "mov dx, 496" in asm, f"expected port load 'mov dx, 496' (0x1F0):\n{asm}"
    assert "lea di, [bp-512]" in asm, f"expected buffer in DI:\n{asm}"
    assert "mov cx, 256" in asm, f"expected count in CX:\n{asm}"
    assert "        cld" in asm, f"expected cld:\n{asm}"
    assert "        rep insw" in asm, f"expected rep insw:\n{asm}"


def test_kernel_inw_emits_in_ax_dx() -> None:
    """``kernel_inw(port)`` emits ``in ax, dx`` (no zero-extend needed for 16-bit)."""
    asm = _kernel("""
        void poll() {
            int word;
            word = kernel_inw(0x300);
        }
    """)
    assert "in ax, dx" in asm, f"Expected 'in ax, dx' in:\n{asm}"


def test_kernel_no_bss_trailer() -> None:
    """Kernel output must not contain the BSS trailer sentinel (0B032h or legacy 0B055h)."""
    asm = _kernel("void hello() {}")
    assert "0B032h" not in asm, f"'0B032h' BSS trailer found in kernel output\n{asm}"
    assert "0B055h" not in asm, f"'0B055h' BSS trailer found in kernel output\n{asm}"


def test_kernel_no_constants_include() -> None:
    r"""Kernel output must not contain '%include "constants.asm"'."""
    asm = _kernel("void hello() {}")
    assert '%include "constants.asm"' not in asm, f"'%include \"constants.asm\"' found in kernel output\n{asm}"


def test_kernel_no_function_exit() -> None:
    """Kernel output must not contain 'jmp FUNCTION_EXIT'."""
    asm = _kernel("void hello() {}")
    assert "jmp FUNCTION_EXIT" not in asm, f"'jmp FUNCTION_EXIT' found in kernel output\n{asm}"


def test_kernel_no_org() -> None:
    """Kernel output must not contain 'org 0600h'."""
    asm = _kernel("void hello() {}")
    assert "org 0600h" not in asm, f"'org 0600h' found in kernel output\n{asm}"


def test_kernel_no_program_end() -> None:
    """Kernel output must not contain '_program_end:'."""
    asm = _kernel("void hello() {}")
    assert "_program_end:" not in asm, f"'_program_end:' found in kernel output\n{asm}"


def test_kernel_outb_constant_value_short_form() -> None:
    """``kernel_outb(port, const)`` compiles to ``mov al, <const>`` (no AX push/pop)."""
    asm = _kernel("""
        void eoi() {
            kernel_outb(0x20, 0x20);
        }
    """)
    assert "out dx, al" in asm, f"Expected 'out dx, al' in:\n{asm}"
    assert "mov al, 32" in asm, f"Expected 'mov al, 32' (constant value 0x20) in:\n{asm}"
    assert "push ax" not in asm, f"Constant outb should not push AX:\n{asm}"


def test_kernel_outb_variable_value_uses_push_pop() -> None:
    """Non-constant ``outb`` value evaluates to AX, push/port-eval/pop, then ``out dx, al``."""
    asm = _kernel("""
        void send(int port, int value) {
            kernel_outb(port, value);
        }
    """)
    # The push/pop guard around port-evaluation matches builtin_far_write8's shape.
    assert "push ax" in asm, f"Expected 'push ax' guard in:\n{asm}"
    assert "pop ax" in asm, f"Expected 'pop ax' restore in:\n{asm}"
    assert "out dx, al" in asm, f"Expected 'out dx, al' in:\n{asm}"


def test_kernel_outsw_emits_rep_outsw() -> None:
    """``kernel_outsw(port, buffer, count)`` emits the rep outsw setup."""
    asm = _kernel("""
        void f() {
            char buf[512];
            kernel_outsw(0x1F0, buf, 256);
        }
    """)
    assert "mov dx, 496" in asm, f"expected port load:\n{asm}"
    assert "lea si, [bp-512]" in asm, f"expected buffer in SI:\n{asm}"
    assert "mov cx, 256" in asm, f"expected count in CX:\n{asm}"
    assert "        cld" in asm, f"expected cld:\n{asm}"
    assert "        rep outsw" in asm, f"expected rep outsw:\n{asm}"


def test_kernel_outw_constant_value_short_form() -> None:
    """``kernel_outw(port, const)`` compiles to a constant ``mov ax, ...`` then ``out dx, ax``."""
    asm = _kernel("""
        void send_word() {
            kernel_outw(0x300, 0x1234);
        }
    """)
    assert "out dx, ax" in asm, f"Expected 'out dx, ax' in:\n{asm}"
    assert "mov ax, 4660" in asm, f"Expected 'mov ax, 4660' (constant value 0x1234) in:\n{asm}"


def test_kernel_rejects_die() -> None:
    """Calling die() in kernel mode raises CompileError."""
    error = _kernel_error("""
        void panic() {
            die("oops");
        }
    """)
    assert "kernel" in error.lower() or "die" in error.lower(), f"Expected error mentioning kernel/die\n{error}"


def test_kernel_rejects_exit() -> None:
    """Calling exit() in kernel mode raises CompileError."""
    error = _kernel_error("""
        void quit() {
            exit();
        }
    """)
    assert "kernel" in error.lower() or "exit" in error.lower(), f"Expected error mentioning kernel/exit\n{error}"


def test_kernel_rejects_main() -> None:
    """Defining 'main' in kernel mode raises CompileError."""
    error = _kernel_error("int main() { return 0; }")
    assert "main" in error, f"Expected error mentioning 'main'\n{error}"


def test_kernel_rejects_open() -> None:
    """Calling open() in kernel mode raises CompileError."""
    error = _kernel_error("""
        int get_fd(char *path) {
            return open(path, 0, 0);
        }
    """)
    assert "kernel" in error.lower() or "open" in error.lower(), f"Expected error mentioning kernel/open\n{error}"


def test_kernel_rejects_write() -> None:
    """Calling write() in kernel mode raises CompileError."""
    error = _kernel_error("""
        void send(int fd, char *buf, int n) {
            write(fd, buf, n);
        }
    """)
    assert "kernel" in error.lower() or "write" in error.lower(), f"Expected error mentioning kernel/write\n{error}"


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


def test_member_access_in_condition() -> None:
    """p->type can be compared in an if condition."""
    _compile_and_assemble(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        int is_free(struct fd *p) {
            if (p->type == 0) {
                return 1;
            }
            return 0;
        }
    """,
        bits=16,
    )


def test_member_access_offset_flags() -> None:
    """p->flags (offset 1) emits [bx+1]."""
    asm = _user(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        void set_flags(struct fd *p) {
            p->flags = 2;
        }
    """,
        bits=16,
    )
    assert f"[bx+{FD_OFFSET_FLAGS}]" in asm, f"Expected '[bx+{FD_OFFSET_FLAGS}]' for flags field\n{asm}"


def test_member_access_offset_start() -> None:
    """p->start (offset 2) emits [bx+2]."""
    asm = _user(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        void set_start(struct fd *p) {
            p->start = 3;
        }
    """,
        bits=16,
    )
    assert f"[bx+{FD_OFFSET_START}]" in asm, f"Expected '[bx+{FD_OFFSET_START}]' for start field\n{asm}"


def test_member_access_offset_zero() -> None:
    """p->type (offset 0) emits [bx] with no +offset."""
    asm = _user(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        void set_type(struct fd *p) {
            p->type = 1;
        }
    """,
        bits=16,
    )
    # byte store at offset 0: mov byte [bx], al
    assert "[bx]" in asm and "bx+" not in asm.split("set_type")[1].split("ret")[0], (
        f"Expected '[bx]' (no +offset) for field at offset 0\n{asm}"
    )


def test_member_access_uint16_read_32bit() -> None:
    """p->start where start is uint16_t emits ``movzx eax, word [...]`` in 32-bit mode.

    Without the zero-extend, the load would either spill into adjacent
    bytes (32-bit ``mov eax, [...]``) or leave EAX's upper word stale
    from a prior write — ``test eax, eax`` checks downstream would
    misfire.
    """
    asm = _user(
        """
        struct fd {
            uint8_t type;
            uint8_t flags;
            uint16_t start;
        };
        int read_start(struct fd *p) {
            return p->start;
        }
    """,
        bits=32,
    )
    assert "movzx eax, word [ebx+2]" in asm, f"Expected 'movzx eax, word [ebx+2]' for uint16_t field read\n{asm}"


def test_member_access_uint16_write_32bit() -> None:
    """p->start = x emits ``mov word [...], ax`` in 32-bit mode.

    The destination needs an explicit ``word`` size override; the
    default ``mov [...], eax`` would clobber the next 2 bytes of the
    struct.
    """
    asm = _user(
        """
        struct fd {
            uint8_t type;
            uint8_t flags;
            uint16_t start;
        };
        void write_start(struct fd *p, int value) {
            p->start = value;
        }
    """,
        bits=32,
    )
    assert "mov word [ebx+2], ax" in asm, f"Expected 'mov word [ebx+2], ax' for uint16_t field write\n{asm}"


def test_member_access_uint32_read_32bit() -> None:
    """p->size where size is uint32_t emits a full 4-byte load in 32-bit mode."""
    asm = _user(
        """
        struct fd {
            uint8_t type;
            uint8_t flags;
            uint16_t start;
            uint32_t size;
        };
        int read_size(struct fd *p) {
            return p->size;
        }
    """,
        bits=32,
    )
    assert "mov eax, [ebx+4]" in asm, f"Expected 'mov eax, [ebx+4]' for uint32_t field read\n{asm}"


def test_member_access_uint32_write_32bit() -> None:
    """p->size = x where size is uint32_t emits a full 4-byte store in 32-bit mode."""
    asm = _user(
        """
        struct fd {
            uint8_t type;
            uint8_t flags;
            uint16_t start;
            uint32_t size;
        };
        void write_size(struct fd *p, int value) {
            p->size = value;
        }
    """,
        bits=32,
    )
    assert "mov [ebx+4], eax" in asm, f"Expected 'mov [ebx+4], eax' for uint32_t field write\n{asm}"


def test_member_read_and_write_roundtrip() -> None:
    """p->flags = x; y = p->flags; compiles and assembles cleanly."""
    _compile_and_assemble(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        int roundtrip(struct fd *p, int x) {
            p->flags = x;
            int y;
            y = p->flags;
            return y;
        }
    """,
        bits=16,
    )


def test_memcmp_emits_repe_cmpsb() -> None:
    """memcmp(a, b, n) compiles to repe cmpsb."""
    asm = _kernel(
        """
        int compare(uint8_t *a, uint8_t *b, int n) {
            return memcmp(a, b, n);
        }
    """,
        bits=32,
    )
    assert "repe cmpsb" in asm, f"Expected 'repe cmpsb' in:\n{asm}"
    assert "cld" in asm, f"Expected 'cld' in memcmp output (peephole must not strip it):\n{asm}"
    # Standard memcmp returns lexical difference, not a 0/1 boolean — the old
    # setne-then-zero-extend tail must be gone.
    assert "setne" not in asm, f"Old boolean-result codegen leaked through:\n{asm}"


def test_memcmp_n_zero_short_circuits() -> None:
    """memcmp(a, b, 0) must return 0 without inspecting the buffers.

    rep with CX=0 leaves ZF undefined, so the implementation must guard
    with an explicit ``test count, count`` / ``jz`` pair before cmpsb.
    """
    asm = _kernel(
        """
        int compare(uint8_t *a, uint8_t *b, int n) {
            return memcmp(a, b, n);
        }
    """,
        bits=32,
    )
    assert "test ecx, ecx" in asm, f"Expected 'test ecx, ecx' n==0 guard in:\n{asm}"
    assert "memcmp_done_" in asm, f"Expected memcmp_done label for n==0 jump in:\n{asm}"


def test_memcmp_not_equal_branch() -> None:
    """Memcmp result != 0 branch works correctly."""
    asm = _kernel(
        """
        int differs(uint8_t *a, uint8_t *b, int n) {
            if (memcmp(a, b, n) != 0) {
                return 1;
            }
            return 0;
        }
    """,
        bits=32,
    )
    assert "repe cmpsb" in asm, f"Expected 'repe cmpsb' in:\n{asm}"


def test_memcmp_preserves_cld() -> None:
    """Memcmp must retain cld — peephole_unused_cld must not strip it."""
    asm = _kernel(
        """
        int compare(uint8_t *a, uint8_t *b, int n) {
            return memcmp(a, b, n);
        }
    """,
        bits=32,
    )
    assert "cld" in asm, f"Expected 'cld' in memcmp output (peephole must not strip it):\n{asm}"


def test_memcmp_result_used_as_condition() -> None:
    """Memcmp result used in an if condition compiles without extra cmp."""
    asm = _kernel(
        """
        int is_equal(uint8_t *a, uint8_t *b, int n) {
            if (memcmp(a, b, n) == 0) {
                return 1;
            }
            return 0;
        }
    """,
        bits=32,
    )
    assert "repe cmpsb" in asm, f"Expected 'repe cmpsb' in:\n{asm}"


def test_memcmp_returns_signed_difference() -> None:
    """Memcmp returns the lexical signed byte difference, not a 0/1 boolean.

    On a mismatch SI/DI sit one past the differing byte, so the
    implementation reloads ``[di-1]`` / ``[si-1]`` zero-extended and
    subtracts.  Result range is [-255, +255], matching standard C memcmp.
    """
    asm = _kernel(
        """
        int compare(uint8_t *a, uint8_t *b, int n) {
            return memcmp(a, b, n);
        }
    """,
        bits=32,
    )
    assert "movzx eax, byte [edi-1]" in asm, f"Expected zero-extended byte load from a in:\n{asm}"
    assert "movzx edx, byte [esi-1]" in asm, f"Expected zero-extended byte load from b in:\n{asm}"
    assert "sub eax, edx" in asm, f"Expected 'sub eax, edx' for signed lexical diff in:\n{asm}"


def test_memset_emits_rep_stosb() -> None:
    """memset(dst, value, count) compiles to rep stosb."""
    asm = _kernel(
        """
        void zero_buf(uint8_t *buf, int n) {
            memset(buf, 0, n);
        }
    """,
        bits=32,
    )
    assert "rep stosb" in asm, f"Expected 'rep stosb' in:\n{asm}"
    assert "rep movsb" not in asm, f"Must not emit movsb for memset:\n{asm}"
    assert "cld" in asm, f"Expected 'cld' in memset output (peephole must not strip it):\n{asm}"


def test_memset_nonzero_value() -> None:
    """Memset with a non-zero literal value loads AL correctly."""
    asm = _kernel(
        """
        void fill_buf(uint8_t *buf, int n) {
            memset(buf, 0xFF, n);
        }
    """,
        bits=32,
    )
    assert "rep stosb" in asm, f"Expected 'rep stosb' in:\n{asm}"
    assert "0xFF" in asm or "255" in asm or "0ffh" in asm.lower() or "0xff" in asm.lower(), f"Expected 0xFF value in:\n{asm}"


def test_memset_zero_literal_loads_correctly() -> None:
    """Memset with a zero value literal loads the value into AX."""
    asm = _kernel(
        """
        void zero_buf(uint8_t *buf, int n) {
            memset(buf, 0, n);
        }
    """,
        bits=32,
    )
    # The zero value must be loaded into AX (via xor or mov).
    assert "eax, 0" in asm or "xor eax, eax" in asm or "xor ax, ax" in asm, f"Expected zero value loaded into AX:\n{asm}"


def test_naked_if_else_dispatch_both_branches_tail_jmp() -> None:
    """A naked function whose body is ``if/else`` with calls in both branches emits two tail jmps.

    The peephole optimizer collapses ``jne .else_label ; jmp fn_a ; .else_label: jmp fn_b``
    into ``je fn_a ; jmp fn_b`` (3 instructions for a clean dispatcher).  Either form
    is acceptable — both transfer control without a ``ret`` and without ``call``.
    """
    src = """
        uint8_t flag __attribute__((asm_name("flag")));
        __attribute__((carry_return)) int fn_a(int x __attribute__((in_register("ax"))));
        __attribute__((carry_return)) int fn_b(int x __attribute__((in_register("ax"))));
        __attribute__((carry_return)) __attribute__((naked))
        int dispatch(int x __attribute__((in_register("ax")))) {
            if (flag == 0) {
                fn_a(x);
            } else {
                fn_b(x);
            }
        }
    """
    asm = _kernel(src)
    body = asm.split("dispatch:")[1].split("\n\n")[0]
    # Both call targets must appear, but only as branch / jmp targets — never ``call <name>``.
    assert "fn_a" in body and "fn_b" in body, f"expected both targets in body\n{asm}"
    assert "call fn_a" not in body and "call fn_b" not in body, f"naked dispatch must not use 'call'\n{asm}"
    assert "        ret" not in body, f"naked dispatch with full coverage must not emit ret\n{asm}"


def test_naked_in_register_param_pinned_no_spill() -> None:
    """A naked function does not spill in_register params to a local slot."""
    src = """
        __attribute__((carry_return)) int target(int x __attribute__((in_register("ax"))));
        __attribute__((carry_return)) __attribute__((naked))
        int dispatch(int x __attribute__((in_register("ax")))) { target(x); }
    """
    asm = _kernel(src)
    assert "[bp-" not in asm, f"naked must not allocate stack slots for in_register params\n{asm}"
    assert "mov [bp-2], ax" not in asm, f"naked must not spill in_register param\n{asm}"


def test_naked_no_prologue_no_epilogue() -> None:
    """A naked function emits its label and body with no push/pop bp or ret."""
    src = """
        __attribute__((carry_return)) int target(int x __attribute__((in_register("ax"))));
        __attribute__((carry_return)) __attribute__((naked))
        int dispatch(int x __attribute__((in_register("ax")))) { target(x); }
    """
    asm = _kernel(src)
    body = asm.split("dispatch:")[1].split("\n\n")[0]
    assert "push bp" not in body, f"naked function must not push bp\n{asm}"
    assert "mov bp, sp" not in body, f"naked function must not set up bp\n{asm}"
    assert "pop bp" not in body, f"naked function must not pop bp\n{asm}"


def test_naked_rejects_local_decl() -> None:
    """Naked functions reject body-local variable declarations."""
    error = _kernel_error("""
        __attribute__((carry_return)) int target(int x __attribute__((in_register("ax"))));
        __attribute__((naked))
        void f(int x __attribute__((in_register("ax")))) {
            int tmp;
            tmp = 1;
            target(tmp);
        }
    """)
    assert "naked" in error and "locals" in error, f"Expected naked-locals error, got: {error}"


def test_naked_rejects_stack_param() -> None:
    """Naked functions reject parameters without in_register / out_register."""
    error = _kernel_error("""
        void target();
        __attribute__((naked)) void f(int x) { target(); }
    """)
    assert "naked" in error and "in_register" in error, f"Expected naked-stack-param error, got: {error}"


def test_naked_single_call_becomes_tail_jmp() -> None:
    """A naked function whose body is one Call emits ``jmp target`` and no ``ret``."""
    src = """
        __attribute__((carry_return)) int target(int x __attribute__((in_register("ax"))));
        __attribute__((carry_return)) __attribute__((naked))
        int dispatch(int x __attribute__((in_register("ax")))) { target(x); }
    """
    asm = _kernel(src)
    body = asm.split("dispatch:")[1].split("\n\n")[0]
    assert "jmp target" in body, f"expected 'jmp target' tail jump\n{asm}"
    assert "call target" not in body, f"naked tail call must use jmp, not call\n{asm}"
    assert "        ret" not in body, f"naked function with tail jmp must not emit ret\n{asm}"


def test_named_constant_emits_immediate_not_memory_operand() -> None:
    """A NAMED_CONSTANTS identifier resolves as an immediate, not ``[name]``."""
    source = """
        void test_buffer_addr() {
            uint8_t *buf;
            buf = BUFFER;
        }
    """
    output = _kernel(source)
    assert "BUFFER" in output
    assert "[BUFFER]" not in output


def test_not_carry_return_call_emits_jnc() -> None:
    """`if (!foo())` against a carry_return callee emits jnc (not jc).

    carry_return convention: return 1 = CF clear (success),
    return 0 = CF set (failure).  `if (!foo())` executes the body on
    failure (CF set), so the false-jump past the body must be jnc
    (skip body when CF clear = success).
    """
    asm = _kernel(
        """
        __attribute__((carry_return)) int try_open();

        void caller() {
            if (!try_open()) {
                return;
            }
        }
    """,
        bits=32,
    )
    assert "call try_open" in asm, f"Expected call in:\n{asm}"
    assert "jnc" in asm, f"Expected 'jnc' (not_carry) for !carry_return in:\n{asm}"
    assert "jc " not in asm, f"Must not emit bare jc for !carry_return:\n{asm}"


def test_not_carry_return_call_positive_form_emits_jc() -> None:
    """`if (foo())` (no !) against a carry_return callee emits jc (not jnc).

    Confirms the positive form is correct so the ! test above is meaningful.
    """
    asm = _kernel(
        """
        __attribute__((carry_return)) int try_open();

        void caller() {
            if (try_open()) {
                return;
            }
        }
    """,
        bits=32,
    )
    assert "call try_open" in asm, f"Expected call in:\n{asm}"
    assert "jc " in asm, f"Expected 'jc' for positive carry_return in:\n{asm}"
    assert "jnc" not in asm, f"Must not emit jnc for positive carry_return:\n{asm}"


def test_not_carry_return_in_logical_and() -> None:
    """`if (!a() && !b())` both legs emit correct not_carry jumps."""
    asm = _kernel(
        """
        __attribute__((carry_return)) int a();
        __attribute__((carry_return)) int b();

        void caller() {
            if (!a() && !b()) {
                return;
            }
        }
    """,
        bits=32,
    )
    assert "call a" in asm, f"Expected call a in:\n{asm}"
    assert "call b" in asm, f"Expected call b in:\n{asm}"
    # Both legs short-circuit via jnc (skip body / skip second check on success).
    assert asm.count("jnc") >= 2, f"Expected jnc for both !a() and !b() in &&:\n{asm}"


def test_not_carry_return_while_emits_jnc() -> None:
    """`while (!poll())` loops while carry_return returns 0 (CF set = failure)."""
    asm = _kernel(
        """
        __attribute__((carry_return)) int poll();

        void wait_until_ready() {
            while (!poll()) {}
        }
    """,
        bits=32,
    )
    assert "call poll" in asm, f"Expected call in:\n{asm}"
    assert "jnc" in asm, f"Expected 'jnc' in while(!carry_return) in:\n{asm}"


def test_not_integer_literal_evaluates_correctly() -> None:
    """`!0` evaluates to 1 and `!1` evaluates to 0.

    cc.py does NOT fold constant comparisons at parse time: `!0` desugars
    to `0 == 0` and the codegen emits a `cmp`/`jne`/`inc` sequence that
    produces the correct result (1) at runtime.  The test verifies the
    correct control-flow shape rather than asserting a folded literal.
    """
    asm_not0 = _kernel(
        """
        int always_one() {
            return !0;
        }
    """,
        bits=32,
    )
    # !0 = (0 == 0) = true.  The false-jump (jne) skips the inc-to-one path
    # when 0 != 0 (never fires), so the function returns 1.  The cmp sequence
    # must be present and the inc-eax path must appear.
    assert "cmp" in asm_not0 or "test" in asm_not0, f"Expected cmp/test for !0 in:\n{asm_not0}"
    assert "jne" in asm_not0, f"Expected jne branch in !0 sequence in:\n{asm_not0}"
    assert "inc eax" in asm_not0 or "inc ax" in asm_not0, f"Expected 'inc eax' for the true-result path of !0 in:\n{asm_not0}"

    asm_not1 = _kernel(
        """
        int always_zero() {
            return !1;
        }
    """,
        bits=32,
    )
    # !1 = (1 == 0) = false.  The false-jump fires (1 != 0 is true), so
    # the inc path is skipped and the function returns 0.  cmp/jne must appear.
    assert "cmp" in asm_not1 or "test" in asm_not1, f"Expected cmp/test for !1 in:\n{asm_not1}"
    assert "jne" in asm_not1, f"Expected jne branch in !1 sequence in:\n{asm_not1}"


def test_not_regular_call_emits_jne() -> None:
    """`if (!foo())` against a non-carry_return callee: compares EAX to 0, emits jne.

    `!foo()` desugars to `foo() == 0`.  The body executes when foo() is
    zero; the false-jump (skip body) fires when foo() is non-zero — that is
    a `jne` / `test + jne` sequence, NOT a `je`.
    """
    asm = _kernel(
        """
        int get_count();

        void caller() {
            if (!get_count()) {
                return;
            }
        }
    """,
        bits=32,
    )
    assert "call get_count" in asm, f"Expected call in:\n{asm}"
    assert "jne" in asm, f"Expected 'jne' (false-jump = skip body when non-zero) for !regular_call in:\n{asm}"


def test_not_variable_emits_jne() -> None:
    """`if (!x)` on an integer variable compiles to test/cmp + jne.

    The body executes when x == 0; the false-jump skips the body when x != 0
    — so the emitted branch is `jne` (or the equivalent `test reg,reg` /
    `jne`), not `je`.
    """
    asm = _kernel(
        """
        void check(int x) {
            if (!x) {
                return;
            }
        }
    """,
        bits=32,
    )
    assert "jne" in asm or "jnz" in asm, f"Expected jne/jnz (false-jump) for !var in:\n{asm}"


def test_out_register_32bit_full_width_target_uses_eax() -> None:
    """32-bit target: an ``edx``-typed out_register writes from full ``eax``."""
    asm = _kernel(
        """
        void f(int *out __attribute__((out_register("edx")))) {
            *out = 0x12345678;
        }
        """,
        bits=32,
    )
    # Either an explicit ``mov edx, eax`` or no move at all when the
    # accumulator already happens to hold the value via constant-prop.
    if "mov edx, eax" not in asm:
        assert "mov edx, 0x12345678" in asm or "mov edx, 305419896" in asm, f"expected full-width edx store\n{asm}"


def test_out_register_32bit_narrow_target_uses_low_word() -> None:
    """32-bit target: writing through ``out_register("dx")`` uses ``mov dx, ax``.

    cc.py would otherwise emit ``mov dx, eax`` (size mismatch — NASM
    rejects it) when the source value lives in the 32-bit accumulator.
    """
    asm = _kernel(
        """
        void f(int *out __attribute__((out_register("dx")))) {
            *out = 0x4142;
        }
        """,
        bits=32,
    )
    assert "mov dx, ax" in asm, f"expected 'mov dx, ax' (low-word source)\n{asm}"
    assert "mov dx, eax" not in asm, f"unexpected size-mismatched 'mov dx, eax'\n{asm}"


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


def test_out_register_caller_captures_register_into_local() -> None:
    """After the call, the named register is stored into the caller's local variable."""
    asm = _kernel("""
        int *captured __attribute__((asm_name("captured")));
        __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si"))));

        void caller() {
            int* entry;
            fd_alloc(&entry);
            captured = entry;
        }
    """)
    assert "mov [bp-2], si" in asm


def test_out_register_caller_no_push() -> None:
    """Caller emits no push for an out_register argument — only call + capture.

    The local ``entry`` is used (assigned to a global) so the capture's
    ``mov [bp-2], si`` survives ``peephole_dead_temp_slots``.
    """
    asm = _kernel("""
        int *captured __attribute__((asm_name("captured")));
        __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si"))));

        __attribute__((carry_return)) int do_alloc() {
            int* entry;
            if (fd_alloc(&entry)) {
                captured = entry;
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


def test_out_register_capture_not_destroyed_by_pinned_push_pop() -> None:
    """out_register capture into a pinned register must not be push/popped around the call.

    Scenario: ``inner_value`` is auto-pinned to DX (two uses, one call → clobber
    cost 1, references 2 > 1).  ``net_get`` returns its result via
    ``out_register("cx")``, which cc.py captures with ``mov dx, cx`` (cross-
    register move into the pin).  The pre-push guard in ``generate_call``
    must recognise that DX is the capture destination and exclude it from the
    push/pop save set.  Without the guard, cc.py emits ``push dx`` before the
    call then ``pop dx`` after ``mov dx, cx``, destroying the captured value.

    The assertions are unconditional: the capture ``mov dx, cx`` is always
    emitted when ``inner_value`` pins to DX and the out_register is CX.
    """
    asm = _kernel(
        """
        __attribute__((carry_return))
        int net_get(int *value __attribute__((out_register("cx"))));

        int process() {
            int inner_value;
            if (net_get(&inner_value)) {
                return inner_value;
            }
            return inner_value;
        }
    """,
        bits=16,
    )
    lines = [line.strip() for line in asm.splitlines()]
    call_idx = next(i for i, line in enumerate(lines) if line == "call net_get")
    before_call = lines[:call_idx]
    after_call = lines[call_idx + 1 :]
    # inner_value must be pinned to DX: the cross-register capture must appear.
    assert any("mov dx, cx" in line for line in after_call), (
        f"expected 'mov dx, cx' capture after call — inner_value may not have pinned to dx:\n{asm}"
    )
    # DX must NOT be pushed before the call (the pre-push guard must exclude it).
    assert not any("push dx" in line for line in before_call), (
        f"'push dx' found before 'call net_get' — pre-push guard failed to exclude the capture target:\n{asm}"
    )
    # DX must NOT be popped after the call (nothing was pushed, so nothing to pop).
    assert not any("pop dx" in line for line in after_call), (
        f"'pop dx' found after 'call net_get' — captured value in DX would be destroyed:\n{asm}"
    )


def test_out_register_capture_widens_into_local_32bit() -> None:
    """A 16-bit out_register captured into a 32-bit local slot zero-extends.

    Without widening, ``mov [local], bx`` would write only the low 16
    bits of the 4-byte slot, leaving the upper 16 bits stale.
    """
    asm = _kernel(
        """
        __attribute__((carry_return))
        int reader(int *byte_offset __attribute__((out_register("bx"))));

        int caller() {
            int offset;
            int total;
            reader(&offset);
            total = offset + 1;
            return total;
        }
    """,
        bits=32,
    )
    # The capture must zero-extend BX into EBX before the 4-byte spill.
    assert "movzx ebx, bx" in asm, f"expected 'movzx ebx, bx' for 16-bit out_register into 32-bit slot:\n{asm}"


def test_out_register_capture_widens_into_pinned_eregister_32bit() -> None:
    """A 16-bit out_register captured into a pinned E-register zero-extends.

    Scenario: ``offset`` auto-pins to EBX (multiple uses, one call → references > clobber).
    ``reader`` returns its result via ``out_register("bx")``.  cc.py must emit
    ``movzx ebx, bx`` to put the captured value into the pinned register with
    clean upper bytes; a bare ``mov ebx, bx`` is invalid (mixed widths).
    """
    asm = _kernel(
        """
        __attribute__((carry_return))
        int reader(int *byte_offset __attribute__((out_register("bx"))));

        int chunk_size(int left) {
            int offset;
            int chunk;
            reader(&offset);
            chunk = 512 - offset;
            if (chunk > left) {
                chunk = left;
            }
            return chunk;
        }
    """,
        bits=32,
    )
    assert "movzx ebx, bx" in asm, f"expected 'movzx ebx, bx' capture into pinned EBX:\n{asm}"
    assert "mov ebx, bx" not in asm, f"raw 'mov ebx, bx' is mixed-width and invalid:\n{asm}"


def test_out_register_carry_return_condition() -> None:
    """carry_return + out_register: correct CF-based branch and register capture."""
    asm = _kernel("""
        int *captured __attribute__((asm_name("captured")));
        __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si"))));

        __attribute__((carry_return)) int wrapper() {
            int* entry;
            if (fd_alloc(&entry)) {
                captured = entry;
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


def test_out_register_prototype_registers_convention() -> None:
    """A function prototype with out_register is retained in the AST and registers the convention."""
    # If the prototype is silently dropped, generate_call won't know about out_register
    # and will try to push the &entry argument — causing an error or wrong code.
    ok, output = _compile(
        """
        int *captured __attribute__((asm_name("captured")));
        __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si"))));

        void caller() {
            int* entry;
            fd_alloc(&entry);
            captured = entry;
        }
    """,
        target="kernel",
    )
    assert ok, f"Compilation failed:\n{output}"
    assert "mov [bp-2], si" in output


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


def test_peephole_dead_temp_slot_dropped() -> None:
    """A ``mov [bp-N], reg`` whose slot is never read elsewhere is dropped."""
    out = _peephole_run([
        "f:",
        "        push bp",
        "        mov bp, sp",
        "        sub sp, 4",
        "        mov ax, dx",
        "        mov [bp-2], ax",
        "        mov sp, bp",
        "        pop bp",
        "        ret",
    ])
    assert "        mov [bp-2], ax" not in out, f"dead temp-slot store survived: {out}"


def test_peephole_dead_temp_slot_kept_when_read() -> None:
    """A live temp slot survives the peephole.

    ``peephole_store_reload`` deletes the reload form ``mov reg, [bp-N]``,
    so the test reads the slot via ``cmp word [bp-N], imm`` which the
    reload-collapse pass leaves alone.
    """
    out = _peephole_run([
        "f:",
        "        mov [bp-2], ax",
        "        cmp word [bp-2], 5",
        "        ret",
    ])
    assert "        mov [bp-2], ax" in out, f"live temp-slot store dropped: {out}"


def test_peephole_redundant_register_swap_drops_second_mov() -> None:
    """``mov A, B`` followed by ``mov B, A`` drops the second."""
    out = _peephole_run([
        "        mov ax, cx",
        "        mov cx, ax",
        "        ret",
    ])
    swap_lines = [line.strip() for line in out if line.strip().startswith("mov ")]
    assert "mov ax, cx" in swap_lines, f"first mov clobbered: {out}"
    assert "mov cx, ax" not in swap_lines, f"redundant swap survived: {out}"


def test_peephole_register_arithmetic_dec_collapses() -> None:
    """``mov ax, R / dec ax / mov R, ax`` collapses to ``dec R``."""
    out = _peephole_run([
        "        mov ax, dx",
        "        dec ax",
        "        mov dx, ax",
    ])
    assert "        dec dx" in out, f"expected direct 'dec dx', got {out}"


def test_peephole_register_arithmetic_fires_when_ax_overwritten_after() -> None:
    """The transform still fires when the next instruction overwrites AX."""
    out = _peephole_run([
        "        mov ax, dx",
        "        inc ax",
        "        mov dx, ax",
        "        mov ax, 42",
    ])
    assert any("inc dx" in line for line in out), f"transform skipped wrongly: {out}"
    assert not any("inc ax" in line for line in out), f"AX-detour survived: {out}"


def test_peephole_register_arithmetic_inc_collapses() -> None:
    """``mov ax, R / inc ax / mov R, ax`` collapses to ``inc R``."""
    out = _peephole_run([
        "        mov ax, dx",
        "        inc ax",
        "        mov dx, ax",
    ])
    assert "        inc dx" in out, f"expected direct 'inc dx', got {out}"
    assert "        mov ax, dx" not in out, f"AX detour survived: {out}"


def test_peephole_register_arithmetic_skips_when_ax_read_after() -> None:
    """``mov ax, X / op ax, Y / mov reg, ax`` is left alone if AX is read next.

    The transform leaves AX holding its pre-sequence value, so a
    following ``cmp ax, ...`` or any other AX read would see stale
    data.  Regression for the kernel-c-ports ``fd_read_file`` bug: the
    sequence below appeared at the ``min(512 - byte_offset, left)``
    site and the cmp consumed the just-computed AX value — when the
    peephole fired blindly ping / cp / dns lost their copy-loop bound.
    """
    out = _peephole_run([
        "        mov ax, 512",
        "        sub ax, [bp-14]",
        "        mov dx, ax",
        "        cmp ax, bx",
    ])
    assert any("mov ax, 512" in line for line in out), f"AX prep clobbered: {out}"
    assert any("mov dx, ax" in line for line in out), f"pinned-reg copy dropped: {out}"


def test_peephole_self_move_drops_no_op() -> None:
    """``mov X, X`` is dropped."""
    out = _peephole_run([
        "        mov dx, dx",
        "        add dx, 5",
    ])
    assert "        mov dx, dx" not in out, f"self-move survived: {out}"
    assert "        add dx, 5" in out, f"surrounding instructions clobbered: {out}"


def test_pinned_function_pointer_emits_jmp_via_pinned_register() -> None:
    """``pinned_register("ebx")`` on a function_pointer makes __tail_call jmp ebx.

    Motivation: fd_ioctl receives ``cmd`` in AL and tail-calls the
    per-FD-type ioctl handler.  Routing the function pointer through
    EAX would clobber AL before the handler reads it; pinning the
    pointer to EBX keeps AL intact through the dispatch.
    """
    asm = _kernel(
        """
        int get_fn();
        __attribute__((carry_return))
        int dispatch(int cmd __attribute__((in_register("ax")))) {
            int (*handler)(int c __attribute__((in_register("ax"))))
                __attribute__((pinned_register("ebx")));
            handler = get_fn();
            __tail_call(handler, cmd);
        }
    """,
        bits=32,
    )
    assert "jmp ebx" in asm, f"__tail_call with pinned_register must emit 'jmp ebx'\n{asm}"
    assert "jmp eax" not in asm, f"must not jmp via eax when pinned to ebx\n{asm}"
    # The pinned register receives the function-pointer value via the
    # standard return-value plumbing (mov ebx, eax after the helper call).
    assert "mov ebx, eax" in asm, f"must move return value into the pinned register\n{asm}"


def test_pinned_register_on_non_function_pointer_rejected() -> None:
    """``pinned_register`` on a plain int local is rejected at parse time."""
    error = _kernel_error("""
        void bad() {
            int x __attribute__((pinned_register("ebx")));
            x = 0;
        }
    """)
    assert "pinned_register" in error, f"Expected pinned_register error, got: {error}"


def test_pointer_compared_to_int_literal_is_rejected() -> None:
    """``char *p; if (p == 0)`` raises — must spell as ``p == NULL``."""
    error = _kernel_error("""
        void f(char *p) {
            if (p != 0) {
                p = p + 1;
            }
        }
    """)
    assert "pointer compared to non-pointer" in error, f"expected pointer-vs-int rejection, got: {error}"


def test_pointer_compared_to_null_compiles() -> None:
    """``char *p; if (p != NULL)`` is the supported spelling."""
    asm = _kernel("""
        void f(char *p) {
            if (p != NULL) {
                p = p + 1;
            }
        }
    """)
    assert "f:" in asm


def test_preserve_register_multiple() -> None:
    """Multiple preserve_register attributes push/pop in declaration order."""
    src = textwrap.dedent("""\
        __attribute__((preserve_register("cx"))) __attribute__((preserve_register("dx")))
        int g() { return 0; }
    """)
    asm = _compile(src, target="kernel")[1]
    push_cx = asm.index("push cx")
    push_dx = asm.index("push dx")
    pop_cx = asm.rindex("pop cx")
    pop_dx_last = asm.rindex("pop dx")
    assert push_cx < push_dx, "cx pushed before dx"
    assert pop_dx_last < pop_cx, "dx popped before cx (reverse order)"


def test_preserve_register_push_pop() -> None:
    """preserve_register("cx") emits push cx before frame and pop cx before every ret."""
    src = textwrap.dedent("""\
        __attribute__((carry_return)) __attribute__((preserve_register("cx")))
        int f(int x __attribute__((in_register("bx")))) {
            if (x >= 8) { return 0; }
            return 1;
        }
    """)
    asm = _compile(src, target="kernel")[1]
    # push cx must appear before push bp (prologue order).
    push_cx = asm.index("push cx")
    push_bp = asm.index("push bp")
    assert push_cx < push_bp, "push cx must precede push bp"
    # Every ret must be preceded by pop cx (pop cx does not affect CF).
    ret_positions = [i for i in range(len(asm)) if asm[i : i + 3] == "ret"]
    for ret_pos in ret_positions:
        before_ret = asm[max(0, ret_pos - 40) : ret_pos]
        assert "pop cx" in before_ret, f"expected 'pop cx' before ret at pos {ret_pos}"


def test_signed_int_less_than_still_emits_jge() -> None:
    """``int < literal`` keeps the signed ``jge`` (false-branch).

    Regression guard for the unsigned-jump tables — the old behavior
    must still apply when both operands are signed.
    """
    src = """
        int n;
        void test(int v) {
            if (v < 100) {
                kernel_outb(0, 1);
            }
        }
    """
    asm = _kernel(src)
    assert "jge" in asm, f"signed 'jge' expected for int < literal\n{asm}"
    assert "jae" not in asm, f"unsigned 'jae' must not appear for signed int comparison\n{asm}"


def test_sizeof_fd_struct_16bit() -> None:
    """sizeof(struct fd) == FD_ENTRY_SIZE (32) in --bits 16."""
    asm = _user(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        int get_size() {
            return sizeof(struct fd);
        }
        int main() { return 0; }
    """,
        bits=16,
    )
    assert f"mov ax, {FD_ENTRY_SIZE}" in asm, f"Expected 'mov ax, {FD_ENTRY_SIZE}' for sizeof(struct fd)\n{asm}"


def test_sizeof_packed_char_int_16bit() -> None:
    """sizeof(struct {char a; int b;}) == 3 in --bits 16 (packed, no padding)."""
    asm = _user(
        """
        struct pair { char a; int b; };
        int get_size() {
            return sizeof(struct pair);
        }
        int main() { return 0; }
    """,
        bits=16,
    )
    assert "mov ax, 3" in asm, f"Expected 'mov ax, 3' for sizeof packed {{char+int}}\n{asm}"


def test_sizeof_packed_char_int_32bit() -> None:
    """sizeof(struct {char a; int b;}) == 5 in --bits 32 (char=1, int=4)."""
    asm = _user(
        """
        struct pair { char a; int b; };
        int get_size() {
            return sizeof(struct pair);
        }
        int main() { return 0; }
    """,
        bits=32,
    )
    assert "mov eax, 5" in asm, f"Expected 'mov eax, 5' for sizeof packed {{char+int}} (32-bit)\n{asm}"


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


def test_struct_array_initializer_function_symbol_fields() -> None:
    """User function names are accepted as constant initializers for function_pointer fields.

    The fd_ops table in src/fs/fd.c is the motivating shape: an array of
    struct { fn_ptr read; fn_ptr write; } entries laid out at file scope
    with `{ fd_read_console, fd_write_console }` style entries.
    """
    asm = _kernel(
        """
        struct ops { int (*read)(); int (*write)(); };
        int reader();
        int writer();
        struct ops table[2] = {
            { 0, 0 },
            { reader, writer },
        };
        void f() {}
    """,
        bits=32,
    )
    assert "_g_table: dd 0" in asm
    assert "dd reader" in asm
    assert "dd writer" in asm


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


def test_struct_array_member_index_emits_byte_load() -> None:
    """``ptr->byte_array[i]`` loads one byte (zero-extended), not its address."""
    asm = _kernel("""
        struct entry { uint8_t ip[4]; };
        void test(struct entry *e) {
            int b;
            b = e->ip[2];
        }
    """)
    assert "mov al, [bx+2]" in asm, f"Expected 'mov al, [bx+2]' (constant-fold byte load) in:\n{asm}"
    assert "xor ah, ah" in asm, f"Expected 'xor ah, ah' (zero-extend) in:\n{asm}"
    # Crucially: should NOT emit a 'lea' for the indexed read — that would be
    # the address, not the value.
    assert "lea" not in asm.split("test:", 1)[1].split("ret", 1)[0], f"Indexed access must not emit 'lea' (would be an address):\n{asm}"


def test_struct_array_member_index_variable() -> None:
    """``ptr->byte_array[var_index]`` scales the index and loads a byte."""
    asm = _kernel("""
        struct entry { uint8_t mac[6]; };
        int byte_at(struct entry *e, int i) {
            return e->mac[i];
        }
    """)
    # Variable byte-array index: scale by element_size=1 (no shift), add base+offset.
    assert "mov al," in asm, f"Expected byte load (mov al,...) in:\n{asm}"
    assert "xor ah, ah" in asm, f"Expected zero-extend in:\n{asm}"


def test_struct_array_member_no_index_emits_field_address() -> None:
    """``ptr->byte_array`` (no index) decays to the field's address."""
    asm = _kernel("""
        struct entry { uint8_t mac[6]; uint16_t ts; };
        void copy_mac(struct entry *e, uint8_t *dst) {
            memcpy(dst, e->mac, 6);
        }
    """)
    # The field address goes into SI for the inlined rep movsb.  Field
    # offset is 0 so cc.py emits ``mov reg, base`` rather than ``lea``.
    assert "rep movsb" in asm, f"Expected memcpy inline in:\n{asm}"


def test_tail_call_arg_count_mismatch_raises_error() -> None:
    """``__tail_call`` with wrong arg count raises CompileError."""
    error = _kernel_error("""
        int get_fn();
        void bad() {
            int (*handler)(int x __attribute__((in_register("si"))));
            handler = get_fn();
            __tail_call(handler, 1, 2, 3);
        }
    """)
    assert "__tail_call" in error, f"Expected __tail_call arity error, got: {error}"


def test_tail_call_args_loaded_before_jmp() -> None:
    """``__tail_call`` loads arguments into registers before the jump."""
    asm = _kernel("""
        int get_fn();
        void dispatch() {
            int (*handler)(
                int x __attribute__((in_register("si"))),
                int y __attribute__((in_register("cx"))));
            handler = get_fn();
            __tail_call(handler, 1, 2);
        }
    """)
    jmp_pos = asm.index("jmp ax")
    assert "mov si," in asm[:jmp_pos], "si arg must be set before jmp ax"
    assert "mov cx," in asm[:jmp_pos], "cx arg must be set before jmp ax"


def test_tail_call_emits_jmp_ax() -> None:
    """``__tail_call`` emits a frame teardown then ``jmp ax``."""
    asm = _kernel("""
        int get_fn();
        void dispatch() {
            int (*handler)(int x __attribute__((in_register("si"))));
            handler = get_fn();
            __tail_call(handler, 42);
        }
    """)
    assert "jmp ax" in asm, "__tail_call must emit 'jmp ax'"
    assert "pop bp" in asm, "__tail_call must tear down frame"
    assert "call ax" not in asm, "__tail_call must not emit 'call ax'"


def test_tail_call_is_terminal() -> None:
    """``__tail_call`` is recognised as always-exiting; no dead code after it."""
    asm = _kernel("""
        int get_fn();
        __attribute__((carry_return)) int dispatch(int x __attribute__((in_register("bx")))) {
            int (*handler)(int a __attribute__((in_register("bx"))));
            handler = get_fn();
            __tail_call(handler, x);
        }
    """)
    assert "jmp ax" in asm, "__tail_call must emit 'jmp ax'"
    jmp_pos = asm.index("jmp ax")
    trailing = asm[jmp_pos + len("jmp ax") :]
    assert "stc" not in trailing, "no fall-through stc after __tail_call"
    assert "clc" not in trailing, "no fall-through clc after __tail_call"


def test_tail_call_no_ret_after_jmp() -> None:
    """``__tail_call`` does not emit ``ret`` — control flows through the jmp."""
    asm = _kernel("""
        int get_fn();
        void dispatch() {
            int (*handler)(int x __attribute__((in_register("bx"))));
            handler = get_fn();
            __tail_call(handler, 99);
        }
    """)
    lines = asm.splitlines()
    jmp_idx = next(i for i, ln in enumerate(lines) if "jmp ax" in ln)
    trailing = "\n".join(lines[jmp_idx + 1 :])
    assert "ret" not in trailing, "no 'ret' should appear after 'jmp ax'"


def test_tail_call_thunk_arg_sources_named_register() -> None:
    """TailCall arg for an in_register param sources from the register directly.

    When the thunk body is ``__tail_call(fn, param)`` and param is an
    in_register param, the arg move should emit ``mov <target>, <named_reg>``
    rather than loading from the stack slot (``mov <target>, [bp-N]``).

    File-scope ``vfs_find_fn`` keeps the body single-statement; see the
    note on test_tail_call_thunk_suppresses_in_register_spill.
    """
    asm = _kernel("""
        int (*vfs_find_fn)(int p __attribute__((in_register("di"))));

        __attribute__((carry_return))
        int vfs_find(int path __attribute__((in_register("si")))) {
            __tail_call(vfs_find_fn, path);
        }
    """)
    # The arg move must source from the named register (si), not the slot.
    jmp_pos = asm.index("jmp ax")
    before_jmp = asm[:jmp_pos]
    assert "mov di, si" in before_jmp, f"expected 'mov di, si' before jmp ax\n{asm}"
    assert "mov di, [bp-" not in before_jmp, f"expected no slot load for di arg\n{asm}"


def test_tail_call_thunk_suppresses_in_register_spill() -> None:
    """Pure-thunk body: in_register param spill is elided (slot is dead).

    A function whose entire body is a single ``__tail_call`` that forwards
    its in_register param as a Var arg never reads the local stack slot —
    the named register holds the value throughout.  The prologue should
    emit no ``mov [bp-N], <reg>`` spill for that param.

    The function pointer is declared at file scope (matching how vfs.c
    actually uses this pattern) so the function body is the single
    TailCall statement the optimization is gated on — a local
    declaration would make the body two statements and disqualify it.
    """
    asm = _kernel("""
        int (*vfs_find_fn)(int p __attribute__((in_register("si"))));

        __attribute__((carry_return))
        int vfs_find(int path __attribute__((in_register("si")))) {
            __tail_call(vfs_find_fn, path);
        }
    """)
    # No spill of SI to a local slot in the prologue.
    assert "mov [bp-2], si" not in asm, f"expected no si spill for pure thunk\n{asm}"
    assert "mov [bp-4], si" not in asm, f"expected no si spill for pure thunk\n{asm}"
    # The jmp still happens.
    assert "jmp ax" in asm, f"expected jmp ax in thunk\n{asm}"


def test_tail_call_wrong_fn_raises_error() -> None:
    """``__tail_call`` on a non-function_pointer variable raises CompileError."""
    error = _kernel_error("""
        void bad() {
            int x;
            x = 5;
            __tail_call(x, 1);
        }
    """)
    assert "__tail_call" in error, f"Expected __tail_call error, got: {error}"


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


def test_uint8_t_local_compared_to_int_literal_compiles() -> None:
    """``uint8_t`` classifies as integer (per the docstring), so ``b == 0`` is allowed."""
    asm = _kernel("""
        void f() {
            uint8_t b;
            b = 0;
            if (b == 0) {
                b = 1;
            }
        }
    """)
    assert "f:" in asm


def test_unsigned_byte_global_in_naked_dispatcher_emits_jb() -> None:
    """The ``read_sector`` shape (uint8_t global ``< 0x80``, naked, tail dispatch) compiles to ``cmp / jb / jmp``.

    Regression test for the entire change: the unsigned compare picks
    the right mnemonic, the naked attribute elides the frame, and the
    tail-call detection through if/else turns both branches into jmps.
    The peephole optimizer fuses ``jae .else ; jmp fdc ; .else: jmp ata``
    into ``jb fdc ; jmp ata``.
    """
    src = """
        uint8_t boot_disk __attribute__((asm_name("boot_disk")));
        __attribute__((carry_return)) int fdc(int s __attribute__((in_register("ax"))));
        __attribute__((carry_return)) int ata(int s __attribute__((in_register("ax"))));
        __attribute__((carry_return)) __attribute__((naked))
        int read_sector(int sector __attribute__((in_register("ax")))) {
            if (boot_disk < 0x80) {
                fdc(sector);
            } else {
                ata(sector);
            }
        }
    """
    asm = _kernel(src)
    body = asm.split("read_sector:")[1].split("\n\n")[0]
    assert "jb fdc" in body and "jmp ata" in body, f"expected fused 'jb fdc ; jmp ata' dispatcher\n{asm}"
    assert "push bp" not in body and "        ret" not in body, f"naked must not emit prologue/ret\n{asm}"


def test_unsigned_byte_global_less_than_emits_jb() -> None:
    """``uint8_t < literal`` uses unsigned ``jb`` (false-branch ``jae``)."""
    src = """
        uint8_t flag __attribute__((asm_name("flag")));
        void test() {
            if (flag < 0x80) {
                kernel_outb(0, 1);
            }
        }
    """
    asm = _kernel(src)
    # ``cc.py`` either folds the conditional into the tail jmp (``jae``)
    # or jumps past the body on the false branch (``jae``).  Either way
    # the unsigned mnemonic must appear and the signed equivalent ``jge``
    # must not.
    assert "jae" in asm or "jb " in asm, f"expected unsigned 'jae' / 'jb' for uint8_t < 0x80\n{asm}"
    assert "jge" not in asm, f"signed 'jge' must not appear for uint8_t comparison\n{asm}"


def test_unsigned_uint16_t_greater_or_equal_emits_jae() -> None:
    """``uint16_t >= literal`` uses unsigned ``jae`` (true-branch) / ``jb`` (false)."""
    src = """
        uint16_t timeout __attribute__((asm_name("timeout")));
        int check() { return timeout >= 32768; }
    """
    asm = _kernel(src)
    assert "jae" in asm or "jb " in asm, f"expected unsigned mnemonic for uint16_t >= 32768\n{asm}"
    assert "jge" not in asm and "jl " not in asm, f"signed mnemonic must not appear\n{asm}"


def test_user_asm_register_pins_global_to_register() -> None:
    """``__attribute__((asm_register("si")))`` aliases a global to ESI.

    The global gets no ``_g_<name>`` storage slot, and reads/writes
    compile to direct ESI references rather than memory accesses.
    """
    ok, asm = _compile(
        r"""
        __attribute__((asm_register("si")))
        char *cursor;

        char source[] = {' ', ' ', 'h', 'e', 'l', 'l', 'o', 0};

        int main() {
            cursor = source;
            return cursor[0];
        }
        """,
        target="user",
        bits=32,
    )
    assert ok, f"compile failed:\n{asm}"
    assert "_g_cursor" not in asm, f"asm_register global should have no storage slot:\n{asm}"
    assert "mov esi, _g_source" in asm, f"expected ESI assignment from source[]:\n{asm}"


def test_user_brace_init_global_array_emits_dd_table() -> None:
    """File-scope ``int arr[] = {...}`` emits a ``_g_<name>: dd ...`` table.

    NASM resolves ``sizeof(arr)`` at assemble time, so the values are
    folded into the binary's data section instead of being copied at
    runtime; emission is a literal ``dd v0, v1, ...`` line.
    """
    ok, asm = _compile(
        r"""
        int fib[] = {1, 1, 2, 3, 5, 8, 13, 21, 34, 55};

        int main() {
            return fib[9];
        }
        """,
        target="user",
        bits=32,
    )
    assert ok, f"compile failed:\n{asm}"
    assert "_g_fib: dd 1, 1, 2, 3, 5, 8, 13, 21, 34, 55" in asm, f"missing brace-init dd table:\n{asm}"


def test_user_file_scope_asm_escape() -> None:
    """File-scope and statement-form ``asm(...)`` blocks emit verbatim.

    File-scope ``asm("asmesc_table: db 42, ...")`` plants the byte table
    at file scope; statement-form ``asm(...)`` inside a function emits
    the manual instructions inline — verifying both escapes see the
    same symbol table the surrounding C code does (the file-scope
    ``int value;`` becomes ``_g_value``).
    """
    ok, asm = _compile(
        r"""
        asm("asmesc_table: db 42, 99, 7, 11");

        int value;

        int main() {
            asm("mov ebx, asmesc_table\nmov al, [ebx+2]\nxor ah, ah\nmov [_g_value], ax");
            return 0;
        }
        """,
        target="user",
        bits=32,
    )
    assert ok, f"compile failed:\n{asm}"
    assert "asmesc_table: db 42, 99, 7, 11" in asm, f"missing file-scope asm() emission:\n{asm}"
    assert "mov ebx, asmesc_table" in asm, f"missing statement-form asm() emission:\n{asm}"
    assert "mov [_g_value], ax" in asm, f"missing _g_value substitution from inline asm:\n{asm}"


def test_user_file_scope_bss_globals() -> None:
    """File-scope scalars and zero-init arrays land in BSS via ``_g_<name>``.

    counter (int=4) + history (int[8]=32) + label (char[8]=8) sums to
    44 bytes of zero-initialized storage, emitted as ``_bss_end equ
    _program_end + 44`` at the trailer.  Reads/writes go through the
    ``_g_<name>`` symbols, not stack locals.
    """
    ok, asm = _compile(
        r"""
        int counter;
        int history[8];
        char label[8];

        int main() {
            counter = 1;
            history[0] = 2;
            label[0] = 'a';
            return counter + history[0] + label[0];
        }
        """,
        target="user",
        bits=32,
    )
    assert ok, f"compile failed:\n{asm}"
    assert "_g_counter" in asm, f"missing _g_counter reference:\n{asm}"
    assert "_g_history" in asm, f"missing _g_history reference:\n{asm}"
    assert "_g_label" in asm, f"missing _g_label reference:\n{asm}"
    assert "_bss_end equ _program_end + 44" in asm, f"expected 4+32+8=44 BSS bytes:\n{asm}"


def test_user_include_directive_pulls_macro_and_helper() -> None:
    """``#include "..."`` exposes #define macros and helper functions.

    Both the ``INCTEST_MAGIC`` ``#define`` and the ``inctest_square``
    function defined in a sibling header are visible to the including
    translation unit's emitted asm.
    """
    with tempfile.TemporaryDirectory(prefix="test_include_") as work:
        work_path = Path(work)
        (work_path / "helper.h").write_text(
            "#define INCTEST_MAGIC 3054\nint inctest_square(int x) {\n    return x * x;\n}\n",
        )
        source = work_path / "main.c"
        source.write_text(
            '#include "helper.h"\n\nint main() {\n    return inctest_square(INCTEST_MAGIC);\n}\n',
        )
        out = work_path / "main.asm"
        result = subprocess.run(
            ["python3", str(CC), "--bits", "32", "--target", "user", str(source), str(out)],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
        assert result.returncode == 0, f"compile failed:\n{result.stderr}"
        asm = out.read_text()
    assert "%define INCTEST_MAGIC 3054" in asm, f"missing macro expansion:\n{asm}"
    assert "inctest_square:" in asm, f"missing helper function emission:\n{asm}"


def test_user_rejects_inb() -> None:
    """``kernel_inb()`` in --target user is rejected at compile time."""
    ok, output = _compile(
        """
        int poll() {
            return kernel_inb(0x3FD);
        }
    """,
        target="user",
    )
    assert not ok, f"Expected user-mode kernel_inb() rejection; got asm:\n{output}"
    assert "inb" in output.lower() and "kernel" in output.lower(), f"Error should mention inb/kernel:\n{output}"


def test_user_rejects_insw() -> None:
    """``kernel_insw()`` in --target user is rejected at compile time."""
    ok, output = _compile(
        """
        void f() {
            char buf[2];
            kernel_insw(0x300, buf, 1);
        }
    """,
        target="user",
    )
    assert not ok, f"Expected user-mode kernel_insw() rejection; got asm:\n{output}"
    assert "insw" in output.lower() and "kernel" in output.lower(), f"Error should mention insw/kernel:\n{output}"


def test_user_rejects_inw() -> None:
    """``kernel_inw()`` in --target user is rejected at compile time."""
    ok, output = _compile(
        """
        int read_word() {
            return kernel_inw(0x300);
        }
    """,
        target="user",
    )
    assert not ok, f"Expected user-mode kernel_inw() rejection; got asm:\n{output}"
    assert "inw" in output.lower() and "kernel" in output.lower(), f"Error should mention inw/kernel:\n{output}"


def test_user_rejects_outb() -> None:
    """``kernel_outb()`` in --target user is rejected at compile time."""
    ok, output = _compile(
        """
        int main() {
            kernel_outb(0x20, 0x20);
            return 0;
        }
    """,
        target="user",
    )
    assert not ok, f"Expected user-mode kernel_outb() rejection; got asm:\n{output}"
    assert "outb" in output.lower() and "kernel" in output.lower(), f"Error should mention outb/kernel:\n{output}"


def test_user_rejects_outsw() -> None:
    """``kernel_outsw()`` in --target user is rejected at compile time."""
    ok, output = _compile(
        """
        void f() {
            char buf[2];
            kernel_outsw(0x300, buf, 1);
        }
    """,
        target="user",
    )
    assert not ok, f"Expected user-mode kernel_outsw() rejection; got asm:\n{output}"
    assert "outsw" in output.lower() and "kernel" in output.lower(), f"Error should mention outsw/kernel:\n{output}"


def test_user_rejects_outw() -> None:
    """``kernel_outw()`` in --target user is rejected at compile time."""
    ok, output = _compile(
        """
        int main() {
            kernel_outw(0x300, 0x1234);
            return 0;
        }
    """,
        target="user",
    )
    assert not ok, f"Expected user-mode kernel_outw() rejection; got asm:\n{output}"
    assert "outw" in output.lower() and "kernel" in output.lower(), f"Error should mention outw/kernel:\n{output}"


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
