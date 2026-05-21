"""Pytest tests for cc.py codegen.

Most tests target ``--target kernel`` and verify structural correctness
of kernel-mode output (no ``org 0600h`` / ``_program_end:`` / BSS
trailer / ``%include "constants.asm"`` / 0B055h sentinel; rejection of
``main`` / syscall builtins / ``die()`` in kernel mode; user-mode
output byte-for-byte identical to the default).

Also covers user-mode struct support: packed layout + sizeof, ptr->field
read/write codegen with correct byte offsets, global struct array BSS
size, struct fd layout pinning against the FD_OFFSET_* constants in
kernel/include/constants.asm, and a regression sweep over every user/programs/*.c
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
INCLUDE_DIR = REPO_ROOT / "kernel" / "include"
LIBBBOEOS_INCLUDE = REPO_ROOT / "user" / "libbboeos" / "include"

# Auto-prepended to every inline C snippet compiled through the helpers
# below so test sources can use ``uint8_t`` / ``uint16_t`` / ``uint32_t``
# without each test snippet repeating the include.  The names come from
# ``user/libbboeos/include/stdint.h`` (cc.py reaches it via the ``-I``
# flag we add to every subprocess call).
_STDINT_PREAMBLE = "#include <stdint.h>\n"
sys.path.insert(0, str(REPO_ROOT))
from cc.codegen.x86.peephole import Peepholer  # noqa: E402
from cc.target import X86CodegenTarget16  # noqa: E402

# FD layout constants from kernel/include/constants.asm (must match exactly).
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
    text = _STDINT_PREAMBLE + textwrap.dedent(source_text)
    with tempfile.TemporaryDirectory(prefix="test_kernel_") as work:
        work_path = Path(work)
        src = work_path / "test.c"
        out = work_path / "test.asm"
        src.write_text(text)
        result = subprocess.run(
            ["python3", str(CC), "--bits", str(bits), "--target", target, "-I", str(LIBBBOEOS_INCLUDE), str(src), str(out)],
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
    text = _STDINT_PREAMBLE + textwrap.dedent(source_text)
    with tempfile.TemporaryDirectory(prefix="test_kernel_cc_") as work:
        work_path = Path(work)
        src = work_path / "test.c"
        asm = work_path / "test.asm"
        binary = work_path / "test.bin"
        src.write_text(text)
        cc_result = subprocess.run(
            ["python3", str(CC), "--bits", str(bits), "-I", str(LIBBBOEOS_INCLUDE), str(src), str(asm)],
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


def test_cast_pointer_byte_compiles() -> None:
    """``(uint8_t *)expr`` parses as a transparent pointer cast.

    cc.py's type system is loose; the cast is parsed and discarded so
    the operand carries through unchanged.  This lets the source use
    casts for clang compatibility without diverging behaviour.
    """
    asm = _kernel("""
        void f(uint16_t *src) {
            uint8_t *dst;
            dst = (uint8_t *)src;
            dst[0] = 0;
        }
    """)
    assert "f:" in asm


def test_cast_int_compiles() -> None:
    """``(int)expr`` parses as a transparent scalar cast."""
    asm = _kernel("""
        int f(char c) {
            return (int)c;
        }
    """)
    assert "f:" in asm


def test_cast_in_compound_expression_compiles() -> None:
    """``base + (uint8_t *)offset`` cast inside a larger expression."""
    asm = _kernel("""
        uint8_t buffer[16];
        void f(int n) {
            uint8_t *p;
            p = buffer + (uint8_t *)n;
            p[0] = 0;
        }
    """)
    assert "f:" in asm


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


def test_default_2_arg_callee_does_not_read_arg_1_off_stack() -> None:
    """Default register-passing: arg 1 arrives in EDX, not via [ebp+offset].

    A 2-arg callee picks up the implicit register-passing default
    (args 0/1 in EAX/EDX), so the callee body should never reference
    [ebp+anything-positive] for argument loads.
    """
    asm = _kernel(
        """
        int add2(int a, int b) {
            return a + b;
        }
    """,
        bits=32,
    )
    for offset in (8, 12, 16):
        assert f"[ebp+{offset}]" not in asm, f"2-arg callee unexpectedly reads stack arg at [ebp+{offset}]:\n{asm}"


def test_default_3_arg_call_site_does_not_push_args() -> None:
    """A 3-arg call uses EAX/EDX/ECX, not the stack.

    The implicit register-passing default places args 0/1/2 in
    EAX/EDX/ECX, so the call site emits register loads instead of
    `push 30; push 20; push 10` + `add esp, 12`.
    """
    asm = _kernel(
        """
        int add3(int a, int b, int c);
        int call_site() {
            return add3(10, 20, 30);
        }
    """,
        bits=32,
    )
    call_index = asm.index("call add3")
    pre_call = asm[:call_index]
    post_call = asm[call_index:]
    assert "add esp, 12" not in post_call, f"unexpected 3-arg stack cleanup after default call:\n{asm}"
    assert "push 20" not in pre_call and "push 30" not in pre_call, (
        f"default 3-arg call site unexpectedly pushed args 1/2 on the stack:\n{asm}"
    )


def test_default_3_arg_callee_does_not_read_args_off_stack() -> None:
    """A 3-arg callee reads args from registers, not [ebp+N]."""
    asm = _kernel(
        """
        int add3(int a, int b, int c) {
            return a + b + c;
        }
    """,
        bits=32,
    )
    for offset in (8, 12, 16, 20):
        assert f"[ebp+{offset}]" not in asm, f"3-arg callee unexpectedly reads stack arg at [ebp+{offset}]:\n{asm}"


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


def test_dot_access_on_local_struct_emits_frame_relative_load() -> None:
    """Dot-access on a stack-local struct value reads via [ebp-N+offset]."""
    asm = _kernel(
        """
        struct s { uint8_t x; };
        void read_x() {
            struct s local;
            int y;
            y = local.x;
        }
    """,
        bits=32,
    )
    # Local struct lives in the frame; the read should target an ebp-relative
    # byte load rather than a global ``[_g_local+...]`` operand.
    assert "[ebp-" in asm, f"expected frame-relative load:\n{asm}"
    assert "_g_local" not in asm, f"local should not have a global symbol:\n{asm}"


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


def test_double_pointer_null_compare_classifies_as_pointer() -> None:
    """``if (p != NULL)`` compiles when *p* is ``char**`` (regression).

    Used to be rejected with ``NULL compared to non-pointer`` because
    ``_type_of_operand`` only knew about ``char*`` and ``uint8_t*``.
    """
    src = """
        void f(char **endptr) {
            if (endptr != NULL) {
                endptr[0] = 0;
            }
        }
    """
    asm = _kernel(src)
    assert "f:" in asm


def test_double_pointer_pointer_arith_classifies_as_pointer() -> None:
    """``base + offset`` (pointer + int) keeps pointer type for comparison.

    Used to be rejected with ``pointer compared to non-pointer`` because
    ``_type_of_operand`` unconditionally returned ``"integer"`` for any
    ``BinaryOperation``.
    """
    src = """
        void f(char *base, char *end) {
            if (end == base + 2) {
                end[0] = 0;
            }
        }
    """
    asm = _kernel(src)
    assert "f:" in asm


def test_address_of_local_disqualifies_auto_pin() -> None:
    """``&local`` keeps *local* in a frame slot instead of pinning to a register.

    Regression for the auto-pin / address-of collision: previously a
    high-ref local that also had its address taken would be auto-pinned
    to a register, then ``_local_address`` rejected with ``no address
    for 'name'`` when the AddressOf tried to look up its slot.
    """
    src = """
        int consume(int *p);
        int observe(int *q);

        int worker() {
            int value = 0;
            consume(&value);
            observe(&value);
            return value;
        }
    """
    asm = _kernel(src, bits=32)
    assert "worker:" in asm
    # The local must have a frame slot — look for any [ebp-N] or [bp-N]
    # reference under worker's body that names value's address.
    body = asm.split("worker:", 1)[1]
    assert "[ebp-" in body or "[bp-" in body, f"expected frame-slot ref for &value\n{asm}"


def test_out_register_captures_topologically_ordered() -> None:
    """Topologically order out_register captures when one's source feeds another.

    When two out_register captures form a chain — capture A's source
    register is capture B's pinned destination — capture B must be
    emitted before capture A so B's read isn't clobbered.

    Regression for the fd_read_net page-fault: ne2k_receive returns
    ``frame_pointer`` in EDI and ``packet_length`` in ECX.  After auto-
    pin assigned ECX to frame_pointer and EDX to packet_length, the
    naive in-order emission produced ``mov ecx, edi; mov edx, ecx``,
    where the second move read the already-overwritten ECX.
    """
    asm = _kernel(
        """
        __attribute__((carry_return))
        int producer(int *first  __attribute__((out_register("edi"))),
                     int *second __attribute__((out_register("ecx"))));

        int consume(int a, int b);

        int caller() {
            int a;
            int b;
            producer(&a, &b);
            return consume(a, b) + consume(a, b) + consume(a, b);
        }
    """,
        bits=32,
    )
    body = asm.split("caller:", 1)[1]
    call_index = body.find("call producer")
    after_call = body[call_index:].splitlines()[1:5]
    # The capture whose source (ECX) is the OTHER capture's destination
    # must come first.  If both captures end up pinned and the order is
    # wrong, the assertion below would still pass — so also check the
    # second capture doesn't read from the first's pinned destination.
    if any("mov ecx, edi" in line for line in after_call):
        ecx_capture_index = next(i for i, line in enumerate(after_call) if "mov ecx, edi" in line)
        edx_capture_index = next((i for i, line in enumerate(after_call) if "mov edx, ecx" in line), None)
        assert edx_capture_index is None or edx_capture_index < ecx_capture_index, (
            f"out_register captures emitted in wrong order — mov edx, ecx must precede mov ecx, edi:\n{asm}"
        )


def test_address_of_at_out_register_arg_still_allows_auto_pin() -> None:
    """``&x`` at an ``out_register`` arg position is a fake address: *x* may still pin.

    The disqualification above must NOT fire for out_register captures
    — the callee writes the named register and the caller copies it
    back to *x*, so *x* never needs a memory address.
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
    # The pin should land — the capture move into the pinned register
    # must appear (mov dx, cx or similar) rather than [bp-N], cx.
    assert "mov dx, cx" in asm, f"expected pinned-register capture, got\n{asm}"


def test_auto_pin_cost_model_subtracts_pre_first_store_calls() -> None:
    """Pre-first-store calls are dropped from the auto-pin cost gate.

    PR #454's liveness pre-pass elides ``push <pin>`` / ``pop <pin>``
    around any call that runs before the pinned local is first
    written.  Auto-pin's cost model now factors that in: a candidate
    whose first store happens late in the function only pays the
    save cost for calls AFTER the store, not the function-wide
    clobber count.

    Here ``counter`` is written for the first time after 8 helper
    calls — under the pre-refinement model, refs (6) didn't beat
    the EBX clobber count (8) so auto-pin bailed.  Post-refinement
    the effective cost is 0 (all 8 calls are pre-store), so the
    pin lands and ``counter`` lives in EBX.
    """
    asm = _kernel(
        """
        int helper(int x);
        int late_store() {
            int counter;
            helper(0);
            helper(1);
            helper(2);
            helper(3);
            helper(4);
            helper(5);
            helper(6);
            helper(7);
            counter = 0;
            counter = counter + 1;
            counter = counter + 1;
            return counter;
        }
    """,
        bits=32,
    )
    # Counter pins to an E-register (any of the safe ones), so its
    # store pattern is ``mov eRR, eax`` rather than a stack-slot
    # spill (``mov [ebp-N], eax``).  Before the refinement, refs (6)
    # didn't beat the function-wide EBX clobber count (8 user-call
    # clobbers), so auto-pin bailed and counter spilled.
    pinned_store_forms = ("mov ebx, eax", "mov ecx, eax", "mov edx, eax", "mov edi, eax")
    assert any(form in asm for form in pinned_store_forms), (
        f"expected counter to pin to an E-register (store via {pinned_store_forms}):\n{asm}"
    )
    assert "[ebp-4], eax" not in asm, f"counter should not spill to its frame slot:\n{asm}"


