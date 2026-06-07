"""Unit tests for the pure AddressPlan dataclasses and helpers."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cc import ir
from cc.ast_nodes import SubscriptPlace
from cc.cli import _discover_include_paths, compile_module  # noqa: PLC2701 — the census needs the CLI's include discovery
from cc.codegen.address_plan import AddressPlan, AddressTerm, scale_encodes_in_operand
from cc.codegen.x86.generator import X86CodeGenerator
from cc.options import CompilerOptions

# Member accesses live in helper functions (not ``main``) because ``main``
# bypasses the IR lowering path — only IR-lowered bodies record Address ops.
ADDRESS_OF_SOURCE = """
struct node { int value; };
int *field_pointer(struct node *n) {
    int *p;
    p = &n->value;
    return p;
}
"""

ARROW_MEMBER_SOURCE = """
struct node { int value; struct node *next; };
int read_value(struct node *n) {
    int out;
    out = n->value;
    return out;
}
"""

ARROW_STORE_SOURCE = """
struct node { int value; };
void write_value(struct node *n, int v) {
    n->value = v;
}
"""

CHAINED_DOT_SOURCE = """
struct inner { int a; int b; };
struct outer { int pad; struct inner mid; };
struct outer g;
int reader() {
    int value;
    value = g.mid.b;
    return value;
}
int main() {
    return reader();
}
"""

#: Userland translation units the residual census compiles — the same corpus
#: ``tests/test_cc_function_sizes.py`` gates byte-for-byte.
CORPUS_SOURCE_GLOBS = ("user/libbboeos/*.c", "user/programs/*.c")

DEREF_STORE_BYTE_SOURCE = """
void write_byte(char *target, int value) {
    *target = value;
}
"""

DEREF_STORE_SOURCE = """
void write_through(int *target, int value) {
    *target = value;
}
"""

DOT_MEMBER_SOURCE = """
struct point { int x; int y; };
struct point g;
int reader() {
    int value;
    value = g.y;
    return value;
}
int main() {
    return reader();
}
"""

INCREMENT_SOURCE = """
struct counter { int hits; };
void bump(struct counter *c) {
    c->hits++;
}
"""

INDIRECT_CALL_SOURCE = """
void (*handlers[4])(void);
int next;
void run_last(void) {
    handlers[--next]();
}
"""

MIXED_CHAIN_STORE_SOURCE = """
struct symbol { int value; char name[8]; };
struct symbol table[4];
void write_name(int i, int j, int c) {
    table[i].name[j] = c;
}
"""

MULTIDIM_CONSTANT_SOURCE = """
int m[4][3];
int read_constant(void) {
    int out;
    out = m[2][1];
    return out;
}
"""

MULTIDIM_FRAME_BASE_SOURCE = """
int read_local(int i, int j) {
    int m[4][3];
    int out;
    m[0][0] = 7;
    out = m[i][j];
    return out;
}
"""

MULTIDIM_MEMBER_ARROW_SOURCE = """
struct grid { int pad; int cells[4][3]; };
int read_cell(struct grid *p, int i, int j) {
    int out;
    out = p->cells[i][j];
    return out;
}
"""

MULTIDIM_MEMBER_DOT_SOURCE = """
struct grid { int pad; int cells[4][3]; };
struct grid g;
int read_cell(int i, int j) {
    int out;
    out = g.cells[i][j];
    return out;
}
"""

MULTIDIM_SOURCE = """
int m[4][3];
int read_cell(int i, int j) {
    int out;
    out = m[i][j];
    return out;
}
"""

MULTIDIM_STORE_SOURCE = """
int m[4][3];
void write_cell(int i, int j, int leaf) {
    m[i][j] = leaf;
}
"""

MULTIDIM_THREE_DIMENSIONAL_SOURCE = """
int m[2][4][3];
int read_cell(int i, int j, int k) {
    int out;
    out = m[i][j][k];
    return out;
}
"""

MULTI_LEVEL_ARROW_SOURCE = """
struct inner { int a; int b; };
struct outer { int pad; struct inner mid; };
int reader(struct outer *p) {
    int value;
    value = p->mid.b;
    return value;
}
"""

POINTER_TO_ARRAY_SOURCE = """
int read_cell(int (*p)[3], int i, int j) {
    int out;
    out = p[i][j];
    return out;
}
"""

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


#: The locked residual census: repo-relative source -> unplanned Address count.
#: Files with zero residuals are omitted.
RESIDUAL_CENSUS_ALLOWLIST = {"user/programs/shell.c": 6}

SPANNING_TEMP_SOURCE = """
struct pair { int first; int second; };
int helper(int alpha, int beta, int gamma) {
    return alpha + beta + gamma;
}
int provoke(struct pair *pair_pointer, int seed) {
    return helper(seed * 3, seed * 5, pair_pointer->first);
}
"""

STRUCT_ARRAY_CONSTANT_SOURCE = """
struct entry { int key; int payload; };
struct entry table[8];
int read_constant(void) {
    int out;
    out = table[3].payload;
    return out;
}
"""

STRUCT_ARRAY_SOURCE = """
struct entry { int key; int payload; };
struct entry table[8];
int read_payload(int index) {
    int out;
    out = table[index].payload;
    return out;
}
"""

STRUCT_ARRAY_STORE_SOURCE = """
struct entry { int key; int payload; };
struct entry table[8];
void write_payload(int index, int leaf) {
    table[index].payload = leaf;
}
"""


def _corpus_residual_census() -> tuple[int, dict[str, list[ir.Address]]]:
    """Compile every corpus source; return (total Address ops, residual ops per file).

    Wraps ``lower_ir_body`` so the per-function ``_ir_address_ops`` dict (which
    is reset at the top of every body) is snapshotted after each body lowers,
    accumulating residual (legacy-recorded) ops across all functions of all
    translation units.  ``include/syscalls.h`` is generated first so the
    libbboeos sources resolve, mirroring ``tests/test_cc_function_sizes.py``.

    Note: a future corpus bitfield member access would appear here despite
    Load/Store consuming it natively — bitfield plans dual-record into
    ``_ir_address_ops`` as a ride-along for the legacy bitfield diagnostic
    path.  Split the allowlist semantics if that ever happens.
    """
    subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "generate_syscalls_h.py")], check=True)
    residual_by_file: dict[str, list[ir.Address]] = {}
    total_addresses = 0
    current_file = ""
    original = X86CodeGenerator.lower_ir_body

    def spy(self: X86CodeGenerator, body: list[ir.Instruction]) -> None:
        nonlocal total_addresses
        total_addresses += sum(isinstance(instruction, ir.Address) for instruction in body)
        original(self, body)
        residual_by_file.setdefault(current_file, []).extend(self._ir_address_ops.values())

    X86CodeGenerator.lower_ir_body = spy  # type: ignore[method-assign]
    try:
        for glob in CORPUS_SOURCE_GLOBS:
            directory, pattern = glob.rsplit("/", 1)
            for source in sorted((REPO_ROOT / directory).glob(pattern)):
                current_file = str(source.relative_to(REPO_ROOT))
                compile_module(
                    input_path=source,
                    options=CompilerOptions(bits=32, object_mode=True, per_function_sections=True),
                    search_paths=_discover_include_paths(extra_include_paths=(), input_path=source),
                )
    finally:
        X86CodeGenerator.lower_ir_body = original  # type: ignore[method-assign]
    return total_addresses, residual_by_file


def _generate(source_text: str, /, *, bits: int = 32) -> X86CodeGenerator:
    """Compile *source_text* and return the generator (plans inspectable)."""
    with tempfile.TemporaryDirectory(prefix="test_address_plan_") as work:
        source_path = Path(work) / "test.c"
        source_path.write_text(source_text)
        return compile_module(
            input_path=source_path,
            options=CompilerOptions(bits=bits),
            search_paths=(),
        )


def _generate_with_reseat_spy(source_text: str, /) -> tuple[X86CodeGenerator, int]:
    """Compile *source_text*; return the generator and the legacy re-seat count.

    Wraps ``_ir_address_with_index`` (the helper every legacy AST re-seat
    branch funnels through) with a counting spy, so a test can assert that a
    migrated terminal consumed its AddressPlan natively instead of rebuilding
    the source ``Place`` node from ``_ir_address_ops``.
    """
    calls: list[object] = []
    original = X86CodeGenerator._ir_address_with_index  # noqa: SLF001

    def spy(self: X86CodeGenerator, address_op: object, /) -> object:
        calls.append(address_op)
        return original(self, address_op)

    X86CodeGenerator._ir_address_with_index = spy  # type: ignore[method-assign] # noqa: SLF001
    try:
        generator = _generate(source_text)
    finally:
        X86CodeGenerator._ir_address_with_index = original  # type: ignore[method-assign] # noqa: SLF001
    return generator, len(calls)


def test_address_plan_defaults() -> None:
    """Default fields on a minimal ``AddressPlan`` have the expected zero values."""
    plan = AddressPlan(base="ebp-8", base_kind="frame")
    assert plan.bitfield is None
    assert plan.clobbers == frozenset()
    assert plan.decay_to_address is False
    assert plan.displacement == 0
    assert plan.terms == ()


def test_address_term_carries_value_and_scale() -> None:
    """``AddressTerm`` round-trips ``index_value`` and ``scale`` faithfully."""
    term = AddressTerm(index_value="_ir_3", scale=4)
    assert term.index_value == "_ir_3"
    assert term.scale == 4


def test_arrow_member_address_of_consumes_native_plan() -> None:
    """``p = &n->value`` consumes its pointer-base plan natively (no AST re-seat).

    The plan facts pin the deferred base; the re-seat spy proves the
    ``ir.AddressOf`` terminal ran the plan-driven path rather than rebuilding
    a ``PlaceAddressOf`` from ``_ir_address_ops``.  Byte-for-byte parity with
    the legacy emission is enforced by ``tests/test_cc_function_sizes.py``
    (``dirent.c`` is the live corpus consumer).
    """
    generator, reseat_count = _generate_with_reseat_spy(ADDRESS_OF_SOURCE)
    assert reseat_count == 0
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "pointer"
    assert plan.base == "n"
    assert plan.bitfield is None


def test_arrow_member_increment_decrement_consumes_native_plan() -> None:
    """``c->hits++`` consumes its pointer-base plan natively (no AST re-seat).

    The asm-shape assertions pin the legacy read-modify-write sequence the
    native path must reproduce: one register-base load, the 1-byte ``inc``,
    the register-base store, and the discarded-value reload + postfix
    recovery.  Byte-for-byte parity is enforced by
    ``tests/test_cc_function_sizes.py`` (``stdio.c`` ``_emit`` is the live
    corpus consumer).
    """
    generator, reseat_count = _generate_with_reseat_spy(INCREMENT_SOURCE)
    assert reseat_count == 0
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "pointer"
    assert plan.base == "c"
    body = generator.output.split("bump:")[1]
    lines = [line.strip() for line in body.splitlines()]
    assert lines.count("inc eax") == 1
    assert lines.count("mov [ebx], eax") == 1
    assert lines.count("mov eax, [ebx]") == 2  # rmw load + discarded reload
    assert lines.count("sub eax, 1") == 1  # postfix pre-update recovery


def test_arrow_member_load_plans_pointer_base() -> None:
    """``n->value`` plans a "pointer" base whose materialization is the SI-or-BX load."""
    generator = _generate(ARROW_MEMBER_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "pointer"
    assert plan.base == "n"
    assert plan.base_preserves_accumulator is True
    assert plan.base_is_static is False
    # ``_load_member_base`` either returns SI without emitting (the
    # si_local alias) or WRITES only BX; SI is read, never written, so
    # the declared materialization clobbers are exactly {"bx"}.
    assert plan.clobbers == frozenset({"bx"})


def test_arrow_member_store_consumes_native_plan() -> None:
    """``n->value = v`` plans an accumulator-preserving pointer base and stores natively.

    The plan facts pin the store ordering (rhs first, then the bare
    ``mov ebx, [n]`` base load); the asm-shape assertions pin the native
    materialization to exactly one base load and one register-base store,
    with the rhs evaluated BEFORE the base load (the accumulator-preserving
    ordering).  Byte-for-byte parity with the legacy path is enforced by
    ``tests/test_cc_function_sizes.py``.
    """
    generator = _generate(ARROW_STORE_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "pointer"
    assert plan.base_is_static is False
    assert plan.base_preserves_accumulator is True
    body = generator.output.split("write_value:")[1]
    lines = [line.strip() for line in body.splitlines()]
    base_loads = [line for line in lines if line.startswith("mov ebx, [")]
    assert base_loads == ["mov ebx, [ebp-4]"]
    assert lines.count("mov [ebx], eax") == 1
    assert lines.index("mov eax, [ebp-8]") < lines.index("mov ebx, [ebp-4]")
    # No spill: the accumulator-preserving ordering never saves the rhs
    # around the base load (a push/pop pair here would be a +2-byte
    # regression the byte gate would also catch corpus-wide).
    assert "push eax" not in lines


def test_chained_dot_member_load_records_no_address_op() -> None:
    """``g.mid.b`` never lowers to ``ir.Address`` — it stays on legacy ``ir.Access``.

    The IR lowering predicates cover dot, arrow, and arrow-then-dot member
    loads only; a ``MemberPlace(MemberPlace(VariablePlace))`` chained-dot read
    is not gated in, so there is no Address op (and hence no plan) to
    materialize.  The legacy chained arm in ``_resolve_member_place_info``
    handles it whole.
    """
    generator = _generate(CHAINED_DOT_SOURCE)
    assert generator._ir_address_ops == {}  # noqa: SLF001
    assert generator._ir_address_plans == {}  # noqa: SLF001


def test_deref_store_byte_pointer_plans_field_size_one() -> None:
    """``*target = value`` through a ``char *`` plans field_size 1 and stores one byte.

    The byte select mirrors the legacy named-pointer arm exactly
    (``pointee_type in BYTE_TYPES`` -> low-byte store), including its
    documented ``unsigned short *`` width gap — the plan's ``field_size``
    only ever carries 1 or the full ``int_size``.
    """
    generator, reseat_count = _generate_with_reseat_spy(DEREF_STORE_BYTE_SOURCE)
    assert reseat_count == 0
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base == "target"
    assert plan.base_kind == "pointer"
    assert plan.deref_store is True
    assert plan.field_size == 1
    body = generator.output.split("write_byte:")[1]
    lines = [line.strip() for line in body.splitlines()]
    assert lines.count("mov [esi], al") == 1
    assert "mov [esi], eax" not in lines


def test_deref_store_consumes_native_plan() -> None:
    """``*target = value`` through an ``int *`` plans and stores natively (no AST re-seat).

    The asm-shape assertions pin the legacy ordering the native path must
    reproduce: rhs first, then the pointer VALUE loaded into SI, then the
    full-accumulator register-base store — with no spill around the base
    load.  Byte-for-byte parity with the legacy emission is enforced by
    ``tests/test_cc_function_sizes.py`` (builtins.c / stdio.c / string.c /
    shell.c are the live corpus consumers).
    """
    generator, reseat_count = _generate_with_reseat_spy(DEREF_STORE_SOURCE)
    assert reseat_count == 0
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base == "target"
    assert plan.base_is_static is False
    assert plan.base_kind == "pointer"
    assert plan.base_preserves_accumulator is True
    assert plan.deref_store is True
    assert plan.field_size == 4
    # Materialization is the bare pointer-VALUE load into SI
    # (``_emit_load_var(..., register=si)``); the rhs evaluation and the
    # store itself are the ``ir.Store`` terminal's business.
    assert plan.clobbers == frozenset({"si"})
    body = generator.output.split("write_through:")[1]
    lines = [line.strip() for line in body.splitlines()]
    pointer_loads = [line for line in lines if line.startswith("mov esi, [")]
    assert pointer_loads == ["mov esi, [ebp-4]"]
    assert lines.count("mov [esi], eax") == 1
    assert lines.index("mov eax, [ebp-8]") < lines.index("mov esi, [ebp-4]")
    # No spill: the rhs stays live in the accumulator across the bare SI
    # pointer load (a push/pop pair here would be a +2-byte regression the
    # byte gate would also catch corpus-wide).
    assert "push eax" not in lines


def test_dot_member_load_produces_pure_displacement_plan() -> None:
    """``g.y`` lowers to one Address whose plan is a pure label+displacement."""
    generator = _generate(DOT_MEMBER_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "label"
    assert plan.displacement == 4  # offset of y
    assert plan.terms == ()
    assert plan.field_size == 4
    assert plan.clobbers == frozenset()  # static base, no terms: nothing emitted


def test_mixed_chain_store_plans_member_offset_and_two_terms() -> None:
    """``table[i].name[j] = c`` plans two accumulate terms with the member offset folded.

    The mixed chain is NOT a Horner plan: the legacy path runs the
    ``resolve_address`` recursion (struct-array arm + subscript arm), which
    accumulates each dynamic subscript via ``_accumulate_subscript`` (the
    second term's push/pop-add pattern).  The plan mirrors that as a
    multi-term ``subscript_terminal`` plan over the static array base.
    """
    generator = _generate(MIXED_CHAIN_STORE_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.horner is False
    assert plan.subscript_terminal is True
    assert [term.scale for term in plan.terms] == [12, 1]  # struct stride, char element
    assert plan.displacement == 4  # offset of name within struct symbol
    assert plan.field_size == 1
    # Each ``_accumulate_subscript`` term evaluates through AX and seeds /
    # sums BX (the second term's push/pop BX restores around the index
    # evaluation; BX is still written by the trailing ``add``).
    assert plan.clobbers == frozenset({"ax", "bx"})


def test_multi_level_arrow_load_plans_nested_plan_base() -> None:
    """``p->mid.b`` plans a "plan" base nesting the arrow plan for ``p->mid``.

    Mirrors the legacy chained arm of ``_resolve_member_place_info``: the
    inner ``p->mid`` struct-value member address is loaded (decayed ``lea``)
    and moved into BX, then the outer field offset rides the register base.
    """
    generator = _generate(MULTI_LEVEL_ARROW_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "plan"
    assert plan.base_is_static is False
    assert plan.base_preserves_accumulator is False
    assert plan.displacement == 4  # offset of b within struct inner
    # The chained base materializes the inner plan (its clobbers), loads
    # the decayed inner address through the accumulator
    # (``_emit_resolved_load``), then seeds BX — inner | {"ax", "bx"}.
    assert plan.clobbers == frozenset({"ax", "bx"})
    inner = plan.base
    assert isinstance(inner, AddressPlan)
    assert inner.base_kind == "pointer"
    assert inner.base == "p"
    assert inner.displacement == 4  # offset of mid within struct outer
    assert inner.decay_to_address is True  # struct-value member decays via lea
    assert inner.clobbers == frozenset({"bx"})  # the SI-or-BX base load writes only BX


def test_multidim_constant_indices_fold_into_displacement() -> None:
    """``m[2][1]`` folds every index into the displacement — Horner plan, no terms.

    The legacy Horner walk folds ``Int`` indices into the static displacement
    (``2 * 12 + 1 * 4``), so the plan must pre-fold them identically; the
    plan stays ``subscript_terminal`` because the legacy dispatch still
    routed the fully-folded shape through the protect-BX terminals.
    """
    generator = _generate(MULTIDIM_CONSTANT_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.horner is True
    assert plan.terms == ()
    assert plan.displacement == 28  # 2 * 12 + 1 * 4
    assert plan.subscript_terminal is True
    # Term-less static-base Horner: the walk and the SI tail both emit
    # nothing (the static-tail SI seed needs a dynamic index over a frame
    # base), so the materialization clobbers nothing.
    assert plan.clobbers == frozenset()


def test_multidim_frame_base_dynamic_index_declares_si_clobber() -> None:
    """A local ``m[i][j]`` declares {"ax", "bx", "si"}; its folded sibling declares nothing.

    The static-tail SI materialization (``[bp+bx]`` is illegal at 16-bit, so
    the frame base ``lea``'s into SI) fires exactly when the plan has a
    dynamic term over a frame base — both plan-time facts, so the clobber
    set is derived precisely rather than as a conservative union.  The
    constant-index store on the same array folds every term and emits
    nothing.
    """
    generator = _generate(MULTIDIM_FRAME_BASE_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 2
    folded_plan, dynamic_plan = plans
    assert folded_plan.base_kind == "frame"
    assert folded_plan.terms == ()
    assert folded_plan.clobbers == frozenset()
    assert dynamic_plan.base_kind == "frame"
    assert len(dynamic_plan.terms) == 2
    assert dynamic_plan.clobbers == frozenset({"ax", "bx", "si"})


def test_multidim_load_plans_horner_terms() -> None:
    """``m[i][j]`` plans a Horner plan with byte strides per dimension."""
    generator = _generate(MULTIDIM_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.horner is True
    assert [term.scale for term in plan.terms] == [12, 4]  # row stride, element
    assert plan.base_kind == "label"
    assert plan.base_always_in_register is False
    assert plan.subscript_terminal is True
    assert plan.element_size == 4
    assert plan.field_size == 4
    # Label-base static tail: the Horner walk uses AX + BX; no SI seed
    # (only a frame base with a dynamic index materializes into SI).
    assert plan.clobbers == frozenset({"ax", "bx"})


def test_multidim_member_arrow_load_plans_pointer_horner_base() -> None:
    """``p->cells[i][j]`` plans a Horner plan whose pointer base loads into SI."""
    generator = _generate(MULTIDIM_MEMBER_ARROW_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.horner is True
    assert plan.base_kind == "pointer"
    assert plan.base == "p"
    assert plan.base_always_in_register is True
    assert plan.base_is_static is False
    assert [term.scale for term in plan.terms] == [12, 4]
    assert plan.displacement == 4  # offset of cells within struct grid
    # Pointer-base Horner tail: the walk (AX + BX) plus the unconditional
    # pointer-VALUE load into SI.
    assert plan.clobbers == frozenset({"ax", "bx", "si"})


def test_multidim_member_dot_load_plans_register_seeded_base() -> None:
    """``g.cells[i][j]`` plans a Horner plan whose static base always seeds SI."""
    generator = _generate(MULTIDIM_MEMBER_DOT_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.horner is True
    assert plan.base_kind == "label"
    assert plan.base_always_in_register is True
    assert [term.scale for term in plan.terms] == [12, 4]
    assert plan.displacement == 4  # offset of cells within struct grid
    # Member-base Horner tail: the walk (AX + BX) plus the unconditional
    # ``lea si, [base]`` seed.
    assert plan.clobbers == frozenset({"ax", "bx", "si"})


def test_multidim_store_consumes_horner_plan() -> None:
    """``m[i][j] = leaf`` plans the same Horner terms and stores through the plan.

    The asm-shape assertions pin the native materialization to the legacy
    ``_emit_subscript_resolved_store`` sequence: the rhs spill before the
    Horner walk, the two scaled index pushes, and the indexed store through
    ``[_g_m+ebx]``.  Byte-for-byte parity is enforced by the byte gate and
    the BASE-vs-HEAD asm diff.
    """
    generator = _generate(MULTIDIM_STORE_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.horner is True
    assert [term.scale for term in plan.terms] == [12, 4]
    assert plan.subscript_terminal is True
    body = generator.output.split("write_cell:")[1]
    lines = [line.strip() for line in body.splitlines()]
    assert lines.count("mov [_g_m+ebx], eax") == 1  # indexed store terminal


def test_multidim_three_dimensional_load_plans_three_terms() -> None:
    """``m[i][j][k]`` over ``int m[2][4][3]`` plans three byte strides 48/12/4."""
    generator = _generate(MULTIDIM_THREE_DIMENSIONAL_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.horner is True
    assert [term.scale for term in plan.terms] == [48, 12, 4]


def test_plans_recorded_before_emission() -> None:
    """Every ir.Address plan is recorded before body emission begins.

    Mechanism: monkeypatch ``_plan_ir_address`` to snapshot ``len(self.lines)``
    at each call, and monkeypatch ``lower_ir_body`` to snapshot
    ``len(self.lines)`` at its entry point (the moment body emission starts).
    After compilation the test asserts that every ``_plan_ir_address`` call
    happened with a smaller line count than the corresponding ``lower_ir_body``
    entry — i.e. all planning occurred before any body instruction was emitted.

    With HEAD's lazy path the planning calls fire INSIDE ``lower_ir_body``, so
    their line-count snapshots are >= the entry snapshot; the assertion fails,
    confirming the test detects the pre-pass ordering requirement.  After the
    eager pre-pass lands the calls fire before the function label is emitted,
    making their snapshots strictly less than the entry snapshot.
    """
    plan_call_line_counts: list[int] = []
    lower_ir_body_entry_line_counts: list[int] = []
    original_plan = X86CodeGenerator._plan_ir_address  # noqa: SLF001
    original_lower = X86CodeGenerator.lower_ir_body

    def plan_spy(self: X86CodeGenerator, address_op: object, /) -> object:
        plan_call_line_counts.append(len(self.lines))
        return original_plan(self, address_op)

    def lower_spy(self: X86CodeGenerator, body: object, /) -> None:
        lower_ir_body_entry_line_counts.append(len(self.lines))
        original_lower(self, body)

    X86CodeGenerator._plan_ir_address = plan_spy  # type: ignore[method-assign] # noqa: SLF001
    X86CodeGenerator.lower_ir_body = lower_spy  # type: ignore[method-assign]
    try:
        _generate(ARROW_STORE_SOURCE)
    finally:
        X86CodeGenerator._plan_ir_address = original_plan  # type: ignore[method-assign] # noqa: SLF001
        X86CodeGenerator.lower_ir_body = original_lower  # type: ignore[method-assign]

    assert plan_call_line_counts, "no _plan_ir_address calls recorded — source has no ir.Address"
    assert lower_ir_body_entry_line_counts, "no lower_ir_body calls recorded"
    # Every planning call must precede body emission: its line-count snapshot
    # must be strictly less than the lowest lower_ir_body entry snapshot seen
    # across the entire compilation (functions are processed sequentially, so
    # the minimum entry snapshot bounds the earliest body-emission point).
    minimum_body_entry = min(lower_ir_body_entry_line_counts)
    for count in plan_call_line_counts:
        assert count < minimum_body_entry, (
            f"_plan_ir_address called at line count {count}, "
            f"but lower_ir_body entry was at {minimum_body_entry} — "
            "planning occurred during body emission (lazy path), not before it"
        )


def test_pointer_to_array_load_plans_outer_row_stride() -> None:
    """``p[i][j]`` over ``int (*p)[3]`` plans a pointer-base Horner plan.

    The outermost index strides by ``sizeof(int[3])`` (the whole pointee
    array); the base is the POINTER VALUE, loaded into SI by the
    materialization (``base_kind="pointer"`` + ``base_always_in_register``).
    """
    generator = _generate(POINTER_TO_ARRAY_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.horner is True
    assert plan.base_kind == "pointer"
    assert plan.base == "p"
    assert plan.base_always_in_register is True
    assert [term.scale for term in plan.terms] == [12, 4]
    assert plan.clobbers == frozenset({"ax", "bx", "si"})  # walk + SI pointer-value load


def test_residual_address_census_matches_allowlist() -> None:
    """Every unplanned corpus Address is the array-of-pointers subscript family, exactly 6 in shell.c.

    With exclusive plan recording, ``_ir_address_ops`` holds ONLY the residual
    (legacy-routed) ops.  The single residual family today is the
    array-of-pointers subscript chain (``pipe_left_argv[0][copy_index]`` and
    friends in shell.c): its mid-chain ELEMENT-POINTER LOAD — dereferencing
    ``name[i]`` to re-root the rest of the chain — has no phase-1 plan model
    (planning it needs emission's registered pointer-array element types
    folded into a chain-splitting plan extension), so
    ``_plan_subscript_chain`` declines and the legacy AST re-seat path owns
    the shape.

    A failure here means a new corpus shape stopped planning (add a planner
    arm, or — if the shape genuinely cannot plan in the phase-1 model —
    document it in ``_plan_ir_address`` and update
    ``RESIDUAL_CENSUS_ALLOWLIST`` deliberately).
    """
    total_addresses, residual_by_file = _corpus_residual_census()
    residual_counts = {source: len(address_ops) for source, address_ops in residual_by_file.items() if address_ops}
    assert residual_counts == RESIDUAL_CENSUS_ALLOWLIST
    for address_ops in residual_by_file.values():
        for address_op in address_ops:
            assert isinstance(address_op.shape, SubscriptPlace)
    residual_total = sum(residual_counts.values())
    assert total_addresses - residual_total > 0  # the planner covers the rest of the corpus


def test_scale_encodes_in_operand_16_bit_only_unscaled() -> None:
    """16-bit addressing has no SIB byte: only scale-1 (unscaled) is valid."""
    assert scale_encodes_in_operand(bits=16, scale=1)
    assert not scale_encodes_in_operand(bits=16, scale=2)
    assert not scale_encodes_in_operand(bits=16, scale=4)


def test_scale_encodes_in_operand_32_bit() -> None:
    """Powers-of-two scales 1/2/4/8 encode in 32-bit SIB; others do not."""
    assert scale_encodes_in_operand(bits=32, scale=1)
    assert scale_encodes_in_operand(bits=32, scale=4)
    assert scale_encodes_in_operand(bits=32, scale=8)
    assert not scale_encodes_in_operand(bits=32, scale=3)
    assert not scale_encodes_in_operand(bits=32, scale=32)


def test_struct_array_constant_index_folds_into_displacement() -> None:
    """``table[3].payload`` folds ``index * stride`` into the displacement — no term.

    A constant subscript still rides ``Address.indices`` (the IR builder
    pre-lowers every index leaf), so the planner must fold it exactly where
    the legacy ``_accumulate_subscript`` did: ``3 * sizeof(struct entry) +
    offsetof(payload)`` lands in ``displacement`` and ``terms`` stays empty.
    The plan keeps ``subscript_terminal`` so the constant-index store/load
    bytes (including the legacy unconditional rhs spill on stores) match.
    """
    generator = _generate(STRUCT_ARRAY_CONSTANT_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.terms == ()
    assert plan.displacement == 28  # 3 * sizeof(struct entry) + offset of payload
    assert plan.subscript_terminal is True
    # Term-less static plan: the materialization emits nothing — the
    # protect-BX guard the subscript terminal wraps around it is the
    # TERMINAL's emission, not the materialization's.
    assert plan.clobbers == frozenset()


def test_struct_array_member_load_plans_one_term() -> None:
    """``table[index].payload`` plans one dynamic term scaled by the struct size."""
    generator = _generate(STRUCT_ARRAY_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert len(plan.terms) == 1
    assert plan.terms[0].scale == 8  # sizeof(struct entry)
    assert plan.displacement == 4  # offset of payload
    # One ``_accumulate_subscript`` term: index through AX, seed BX.
    assert plan.clobbers == frozenset({"ax", "bx"})


def test_struct_array_member_store_plans_term_and_emits_indexed_store() -> None:
    """``table[index].payload = leaf`` plans one term and stores through ``[base+disp+ebx]``.

    The asm-shape assertions pin the native materialization to the legacy
    ``_emit_subscript_resolved_store`` sequence: the rhs is spilled before the
    index evaluation, exactly one ``mov ebx, eax`` seeds the index register,
    and the store goes through the indexed operand.  No ``push ebx`` guard is
    emitted (no variable is pinned to EBX here).  Byte-for-byte parity with
    the legacy path is enforced by ``tests/test_cc_function_sizes.py``.
    """
    generator = _generate(STRUCT_ARRAY_STORE_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert len(plan.terms) == 1
    assert plan.terms[0].scale == 8  # sizeof(struct entry)
    assert plan.displacement == 4  # offset of payload
    assert plan.subscript_terminal is True
    body = generator.output.split("write_payload:")[1]
    lines = [line.strip() for line in body.splitlines()]
    assert lines.count("mov ebx, eax") == 1  # exactly one index seed
    assert lines.count("mov [_g_table+4+ebx], eax") == 1  # indexed store terminal
    assert lines.count("push eax") == 1  # the rhs spill across the index eval
    assert "push ebx" not in lines  # no pinned-EBX guard needed here


def test_subscript_call_slot_plans_and_calls_natively() -> None:
    """``handlers[--next]()`` plans a call-slot Address and calls through it natively.

    The function-pointer slot of a bare array variable plans one
    pointer-size-scaled term over the array's static base, marked
    ``call_slot`` so only the ``ir.IndirectCall`` terminal materializes it.
    The asm-shape assertions pin the legacy ``generate_indexed_call``
    global-array sequence: SI base seed, index scale, slot load, and the
    indirect ``call``.  Byte-for-byte parity is enforced by
    ``tests/test_cc_function_sizes.py`` (``stdlib.c`` ``exit`` is the live
    corpus consumer).
    """
    generator, reseat_count = _generate_with_reseat_spy(INDIRECT_CALL_SOURCE)
    assert reseat_count == 0
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.call_slot is True
    assert plan.base_kind == "label"
    assert plan.base == "_g_handlers"
    assert [term.scale for term in plan.terms] == [4]  # pointer-size slot stride
    # Documented choice: call-slot plans declare NO materialization
    # clobbers — the IndirectCall terminal interleaves the slot walk with
    # the call and already saves under the conservative full-register-pool
    # call default, which governs the whole sequence.
    assert plan.clobbers == frozenset()
    body = generator.output.split("run_last:")[1]
    lines = [line.strip() for line in body.splitlines()]
    assert lines.count("lea esi, [_g_handlers]") == 1
    assert lines.count("shl eax, 2") == 1
    assert lines.count("mov eax, [eax]") == 1  # slot load
    assert lines.count("call eax") == 1


def test_temp_homes_avoid_member_store_base_clobber() -> None:
    """A temp live across an arrow-store terminal is never homed in BX.

    The store plan declares clobbers={'bx'} (``_load_member_base`` writes BX
    to seed the member base); the allocator must exclude BX from any temp
    live across — or read by — that Store.  Step-1 probe verdict: HEAD
    MISCOMPILES the live-across-Load twin of this constraint —
    ``SPANNING_TEMP_SOURCE`` homes the ``seed * 3`` temp in EBX, the planned
    arrow-member Load's materialization then runs ``mov ebx,
    [pair_pointer]`` (no guard exists for non-subscript member plans), and
    the call argument pushes the pointer instead of the product.  The
    spanning-a-Store variant cannot be constructed from C today (Store
    values are always leaves and temps never live across statement
    boundaries), so the Store fact is locked by a direct call into
    ``_instruction_clobber_registers`` while the compile fixture exercises
    the Load instance of the same ``_load_member_base`` clobber.
    """
    generator = _generate(SPANNING_TEMP_SOURCE)
    homes = generator.temp_pinned_registers
    # The fixture forces two interfering deferred-single-use temps
    # (``seed * 3`` / ``seed * 5``) live across the ``pair_pointer->first``
    # Load; both must be homed, and neither in the clobbered EBX (the
    # allocator relocates them onto EDX / EDI instead of spilling).
    assert "ebx" not in homes.values()
    assert len(homes) == 2  # both temps homed, neither spilled
    # Direct fact check for the Store terminal: an arrow-member store plan
    # declares the BX base-load clobber, and the helper reports it for the
    # consuming ``ir.Store``.
    store_generator = _generate(ARROW_STORE_SOURCE)
    (address_destination,) = store_generator._ir_address_plans  # noqa: SLF001
    store = ir.Store(address=address_destination, value=7, width=4)
    assert store_generator._instruction_clobber_registers(store) == frozenset({"bx"})  # noqa: SLF001