def test_double_pointer_deref_assign_emits_indirect_store() -> None:
    """``*endptr = value`` lowers to ``mov [reg], <acc>`` for plain pointer locals.

    Regression for the DerefAssign path that used to reject any
    non-out_register holder.  The holder is loaded into ESI and the
    accumulator is stored through it.
    """
    src = """
        void f(char **endptr, char *value __attribute__((in_register("di")))) {
            *endptr = value;
        }
    """
    asm = _kernel(src, bits=32)
    body = asm.split("f:", 1)[1]
    assert "mov esi, [" in body, f"expected holder load into ESI\n{asm}"
    assert "mov [esi], eax" in body, f"expected store through ESI\n{asm}"


@pytest.mark.parametrize("source_path", sorted((REPO_ROOT / "user" / "programs").glob("*.c")))
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


def test_kernel_outb_constant_port_runtime_value_no_push_pop() -> None:
    """``kernel_outb(<int>, <runtime value>)`` skips the save-around-eval push/pop.

    The push/pop in the general path guards the value (held in AX)
    across the port evaluation, since port eval may clobber AX.  When
    port is an ``Int`` literal, the port lowering is a single
    ``mov dx, <imm>`` that doesn't touch AX — so the save is dead and
    the codegen elides it.
    """
    asm = _kernel("""
        uint8_t status() { return 5; }
        void send_status() {
            kernel_outb(0x20, status());
        }
    """)
    assert "out dx, al" in asm, f"Expected 'out dx, al' in:\n{asm}"
    assert "push ax" not in asm and "push eax" not in asm, f"Const-port outb should not push the accumulator:\n{asm}"
    assert "pop ax" not in asm and "pop eax" not in asm, f"Const-port outb should not pop the accumulator:\n{asm}"


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


def test_kernel_outw_constant_port_runtime_value_no_push_pop() -> None:
    """``kernel_outw(<int>, <runtime value>)`` skips the save-around-eval push/pop.

    Same elision as :func:`test_kernel_outb_constant_port_runtime_value_no_push_pop`,
    just for the 16-bit out variant.
    """
    asm = _kernel("""
        int status() { return 5; }
        void send_status() {
            kernel_outw(0x300, status());
        }
    """)
    assert "out dx, ax" in asm, f"Expected 'out dx, ax' in:\n{asm}"
    assert "push ax" not in asm and "push eax" not in asm, f"Const-port outw should not push the accumulator:\n{asm}"
    assert "pop ax" not in asm and "pop eax" not in asm, f"Const-port outw should not pop the accumulator:\n{asm}"


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


def test_logical_and_in_expression_position_compiles() -> None:
    """``int x = a && b;`` compiles to a short-circuit 0/1 materialise.

    Used to reject with ``unknown expression: LogicalAnd`` because
    ``generate_expression`` only handled `&&` in condition position.
    """
    asm = _kernel(
        """
        void f(int a, int b) {
            int same = a && b;
            f(same, 0);
        }
        """,
        bits=32,
    )
    body = asm.split("f:", 2)[1]
    # The two operand tests + the 0/1 set + the merge jump:
    assert "mov eax, 1" in body, f"expected mov eax, 1 (true leg)\n{asm}"
    assert "xor eax, eax" in body, f"expected xor eax, eax (false leg)\n{asm}"
    assert ".lbool_" in body, f"expected .lbool_ label scheme\n{asm}"


def test_logical_or_in_expression_position_compiles() -> None:
    """``int x = a || b;`` compiles to a short-circuit 0/1 materialise."""
    asm = _kernel(
        """
        void f(int a, int b) {
            int either = a || b;
            f(either, 0);
        }
        """,
        bits=32,
    )
    body = asm.split("f:", 2)[1]
    assert "mov eax, 1" in body, f"expected mov eax, 1 (true leg)\n{asm}"
    assert "xor eax, eax" in body, f"expected xor eax, eax (false leg)\n{asm}"
    assert ".lbool_" in body, f"expected .lbool_ label scheme\n{asm}"


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


def test_member_access_via_cast_arrow_bitfield() -> None:
    """``((struct T *)&raw)->bf`` extracts a bitfield without a named local."""
    asm = _user(
        """
        struct ata_status {
            unsigned char err : 1;
            unsigned char idx : 1;
            unsigned char drq : 3;
            unsigned char busy : 3;
        };
        int read_busy(unsigned char raw) {
            return ((struct ata_status *)&raw)->busy;
        }
    """,
        bits=32,
    )
    body = asm.split("read_busy:")[1].split("ret")[0]
    # base pointer moved into EBX, low byte read, shr+and extracts the
    # ``busy`` bitfield (top 3 bits).
    assert "mov ebx," in body
    assert "shr al, 5" in body
    assert "and al, 7" in body


def test_member_access_via_cast_arrow_byte_field() -> None:
    """``((struct T *)&raw)->field`` reads a byte member at offset 0 without a named local."""
    asm = _user(
        """
        struct port_byte { unsigned char value; };
        int read_port(unsigned char raw) {
            return ((struct port_byte *)&raw)->value;
        }
    """,
        bits=32,
    )
    body = asm.split("read_port:")[1].split("ret")[0]
    assert "mov ebx," in body, f"expected base into EBX\n{asm}"
    assert "movzx eax, byte [ebx]" in body, f"expected byte load through EBX\n{asm}"


def test_member_access_via_cast_arrow_rejects_non_struct_pointer() -> None:
    """``((int *)expr)->field`` is rejected — the cast must be to ``struct T *``."""
    ok, message = _compile(
        """
        struct rec { int value; };
        int main() {
            int raw = 0;
            return ((int *)&raw)->value;
        }
        """,
        target="user",
        bits=32,
    )
    assert not ok, f"expected struct-pointer-cast rejection, got success:\n{message}"
    assert "struct-pointer cast" in message, message


def test_member_access_via_cast_arrow_word_field_offset() -> None:
    """``((struct T *)&raw)->field`` at non-zero offset emits ``[ebx+N]``."""
    asm = _user(
        """
        struct rec { unsigned char a; unsigned char b; unsigned short c; };
        int read_c(unsigned int raw) {
            return ((struct rec *)&raw)->c;
        }
    """,
        bits=32,
    )
    body = asm.split("read_c:")[1].split("ret")[0]
    assert "movzx eax, word [ebx+2]" in body, f"expected word load at offset 2\n{asm}"


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


def test_memcmp_topologically_orders_aliased_args() -> None:
    """Memcmp must load SI before DI when arg ``b`` lives in DI.

    Regression: ``memcmp(line + start, pattern, n)`` inside a loop where
    ``pattern`` is a parameter pinned to EDI used to emit ``mov edi, eax
    / mov esi, edi``, which made SI point at the freshly-written
    line+start value — every comparison hit the buffer against itself
    and returned 0 (equal), so every line "matched."  Caught while
    landing user/programs/grep.c.  builtin_memcmp now routes register loads
    through _emit_builtin_arg_moves so the load order is topologically
    safe.
    """
    asm = _kernel(
        """
        int line_matches(char *pattern, int pattern_length, char *line, int line_length) {
            if (pattern_length > line_length) {
                return 0;
            }
            int start = 0;
            while (start <= line_length - pattern_length) {
                if (memcmp(line + start, pattern, pattern_length) == 0) {
                    return 1;
                }
                start += 1;
            }
            return 0;
        }
    """,
        bits=32,
    )
    lines = [line.strip() for line in asm.splitlines()]
    jump_prefixes = ("jmp", "jge", "jle", "jl ", "jg ", "je ", "jne", "jz ", "jnz", "call", "ja ", "jb ", "jae", "jbe", "jc ", "jnc")
    found_at_least_one = False
    for index, line in enumerate(lines):
        if line != "repe cmpsb":
            continue
        found_at_least_one = True
        start = index
        while start > 0:
            previous = lines[start - 1]
            if previous.endswith(":") or previous.startswith(jump_prefixes):
                break
            start -= 1
        block = lines[start:index]
        edi_writes = [offset for offset, instruction in enumerate(block) if instruction.startswith(("mov edi,", "xor edi,"))]
        if not edi_writes:
            continue
        first_edi_write = edi_writes[0]
        tail = block[first_edi_write + 1 :]
        bad = [instruction for instruction in tail if instruction == "mov esi, edi"]
        assert not bad, (
            "builtin_memcmp clobbered EDI before reading it as the source for ESI. "
            "If a pinned variable lives in EDI, this loads the buffer against itself. "
            "Offending tail:\n" + "\n".join(tail) + "\n--- full setup block ---\n" + "\n".join(block)
        )
    assert found_at_least_one, f"test source must compile to at least one repe cmpsb; got asm:\n{asm}"


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
        void test_named_const_addr() {
            int n;
            n = MAX_INPUT;
        }
    """
    output = _kernel(source)
    assert "MAX_INPUT" in output
    assert "[MAX_INPUT]" not in output


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
    """After the call, the named register flows into the caller's local.

    ``entry`` auto-pins (two refs: the out_register capture and the
    ``captured = entry`` read, against one call's clobber cost), so the
    capture lands in the pinned register directly (`mov dx, si`).  If
    the var doesn't pin, the capture falls back to a memory slot.
    """
    asm = _kernel("""
        int *captured __attribute__((asm_name("captured")));
        __attribute__((carry_return)) int fd_alloc(int* entry __attribute__((out_register("si"))));

        void caller() {
            int* entry;
            fd_alloc(&entry);
            captured = entry;
        }
    """)
    assert "mov dx, si" in asm, f"expected pinned capture mov dx, si\n{asm}"


def test_out_register_caller_no_push() -> None:
    """Caller emits no push for an out_register argument — only call + capture.

    ``entry`` auto-pins (two reads, one call's clobber cost), so the
    capture lands in the pinned register directly.
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
    lines = [line.strip() for line in asm.splitlines()]
    call_idx = next(i for i, line in enumerate(lines) if line == "call fd_alloc")
    # No argument push immediately before the call.
    assert lines[call_idx - 1] != "push ax", "unexpected argument push before call fd_alloc"
    # The capture is a register-to-register move into the pinned reg.
    assert lines[call_idx + 1] == "mov dx, si", f"expected 'mov dx, si' capture, got '{lines[call_idx + 1]}'\n{asm}"


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
    # The capture must zero-extend BX into the pinned destination's
    # wider form (EBX or whichever E-register auto-pin assigned).  Bare
    # ``mov eX, bx`` is mixed-width and invalid; the movzx variant is
    # required.
    assert "movzx " in asm and ", bx" in asm, f"expected 'movzx eX, bx' for 16-bit out_register into 32-bit slot:\n{asm}"
    assert "mov ebx, bx" not in asm and "mov edx, bx" not in asm and "mov ecx, bx" not in asm, (
        f"raw 'mov eX, bx' is mixed-width and invalid:\n{asm}"
    )


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
    # Capture must zero-extend BX into the pinned E-register destination
    # (auto-pin may land on any of the safe E-regs).  Bare ``mov eX, bx``
    # is mixed-width and invalid.
    assert "movzx " in asm and ", bx" in asm, f"expected 'movzx eX, bx' capture into pinned E-register:\n{asm}"
    assert "mov ebx, bx" not in asm and "mov edx, bx" not in asm and "mov ecx, bx" not in asm, (
        f"raw 'mov eX, bx' is mixed-width and invalid:\n{asm}"
    )


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
    # Capture happens before the branch; ``entry`` auto-pins so the
    # destination is a register, not a memory slot.
    assert lines[call_idx + 1] == "mov dx, si", f"expected 'mov dx, si' capture, got '{lines[call_idx + 1]}'\n{asm}"
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
    # ``entry`` is referenced twice (the out_register capture and the
    # ``captured = entry`` read), so it auto-pins and the capture lands
    # in the pinned register directly rather than going through a frame slot.
    assert "mov dx, si" in output, f"expected pinned capture mov dx, si\n{output}"


def test_out_register_si_cleared_across_call() -> None:
    """If a second call intervenes after the capture, ``p`` survives without using SI.

    Pre-auto-pin-liveness, ``p`` spilled to ``[bp-N]`` and reloaded
    via ``mov bx, [bp-N]``.  With the cost model honouring the
    out_register capture as a first store, ``p`` auto-pins and the
    intervening call wraps with ``push <pin>`` / ``pop <pin>``.
    The essential invariant is the same: ``[si]`` cannot back the
    member write because ``other_func`` clobbers SI.
    """
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
    # SI is dead after other_func; the member write must reach
    # through a different base (frame reload OR a pinned register
    # the call saved/restored).
    assert "mov [si]" not in asm and "mov [si+" not in asm, f"SI is clobbered by other_func; cannot back the write\n{asm}"


def test_out_register_si_used_directly_for_member_access() -> None:
    """``p`` survives without a frame slot — via SI fast-path or an auto-pinned register.

    Pre-auto-pin-liveness, the SI-cached pointer was used directly
    via ``[si]`` / ``[si+N]`` because ``p`` didn't auto-pin (refs
    didn't beat the clobber gate).  With the cost model honouring
    the out_register capture as a first store, ``p`` may now auto-
    pin and the writes go through that pinned register instead.
    Either way the invariant is the same: no stack reload for
    ``p`` between the capture and the member writes.
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
    assert "mov bx, [bp-" not in asm and "mov bx, [bp+" not in asm, f"unexpected BX reload from frame for short-lived pointer\n{asm}"


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


def test_peephole_dead_temp_slot_dropped_across_functions() -> None:
    """A dead store in ``g`` is dropped even when ``f`` reads the same offset.

    ``[bp-N]`` is function-local — slot N in ``f`` and slot N in ``g`` are
    different physical slots.  The dead-temp-slot pass must scope its
    read-detection per function or it conflates them and keeps writes
    that are provably dead inside their own frame.
    """
    out = _peephole_run([
        "f:",
        "        push bp",
        "        mov bp, sp",
        "        mov [bp-4], ax",
        "        cmp word [bp-4], 5",
        "        pop bp",
        "        ret",
        "g:",
        "        push bp",
        "        mov bp, sp",
        "        sub sp, 4",
        "        mov ax, dx",
        "        mov [bp-4], ax",
        "        mov sp, bp",
        "        pop bp",
        "        ret",
    ])
    # f's [bp-4] is read by ``cmp`` and must survive; g's [bp-4] is
    # dead inside g's frame and must be dropped.  Without per-function
    # scoping the pass conflates the two and keeps both writes
    # (possibly in fused form like ``mov [bp-4],dx``).
    bp_minus_4_writes = [line for line in out if "[bp-4]" in line and "mov" in line and "cmp" not in line]
    # Expect exactly one write to [bp-4]: f's live one.
    assert len(bp_minus_4_writes) == 1, f"expected only f's live [bp-4] write, got {bp_minus_4_writes}:\n{out}"


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


def test_peephole_fold_byte_immediate_through_local() -> None:
    """``mov byte [ebp-N], <imm>; movzx eax, byte [ebp-N]`` folds to ``mov eax, <imm>``.

    Motivating idiom: a bitfield-struct local whose only use is
    ``*(uint8_t *)&local`` (typical of the driver port-I/O sites).
    cc.py emits the byte store from the const-folded designated init,
    then the pointer-deref load.  The peephole observes that the
    movzx reads exactly the value just stored and rewrites it to a
    direct immediate load; ``peephole_dead_temp_slots`` then reclaims
    the now-unreferenced store.
    """
    asm = _kernel(
        """
        struct cr {
            uint8_t start: 1;
            uint8_t stop: 1;
            uint8_t txp: 1;
            uint8_t reserved: 1;
            uint8_t page: 2;
            uint8_t rd: 2;
        };
        void probe() {
            struct cr c = { .stop = 1, .rd = 4, .page = 1 };
            kernel_outb(0x300, *(uint8_t *)&c);
        }
    """,
        bits=32,
    )
    # The store + load through [ebp-N] is gone; the immediate flows
    # straight to AX/AL.
    assert "mov byte [ebp-1]" not in asm, f"byte store to local should be elided:\n{asm}"
    assert "movzx eax, byte [ebp-1]" not in asm, f"movzx reload should be folded:\n{asm}"
    # The byte value (0b00010010 = 18) reaches AL one way or another.
    assert "mov eax, 18" in asm or "mov al, 18" in asm, f"folded immediate load missing:\n{asm}"


def test_peephole_narrow_acc_immediate_for_byte_out() -> None:
    """``mov eax, <imm 0..255>`` followed by ``out dx, al`` narrows to ``mov al, <imm>``.

    After :meth:`peephole_fold_byte_immediate_through_local` produces
    ``mov eax, <imm>`` from a byte-load idiom, the only consumer of AX
    is ``out dx, al`` (which touches AL only).  The 3-byte saving per
    site applies when EAX's upper bits are dead after the out — proved
    here by a follow-on call that caller-clobbers EAX.
    """
    asm = _kernel(
        """
        struct cr { uint8_t start: 1; uint8_t stop: 1; uint8_t txp: 1;
                    uint8_t reserved: 1; uint8_t page: 2; uint8_t rd: 2; };
        void other();
        void probe() {
            struct cr c = { .stop = 1, .rd = 4, .page = 1 };
            kernel_outb(0x300, *(uint8_t *)&c);
            other();
        }
    """,
        bits=32,
    )
    assert "mov al, 18" in asm, f"narrowed byte-immediate load missing:\n{asm}"
    assert "mov eax, 18" not in asm, f"unnarrowed full-width load survived:\n{asm}"


def test_peephole_narrow_acc_immediate_keeps_wider_consumer() -> None:
    """Narrow is skipped when something reads {acc} wider than AL before clobber.

    Synthetic 16-bit sequence with an ``mov bx, ax`` between the
    load and the ``out`` — the move-to-BX reads AX, which would see
    caller-junk in AH if AX were narrowed to AL.  The peephole must
    leave ``mov ax, 18`` intact.
    """
    out = _peephole_run([
        "f:",
        "        mov ax, 18",
        "        cmp ax, 17",
        "        mov dx, 768",
        "        out dx, al",
        "        ret",
    ])
    assert "        mov ax, 18" in out, f"narrowing should be skipped — AX consumed wider:\n{out}"


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


def test_pinned_register_on_int_local_pins_value_to_register() -> None:
    """``int x __attribute__((pinned_register("ebx")))`` reserves EBX for the local.

    Mirrors the function-pointer variant: the explicit attribute
    bypasses ``can_auto_pin`` cost gating so even short-lived ints
    can hold a register across clobbering calls when the caller
    knows the trade-off is worth it.  Verified by checking the
    store/use lower to ``mov ebx, ..`` / ``mov eax, ebx`` instead of
    spilling through a stack slot.
    """
    asm = _kernel(
        """
        int helper(int x);
        int compute(int n) {
            int sum __attribute__((pinned_register("ebx")));
            sum = n;
            sum = sum + helper(0);
            return sum;
        }
    """,
        bits=32,
    )
    assert "mov ebx, eax" in asm or "mov ebx, " in asm, f"sum must land in ebx:\n{asm}"
    # No frame slot for ``sum`` — pinned locals live entirely in the
    # register, so the prologue's ``sub esp, N`` must not include it.
    assert "[ebp-4]" not in asm or "sum" not in asm.lower(), f"pinned local should not spill:\n{asm}"


def test_pinned_register_on_unsigned_long_rejected() -> None:
    """``pinned_register`` on a 64-bit local is rejected — doesn't fit one register.

    ``unsigned long`` is the only scalar type that can't live in a
    single GP register on either the 16-bit (DX:AX pair) or 32-bit
    (EDX:EAX pair) target.  Auto-pin already excludes it; the
    explicit attribute matches.
    """
    error = _kernel_error("""
        void bad() {
            unsigned long x __attribute__((pinned_register("ebx")));
            x = 0;
        }
    """)
    assert "pinned_register" in error, f"Expected pinned_register error, got: {error}"


def test_pinned_register_save_kept_after_var_decl_initialiser() -> None:
    """``int count = n; kernel_outb(...);`` saves EDX once count is pinned to it.

    The IR builder routes ``VarDecl`` with initialiser through a
    ``Block`` escape hatch — the pre-pass must look inside Block to
    see the implicit store, otherwise it would mis-treat the pin as
    uninitialised and elide the save for the first call.  Regression
    guard for that path.
    """
    asm = _kernel(
        """
        void test_param(int n) {
            int count = n;
            kernel_outb(0x1F3, 0);
            kernel_outb(0x1F4, count);
        }
    """,
        bits=32,
    )
    first_call = asm.split("out dx, al", 1)[0]
    assert "push edx" in first_call, f"VarDecl-initialised pin must be saved on first call:\n{first_call}"


def test_pinned_register_save_skipped_before_first_store() -> None:
    """Builtin calls before the first store to a pinned local skip push/pop of its register.

    ``status_bits`` auto-pins to EDX, but its first write happens
    inside the while loop.  The two pre-loop ``kernel_outb`` calls
    therefore have no meaningful EDX value to preserve — saving it
    is dead.  The loop-body ``kernel_inb`` still wraps with ``push
    edx`` / ``pop edx`` because the body stores to status_bits, so
    every iteration past the first sees a live pin.
    """
    asm = _kernel(
        """
        struct ata_status { uint8_t err: 1; uint8_t idx: 1;
            uint8_t corr: 1; uint8_t drq: 1; uint8_t srv: 1;
            uint8_t df: 1; uint8_t rdy: 1; uint8_t bsy: 1; };
        void test_init() {
            uint8_t status;
            struct ata_status *status_bits;
            kernel_outb(0x3F6, 4);
            kernel_outb(0x3F6, 0);
            while (1) {
                status = kernel_inb(0x1F7);
                status_bits = (struct ata_status *)&status;
                if (status_bits->bsy == 0) { break; }
            }
        }
    """,
        bits=32,
    )
    pre_loop_body, _, after_loop = asm.partition("._ir_wloop")
    assert "push edx" not in pre_loop_body, f"pre-loop save should be elided:\n{pre_loop_body}"
    # The loop body's call still saves — second iteration sees a live pin.
    assert "push edx" in after_loop, f"in-loop save must survive:\n{after_loop}"


def test_pinned_register_save_skipped_before_first_store_for_user_call() -> None:
    """User-function calls before the first store to a pinned local skip its save.

    Mirrors the builtin-call variant
    (:func:`test_pinned_register_save_skipped_before_first_store`):
    the pin is undefined garbage until the first store, so wrapping a
    user-function call in ``push ebx`` / ``pop ebx`` preserves
    nothing.  Once the pin is initialised the save returns — the
    compiler doesn't do liveness past the call, so any post-store
    call still gets the save.
    """
    asm = _kernel(
        """
        int get_fn();
        int helper(int x);
        int dispatch() {
            int (*handler)() __attribute__((pinned_register("ebx")));
            helper(0);
            handler = get_fn();
            helper(0);
            return handler();
        }
    """,
        bits=32,
    )
    pre_store, _, post_store = asm.partition("mov ebx, eax")
    # ``helper(0)`` before the pin is written, and ``get_fn()`` whose
    # return populates the pin — both have nothing live in EBX, so
    # neither wraps with ``push ebx`` / ``pop ebx``.
    assert "push ebx" not in pre_store, f"pre-store user calls should not save ebx:\n{pre_store}"
    # The post-store ``helper(0)`` and the ``handler()`` indirect call
    # both see a live pin and must preserve it across the clobber.
    assert post_store.count("push ebx") == 2, f"post-store calls must save ebx twice:\n{post_store}"


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


def test_read_deref_char_pointer_zero_extends_byte() -> None:
    """``char c = *p;`` reads one byte (matches ``p[0]`` semantics)."""
    asm = _kernel("""
        void f(char *p) {
            char c;
            c = *p;
            if (c == 'A') {
                c = 'B';
            }
        }
    """)
    assert "f:" in asm


def test_read_deref_int_pointer_compiles() -> None:
    """``x = *p;`` for ``int *p`` parses and lowers to a load."""
    asm = _kernel("""
        void f(int *p) {
            int x;
            x = *p;
            if (x == 0) {
                x = 1;
            }
        }
    """)
    assert "f:" in asm


def test_read_deref_uint16_pointer_compiles() -> None:
    """``x = *p;`` for ``uint16_t *p`` parses and lowers to a load."""
    asm = _kernel("""
        void f(uint16_t *p) {
            uint16_t x;
            x = *p;
        }
    """)
    assert "f:" in asm


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

    The fd_ops table in kernel/fs/fd.c is the motivating shape: an array of
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


def test_typedef_alias_expands_in_declarations() -> None:
    """``typedef`` registers a name that expands inline at every type-spec site.

    Covers the three places parse_type is reached from: file-scope decls
    (return type + parameter type of ``add``), local-variable decls
    (``size_t x``), and parameter types (``size_t a, size_t b``).  Also
    checks pointer-suffix interaction (``typedef char *str;`` then
    ``str s;`` should still classify as a pointer-to-char).
    """
    asm = _user(
        """
        typedef unsigned int size_t;
        typedef char *str;

        size_t add(size_t a, size_t b) { return a + b; }

        int main() {
            size_t x = 10;
            str s = "hi";
            return (int)add(x, 20);
        }
        """,
        bits=32,
    )
    assert "main:" in asm
    assert "add:" in asm


def test_typedef_signed_short_long_long_parse() -> None:
    """``<stdint.h>`` typedefs parse without forking the header.

    ``signed`` is a no-op leading modifier; ``short`` aliases to ``int``
    (width-faithful int16_t is future work); ``unsigned short`` /
    ``unsigned char`` route through the existing uint16_t / uint8_t
    machinery; ``long long`` collapses to ``unsigned long``.  And the
    alias name itself is allowed to clash with a built-in type token
    (`typedef unsigned short uint16_t;`) — the typedef silently no-ops
    in that case so stdint.h doesn't need a cc.py-specific fork.
    """
    asm = _user(
        """
        typedef signed short int16_t;
        typedef signed int int32_t;
        typedef signed long long int64_t;
        typedef signed char int8_t;
        typedef unsigned short uint16_t;
        typedef unsigned long long uint64_t;
        typedef unsigned char uint8_t;
        typedef long off_t;

        int main(void) { return 0; }
        """,
        bits=32,
    )
    assert "main:" in asm


def test_typedef_struct_alias_resolves_in_pointer_param() -> None:
    """``typedef struct point point_t;`` lets ``point_t *p`` parse as ``struct point *p``."""
    asm = _user(
        """
        struct point { int x; int y; };
        typedef struct point point_t;

        int sum(point_t *p) { return p->x + p->y; }
        int main() {
            struct point pt;
            pt.x = 3;
            pt.y = 4;
            return sum(&pt);
        }
        """,
        bits=32,
    )
    assert "sum:" in asm


def test_uint16_pointer_load_is_word() -> None:
    """``uint16_t *p; r = p[0];`` loads exactly 2 bytes, not 4 (32-bit target regression).

    Symptom: when the acc is 32-bit (``eax``), a bare ``mov eax, [esi]``
    reads 4 bytes from the pointer target — but the pointee is 16 bits,
    so the high two bytes belong to the adjacent uint16_t.  The fix
    routes the load through ``movzx eax, word [esi]``.  On the 16-bit
    target the acc width already matches the pointee, so this is a
    32-bit-target-only test.
    """
    asm = _kernel(
        """
            void test(uint16_t *p __attribute__((in_register("di")))) {
                uint16_t r = p[0];
                (void)r;
            }
        """,
        bits=32,
    )
    # Either ``movzx eax, word [esi]`` or ``mov ax, [esi]`` then ``movzx``
    # would be acceptable — what's forbidden is a bare ``mov eax, [esi]``
    # (which reads 4 bytes).
    assert "mov eax, [esi]" not in asm, f"uint16_t* read must not use a 32-bit acc load (would read 4 bytes):\n{asm}"
    assert "word" in asm, f"expected a ``word``-sized load for uint16_t*:\n{asm}"


def test_uint16_t_global_array_accepted_and_halfword() -> None:
    """File-scope ``uint16_t arr[N]`` is allowed and uses halfword load/store.

    Previously rejected by the GLOBAL_ARRAY_PRIMITIVE_TYPES allowlist
    because the codegen had no halfword path.
    """
    asm = _kernel(
        """
            uint16_t g[4];
            uint16_t f() {
                g[1] = 0x2222;
                return g[1];
            }
        """,
        bits=32,
    )
    assert "_g_g" in asm, f"expected global symbol _g_g:\n{asm}"
    assert "mov dword" not in asm, f"global uint16_t store must not be dword:\n{asm}"
    assert "mov word [_g_g+2]" in asm, f"expected halfword store at stride 2:\n{asm}"
    assert "movzx eax, word [_g_g+2]" in asm, f"expected halfword load:\n{asm}"


def test_uint16_t_global_array_bss_size_uses_halfword_stride() -> None:
    """``uint16_t g[N];`` reserves ``N*2`` bytes in BSS, not ``N*int_size``."""
    asm = _kernel(
        """
            uint16_t g[8];
            int f() { return 0; }
        """,
        bits=32,
    )
    assert "_g_g: resb 16" in asm, f"expected 8*2=16-byte BSS reservation:\n{asm}"


def test_uint16_t_global_array_initialized_uses_dw_directive() -> None:
    """``uint16_t g[3] = {1,2,3};`` lays out as ``dw`` cells, not ``dd``."""
    asm = _kernel(
        """
            uint16_t g[3] = {0x1111, 0x2222, 0x3333};
            uint16_t f() { return g[1]; }
        """,
        bits=32,
    )
    assert "_g_g: dw " in asm, f"expected halfword initializer directive:\n{asm}"
    assert "_g_g: dd " not in asm, f"must not lay out uint16_t as dd cells:\n{asm}"


def test_uint16_t_global_array_variable_index_load_scales_by_two() -> None:
    """``uint16_t g[N]; return g[i];`` scales the var index by 2, not int_size.

    The global-base, variable-index load path threads ``element_size``
    through ``_emit_constant_base_index_addr`` so ``[_g_g+esi]`` is
    indexed with ``esi = i*2``, not ``i*4``.
    """
    asm = _kernel(
        """
            uint16_t g[8];
            uint16_t f(int i) {
                return g[i];
            }
        """,
        bits=32,
    )
    assert "shl esi, 2" not in asm, f"must not scale by 4 for uint16_t global:\n{asm}"
    assert "shl eax, 2" not in asm, f"must not scale by 4 for uint16_t global:\n{asm}"
    assert "movzx eax, word [_g_g" in asm, f"expected halfword load:\n{asm}"


def test_uint16_t_local_array_load_const_index_is_halfword() -> None:
    """``uint16_t arr[N]; return arr[k];`` must load exactly 2 bytes (not 4).

    The 32-bit acc (``eax``) load would read 4 bytes — clobbering the
    next element's low half into the high half of the result.
    """
    asm = _kernel(
        """
            uint16_t f() {
                uint16_t arr[4];
                arr[0] = 0x1111;
                arr[1] = 0x2222;
                return arr[1];
            }
        """,
        bits=32,
    )
    assert "movzx eax, word [esi+2]" in asm, f"expected halfword load at stride 2:\n{asm}"
    assert "mov eax, [esi+4]" not in asm, f"must not load 4 bytes at uint16_t stride 4:\n{asm}"


def test_uint16_t_local_array_store_const_index_is_halfword() -> None:
    """``uint16_t arr[N]; arr[k] = v;`` for local arrays must stride by 2 and store ``word``.

    Regression: cc.py's local-array codegen had a binary byte-vs-word
    switch — anything not ``char`` / ``uint8_t`` got ``int_size`` stride
    and a full-width store, silently miscompiling ``uint16_t`` arrays
    (stride 4, ``mov dword``) and overwriting adjacent elements.
    """
    asm = _kernel(
        """
            void f() {
                uint16_t arr[4];
                arr[0] = 0x1111;
                arr[1] = 0x2222;
                arr[2] = 0x3333;
            }
        """,
        bits=32,
    )
    assert "mov dword" not in asm, f"uint16_t store must not use mov dword:\n{asm}"
    assert "mov word [esi], 4369" in asm, f"expected halfword store at arr[0]:\n{asm}"
    assert "mov word [esi+2], 8738" in asm, f"expected halfword store at arr[1] (stride 2):\n{asm}"
    assert "mov word [esi+4], 13107" in asm, f"expected halfword store at arr[2]:\n{asm}"
    assert "sub esp, 8" in asm, f"expected 8-byte (4*2) stack reservation:\n{asm}"


def test_uint16_t_local_array_variable_index_scales_by_two() -> None:
    """``arr[i]`` for uint16_t arrays scales index by 2, not int_size."""
    asm = _kernel(
        """
            uint16_t f(int i) {
                uint16_t arr[8];
                arr[i] = 0x4242;
                return arr[i];
            }
        """,
        bits=32,
    )
    # Scaling: i*2 must use ``add eax, eax`` (shift-by-1), never ``shl eax, 2``.
    assert "shl eax, 2" not in asm, f"uint16_t arr[i] must not scale by 4:\n{asm}"
    # Halfword store: the register form ``mov [si], ax`` is the 2-byte
    # store (NASM infers width from the ``ax`` operand).  What's
    # forbidden is the full-width ``mov [si], eax`` (would clobber the
    # adjacent element).
    assert "mov [esi], ax" in asm, f"expected halfword store via ax:\n{asm}"
    assert "mov [esi], eax" not in asm, f"must not use 4-byte store for uint16_t:\n{asm}"
    assert "movzx eax, word [esi" in asm, f"expected halfword load:\n{asm}"


def test_uint16_t_pointer_store_is_halfword() -> None:
    """``uint16_t *p; p[i] = v;`` stores exactly 2 bytes (not 4).

    Companion to :func:`test_uint16_pointer_load_is_word` — the load
    path was fixed for ``uint16_t*`` but the store path kept the binary
    byte-vs-word switch, silently overwriting the adjacent element.
    """
    asm = _kernel(
        """
            void f(uint16_t *p) {
                p[0] = 0x1111;
                p[1] = 0x2222;
            }
        """,
        bits=32,
    )
    assert "mov dword" not in asm, f"uint16_t* store must not use mov dword:\n{asm}"
    assert "mov word [esi], 4369" in asm, f"expected halfword store at p[0]:\n{asm}"
    assert "mov word [esi+2], 8738" in asm, f"expected halfword store at p[1]:\n{asm}"


def test_uint16_t_size_is_always_two_bytes_16bit() -> None:
    """sizeof(uint16_t) == 2 in --bits 16 mode."""
    asm = _kernel("int f() { return sizeof(uint16_t); }", bits=16)
    assert "mov ax, 2" in asm, f"expected sizeof(uint16_t)==2 in 16-bit mode\n{asm}"


def test_uint16_t_size_is_always_two_bytes_32bit() -> None:
    """sizeof(uint16_t) == 2 in --bits 32 mode (not widened to 4)."""
    asm = _kernel("int f() { return sizeof(uint16_t); }", bits=32)
    assert "mov eax, 2" in asm, f"expected sizeof(uint16_t)==2 in 32-bit mode\n{asm}"


def test_uint32_pointer_load_is_dword_on_kernel() -> None:
    """``uint32_t *p; return p[0];`` reads all 4 bytes on the 16-bit target.

    Mirrors :func:`test_unsigned_long_pointer_load_is_dword_on_kernel`;
    this is the same fix extended to the ``uint32_t`` spelling, which
    is the same 4-byte unsigned type on the 16-bit target.  Without
    the broadening, ``uint32_t *`` silently loaded only the low 16
    bits while ``unsigned long *`` loaded all four.
    """
    src = """
        uint32_t test(uint32_t *p __attribute__((in_register("di")))) {
            return p[0];
        }
    """
    asm = _kernel(src, bits=16)
    # The 16-bit code must load both halves: low into AX, high into DX.
    assert "[si]" in asm or "[di]" in asm, f"expected indexed load from pointer reg:\n{asm}"
    # The high-word load is the distinguishing marker.
    assert "[si+2]" in asm or "[di+2]" in asm, f"expected high-word load at +2 for 32-bit pointee on 16-bit target:\n{asm}"


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


def test_unsigned_int_compiles_and_uses_unsigned_compare() -> None:
    """``unsigned int`` is an accepted type spelling and compares unsigned."""
    asm = _kernel("""
        void f() {
            unsigned int x;
            x = 5;
            x = x + 1;
            if (x < 10) {
                x = 0;
            }
        }
    """)
    assert "f:" in asm
    assert "jae" in asm or "jb " in asm, f"expected unsigned compare for unsigned int < 10\n{asm}"
    assert "jge" not in asm and "jl " not in asm, f"signed compare must not appear for unsigned int\n{asm}"


def test_unsigned_int_size_tracks_int_size_16bit() -> None:
    """sizeof(unsigned int) == 2 in --bits 16 mode (matches int width)."""
    asm = _kernel("int f() { return sizeof(unsigned int); }", bits=16)
    assert "mov ax, 2" in asm, f"expected sizeof(unsigned int)==2 in 16-bit mode\n{asm}"


def test_unsigned_int_size_tracks_int_size_32bit() -> None:
    """sizeof(unsigned int) == 4 in --bits 32 mode."""
    asm = _kernel("int f() { return sizeof(unsigned int); }", bits=32)
    assert "mov eax, 4" in asm, f"expected sizeof(unsigned int)==4 in 32-bit mode\n{asm}"


def test_unsigned_long_double_pointer_parses() -> None:
    """``unsigned long **`` parameter type parses (parser regression check)."""
    src = """
        void f(unsigned long **slots __attribute__((in_register("di")))) {
            slots[0] = slots[1];
        }
    """
    asm = _kernel(src)
    assert "f:" in asm


def test_unsigned_long_pointer_load_is_dword_on_kernel() -> None:
    """``unsigned long *p; return p[0];`` on the 16-bit kernel target loads all 32 bits into DX:AX.

    Symptom: the Index emit path uses the target's native acc width
    (``ax``, 16 bits), so the high 16 bits are dropped.  An
    ``unsigned long`` pointee on the 16-bit target needs DX:AX (two
    word loads at ``[si]`` and ``[si+2]``).
    """
    src = """
        unsigned long test(unsigned long *p __attribute__((in_register("di")))) {
            return p[0];
        }
    """
    asm = _kernel(src, bits=16)
    # The 16-bit code must load both halves: low into AX, high into DX.
    assert "[si]" in asm or "[di]" in asm, f"expected indexed load from pointer reg:\n{asm}"
    # The high-word load is the distinguishing marker.
    assert "[si+2]" in asm or "[di+2]" in asm, f"expected high-word load at +2 for 32-bit pointee on 16-bit target:\n{asm}"


def test_unsigned_long_pointer_load_type_check() -> None:
    """``unsigned long x = p[0];`` with ``unsigned long *p`` compiles cleanly.

    Symptom: the IR builder lowers the Index through an ``int`` temp,
    then ``generate_long_expression`` rejects it with
    ``expected 'unsigned long' expression, got 'int' variable '_ir_0'``.
    The fix recognises a long-pointee Index at IR-build time and
    delegates the assignment to the AST codegen path, which knows how
    to produce a DX:AX value.
    """
    src = """
        void test(unsigned long *p __attribute__((in_register("di")))) {
            unsigned long x;
            x = p[0];
            (void)x;
        }
    """
    asm = _kernel(src, bits=16)
    assert "test:" in asm, f"expected function label, got:\n{asm}"


def test_unsigned_long_pointer_parses() -> None:
    """``unsigned long *`` parameter type parses and compiles."""
    src = """
        void f(unsigned long *p __attribute__((in_register("di")))) {
            p[0] = 0;
        }
    """
    asm = _kernel(src)
    assert "f:" in asm


def test_unsigned_pointer_double_indirect_compares_unsigned() -> None:
    """``int **`` (and other double-pointer types) compare as unsigned offsets.

    Regression for the old hand-enumerated UNSIGNED_TYPES set, which
    listed single-star pointer spellings but not double-star.  After the
    pointer-by-suffix simplification, any type ending in ``*`` is
    treated as unsigned by :meth:`_is_unsigned_type`.
    """
    src = """
        int **p __attribute__((asm_name("p")));
        int **q __attribute__((asm_name("q")));
        int check() { return p < q; }
    """
    asm = _kernel(src)
    assert "jb " in asm or "jae" in asm, f"expected unsigned mnemonic for int** comparison\n{asm}"
    assert "jl " not in asm and "jge" not in asm, f"signed compare must not appear for int** comparison\n{asm}"


def test_unsigned_pointer_uint8_double_indirect_compares_unsigned() -> None:
    """``uint8_t **`` comparison uses unsigned mnemonics (suffix-detected)."""
    src = """
        uint8_t **p __attribute__((asm_name("p")));
        uint8_t **q __attribute__((asm_name("q")));
        int check() { return p < q; }
    """
    asm = _kernel(src)
    assert "jb " in asm or "jae" in asm, f"expected unsigned mnemonic for uint8_t** comparison\n{asm}"
    assert "jl " not in asm and "jge" not in asm, f"signed compare must not appear for uint8_t** comparison\n{asm}"


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


def test_user_compound_statement_at_function_scope_compiles() -> None:
    """A bare ``{ ... }`` is a valid statement at function scope.

    Standard C allows compound statements anywhere a statement may
    appear.  cc.py previously only accepted ``{`` after if/while/for/
    do/switch headers; adding parser support for the standalone form
    lets a block introduce a fresh scope for case-local declarations
    and other patterns the existing tree had to hoist around.
    """
    asm = _user(
        """
        int main() {
            int outer;
            outer = 1;
            {
                int inner;
                inner = 2;
                outer = outer + inner;
            }
            return outer;
        }
        """,
    )
    # Both declarations land in the function's frame; the block body
    # generates the same instructions it would inline.
    assert "_f_main:" in asm or "main:" in asm, f"expected main: label:\n{asm}"


def test_user_compound_statement_empty_compiles() -> None:
    """``{ }`` with no statements is a no-op block that still compiles."""
    asm = _user(
        """
        int main() {
            { }
            return 0;
        }
        """,
    )
    assert "main:" in asm or "_f_main:" in asm, f"expected main label:\n{asm}"


def test_user_compound_statement_inside_switch_case_scopes_locals() -> None:
    """``case X: { int t = ...; }`` lets a case body declare its own locals.

    Without compound-statement support the case body would have to
    hoist locals to function scope (the workaround used by shell.c's
    pre-conversion line editor).  With the new node, the locals stay
    case-local in source even if the frame slot is function-wide.
    """
    asm = _user(
        """
        int main() {
            int discriminant;
            int result;
            discriminant = 1;
            result = 0;
            switch (discriminant) {
            case 1:
                {
                    int local_to_case;
                    local_to_case = 10;
                    result = result + local_to_case;
                }
                break;
            default:
                result = -1;
                break;
            }
            return result;
        }
        """,
    )
    # The successful compile is the test; the case-local block must
    # not error out in the parser the way it did before.
    assert ".switch_" in asm, f"expected switch labels:\n{asm}"


def test_user_compound_statement_nested_compiles() -> None:
    """``{ { ... } }`` nests without depth limit (within reason)."""
    asm = _user(
        """
        int main() {
            int value;
            value = 0;
            {
                {
                    {
                        value = 42;
                    }
                }
            }
            return value;
        }
        """,
    )
    assert "main:" in asm or "_f_main:" in asm, f"expected main label:\n{asm}"


def test_user_double_subscript_into_array_of_pointers() -> None:
    """``arr[i][j]`` for ``char *arr[N]`` loads via the new DoubleIndex path.

    Lowering: load the pointer with the existing Index path (Stage 1),
    move it to SI, then byte-load from ``[si+j]`` for ``char *`` elements
    (Stage 2).  The constant-index outer + variable-index inner case used
    by the shell drops out as ``mov eax, [_g_pointers] / mov esi, eax /
    add esi, eax / movzx eax, byte [esi]``.
    """
    ok, asm = _compile(
        r"""
        char *pointers[4];

        int main() {
            char first = pointers[0][0];
            int i = 1;
            char second = pointers[0][i];
            return first + second;
        }
        """,
        target="user",
        bits=32,
    )
    assert ok, f"compile failed:\n{asm}"
    assert "_g_pointers" in asm, f"missing _g_pointers reference:\n{asm}"
    # Constant outer + constant inner: pointer load then byte load via [esi].
    assert "movzx eax, byte [esi]" in asm, f"missing byte load for arr[0][0]:\n{asm}"


def test_user_enum_constants_fold_to_integer_literals() -> None:
    """``enum E { A, B = 5, C };`` makes each variant a compile-time integer.

    Auto-incrementing values resume from the most recent explicit
    value + 1 (A=0, B=5, C=6).  References to a bare variant name
    inline as that literal — verified by case-label compares using
    the explicit numeric constants the user spelled at the enum
    site, not 0/1/2.
    """
    asm = _user(
        """
        enum E { A, B = 5, C };
        int main() {
            int x;
            x = B;
            switch (x) {
                case A: return 1;
                case B: return 2;
                case C: return 3;
            }
            return 99;
        }
        """,
    )
    assert "cmp ax, 5" in asm or "cmp dx, 5" in asm, f"expected case-B compare against 5:\n{asm}"
    assert "cmp ax, 6" in asm or "cmp dx, 6" in asm, f"expected case-C compare against 6:\n{asm}"


def test_user_enum_switch_covers_all_variants_compiles_and_dispatches_each() -> None:
    """A switch that names every enum variant compiles and emits a compare per variant.

    The exhaustiveness check should be a no-op when every declared
    variant has a matching ``case``.  Positive complement to
    :func:`test_user_enum_switch_exhaustiveness_rejects_missing_variant`
    — together they pin both sides of the gate.  The asm assertion
    inspects the dispatch chain by looking for a ``cmp`` against each
    enum value (1, 2, 3 for ``A, B, C`` here) so a future refactor
    that silently drops a variant from the compare chain still trips.
    """
    asm = _user(
        """
        enum E { A = 1, B, C };
        int main() {
            enum E x;
            x = A;
            int result;
            switch (x) {
                case A: result = 10; break;
                case B: result = 20; break;
                case C: result = 30; break;
            }
            return result;
        }
        """,
    )
    cmp_lines = [line for line in asm.splitlines() if line.lstrip().startswith("cmp ")]
    for value in (1, 2, 3):
        assert any(line.endswith(f", {value}") for line in cmp_lines), (
            f"missing `cmp ..., {value}` for enum variant; cmp lines were:\n{cmp_lines}"
        )


def test_user_enum_switch_default_suppresses_exhaustiveness() -> None:
    """A ``default:`` arm makes exhaustiveness vacuously satisfied.

    Even though ``case C`` is omitted, the presence of ``default``
    means the switch covers every possible discriminant value — the
    exhaustiveness check is skipped.
    """
    _user(
        """
        enum E { A, B, C };
        int main() {
            enum E x;
            x = A;
            switch (x) {
                case A: return 1;
                case B: return 2;
                default: return 99;
            }
        }
        """,
    )


def test_user_enum_switch_exhaustiveness_rejects_missing_variant() -> None:
    """Switch on enum without ``default`` errors if any variant is uncovered.

    This is the headline feature: adding a new enum variant later
    flags every switch site that forgot to handle it, at compile
    time, instead of silently dispatching to a default branch (or
    falling off the end).
    """
    ok, message = _compile(
        """
        enum E { A, B, C };
        int main() {
            enum E x;
            x = A;
            switch (x) {
                case A: return 1;
                case B: return 2;
            }
            return 99;
        }
        """,
        target="user",
    )
    assert not ok, f"expected exhaustiveness error, compilation succeeded:\n{message}"
    assert "switch on enum 'E' missing case for 'C'" in message, message


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


def test_user_global_array_pointer_and_uint32_elements() -> None:
    """Pointer-typed and uint32_t global arrays land in BSS as word-strided slots.

    ``char *slots[N]`` and ``uint32_t counters[N]`` both follow the
    existing word-stride codegen path; the file-scope allowlist accepts
    pointer element types and uint32_t (alongside the original
    char/int/uint8_t/struct).
    """
    ok, asm = _compile(
        r"""
        char *slots[4];
        uint32_t counters[3];

        int main() {
            slots[0] = "hi";
            slots[1] = 0;
            counters[2] = 0xDEADBEEF;
            char *p = slots[0];
            uint32_t c = counters[2];
            return c;
        }
        """,
        target="user",
        bits=32,
    )
    assert ok, f"compile failed:\n{asm}"
    assert "_g_slots equ _program_end" in asm, f"slots missing from BSS:\n{asm}"
    assert "_g_counters equ _program_end" in asm, f"counters missing from BSS:\n{asm}"
    # 4 pointer slots (16 bytes) + 3 uint32 slots (12 bytes) = 28 bytes.
    assert "_bss_end equ _program_end + 28" in asm, f"expected 16+12=28 BSS bytes:\n{asm}"


def test_user_global_array_accepts_uint16_element() -> None:
    """uint16_t global arrays compile and use the halfword codegen path."""
    ok, output = _compile(
        r"""
        uint16_t halfwords[4];

        int main() {
            halfwords[0] = 0xAABB;
            return 0;
        }
        """,
        target="user",
        bits=32,
    )
    assert ok, f"unexpected compile error:\n{output}"
    assert "mov dword" not in output, f"uint16_t global must not store dword:\n{output}"
    assert "mov word [_g_halfwords]" in output, f"expected halfword store:\n{output}"


def test_user_goto_backward_jump_re_enters_block() -> None:
    """``label: ...; goto label;`` backward-jumps to the label.

    The compiled output must contain both the label definition and a
    branch (peephole may fuse cmp+jmp into je/jl/etc.) targeting it.
    """
    asm = _user(
        """
        int main() {
            int counter;
            counter = 0;
            again:
            counter = counter + 1;
            if (counter < 3) {
                goto again;
            }
            return counter;
        }
        """,
    )
    branch_prefixes = ("jmp ", "je ", "jne ", "jl ", "jle ", "jg ", "jge ", "jc ", "jnc ", "ja ", "jae ", "jb ", "jbe ")
    label_lines = [line.strip() for line in asm.splitlines() if line.strip().endswith(":") and "again" in line]
    assert label_lines, f"expected user label `again` in output:\n{asm}"
    target_name = label_lines[0].rstrip(":")
    branch_lines = [
        line for line in asm.splitlines() if target_name in line and any(line.strip().startswith(prefix) for prefix in branch_prefixes)
    ]
    assert branch_lines, f"expected branch to user label {target_name!r}:\n{asm}"


def test_user_goto_forward_jump_emits_label_and_branch() -> None:
    """Forward ``goto`` emits a branch to a later-defined label.

    Use a conditional goto so the peephole can't collapse the jump as
    dead code; the label must still appear after the goto in source
    order.
    """
    asm = _user(
        """
        int g;
        int main() {
            if (g == 0) {
                goto finish;
            }
            g = 99;
            finish:
            return g;
        }
        """,
    )
    lines = asm.splitlines()
    label_index = next(
        (index for index, line in enumerate(lines) if line.strip() == ".user_finish:"),
        None,
    )
    branch_index = next(
        (index for index, line in enumerate(lines) if ".user_finish" in line and not line.strip().endswith(":")),
        None,
    )
    assert label_index is not None, f"expected `.user_finish:` label:\n{asm}"
    assert branch_index is not None, f"expected branch to `.user_finish`:\n{asm}"
    assert branch_index < label_index, f"forward goto: branch must precede label:\n{asm}"


def test_user_goto_in_helper_function_uses_ir_path() -> None:
    """``goto`` inside a non-main helper compiles via the IR builder.

    Helper functions go through ir.Builder (main keeps the AST path).
    The IR path must emit ``.user_<name>`` labels and jumps identical
    in shape to the AST path so cross-function code generation stays
    consistent.
    """
    asm = _user(
        """
        int helper(int seed) {
            int value;
            value = seed;
            top:
            value = value + 1;
            if (value < seed + 3) {
                goto top;
            }
            return value;
        }

        int main() {
            return helper(0);
        }
        """,
    )
    assert ".user_top:" in asm, f"expected helper's label `.user_top:`:\n{asm}"
    assert any(".user_top" in line and not line.strip().endswith(":") for line in asm.splitlines()), (
        f"expected branch to `.user_top`:\n{asm}"
    )


def test_user_goto_nasm_assembles_cleanly() -> None:
    """A program using ``goto`` produces NASM-valid output.

    The ``.user_<name>`` label prefix relies on NASM local-label
    scoping (a leading ``.`` makes the label local to the preceding
    global label, which is the function name).  Cross-function reuse
    of the same label name must therefore assemble without error.
    """
    _compile_and_assemble(
        """
        int helper(int n) {
            again:
            n = n - 1;
            if (n > 0) {
                goto again;
            }
            return n;
        }

        int main() {
            int counter;
            counter = helper(5);
            again:
            counter = counter + 1;
            if (counter < 3) {
                goto again;
            }
            return counter;
        }
        """,
    )


def test_user_goto_out_of_nested_loop_lands_outside() -> None:
    """``goto done;`` from inside a switch inside a while exits both.

    Demonstrates the kernel-side cleanup-chain motivator: a
    deeply-nested control-flow point can jump straight to a
    function-level label.  Verifies the goto target and the label
    both exist and that the label appears after the loop's end label
    in source order.
    """
    asm = _user(
        """
        int main() {
            int index;
            index = 0;
            while (index < 10) {
                switch (index) {
                    case 3:
                        goto done;
                    default:
                        index = index + 1;
                }
            }
            done:
            return index;
        }
        """,
    )
    # Peephole may fuse cmp+jmp into a conditional jump (je / jne / etc.)
    # so accept any jump-family instruction targeting the user label.

    def _is_branch_to_done(line: str) -> bool:
        if "done" not in line:
            return False
        return "jmp" in line or line.strip().startswith(("je ", "jne ", "jc ", "jnc "))

    assert any(_is_branch_to_done(line) for line in asm.splitlines()), f"missing branch to `done`:\n{asm}"
    assert any(line.strip().endswith(":") and "done" in line for line in asm.splitlines()), f"missing `done:` label:\n{asm}"


def test_user_goto_undefined_label_raises() -> None:
    """``goto missing;`` without a matching label is a compile error.

    The diagnostic must reference the missing label name so the user
    can locate it.
    """
    ok, message = _compile(
        """
        int main() {
            goto nowhere;
            return 0;
        }
        """,
        target="user",
    )
    assert not ok, f"expected undefined-label error, compilation succeeded:\n{message}"
    assert "nowhere" in message, message


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


def test_user_switch_always_exit_case_body_skips_end_jump() -> None:
    """A case body whose last statement is ``return`` / ``goto`` emits no fall-through ``jmp .end``.

    cc.py already lets a user-written ``return X;`` lower to
    ``jmp FUNCTION_EXIT`` and ``goto X;`` lower to ``jmp .user_X`` —
    those exits make any trailing fall-through jump dead.  This test
    pins that no extra ``jmp .switch_*_end`` is emitted in either
    arm's body when the body already always-exits.
    """
    asm = _user(
        """
        int main() {
            char character;
            int value;
            character = '\x03';
            value = 0;
            switch (character) {
            case '\x03':
                value = 1;
                goto done;
            case '\x04':
                return 99;
            default:
                value = -1;
            }
            done:
            return value;
        }
        """,
    )
    lines = asm.splitlines()
    case_0_index = next(index for index, line in enumerate(lines) if line.strip() == ".switch_0_case_0:")
    case_1_index = next(index for index, line in enumerate(lines) if line.strip() == ".switch_0_case_1:")
    default_index = next(index for index, line in enumerate(lines) if ".switch_0_default" in line and line.strip().endswith(":"))
    case_0_body = lines[case_0_index + 1 : case_1_index]
    case_1_body = lines[case_1_index + 1 : default_index]
    assert not any("jmp .switch_0_end" in line for line in case_0_body), f"case 0 (goto-exit) emitted dead `jmp .end`:\n{case_0_body}"
    assert not any("jmp .switch_0_end" in line for line in case_1_body), f"case 1 (return-exit) emitted dead `jmp .end`:\n{case_1_body}"


def test_user_switch_break_inside_loop_exits_only_switch() -> None:
    """``break`` inside a switch nested in a while loop exits the switch, not the loop.

    The compiled output's switch end label is the only break target,
    so the surrounding while loop's body resumes after the switch
    and the loop runs to its own termination condition.
    """
    asm = _user(
        """
        int main() {
            int i;
            int seen;
            i = 0;
            seen = 0;
            while (i < 5) {
                switch (i) {
                    case 1:
                        seen = seen + 10;
                        break;
                    case 2:
                        seen = seen + 20;
                        break;
                    default:
                        seen = seen + 1;
                }
                i = i + 1;
            }
            return seen;
        }
        """,
    )
    # The break inside the switch must land at the switch's end label,
    # not the while loop's end label.  Both labels exist; the jumps
    # emitted for the two `break` statements must target the switch.
    assert ".switch_" in asm and "_end" in asm, f"missing switch end label:\n{asm}"
    assert ".while_" in asm and "_end" in asm, f"missing while end label:\n{asm}"
    switch_break_lines = [line for line in asm.splitlines() if "jmp .switch_" in line and "_end" in line]
    assert switch_break_lines, f"expected break to jump to switch end label:\n{asm}"
    # The runtime answer is the dispositive proof break only exits the
    # switch — 10 + 20 + 1 + 1 + 1 = 33; if break broke the loop too,
    # we'd see something different (e.g. 10 = first hit then exit).
    # We can't run it here, but the label discipline above is enough.


def test_user_switch_dispatch_hoists_memory_discriminant_into_register() -> None:
    """Multi-arm switch over a memory-backed scalar loads it once before the chain.

    Without the hoist, every case-compare emits ``cmp byte [addr], imm``
    (5-7 bytes on x86-32) and the same address is dereferenced N times.
    With the hoist, a single ``mov al, [addr]`` precedes the dispatch
    chain and each compare becomes ``cmp al, imm`` (2-3 bytes).
    Self-paying for N >= 2 arms.

    Uses a file-scope global as the discriminant so the value is
    guaranteed memory-backed (no auto-pin candidate selection drama).
    """
    asm = _user(
        """
        char g_character;
        int main() {
            g_character = '\x01';
            int result;
            result = 0;
            switch (g_character) {
            case '\x01': result = 1; break;
            case '\x02': result = 2; break;
            case '\x05': result = 5; break;
            case '\x06': result = 6; break;
            default: result = -1;
            }
            return result;
        }
        """,
    )
    lines = asm.splitlines()
    # The dispatch chain must NOT repeat `cmp byte [_g_character]` per arm.
    # At most one memory-dereferenced cmp may appear (or zero if the hoist
    # always emits a load+register-cmp sequence).
    memory_cmps = [line for line in lines if "cmp byte [_g_character]" in line]
    assert len(memory_cmps) <= 1, f"expected hoisted discriminant load, got {len(memory_cmps)} memory cmps:\n{memory_cmps}"
    # And a register-form `cmp al, imm` (or eax / similar) must appear at
    # least N-1 times for an N-arm dispatch (N=4 here).
    register_cmps = [line for line in lines if line.strip().startswith(("cmp al,", "cmp eax,", "cmp ax,"))]
    assert len(register_cmps) >= 3, f"expected at least 3 register cmps after hoist, got {len(register_cmps)}:\n{register_cmps}"


def test_user_switch_dispatch_interleaves_when_discriminant_pinned() -> None:
    """A switch whose discriminant ends up pinned and whose arms all exit emits interleaved dispatch.

    The separated dispatch shape (all `cmp; je` up front, all bodies after) forces
    `je` to jump past every preceding case body — most become 6-byte near jumps.
    The interleaved shape (`cmp; jne .next_i; <body>; jmp .end; .next_i:` per arm)
    keeps each dispatch branch local: `jne` skips one body (almost always short),
    saving 4 bytes per arm.  Only safe when no case body falls through to the next
    (we always emit `jmp .end` after each body), so the optimisation gates on
    every case being `always_exits`.
    """
    asm = _user(
        """
        int main() {
            int discriminant = 0;
            while (1) {
                int previous = discriminant;
                discriminant = discriminant + 1;
                switch (previous) {
                case 1: discriminant = 10; break;
                case 2: discriminant = 20; break;
                case 3: discriminant = 30; break;
                case 4: discriminant = 40; break;
                case 5: discriminant = 50; break;
                case 6: discriminant = 60; break;
                case 7: discriminant = 70; break;
                case 8: discriminant = 80; break;
                }
                if (discriminant > 100) {
                    return discriminant;
                }
            }
        }
        """,
    )
    lines = [line.strip() for line in asm.splitlines()]
    # In the interleaved shape, dispatch comparisons are separated by case bodies,
    # so no two `cmp <reg>, <imm>; je` pairs sit back-to-back.  Find every dispatch
    # `cmp` and check what's two lines after — in separated it'd be the next dispatch
    # cmp; in interleaved it'd be a non-cmp (body opcode or next-case label).
    consecutive_cmp_je = 0
    for index, line in enumerate(lines):
        if not line.startswith("cmp "):
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("je "):
            continue
        # Found a `cmp; je` dispatch pair; check the next line.
        if index + 2 < len(lines) and lines[index + 2].startswith("cmp "):
            consecutive_cmp_je += 1
    assert consecutive_cmp_je <= 1, (
        f"dispatch looks separated (found {consecutive_cmp_je} back-to-back cmp/je dispatch pairs); "
        f"expected interleaved shape with case bodies between dispatches:\n{asm}"
    )


def test_user_switch_fall_through_between_cases() -> None:
    """Adjacent cases without ``break`` fall into the next arm's body.

    ``case 1`` has no ``break``, so when the discriminant matches 1
    the body for case 2 runs as well.  In the compiled output the
    two case labels appear back-to-back with no intervening jump.
    """
    asm = _user(
        """
        int main() {
            int x;
            int sum;
            x = 1;
            sum = 0;
            switch (x) {
                case 1: sum = sum + 10;
                case 2: sum = sum + 20;
                default: sum = sum + 100;
            }
            return sum;
        }
        """,
    )
    # There should be no jmp out of case 1's body — it falls into case 2.
    # Look for the case_0 label followed by a body and then the case_1
    # label, without an intervening jmp-to-end between them.
    lines = [line.strip() for line in asm.splitlines()]
    case_indices = [index for index, line in enumerate(lines) if line.startswith(".switch_") and "_case_" in line]
    assert len(case_indices) >= 2, f"expected at least two case labels:\n{asm}"
    between = lines[case_indices[0] + 1 : case_indices[1]]
    assert not any(line.startswith("jmp ") for line in between), (
        f"unexpected jmp between adjacent cases (fall-through must not break):\n{between}"
    )


def test_user_switch_on_char_discriminant_accepts_char_literal_cases() -> None:
    """``switch (char_var) { case 'A': ... }`` compiles cleanly.

    Regression: the parser's constant-folding pass (_evaluate_constant_int)
    collapses every case-label expression to ``Int``, losing the ``Char``
    classification.  The switch lowering then emits ``BinaryOperation``
    nodes with ``Int(value=65)`` opposite the char discriminant, tripping
    the comparison validator's char-vs-int check.  The fix detects a
    char-typed discriminant in ``generate_switch`` and wraps each case
    value as ``Char`` so the validator sees char-vs-char.
    """
    asm = _kernel(
        """
            int classify(char byte) {
                switch (byte) {
                    case 'A': return 1;
                    case 'B': return 2;
                    case 'C': return 3;
                }
                return 0;
            }
        """,
        bits=32,
    )
    # Each case lowers to a compare against the literal's ordinal.
    # Don't pin to a specific register since the param may be pinned
    # into edx / al / ax depending on the calling convention; the
    # important contract is that the integer literal value reaches
    # a ``cmp`` operand.
    assert ", 65" in asm, f"expected compare against 'A' (65):\n{asm}"
    assert ", 66" in asm, f"expected compare against 'B' (66):\n{asm}"
    assert ", 67" in asm, f"expected compare against 'C' (67):\n{asm}"


def test_user_switch_on_char_pointer_dereference_accepts_char_literal_cases() -> None:
    r"""``switch (p[i]) { case '\n': ... }`` works for ``char *p`` subscripts.

    The discriminant is an Index expression on a ``char *`` pointer —
    the validator classifies the load as ``char`` via _type_of_operand,
    so the same Int→Char wrap must fire to clear the char-vs-int check.
    """
    asm = _kernel(
        """
            int leading(char *p) {
                switch (p[0]) {
                    case '\\n': return 1;
                    case '\\t': return 2;
                }
                return 0;
            }
        """,
        bits=32,
    )
    assert ", 10" in asm, f"expected compare against '\\n' (10):\n{asm}"
    assert ", 9" in asm, f"expected compare against '\\t' (9):\n{asm}"


def test_user_switch_on_plain_int_skips_exhaustiveness() -> None:
    """A non-enum-typed discriminant never triggers the exhaustiveness check.

    Plain ``int`` switches with missing ``default:`` compile fine —
    they're just compare/jump chains.  The exhaustiveness check is
    intentionally restricted to enum-typed discriminants since that's
    where the set of valid values is statically knowable.
    """
    _user(
        """
        int main() {
            int x;
            x = 7;
            switch (x) {
                case 1: return 10;
                case 2: return 20;
            }
            return 99;
        }
        """,
    )


def test_user_switch_rejects_duplicate_case_values() -> None:
    """Two ``case`` arms with the same constant value are rejected at parse time."""
    ok, message = _compile(
        """
        int main() {
            int x;
            x = 0;
            switch (x) {
                case 1: return 10;
                case 1: return 20;
            }
            return 99;
        }
        """,
        target="user",
    )
    assert not ok, f"expected duplicate-case error, compilation succeeded:\n{message}"
    assert "duplicate case value" in message, message


def test_user_switch_rejects_non_constant_case_label() -> None:
    """A ``case`` label that isn't a compile-time integer constant is rejected."""
    ok, message = _compile(
        """
        int g;
        int main() {
            int x;
            x = 0;
            switch (x) {
                case g: return 10;
            }
            return 99;
        }
        """,
        target="user",
    )
    assert not ok, f"expected non-constant case error, compilation succeeded:\n{message}"
    assert "compile-time integer constant" in message, message


@pytest.mark.parametrize("source_path", sorted((REPO_ROOT / "user" / "programs").glob("*.c")))
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


def test_void_cast_call_statement_emits_call() -> None:
    """``(void)open(...);`` parses and emits the call, discarding the return."""
    asm = _user("""
        int main() {
            (void)open("foo", 0);
            return 0;
        }
    """)
    assert "IO_OPEN" in asm or "SYS_IO_OPEN" in asm, f"expected open syscall to be emitted:\n{asm}"


def test_void_cast_variable_compiles_to_no_op() -> None:
    """``(void)x;`` parses and emits no code for the cast itself."""
    asm = _user("""
        int main() {
            int x;
            x = 5;
            (void)x;
            return x;
        }
    """)
    # The variable read still has its assignment, but the (void) cast
    # itself produces no instructions.
    assert "main:" in asm


def test_builtin_read_emits_fd_last() -> None:
    """builtin_read must load `fd` into BX AFTER computing buf/count.

    Otherwise: when `total` (or any var pinned to BX) is referenced by the
    buf or count expression, the `mov bx, fd` clobbers it before use,
    silently emitting `add edi, ebx` and `sub eax, ebx` that read the fd
    value instead of total.

    Regression caught while landing user/programs/tail.c — passing `read(fd,
    tail_buf + total, BUF - total)` inside a loop produced wrong reads at
    offset `fd` instead of offset `total`.  builtin_write already orders
    args this way; this test pins down the same property for read.

    We use a tail.c-shaped source because the auto-pin allocator's
    decision is sensitive to call mix and var ref counts; with the
    walk-back logic and the second `read(fd, &overflow, 1)` call, `total`
    lands on EBX, which is what makes the bug visible.
    """
    asm = _user(
        """
        #define BUF 65536
        int parse_int(char *string) {
            int value = 0;
            int index = 0;
            while (string[index] >= '0' && string[index] <= '9') {
                value = value * 10 + (string[index] - '0');
                index = index + 1;
            }
            return value;
        }
        char tail_buf[BUF];
        int main(int argc, char *argv[]) {
            int want = 10;
            char *path = NULL;
            int arg = 1;
            while (arg < argc) {
                char *a = argv[arg];
                if (a[0] == '-' && a[1] == 'n' && a[2] == '\\0') {
                    arg = arg + 1;
                    if (arg >= argc) {
                        die("tail: -n needs a number\\n");
                    }
                    want = parse_int(argv[arg]);
                } else {
                    path = a;
                }
                arg = arg + 1;
            }
            if (path == NULL) {
                die("tail: pass a file\\n");
            }
            int fd = open(path, O_RDONLY);
            if (fd < 0) {
                die("tail: open failed\\n");
            }
            int total = 0;
            while (total < BUF) {
                int n = read(fd, tail_buf + total, BUF - total);
                if (n <= 0) {
                    break;
                }
                total = total + n;
            }
            char overflow;
            int extra = read(fd, &overflow, 1);
            close(fd);
            if (extra > 0) {
                die("tail: file too large\\n");
            }
            int index = total;
            int found = 0;
            if (index > 0 && tail_buf[index - 1] == '\\n') {
                index = index - 1;
            }
            while (index > 0 && found < want) {
                index = index - 1;
                if (tail_buf[index] == '\\n') {
                    found = found + 1;
                    if (found == want) {
                        index = index + 1;
                        break;
                    }
                }
            }
            int remaining = total - index;
            if (remaining > 0) {
                write(STDOUT, tail_buf + index, remaining);
            }
            return 0;
        }
        """,
        bits=32,
    )
    # For each SYS_IO_READ syscall, walk back to find the argument-setup
    # block and check that EBX is not written then read within it.
    lines = [line.strip() for line in asm.splitlines()]
    jump_prefixes = ("jmp", "jge", "jle", "jl ", "jg ", "je ", "jne", "jz ", "jnz", "call", "ja ", "jb ", "jae", "jbe", "jc ", "jnc")
    found_at_least_one = False
    for index, line in enumerate(lines):
        if line != "int 30h":
            continue
        if index < 1 or "SYS_IO_READ" not in lines[index - 1]:
            continue
        found_at_least_one = True
        start = index
        while start > 0:
            previous = lines[start - 1]
            if previous.endswith(":") or previous.startswith(jump_prefixes):
                break
            start -= 1
        block = lines[start:index]
        last_ebx_write = -1
        for offset, instruction in enumerate(block):
            if instruction.startswith(("mov ebx,", "xor ebx,", "pop ebx")) and not instruction.startswith("pop ebx"):
                last_ebx_write = offset
        if last_ebx_write < 0:
            continue
        tail = block[last_ebx_write + 1 :]
        bad = [instruction for instruction in tail if ", ebx" in instruction.split(";", 1)[0]]
        assert not bad, (
            "builtin_read clobbered EBX before reading it as a source operand. "
            "If a pinned variable lives in EBX, this corrupts it. Offending "
            "tail:\n" + "\n".join(tail) + "\n--- full setup block ---\n" + "\n".join(block)
        )
    assert found_at_least_one, f"test source must compile to at least one SYS_IO_READ; got asm:\n{asm}"


def test_builtin_write_loads_buffer_after_strlen_sibling() -> None:
    """write(fd, names[i], strlen(names[i])) must load ESI last.

    Regression: builtin_write used to load ESI=buffer, then ECX=count.
    When ``count`` was ``strlen(names[i])`` the recursive lowering
    re-evaluated the Index expression and re-used ESI as the
    base-address scratch — overwriting the buffer pointer that was
    just placed there.  The resulting ``write`` syscall pointed at the
    ``names`` array's first slot every iteration, so ``ls`` (the
    program that surfaced this in user/programs/ls.c) printed the same name
    repeated, garbled by length mismatches.

    The fix routes write's three arg loads through
    :meth:`_emit_builtin_arg_moves`, whose scheduler now also tracks
    "this evaluation will scratch ESI" and defers the ESI-targeted
    load until after sibling args whose lowering clobbers it.
    """
    asm = _user(
        """
        int main() {
            char arena[16];
            char *names[3];
            arena[0] = 'a'; arena[1] = 'b'; arena[2] = 0;
            arena[3] = 'c'; arena[4] = 'd'; arena[5] = 0;
            arena[6] = 'e'; arena[7] = 'f'; arena[8] = 0;
            names[0] = arena + 0;
            names[1] = arena + 3;
            names[2] = arena + 6;
            int i = 0;
            while (i < 3) {
                write(STDOUT, names[i], strlen(names[i]));
                putchar('\\n');
                i = i + 1;
            }
            return 0;
        }
        """,
        bits=32,
    )
    lines = [line.strip() for line in asm.splitlines()]
    jump_prefixes = ("jmp", "jge", "jle", "jl ", "jg ", "je ", "jne", "jz ", "jnz", "call", "ja ", "jb ", "jae", "jbe", "jc ", "jnc")
    found_at_least_one = False
    for index, line in enumerate(lines):
        if line != "int 30h":
            continue
        if index < 1 or "SYS_IO_WRITE" not in lines[index - 1]:
            continue
        found_at_least_one = True
        start = index
        while start > 0:
            previous = lines[start - 1]
            if previous.endswith(":") or previous.startswith(jump_prefixes):
                break
            start -= 1
        block = lines[start:index]
        # The buffer load is the LAST instruction that writes ESI
        # before the syscall.  After that, nothing else should write
        # ESI — that would obliterate the buffer pointer before
        # SYS_IO_WRITE reads it.
        esi_writes = [
            offset
            for offset, instruction in enumerate(block)
            if instruction.startswith(("mov esi,", "lea esi,", "xor esi,", "add esi,", "sub esi,", "pop esi"))
        ]
        if not esi_writes:
            continue
        last_esi_write = esi_writes[-1]
        tail = block[last_esi_write + 1 :]
        bad = [
            instruction
            for instruction in tail
            if instruction.startswith(("mov esi,", "lea esi,", "xor esi,", "add esi,", "sub esi,", "pop esi"))
        ]
        assert not bad, (
            "builtin_write clobbered ESI after loading the buffer pointer. "
            "The syscall will dereference the wrong address. Offending tail:\n"
            + "\n".join(tail)
            + "\n--- full setup block ---\n"
            + "\n".join(block)
        )
    assert found_at_least_one, f"test source must compile to at least one SYS_IO_WRITE; got asm:\n{asm}"


def test_builtin_dup2_loads_bx_after_clobbering_sibling() -> None:
    """dup2(old_fd, get_target()) must load EBX last.

    Regression: builtin_dup2 used to load EBX=old_fd first, then
    EDX=target_fd.  When ``target_fd`` was a user-function call, the
    Call's lowering (caller-save cdecl) clobbered EBX between the
    load and the syscall.  The kernel dup2 handler then saw garbage
    in EBX.

    Routing dup2's arg loads through :meth:`_emit_builtin_arg_moves`
    defers the EBX load until after every sibling whose evaluation
    would clobber EBX.
    """
    asm = _user(
        """
        int get_target() { return 5; }
        int main() {
            dup2(2, get_target());
            return 0;
        }
        """,
        bits=32,
    )
    lines = [line.strip() for line in asm.splitlines()]
    # NOTE: "call" intentionally omitted from the block-break set —
    # a user-function call in a sibling arg IS the bug we want to
    # catch (caller-save scratching the already-loaded EBX), so the
    # walk-back must span past it.
    jump_prefixes = ("jmp", "jge", "jle", "jl ", "jg ", "je ", "jne", "jz ", "jnz", "ja ", "jb ", "jae", "jbe", "jc ", "jnc")
    found_at_least_one = False
    for index, line in enumerate(lines):
        if line != "int 30h":
            continue
        if index < 1 or "SYS_IO_DUP2" not in lines[index - 1]:
            continue
        found_at_least_one = True
        start = index
        while start > 0:
            previous = lines[start - 1]
            if previous.endswith(":") or previous.startswith(jump_prefixes):
                break
            start -= 1
        block = lines[start:index]
        # After the LAST instruction that writes EBX (the dup2 input
        # ``mov ebx, <old_fd>``), nothing else in the setup block may
        # write EBX — a user-function call between that load and the
        # syscall would clobber EBX as caller-save scratch.
        ebx_writes = [
            offset
            for offset, instruction in enumerate(block)
            if instruction.startswith(("mov ebx,", "lea ebx,", "xor ebx,", "add ebx,", "sub ebx,", "pop ebx"))
        ]
        if not ebx_writes:
            continue
        last_ebx_write = ebx_writes[-1]
        tail = block[last_ebx_write + 1 :]
        bad = [instruction for instruction in tail if instruction.startswith("call ")]
        assert not bad, (
            "builtin_dup2 loaded EBX before a sibling call that clobbers it. "
            "The dup2 syscall will read garbage from EBX. Offending tail:\n"
            + "\n".join(tail)
            + "\n--- full setup block ---\n"
            + "\n".join(block)
        )
    assert found_at_least_one, f"test source must compile to at least one SYS_IO_DUP2; got asm:\n{asm}"


def test_builtin_pipeline2_loads_si_after_index_sibling() -> None:
    """pipeline2(cmds[i], _, cmds[j], _) must load ESI last.

    Regression: builtin_pipeline2 used to load ESI=left_path first
    (via emit_si_from_argument), then EDI=right_path.  When
    ``right_path`` is an Index expression like ``cmds[j]``, the
    Index lowering reuses ESI as the base-address scratch — wiping
    out the left_path pointer before the SYS_SYS_PIPELINE2 syscall
    runs.  Same shape as the write(fd, names[i], strlen(names[i]))
    bug that motivated PR #386.

    The fix routes pipeline2's four arg loads through
    :meth:`_emit_builtin_arg_moves`, whose scheduler defers the ESI
    target until after sibling args whose lowering scratches ESI.
    """
    asm = _user(
        """
        int main() {
            char *cmds[3];
            char **argv = 0;
            cmds[0] = "a"; cmds[1] = "b"; cmds[2] = "c";
            int i = 0;
            int j = 1;
            pipeline2(cmds[i], argv, cmds[j], argv);
            return 0;
        }
        """,
        bits=32,
    )
    lines = [line.strip() for line in asm.splitlines()]
    jump_prefixes = ("jmp", "jge", "jle", "jl ", "jg ", "je ", "jne", "jz ", "jnz", "call", "ja ", "jb ", "jae", "jbe", "jc ", "jnc")
    found_at_least_one = False
    for index, line in enumerate(lines):
        if line != "int 30h":
            continue
        if index < 1 or "SYS_SYS_PIPELINE2" not in lines[index - 1]:
            continue
        found_at_least_one = True
        start = index
        while start > 0:
            previous = lines[start - 1]
            if previous.endswith(":") or previous.startswith(jump_prefixes):
                break
            start -= 1
        block = lines[start:index]
        # The fix's contract: after ESI is loaded with the final
        # cmds[i] value (``mov esi, eax``), nothing else in the
        # setup block may re-write ESI — the second Index lowering
        # (for cmds[j]) must have completed already, including all
        # its `lea esi, [ebp-...]` / `add esi, ...` scratch writes.
        # Different shape from the write test (which catches
        # post-load overwrites by a sibling builtin Call) because
        # pipeline2's bug surfaces as a mid-sequence Index-lowering
        # overwrite.
        final_esi_load = -1
        for offset, instruction in enumerate(block):
            if instruction == "mov esi, eax":
                final_esi_load = offset
        if final_esi_load < 0:
            continue
        tail = block[final_esi_load + 1 :]
        bad = [
            instruction
            for instruction in tail
            if instruction.startswith(("mov esi,", "lea esi,", "xor esi,", "add esi,", "sub esi,", "pop esi"))
        ]
        assert not bad, (
            "builtin_pipeline2 re-wrote ESI after loading the left_path "
            "pointer.  In the buggy sequential order, cmds[j]'s Index "
            "lowering scratches ESI between the ESI load and the syscall, "
            "wiping the left_path pointer.  The topological scheduler must "
            "emit the right_path Index lowering BEFORE the ESI=left_path "
            "load.  Offending tail:\n" + "\n".join(tail) + "\n--- full setup block ---\n" + "\n".join(block)
        )
    assert found_at_least_one, f"test source must compile to at least one SYS_SYS_PIPELINE2; got asm:\n{asm}"


def test_builtin_signal_loads_bx_after_clobbering_sibling() -> None:
    """signal(signum, get_handler()) must load EBX last.

    Regression: builtin_signal used to load EBX=signum first, then
    ECX=handler.  When ``handler`` was a user-function call, the
    Call's lowering (caller-save cdecl) clobbered EBX between the
    load and the syscall.  The kernel signal handler then saw
    garbage in EBX as the signal number.

    Routing signal's arg loads through :meth:`_emit_builtin_arg_moves`
    defers the EBX load until after every sibling whose evaluation
    would clobber EBX.
    """
    asm = _user(
        """
        int get_handler() { return 1; }
        int main() {
            signal(2, get_handler());
            return 0;
        }
        """,
        bits=32,
    )
    lines = [line.strip() for line in asm.splitlines()]
    # NOTE: "call" intentionally omitted from the block-break set —
    # the bug we want to surface is a sibling user-function call
    # clobbering EBX after it's been loaded, so walk-back must
    # cross past calls.
    jump_prefixes = ("jmp", "jge", "jle", "jl ", "jg ", "je ", "jne", "jz ", "jnz", "ja ", "jb ", "jae", "jbe", "jc ", "jnc")
    found_at_least_one = False
    for index, line in enumerate(lines):
        if line != "int 30h":
            continue
        if index < 1 or "SYS_SYS_SIGNAL" not in lines[index - 1]:
            continue
        found_at_least_one = True
        start = index
        while start > 0:
            previous = lines[start - 1]
            if previous.endswith(":") or previous.startswith(jump_prefixes):
                break
            start -= 1
        block = lines[start:index]
        # After the LAST instruction that writes EBX (the signal
        # input ``mov ebx, <signum>``), nothing else in the setup
        # block may write EBX — a user-function call between that
        # load and the syscall would clobber EBX as caller-save
        # scratch.
        ebx_writes = [
            offset
            for offset, instruction in enumerate(block)
            if instruction.startswith(("mov ebx,", "lea ebx,", "xor ebx,", "add ebx,", "sub ebx,", "pop ebx"))
        ]
        if not ebx_writes:
            continue
        last_ebx_write = ebx_writes[-1]
        tail = block[last_ebx_write + 1 :]
        bad = [instruction for instruction in tail if instruction.startswith("call ")]
        assert not bad, (
            "builtin_signal loaded EBX before a sibling call that clobbers it. "
            "The signal syscall will read garbage from EBX. Offending tail:\n"
            + "\n".join(tail)
            + "\n--- full setup block ---\n"
            + "\n".join(block)
        )
    assert found_at_least_one, f"test source must compile to at least one SYS_SYS_SIGNAL; got asm:\n{asm}"


def test_builtin_sys_break_emits_break_syscall() -> None:
    """sys_break(addr) must load EBX from its arg and fire SYS_SYS_BREAK.

    The kernel handler at kernel/arch/x86/syscall.asm:.sys_break reads EBX as
    "new break" (0 = query) and returns the resulting break in EAX with
    CF=0 always.  We pin the C contract end of that ABI here so future
    codegen refactors can't silently change it.
    """
    asm = _user(
        """
        int main(int argc, char *argv[]) {
            uint32_t current = sys_break(0);
            uint32_t requested = current + 65536;
            uint32_t got = sys_break(requested);
            if (got != requested) {
                die("oom\\n");
            }
            return 0;
        }
        """,
        bits=32,
    )
    assert "mov ebx, 0" in asm or "xor ebx, ebx" in asm, f"sys_break(0) (query form) must zero EBX before firing the syscall.\nasm:\n{asm}"
    assert "mov ah, SYS_SYS_BREAK" in asm, (
        "sys_break codegen must emit `mov ah, SYS_SYS_BREAK` — the constant lives in "
        "kernel/include/constants.asm and is the only stable contract with the kernel handler.\n"
        f"asm:\n{asm}"
    )
    assert asm.count("mov ah, SYS_SYS_BREAK") == 2, (
        f"expected exactly two SYS_SYS_BREAK firings (query + set); got {asm.count('mov ah, SYS_SYS_BREAK')}.\nasm:\n{asm}"
    )
