"""x86 NASM code generator.

Consumes an AST produced by :class:`cc.parser.Parser` (with IR lowering
for most function bodies via :class:`cc.ir.Builder`) and emits NASM
assembly source.  ``X86CodeGenerator.generate`` returns the assembly
as a single string.

All mode-dependent decisions route through a :class:`cc.target.CodegenTarget`
instance; currently we ship :class:`X86CodegenTarget16` (real mode) and
:class:`X86CodegenTarget32` (flat protected mode).
"""

from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field, fields
from typing import ClassVar, NamedTuple

from cc import ir, regalloc
from cc.ast_nodes import (
    ArrayDecl,
    ArrayInit,
    Assign,
    BinaryOperation,
    Call,
    Cast,
    Char,
    Compound,
    Conditional,
    DereferencePlace,
    DoWhile,
    EnumDecl,
    ExtendedAsm,
    For,
    Function,
    If,
    Index,
    IndexAssign,
    InlineAsm,
    Int,
    LogicalAnd,
    LogicalOr,
    MemberPlace,
    Node,
    Param,
    Place,
    PlaceAddressOf,
    PlaceCall,
    PlaceIncrementDecrement,
    PlaceLoad,
    PlaceStore,
    SizeofExpr,
    SizeofType,
    SizeofVar,
    String,
    StructDecl,
    StructInitializer,
    SubscriptPlace,
    Switch,
    Var,
    VarDecl,
    VariablePlace,
    While,
    address_of_variable_name,
)
from cc.codegen.base import CodeGeneratorBase
from cc.codegen.liveness import LivenessAnalysisError, LivenessAnalyzer
from cc.codegen.x86.builtins import BuiltinsMixin
from cc.codegen.x86.emission import EmissionMixin
from cc.codegen.x86.jumps import (
    JUMP_WHEN_FALSE,
    JUMP_WHEN_FALSE_UNSIGNED,
    JUMP_WHEN_TRUE,
    JUMP_WHEN_TRUE_UNSIGNED,
)
from cc.codegen.x86.regalloc_inputs import build_allocator_inputs
from cc.errors import CompileError
from cc.options import CompilerOptions
from cc.target import LOW_BYTE, CodegenTarget, X86CodegenTarget16, X86CodegenTarget32
from cc.tokens import COMPARISON_OPERATIONS
from cc.types import ArrayType, PointerType, Type
from cc.utils import decode_string_escapes, string_byte_length

# Regexes used by the known_local_bytes tracker in _update_known_bytes.
# Each pattern matches a single line of NASM output that writes a byte
# immediate to a frame-relative slot of the form [ebp-N] or [ebp-N+M].
# K (the canonical frame-offset key) is N - M.
RE_AND_BYTE_LOCAL_IMMEDIATE = re.compile(r"^\s*and byte \[ebp-(\d+)(?:\+(\d+))?\], (\d+)\s*$")
RE_LOCAL_BYTE_ADDR = re.compile(r"^\[ebp-(\d+)(?:\+(\d+))?\]$")
RE_MOV_EAX_IMMEDIATE = re.compile(r"^\s*mov eax, (\d+)\s*$")
RE_MOV_BYTE_LOCAL_IMMEDIATE = re.compile(r"^\s*mov byte \[ebp-(\d+)(?:\+(\d+))?\], (\d+)\s*$")
RE_NON_BYTE_WRITE = re.compile(r"^\s*mov\b.*\[(?!ebp\b)")
RE_OR_BYTE_LOCAL_IMMEDIATE = re.compile(r"^\s*or byte \[ebp-(\d+)(?:\+(\d+))?\], (\d+)\s*$")


@dataclass(kw_only=True, slots=True)
class AutoPinEconomics:
    """The register-allocation economics gathered from a function body.

    The pure inputs both the legacy auto-pin heuristic and the regalloc
    adapter consume: which locals/params are pin-eligible, how often each is
    referenced (the spill benefit), how many subscript uses each has (the BP
    index penalty), and which pre-first-store call clobbers are elided per
    candidate/register.  ``byte_typed`` is the subset whose width has no 8-bit
    register alias outside AL/BL/CL/DL (so they may not be homed in DI/SI/BP).
    ``ranked`` is the candidates surviving expression-temporary and
    address-taken filtering, body locals first then params, sorted by
    descending reference count with declaration order as the tiebreaker.
    """

    address_taken: set[str] = field(default_factory=set)
    allocatable: frozenset[str] = field(default_factory=frozenset)
    byte_typed: frozenset[str] = field(default_factory=frozenset)
    index_uses: dict[str, int] = field(default_factory=dict)
    pre_store_clobbers: dict[str, dict[str, int]] = field(default_factory=dict)
    ranked: list[tuple[str, int]] = field(default_factory=list)
    reference_counts: dict[str, int] = field(default_factory=dict)


@dataclass(kw_only=True, slots=True)
class AutoPinTallyState:
    """Bundle of per-walk tallies for the auto-pin candidate scan.

    Carries the six dicts plus address-taken set and body-candidate
    list that :meth:`X86CodeGenerator._tally_auto_pin_counts` reads
    and writes during its recursive AST walk.  Replaces the closure
    scope the tally previously shared via nested ``def``.
    """

    address_taken: set[str] = field(default_factory=set)
    ax_resident_uses: dict[str, int] = field(default_factory=dict)
    body_candidates: list[tuple[str, int]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    index_uses: dict[str, int] = field(default_factory=dict)
    init_count: dict[str, int] = field(default_factory=dict)
    init_expr: dict[str, Node] = field(default_factory=dict)
    other_uses: dict[str, int] = field(default_factory=dict)


class FieldInfo(NamedTuple):
    """One struct field's layout.

    ``bit_offset`` and ``bit_width`` are populated for bitfield
    members (currently always ``None`` until Task 2.4 lands the
    bitfield-aware layout builder).  ``byte_offset`` is the field's
    start byte within the struct; ``field_size`` is the field's
    total byte size (``element_size * count`` for array fields,
    ``element_size`` for scalar fields).
    """

    bit_offset: int | None
    bit_width: int | None
    byte_offset: int
    element_size: int
    field_size: int
    type_name: str


@dataclass(kw_only=True, slots=True)
class MemoryOperand:
    """A machine memory operand ``[base (+displacement) (+index)]``.

    base_kind selects how *base* reads: a NASM label ("_g_arr"), a
    frame-relative string ("ebp-12"), or a register holding an address
    materialized by a prior dereference.  *displacement* sums member
    offsets and constant subscripts; *index* (when not None) is a register
    holding the summed dynamic byte-offset.  *field_size* / *element_size*
    size the terminal load/store (field_size != element_size marks an
    array-typed member whose load decays to its address via ``lea``).

    *bitfield* carries the :class:`FieldInfo` of a bitfield member so the
    terminal emits the mask/shift read or read-modify-write sequence.
    *raw_width* marks the ``base.field[i]`` member-index operand whose load
    uses a plain ``mov`` for a word element (no ``movzx`` promotion), matching
    the legacy member-index lowerer.
    """

    base: str
    base_kind: str  # "label" | "frame" | "register"
    bitfield: FieldInfo | None = None
    decay_to_address: bool = False  # bare array / struct-value member: load yields its address
    displacement: int = 0
    element_size: int = 0
    field_size: int = 0
    index: str | None = None
    raw_width: bool = False  # member-index load uses plain mov (no word->movzx promotion)


class X86CodeGenerator(BuiltinsMixin, EmissionMixin, CodeGeneratorBase):
    """Generates NASM x86 assembly from the parsed AST.

    Composed from concern-specific mixins (``BuiltinsMixin``,
    ``EmissionMixin``) alongside the arch-agnostic
    ``CodeGeneratorBase``.  Put the mixins before
    ``CodeGeneratorBase`` so MRO resolves their overrides first,
    though none of them override base methods today.  The peephole
    pass is a standalone collaborator (:class:`cc.codegen.x86.peephole.Peepholer`)
    rather than a mixin — it runs as a post-processing stage over
    the finished line buffer and has no need to share per-statement
    state with the generator.
    """

    BUILTIN_CLOBBERS: ClassVar[dict[str, frozenset[str]]] = {
        "__builtin_va_arg": frozenset({"ax", "bx"}),
        "__builtin_va_copy": frozenset({"ax"}),
        "__builtin_va_end": frozenset(),
        "__builtin_va_start": frozenset({"ax", "bx"}),
        "_exit": frozenset({"ax"}),
        "alarm_ms": frozenset({"ax", "bx", "cx"}),
        "asm": frozenset({"ax", "bx", "cx", "dx", "si", "di"}),
        "checksum": frozenset({"ax", "bx", "cx", "si"}),
        "chmod": frozenset({"ax", "si"}),
        "close": frozenset({"ax", "bx"}),
        "datetime": frozenset({"ax"}),
        "die": frozenset(),
        "dup": frozenset({"ax", "bx"}),
        "dup2": frozenset({"ax", "bx", "dx"}),
        "exec": frozenset({"ax", "si"}),
        "exit": frozenset(),
        "far_read16": frozenset({"ax", "bx"}),
        "far_read32": frozenset({"ax", "bx"}),
        "far_read8": frozenset({"ax", "bx"}),
        "far_write16": frozenset({"ax", "bx"}),
        "far_write32": frozenset({"ax", "bx"}),
        "far_write8": frozenset({"ax", "bx"}),
        "fill_block": frozenset({"ax", "bx", "cx", "dx"}),
        "fstat": frozenset({"ax", "bx", "cx", "dx"}),
        "getchar": frozenset({"ax"}),
        "getdents": frozenset({"ax", "bx", "cx", "di"}),
        "kernel_inb": frozenset({"ax", "dx"}),
        "kernel_insw": frozenset({"ax", "cx", "di", "dx"}),
        "kernel_inw": frozenset({"ax", "dx"}),
        "kernel_outb": frozenset({"ax", "dx"}),
        "kernel_outsw": frozenset({"ax", "cx", "dx", "si"}),
        "kernel_outw": frozenset({"ax", "dx"}),
        "mac": frozenset({"ax", "di"}),
        "memcmp": frozenset({"ax", "cx", "di", "dx", "si"}),
        "memcpy": frozenset({"ax", "cx", "di", "si"}),
        "memset": frozenset({"ax", "cx", "di"}),
        "mkdir": frozenset({"ax", "si"}),
        "net_open": frozenset({"ax", "dx"}),
        "open": frozenset({"ax", "dx", "si"}),
        "parse_ip": frozenset({"ax", "di", "si"}),
        "pipeline2": frozenset({"ax", "cx", "di", "dx", "si"}),
        "print_datetime": frozenset({"ax"}),
        "print_ip": frozenset({"ax", "cx", "si"}),
        "print_mac": frozenset({"ax", "cx", "si"}),
        "printf": frozenset({"ax", "bx", "cx", "dx", "si", "di"}),
        "putchar": frozenset({"ax"}),
        "read": frozenset({"ax", "bx", "cx", "di"}),
        "reboot": frozenset({"ax"}),
        "recvfrom": frozenset({"ax", "bx", "cx", "di", "dx"}),
        "rename": frozenset({"ax", "di", "si"}),
        "rmdir": frozenset({"ax", "si"}),
        "seek": frozenset({"ax", "bx", "cx"}),
        "sendto": frozenset({"ax", "bx", "cx", "di", "dx", "si"}),
        "set_palette_color": frozenset({"ax", "bx", "cx", "dx"}),
        "setsockopt": frozenset({"ax", "bx", "cx"}),
        "shutdown": frozenset({"ax"}),
        "signal": frozenset({"ax", "bx", "cx"}),
        "sleep": frozenset({"ax", "cx"}),
        "strlen": frozenset({"ax", "cx", "di"}),
        "sys_break": frozenset({"ax", "bx"}),
        "unlink": frozenset({"ax", "si"}),
        "uptime": frozenset({"ax"}),
        "uptime_ms": frozenset({"ax"}),
        "video_mode": frozenset({"ax", "bx", "dx"}),
        "write": frozenset({"ax", "bx", "cx", "si"}),
    }

    ERROR_RETURNING_BUILTINS: ClassVar[frozenset[str]] = frozenset({"chmod", "mac", "mkdir", "parse_ip", "rename", "rmdir", "unlink"})

    def __init__(
        self,
        options: CompilerOptions | None = None,
        *,
        constant_values: dict[str, int] | None = None,
        defines: dict[str, str] | None = None,
    ) -> None:
        """Initialize code generator state.

        ``options`` (:class:`CompilerOptions`) carries the compiler knobs
        — ``bits``, ``object_mode``, ``per_function_sections``,
        ``permissive``, ``target_mode`` — that flow together from the
        CLI; they are unpacked into locals below so the body reads them
        by their bare names.  ``None`` falls back to the defaults.

        ``options.bits`` selects the target: 16 → ``X86CodegenTarget16``,
        32 → ``X86CodegenTarget32``.  All mode-dependent decisions
        (register names, operand widths, type sizes, kernel ABI) live
        in the target object.  The arch-agnostic state
        (symbol tables, output buffer, counters, BBoeOS constant
        tables) is initialized by ``CodeGeneratorBase.__init__``;
        this class adds the x86-specific trackers — accumulator
        aliasing, the DX:AX remainder cache, the pinned-register
        and register-aliased-global dicts (x86 register names), and
        the store-target hint used by the pinned-destination
        peephole.

        ``constant_values`` maps NASM constant names (from
        ``constants.asm``) to their evaluated integer values and is used
        by :meth:`_eval_local_array_size` to size stack-local arrays
        whose element counts are named constants.  When omitted or
        ``None`` the generator falls back to the empty mapping.

        ``object_mode`` is True when the caller wants object-file-friendly
        NASM (section directives, CCREL_* marker macros, no flat-binary org
        or BSS trailer).  Default False preserves flat-binary emission.

        ``target_mode`` is either ``"user"`` (default, stand-alone program
        at ``PROGRAM_BASE``) or ``"kernel"`` (bare assembly for ``%include``
        into the kernel blob: no ``org``, no ``_program_end``, no BSS
        trailer, no ``int 30h`` self-call builtins).
        """
        if options is None:
            options = CompilerOptions()
        # Unpack the knobs into the same local names the body already
        # uses, so threading them as one object changes only this line
        # block — not the dozens of `self.<knob>` reads below.
        bits = options.bits
        object_mode = options.object_mode
        per_function_sections = options.per_function_sections
        permissive = options.permissive
        target_mode = options.target_mode
        if bits not in (16, 32):
            message = f"unsupported bits={bits}; expected 16 or 32"
            raise ValueError(message)
        if target_mode not in ("user", "kernel"):
            message = f"unsupported target_mode={target_mode!r}; expected 'user' or 'kernel'"
            raise ValueError(message)
        target: CodegenTarget = X86CodegenTarget32() if bits == 32 else X86CodegenTarget16()
        super().__init__(constant_values=constant_values, defines=defines, target=target)
        # Materialise the per-target clobber table once at init.  The
        # class-level BUILTIN_CLOBBERS table is 32-bit-correct; targets
        # that need extras (16-bit declares ``BUILTIN_CLOBBERS_EXTRA``
        # for the long-shape adapter glue around RTC syscalls) augment
        # by name.  Plain ``dict |`` overrides on key collision rather
        # than unioning the values, so patch only the overlapping keys
        # instead of recomputing a no-op union for every entry.  Both
        # lookup sites (per-call-site emit, whole-program pinning-cost
        # pass) hit this table once per builtin call site, but it
        # never changes for the lifetime of the generator.
        target_extra: dict[str, frozenset[str]] = getattr(target, "BUILTIN_CLOBBERS_EXTRA", {})
        self._builtin_clobbers: dict[str, frozenset[str]] = dict(self.BUILTIN_CLOBBERS)
        for name, extra in target_extra.items():
            self._builtin_clobbers[name] |= extra
        self.array_types: dict[str, ArrayType] = {}  # name → structured ArrayType for multidim arrays
        self.asm_symbol_globals: dict[str, str] = {}  # name → asm symbol (no _g_ prefix)
        self.extern_globals: set[str] = set()  # names declared with `extern` (storage lives in another translation unit)
        self.extern_functions: set[str] = set()  # functions declared but not defined in this translation unit
        # Subset of extern_functions whose name matches a FUNCTION_<NAME>_PTR
        # constant in constants.asm: these are libbboeos exports and resolve
        # via `call [FUNCTION_<NAME>_PTR]` (cdecl indirect) instead of a
        # direct/CCREL call.  Populated by the prototype-registration loop
        # in EmissionMixin and consumed by the Call AST visitor.  A bare
        # libbboeos call without a prior prototype declaration is a
        # CompileError under --target user — strict-on-libbboeos hygiene.
        self.libbboeos_extern_declarations: dict[str, int] = {}
        self.ax_is_byte: bool = False
        self.ax_literal: int | None = None
        self.ax_local: str | None = None
        self.bss_total: int | str = 0  # total BSS bytes; int when all literal, str EQU name otherwise
        self.bss_vars: list[tuple[str, str]] = []  # (name, byte_count_expr) for zero-init globals
        self.division_remainder: tuple | None = None
        # Object-mode-only: zero-init locals from elide_frame functions
        # (e.g. main's static-storage locals).  In flat mode these are
        # emitted inline at the tail of the function body; in object
        # mode they're laid down in section .bss via `resb` so .text
        # stays code-only.  Each entry: (vname, byte_count_expr) — same
        # shape as bss_vars but with an `_l_` prefix at emit time.
        self.elided_local_bss_vars: list[tuple[str, str]] = []
        # in_register_params / out_register_params map function name → {param_index → register}.
        # Populated during the first pass over function definitions in generate().
        self.in_register_params: dict[str, dict[int, str]] = {}
        self.object_mode: bool = object_mode
        self.per_function_sections: bool = per_function_sections
        # permissive: relax bboeos house-style comparison strictness so
        # unmodified third-party C (kilo, lua, Doom) compiles — integer 0
        # counts as a null-pointer constant, `if (p)` and `c != 0` are
        # accepted.  See validate_comparison_types.  Set via --permissive.
        self.permissive: bool = permissive
        self.out_register_params: dict[str, dict[int, str]] = {}
        self.param_in_register: dict[str, str] = {}
        self.pinned_register: dict[str, str] = {}
        self.output: str = ""  # emitted NASM, populated by generate()
        self.register_homes: dict[str, dict[str, str]] = {}  # function name -> {var: register}; always_inline functions are absent
        # Liveness map for pinned-register saves: maps id(ir.Call /
        # ir.CarryBranch) → frozenset of pinned-register names that are
        # may-defined at that call site.  Populated per function before
        # IR lowering by _compute_pinned_initialized_per_call.
        # _pinned_registers_to_save consults this to skip saves for
        # pinned locals whose value isn't yet meaningful (e.g.,
        # auto-pinned locals declared but not yet stored to).  None
        # means "no info available" — fall back to saving everything.
        self._ir_call_pinned_initialized: dict[int, frozenset[str]] = {}
        self._current_call_pinned_initialized: frozenset[str] | None = None
        # IR temps that won a pool-register home in _allocate_ir_temps.
        # Maps temp name -> register.  Distinct from auto-pinned locals
        # (which _compute_pinned_initialized_per_call tracks): temps are
        # single-assignment, so their save-liveness is a simple
        # def-index < call-index <= last-use-index test, computed by
        # _compute_temp_pinned_live_per_call and folded into the
        # per-call filter so _pinned_registers_to_save saves a temp's
        # register exactly across the calls / rep-string-ops it lives
        # across.  Reset per function in _allocate_ir_temps.
        self.temp_pinned_registers: dict[str, str] = {}
        # pointer_array_types maps a variable / parameter name → the structured
        # PointerType(ArrayType(...)) for a pointer-to-array (``int (*p)[3]``)
        # or a decayed multidim array parameter (``int m[][3]`` ==
        # ``int (*m)[3]``).  variable_types[name] still carries a flat pointer
        # string so legacy pointer-ness (width, deref) holds; this structured
        # dict drives subscript addressing and sizeof.  Consulted BEFORE the
        # legacy string / array_types paths.
        self.pointer_array_types: dict[str, PointerType] = {}
        self.register_aliased_globals: dict[str, str] = {}  # name → register (e.g. "si")
        self.store_target_register: str | None = None
        # known_local_bytes and _last_byte_store support the Phase C
        # peephole tracker.  Seeded empty here; reset per function in
        # generate_function (emission.py).  _last_byte_store records
        # the most recently emitted qualifying mov-byte-immediate so
        # that peepholes can fold it; known_local_bytes tracks the
        # last-known constant byte value at each frame offset K.
        self.known_local_bytes: dict[int, int] = {}
        self._last_byte_store: tuple[int, int] | None = None
        # struct_layouts maps struct tag name → {field_name: FieldInfo}.
        # Populated by _register_globals when StructDecl nodes are encountered.
        self.struct_layouts: dict[str, dict[str, FieldInfo]] = {}
        self.struct_sizes: dict[str, int] = {}
        self.target_mode: str = target_mode
        # When BBOE_REGALLOC=1 the per-function home decision routes through
        # cc.regalloc.color() (see _allocator_homes) instead of the legacy
        # _select_auto_pin_candidates heuristic.  Read once at construction;
        # compile_source_homes builds a fresh generator per compile so the
        # env var is honored per call.
        self.use_regalloc: bool = os.environ.get("BBOE_REGALLOC") == "1"
        self.regalloc_liveness_fallbacks: int = 0

    def _accumulate_subscript(self, operand: MemoryOperand, /, *, index: Node, element_size: int) -> None:
        """Add *index * element_size* into *operand*, folding constants into displacement.

        Constant indices (ast_nodes.Int) fold into *operand.displacement*.
        Dynamic indices are evaluated into AX, scaled by *element_size* using
        :meth:`_emit_scale_index`, and accumulated into a BX index register so
        that multiple dynamic subscripts on the same base sum correctly (the
        struct-array ``arr[i].member[j]`` shape uses the same ``add bx, ax``
        accumulation pattern).
        """
        if isinstance(index, Int):
            operand.displacement += index.value * element_size
            return
        bx = self.target.bx_register
        if operand.index is not None:
            self.emit(f"        push {bx}")
        self.generate_expression(index)  # AX = dynamic index
        self._emit_scale_index(self.target.acc, scale=element_size)  # AX = byte offset
        if operand.index is not None:
            self.emit(f"        pop {bx}")
            self.emit(f"        add {bx}, {self.target.acc}")  # BX = accumulated byte offset
        else:
            self.emit(f"        mov {bx}, {self.target.acc}")  # BX = byte offset
            operand.index = bx

    def _accumulate_subscript_on_register(self, operand: MemoryOperand, /, *, index: Node) -> None:
        """Fold a subscript onto a register-based operand (a dereferenced pointer base).

        The base segment's pointer is live in the accumulator (set by
        :meth:`_resolve_dereference`).  Move it to the ESI index base, then add
        ``index * element_size`` onto ESI so the terminal load / store reads
        ``[esi (+displacement)]``.  This reproduces the byte sequence the retired
        ``_emit_double_index_place_load`` emitted for ``name[outer][inner]`` over
        an array of pointers: a constant inner index folds into the operand
        displacement; a :class:`Var` index evaluates into AX and adds onto ESI
        without a save; any other expression saves / restores ESI around its
        evaluation (the general subexpression may itself clobber ESI).
        """
        si = self.target.si_register
        accumulator = self.target.acc
        element_size = operand.element_size or operand.field_size
        self.emit(f"        mov {si}, {accumulator}")
        operand.base = si  # base_kind stays "register" per the docstring invariant; only base changes from acc to SI.
        if isinstance(index, Int):
            operand.displacement += index.value * element_size
            return
        if isinstance(index, Var):
            self.generate_expression(index)
            self._emit_scale_index(accumulator, scale=element_size)
            self.emit(f"        add {si}, {accumulator}")
            return
        self.emit(f"        push {si}")
        self.generate_expression(index)
        self._emit_scale_index(accumulator, scale=element_size)
        self.emit(f"        pop {si}")
        self.emit(f"        add {si}, {accumulator}")

    def _allocator_homes(
        self,
        *,
        apply_liveness_elision: bool = True,
        body: list[Node],
        parameters: list,
        precolored: dict[str, str],
    ) -> dict[str, str]:
        """Color locals/params with cc.regalloc; return {name: register} homes.

        Drop-in replacement for :meth:`_select_auto_pin_candidates`: same economics,
        but coloring (which generalizes the heuristic's primary + sharing passes)
        decides homes.  Interference comes from the AST LivenessAnalyzer; on
        LivenessAnalysisError every allocatable value is treated as mutually
        interfering (no illegal sharing), and the byte gate catches any cost.

        ``apply_liveness_elision`` mirrors the heuristic's ``main`` carve-out: the
        AST codegen path always emits pinned-register saves around calls, so for
        ``main`` the pre-first-store clobber elision must not be subtracted (it
        would make those saves look free and over-pin).  When ``False`` the
        economics are rebuilt with empty ``pre_store_clobbers`` before the cost
        model is derived.
        """
        if not self.safe_pin_registers:
            return {}
        economics = self._compute_pin_economics(body=body, parameters=parameters)
        if not economics.allocatable:
            return {}
        if not apply_liveness_elision:
            economics = dataclasses.replace(economics, pre_store_clobbers={})
        try:
            interference = LivenessAnalyzer(body=body, parameters=parameters).interference()
        except LivenessAnalysisError:
            self.regalloc_liveness_fallbacks += 1
            interference = {name: set(economics.allocatable) - {name} for name in economics.allocatable}

        pool = tuple(self.safe_pin_registers)
        byte_pool = frozenset(register for register in pool if register in LOW_BYTE)
        base_register = self.target.base_register if self.elide_frame else None
        argument_affinity = self._compute_argument_register_affinity(body=body, parameters=parameters)
        inputs = build_allocator_inputs(
            argument_affinity=argument_affinity,
            base_register=base_register,
            byte_pool=byte_pool,
            economics=economics,
            interference=interference,
            pool=pool,
            precolored=precolored,
            register_clobber_counts=self.register_clobber_counts,
        )
        allocation = regalloc.color(
            constraints=inputs.constraints,
            costs=inputs.costs,
            interference=inputs.interference,
            moves=set(),  # no coalescing: AST locals/params have no IR Copy pairs; coalescing is PR 3's IR-temp concern
        )
        return {name: register for name, register in allocation.homes.items() if name in economics.allocatable}

    def _analyze_user_function_conventions(self, functions: list[Node], /) -> None:
        """Pre-compute each user function's pinned-param register map.

        Runs the same pin-selection logic that :meth:`generate_function`
        uses, but purely analytically — no code is emitted.  The result
        populates :attr:`user_function_pin_params` so call-site emission
        knows which registers every callee expects.

        A function also qualifies for the register calling convention
        (added to :attr:`register_convention_functions`) when every
        call to it in the program passes only simple arguments (``Int``,
        ``String``, or ``Var``).  Complex-expression arguments would
        require ordering complex eval against register-moves without
        clobbering caller pins, so those callees fall back to the
        stack convention.
        """
        self.user_function_pin_params: dict[str, dict[int, str]] = {}
        self.register_convention_functions: set[str] = set()

        for function in functions:
            if function.name == "main" or function.is_prototype:
                continue
            self.safe_pin_registers = self.compute_safe_pin_registers(function.body, parameters=function.params)
            # Fastcall param 0 lives in AX on entry and is spilled
            # to a local stack slot in the prologue; it never becomes a pin
            # candidate so auto-pin selection skips it entirely.  Params 1..N
            # of a fastcall function keep the standard stack convention in the
            # MVP — they don't mix with register_convention.
            all_params = function.params
            if function.regparm_count > 0:
                pin_params = [p for p in all_params[1:] if p.out_register is None and p.in_register is None]
            else:
                pin_params = [p for p in all_params if p.out_register is None and p.in_register is None]
            assignments = self._select_auto_pin_candidates(body=function.body, parameters=pin_params)
            param_pins: dict[int, str] = {}
            for index, param in enumerate(all_params):
                if function.regparm_count > 0 and index == 0:
                    continue
                if param.out_register is not None or param.in_register is not None:
                    continue
                if param.name in assignments:
                    param_pins[index] = assignments[param.name]
            self.user_function_pin_params[function.name] = param_pins

        has_complex_call: dict[str, bool] = dict.fromkeys(self.user_functions, False)

        def visit(node: Node) -> None:
            if (
                isinstance(node, Call)
                and node.name in self.user_functions
                and len(node.args) > 1
                and any(not self._is_simple_arg(arg) for arg in node.args)
            ):
                # 1-arg fastcall calls take the ``emit_register_from_argument``
                # path (any expression OK); the register-convention auto-pin
                # is only at risk when multiple args could clobber each other.
                has_complex_call[node.name] = True
            for node_field in fields(node):
                value = getattr(node, node_field.name)
                if isinstance(value, Node):
                    visit(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, Node):
                            visit(item)

        for function in functions:
            for statement in function.body:
                visit(statement)

        for name, pins in self.user_function_pin_params.items():
            if name in self.fastcall_functions:
                # Fastcall and register_convention are mutually exclusive
                # in the MVP: the former passes arg 0 in AX and the rest
                # via the standard stack; the latter piggybacks on
                # auto-pinned params.  Skip the register_convention
                # promotion so call sites take the fastcall path.
                continue
            if pins and not has_complex_call.get(name):
                self.register_convention_functions.add(name)

    def _arg_pinned_sources(self, arg: Node, /) -> set[str]:
        """Return caller-pinned registers read while evaluating *arg*.

        Used by :meth:`_emit_register_arg_moves` to schedule arg loads
        without overwriting a register that another arg still needs.
        Walks ``Var``/``BinaryOperation`` recursively; non-leaf nodes outside
        the simple-arg shape contribute no sources (and would be
        rejected by :meth:`_is_simple_arg` upstream anyway).
        """
        if isinstance(arg, Var):
            if arg.name in self.pinned_register:
                return {self.pinned_register[arg.name]}
            if arg.name in self.param_in_register:
                return {self.param_in_register[arg.name]}
            return set()
        if isinstance(arg, BinaryOperation):
            return self._arg_pinned_sources(arg.left) | self._arg_pinned_sources(arg.right)
        return set()

    def _arithmetic_element_size(self, var_name: str, /) -> int:
        """Return the element stride for pointer/array arithmetic on *var_name*.

        ``ptr + N`` scales ``N`` by the pointed-to element's byte size so that
        ``struct fd *p; p + 1`` advances by ``sizeof(struct fd)`` rather than 1.

        Rules:
        - Array variables (in ``variable_arrays``): element size is the declared
          element type's byte size.
        - Pointer variables (type ends with ``*``): element size is the
          pointed-to type's byte size.
        - Byte types (``char``, ``unsigned char``) always return 1 so byte-string
          arithmetic is never scaled.
        - Unknown or non-pointer scalars: return 1 (no scaling).
        """
        type_name = self.variable_types.get(var_name, "")
        if var_name in self.variable_arrays:
            # Array: element type is the stored type_name directly.
            if type_name in self.BYTE_TYPES:
                return 1
            if type_name.startswith("struct "):
                tag = type_name[7:]
                if tag not in self.struct_sizes:
                    message = f"unknown struct '{tag}'"
                    raise CompileError(message)
                return self.struct_sizes[tag]
            return self.target.type_size(type_name)
        if type_name.endswith("*"):
            base = type_name[:-1]
            if base in self.BYTE_TYPES:
                return 1
            if base.startswith("struct "):
                tag = base[7:]
                if tag not in self.struct_sizes:
                    message = f"unknown struct '{tag}'"
                    raise CompileError(message)
                return self.struct_sizes[tag]
            return self.target.type_size(base)
        return 1

    @staticmethod
    def _build_address(base: str, offset: int, /, *, index: str = "") -> str:
        """Return ``[base+offset+index]``, collapsing zero ``offset``.

        Every NASM memory-operand site that adds a literal byte offset
        to a base operand (frame slot, register, label) goes through
        here so the ``[base+0]`` case stays out of the emitted text.
        ``index`` is appended after ``offset`` for the constant-base /
        register-index addresses emitted by the ``resolve_address`` /
        ``_emit_place_*`` struct-array path (``[label+12+bx]``).
        """
        parts = [base]
        if offset:
            parts.append(str(offset))
        if index:
            parts.append(index)
        return f"[{'+'.join(parts)}]"

    def _bx_holds_pinned_var(self) -> bool:
        """Return True if any variable is auto-pinned to BX/EBX.

        The struct-array-indexing generators clobber BX as a scratch
        register for the byte offset.  When BX also holds a pinned
        function parameter or local, the clobber loses the variable's
        value — subsequent reads emit ``mov ax, bx`` (or ``mov eax,
        ebx``) and pick up the offset instead of the value.  Callers
        wrap the clobber with ``push bx``/``pop bx`` when this helper
        returns True.
        """
        return any(reg == self.target.bx_register for reg in self.pinned_register.values())

    def _byte_index_direct(self, node: Index, /) -> str | None:
        """Return a direct NASM memory operand for a constant-base Index.

        When the base is a named constant or constant alias, returns
        e.g. ``"BUFFER+128+12"`` without emitting any instructions.
        Returns ``None`` for runtime (non-constant) bases.
        """
        vname = node.array.name
        const_base = self._resolve_constant(vname)
        if const_base is None:
            return None
        offset = node.index.value
        return f"{const_base}+{offset}" if offset else const_base

    def _clobbers_for_call(self, call_node: Call, /, *, function_pointer_vars: set[str]) -> tuple[str, ...] | frozenset[str]:
        """Mirror :meth:`compute_safe_pin_registers`'s per-call clobber set."""
        if call_node.name in self.user_functions or call_node.name in function_pointer_vars:
            return self.target.register_pool
        if call_node.name in self.libbboeos_extern_declarations:
            return self.target.register_pool
        if call_node.name in self._builtin_clobbers:
            return self._builtin_clobbers[call_node.name]
        return ()

    @staticmethod
    def _collect_asm_operand_vars(body: list[Node], /) -> set[str]:
        """Return variable names that appear in ExtendedAsm operands.

        Variables referenced by inline asm operands (especially memory
        constraints ``=m`` / ``m``) must not be auto-pinned to registers
        because the asm template may require a memory operand.
        """
        result: set[str] = set()

        def _walk(statements: list[Node]) -> None:
            for statement in statements:
                if isinstance(statement, ExtendedAsm):
                    for operand in (*statement.inputs, *statement.outputs):
                        if isinstance(operand.expression, Var):
                            result.add(operand.expression.name)
                elif isinstance(statement, (Compound, DoWhile, While)):
                    _walk(statement.body)
                elif isinstance(statement, For):
                    _walk(statement.init)
                    _walk(statement.body)
                elif isinstance(statement, If):
                    _walk(statement.body)
                    if statement.else_body is not None:
                        _walk(statement.else_body)
                elif isinstance(statement, Switch):
                    for case in statement.cases:
                        _walk(case.body)

        _walk(body)
        return result

    def _collect_auto_pin_body_candidates(
        self, statements: list[Node], /, *, body_candidates: list[tuple[str, int]], top_level: bool
    ) -> None:
        """Walk *statements*, appending eligible VarDecls to *body_candidates*.

        Recurses into Compound/DoWhile/While/For/If/Switch bodies.  The
        ``top_level`` flag controls constant-alias filtering: only
        directly-enclosed VarDecls (``top_level=True``) defer to
        :meth:`_is_constant_alias`, matching the legacy closure that
        relied on the parent ``nodes`` reference to distinguish.

        The original closure also threaded an ``order`` nonlocal int
        that incremented at each append.  Because this method is the
        only writer of *body_candidates* during the auto-pin selection
        phase, ``len(body_candidates)`` at each append equals the next
        order value, so the nonlocal is gone.
        """
        for statement in statements:
            if isinstance(statement, VarDecl):
                eligible = (
                    statement.type_name not in ("unsigned long", "function_pointer")
                    and not (top_level and self._is_constant_alias(body=statements, statement=statement))
                    and not isinstance(statement.init, Call)
                )
                if eligible:
                    body_candidates.append((statement.name, len(body_candidates)))
            if isinstance(statement, (Compound, DoWhile, While)):
                self._collect_auto_pin_body_candidates(statement.body, body_candidates=body_candidates, top_level=False)
            elif isinstance(statement, For):
                self._collect_auto_pin_body_candidates(statement.init, body_candidates=body_candidates, top_level=False)
                self._collect_auto_pin_body_candidates(statement.body, body_candidates=body_candidates, top_level=False)
            elif isinstance(statement, If):
                self._collect_auto_pin_body_candidates(statement.body, body_candidates=body_candidates, top_level=False)
                if statement.else_body is not None:
                    self._collect_auto_pin_body_candidates(statement.else_body, body_candidates=body_candidates, top_level=False)
            elif isinstance(statement, Switch):
                for case in statement.cases:
                    self._collect_auto_pin_body_candidates(case.body, body_candidates=body_candidates, top_level=False)

    def _collect_byte_typed_locals(self, statements: list[Node], /, *, byte_types: set[str], byte_typed: set[str]) -> None:
        """Record names of VarDecl locals whose declared type has no high-register byte alias."""
        for statement in statements:
            if isinstance(statement, (Compound, DoWhile, While)):
                self._collect_byte_typed_locals(statement.body, byte_types=byte_types, byte_typed=byte_typed)
            elif isinstance(statement, For):
                self._collect_byte_typed_locals(statement.init, byte_types=byte_types, byte_typed=byte_typed)
                self._collect_byte_typed_locals(statement.body, byte_types=byte_types, byte_typed=byte_typed)
            elif isinstance(statement, If):
                self._collect_byte_typed_locals(statement.body, byte_types=byte_types, byte_typed=byte_typed)
                if statement.else_body is not None:
                    self._collect_byte_typed_locals(statement.else_body, byte_types=byte_types, byte_typed=byte_typed)
            elif isinstance(statement, Switch):
                for case in statement.cases:
                    self._collect_byte_typed_locals(case.body, byte_types=byte_types, byte_typed=byte_typed)
            elif isinstance(statement, VarDecl) and statement.type_name in byte_types:
                byte_typed.add(statement.name)

    def _collect_function_pointer_vars(self, body: list[Node], /, *, parameters: list | None = None) -> set[str]:
        """Return every name that names a function_pointer (params + locals + file-scope globals).

        Shared by :meth:`compute_safe_pin_registers` (per-call clobber
        tally) and :meth:`_select_auto_pin_candidates` (per-candidate
        pre-store clobber tally) so they classify indirect calls the
        same way.
        """
        function_pointer_vars: set[str] = set()
        if parameters is not None:
            for param in parameters:
                if param.type == "function_pointer":
                    function_pointer_vars.add(param.name)

        def visit(statements: list[Node]) -> None:
            for statement in statements:
                if isinstance(statement, VarDecl) and statement.type_name == "function_pointer":
                    function_pointer_vars.add(statement.name)
                elif isinstance(statement, (Compound, DoWhile, While)):
                    visit(statement.body)
                elif isinstance(statement, If):
                    visit(statement.body)
                    if statement.else_body is not None:
                        visit(statement.else_body)
                elif isinstance(statement, Switch):
                    for case in statement.cases:
                        visit(case.body)

        visit(body)
        for global_name, declaration in self.global_scalars.items():
            if declaration.type_name == "function_pointer":
                function_pointer_vars.add(global_name)
        return function_pointer_vars

    def _collect_pinned_reads(self, node: Node, /) -> set[str]:
        """Return every pinned register that *node*'s expression reads.

        Like :meth:`_arg_pinned_sources` but walks the full AST shape —
        ``UnaryOperation``, ``PlaceAddressOf``, ``Index``, etc. — so it can
        be used to schedule syscall-builtin argument loads where the
        arg AST is not restricted to the simple-call shape.  Returns
        a set of register names (e.g. ``{"ebx", "edi"}``).
        """
        reads: set[str] = set()
        stack: list[Node] = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, Var):
                if current.name in self.pinned_register:
                    reads.add(self.pinned_register[current.name])
                elif current.name in self.param_in_register:
                    reads.add(self.param_in_register[current.name])
                continue
            for slot in getattr(type(current), "__slots__", ()):
                child = getattr(current, slot, None)
                if isinstance(child, Node):
                    stack.append(child)
                elif isinstance(child, list):
                    stack.extend(item for item in child if isinstance(item, Node))
        return reads

    def _compute_argument_register_affinity(self, *, body: list[Node], parameters: list) -> dict[str, dict[str, int]]:
        """Tally how often each local/param is a register-convention call argument.

        Walks *body* and, for every :class:`Call` to a register-convention callee
        (a fastcall user function or a libbboeos extern), records — for argument
        positions 1 (``edx``) and 2 (``ecx``) that fall within the callee's
        regparm count — an affinity for the convention register when the argument
        is a plain :class:`Var`.  Homing such a value in its convention register
        lets the call site skip a ``mov reg, src`` (see
        :meth:`_emit_register_arg_single`), the byte saving the legacy heuristic
        captured by greedy-rank luck.

        Position 0 (``eax``) is never recorded: EAX is not in the pinnable pool,
        so arg0 affinity can never influence coloring.  Builtins (custom per-arg
        register maps — e.g. ``memcpy``/``memset`` use ESI/EDI/ECX via
        ``rep movsb``, NOT the regparm convention) are skipped even when the
        builtin name also appears in ``libbboeos_extern_declarations``, mirroring
        the call-emission dispatch which prefers a ``builtin_<name>`` handler over
        the regparm extern path.  Function-pointer/unknown callees and variadic
        extra arguments are skipped too; the ``index < regparm_count`` guard
        already drops the variadic stack args.  Names are recorded
        unconditionally; the caller restricts the dictionary to allocatable
        values.

        Safety: the whole tally is suppressed (returns ``{}``) for any function
        that contains a *register-custom builtin* call (one with a
        ``builtin_<name>`` handler) taking two or more arguments where at least
        one argument is not a plain :class:`Var`.  Such a call site loads several
        registers from caller expressions before a single emitted operation
        (``write``/``read``/``memcpy`` …); its cycle-breaker can only spill a
        simple-``Var`` argument to AX (see :meth:`_emit_register_arg_moves`), so
        an Index/expression argument leaves a cycle unbreakable.  Perturbing the
        register assignment with affinity can push exactly these sites into that
        unbreakable cycle in 16-bit mode (``ls.c`` line 91), so we conservatively
        keep parity-as-baseline for those functions rather than risk a
        compile-time failure.  Functions whose register-custom builtin calls take
        only simple-``Var`` arguments (e.g. ``write(STDOUT, line, length)``) stay
        eligible.
        """
        affinity: dict[str, dict[str, int]] = {}
        position_register = {1: self.target.dx_register, 2: self.target.count_register}

        stack: list[Node] = [statement for statement in body if isinstance(statement, Node)]
        stack.extend(parameter for parameter in parameters if isinstance(parameter, Node))
        while stack:
            current = stack.pop()
            if isinstance(current, Call):
                # A ``builtin_<name>`` handler wins over the regparm extern path
                # in call emission, so a builtin name (even one shadowing a
                # libbboeos export) never follows the EAX/EDX/ECX convention.
                if getattr(self, f"builtin_{current.name}", None) is not None:
                    # Register-custom builtin call: if it loads 2+ registers from
                    # caller expressions and any source is not a plain Var, the
                    # arg-lowering cycle-breaker may be unable to break a cycle
                    # the affinity-perturbed allocation introduces (16-bit).
                    # Suppress affinity entirely for this function.
                    if len(current.args) >= 2 and any(not isinstance(argument, Var) for argument in current.args):
                        return {}
                    regparm_count = 0
                elif current.name in self.fastcall_functions:
                    regparm_count = self.function_regparm_count.get(current.name, 0)
                elif current.name in self.libbboeos_extern_declarations:
                    regparm_count = min(3, self.libbboeos_extern_declarations[current.name])
                else:
                    regparm_count = 0
                if regparm_count:
                    for index, argument in enumerate(current.args):
                        if index not in position_register or index >= regparm_count:
                            continue
                        if isinstance(argument, Var):
                            register = position_register[index]
                            affinity.setdefault(argument.name, {})
                            affinity[argument.name][register] = affinity[argument.name].get(register, 0) + 1
            for slot in getattr(type(current), "__slots__", ()):
                child = getattr(current, slot, None)
                if isinstance(child, Node):
                    stack.append(child)
                elif isinstance(child, list):
                    stack.extend(item for item in child if isinstance(item, Node))
        return affinity

    def _compute_pin_economics(
        self,
        *,
        body: list[Node],
        parameters: list,
    ) -> AutoPinEconomics:
        """Gather the pin economics for *body* (pure; no register assignment).

        Mirrors the candidate-collection and tally prefix of
        :meth:`_select_auto_pin_candidates`: eligible candidates (params + body
        locals minus asm-operand, expression-temporary, and address-taken vars),
        reference counts, subscript counts, and per-candidate pre-first-store
        clobbers.  Both the legacy heuristic and the regalloc adapter consume the
        returned bundle.
        """
        self.switch_pin_overrides = set()

        param_candidates: list[tuple[str, int]] = []
        byte_types: set[str] = {"char", "signed char", "unsigned char", "_Bool"}
        byte_typed: set[str] = set()
        for order, param in enumerate(parameters):
            if param.is_array:
                continue
            param_candidates.append((param.name, order))
            if param.type in byte_types:
                byte_typed.add(param.name)

        body_candidates: list[tuple[str, int]] = []
        function_pointer_vars: set[str] = self._collect_function_pointer_vars(body, parameters=parameters)
        self._collect_auto_pin_body_candidates(body, body_candidates=body_candidates, top_level=True)
        asm_operand_vars = self._collect_asm_operand_vars(body)
        body_candidates = [(name, o) for name, o in body_candidates if name not in asm_operand_vars]

        state = AutoPinTallyState(body_candidates=body_candidates)
        for statement in body:
            self._tally_auto_pin_counts(statement, state=state)
        address_taken = state.address_taken
        ax_resident_uses = state.ax_resident_uses
        body_candidates = state.body_candidates
        counts = state.counts
        index_uses = state.index_uses
        init_count = state.init_count
        init_expr = state.init_expr
        other_uses = state.other_uses

        candidate_names = {name for name, _ in body_candidates}
        pre_store_clobbers: dict[str, dict[str, int]] = {name: {} for name in candidate_names}
        written: dict[str, bool] = dict.fromkeys(candidate_names, False)
        for statement in body:
            self._tally_pre_store_clobbers(
                statement,
                candidate_names=candidate_names,
                function_pointer_vars=function_pointer_vars,
                pre_store_clobbers=pre_store_clobbers,
                written=written,
            )

        self._collect_byte_typed_locals(body, byte_types=byte_types, byte_typed=byte_typed)

        combined = self._rank_candidates(body_candidates, counts=counts) + self._rank_candidates(param_candidates, counts=counts)
        combined = [
            item
            for item in combined
            if not self._is_candidate_expression_temporary(
                item[0],
                ax_resident_uses=ax_resident_uses,
                init_count=init_count,
                init_expr=init_expr,
                other_uses=other_uses,
            )
        ]
        combined = [item for item in combined if item[0] not in address_taken]

        return AutoPinEconomics(
            address_taken=address_taken,
            allocatable=frozenset(name for name, _ in combined),
            byte_typed=frozenset(byte_typed),
            index_uses=index_uses,
            pre_store_clobbers=pre_store_clobbers,
            ranked=combined,
            reference_counts=counts,
        )

    def _compute_pinned_initialized_per_call(self, ir_body: list, /) -> dict[int, frozenset[str]]:
        """Pre-pass: for each ir.Call / ir.CarryBranch, the may-defined pinned register set.

        Auto-pinned locals are not initialized until the first store to
        them.  Saving a pinned register around a call before that
        store preserves garbage — :meth:`_pinned_registers_to_save`
        consults the map this method produces and skips the save when
        the local can't yet hold a meaningful value.

        Initial defined set: registers held by parameters (loaded into
        their pin in the prologue) and locals declared with
        ``__attribute__((pinned_register(R)))`` whose initializer fired
        as part of the declaration.  Auto-pinned locals start
        undefined.

        Loop bodies are pre-merged: any store inside a loop region
        (Label..back-Jump) is added to the defined set BEFORE the
        first instruction of the loop, so subsequent iterations see
        the value as live.  Without this, calls inside the loop body
        that appear before the store in source order would skip a
        save that the second iteration actually needs.

        Returns dict keyed by id(instruction).  Empty / missing key
        means "no live pin" so callers should treat absence as
        ``frozenset()`` — distinct from ``None`` which means "no
        analysis was performed" (AST path, naked function, etc.).
        """
        # Locals / params only: register-resident IR temps are pinned in
        # self.pinned_register too, but their save-liveness is computed
        # separately by :meth:`_compute_temp_pinned_live_per_call` (a
        # temp is single-assignment, so a simple def/last-use range, not
        # the may-defined-store dataflow locals need).  Excluding them
        # here keeps the two analyses from double-counting; the per-call
        # filters are unioned in :meth:`_merge_pinned_save_filters`.
        pinned_locals: dict[str, str] = {
            name: register for name, register in self.pinned_register.items() if name not in self.temp_pinned_registers
        }
        if not pinned_locals:
            return {}
        initial: set[str] = set(self._prologue_initialized_pinned_registers())
        label_positions: dict[str, int] = {}
        for index, instruction in enumerate(ir_body):
            if isinstance(instruction, ir.Label):
                label_positions[instruction.name] = index
        loop_ranges: list[tuple[int, int]] = []
        for index, instruction in enumerate(ir_body):
            if isinstance(instruction, ir.Jump):
                target = label_positions.get(instruction.target)
                if target is not None and target < index:
                    loop_ranges.append((target, index))
        loop_stores: list[set[str]] = []
        for start, end in loop_ranges:
            stores: set[str] = set()
            for k in range(start, end + 1):
                for target_name in self._ir_instruction_store_targets(ir_body[k]):
                    if target_name in pinned_locals:
                        stores.add(pinned_locals[target_name])
            loop_stores.append(stores)
        result: dict[int, frozenset[str]] = {}
        defined: set[str] = set(initial)
        for index, instruction in enumerate(ir_body):
            for loop_index, (start, _end) in enumerate(loop_ranges):
                if start == index:
                    defined |= loop_stores[loop_index]
            # Record filter sets for every direct IR call — builtin
            # and user-function alike — plus CarryBranch
            # (``carry_return`` callee invoked from a condition).
            # Block / Access-wrapped statements are not analysed; ``ir.Block`` / ``ir.Access``
            # lowering leaves :attr:`_current_call_pinned_initialized`
            # at ``None`` so any nested calls fall back to the
            # conservative full save-set.
            if isinstance(instruction, (ir.Call, ir.CarryBranch, ir.RepString, ir.TailCall)):
                # ``ir.RepString`` clobbers EDI/ESI/ECX/EAX like memcpy /
                # memset; :meth:`generate_rep_string` saves the live pins
                # among them, and consults this same liveness filter so a
                # pin whose local is not yet written isn't pushed (its
                # value is garbage).
                result[id(instruction)] = frozenset(defined)
            for target_name in self._ir_instruction_store_targets(instruction):
                if target_name in pinned_locals:
                    defined.add(pinned_locals[target_name])
        return result

    def _compute_temp_pinned_live_per_call(self, ir_body: list, /) -> dict[int, frozenset[str]]:
        """Per-clobber-site map of temp-pinned registers that are live across the site.

        Companion to :meth:`_compute_pinned_initialized_per_call` for the
        register-resident IR temps :meth:`_allocate_ir_temps` produced
        (recorded in :attr:`temp_pinned_registers`).  An IR temp is
        single-assignment, so it is live across a clobbering call /
        ``rep`` string-op / ``CarryBranch`` / ``TailCall`` iff that
        site's index lies strictly after the temp's definition and at or
        before its last use::

            definition_index < site_index <= last_use_index

        Temps live across a loop back-edge are handled conservatively:
        any temp whose live range overlaps a loop region (a back-Jump
        and its target label) is treated as live across EVERY clobber
        site inside that region, mirroring the loop pre-merge in
        :meth:`_compute_pinned_initialized_per_call`.  This errs toward
        saving — a temp written once before the loop and read again on
        the next iteration would otherwise look dead at a mid-loop call
        on the source-order pass.

        The result is folded into :attr:`_ir_call_pinned_initialized`
        (built just before IR lowering) so :meth:`_pinned_registers_to_save`
        pushes / pops a temp's register exactly across the sites it lives
        across.  Returns a dict keyed by ``id(instruction)``; a site
        absent from the dict has no live temp register.
        """
        if not self.temp_pinned_registers:
            return {}
        definition_index: dict[str, int] = {}
        last_use_index: dict[str, int] = {}
        use_indices: dict[str, list[int]] = {}
        for index, instruction in enumerate(ir_body):
            for target_name in self._ir_instruction_store_targets(instruction):
                if target_name in self.temp_pinned_registers and target_name not in definition_index:
                    definition_index[target_name] = index
            for name in regalloc.instruction_uses(instruction=instruction):
                if name in self.temp_pinned_registers:
                    last_use_index[name] = index
                    use_indices.setdefault(name, []).append(index)
        # Loop carry: an IR temp is single-assignment, so its live range
        # is exactly [definition, last-use] — NO loop extension is needed
        # for a temp defined and consumed within one iteration (extending
        # it backward to the loop head would mark it live across calls
        # that PRECEDE its definition, over-saving its register and, when
        # that inflates the save set past the pusha threshold, corrupting
        # an after-call out_register capture via popa — the fd_read_file
        # vfs_read_sec miscompile).  The only temp that genuinely lives
        # across a loop back-edge is one with a use at or before its
        # definition index (it reads the value the PREVIOUS iteration
        # produced); extend just those across their enclosing loop.
        label_positions: dict[str, int] = {}
        for index, instruction in enumerate(ir_body):
            if isinstance(instruction, ir.Label):
                label_positions[instruction.name] = index
        loop_ranges: list[tuple[int, int]] = []
        for index, instruction in enumerate(ir_body):
            if isinstance(instruction, ir.Jump):
                target = label_positions.get(instruction.target)
                if target is not None and target < index:
                    loop_ranges.append((target, index))
        live_definition = dict(definition_index)
        live_last_use = dict(last_use_index)
        for temp, define_at in definition_index.items():
            if not any(use_at <= define_at for use_at in use_indices.get(temp, ())):
                continue  # consumed after its def within the iteration; exact range
            for start, end in loop_ranges:
                if start <= define_at <= end:
                    live_definition[temp] = min(live_definition[temp], start)
                    live_last_use[temp] = max(live_last_use[temp], end)
        # Record an entry for EVERY clobber site (even with no live
        # temp) so the merged filter covers all sites: a site present in
        # the filter with an empty temp contribution still anchors the
        # precise locals set, whereas an absent site falls back to the
        # conservative save-everything default — which would re-save a
        # dead temp register when temps are the only pins.
        result: dict[int, frozenset[str]] = {}
        for index, instruction in enumerate(ir_body):
            if not isinstance(instruction, (ir.Call, ir.CarryBranch, ir.RepString, ir.TailCall)):
                continue
            live_registers = {
                self.temp_pinned_registers[temp]
                for temp in live_definition
                if live_definition[temp] < index <= live_last_use.get(temp, live_definition[temp])
            }
            result[id(instruction)] = frozenset(live_registers)
        return result

    def _dereference_place_width(self, place: DereferencePlace, /) -> int:
        """Return the byte width of a load/store through a standalone ``DereferencePlace``.

        The pointee type string (one ``*`` stripped from the pointer
        expression's type) maps
        to 1 for byte types, 2 for ``unsigned short`` on targets whose
        ``int_size`` exceeds 2, otherwise the full ``int_size``.
        """
        pointee_type = self._place_type(place)
        if pointee_type in self.BYTE_TYPES:
            return 1
        if pointee_type == "unsigned short" and self.target.int_size > 2:
            return 2
        return self.target.int_size

    def _emit_bitfield_read(self, info: FieldInfo, /, *, addr: str) -> None:
        """Emit the load-shift-mask-extend sequence for a bitfield read.

        ``info`` carries the bit_offset / bit_width.  ``addr`` is the
        byte's NASM memory operand (e.g. ``[ebx+4]``).  Result lands in
        the accumulator, zero-extended.  Callers ``return`` after this
        helper since it produces the rvalue and clears AX-state.
        """
        self.emit(f"        mov al, {addr}")
        if info.bit_offset != 0:
            self.emit(f"        shr al, {info.bit_offset}")
        if info.bit_width != 8:
            self.emit(f"        and al, {(1 << info.bit_width) - 1}")
        self.emit(f"        movzx {self.target.acc}, al")
        self.ax_clear()

    def _emit_bitfield_write(self, info: FieldInfo, /, *, addr: str) -> None:
        """Emit the read-modify-write store sequence for a bitfield write.

        The rhs must already be in AL.  ``info`` carries bit_offset /
        bit_width; ``addr`` is the byte's NASM memory operand.  Uses
        CL as scratch — not BL — because ``addr`` is commonly
        ``[ebx+N]`` (the arrow path loads the struct pointer into EBX),
        and stashing into BL would clobber EBX's low byte and corrupt
        the subsequent load / store through the same operand.

        Const-fold: when the target byte is a known local constant AND the
        rhs was just loaded as a literal (``ax_literal`` is set), compute
        the result byte at compile time and emit a single ``mov byte``.
        """
        field_mask = ((1 << info.bit_width) - 1) << info.bit_offset
        clear_mask = (~field_mask) & 0xFF
        # Const-fold: target byte is known local AND rhs is a literal AX.
        slot = self._parse_local_byte_addr(addr)
        if slot is not None and slot in self.known_local_bytes and self.ax_literal is not None:
            known = self.known_local_bytes[slot]
            rhs = self.ax_literal & ((1 << info.bit_width) - 1)
            new_byte = (known & clear_mask) | (rhs << info.bit_offset)
            self.emit(f"        mov byte {addr}, {new_byte}")
            return
        # General RMW path.
        self.emit("        mov cl, al")
        if info.bit_width != 8:
            self.emit(f"        and cl, {(1 << info.bit_width) - 1}")
        if info.bit_offset != 0:
            self.emit(f"        shl cl, {info.bit_offset}")
        self.emit(f"        mov al, {addr}")
        self.emit(f"        and al, {clear_mask}")
        self.emit("        or al, cl")
        self.emit(f"        mov {addr}, al")

    def _emit_bitfield_write_literal(self, info: FieldInfo, /, *, addr: str, value: int) -> None:
        """Emit the single-instruction peephole for a 1-bit bitfield literal 0/1 store.

        ``value`` must be 0 or 1; ``info.bit_width`` must be 1.  Emits
        ``and byte addr, ~mask`` for value 0 or ``or byte addr, mask``
        for value 1.  When ``addr`` resolves to a ``known_local_bytes`` slot,
        const-folds the entire byte into a single ``mov byte addr, <result>``.
        """
        field_mask = ((1 << info.bit_width) - 1) << info.bit_offset
        clear_mask = (~field_mask) & 0xFF
        # Const-fold: if the target byte is a known local constant,
        # compute the resulting byte and emit a single mov.
        slot = self._parse_local_byte_addr(addr)
        if slot is not None and slot in self.known_local_bytes:
            known = self.known_local_bytes[slot]
            new_byte = (known & clear_mask) | ((value << info.bit_offset) & field_mask)
            self.emit(f"        mov byte {addr}, {new_byte}")
            return
        if value == 0:
            self.emit(f"        and byte {addr}, {clear_mask}")
        else:
            self.emit(f"        or byte {addr}, {field_mask}")

    def _emit_bss_equs(self) -> None:
        """Emit BSS EQU definitions and ``_bss_end`` after ``_program_end:``.

        Placing EQUs after ``_program_end:`` ensures they are never forward
        references, which is important for the self-hosted assembler whose
        EQU resolution does not handle forward references correctly.
        """
        # Always emit _bss_end so programs can reference it regardless of
        # whether they have BSS variables (e.g. asm_layout.h).
        if (isinstance(self.bss_total, int) and self.bss_total > 0) or isinstance(self.bss_total, str):
            self.emit(f"_bss_end equ _program_end + {self.bss_total}")
        else:
            self.emit("_bss_end equ _program_end")

        if not self.bss_vars:
            return

        self.emit(";; --- BSS (zero-initialized) ---")
        if isinstance(self.bss_total, int):
            # All sizes are literals: emit with Python-computed integer offsets.
            offset = 0
            for name, size_expr in self.bss_vars:
                suffix = f" + {offset}" if offset else ""
                self.emit(f"{self._global_label(name)} equ _program_end{suffix}")
                self._emit_global_export(name)
                offset += int(size_expr)
        else:
            # Non-literal sizes: use EQU chain and define _bss_total_size.
            prev_end = "_program_end"
            for name, size_expr in self.bss_vars:
                label = self._global_label(name)
                self.emit(f"{label} equ {prev_end}")
                self._emit_global_export(name)
                prev_end = f"{label} + {size_expr}"
            self.emit(f"_bss_total_size equ {prev_end} - _program_end")

    def _emit_bss_trailer(self) -> None:
        """Emit the 6-byte BSS trailer (``dd <size>; dw 0B032h``) just before ``_program_end``.

        Widened from 16-bit to 32-bit BSS size so programs can declare
        more than 64 KB of BSS (used by ``edit``'s 1 MB gap buffer once
        paging is on).  Sets ``self.bss_total`` so the caller can emit
        ``_bss_end`` and the per-variable EQUs after ``_program_end:``
        (avoiding forward references that the self-hosted assembler
        cannot resolve).

        In object mode there's no flat-binary trailer — the linker
        appends the BSS trailer when producing the final image.
        Instead, zero-init globals (``self.bss_vars``) and elided
        local-static cells (``self.elided_local_bss_vars``) are
        emitted into ``section .bss`` as ``resb`` reservations so the
        linker can sum them and emit one trailer for the whole image.
        """
        if self.object_mode:
            if not self.bss_vars and not self.elided_local_bss_vars:
                return
            self.emit()
            self.emit("section .bss")
            for name, size_expression in self.bss_vars:
                self._emit_global_export(name)
                self.emit(f"{self._global_label(name)}: resb {size_expression}")
            for name, size_expression in self.elided_local_bss_vars:
                self.emit(f"_l_{name}: resb {size_expression}")
            return
        if not self.bss_vars:
            return

        # Compute total BSS size as Python int when all sizes are decimal literals.
        total = 0
        all_literal = True
        for _name, size_expr in self.bss_vars:
            try:
                total += int(size_expr)
            except ValueError:
                all_literal = False
                break

        if all_literal:
            self.bss_total = total
            self.emit(f"        dd {total}")
        else:
            self.bss_total = "_bss_total_size"
            self.emit("        dd _bss_total_size")
        self.emit("        dw 0B032h")

    def _emit_builtin_arg_moves(self, register_args: list[tuple[str, Node]], /) -> None:
        """Emit builtin-arg loads in a topologically safe order.

        Each item is ``(target_register, ast_node)``.  The scheduler
        picks an item whose target register is (a) not read by any
        other pending item, and (b) not clobbered as scratch by any
        other pending item's evaluation, then emits it through
        :meth:`emit_register_from_argument` (which handles every leaf
        shape — pinned vars, memory scalars, expressions, address-of,
        constants, etc.).  Constraint (a) prevents
        ``mov bx, fd; ... add edi, ebx`` where loading one argument
        into BX would clobber a pinned variable that another argument's
        expression still needs to read.  Constraint (b) prevents
        ``mov esi, names[i]; mov edi, strlen(names[i])`` where the
        second arg's Index lowering reuses ESI as scratch and erases
        the buffer pointer the surrounding builtin (``write``) needs.

        Used by both syscall builtins (``read``, ``recvfrom``, etc.) and
        string-op builtins (``memcmp``, ``memcpy``, ``memset``) — anywhere
        multiple registers must be loaded from caller expressions before
        a single emitted operation.

        Cycles (e.g. two args whose sources and targets mutually swap)
        would need a temp-register spill; in practice every builtin's
        argument shape is acyclic, so we raise :class:`CompileError`
        rather than silently mis-compiling.
        """
        items = [
            {
                "target": target,
                "arg": arg,
                "reads": self._collect_pinned_reads(arg),
                "scratch": self._estimate_scratch_clobbers(arg),
                "spilled": False,
            }
            for target, arg in register_args
        ]
        while items:
            progress = None
            for index, item in enumerate(items):
                target = item["target"]
                read_blocked = any(j != index and target in other["reads"] for j, other in enumerate(items))
                scratch_blocked = any(j != index and target in other["scratch"] for j, other in enumerate(items))
                if not read_blocked and not scratch_blocked:
                    progress = index
                    break
            if progress is None:
                # Cycle break: spill a simple-Var arg whose value lives in
                # a single pinned register to AX, then re-emit it from AX
                # later.  This breaks the dependency edge "other items
                # block me because they read MY source register" — once
                # the value is also in AX, no one else's reads point at
                # the now-stale register home.
                spillable = next(
                    (
                        index
                        for index, item in enumerate(items)
                        if isinstance(item["arg"], Var)
                        and item["arg"].name in self.pinned_register
                        and item["reads"] == {self.pinned_register[item["arg"].name]}
                        and not any(
                            j != index and self.pinned_register[item["arg"].name] in other["reads"] for j, other in enumerate(items)
                        )
                    ),
                    None,
                )
                if spillable is None:
                    message = "builtin arg lowering hit an unbreakable cyclic register dependency"
                    raise CompileError(message, line=getattr(items[0]["arg"], "line", None))
                spilled = items[spillable]
                source_register = next(iter(spilled["reads"]))
                self.emit(f"        mov {self.target.acc}, {source_register}")
                self.ax_clear()
                spilled["spilled"] = True
                spilled["reads"] = set()
                continue
            item = items.pop(progress)
            if item["spilled"]:
                self.emit(f"        mov {item['target']}, {self.target.acc}")
            else:
                self.emit_register_from_argument(argument=item["arg"], register=item["target"])

    def _emit_byte_index_si(self, node: Index, /) -> tuple[str, bool]:
        """Load the base pointer of a byte-indexed node into SI.

        Returns ``(operand, guarded)`` where *operand* is the NASM
        memory operand (e.g. ``byte [si+12]`` or ``byte [si]``)
        suitable for use in a ``cmp`` instruction, and *guarded* is
        True when an SI-scratch guard (``push si``) was emitted —
        callers must pair it with :meth:`_si_scratch_guard_end` after
        the operand is consumed, else SI = aliased-source_cursor gets
        clobbered by the base load.  Prefers direct addressing when
        the base is a constant (no guard needed).
        """
        if (direct := self._byte_index_direct(node)) is not None:
            return (f"byte [{direct}]", False)
        vname = node.array.name
        offset = node.index.value
        guarded = self._si_scratch_guard_begin(vname)
        self._emit_load_var(vname, register=self.target.si_register)
        si = self.target.si_register
        operand = f"byte [{si}+{offset}]" if offset else f"byte [{si}]"
        return (operand, guarded)

    def _emit_comparison_against_constant(self, *, is_zero: bool, left: Node, literal: str) -> None:
        """Emit a comparison whose right operand reduced to a constant immediate.

        Four fast paths layered ahead of the generic
        ``generate_expression`` + ``cmp/test ax, imm`` fallback:

        * pinned-register ``left`` — ``cmp R, imm`` / ``test R, R`` in place.
        * memory-backed scalar ``left`` — ``cmp [L], imm`` skips the
          ``mov ax, [L]`` load (``byte`` width for byte-scalars).
        * byte-indexed ``left`` — ``cmp byte [bx+N], imm`` skips the
          AL load + zero-extend.
        * everything else — load into AX, then ``cmp ax, imm`` /
          ``test ax, ax``.
        """
        if isinstance(left, Var) and left.name in self.pinned_register:
            register = self.pinned_register[left.name]
            if is_zero:
                self.emit(f"        test {register}, {register}")
            else:
                self.emit(f"        cmp {register}, {literal}")
            return
        # Memory-backed local compared to a constant: fuse into a
        # direct ``cmp word [L], imm`` (or ``cmp byte [L], imm`` for
        # byte-scalar locals / globals whose storage is a single
        # ``db`` cell) so we skip the ``mov ax, [L]`` load.  Safe
        # because the flags are consumed by the next conditional
        # jump and AX's prior value was not promised.
        if (
            isinstance(left, Var)
            and self._is_memory_scalar(left.name)
            and left.name not in self.variable_arrays
            and left.name != self.ax_local
            and self.variable_types.get(left.name) != "unsigned long"
        ):
            address = self._local_address(left.name)
            width = "byte" if self._is_byte_scalar(left.name) else self.target.word_size
            if is_zero:
                self.emit(f"        cmp {width} [{address}], 0")
            else:
                self.emit(f"        cmp {width} [{address}], {literal}")
            return
        # Byte-indexed variable compared to a constant: fuse into
        # ``cmp byte [bx+N], imm`` so we skip the load-into-AL and
        # the zero-extend into AX.
        if self._is_byte_index(left):
            operand, guarded = self._emit_byte_index_si(left)
            if is_zero:
                self.emit(f"        cmp {operand}, 0")
            else:
                self.emit(f"        cmp {operand}, {literal}")
            self._si_scratch_guard_end(guarded=guarded)
            return
        self.generate_expression(left)
        if is_zero:
            self.emit("        test al, al" if self.ax_is_byte else f"        test {self.target.acc}, {self.target.acc}")
        else:
            register = "al" if self.ax_is_byte else self.target.acc
            self.emit(f"        cmp {register}, {literal}")

    def _emit_comparison_general(self, *, left: Node, right: Node) -> None:
        """Emit a comparison whose right operand isn't a constant immediate.

        Three fast paths, then the generic CX-scratch fallback:

        * two byte-indexed vars — ``mov al, [bx+N]`` then ``cmp al,
          [bx+M]`` (avoid the zero-extend + push/pop roundtrip).
        * pinned-register right operand — compare AX against it
          directly (no CX load).  Requires a leaf ``left`` when the
          pin happens to live in CX.
        * memory-backed right operand — ``cmp ax, [mem]`` skips the
          CX load entirely.  Byte-scalar memory bails to the generic
          path (word-sized ``cmp`` would read an adjacent byte).
        * generic — :meth:`emit_binary_operator_operands` (AX = left,
          CX = right), then ``cmp ax, cx``.  Saves CX around the
          ``emit_binary_operator_operands`` clobber when a pinned
          variable lives there.
        """
        # Two byte-indexed variables: load left byte into AL, then
        # compare directly against the right byte in memory.  Saves
        # the zero-extend, push/pop, and CX round-trip.
        if self._is_byte_index(left) and self._is_byte_index(right):
            left_operand, left_guarded = self._emit_byte_index_si(left)
            left_mem = left_operand.removeprefix("byte ")
            self.emit(f"        mov al, {left_mem}")
            self._si_scratch_guard_end(guarded=left_guarded)
            right_operand, right_guarded = self._emit_byte_index_si(right)
            right_mem = right_operand.removeprefix("byte ")
            self.emit(f"        cmp al, {right_mem}")
            self._si_scratch_guard_end(guarded=right_guarded)
            return
        # Fast path: right is a pinned register variable.  Compare
        # AX against it directly, skipping the CX load and any
        # push/pop protection.  When the pinned register is CX we
        # additionally require ``left`` to be a leaf expression so
        # generate_expression can't clobber CX mid-compare.
        if isinstance(right, Var) and right.name in self.pinned_register:
            source = self.pinned_register[right.name]
            if source != self.target.count_register or isinstance(left, (Int, Var, String)):
                left_pinned = isinstance(left, Var) and left.name in self.pinned_register
                self.generate_expression(left)
                # Use matching-width operands for cmp: if source is
                # narrower than acc (e.g., bp vs eax), compare ax/source.
                cmp_acc = self.target.low_word(self.target.acc) if len(source) < len(self.target.acc) else self.target.acc
                self.emit(f"        cmp {cmp_acc}, {source}")
                # ``peephole_compare_through_register`` deletes the
                # ``mov ax, <pin>`` emitted by ``generate_expression``
                # above when a conditional jump follows the cmp.
                # Without this clear, downstream reads of ``left``
                # would skip their own load (``ax_local ==
                # left.name``) and pick up whatever AX actually held
                # — the peephole-deleted source register, not the
                # pinned local.  Mirrors the memory-backed sibling.
                if left_pinned:
                    self.ax_clear()
                return
        # Fast path: right is a memory-backed local.  ``cmp ax, [mem]``
        # skips the CX load entirely.  Byte-scalar locals / globals
        # bail out — their storage is a single byte and a word-sized
        # ``cmp ax, [mem]`` would read the adjacent byte into the
        # high comparison byte.
        if (
            isinstance(right, Var)
            and self._is_memory_scalar(right.name)
            and right.name not in self.pinned_register
            and right.name not in self.variable_arrays
            and self.variable_types.get(right.name) != "unsigned long"
            and not self._is_byte_scalar(right.name)
        ):
            # Invalidate ax_local when ``left`` is pinned — the
            # ``mov ax, reg`` that generate_expression emits here
            # will be removed by ``peephole_compare_through_register``
            # once the caller emits a conditional jump after the
            # cmp, leaving AX without the loaded value.  Without
            # this clear, downstream reads of ``left`` would skip
            # their own load (ax_local == left.name) and pick up
            # whatever AX held from an unrelated earlier expression.
            left_pinned = isinstance(left, Var) and left.name in self.pinned_register
            self.generate_expression(left)
            self.emit(f"        cmp {self.target.acc}, [{self._local_address(right.name)}]")
            if left_pinned:
                self.ax_clear()
            return
        # emit_binary_operator_operands clobbers CX; save it when a
        # pinned variable lives there (push/pop don't modify flags,
        # so the cmp's flags survive the restore for the caller's
        # conditional jump).
        count_pinned = any(register == self.target.count_register for register in self.pinned_register.values())
        if count_pinned:
            self.emit(f"        push {self.target.count_register}")
        self.emit_binary_operator_operands(left, right)
        self.emit(f"        cmp {self.target.acc}, {self.target.count_register}")
        if count_pinned:
            self.emit(f"        pop {self.target.count_register}")

    def _emit_constant_base_index_addr(
        self,
        *,
        const_base: str,
        element_size: int | None = None,
        index: Node,
        is_byte: bool | None = None,
        preserve_ax: bool,
    ) -> str:
        """Set up ``[CONST + disp + si]`` addressing for a constant-base index.

        Folds a trailing ``±Int`` off a ``Var ± Int`` index into the
        displacement so ``BUFFER[i - 1]`` becomes
        ``[BUFFER-1+si]`` after a single ``mov si, [_l_i]``.  Byte-indexed references skip the
        load entirely when the index variable is pinned to DI or BX
        (``[CONST+di]`` / ``[CONST+bx]`` are valid 8086 addressing);
        BP-pinned vars don't qualify because BP would resolve through
        SS, not DS, and CX/DX aren't general index registers in real
        mode either.  This BX/DI restriction is what
        :meth:`_select_auto_pin_candidates` reads via
        ``index_uses`` to keep heavily-subscripted vars off BP.

        Callers pass *element_size* (the stride in bytes — 1 for byte
        arrays, 2 for ``unsigned short``, 4 for full-int / pointer-target on
        32-bit, etc.) which drives both the displacement folding and
        the index-register scaling.  The legacy *is_byte* alias is kept
        for callers that haven't been migrated; it maps to
        ``element_size = 1`` (byte) or ``int_size`` (full word).

        When *preserve_ax* is True, any path that evaluates the index
        through AX pushes/pops AX so the caller's value survives.
        """
        if element_size is None:
            element_size = 1 if is_byte else self.target.int_size
        is_byte = element_size == 1
        displacement = 0
        if isinstance(index, BinaryOperation) and index.operation in ("+", "-") and isinstance(index.right, Int):
            sign = 1 if index.operation == "+" else -1
            displacement = sign * index.right.value * element_size
            index = index.left
        si = self.target.si_register
        base_register = si
        # No-base SIB fast path: when the index is a Var pinned to a
        # register and the element size is x86-encodable as a SIB scale
        # (4 or 8), return ``[const_base + disp + idx*scale]`` directly
        # without staging the scaled index through SI.  Saves the
        # ``mov si, idx_reg / shl si, k`` sequence at every use site.
        # Restricted to scales 4 and 8: NASM canonicalizes scale 1 and
        # 2 to non-SIB / ``[base+base]`` encodings, and matching its
        # byte stream means avoiding those scales (PR #559 asm.c
        # fixture sib_no_base.asm documents the same restriction).
        # Gated on 32-bit-or-wider since 16-bit addressing forms reject
        # general SIB.
        sib_no_base_eligible = (
            isinstance(index, Var)
            and index.name in self.pinned_register
            and element_size in (4, 8)
            and self.pinned_register[index.name] != si
            and self.target.int_size >= 4
        )
        if sib_no_base_eligible:
            pinned_index_reg = self.pinned_register[index.name]
            addr = const_base
            if displacement != 0:
                addr += f"{displacement:+d}"
            addr += f"+{pinned_index_reg}*{element_size}"
            return addr
        if isinstance(index, Int):
            displacement += index.value * element_size
            self.emit(f"        xor {si}, {si}")
        elif (
            is_byte
            and isinstance(index, Var)
            and index.name in self.pinned_register
            and self.pinned_register[index.name] in (self.target.di_register, self.target.bx_register)
        ):
            base_register = self.pinned_register[index.name]
        elif isinstance(index, Var) and index.name in self.pinned_register:
            self.emit(f"        mov {si}, {self.pinned_register[index.name]}")
            self._emit_scale_index(si, scale=element_size)
        elif isinstance(index, Var) and self._is_memory_scalar(index.name) and not self._is_byte_scalar(index.name):
            self.emit(f"        mov {si}, [{self._local_address(index.name)}]")
            self._emit_scale_index(si, scale=element_size)
        else:
            if preserve_ax:
                self.emit(f"        push {self.target.acc}")
            self.generate_expression(index)
            self._emit_scale_index(self.target.acc, scale=element_size)
            self.emit(f"        mov {si}, {self.target.acc}")
            if preserve_ax:
                self.emit(f"        pop {self.target.acc}")
        addr = const_base
        if displacement != 0:
            addr += f"{displacement:+d}"
        addr += f"+{base_register}"
        return addr

    def _emit_double_index_resolved_load(self, place: Place, /) -> None:
        """Load an array-of-pointers ``name[outer][inner]`` via the address resolver.

        Resolves *place* (a deref-rooted ``SubscriptPlace(DereferencePlace(...))``)
        into a MemoryOperand based at ESI, then runs the terminal load.  The
        terminal deliberately does NOT share :meth:`_emit_field_load`: the legacy
        double-index emitter loaded a byte element zero-extended but loaded every
        wider element (including ``unsigned short *``) with a plain full-width
        ``mov`` — reading the extra high bytes for a 2-byte pointee.  That width
        gap is preserved byte-for-byte here (fixing it would widen the emitted
        bytes and belongs in its own pass) rather than routed through
        :meth:`_emit_field_load`'s ``movzx`` word handling.
        """
        operand = self.resolve_address(place)
        address = self._build_address(operand.base, operand.displacement, index=operand.index or "")
        if operand.field_size == 1:
            self.emit_byte_load_zx(address)
        else:
            # field_size == 2 intentionally uses a full-width mov (not movzx) — this
            # preserves the legacy unsigned short * gap described in the docstring.
            self.emit(f"        mov {self.target.acc}, {address}")
        self.ax_clear()

    def _emit_field_load(self, *, addr: str, field_size: int) -> None:
        """Emit the struct-field load instruction matching *field_size*.

        Byte fields go through :meth:`emit_byte_load_zx`; word fields on a
        32-bit target zero-extend through ``movzx`` so downstream
        ``test eax, eax`` / signed compares don't read stale upper bytes
        left behind by a wider previous load.  All other widths use a
        plain ``mov`` into the accumulator.
        """
        if field_size == 1:
            self.emit_byte_load_zx(addr)
        elif field_size == 2 and self.target.int_size == 4:
            self.emit(f"        movzx {self.target.acc}, word {addr}")
        else:
            self.emit(f"        mov {self.target.acc}, {addr}")

    def _emit_field_store(self, *, addr: str, field_size: int) -> None:
        """Emit the struct-field store instruction matching *field_size*.

        Mirror of :meth:`_emit_field_load`.  Byte fields use ``mov byte
        [addr], al``; word fields on a 32-bit target use ``mov word
        [addr], ax``; all other widths store the full accumulator.  The
        source register is the width-matched accumulator slice, so
        callers only need to evaluate the rhs into the accumulator
        before calling.
        """
        if field_size == 1:
            self.emit(f"        mov byte {addr}, al")
        elif field_size == 2 and self.target.int_size == 4:
            self.emit(f"        mov word {addr}, ax")
        else:
            self.emit(f"        mov {addr}, {self.target.acc}")

    def _emit_global_array(self, name: str, /) -> None:
        """Lay down storage for one file-scope array global.

        Skips extern arrays.  Zero-initialized arrays are deferred to
        ``self.bss_vars``.  Initialized struct arrays unroll each
        element's fields and pad to the declared size; primitive-typed
        arrays use one ``db`` / ``dw`` / ``dd`` directive carrying every
        element.
        """
        declaration = self.global_arrays[name]
        if name in self.extern_globals:
            # Storage lives in another translation unit; references
            # to the bare ``_g_<name>`` label still resolve.
            return
        is_byte = declaration.type_name in self.BYTE_TYPES
        is_struct = declaration.type_name.startswith("struct ")
        # Stride is sizeof(element) for every shape: structs sum
        # field widths, ``char`` / ``unsigned char`` resolve to 1,
        # ``unsigned short`` to 2, pointer / ``int`` / ``unsigned int`` to
        # ``int_size``.  Unifies what used to be a binary
        # byte-vs-int_size switch that silently miscompiled
        # ``unsigned short`` globals.
        stride = self._type_size(declaration.type_name)
        if is_struct and declaration.init is not None:
            struct_name = declaration.type_name[len("struct ") :]
            layout = self.struct_layouts[struct_name]
            lines: list[str] = []
            for element in declaration.init.elements:
                assert isinstance(element, StructInitializer)
                assert element.positional is not None, "array-of-struct globals require positional initializers"
                for i, (field_name, info) in enumerate(layout.items()):
                    field_size = info.field_size
                    value = self._constant_expression(element.positional[i]) if i < len(element.positional) else "0"
                    if field_size == 1:
                        lines.append(f"db {value}")
                    elif field_size == 2:
                        lines.append(f"dw {value}")
                    elif field_size == 4:
                        lines.append(f"dd {value}")
                    else:
                        lines.append(f"times {field_size} db 0")
            count = len(declaration.init.elements)
            size_expression = self._constant_expression(declaration.size)
            lines.append(f"times ({size_expression}-{count})*{stride} db 0")
            self._maybe_emit_data_header()
            self._emit_global_export(name)
            self.emit(f"{self._global_label(name)}: {lines[0]}")
            for line in lines[1:]:
                self.emit(f"        {line}")
            return
        if name in self.array_types and declaration.init is not None:
            # Multidimensional scalar array: flatten the (possibly nested)
            # row-major initializer into one contiguous run of cells, then
            # zero-fill any remaining element slots so partial / ``{0}``
            # initializers leave a fully-defined image.
            array_type = self.array_types[name]
            total_elements = 1
            dimension: Type = array_type
            while isinstance(dimension, ArrayType):
                total_elements *= dimension.count
                dimension = dimension.pointee
            flat = self._flatten_array_init(declaration.init, name=name, total=total_elements, line=declaration.line)
            int_directive = "dd" if self.target.int_size == 4 else "dw"
            if is_byte:
                directive = "db"
            elif stride == 2 and stride < self.target.int_size:
                directive = "dw"
            else:
                directive = int_directive
            rendered = [
                self.new_string_label(element.content) if isinstance(element, String) else self._constant_expression(element)
                for element in flat
            ]
            self._maybe_emit_data_header()
            self._emit_global_export(name)
            self.emit(f"{self._global_label(name)}: {directive} {', '.join(rendered)}")
            if len(flat) < total_elements:
                self.emit(f"        times ({total_elements}-{len(flat)})*{stride} db 0")
            return
        if declaration.init is not None:
            # Match the data-cell width to the element width:
            # ``db`` for byte, ``dw`` for halfword (``unsigned short``),
            # ``dd`` / ``dw`` for full-int (``int_directive``).
            int_directive = "dd" if self.target.int_size == 4 else "dw"
            if is_byte:
                directive = "db"
            elif stride == 2 and stride < self.target.int_size:
                directive = "dw"
            else:
                directive = int_directive
            rendered = [
                self.new_string_label(element.content) if isinstance(element, String) else self._constant_expression(element)
                for element in declaration.init.elements
            ]
            self._maybe_emit_data_header()
            self._emit_global_export(name)
            self.emit(f"{self._global_label(name)}: {directive} {', '.join(rendered)}")
            return
        # Multidimensional arrays use the registered ArrayType for the total
        # byte count so all dimensions contribute (row-major contiguous storage).
        # Single-dimension arrays use the legacy size-expression * stride path
        # so their output is byte-identical.
        if name in self.array_types:
            array_type = self.array_types[name]
            total_bytes = array_type.sizeof(pointer_width=self.target.int_size, scalar_width=self._type_size)
            self.bss_vars.append((name, str(total_bytes)))
            return
        size_expression = self._constant_expression(declaration.size)
        # Fold ``size * stride`` at compile time when the size is a
        # plain integer — the self-hosted assembler in user/programs/asm.c
        # uses flat operator precedence, so emitting ``(N)*4`` next
        # to surrounding ``+`` / ``-`` (as the BSS chain does) makes
        # the self-host group ``(N) * (4 - <next_term>)`` instead of
        # ``(N)*4`` first.  Pre-folding to a literal sidesteps that.
        if stride == 1:
            byte_count = size_expression
        elif size_expression is not None and size_expression.isdigit():
            byte_count = str(int(size_expression) * stride)
        else:
            byte_count = f"({size_expression})*{stride}"
        self.bss_vars.append((name, byte_count))

    def _emit_global_export(self, name: str, /) -> None:
        """Emit the per-definition export directive matching the active mode.

        Object mode emits ``global <name>`` so the C-conformant symbol
        is visible to external linkers (the ``<name>:`` label follows
        from :meth:`_global_label`).  NASM reserved words (``abs``,
        ``seg``, ``wrt``) need special handling: ``global $abs`` is
        rejected by NASM >= 3.x, so we use ``%deftok`` to mint a
        non-reserved alias whose expansion carries the raw token
        through the ``global`` directive parser.

        Object mode additionally emits a ``_g_<name>:`` label on the
        line just before the storage so inline-asm callers that use the
        legacy ``_g_<name>`` spelling (e.g. the self-hosted assembler in
        ``user/programs/asm.c``) resolve to the same address as the
        C-conformant ``<name>``.  This is the mirror of the flat-mode
        alias below — and it must be a real label, not an ``equ``: the
        ccobj relocation scanner records addresses only for labels with
        an emit offset, so an ``_g_<name> equ <name>`` alias would be
        seen as a defined label (and picked as a relocation target) yet
        carry no address, and ccld would reject it as an unknown symbol.

        Flat-binary / kernel mode emits ``<name> equ _g_<name>`` so
        inline-asm callers that reference the C-conformant name resolve
        to the same address as cc.py-internal references via
        ``_g_<name>``.  NASM resolves the forward reference; the alias
        adds no output bytes.  Mirrors the manual ``asm("name equ
        _g_name");`` pattern several kernel drivers already use.
        """
        if self.object_mode:
            if name in self.NASM_RESERVED_WORDS:
                alias = f"__nasm_global_{name}"
                self.emit(f'%deftok {alias} "{name}"')
                self.emit(f"global {alias}")
            else:
                self.emit(f"global {name}")
            self.emit(f"_g_{name}:")
        else:
            self.emit(f"{name} equ _g_{name}")

    def _emit_global_scalar(self, name: str, /) -> None:
        """Lay down storage for one file-scope scalar global.

        Skips register-aliased / asm-symbol / extern names (their
        storage lives elsewhere).  Zero-initialized scalars are
        appended to ``self.bss_vars``; struct initializers are unrolled
        into per-field directives; everything else takes one ``db`` /
        ``dw`` / ``dd`` cell.
        """
        declaration = self.global_scalars[name]
        if name in self.register_aliased_globals:
            # Storage lives in the aliased CPU register, not memory,
            # so no ``_g_<name>`` label is emitted.
            return
        if name in self.asm_symbol_globals:
            # Storage lives in an existing asm symbol, not here,
            # so no ``_g_<name>`` label is emitted.
            return
        if name in self.extern_globals:
            # Storage lives in another translation unit; references
            # still resolve to ``_g_<name>`` (matching the symbol the
            # owning .c file emits).
            return
        if declaration.init is None:
            if declaration.pointer_array_dimensions is not None:
                # Pointer-to-array global: one pointer-sized cell regardless
                # of the (array) element type.
                stride = self.target.int_size
            elif declaration.type_name.startswith("struct ") and not declaration.type_name.endswith("*"):
                stride = self.struct_sizes[declaration.type_name[len("struct ") :]]
            elif self._is_byte_scalar_global(name):
                stride = 1
            else:
                # Use _type_size so double (8 bytes), unsigned short (2 bytes),
                # etc. get the correct allocation rather than always
                # falling back to int_size (4 bytes on x86-32).
                try:
                    stride = self._type_size(declaration.type_name)
                except CompileError:
                    stride = self.target.int_size
            self.bss_vars.append((name, str(stride)))
            return
        if isinstance(declaration.init, StructInitializer):
            tag = declaration.type_name[len("struct ") :]
            layout = self.struct_layouts[tag]
            init = declaration.init
            if init.designated is not None:
                value_by_field = init.designated
            else:
                assert init.positional is not None
                field_order = list(layout.keys())
                value_by_field = dict(zip(field_order, init.positional, strict=False))
            directives = []
            for field_name, info in layout.items():
                field_size = info.field_size
                value_node = value_by_field.get(field_name)
                value = self._constant_expression(value_node) if value_node is not None else "0"
                if field_size == 1:
                    directives.append(f"db {value}")
                elif field_size == 2:
                    directives.append(f"dw {value}")
                elif field_size == 4:
                    directives.append(f"dd {value}")
                else:
                    directives.append(f"times {field_size} db 0")
            self._maybe_emit_data_header()
            self._emit_global_export(name)
            self.emit(f"{self._global_label(name)}: {directives[0]}")
            for directive in directives[1:]:
                self.emit(f"        {directive}")
            return
        init_expression = self._constant_expression(declaration.init)
        int_directive = "dd" if self.target.int_size == 4 else "dw"
        directive = "db" if self._is_byte_scalar_global(name) else int_directive
        self._maybe_emit_data_header()
        self._emit_global_export(name)
        self.emit(f"{self._global_label(name)}: {directive} {init_expression}")

    def _emit_global_storage(self) -> None:
        """Emit ``_g_<name>`` data cells for every initialized global, once at tail.

        Scalars lay out as a single ``dw`` / ``dd`` cell (target's native
        int width) / ``db`` (byte scalars) with the constant initializer.
        Initialized arrays use ``db`` / ``dw`` / ``dd`` literals matching
        the element type.

        In *user* mode, zero-initialized globals are deferred to BSS:
        collected in ``self.bss_vars`` and emitted by ``_emit_bss_trailer``
        as EQU definitions pointing past the binary end.  In *kernel* mode,
        zero-initialized globals are also collected in ``self.bss_vars``
        and emitted by ``_emit_kernel_bss_trailer`` as ``resb`` reservations
        inside the kernel's ``.bss nobits`` section — keeping the zero
        bytes off the on-disk kernel image.
        """
        if not self.global_scalars and not self.global_arrays:
            return
        # In object mode the initialized-globals chunk belongs in
        # ``section .data`` so the linker can place writable data
        # independently of code.  The switch + comment are emitted
        # once, lazily, on the first initialized cell — purely
        # zero-init globals end up in ``self.bss_vars`` and never
        # need ``.data``.  ``_data_header_emitted`` tracks whether
        # we've written the header yet within this call.  In flat
        # mode the header is emitted eagerly up front, matching the
        # long-standing layout.
        self._data_header_emitted = False
        if not self.object_mode:
            self._maybe_emit_data_header()
        for name in sorted(self.global_scalars):
            self._emit_global_scalar(name)
        for name in sorted(self.global_arrays):
            self._emit_global_array(name)

    def _emit_inline_body(self, name: str, /) -> None:
        """Emit the stored body for an ``always_inline`` function.

        Local labels (``.foo:`` / ``.bar:``) are renamed with a
        per-call-site suffix so that multiple inline sites of the
        same function don't produce duplicate labels.  The asm text
        is emitted line-by-line with the same indentation style cc.py
        uses for file-scope inline-asm blocks.
        """
        body = decode_string_escapes(self.inline_bodies[name])
        self.inline_call_counter += 1
        suffix = f"_inl{self.inline_call_counter}"
        label_pattern = re.compile(r"^\s*(\.\w+)\s*:", re.MULTILINE)
        labels = {match.group(1) for match in label_pattern.finditer(body)}
        for label in labels:
            new_label = f"{label}{suffix}"
            body = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(label)}(?![A-Za-z0-9_])", new_label, body)
        self.emit(f";; --- inline {name} ---")
        for line in body.splitlines():
            if line:
                self.emit(line if line.startswith((" ", "\t", ".")) else f"        {line}")

    def _emit_kernel_bss_trailer(self) -> None:
        """Emit kernel-mode zero-init globals as a ``section .bss`` block.

        The kernel binary declares ``section .bss nobits follows=.text``
        in kernel/arch/x86/kernel.asm; switching to ``.bss`` here parks each
        ``resb N`` reservation in that section so the zero bytes never
        ride on disk.  Switch back to ``.text`` afterwards so the next
        ``%include``'d kasm (and any inline kernel code that follows)
        lands in the code section.
        """
        if not self.bss_vars:
            return
        self.emit(";; --- kernel BSS (zero-initialized) ---")
        self.emit("section .bss")
        for name, size_expression in self.bss_vars:
            self.emit(f"{self._global_label(name)}: resb {size_expression}")
        self.emit("section .text")

    def _emit_libbboeos_call(self, name: str, /) -> None:
        """Emit a ``call`` to the named libbboeos entry point.

        In flat mode this is the direct ``call FUNCTION_NAME``
        (``E8 <rel32>``).  In object mode it's the indirect
        ``call [FUNCTION_NAME_PTR]`` (``FF 15 <abs32>``), which fetches
        the target from the FUNCTION_POINTER_TABLE at libbboeos offset 0x800
        and is base-invariant — the bytes survive ``ccld`` relocation
        without any per-site patching.
        """
        if self.object_mode:
            self.emit(f"        call [{name}_PTR]")
        else:
            self.emit(f"        call {name}")

    def _emit_libbboeos_jcc(self, condition: str, name: str, /) -> None:
        """Emit a conditional jump to the named libbboeos entry.

        ``condition`` is the x86 mnemonic (``jc`` / ``jnc``) for the
        predicate under which the jump should be taken.  In flat mode
        this is the direct ``<cond> FUNCTION_NAME``.  In object mode
        there is no indirect conditional-jump form, so we invert the
        predicate: ``<inverse> skip; jmp [FUNCTION_NAME_PTR]; skip:``.
        Costs ~4 extra bytes per site relative to the flat form.
        """
        if self.object_mode:
            inverse = {"jc": "jnc", "jnc": "jc", "je": "jne", "jne": "je"}[condition]
            skip_label = f".libbboeos_skip_{self.new_label()}"
            self.emit(f"        {inverse} {skip_label}")
            self.emit(f"        jmp [{name}_PTR]")
            self.emit(f"{skip_label}:")
        else:
            self.emit(f"        {condition} {name}")

    def _emit_libbboeos_jmp(self, name: str, /) -> None:
        """Emit a ``jmp`` to the named libbboeos entry point.

        Object mode uses the indirect ``jmp [FUNCTION_NAME_PTR]``
        (``FF 25 <abs32>``); see :meth:`_emit_libbboeos_call`.
        """
        if self.object_mode:
            self.emit(f"        jmp [{name}_PTR]")
        else:
            self.emit(f"        jmp {name}")

    def _emit_load_var(self, name: str, /, *, register: str = "bx") -> None:
        """Load a variable's value into *register*.

        Checks pinned registers first, then constant aliases, then
        falls back to the memory frame slot.  Local stack arrays
        compute their base address (``lea`` or label-immediate) rather
        than dereferencing a pointer slot.
        """
        if name in self.pinned_register:
            source = self.pinned_register[name]
            if len(register) < len(source):
                source = self.target.low_word(source)
            self.emit(f"        mov {register}, {source}")
        elif name in self.register_aliased_globals:
            source = self.register_aliased_globals[name]
            if len(register) < len(source):
                source = self.target.low_word(source)
            if source != register:
                self.emit(f"        mov {register}, {source}")
        elif name in self.constant_aliases:
            self.emit(f"        mov {register}, {self.constant_aliases[name]}")
        elif name in self.local_stack_arrays:
            if self.elide_frame:
                self.emit(f"        mov {register}, _l_{name}")
            else:
                offset = self.locals[name]
                self.emit(f"        lea {register}, [{self.target.base_register}-{offset}]")
        else:
            self.emit(f"        mov {register}, [{self._local_address(name)}]")

    def _emit_long_after_syscall(self) -> None:
        """Settle a long-returning syscall's value into the target's shape.

        The kernel always returns 32-bit longs in EAX.  Targets whose
        ``unsigned long`` storage uses a different shape (16-bit's
        DX:AX) declare the bridging instructions in
        ``target.LONG_AFTER_SYSCALL``; targets that don't need any
        normalization (32-bit, where the value already lives in EAX)
        omit the attribute and the helper emits nothing.
        """
        for instruction in getattr(self.target, "LONG_AFTER_SYSCALL", ()):
            self.emit(f"        {instruction}")

    def _emit_long_store(self, *, expression: Node, name: str) -> None:
        """Generate a long-typed expression and spill it to local ``name``.

        Used for both ``unsigned long`` locals and virtual-long locals
        (where 32-bit targets fold ``unsigned long`` into ``unsigned int``
        and ``discover_virtual_long_locals`` registers the name as
        long-valued).  Virtual-long locals stay live in the target's
        long register pair (EAX, or DX:AX in 16-bit) without a frame
        spill; real ``unsigned long`` locals get their pair stored to
        the dual-word frame slot.
        """
        self.ax_clear()
        self.generate_long_expression(expression)
        if name in self.virtual_long_locals:
            self.live_long_local = name
            return
        address = self._local_address(name)
        if self.elide_frame:
            self.emit(f"        mov [{address}], {self.target.acc}")
            if isinstance(self.target, X86CodegenTarget16):
                self.emit(f"        mov [{address}+2], {self.target.dx_register}")
        else:
            low_offset = self.locals[name]
            self.emit(f"        mov [{self.target.base_register}-{low_offset}], {self.target.acc}")
            if isinstance(self.target, X86CodegenTarget16):
                self.emit(f"        mov [{self.target.base_register}-{low_offset - 2}], {self.target.dx_register}")
        self.ax_is_byte = False
        self.ax_local = None

    def _emit_long_to_eax(self) -> None:
        """Place a long, currently in the target's shape, into EAX.

        Mirror of :meth:`_emit_long_after_syscall` for the call-site
        direction: when feeding an EAX-shaped callee (such as the
        ``FUNCTION_PRINT_DATETIME`` libbboeos entry point) from a long held
        in the target's native representation.  Targets that already
        hold longs in EAX omit ``LONG_TO_EAX`` and the helper emits
        nothing.
        """
        for instruction in getattr(self.target, "LONG_TO_EAX", ()):
            self.emit(f"        {instruction}")

    def _emit_member_address(self, *, const_base: str, base_is_register: bool, is_global_label: bool, offset: int) -> None:
        """Emit the address-yielding terminal for a bare struct-value / array member.

        Reproduces the three legacy sequences: a global label folds the offset
        into an immediate ``mov``; a local frame base uses ``lea``; a register
        base uses ``lea [reg+offset]`` (or a bare ``mov acc, reg`` when the
        offset is zero).
        """
        acc = self.target.acc
        if base_is_register:
            if offset:
                self.emit(f"        lea {acc}, [{const_base}+{offset}]")
            else:
                self.emit(f"        mov {acc}, {const_base}")
            return
        if is_global_label:
            # Global struct: load the address as a label-arithmetic immediate.
            if offset:
                self.emit(f"        mov {acc}, {const_base}+{offset}")
            else:
                self.emit(f"        mov {acc}, {const_base}")
            return
        # Local frame base: lea the field address.
        if offset:
            self.emit(f"        lea {acc}, [{const_base}+{offset}]")
        else:
            self.emit(f"        lea {acc}, [{const_base}]")

    def _emit_member_index_base(self, *, arrow: bool, object_name: str, register: str) -> None:
        """Load the struct base address into *register* for member-index codegen.

        Arrow form: loads the pointer value from the variable.
        Dot form: loads the frame address of the local struct via LEA.
        """
        if arrow:
            self._emit_load_var(object_name, register=register)
        elif object_name in self.locals:
            local_addr = self._local_address(object_name)
            self.emit(f"        lea {register}, [{local_addr}]")
        elif object_name in self.global_scalars:
            global_addr = self._local_address(object_name)
            self.emit(f"        lea {register}, [{global_addr}]")
        else:
            message = f"undefined variable '{object_name}'"
            raise CompileError(message)

    def _emit_member_index_resolved_store(self, place: SubscriptPlace, value: Node, /) -> None:
        """Store *value* into ``base.field[index]`` via :meth:`_resolve_member_index`.

        A constant index resolves the address without touching the
        accumulator, so the rhs is evaluated into the accumulator first and
        stored directly (no spill).  A variable index evaluates and scales the
        index through the accumulator, so the rhs is pushed first and recovered
        after the address is live in the base register.
        """
        self.ax_clear()
        if isinstance(place.index, Int):
            self.generate_expression(value)  # rhs in the accumulator
            operand = self._resolve_member_index(place)
            destination = self._build_address(operand.base, operand.displacement)
            self._emit_field_store(addr=destination, field_size=operand.field_size)
            self.ax_clear()
            return
        self.generate_expression(value)
        self.emit(f"        push {self.target.acc}")  # save rhs across the index eval
        operand = self._resolve_member_index(place)
        self.emit(f"        pop {self.target.acc}")
        destination = self._build_address(operand.base, operand.displacement)
        self._emit_field_store(addr=destination, field_size=operand.field_size)
        self.ax_clear()

    def _emit_member_scalar_resolved_store(self, place: MemberPlace, value: Node, /) -> None:
        """Store *value* into a non-array member via :meth:`resolve_address`.

        A static base (dot access on a named struct value, or the
        ``((struct T *)&local)->field`` cast fast path) resolves without
        emitting any code, so the rhs is evaluated into the accumulator first
        and stored directly — matching the legacy no-spill dot store.  A
        register base (arrow ``ptr->field``, chained ``a->b.c``, general
        via-expr) materializes through the accumulator, so the rhs is pushed
        first and recovered after the base register is live.  Bitfield members
        dispatch to the dedicated literal / const-fold / read-modify-write
        sequences in :meth:`_emit_resolved_field_store`.
        """
        self.ax_clear()
        # Static base (dot / cast-fast-path local): the base resolves without
        # emitting any code, so resolve it first.  A bitfield Int write into a
        # known local byte const-folds to a single ``mov byte`` (no rhs eval);
        # otherwise the rhs is evaluated and stored, matching the legacy
        # no-spill dot store.
        if self._member_base_is_static(place):
            operand = self.resolve_address(place)
            if operand.bitfield is not None:
                address = self._build_address(operand.base, operand.displacement)
                if operand.bitfield.bit_width == 1 and isinstance(value, Int) and value.value in (0, 1):
                    self._emit_bitfield_write_literal(operand.bitfield, addr=address, value=value.value)
                    return
                if self._try_fold_bitfield_int_store(operand, value):
                    return
            self.generate_expression(value)
            self._emit_resolved_field_store(operand, value)
            return
        # 1-bit literal bitfield write needs no rhs in a register; resolve the
        # base (the remaining acc-preserving / clobbering register bases never
        # read the accumulator before the load) and emit the single
        # ``and``/``or`` byte directly, with no spill.
        bitfield_literal = self._member_bitfield_literal(place, value)
        if bitfield_literal is not None:
            operand = self.resolve_address(place)
            address = self._build_address(operand.base, operand.displacement)
            self._emit_bitfield_write_literal(operand.bitfield, addr=address, value=bitfield_literal)
            return
        # Accumulator-preserving register base (the arrow named-pointer whose
        # base load is a bare ``mov bx, [ptr]``): the rhs is evaluated into the
        # accumulator first, then the base is materialized into BX without
        # disturbing it, then stored — no spill.
        if self._member_base_preserves_accumulator(place):
            self.generate_expression(value)
            operand = self.resolve_address(place)
            self._emit_resolved_field_store(operand, value)
            return
        # Accumulator-clobbering base (chained / general via-expr): the base
        # materialization evaluates a pointer expression through the
        # accumulator into BX, so it must run first; the rhs is then evaluated
        # into the accumulator and stored.  This matches the legacy base-first
        # chained / via-expr ordering (the rhs shares BX's live range, as it did
        # before this refactor — a complex rhs that itself clobbers BX was never
        # supported on this shape).
        operand = self.resolve_address(place)
        self.generate_expression(value)
        self._emit_resolved_field_store(operand, value)

    def _emit_mov_from_acc(self, register: str, /) -> None:
        """Move the accumulator into *register*, narrowing the low word when needed.

        No-op when *register* is already the accumulator.  Used after
        ``generate_expression`` (or any other AX-leaving sequence) when
        the value needs to land in a 16-bit destination on a 32-bit
        target — in that case the low word of EAX is the right source.
        """
        if register == self.target.acc:
            return
        source = self.target.low_word(self.target.acc) if len(register) < len(self.target.acc) else self.target.acc
        self.emit(f"        mov {register}, {source}")

    def _emit_multidim_member_address(
        self, object_name: str, *, arrow: bool, field_name: str, indices: list[Node], line: int
    ) -> MemoryOperand:
        """Emit the row-major address of ``g.field[i][j]...`` / ``p->field[i][j]...``.

        Returns a :class:`MemoryOperand` naming the resolved element address
        (the multidim-member branch of :meth:`resolve_address`).  Mirrors
        :meth:`_emit_multidim_subscript_address` but roots the linear byte
        offset at ``(struct base address + field byte offset)`` and walks
        the FIELD's dimensions (parsed from its declared multidim type).
        Constant indices and the field offset fold into a static
        displacement; dynamic indices are scaled and summed into BX, then
        the full field base is materialized into SI (so ``[si+disp]`` stays
        legal at 16-bit, where ``[bp+bx]`` is not).  The terminal load/store
        uses the field's innermost element size (handles int / 4-byte
        elements, bypassing the bespoke member-index 1/2-byte gate).

        Raises:
            CompileError: if the subscript count does not match the field's
            dimension count.

        """
        info = self._resolve_member_index_layout(arrow=arrow, line=line, member_name=field_name, object_name=object_name)
        field_offset = info.byte_offset
        array_type = Type.from_string(info.type_name)
        assert isinstance(array_type, ArrayType)
        dimension_counts: list[int] = []
        element_type: Type = array_type
        while isinstance(element_type, ArrayType):
            dimension_counts.append(element_type.count or 0)
            element_type = element_type.pointee
        if len(indices) != len(dimension_counts):
            message = f"wrong number of subscripts for '{field_name}'"
            raise CompileError(message, line=line)
        element_size = self._type_size(element_type.to_string())
        # Per-position byte stride: product of inner dimension counts after
        # this position, times the element size (row-major Horner).
        strides: list[int] = []
        running = element_size
        for count in reversed(dimension_counts):
            strides.append(running)
            running *= count
        strides.reverse()
        bx = self.target.bx_register
        si = self.target.si_register
        displacement = field_offset
        # Pre-evaluate every dynamic index into a scaled byte offset, stashing
        # each on the stack before any BX accumulation (so an index pinned to
        # BX is read before BX is first clobbered) — same ordering as the
        # contiguous-array emitter.
        dynamic_index_count = 0
        protect_bx = self._bx_holds_pinned_var()
        for index_node, stride in zip(indices, strides, strict=True):
            if isinstance(index_node, Int):
                displacement += index_node.value * stride
                continue
            if protect_bx:
                self.emit(f"        push {bx}")
            self.generate_expression(index_node)  # AX = dynamic index
            self._emit_scale_index(self.target.acc, scale=stride)  # AX = byte offset
            if protect_bx:
                self.emit(f"        pop {bx}")
            self.emit(f"        push {self.target.acc}")
            dynamic_index_count += 1
        index_register: str | None = None
        for _ in range(dynamic_index_count):
            if index_register is None:
                self.emit(f"        pop {bx}")
                index_register = bx
            else:
                self.emit(f"        pop {self.target.acc}")
                self.emit(f"        add {bx}, {self.target.acc}")
        # Materialize the struct base into SI.  Arrow: load the pointer value;
        # dot: lea the struct's frame/label address.  The field offset stays in
        # the displacement so ``[si+disp]`` carries it.
        if arrow:
            self._emit_load_var(object_name, register=si)
        else:
            _base_kind, base = self._variable_base(object_name, line=line)
            self.emit(f"        lea {si}, [{base}]")
        if index_register is not None:
            self.emit(f"        add {si}, {index_register}")
            index_register = None
        return MemoryOperand(
            base=si,
            base_kind="register",
            displacement=displacement,
            element_size=element_size,
            field_size=element_size,
            index=index_register,
        )

    def _emit_multidim_subscript_address(self, base_name: str, indices: list[Node], /, *, line: int) -> MemoryOperand:
        """Emit the row-major address of ``base_name[i0][i1]...`` and return its operand.

        Computes the linear byte offset by Horner expansion over the
        registered dimensions: byte offset ``= sum(i_p * stride_bytes_p)``
        where ``stride_bytes_p`` is the product of the inner dimensions
        after position *p* times the element size.  Constant indices fold
        into a static displacement; dynamic indices are evaluated into the
        accumulator, scaled, and summed into the BX index register (the
        same convention used by :meth:`_accumulate_subscript`).  Returns a
        :class:`MemoryOperand` naming the resolved element address sized at
        the innermost element width, so the shared terminal load/store runs
        unchanged (this is the contiguous-multidim branch of
        :meth:`resolve_address`).

        Raises:
            CompileError: if the subscript count does not match the array's
            dimension count.

        """
        array_type = self.array_types[base_name]
        dimension_counts: list[int] = []
        element_type: Type = array_type
        while isinstance(element_type, ArrayType):
            dimension_counts.append(element_type.count or 0)
            element_type = element_type.pointee
        if len(indices) != len(dimension_counts):
            message = f"wrong number of subscripts for '{base_name}'"
            raise CompileError(message, line=line)
        element_size = self._type_size(element_type.to_string())
        # Per-position byte stride: product of the inner dimension counts
        # after this position, times the element size.
        strides: list[int] = []
        running = element_size
        for count in reversed(dimension_counts):
            strides.append(running)
            running *= count
        strides.reverse()
        base_kind, base = self._variable_base(base_name, line=line)
        displacement = 0
        bx = self.target.bx_register
        # Pre-evaluate every dynamic index: compute its scaled byte offset into AX
        # and push it to the stack, protecting BX across each expression via
        # push/pop when BX holds a pinned variable.  Evaluating all expressions
        # before any BX accumulation ensures that an index variable pinned to BX
        # (e.g. the innermost loop counter k in a triple-nested loop) is read from
        # the correct register before BX is first overwritten by the accumulator.
        dynamic_index_count = 0
        protect_bx = self._bx_holds_pinned_var()
        for index_node, stride in zip(indices, strides, strict=True):
            if isinstance(index_node, Int):
                displacement += index_node.value * stride
                continue
            if protect_bx:
                self.emit(f"        push {bx}")
            self.generate_expression(index_node)  # AX = dynamic index
            self._emit_scale_index(self.target.acc, scale=stride)  # AX = byte offset
            if protect_bx:
                self.emit(f"        pop {bx}")
            self.emit(f"        push {self.target.acc}")  # stash scaled offset
            dynamic_index_count += 1
        # Accumulate all stashed offsets into BX (sum; addition is commutative so
        # pop order does not affect the result).
        index_register: str | None = None
        for _ in range(dynamic_index_count):
            if index_register is None:
                self.emit(f"        pop {bx}")
                index_register = bx
            else:
                self.emit(f"        pop {self.target.acc}")
                self.emit(f"        add {bx}, {self.target.acc}")
        if index_register is not None and base_kind == "frame":
            # A frame base (BP-relative) cannot be combined with a BX index in a
            # 16-bit effective address ([BP+BX] is illegal — a 16-bit index must
            # be SI/DI).  Materialize the full address into SI (as the
            # double-index emitter does) so ``[si+disp]`` is valid at both widths.
            si = self.target.si_register
            self.emit(f"        lea {si}, [{base}]")
            self.emit(f"        add {si}, {index_register}")
            base = si
            index_register = None
        return MemoryOperand(
            base=base,
            base_kind="register",
            displacement=displacement,
            element_size=element_size,
            field_size=element_size,
            index=index_register,
        )

    def _emit_place_address_of(self, place: Place, /) -> None:
        """Emit the address of *place* into the accumulator (``&place``).

        Handles the named-variable form (``&x``), the pointee form
        (``&*p`` / ``&*(T *)e`` == the pointer value), the scalar member
        forms (``&obj.field`` / ``&ptr->field``) and the element-address
        form (``&base.field[index]``).
        """
        if isinstance(place, VariablePlace):
            # ``&x`` — reproduces the legacy address-of codegen byte-for-byte:
            # locals lea their frame address, globals/constants mov their
            # label, and out_register parameters have no addressable storage.
            name = place.name
            if name in self.out_register_locals:
                message = f"cannot take address of out_register parameter '{name}'"
                raise CompileError(message, line=place.line)
            address = self._local_address(name)
            if name in self.locals:
                self.emit(f"        lea {self.target.acc}, [{address}]")
            else:
                self.emit(f"        mov {self.target.acc}, {address}")
            self.ax_local = None
            self.ax_is_byte = False
            return
        if isinstance(place, DereferencePlace):
            # ``&*p`` / ``&*(T *)e`` collapses to the pointer value itself —
            # evaluate the pointer expression into the accumulator.  No load
            # through the pointer happens (that would be the rvalue ``*p``).
            self.generate_expression(place.pointer)
            return
        if self._is_member_index_place(place):
            self.ax_clear()
            operand = self._resolve_member_index(place)
            address = self._build_address(operand.base, operand.displacement, index=operand.index or "")
            self.emit(f"        lea {self.target.acc}, {address}")
            self.ax_clear()
            return
        assert isinstance(place, MemberPlace)
        base = place.base
        # Arrow form: ``&ptr->field`` (named pointer).
        if isinstance(base, DereferencePlace) and isinstance(base.pointer, VariablePlace):
            object_name = base.pointer.name
            struct_type = self.variable_types.get(object_name)
            if struct_type is None:
                message = f"undefined variable '{object_name}'"
                raise CompileError(message, line=place.line)
            if not struct_type.startswith("struct ") or not struct_type.endswith("*"):
                message = f"'->' requires a pointer to struct, got type '{struct_type}'"
                raise CompileError(message, line=place.line)
            tag = struct_type[7:-1]
            info = self._lookup_struct_field(tag, place.member_name, place.line)
            if info.bit_width is not None:
                message = f"cannot take address of bitfield '{place.member_name}'"
                raise CompileError(message, line=place.line)
            self.ax_clear()
            self._emit_load_var(object_name, register=self.target.acc)
            if info.byte_offset:
                self.emit(f"        add {self.target.acc}, {info.byte_offset}")
            self.ax_clear()
            return
        # Dot form: ``&obj.field`` (named struct value).
        if isinstance(base, VariablePlace):
            object_name = base.name
            struct_type = self.variable_types.get(object_name)
            if struct_type is None:
                message = f"undefined variable '{object_name}'"
                raise CompileError(message, line=place.line)
            if struct_type.endswith("*"):
                message = "'&obj.field' requires a struct value, not a pointer; use '&ptr->field' or '&(*ptr).field'"
                raise CompileError(message, line=place.line)
            if not struct_type.startswith("struct "):
                message = f"'.' requires a struct value, got type '{struct_type}'"
                raise CompileError(message, line=place.line)
            tag = struct_type[7:]
            info = self._lookup_struct_field(tag, place.member_name, place.line)
            if info.bit_width is not None:
                message = f"cannot take address of bitfield '{place.member_name}'"
                raise CompileError(message, line=place.line)
            if object_name in self.global_scalars:
                base_label = self._local_address(object_name)
                if info.byte_offset:
                    self.emit(f"        lea {self.target.acc}, [{base_label}+{info.byte_offset}]")
                else:
                    self.emit(f"        lea {self.target.acc}, [{base_label}]")
            elif object_name in self.locals:
                frame_offset = self.locals[object_name]
                if info.byte_offset:
                    self.emit(f"        lea {self.target.acc}, [ebp-{frame_offset}+{info.byte_offset}]")
                else:
                    self.emit(f"        lea {self.target.acc}, [ebp-{frame_offset}]")
            else:
                message = f"undefined variable '{object_name}'"
                raise CompileError(message, line=place.line)
            self.ax_clear()
            return
        message = "unsupported Place shape in _emit_place_address_of"
        raise CompileError(message, line=place.line)

    def _emit_place_call(self, node: PlaceCall, /, *, discard_return: bool = False) -> None:
        """Generate a call through a function-pointer *place*.

        Two place shapes:

        - ``SubscriptPlace(VariablePlace(array), index)`` — ``array[index](args)``.
          Reproduces the legacy generate_indexed_call byte-for-byte (global vs.
          local array, constant vs. variable index, pusha/save path, cdecl arg
          push order, discard_return cleanup).
        - ``DereferencePlace(pointer)`` — ``(*fp)(args)``.  The callee is the
          pointer value: save pinned registers, push args cdecl, evaluate the
          pointer expression into the accumulator, ``call acc``, clean up.
        """
        place = node.place
        if isinstance(place, SubscriptPlace) and isinstance(place.base, VariablePlace):
            self.generate_indexed_call(
                array_name=place.base.name,
                arguments=node.args,
                discard_return=discard_return,
                index=place.index,
                line=node.line,
            )
            return
        if isinstance(place, DereferencePlace):
            self._emit_place_call_through_pointer(node, discard_return=discard_return)
            return
        message = "unsupported Place shape in _emit_place_call"
        raise CompileError(message, line=node.line)

    def _emit_place_call_through_pointer(self, node: PlaceCall, /, *, discard_return: bool = False) -> None:
        """Call through ``(*pointer_expression)(args)``.

        The callee address is the pointer value.  Mirrors the indirect-call
        register-save / cdecl-push sequence of generate_indexed_call but
        evaluates an arbitrary pointer expression into the accumulator
        instead of computing a base+index*stride element address.
        """
        assert isinstance(node.place, DereferencePlace)
        self.si_local = None
        clobbers: frozenset[str] = frozenset(self.target.register_pool)
        saved = self._pinned_registers_to_save(clobbers)
        use_pusha = discard_return and len(saved) >= 3
        if use_pusha:
            self.emit("        pusha")
        else:
            for register in saved:
                self.emit(f"        push {register}")
        for argument in reversed(node.args):
            self._emit_push_arg(argument)
        self.generate_expression(node.place.pointer)  # acc = callee address
        self.emit(f"        call {self.target.acc}")
        if node.args:
            self.emit(f"        add {self.target.stack_register}, {len(node.args) * self.target.int_size}")
        if use_pusha:
            self.emit("        popa")
        else:
            for register in reversed(saved):
                self.emit(f"        pop {register}")
        self.ax_clear()

    def _emit_place_increment_decrement(self, node: PlaceIncrementDecrement, /) -> None:
        """Emit a postfix/prefix ``++`` / ``--`` over a Place.

        Named variables and named-array elements reproduce the legacy
        increment-decrement / IndexAssign lowering byte-for-byte; member and
        dereference places synthesize ``place = place ± delta`` through
        :meth:`_emit_place_store`, reload with a :class:`PlaceLoad`, and — for
        the postfix form — recover the pre-update value with one ``sub`` /
        ``add``.
        """
        place = node.place
        delta_value = abs(node.delta)
        if isinstance(place, VariablePlace):
            # ``x++`` / ``++x`` — byte-identical to the legacy
            # increment-decrement expression codegen: lower ``x ± 1`` through
            # emit_store_local, reload x into the accumulator, then recover the
            # pre-update value for postfix.
            target = place.name
            self._check_defined(target, line=node.line)
            update_expression = BinaryOperation(
                left=Var(line=node.line, name=target),
                line=node.line,
                operation="+" if node.delta > 0 else "-",
                right=Int(line=node.line, value=delta_value),
            )
            self.emit_store_local(expression=update_expression, name=target)
            self.generate_expression(Var(line=node.line, name=target))
            if node.is_postfix:
                reverse = "sub" if node.delta > 0 else "add"
                self.emit(f"        {reverse} {self.target.acc}, {delta_value}")
                self.ax_clear()
            return
        if isinstance(place, SubscriptPlace) and isinstance(place.base, VariablePlace):
            # ``a[i]++`` / ``a[i]--`` on a NAMED array.  The generic store path
            # does not model SubscriptPlace(VariablePlace) (Plan 5), so lower the
            # store through the existing IndexAssign codegen — exactly the way
            # the named-variable arm lowers through emit_store_local — and
            # reload the element with an Index read.  Postfix recovers the
            # pre-update value with one sub/add.
            #
            # Index re-evaluation caveat: the index is evaluated up to three
            # times (store address, store RHS Index, reload Index).  For the
            # supported shapes (a Var, an Int, or pure arithmetic) this is
            # benign and matches C; an index with side effects (a[i++]++) is
            # unsupported / undefined and is not exercised.
            array_name = place.base.name
            self._check_defined(array_name, line=node.line)
            update_expression = BinaryOperation(
                left=Index(array=Var(line=node.line, name=array_name), index=place.index, line=node.line),
                line=node.line,
                operation="+" if node.delta > 0 else "-",
                right=Int(line=node.line, value=delta_value),
            )
            self.generate_index_assign(
                IndexAssign(
                    array=Var(line=node.line, name=array_name),
                    expr=update_expression,
                    index=place.index,
                    line=node.line,
                )
            )
            self.generate_expression(Index(array=Var(line=node.line, name=array_name), index=place.index, line=node.line))
            if node.is_postfix:
                reverse = "sub" if node.delta > 0 else "add"
                self.emit(f"        {reverse} {self.target.acc}, {delta_value}")
                self.ax_clear()
            return
        update_expression = BinaryOperation(
            left=PlaceLoad(line=node.line, place=place),
            line=node.line,
            operation="+" if node.delta > 0 else "-",
            right=Int(line=node.line, value=delta_value),
        )
        self._emit_place_store(place, update_expression)
        self.generate_expression(PlaceLoad(line=node.line, place=place))
        if node.is_postfix:
            reverse = "sub" if node.delta > 0 else "add"
            self.emit(f"        {reverse} {self.target.acc}, {delta_value}")
            self.ax_clear()

    def _emit_place_load(self, place: Place, /) -> None:
        """Load the value at *place* into the accumulator (rvalue)."""
        # Member shapes (dot / arrow / chained scalar, and member-index
        # subscript) own their own base register and never push/pop BX
        # around it; resolve them to a MemoryOperand and run the shared
        # terminal load (bitfield read, array / struct-value lea decay, or
        # width-aware field load).  Multidim array members (``p->v[i][j]``)
        # need the protect-BX guard around their dynamic-index scratch, so they
        # dispatch to the shared subscript terminal ahead of the scalar arms.
        if self._match_multidim_member_chain(place) is not None:
            # Multidim array member (``p->v[i][j]``): row-major addressing folds
            # into resolve_address; run it through the shared protect-BX
            # terminal load.
            self._emit_subscript_resolved_load(place)
            return
        if self._is_member_index_place(place):
            self.ax_clear()
            operand = self._resolve_member_index(place)
            self._emit_resolved_load(operand)
            return
        if isinstance(place, MemberPlace) and self._match_struct_array_member(place) is None:
            self.ax_clear()
            operand = self.resolve_address(place)
            self._emit_resolved_load(operand)
            return
        # Standalone dereference shapes own bespoke load sequences (frame-direct
        # fast path, self-load through the accumulator); route them to dedicated
        # emitters before the generic resolve_address fall-through.
        if (chain := self._uniform_subscript_chain(place)) is not None:
            base_name, indices = chain
            if base_name in self.pointer_array_types or self._is_multidim_array(base_name):
                # ``p[i][j]...`` over a pointer-to-array or contiguous multidim
                # array: row-major (Horner) addressing folds into
                # resolve_address; run it through the shared protect-BX
                # terminal load.
                self._emit_subscript_resolved_load(place)
            else:
                # Array of pointers (or pointer base): reconstruct the
                # deref-rooted ``SubscriptPlace(DereferencePlace(Index(Var)))``
                # node and resolve it like any other lvalue address — the
                # dereference materializes the element pointer into ESI and the
                # outer subscript folds onto it.
                node = self._reconstruct_double_index_place(base_name, indices, line=place.line)
                self._emit_double_index_resolved_load(node)
            return
        if (
            isinstance(place, SubscriptPlace)
            and isinstance(place.base, DereferencePlace)
            and isinstance(place.base.pointer, Index)
            and isinstance(place.base.pointer.array, Var)
        ):
            # ``name[outer][inner]`` already in the deref-rooted shape — the
            # increment/decrement lowering synthesizes a ``PlaceLoad`` over this
            # reconstructed node rather than the uniform chain above.  Resolve it
            # straight through the recursive resolver (the dereference materializes
            # the element pointer into ESI; the outer subscript folds onto it).
            self._emit_double_index_resolved_load(place)
            return
        if isinstance(place, DereferencePlace):
            # Standalone ``*ptr`` load: resolve to a frame-direct or
            # register-base MemoryOperand (no BX scratch), then run the shared
            # width-aware terminal load.
            operand = self.resolve_address(place)
            address = self._build_address(operand.base, operand.displacement, index=operand.index or "")
            self._emit_field_load(addr=address, field_size=operand.field_size)
            self.ax_clear()
            return
        # Generic fall-through: struct-array shapes (arr[i].member and
        # arr[i].member[j]) and the contiguous / pointer-to-array / multidim-
        # member subscript shapes all resolve through the recursive address
        # resolver and run the shared protect-BX terminal load.
        self._emit_subscript_resolved_load(place)

    def _emit_place_store(self, place: Place, value: Node, /) -> None:
        """Store the result of *value* into *place*."""
        if self._match_multidim_member_chain(place) is not None:
            # Multidim array member (``p->v[i][j] = value``): row-major
            # addressing folds into resolve_address; run it through the shared
            # protect-BX terminal store.
            self._emit_subscript_resolved_store(place, value)
            return
        if self._is_member_index_place(place):
            self._emit_member_index_resolved_store(place, value)
            return
        if isinstance(place, MemberPlace) and self._match_struct_array_member(place) is None:
            self._emit_member_scalar_resolved_store(place, value)
            return
        if (chain := self._uniform_subscript_chain(place)) is not None:
            base_name, indices = chain
            if base_name in self.pointer_array_types or self._is_multidim_array(base_name):
                # ``p[i][j]... = value`` over a pointer-to-array or contiguous
                # multidim array: row-major addressing folds into
                # resolve_address; run it through the shared protect-BX
                # terminal store.
                self._emit_subscript_resolved_store(place, value)
            else:
                # Array of pointers: reconstruct the deref-rooted node and store
                # through the recursive resolver.
                node = self._reconstruct_double_index_place(base_name, indices, line=place.line)
                self._emit_resolved_address_store(node, value)
            return
        if (
            isinstance(place, SubscriptPlace)
            and isinstance(place.base, DereferencePlace)
            and isinstance(place.base.pointer, Index)
            and isinstance(place.base.pointer.array, Var)
        ):
            # ``name[outer][inner] = value`` already in the deref-rooted shape
            # (synthesized by the increment/decrement lowering): store through
            # the recursive resolver, mirroring the matching load arm.
            self._emit_resolved_address_store(place, value)
            return
        if isinstance(place, DereferencePlace):
            # Standalone ``*ptr = value``.  The terminal store width is the
            # pointee width; the three sequences below preserve the exact
            # ordering the legacy bespoke store emitted (named-pointer
            # register-alias write, ``&local`` frame-direct fast store, and
            # the general push / evaluate-pointer / pop / store).
            pointer = place.pointer
            if isinstance(pointer, Var):
                pointer_name = pointer.name
                if pointer_name in self.out_register_locals:
                    register = self.out_register_locals[pointer_name]
                    self.generate_expression(value)
                    source = self.target.acc
                    if len(register) < len(source):
                        source = self.target.low_word(source)
                    if register != source:
                        self.emit(f"        mov {register}, {source}")
                    self.ax_clear()
                    return
                holder_type = self.variable_types.get(pointer_name)
                if not holder_type or not holder_type.endswith("*"):
                    message = f"pointer dereference write to non-pointer variable '{pointer_name}'"
                    raise CompileError(message, line=place.line)
                pointee_type = holder_type[:-1]
                self.generate_expression(value)
                self._emit_load_var(pointer_name, register=self.target.si_register)
                # NOTE: this byte/full select reproduces the legacy bespoke
                # emitter exactly and shares its latent gap — a 32-bit
                # ``unsigned short *`` write stores the full accumulator
                # instead of the low word.  Left byte-identical here; fixing
                # it is a separate codegen change (own commit + width test)
                # because it alters emitted bytes for that case.
                if pointee_type in self.BYTE_TYPES:
                    self.emit(f"        mov [{self.target.si_register}], {self.target.low_byte(self.target.acc)}")
                else:
                    self.emit(f"        mov [{self.target.si_register}], {self.target.acc}")
                self.ax_clear()
                return
            # Cast / arbitrary address expression: ``*(T *)e = value``.
            width = self._dereference_place_width(place)
            self.generate_expression(value)
            accumulator = self.target.acc
            # This ``&local`` guard MUST stay in sync with the fast-path guard
            # in _resolve_dereference: we can only call resolve_address(place)
            # here when it will NOT emit a pointer evaluation (the frame-direct
            # case), because the value is already live in the accumulator and an
            # eager pointer eval would clobber it.  The two guards are therefore
            # intentionally duplicated; unifying them needs a non-emitting
            # ("lazy") resolver variant for the general deref store.
            fast_path_target = pointer.expression if isinstance(pointer, Cast) else pointer
            fast_path_name = address_of_variable_name(fast_path_target)
            if fast_path_name is not None and fast_path_name in self.locals:
                operand = self.resolve_address(place)
                destination = self._build_address(operand.base, operand.displacement, index=operand.index or "")
                self._emit_store_accumulator_at_width(destination=destination, width=width)
                return
            scratch = self.target.si_register
            self.emit(f"        push {accumulator}")
            self.generate_expression(pointer)
            self.emit(f"        mov {scratch}, {accumulator}")
            self.emit(f"        pop {accumulator}")
            self._emit_store_accumulator_at_width(destination=f"[{scratch}]", width=width)
            return
        # Generic fall-through: struct-array shapes (arr[i].member and
        # arr[i].member[j]) and the contiguous / pointer-to-array / multidim-
        # member subscript shapes all resolve through the recursive address
        # resolver and run the shared protect-BX terminal store.
        self._emit_subscript_resolved_store(place, value)

    def _emit_pointer_to_array_address(self, base_name: str, indices: list[Node], /, *, line: int) -> MemoryOperand:
        """Emit the row-major address of ``p[i0][i1]...`` for a pointer-to-array ``p``.

        ``p`` has structured type ``PointerType(ArrayType(d1, ... ArrayType(dn, E)))``
        in :attr:`pointer_array_types`.  Unlike a contiguous multidim array
        (where the base is the array's frame/label address), the base here is
        the POINTER VALUE — the address stored in ``p``'s slot.  A full
        subscript supplies ``n + 1`` indices: the outermost strides by
        ``sizeof(pointee array)`` and the remaining dims stride row-major over
        the pointee.  Returns a :class:`MemoryOperand` naming the resolved
        element address (the pointer-to-array branch of :meth:`resolve_address`).

        Mirrors :meth:`_emit_multidim_subscript_address` (Horner over the
        strides) but loads the pointer value into a base register and
        materializes it into SI so ``[si+disp]`` stays legal at 16-bit.

        Raises:
            CompileError: on a partial subscript (fewer than ``n + 1`` indices).

        """
        pointer_type = self.pointer_array_types[base_name]
        pointee_dimension_counts: list[int] = []
        element_type: Type = pointer_type.pointee
        while isinstance(element_type, ArrayType):
            pointee_dimension_counts.append(element_type.count or 0)
            element_type = element_type.pointee
        expected = len(pointee_dimension_counts) + 1
        if len(indices) != expected:
            message = f"unsupported partial subscript of pointer-to-array '{base_name}'"
            raise CompileError(message, line=line)
        element_size = self._type_size(element_type.to_string())
        # ``n + 1`` strides: the inner ``n`` strides come from the pointee
        # dimension Horner; the outermost stride is the size of the whole
        # pointee array (== element_size * product(d1..dn)).
        strides: list[int] = []
        running = element_size
        for count in reversed(pointee_dimension_counts):
            strides.append(running)
            running *= count
        strides.append(running)  # outermost index strides by sizeof(pointee array)
        strides.reverse()
        bx = self.target.bx_register
        si = self.target.si_register
        displacement = 0
        dynamic_index_count = 0
        protect_bx = self._bx_holds_pinned_var()
        for index_node, stride in zip(indices, strides, strict=True):
            if isinstance(index_node, Int):
                displacement += index_node.value * stride
                continue
            if protect_bx:
                self.emit(f"        push {bx}")
            self.generate_expression(index_node)  # AX = dynamic index
            self._emit_scale_index(self.target.acc, scale=stride)  # AX = byte offset
            if protect_bx:
                self.emit(f"        pop {bx}")
            self.emit(f"        push {self.target.acc}")
            dynamic_index_count += 1
        index_register: str | None = None
        for _ in range(dynamic_index_count):
            if index_register is None:
                self.emit(f"        pop {bx}")
                index_register = bx
            else:
                self.emit(f"        pop {self.target.acc}")
                self.emit(f"        add {bx}, {self.target.acc}")
        # Load the POINTER VALUE (the address stored in p's slot) into SI.
        self._emit_load_var(base_name, register=si)
        if index_register is not None:
            self.emit(f"        add {si}, {index_register}")
            index_register = None
        return MemoryOperand(
            base=si,
            base_kind="register",
            displacement=displacement,
            element_size=element_size,
            field_size=element_size,
            index=index_register,
        )

    def _emit_push_arg(self, arg: Node, /) -> None:
        """Push a single argument onto the stack, preferring compact forms.

        Immediates, string labels, NAMED_CONSTANTs, constant aliases,
        and pinned-register variables all avoid the ``mov ax, X / push
        ax`` pair.  Any other form falls back to ``generate_expression``
        followed by ``push ax``.
        """
        if isinstance(arg, Int):
            self.emit(f"        push {arg.value}")
        elif isinstance(arg, String):
            label = self.new_string_label(arg.content)
            self.emit(f"        push {label}")
        elif isinstance(arg, Var) and arg.name in self.NAMED_CONSTANTS:
            self.emit_constant_reference(arg.name)
            self.emit(f"        push {arg.name}")
        elif isinstance(arg, Var) and arg.name in self.constant_aliases:
            self.emit(f"        push {self.constant_aliases[arg.name]}")
        elif isinstance(arg, Var) and arg.name in self.global_arrays:
            self.emit(f"        push {self._global_label(arg.name)}")
        elif isinstance(arg, Var) and arg.name in self.local_stack_arrays:
            if self.elide_frame:
                self.emit(f"        push _l_{arg.name}")
            else:
                offset = self.locals[arg.name]
                self.emit(f"        lea {self.target.acc}, [{self.target.base_register}-{offset}]")
                self.emit(f"        push {self.target.acc}")
        elif isinstance(arg, Var) and arg.name in self.pinned_register:
            self.emit(f"        push {self.pinned_register[arg.name]}")
        else:
            self.generate_expression(arg)
            self.emit(f"        push {self.target.acc}")

    def _emit_register_arg_moves(self, register_args: list[tuple[str, Node]], /) -> None:
        """Emit ``mov`` instructions that place args in target registers.

        Each item carries a ``sources`` set of caller-pinned registers
        it reads (``{caller_pin}`` for simple ``Var`` args,
        recursively-collected for ``BinaryOperation`` args, empty otherwise).
        The topological loop picks an item whose target register is
        not in any other item's source set, which guarantees that
        emitting the item won't trash a value another item still
        needs.  When two simple args form a read/write cycle
        (``mov bx, di`` / ``mov di, bx``), the first item's source is
        copied through AX to break it.  ``BinaryOperation`` args participating
        in a cycle would need a stack temp that the current cdecl-
        fallback never has to emit; we raise a ``CompileError`` so
        the caller can be reshaped instead.
        """
        items: list[dict] = []
        for target, arg in register_args:
            sources = self._arg_pinned_sources(arg)
            primary_source: str | None = None
            if isinstance(arg, Var) and arg.name in self.pinned_register:
                primary_source = self.pinned_register[arg.name]
            elif isinstance(arg, Var) and arg.name in self.param_in_register:
                primary_source = self.param_in_register[arg.name]
            items.append({"target": target, "arg": arg, "source": primary_source, "sources": sources})
        while items:
            progress_index = None
            for index, item in enumerate(items):
                target = item["target"]
                blocked = any(j != index and target in other["sources"] for j, other in enumerate(items))
                if not blocked:
                    progress_index = index
                    break
            if progress_index is not None:
                item = items.pop(progress_index)
                self._emit_register_arg_single(arg=item["arg"], source=item["source"], target=item["target"])
                continue
            # Cycle break: only the simple-Var case supports the AX
            # spill (the BinaryOperation path can't reroute its operand reads).
            item = items[0]
            if not isinstance(item["arg"], Var) or item["source"] is None:
                message = "register-convention call has a cyclic register dependency that involves a complex argument"
                raise CompileError(message, line=getattr(item["arg"], "line", None))
            source = item["source"]
            if len(source) < len(self.target.acc):
                self.emit(f"        movzx {self.target.acc}, {source}")
            else:
                self.emit(f"        mov {self.target.acc}, {source}")
            for other in items:
                if source in other["sources"]:
                    other["sources"] = {register if register != source else self.target.acc for register in other["sources"]}
                    if other["source"] == source:
                        other["source"] = self.target.acc
                        other["arg"] = None  # mark as "load from acc"

    def _emit_register_arg_single(self, *, target: str, arg: Node, source: str | None) -> None:
        """Emit a single register-arg load for :meth:`_emit_register_arg_moves`.

        *source* is the register currently holding the value to move
        (set when the original ``arg`` was a pinned-register ``Var``
        and may have been redirected to ``ax`` after a cycle break).
        A ``None`` *source* means read directly from the AST node.
        """
        if source is not None:
            if source != target:
                if len(source) < len(target):
                    # 16-bit source into wider target: zero-extend.
                    self.emit(f"        movzx {target}, {source}")
                elif len(source) > len(target):
                    # 32-bit source into narrower target: use low word.
                    self.emit(f"        mov {target}, {self.target.low_word(source)}")
                else:
                    self.emit(f"        mov {target}, {source}")
            return
        if isinstance(arg, Int):
            if arg.value == 0 and target != self.target.acc:
                self.emit(f"        xor {target}, {target}")
            else:
                self.emit(f"        mov {target}, {arg.value}")
        elif isinstance(arg, String):
            label = self.new_string_label(arg.content)
            self.emit(f"        mov {target}, {label}")
        elif isinstance(arg, Var) and arg.name in self.NAMED_CONSTANTS:
            self.emit_constant_reference(arg.name)
            self.emit(f"        mov {target}, {arg.name}")
        elif isinstance(arg, Var) and arg.name in self.constant_aliases:
            self.emit(f"        mov {target}, {self.constant_aliases[arg.name]}")
        elif isinstance(arg, Var) and arg.name in self.global_arrays:
            self.emit(f"        mov {target}, {self._global_label(arg.name)}")
        elif isinstance(arg, Var) and arg.name in self.local_stack_arrays:
            if self.elide_frame:
                self.emit(f"        mov {target}, _l_{arg.name}")
            else:
                offset = self.locals[arg.name]
                self.emit(f"        lea {target}, [{self.target.base_register}-{offset}]")
        elif isinstance(arg, Var):
            if self._is_byte_scalar(arg.name):
                # Byte-scalar source into a word target: byte-load +
                # zero-extend, then shuttle into the target if it
                # isn't acc already.
                self.emit_byte_load_zx(f"[{self._local_address(arg.name)}]")
                self._emit_mov_from_acc(target)
            else:
                self.emit(f"        mov {target}, [{self._local_address(arg.name)}]")
        elif isinstance(arg, BinaryOperation):
            # ``_is_simple_arg`` admits BinaryOperation(+ - | & ^, leaf, leaf)
            # plus shifts with Int RHS — all stay in the accumulator. The
            # topological scheduler in ``_emit_register_arg_moves``
            # already verified that ``target`` is not read by any other
            # pending arg.  Evaluate into AX, then move into target.
            self.generate_expression(arg)
            self._emit_mov_from_acc(target)
        else:
            message = f"register-arg target {target} given unexpected complex node {arg!r}"
            raise CompileError(message, line=getattr(arg, "line", None))

    def _emit_resolved_address_store(self, place: Place, value: Node, /) -> None:
        """Store *value* into *place* via the recursive address resolver.

        Evaluates *value* first and stashes it on the stack so the subsequent
        address computation is free to clobber the accumulator, then resolves
        *place* to a MemoryOperand, recovers the value, and writes it at the
        operand's field width.  Used for the array-of-pointers double-index
        shapes (``name[outer][inner] = value``), whose base segment ends in a
        dereference that materializes the element pointer into ESI.
        """
        accumulator = self.target.acc
        self.generate_expression(value)
        self.emit(f"        push {accumulator}")
        operand = self.resolve_address(place)
        self.emit(f"        pop {accumulator}")
        destination = self._build_address(operand.base, operand.displacement, index=operand.index or "")
        self._emit_store_accumulator_at_width(destination=destination, width=operand.field_size)
        self.ax_clear()

    def _emit_resolved_field_store(self, operand: MemoryOperand, value: Node, /) -> None:
        """Store the accumulator into *operand* (bitfield-aware width store).

        The rhs is already in the accumulator.  A bitfield operand dispatches
        to the 1-bit literal, const-fold, or general read-modify-write store;
        otherwise the value is written at the field width.
        """
        address = self._build_address(operand.base, operand.displacement, index=operand.index or "")
        if (info := operand.bitfield) is not None:
            if info.bit_width == 1 and isinstance(value, Int) and value.value in (0, 1):
                self._emit_bitfield_write_literal(info, addr=address, value=value.value)
                return
            self._emit_bitfield_write(info, addr=address)
            return
        allowed_sizes = (1, 2, 4) if self.target.int_size == 4 else (1, 2)
        if operand.field_size not in allowed_sizes:
            message = f"writing field (size {operand.field_size}) not yet supported; use asm()"
            raise CompileError(message)
        self._emit_field_store(addr=address, field_size=operand.field_size)

    def _emit_resolved_load(self, operand: MemoryOperand, /) -> None:
        """Load the value named by *operand* into the accumulator.

        A bitfield operand emits the shift/mask read; a bare array-typed or
        struct-value member (``field_size != element_size`` or carried via the
        ``raw_width`` register-base subscript producing a decayed address)
        decays to its address via ``lea`` / label-immediate; otherwise the
        value is loaded at the field width.  ``raw_width`` operands (the
        ``base.field[i]`` member-index shape) load a word element with a plain
        ``mov`` rather than a ``movzx`` promotion, matching the legacy lowerer.
        """
        address = self._build_address(operand.base, operand.displacement, index=operand.index or "")
        if operand.bitfield is not None:
            self._emit_bitfield_read(operand.bitfield, addr=address)
            return
        if operand.decay_to_address:
            # Bare array-typed / struct-value member decays to its address,
            # mirroring _emit_member_address: a global label folds the offset
            # into a label-immediate ``mov``; a register base with no offset is
            # a bare ``mov``; a frame / offset-bearing register base uses ``lea``.
            self._emit_member_address(
                const_base=operand.base,
                base_is_register=operand.base_kind == "register",
                is_global_label=operand.base_kind == "label",
                offset=operand.displacement,
            )
            self.ax_clear()
            return
        if operand.raw_width and operand.field_size != 1:
            self.emit(f"        mov {self.target.acc}, {address}")
            self.ax_clear()
            return
        self._emit_field_load(addr=address, field_size=operand.field_size)
        self.ax_clear()

    def _emit_store_accumulator_at_width(self, *, destination: str, width: int) -> None:
        """Store the accumulator into *destination* at *width* (byte / word / full).

        *destination* is a bracket-enclosed memory operand (``[ebp-4]`` /
        ``[esi]`` / …).  Byte width stores the low-byte accumulator alias;
        2-byte width on a 32-bit target stores the low-word alias with an
        explicit ``word`` size prefix; every other width stores the full
        accumulator.  Shared by the standalone-``DereferencePlace`` store
        (cast fast path and general path) and the ``*p++ =`` increment
        store, which all wrote this same byte/word/full triple inline.
        """
        accumulator = self.target.acc
        if width == 1:
            self.emit(f"        mov {destination}, {self.target.low_byte(accumulator)}")
        elif width == 2 and self.target.int_size > 2:
            self.emit(f"        mov word {destination}, {self.target.low_word(accumulator)}")
        else:
            self.emit(f"        mov {destination}, {accumulator}")

    def _emit_subscript_resolved_load(self, place: Place, /) -> None:
        """Resolve *place* and load its value through the protect-BX terminal.

        Shared by every indexed lvalue whose address computation may clobber
        BX as a scratch index register: struct-array members, contiguous
        multidim arrays, pointer-to-array, and multidim array members.  The
        resolver's first dynamic-index ``mov bx, ax`` does not itself guard a
        pinned BX, so the outer push / pop guard is preserved here.  An
        array-typed member (field_size != element_size) decays to its address
        via ``lea`` over the full indexed operand (the resolved member-address
        terminal drops the index register, so the lea is emitted directly).
        """
        self.ax_clear()
        protect_bx = self._bx_holds_pinned_var()
        if protect_bx:
            self.emit(f"        push {self.target.bx_register}")
        operand = self.resolve_address(place)
        addr = self._build_address(operand.base, operand.displacement, index=operand.index or "")
        if operand.decay_to_address:
            self.emit(f"        lea {self.target.acc}, {addr}")
        else:
            self._emit_field_load(addr=addr, field_size=operand.field_size)
        if protect_bx:
            self.emit(f"        pop {self.target.bx_register}")
        self.ax_clear()

    def _emit_subscript_resolved_store(self, place: Place, value: Node, /) -> None:
        """Resolve *place* and store *value* through the protect-BX terminal.

        Shared by every indexed lvalue whose address computation may clobber
        BX as a scratch index register (struct-array members, contiguous
        multidim arrays, pointer-to-array, and multidim array members).  The
        value is evaluated and stashed before the address computation (which is
        free to clobber the accumulator); the outer push / pop guard preserves
        a pinned BX across the resolver's first dynamic-index ``mov bx, ax``.
        """
        allowed = (1, 2, 4) if self.target.int_size == 4 else (1, 2)
        self.ax_clear()
        protect_bx = self._bx_holds_pinned_var()
        if protect_bx:
            self.emit(f"        push {self.target.bx_register}")
        self.generate_expression(value)  # AX = value
        self.emit(f"        push {self.target.acc}")  # save value on top of stack
        operand = self.resolve_address(place)  # may use BX/AX as scratch
        if operand.field_size not in allowed:
            message = f"writing field (size {operand.field_size}) not yet supported; use asm()"
            raise CompileError(message, line=place.line)
        self.emit(f"        pop {self.target.acc}")  # AX = value
        self.ax_clear()
        addr = self._build_address(operand.base, operand.displacement, index=operand.index or "")
        self._emit_field_store(addr=addr, field_size=operand.field_size)
        if protect_bx:
            self.emit(f"        pop {self.target.bx_register}")

    def _emit_syscall(self, name: str, /) -> None:
        """Emit the invocation sequence for a named kernel syscall.

        Looks up :attr:`SYSCALL_SEQUENCES` and emits one instruction per
        entry.  This is the only path by which cc.py-generated C code
        reaches the kernel, so retargeting the OS to a different ABI
        (e.g., protected-mode ``syscall`` / ``sysenter``) is done by
        editing that table — no per-builtin edits required.

        Raises :class:`CompileError` when ``target_mode`` is ``"kernel"``
        — syscall self-calls are user-space only; kernel code calls
        handler implementations directly.
        """
        if self.target_mode == "kernel":
            builtin_name = name.lower().replace("_", "")
            message = f"syscall builtin '{builtin_name}' not available in --target kernel; call the implementation directly"
            raise CompileError(message)
        if name not in self.target.syscall_sequences:
            message = f"unknown syscall: {name!r}"
            raise CompileError(message)
        for instruction in self.target.syscall_sequences[name]:
            self.emit(f"        {instruction}")

    def _estimate_scratch_clobbers(self, node: Node, /) -> set[str]:
        """Return registers that *node*'s evaluation may clobber as scratch.

        Distinct from :meth:`_collect_pinned_reads`, which tracks which
        *pinned* register a node *reads*.  This tracks which registers
        the lowering will *write* to internally on its way to leaving
        the result in the accumulator.

        Conservative — only models the clobbers that have actually been
        observed to corrupt sibling argument loads.  The known one is
        SI: a non-trivial ``Index`` expression with a non-constant base
        ``mov``s into SI as the addressing scratch (see
        :meth:`generate_expression`'s Index path), trashing any value
        the surrounding builtin loaded earlier into SI for a different
        argument.  ``Call`` arguments inherit their callee's documented
        ``BUILTIN_CLOBBERS`` set so a builtin like ``strlen`` (clobbers
        AX/CX/DI) blocks a sibling load into one of those registers
        from being emitted first.
        """
        clobbers: set[str] = set()
        stack: list[Node] = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, Index):
                # Index lowering uses SI as the base-address scratch
                # whenever the base isn't a compile-time constant — by
                # far the most common shape.  Be conservative and always
                # claim SI.
                clobbers.add(self.target.si_register)
            elif isinstance(current, Call):
                builtin_clobbers = self._builtin_clobbers.get(current.name)
                if builtin_clobbers is not None:
                    # BUILTIN_CLOBBERS uses 16-bit names; widen so the
                    # 32-bit scheduler comparisons line up with target
                    # register names like ``esi`` / ``ecx``.
                    clobbers.update(self.target.widen_gp(register) for register in builtin_clobbers)
                # User functions / unknown callees: assume they trample
                # everything except BP (the frame register).  Acc, BX,
                # CX, DX, SI, DI are all fair game for the caller-save
                # cdecl convention this compiler emits.
                else:
                    clobbers.update(
                        getattr(self.target, register)
                        for register in ("acc", "bx_register", "count_register", "dx_register", "si_register", "di_register")
                    )
            for slot in getattr(type(current), "__slots__", ()):
                child = getattr(current, slot, None)
                if isinstance(child, Node):
                    stack.append(child)
                elif isinstance(child, list):
                    stack.extend(item for item in child if isinstance(item, Node))
        return clobbers

    def _eval_local_array_size(self, size: Node, /, *, stride: int) -> int | None:
        """Return the byte count for a local array declaration, or ``None``.

        Only ``Int`` literals and :attr:`NAMED_CONSTANT_VALUES` entries
        can be resolved at Python time — those are the only cases where
        cc.py knows the integer value needed to size the stack frame slot.
        Any other expression returns ``None`` and the caller falls back to
        the old 2-byte-pointer behavior (raising a compile error or keeping
        the array at file scope).
        """
        if isinstance(size, Int):
            return size.value * stride
        if isinstance(size, Var) and size.name in self.NAMED_CONSTANT_VALUES:
            return self.NAMED_CONSTANT_VALUES[size.name] * stride
        return None

    def _eval_constant_dimension(self, dimension: Node, /, *, line: int) -> int:
        """Return the integer value of a compile-time-constant array dimension.

        Delegates to :meth:`_eval_local_array_size` with ``stride=1`` so that
        only :class:`Int` literals and :attr:`NAMED_CONSTANT_VALUES` entries
        resolve.  Raises :class:`CompileError` for any other expression (e.g.
        a runtime variable) since multidimensional array sizes must be known
        at compile time.
        """
        result = self._eval_local_array_size(dimension, stride=1)
        if result is None:
            message = "multidimensional array dimension must be a compile-time constant"
            raise CompileError(message, line=line)
        return result

    def _expression_type(self, node: Node, /) -> str:
        """Infer the compile-time type of *node* for ``sizeof(expression)``.

        The expression is NEVER evaluated at runtime — sizeof is a
        compile-time constant.  Walks the AST shape to produce a type
        string in the same form as :attr:`variable_types` entries.
        """
        if isinstance(node, BinaryOperation):
            return "int"
        if isinstance(node, Cast):
            return node.target_type
        if isinstance(node, Index):
            # ``p[i]`` / ``*p`` (parsed as Index(p, 0)) for a pointer-to-array
            # ``int (*p)[3]`` yields the pointee array type (``int[3]``).
            if isinstance(node.array, Var) and node.array.name in self.pointer_array_types:
                return self.pointer_array_types[node.array.name].pointee.to_string()
            if isinstance(node.array, Var) and node.array.name in self.variable_arrays and node.array.name not in self.array_types:
                # A non-multidim array variable stores its ELEMENT type
                # directly in variable_types (e.g. ``char *names[4]`` ->
                # "char *"), so indexing yields that element type as-is.
                # Stripping a level here (as the pointer-decay branch below
                # does) would mis-type ``names[i]`` as the pointee ("char")
                # rather than the element ("char *") and break a following
                # dereference.  Genuine multidim arrays (``int m[2][3]``,
                # registered in array_types) are excluded — they have
                # dedicated row-major type handling and must fall through.
                return self.variable_types[node.array.name]
            array_type = self._expression_type(node.array)
            if array_type.endswith("*"):
                return array_type[:-1].rstrip()
            if "[" in array_type:
                # Local array type like "int [10]" — strip the "[N]" suffix.
                return array_type[: array_type.index("[")].rstrip()
            message = f"sizeof: cannot dereference non-pointer type '{array_type}'"
            raise CompileError(message, line=node.line)
        if isinstance(node, Int):
            # Char subclasses Int and a C character constant has type int
            # (sizeof('a') == sizeof(int)), so both flow through here — there
            # is deliberately no separate Char branch.
            return "int"
        if isinstance(node, PlaceAddressOf):
            # ``&place`` — a pointer to the place's declared type.  Reproduces
            # the legacy address-of ("<type> *") result so sizeof(&x) is
            # unchanged (the space before ``*`` is part of the contract).
            return f"{self._place_type(node.place)} *"
        if isinstance(node, PlaceLoad):
            # Rvalue read of any Place — resolve the place's declared type.
            # Preserves the struct-array sizeof behavior: sizeof(arr[i].field)
            # resolves through MemberPlace/SubscriptPlace to the field type.
            return self._place_type(node.place)
        if isinstance(node, Place):
            # A bare Place used as an expression operand (e.g. the pointer
            # inside a DereferencePlace) — resolve its declared type.
            return self._place_type(node)
        if isinstance(node, (SizeofType, SizeofVar, SizeofExpr)):
            return "int"
        if isinstance(node, String):
            return "char *"
        if isinstance(node, Var):
            variable_type = self.variable_types.get(node.name)
            if variable_type is None:
                message = f"sizeof: unknown variable '{node.name}'"
                raise CompileError(message, line=node.line)
            return variable_type
        message = f"sizeof: cannot determine type of {type(node).__name__}"
        raise CompileError(message, line=node.line)

    def _flatten_array_init(self, init: ArrayInit, /, *, name: str, total: int, line: int) -> list[Node]:  # noqa: PLR6301
        """Flatten a (possibly nested) ArrayInit into a row-major element list.

        Walks the nested ``ArrayInit`` tree depth-first, collecting leaf
        (non-``ArrayInit``) element nodes in row-major order; raises a
        :class:`CompileError` if the initializer has more than *total*
        elements for array *name*.
        """
        flat: list[Node] = []

        def walk(node: ArrayInit) -> None:
            for element in node.elements:
                if isinstance(element, ArrayInit):
                    walk(element)
                else:
                    flat.append(element)

        walk(init)
        if len(flat) > total:
            message = f"too many initializers for '{name}'"
            raise CompileError(message, line=line)
        return flat

    def _has_remainder(self, left: Node, right: Node, /) -> bool:
        """Check if DX already holds left % right.

        Handles both direct matches and the transitive property:
        (A % N) % M == A % M when M divides N.
        """
        if self.division_remainder is None:
            return False
        remainder_left, remainder_right = self.division_remainder
        # Direct match: same operands.
        if remainder_left == left and remainder_right == right:
            return True
        # Transitive: DX = (A % N) % M, want A % M, and M divides N.
        return (
            remainder_right == right
            and isinstance(right, Int)
            and self._is_modulo_of(base=left, expression=remainder_left)
            and remainder_left.right.value % right.value == 0
        )

    def _ir_instruction_store_targets(self, instruction: object, /) -> list[str]:
        """Return every local name written by *instruction*.

        Most shapes write at most one local; ``ir.Call`` is the
        exception — beyond its (optional) ``destination``, every
        ``out_register`` arg captures into the named local AFTER the
        call returns, so all of them count as stores for the purposes
        of "is the pin live around the next call".

        Used by :meth:`_compute_pinned_initialized_per_call` to mark
        which pinned-locals become defined at each IR instruction.
        """
        if isinstance(instruction, (ir.BinaryOperation, ir.Copy, ir.Index)):
            return [instruction.destination]
        if isinstance(instruction, (ir.Block, ir.Access)):
            # Block / Access wrap an AST escape hatch.  A VarDecl with
            # initialiser is a store to its name; ditto an
            # ``unsigned long`` Assign that the IR builder routes
            # through Block.  Pinned-to-register locals can't be
            # ``unsigned long`` (they wouldn't fit a single register),
            # so only the VarDecl case can hit a pinned target —
            # but we still extract Assign destinations defensively in
            # case future IR shapes wrap them.
            node = instruction.node
            if isinstance(node, Assign):
                return [node.name]
            if isinstance(node, VarDecl) and node.init is not None:
                return [node.name]
            # PlaceStore / IndexAssign / inline asm write through
            # pointers or are opaque — they don't store to a single
            # named local register.  Skip.
            return []
        if isinstance(instruction, ir.Call):
            stores: list[str] = []
            if instruction.destination is not None:
                stores.append(instruction.destination)
            out_regs = self.out_register_params.get(instruction.name, {})
            for index, arg in enumerate(instruction.args):
                taken_name = address_of_variable_name(arg)
                if index in out_regs and taken_name is not None:
                    stores.append(taken_name)
            return stores
        if isinstance(instruction, ir.CarryBranch):
            # ``carry_return`` callees can also have ``out_register``
            # captures — match the ir.Call handling so the pin
            # tracker sees their writes too.
            call_ast = instruction.call_ast
            stores = []
            out_regs = self.out_register_params.get(call_ast.name, {})
            for index, arg in enumerate(call_ast.args):
                taken_name = address_of_variable_name(arg)
                if index in out_regs and taken_name is not None:
                    stores.append(taken_name)
            return stores
        if isinstance(instruction, ir.IndexAssign):
            # IndexAssign writes through a base pointer, not to the
            # named base itself — leaves the base's register
            # contents unchanged.  Not a store to the pin.
            return []
        if isinstance(instruction, ir.RepString):
            # A rep-string fill / copy writes through ``dest`` (and reads
            # ``source``) but those are base pointers, not stores to the
            # named local.  Only ``final_iv`` materializes a named local
            # — the induction variable's post-loop value.  The matcher
            # currently only emits ``final_iv=None`` so this contributes
            # nothing today, but recording it keeps the pinned-liveness
            # tracker correct once final_iv materialization lands.
            if instruction.final_iv is not None:
                return [instruction.final_iv[0]]
            return []
        return []

    @staticmethod
    def _is_candidate_expression_temporary(
        name: str,
        /,
        *,
        ax_resident_uses: dict[str, int],
        init_count: dict[str, int],
        init_expr: dict[str, Node],
        other_uses: dict[str, int],
    ) -> bool:
        """Skip pinning vars whose value lives in AX between assignment and consumer.

        A var assigned exactly once from a non-trivial expression
        (Call/Index/BinaryOperation — all leave the value in AX) and consumed
        only as the LEFT operand of a comparison against an integer
        literal naturally lives in AX through its lifetime.
        ``emit_comparison``'s fast path emits ``cmp ax, imm`` for
        those uses without re-loading the value, so pinning the
        var would only add a redundant ``mov pin, ax`` after the
        assignment.  Vars used as right-of-cmp or in arithmetic
        still benefit from a pin (the left operand's eval clobbers
        AX before reaching them) so they're left alone here.
        """
        if init_count.get(name, 0) != 1:
            return False
        if other_uses.get(name, 0) != 0:
            return False
        if ax_resident_uses.get(name, 0) == 0:
            return False
        return isinstance(init_expr.get(name), (Call, Index, BinaryOperation))

    def _is_member_index_place(self, place: Place, /) -> bool:
        """Return True if *place* is ``base.field[index]`` (member-index, not struct-array)."""
        return (
            isinstance(place, SubscriptPlace)
            and isinstance(place.base, MemberPlace)
            and self._match_struct_array_member(place.base) is None
        )

    def _is_multidim_array(self, name: str, /) -> bool:
        """Return True if *name* is a registered contiguous multidimensional array.

        A name appears in :attr:`array_types` only for genuine multidim
        declarations (``int m[2][3]``), whose registered type is a nested
        :class:`ArrayType`.  Single-dimension arrays and arrays of pointers
        are not registered here, so this is the type-driven discriminator
        that routes ``name[i][j]`` to the row-major path versus the legacy
        array-of-pointers deref path.
        """
        return isinstance(self.array_types.get(name), ArrayType)

    def _load_member_base(self, object_name: str, /) -> str:
        """Return a base register holding the struct address for ``object_name``.

        When the auto-pin live-range for ``object_name`` is intact and
        SI/ESI still holds the variable's value, return SI directly —
        every ``generate_member_*`` lowerer can read fields off it
        without a fresh load.  Otherwise emit a load into BX/EBX and
        return that.  Used by the prologue of every member-access /
        member-assign / member-index lowerer that needs the struct
        base in a register.
        """
        if self.si_local == object_name:
            return self.target.si_register
        self._emit_load_var(object_name, register=self.target.bx_register)
        return self.target.bx_register

    def _local_address(self, name: str, /) -> str:
        """Return the memory operand string for a local or global scalar.

        Local variables shadow globals with the same name (standard C),
        so the local-frame path runs first and only falls through to
        ``_g_<name>`` when no local slot exists.  Register-aliased
        globals have no memory address — they live in a CPU register —
        so this path raises if called on one (caller should have
        routed through ``register_aliased_globals`` instead).
        """
        if name in self.locals:
            if self.elide_frame:
                return f"_l_{name}"
            offset = self.locals[name]
            if offset > 0:
                return f"{self.target.base_register}-{offset}"
            return f"{self.target.base_register}+{-offset}"
        if name in self.register_aliased_globals:
            message = f"register-aliased global '{name}' has no memory address"
            raise CompileError(message)
        if name in self.asm_symbol_globals:
            return self.asm_symbol_globals[name]
        if name in self.global_scalars:
            return self._global_label(name)
        message = f"no address for '{name}' (not a local or global scalar)"
        raise CompileError(message)

    def _lookup_struct_field(self, tag: str, member_name: str, line: int, /) -> FieldInfo:
        """Return the :class:`FieldInfo` for ``member_name`` of struct ``tag``.

        Raises :class:`CompileError` for an unknown struct tag or field,
        with the same message strings the legacy ``generate_member_*``
        lowerers emit.
        """
        layout = self.struct_layouts.get(tag)
        if layout is None:
            message = f"unknown struct '{tag}'"
            raise CompileError(message, line=line)
        if member_name not in layout:
            message = f"struct '{tag}' has no field '{member_name}'"
            raise CompileError(message, line=line)
        return layout[member_name]

    @staticmethod
    def _loop_assigned_names(statements: list[Node], /) -> set[str]:
        """Return the set of names assigned anywhere within *statements*.

        Pre-merge stores from loop bodies into the written set before
        walking the body, mirroring the liveness pre-pass's loop-pre-
        merge (a store inside a loop is live on every iteration
        including the first, so calls BEFORE that store inside the body
        still see a live pin).
        """
        found: set[str] = set()
        for statement in statements:
            if isinstance(statement, Assign) or (isinstance(statement, VarDecl) and statement.init is not None):
                found.add(statement.name)
            elif isinstance(statement, If):
                found |= X86CodeGenerator._loop_assigned_names(statement.body)
                if statement.else_body is not None:
                    found |= X86CodeGenerator._loop_assigned_names(statement.else_body)
            elif isinstance(statement, (Compound, DoWhile, While)):
                found |= X86CodeGenerator._loop_assigned_names(statement.body)
            elif isinstance(statement, Switch):
                for case in statement.cases:
                    found |= X86CodeGenerator._loop_assigned_names(case.body)
        return found

    @staticmethod
    def _match_struct_array_member(place: Place, /) -> tuple[str, Node, str] | None:
        """Recognize the one struct-array access shape the Place codegen handles.

        If *place* is ``arr[i].member`` — a :class:`MemberPlace` whose base is a
        :class:`SubscriptPlace` of a :class:`VariablePlace` — return
        ``(array_name, index, member_name)``; otherwise ``None``.  The
        ``arr[i].member[j]`` element shape is this same match applied to the
        outer subscript's base.
        """
        if isinstance(place, MemberPlace) and isinstance(place.base, SubscriptPlace) and isinstance(place.base.base, VariablePlace):
            return place.base.base.name, place.base.index, place.member_name
        return None

    def _match_multidim_member_chain(self, place: Place, /) -> tuple[str, bool, str, list[Node]] | None:
        """Recognize ``g.field[i][j]...`` / ``p->field[i][j]...`` over a multidim field.

        Returns ``(object_name, arrow, field_name, [index nodes outer→inner])``
        when *place* is a uniform left-nested :class:`SubscriptPlace` chain
        whose innermost base is a :class:`MemberPlace` over a
        :class:`VariablePlace` (dot) or :class:`DereferencePlace` of a
        :class:`VariablePlace` (arrow), AND the named field's
        :class:`FieldInfo` type carries two or more ``[`` (a genuine
        multidimensional array field).  Returns ``None`` for any other
        shape — single-subscript member access, non-multidim fields, and
        deref/var-rooted chains all fall through to their existing
        dispatch arms unchanged.
        """
        indices: list[Node] = []
        current: Place = place
        while isinstance(current, SubscriptPlace):
            indices.append(current.index)
            current = current.base
        if not isinstance(current, MemberPlace):
            return None
        resolved = self._member_index_arrow_object(current)
        if resolved is None:
            return None
        arrow, object_name = resolved
        struct_type = self.variable_types.get(object_name)
        if struct_type is None:
            return None
        if arrow:
            if not struct_type.startswith("struct ") or not struct_type.endswith("*"):
                return None
            tag = struct_type[len("struct ") : -1].rstrip()
        else:
            if not struct_type.startswith("struct ") or struct_type.endswith("*"):
                return None
            tag = struct_type[len("struct ") :]
        layout = self.struct_layouts.get(tag)
        if layout is None:
            return None
        info = layout.get(current.member_name)
        if info is None or info.type_name.count("[") < 2:
            return None
        indices.reverse()
        return object_name, arrow, current.member_name, indices

    def _maybe_emit_data_header(self) -> None:
        """Emit the ``section .data`` (or flat-mode comment) header at most once per call to :meth:`_emit_global_storage`."""
        if self._data_header_emitted:
            return
        if self.object_mode:
            self.emit()
            self.emit("section .data")
        self.emit(";; --- global data ---")
        self._data_header_emitted = True

    def _member_base_is_static(self, place: MemberPlace, /) -> bool:
        """Return True if *place*'s base resolves without materializing a register.

        Two shapes resolve to a static address (and so do not clobber the
        accumulator): a dot access on a named struct value (``obj.field``) and
        the ``((struct T *)&local)->field`` cast fast path.  For these the store
        terminal evaluates the rhs into the accumulator first and writes it
        directly; every other base (arrow, chained, general via-expr) is a
        register base that must have the rhs spilled across the base load.
        """
        base = place.base
        if isinstance(base, VariablePlace):
            return True
        if isinstance(base, DereferencePlace) and isinstance(base.pointer, Cast):
            cast_address_name = address_of_variable_name(base.pointer.expression)
            return cast_address_name is not None and cast_address_name in self.locals
        return False

    def _member_base_kind(self, place: MemberPlace, /) -> str:
        """Return the MemoryOperand base_kind for a non-register member base.

        Only reached for the two shapes ``_resolve_member_place_info`` resolves
        without materializing a register: dot access on a named struct value
        (``obj.field`` — "label" for a file-scope global, "frame" for a local)
        and the ``((struct T *)&local)->field`` cast fast path ("frame").  The
        load terminal keys off this to choose the address form for a bare
        array / struct-value member (label-immediate vs ``lea``).
        """
        if self._member_dot_targets_global(place):
            return "label"
        return "frame"

    def _member_base_preserves_accumulator(self, place: MemberPlace, /) -> bool:
        """Return True if *place*'s base materializes without reading the accumulator.

        Covers the static bases (:meth:`_member_base_is_static`) and the arrow
        named-pointer ``ptr->field`` whose base load is a bare ``mov bx, [ptr]``
        (or a reuse of SI).  For these the store terminal evaluates the rhs into
        the accumulator first and then materializes the base into BX without
        disturbing it; every other register base (chained, general via-expr)
        evaluates a pointer expression through the accumulator and needs a spill.
        """
        if self._member_base_is_static(place):
            return True
        base = place.base
        return isinstance(base, DereferencePlace) and isinstance(base.pointer, VariablePlace)

    def _member_bitfield_literal(self, place: MemberPlace, value: Node, /) -> int | None:
        """Return the 0/1 literal for a 1-bit bitfield member store, else None.

        A ``field : 1`` bitfield assigned an ``Int`` literal 0 or 1 writes a
        single ``and``/``or`` (or const-folded ``mov``) byte and needs no rhs in
        a register; the store terminal uses this to skip the rhs spill on a
        register-base member.
        """
        info = self._member_field_info(place)
        if info.bit_width == 1 and isinstance(value, Int) and value.value in (0, 1):
            return value.value
        return None

    def _member_dot_targets_global(self, place: MemberPlace, /) -> bool:
        """Return True if *place* is ``global.field`` on a file-scope struct global.

        Only the dot access of a named global struct loads the field address
        as a label-arithmetic immediate; locals (and the cast / chained
        register bases) use ``lea`` / ``mov reg`` instead.
        """
        return isinstance(place.base, VariablePlace) and place.base.name in self.global_scalars

    def _member_field_info(self, place: MemberPlace, /) -> FieldInfo:
        """Return the :class:`FieldInfo` for a non-array member without emitting code.

        Mirrors the struct-tag resolution of :meth:`_resolve_member_place_info`
        (dot / arrow / cast / chained) but performs no base materialization, so
        a caller can branch on a field's bitfield-ness (e.g. the 1-bit-literal
        store fast path) before deciding the rhs-vs-base evaluation order.
        """
        base = place.base
        member_name = place.member_name
        line = place.line
        if isinstance(base, VariablePlace):
            struct_type = self.variable_types.get(base.name)
            if struct_type is None:
                message = f"undefined variable '{base.name}'"
                raise CompileError(message, line=line)
            if struct_type.endswith("*") or not struct_type.startswith("struct "):
                message = f"'.' requires a struct value, got type '{struct_type}'"
                raise CompileError(message, line=line)
            return self._lookup_struct_field(struct_type[7:], member_name, line)
        if isinstance(base, DereferencePlace) and isinstance(base.pointer, VariablePlace):
            return self._resolve_member_index_layout(
                arrow=True,
                line=line,
                member_name=member_name,
                object_name=base.pointer.name,
            )
        if isinstance(base, DereferencePlace):
            base_type = self._expression_type(base.pointer)
            if not base_type.startswith("struct ") or not base_type.endswith("*"):
                message = f"'->' requires a pointer to struct, got type '{base_type}'"
                raise CompileError(message, line=line)
            return self._lookup_struct_field(base_type[7:-1].rstrip(), member_name, line)
        if isinstance(base, MemberPlace):
            base_type = self._place_type(base)
            if not base_type.startswith("struct ") or base_type.endswith("*"):
                message = f"'.' requires a struct value, got type '{base_type}'"
                raise CompileError(message, line=line)
            return self._lookup_struct_field(base_type[7:], member_name, line)
        message = "unsupported member Place base in _member_field_info"
        raise CompileError(message, line=line)

    @staticmethod
    def _member_index_arrow_object(member: MemberPlace, /) -> tuple[bool, str] | None:
        """Return ``(arrow, object_name)`` for a named member-index base.

        ``ptr->field[i]`` has base ``MemberPlace(DereferencePlace(Var), field)``
        → ``(True, ptr)``; ``obj.field[i]`` has base
        ``MemberPlace(Var, field)`` → ``(False, obj)``.  Returns ``None`` for
        any other base shape (handled elsewhere).
        """
        base = member.base
        if isinstance(base, DereferencePlace) and isinstance(base.pointer, VariablePlace):
            return True, base.pointer.name
        if isinstance(base, VariablePlace):
            return False, base.name
        return None

    def _member_index_element_size(self, info: FieldInfo, /) -> tuple[int, bool]:
        """Return ``(element_size, is_pointer_field)`` for an indexed field access.

        For inline-array fields (e.g. ``char buf[16]``) returns the
        declared element size and ``False``.  For pointer fields (e.g.
        ``char *buf``) returns the pointee size and ``True``: the
        codegen path must load the field's pointer value rather than
        compute an offset into the struct.
        """
        if "[" in info.type_name:
            return info.element_size, False
        if "*" in info.type_name:
            # Pointer field: strip one trailing star to get the pointee type.
            pointee = info.type_name.rstrip()
            assert pointee.endswith("*"), pointee
            pointee = pointee[:-1].rstrip()
            return self._type_size(pointee), True
        # Scalar struct field — not indexable.
        return info.element_size, False

    def _member_layout_on(self, base: Place, member_name: str, /, *, line: int) -> tuple[int, int, int]:
        """Return ``(field_offset, field_size, element_size)`` for *member_name*.

        Dispatches on the struct type that *base* denotes.  For a struct-array
        base (SubscriptPlace over a VariablePlace, the shape handled by
        _match_struct_array_member) the layout comes from
        _resolve_index_member_layout.  For a struct-value base (VariablePlace
        naming a local or global struct value) it is resolved by looking up the
        struct tag from variable_types and consulting the struct layout table
        (the same path _resolve_member_place_info takes for the dot-access case).
        element_size equals field_size for scalar fields, or the per-element byte
        count for array-typed members.
        """
        # Struct-array base: arr[i].member — layout from _resolve_index_member_layout.
        if (matched := self._match_struct_array_member(MemberPlace(base=base, member_name=member_name, line=line))) is not None:
            array_name, _index_node, matched_member_name = matched
            _const_base, _struct_size, field_offset, field_size, element_size = self._resolve_index_member_layout(
                array_name, matched_member_name, line
            )
            return field_offset, field_size, element_size
        # Struct-value base: obj.member — resolve tag from variable_types, then look up layout.
        if isinstance(base, VariablePlace):
            struct_type = self.variable_types.get(base.name)
            if struct_type is None:
                message = f"undefined variable '{base.name}'"
                raise CompileError(message, line=line)
            if struct_type.endswith("*") or not struct_type.startswith("struct "):
                message = f"'.' requires a struct value, got type '{struct_type}'"
                raise CompileError(message, line=line)
            tag = struct_type[7:]
            info = self._lookup_struct_field(tag, member_name, line)
            return info.byte_offset, info.field_size, info.element_size
        message = f"unsupported base shape for member '{member_name}' in _member_layout_on"
        raise CompileError(message, line=line)

    @staticmethod
    def _parse_local_byte_addr(addr: str) -> int | None:
        """Return the frame slot K if addr is ``[ebp-N]`` or ``[ebp-N+M]``; otherwise None.

        K is the absolute frame offset of the targeted byte: K = N - M
        for the +M form, K = N otherwise.
        """
        match = RE_LOCAL_BYTE_ADDR.match(addr.strip())
        if match is None:
            return None
        base = int(match.group(1))
        offset = int(match.group(2) or 0)
        return base - offset

    def _peephole_will_strand_ax(self) -> bool:
        """Return True if the last emitted lines form a fusion target.

        :meth:`peephole_memory_arithmetic` collapses
        ``mov ax, D / <operation> ax, ... / mov D, ax`` into ``<operation> D, ...`` when
        source and destination match (passes 2 and 3); :meth:`peephole_register_arithmetic`
        pushes the computation directly into a pin-eligible destination
        register when it differs from the source.
        :meth:`peephole_memory_arithmetic_byte` collapses the 4-line
        byte-scalar-global shape (``mov al, [mem] / xor ah, ah / <operation>
        ax, ... / mov [mem], al``) into ``<operation> byte [mem], ...``.
        :meth:`peephole_dx_to_memory` collapses ``mov ax, dx / mov [mem],
        ax`` (emitted after a ``%`` operation stages the remainder from
        DX through AX so the standard store path can flush it) into
        ``mov [mem], dx`` — and AX then still holds the quotient from
        the preceding ``div`` rather than the remainder that actually
        reached memory.  All four leave AX holding something other
        than the new stored value, so the ``ax_local`` tracking the
        caller just set (pointing at the store's destination local)
        would mislead later reads into skipping a reload and picking
        up stale contents.

        The caller — :meth:`emit_store_local` — consults this after the
        final ``mov <D>, ax`` (or ``mov [_g_X], al`` for byte globals)
        has been emitted; if we report True it clears its own
        tracking instead of guessing at peephole time.
        """
        acc = self.target.acc
        # Byte-global fusion: last 4 lines are ``mov al, [mem] / xor
        # ah, ah / <operation> ax, ... / mov [mem], al`` and the two mem refs
        # match — peephole_memory_arithmetic_byte will delete all four.
        if len(self.lines) >= 4:
            first = self.lines[-4].strip()
            second = self.lines[-3].strip()
            third = self.lines[-2].strip()
            last = self.lines[-1].strip()
            if (
                first.startswith("mov al, [")
                and first.endswith("]")
                and second == "xor ah, ah"
                and last.startswith("mov [")
                and last.endswith(", al")
            ):
                source = first[len("mov al, ") :]
                destination = last[len("mov ") : -len(", al")].strip()
                if source == destination:
                    if third in (f"inc {acc}", f"dec {acc}"):
                        return True
                    if third.startswith((f"add {acc}, ", f"sub {acc}, ", f"and {acc}, ", f"or {acc}, ", f"xor {acc}, ")):
                        return True
        # peephole_dx_to_memory: ``mov ax, dx / mov [mem], ax`` folds
        # to ``mov [mem], dx`` and leaves AX holding the pre-``mov
        # ax, dx`` value (the quotient, when the pair was emitted by
        # a ``%`` expression).
        if len(self.lines) >= 2:
            penultimate = self.lines[-2].strip()
            last = self.lines[-1].strip()
            if penultimate == f"mov {acc}, {self.target.dx_register}" and last.startswith("mov [") and last.endswith(f", {acc}"):
                return True
        if len(self.lines) < 3:
            return False
        first = self.lines[-3].strip()
        middle = self.lines[-2].strip()
        last = self.lines[-1].strip()
        mov_acc_prefix = f"mov {acc}, "
        if not (first.startswith(mov_acc_prefix) and last.startswith("mov ") and last.endswith(f", {acc}")):
            return False
        source = first[len(mov_acc_prefix) :]
        destination = last[len("mov ") : -len(f", {acc}")].strip()
        if source == destination:
            # Passes 2 and 3 of peephole_memory_arithmetic cover inc/dec
            # and (add|sub|and) with any operand shape (imm, register,
            # or ``[mem]``).
            if middle in (f"inc {acc}", f"dec {acc}"):
                return True
            return middle.startswith((f"add {acc}, ", f"sub {acc}, ", f"and {acc}, ", f"or {acc}, ", f"xor {acc}, "))
        # peephole_register_arithmetic: different register destination,
        # operation in {add, sub, and, or, xor}, operand doesn't reference the target.
        if destination in self.target.non_acc_registers:
            for prefix in (f"add {acc}, ", f"sub {acc}, ", f"and {acc}, ", f"or {acc}, ", f"xor {acc}, "):
                if middle.startswith(prefix):
                    operand = middle[len(prefix) :]
                    return destination not in operand.split()
        return False

    def _pinned_registers_to_save(self, clobbers: frozenset[str], /) -> list[str]:
        """Return the pinned registers that need push/pop around a call.

        Order is deterministic (sorted) so ``push`` / ``pop`` pairs
        nest correctly.  ``ax`` is never pinned, so never saved here.

        ``BUILTIN_CLOBBERS`` uses canonical 16-bit names (``cx``,
        ``bx``, etc.).  Caller-side clobber sets (the
        ``register_pool`` passed for user-function calls) name
        E-registers in protected mode and 16-bit aliases in real mode.
        Normalise both sides through ``target.low_word`` so the
        comparison still matches when the two halves disagree.

        When :attr:`_current_call_pinned_initialized` is set (by the
        IR lowering pass via :meth:`_compute_pinned_initialized_per_call`),
        registers whose pinned local has not yet been written are
        filtered out — their value is undefined garbage and saving it
        is dead.
        """
        low_word = self.target.low_word
        normalised_clobbers = frozenset(low_word(register) for register in clobbers)
        initialized_filter = self._current_call_pinned_initialized
        # Dedup via ``set``: liveness-driven sharing maps several names
        # to the same register, and emitting push/pop pairs once per
        # name would unbalance the stack.
        return sorted({
            register
            for register in self.pinned_register.values()
            if low_word(register) in normalised_clobbers
            and low_word(register) != "ax"
            and (initialized_filter is None or register in initialized_filter)
        })

    def _place_type(self, place: Place, /) -> str:
        """Infer the declared type string of *place* (compile-time, never evaluated).

        Recursive companion to :meth:`_expression_type` for the ``Place``
        lvalue tree.  ``VariablePlace`` reads :attr:`variable_types`;
        ``SubscriptPlace`` strips one pointer / array level off its base;
        ``DereferencePlace`` strips the pointer of its pointee expression;
        ``MemberPlace`` resolves the field's declared type from the struct
        layout.  Used for ``sizeof`` and to recover the struct tag when a
        chained member access dots through a struct-value member.
        """
        if isinstance(place, VariablePlace):
            variable_type = self.variable_types.get(place.name)
            if variable_type is None:
                message = f"sizeof: unknown variable '{place.name}'"
                raise CompileError(message, line=place.line)
            return variable_type
        if isinstance(place, SubscriptPlace):
            # ``p[i]`` for a pointer-to-array ``int (*p)[3]`` yields the pointee
            # array type (``int[3]``), not the stripped pointer element.
            if isinstance(place.base, VariablePlace) and place.base.name in self.pointer_array_types:
                return self.pointer_array_types[place.base.name].pointee.to_string()
            base_type = self._place_type(place.base)
            if base_type.endswith("*"):
                return base_type[:-1].rstrip()
            if "[" in base_type:
                return base_type[: base_type.index("[")].rstrip()
            if base_type.startswith("struct "):
                # An array of struct values stores its element type bare
                # (``struct point``); ``arr[i]`` yields that struct value.
                return base_type
            message = f"sizeof: cannot index non-pointer type '{base_type}'"
            raise CompileError(message, line=place.line)
        if isinstance(place, DereferencePlace):
            # ``*p`` for a pointer-to-array ``int (*p)[3]`` yields the pointee
            # array type (``int[3]``).
            if isinstance(place.pointer, VariablePlace) and place.pointer.name in self.pointer_array_types:
                return self.pointer_array_types[place.pointer.name].pointee.to_string()
            pointer_type = self._expression_type(place.pointer)
            if not pointer_type.endswith("*"):
                message = f"sizeof: cannot dereference non-pointer type '{pointer_type}'"
                raise CompileError(message, line=place.line)
            return pointer_type[:-1].rstrip()
        if isinstance(place, MemberPlace):
            base_type = self._place_type(place.base)
            if base_type.startswith("struct ") and base_type.endswith("*"):
                tag = base_type[7:-1].rstrip()
            elif base_type.startswith("struct "):
                tag = base_type[7:]
            else:
                message = f"sizeof: member access on non-struct type '{base_type}'"
                raise CompileError(message, line=place.line)
            info = self._lookup_struct_field(tag, place.member_name, place.line)
            return info.type_name
        message = f"sizeof: cannot determine type of {type(place).__name__}"
        raise CompileError(message, line=place.line)

    def _prologue_initialized_pinned_registers(self) -> set[str]:
        """Return the set of pinned registers whose value is meaningful at function entry.

        Parameters that are pinned (via ``in_register`` attribute,
        auto-pin, or fastcall) are loaded into their pin by the
        function prologue, so the register holds a meaningful caller-
        supplied value from the first instruction onward.  Auto-pinned
        LOCALS (not parameters) are uninitialized until the first
        store and are excluded.

        Locals with explicit ``__attribute__((pinned_register(R)))``
        live entirely in the register (no stack slot) — their first
        write IS the initialisation, so they're treated the same as
        auto-pinned locals here.
        """
        initialized: set[str] = set()
        for name, register in self.pinned_register.items():
            if name in self.param_in_register or name in self.in_register_params:
                initialized.add(register)
        # Catch all parameters that landed in self.pinned_register —
        # the prologue loads them either from caller-pushed slots
        # ([bp+N]) or from the register-convention fastcall slots
        # (acc/dx/cx).  Any name from the function's parameter list
        # counts; locals do not.
        for name in getattr(self, "_current_function_parameter_names", ()):
            if name in self.pinned_register:
                initialized.add(self.pinned_register[name])
        return initialized

    @staticmethod
    def _rank_candidates(items: list[tuple[str, int]], /, *, counts: dict[str, int]) -> list[tuple[str, int]]:
        """Sort *items* by descending ref count then ascending declaration order.

        The auto-pin allocator ranks each candidate class (body locals
        first, parameters second) by ``counts`` so the top entry gets
        the cheapest register; ties break by declaration order so the
        result is deterministic across runs.
        """
        return sorted(items, key=lambda item: (-counts.get(item[0], 0), item[1]))

    def _register_array_type(self, name: str, /, *, dimensions: list | None, line: int, type_name: str) -> None:
        """Record the structured ArrayType for array variable *name* (row-major).

        Builds a nested :class:`ArrayType` chain from *dimensions* (outer-to-inner
        bracket size expressions) wrapping the element type parsed from
        *type_name*.  Each dimension expression must be a compile-time constant;
        non-constant expressions raise :class:`CompileError`.

        Only called for multidimensional declarations (``dimensions is not None``).
        Single-dimension arrays continue using the legacy ``size``-field path and
        are not registered here (YAGNI — Task 3 will extend if needed).
        """
        element = Type.from_string(type_name)
        sizes = [self._eval_constant_dimension(dimension, line=line) for dimension in (dimensions or [])]
        array_type: Type = element
        for count in reversed(sizes):
            array_type = ArrayType(count=count, pointee=array_type)
        self.array_types[name] = array_type

    def _register_globals(self, declarations: list[Node], /) -> None:
        """Record file-scope declarations and validate their shapes.

        Scalars are stashed in :attr:`global_scalars`; arrays in
        :attr:`global_arrays`.  Byte-element arrays (``char`` or
        ``unsigned char``) are additionally tracked in
        :attr:`global_byte_arrays` so :meth:`_is_byte_var` reports
        byte-wide element access (``int`` arrays keep word access).
        """
        for declaration in declarations:
            if isinstance(declaration, InlineAsm):
                continue
            if isinstance(declaration, EnumDecl):
                # Register every variant as a named integer constant so
                # any expression that references the bare variant name
                # resolves to the literal value (the same path
                # ``#define``'d names take after preprocessing).  The
                # declared variant list is retained for the switch
                # exhaustiveness check; storage for enum-typed locals
                # uses the standard int slot.
                self.enum_decls[declaration.name] = declaration
                for variant_name, variant_value in declaration.variants:
                    if variant_name in self.NAMED_CONSTANT_VALUES:
                        message = f"enum constant '{variant_name}' shadows a kernel constant"
                        raise CompileError(message, line=declaration.line)
                    self.enum_constants[variant_name] = variant_value
                    self.NAMED_CONSTANT_VALUES[variant_name] = variant_value
                continue
            if isinstance(declaration, StructDecl):
                self._register_struct_layout(declaration)
                continue
            name = declaration.name
            if name in self.NAMED_CONSTANTS:
                message = f"global '{name}' shadows a kernel constant"
                raise CompileError(message, line=declaration.line)
            if name in self.user_functions or name == "main":
                message = f"global '{name}' collides with a function name"
                raise CompileError(message, line=declaration.line)
            if name in self.global_scalars or name in self.global_arrays:
                # An ``extern`` forward declaration followed by the real
                # definition is standard C — allow the definition to
                # supersede the earlier extern.  Duplicate non-extern
                # declarations remain an error.
                prior_is_extern = name in self.extern_globals
                current_is_extern = getattr(declaration, "is_extern", False)
                if not prior_is_extern or current_is_extern:
                    message = f"duplicate global declaration: {name}"
                    raise CompileError(message, line=declaration.line)
                self.extern_globals.discard(name)
                self.global_scalars.pop(name, None)
                self.global_arrays.pop(name, None)
            if isinstance(declaration, VarDecl):
                if declaration.pointer_array_dimensions is not None:
                    # ``int (*p)[3];`` at file scope — a pointer-sized global
                    # cell.  Register the structured type, then fall through
                    # to the scalar-global path with a flat pointer type so
                    # storage / load width is a plain pointer.
                    self._register_pointer_to_array(
                        declaration.name,
                        element_type_name=declaration.type_name,
                        line=declaration.line,
                        pointee_dimensions=declaration.pointer_array_dimensions,
                    )
                    if declaration.is_extern:
                        self.extern_globals.add(name)
                    self.global_scalars[name] = declaration
                    continue
                if declaration.type_name == "unsigned long":
                    message = "unsigned long globals are not supported"
                    raise CompileError(message, line=declaration.line)
                if declaration.type_name == "void":
                    message = f"global '{name}' cannot have type void"
                    raise CompileError(message, line=declaration.line)
                is_struct_value = declaration.type_name.startswith("struct ") and not declaration.type_name.endswith("*")
                if declaration.init is not None and isinstance(declaration.init, StructInitializer):
                    if not is_struct_value:
                        message = f"global '{name}' has a brace initializer but is not a struct"
                        raise CompileError(message, line=declaration.line)
                    self._validate_struct_global_initializer(declaration, name=name)
                elif declaration.init is not None and self._constant_expression(declaration.init) is None:
                    message = f"global '{name}' initializer must be a constant expression"
                    raise CompileError(message, line=declaration.line)
                if declaration.init is not None and not isinstance(declaration.init, StructInitializer):
                    for constant in self._collect_constant_references(declaration.init):
                        self.emit_constant_reference(constant)
                if declaration.asm_register is not None:
                    if declaration.init is not None:
                        message = f"register-aliased global '{name}' cannot have a constant initializer (initialize from main() instead)"
                        raise CompileError(message, line=declaration.line)
                    # Widen the user's 16-bit alias ("si") to the target
                    # width ("esi" in 32-bit protected mode) so every downstream
                    # read emits the right-width register without a
                    # per-use lookup.
                    self.register_aliased_globals[name] = self.target.widen_gp(declaration.asm_register)
                if declaration.asm_symbol is not None:
                    self.asm_symbol_globals[name] = declaration.asm_symbol
                if declaration.is_extern:
                    self.extern_globals.add(name)
                # Track the type so member-access codegen can resolve
                # ``vfs_found.field`` on struct globals (only globals that
                # are actually struct values participate, since
                # variable_types is otherwise scoped to function locals).
                if declaration.type_name.startswith("struct ") and not declaration.type_name.endswith("*"):
                    self.variable_types[name] = declaration.type_name
                # File-scope function_pointer globals (e.g. vfs.asm's
                # vfs_find_fn) need the variable type recorded here so
                # downstream codegen knows the symbol is callable; the
                # per-param in_register map is re-published into
                # ``function_pointer_in_registers`` from
                # ``generate_function`` since that dict is per-function
                # state.
                if declaration.type_name == "function_pointer":
                    self.variable_types[name] = "function_pointer"
                self.global_scalars[name] = declaration
            elif isinstance(declaration, ArrayDecl):
                if declaration.dimensions is not None:
                    self._register_array_type(
                        declaration.name,
                        dimensions=declaration.dimensions,
                        line=declaration.line,
                        type_name=declaration.type_name,
                    )
                    if declaration.is_extern:
                        self.extern_globals.add(name)
                    self.global_arrays[name] = declaration
                    continue
                if (
                    declaration.type_name not in self.GLOBAL_ARRAY_PRIMITIVE_TYPES
                    and not declaration.type_name.startswith("struct ")
                    and not declaration.type_name.endswith("*")
                ):
                    allowed = ", ".join(f"'{name}'" for name in sorted(self.GLOBAL_ARRAY_PRIMITIVE_TYPES))
                    message = f"global array '{name}' must have element type {allowed}, a pointer, or a struct type"
                    raise CompileError(message, line=declaration.line)
                if declaration.type_name in self.BYTE_TYPES:
                    self.global_byte_arrays.add(name)
                if declaration.size is not None:
                    if self._constant_expression(declaration.size) is None:
                        message = f"global array '{name}' size must be a constant expression"
                        raise CompileError(message, line=declaration.line)
                    for constant in self._collect_constant_references(declaration.size):
                        self.emit_constant_reference(constant)
                if declaration.init is not None:
                    self._validate_array_init(declaration.init.elements)
                if declaration.is_extern:
                    self.extern_globals.add(name)
                self.global_arrays[name] = declaration
            else:
                message = f"unexpected top-level declaration: {type(declaration).__name__}"
                raise CompileError(message, line=declaration.line)

    def _register_inline_body(self, function: Function, /) -> None:
        """Record an ``always_inline`` function's asm body for splicing.

        The function must have a single ``asm("...")`` statement as its
        entire body.  The raw string (unescaped) is stored; each call
        site pastes it in place of ``call <name>``.  Stack parameters
        are already blocked at parse time (``always_inline`` requires
        ≤3 plain params, all register-passed), so callers never need
        a ``add sp, N`` cleanup that would fall between the inlined
        body and the following code.
        """
        body = function.body
        if len(body) != 1 or not isinstance(body[0], Call) or body[0].name != "asm":
            message = f"always_inline function '{function.name}' must have a single asm() body"
            raise CompileError(message, line=function.line)
        asm_arg = body[0].args[0]
        if not isinstance(asm_arg, String):
            message = f"always_inline function '{function.name}' asm() body must be a string literal"
            raise CompileError(message, line=function.line)
        self.inline_bodies[function.name] = asm_arg.content

    def _register_pointer_to_array(self, name: str, /, *, element_type_name: str, line: int, pointee_dimensions: list) -> None:
        """Register *name* as a pointer-to-array ``T (*name)[d1]...[dn]``.

        Builds ``PointerType(ArrayType(d1, ... ArrayType(dn, T)))`` from the
        pointee bracket dimensions (outer-to-inner) and the element type
        string, stores it in :attr:`pointer_array_types`, and sets
        :attr:`variable_types` to a flat pointer string so legacy pointer-ness
        (pointer width, deref) still holds.  Each dimension must be a
        compile-time constant.

        This is the shared lowering for both an explicit pointer-to-array
        declaration (``int (*p)[3]``) and a decayed multidim array parameter
        (``int m[][3]`` == ``int (*m)[3]``, pointee dims ``[3]``).
        """
        element: Type = Type.from_string(element_type_name)
        sizes = [self._eval_constant_dimension(dimension, line=line) for dimension in pointee_dimensions]
        pointee: Type = element
        for count in reversed(sizes):
            pointee = ArrayType(count=count, pointee=pointee)
        self.pointer_array_types[name] = PointerType(pointee=pointee)
        self.variable_types[name] = f"{element_type_name}*"

    def _register_struct_layout(self, declaration: StructDecl, /) -> None:
        """Compute and record a struct's packed field layout and total byte size.

        Extracted from :meth:`_register_globals` for the StructDecl
        branch.  Builds a ``{field_name: FieldInfo}`` map stored in
        :attr:`struct_layouts` and the total byte count in
        :attr:`struct_sizes`.

        Regular fields (``bit_width is None``) take their ``field_size``
        from ``_type_size``; array fields get
        ``field_size = element_size * count`` and
        ``element_size = per-element width``.

        Bitfields (``bit_width`` 1..8 from the parser) pack consecutive
        bits LSB-first into a single byte run; ``bit_offset`` tracks
        the next free bit within the current run.  Anonymous bitfields
        (``field_name is None``) advance ``run_bits`` but don't enter
        the layout map.  A regular field after a bitfield run closes
        the run (advances cursor by 1) before its own ``byte_offset``
        is computed.
        """
        layout: dict[str, FieldInfo] = {}
        cursor = 0
        run_bits = 0  # bits already consumed in the current bitfield run
        for declaration_field in declaration.fields:
            if declaration_field.bit_width is not None:
                if run_bits + declaration_field.bit_width > 8:
                    message = f"bitfield run exceeds 8 bits in struct '{declaration.name}' at line {declaration_field.line}"
                    raise CompileError(message, line=declaration_field.line)
                if declaration_field.field_name is not None:
                    layout[declaration_field.field_name] = FieldInfo(
                        bit_offset=run_bits,
                        bit_width=declaration_field.bit_width,
                        byte_offset=cursor,
                        element_size=1,
                        field_size=1,
                        type_name="unsigned char",
                    )
                run_bits += declaration_field.bit_width
                continue
            # Regular field: close any open bitfield run first.
            if run_bits > 0:
                cursor += 1
                run_bits = 0
            ftype = declaration_field.type_name
            if ftype.count("[") >= 2:
                # Multidimensional array field, e.g. "int[2][3]".  Parse the
                # structured type so the total byte size (24) and innermost
                # element size (int → 4) come from the same row-major model the
                # addressing path uses; the element type is the innermost
                # pointee after peeling every ArrayType level.
                array_type = Type.from_string(ftype)
                assert isinstance(array_type, ArrayType)
                field_size = array_type.sizeof(pointer_width=self.target.int_size, scalar_width=self._type_size)
                element_type: Type = array_type
                while isinstance(element_type, ArrayType):
                    element_type = element_type.pointee
                element_size = self._type_size(element_type.to_string())
            elif "[" in ftype:
                # "char[15]" → element_type="char", count=15
                bracket = ftype.index("[")
                single_element_type = ftype[:bracket]
                count = int(ftype[bracket + 1 : -1])
                element_size = self._type_size(single_element_type)
                field_size = element_size * count
            else:
                field_size = self._type_size(ftype)
                element_size = field_size
            layout[declaration_field.field_name] = FieldInfo(
                bit_offset=None,
                bit_width=None,
                byte_offset=cursor,
                element_size=element_size,
                field_size=field_size,
                type_name=ftype,
            )
            cursor += field_size
        if run_bits > 0:
            cursor += 1
        self.struct_layouts[declaration.name] = layout
        self.struct_sizes[declaration.name] = cursor

    def _resolve_dereference(self, place: DereferencePlace, /) -> MemoryOperand:
        """Resolve a standalone ``DereferencePlace`` (``*ptr``) to a MemoryOperand.

        A dereference breaks the deref-free address segment: the prior
        segment's pointer value is materialized and a fresh segment begins
        based at it.  Two shapes:

        - ``&local`` (a ``PlaceAddressOf(VariablePlace)`` of a local, optionally
          wrapped in a transparent ``Cast``): a frame-direct operand with NO pointer load,
          mirroring the fast path the legacy
          ``_emit_dereference_place_load`` / store emitted.
        - any other pointer expression: evaluate it into the accumulator and
          base the operand at that register.

        ``field_size`` / ``element_size`` carry the pointee width so the
        terminal load/store sizes itself; they are equal because a scalar
        dereference never decays.
        """
        pointer = place.pointer
        width = self._dereference_place_width(place)
        fast_path_target = pointer.expression if isinstance(pointer, Cast) else pointer
        fast_path_name = address_of_variable_name(fast_path_target)
        if fast_path_name is not None and fast_path_name in self.locals:
            return MemoryOperand(
                base=self._local_address(fast_path_name),
                base_kind="frame",
                element_size=width,
                field_size=width,
            )
        self.generate_expression(pointer)
        return MemoryOperand(
            base=self.target.acc,
            base_kind="register",
            element_size=width,
            field_size=width,
        )

    def _resolve_index_member_layout(self, name: str, member_name: str, line: int, /) -> tuple[str, int, int, int, int]:
        """Return layout tuple for a struct array member access.

        Tuple shape: ``(const_base, struct_size, field_offset, field_size, element_size)``.

        ``const_base`` is a NASM operand fragment usable as the base inside a
        memory reference: a label string (e.g. ``_g_arr``) for globals, or a
        frame-relative expression (e.g. ``ebp-12``) for local stack arrays.

        Validates that *name* is a global or local array of a known struct type
        and that *member_name* is a declared field.  Raises :exc:`CompileError`
        for unknown names or fields.
        """
        if name in self.global_arrays:
            declaration = self.global_arrays[name]
            type_name = declaration.type_name
            if not type_name.startswith("struct "):
                message = f"'{name}' element type '{type_name}' is not a struct"
                raise CompileError(message, line=line)
            tag = type_name[7:]
            const_base = self._resolve_constant(name)
            assert const_base is not None
        elif name in self.local_stack_arrays:
            type_name = self.variable_types.get(name, "")
            if not type_name.startswith("struct "):
                message = f"'{name}' is not a local struct array"
                raise CompileError(message, line=line)
            tag = type_name[7:]
            frame_offset = self.locals[name]
            if self.elide_frame:
                const_base = f"_l_{name}"
            elif frame_offset > 0:
                const_base = f"{self.target.base_register}-{frame_offset}"
            else:
                const_base = f"{self.target.base_register}+{-frame_offset}"
        else:
            message = f"'{name}' is not a struct array"
            raise CompileError(message, line=line)
        layout = self.struct_layouts.get(tag)
        if layout is None:
            message = f"unknown struct '{tag}'"
            raise CompileError(message, line=line)
        if member_name not in layout:
            message = f"struct '{tag}' has no field '{member_name}'"
            raise CompileError(message, line=line)
        struct_size = self._type_size(type_name)
        info = layout[member_name]
        return const_base, struct_size, info.byte_offset, info.field_size, info.element_size

    def _resolve_member_index(self, place: SubscriptPlace, /) -> MemoryOperand:
        """Resolve ``base.field[index]`` to a register-base MemoryOperand.

        Emits the byte-exact address computation the legacy member-index
        lowerer used (struct base into SI/BX, constant indices folded into a
        displacement, variable indices scaled and added through BX, pointer
        fields dereferenced first), then returns a ``register``-base operand
        naming the final address so the shared terminal performs the load /
        store.  ``raw_width`` is set so the load uses a plain ``mov`` for a
        word element (no ``movzx`` promotion), matching the legacy output.
        """
        member = place.base
        assert isinstance(member, MemberPlace)
        resolved = self._member_index_arrow_object(member)
        if resolved is None:
            message = "unsupported member-index base in _resolve_member_index"
            raise CompileError(message, line=place.line)
        arrow, object_name = resolved
        index = place.index
        info = self._resolve_member_index_layout(
            arrow=arrow,
            line=place.line,
            member_name=member.member_name,
            object_name=object_name,
        )
        if info.bit_width is not None:
            message = f"indexing bitfield '{member.member_name}' is not supported"
            raise CompileError(message, line=place.line)
        element_size, is_pointer_field = self._member_index_element_size(info)
        allowed_sizes = (1, 2, 4) if is_pointer_field else (1, 2)
        if element_size not in allowed_sizes:
            message = f"indexing '{member.member_name}' (element size {element_size}) not supported"
            raise CompileError(message, line=place.line)
        field_offset = info.byte_offset

        def operand_at(base_register: str, displacement: int) -> MemoryOperand:
            return MemoryOperand(
                base=base_register,
                base_kind="register",
                displacement=displacement,
                element_size=element_size,
                field_size=element_size,
                raw_width=True,
            )

        # Constant index: fold offset + index*element_size into a displacement.
        if isinstance(index, Int):
            self.ax_clear()
            if arrow and self.si_local == object_name:
                base_reg = self.target.si_register
            else:
                self._emit_member_index_base(arrow=arrow, object_name=object_name, register=self.target.bx_register)
                base_reg = self.target.bx_register
            if is_pointer_field:
                ptr_addr = self._build_address(base_reg, field_offset)
                self.emit(f"        mov {self.target.bx_register}, {ptr_addr}")
                return operand_at(self.target.bx_register, index.value * element_size)
            return operand_at(base_reg, field_offset + index.value * element_size)
        # Variable index: AX = index, scale, add base+offset.
        self.ax_clear()
        self.generate_expression(index)
        if element_size in (2, 4):
            shift = 1 if element_size == 2 else 2
            self.emit(f"        shl {self.target.acc}, {shift}")
        elif element_size != 1:
            self.emit(f"        imul {self.target.acc}, {element_size}")
        self.emit(f"        push {self.target.acc}")
        if arrow and self.si_local == object_name:
            self.emit(f"        mov {self.target.bx_register}, {self.target.si_register}")
        else:
            self._emit_member_index_base(arrow=arrow, object_name=object_name, register=self.target.bx_register)
        if is_pointer_field:
            ptr_addr = self._build_address(self.target.bx_register, field_offset)
            self.emit(f"        mov {self.target.bx_register}, {ptr_addr}")
        self.emit(f"        pop {self.target.acc}")
        self.emit(f"        add {self.target.bx_register}, {self.target.acc}")
        if is_pointer_field:
            return operand_at(self.target.bx_register, 0)
        return operand_at(self.target.bx_register, field_offset)

    def _resolve_member_index_layout(
        self,
        *,
        arrow: bool = True,
        line: int,
        member_name: str,
        object_name: str,
    ) -> FieldInfo:
        """Validate ``object_name`` is a struct (pointer or value) and look up ``member_name``."""
        struct_type = self.variable_types.get(object_name)
        if struct_type is None:
            message = f"undefined variable '{object_name}'"
            raise CompileError(message, line=line)
        if arrow:
            if not struct_type.startswith("struct ") or not struct_type.endswith("*"):
                message = f"'->' requires a pointer to struct, got type '{struct_type}'"
                raise CompileError(message, line=line)
            tag = struct_type[7:-1]
        else:
            if not struct_type.startswith("struct ") or struct_type.endswith("*"):
                message = f"'.' requires a struct value, got type '{struct_type}'"
                raise CompileError(message, line=line)
            tag = struct_type[7:]
        layout = self.struct_layouts.get(tag)
        if layout is None:
            message = f"unknown struct '{tag}'"
            raise CompileError(message, line=line)
        if member_name not in layout:
            message = f"struct '{tag}' has no field '{member_name}'"
            raise CompileError(message, line=line)
        return layout[member_name]

    def _resolve_member_place_info(self, place: MemberPlace, /) -> tuple[str, bool, FieldInfo]:
        """Resolve the base operand and field info for a non-array member ``place``.

        Returns ``(const_base, base_is_register, info)``.  Dispatches on
        ``place.base`` to reproduce the legacy member-codegen base
        materialization byte-for-byte:

        - :class:`VariablePlace` — dot access ``obj.field`` on a struct value.
          *const_base* is the struct's memory operand (``_g_obj`` or
          ``ebp-N``); no register is emitted, *base_is_register* False.
        - :class:`DereferencePlace` of a :class:`VariablePlace` — arrow access
          ``ptr->field``.  Emits the shared SI-or-BX base load and returns the
          register, *base_is_register* True.
        - :class:`DereferencePlace` of any other expression — the
          ``((struct T *)e)->field`` / chained ``a->b.c`` form.  The
          ``Cast(&local)`` fast path returns the local's frame
          operand without a register; the general path evaluates the pointer
          expression into BX (``mov bx, acc``) and returns it.
        - :class:`MemberPlace` — chained ``a.b.c`` dot on a struct-value
          member.  Evaluates ``PlaceLoad(place.base)`` (the intermediate
          struct address) into BX and returns it, *base_is_register* True.
        """
        base = place.base
        member_name = place.member_name
        line = place.line
        # Dot access on a named struct value: ``obj.field``.
        if isinstance(base, VariablePlace):
            struct_type = self.variable_types.get(base.name)
            if struct_type is None:
                message = f"undefined variable '{base.name}'"
                raise CompileError(message, line=line)
            if struct_type.endswith("*") or not struct_type.startswith("struct "):
                message = f"'.' requires a struct value, got type '{struct_type}'"
                raise CompileError(message, line=line)
            tag = struct_type[7:]
            info = self._lookup_struct_field(tag, member_name, line)
            base_operand = self._resolve_struct_value_base(base.name, line=line)
            return base_operand, False, info
        # Arrow access on a named pointer: ``ptr->field``.
        if isinstance(base, DereferencePlace) and isinstance(base.pointer, VariablePlace):
            object_name = base.pointer.name
            info = self._resolve_member_index_layout(
                arrow=True,
                line=line,
                member_name=member_name,
                object_name=object_name,
            )
            base_register = self._load_member_base(object_name)
            return base_register, True, info
        # Arrow access through an arbitrary pointer expression:
        # ``((struct T *)e)->field`` or chained ``a->b.c`` (DereferencePlace
        # wrapping a PlaceLoad of the inner pointer member).
        if isinstance(base, DereferencePlace):
            pointer_expression = base.pointer
            base_type = self._expression_type(pointer_expression)
            if not base_type.startswith("struct ") or not base_type.endswith("*"):
                message = f"'->' requires a pointer to struct, got type '{base_type}'"
                raise CompileError(message, line=line)
            tag = base_type[7:-1].rstrip()
            info = self._lookup_struct_field(tag, member_name, line)
            cast_address_name = address_of_variable_name(pointer_expression.expression) if isinstance(pointer_expression, Cast) else None
            if cast_address_name is not None and cast_address_name in self.locals:
                direct_address = self._local_address(cast_address_name)
                return direct_address, False, info
            self.generate_expression(pointer_expression)
            self.emit(f"        mov {self.target.bx_register}, {self.target.acc}")
            self.ax_clear()
            return self.target.bx_register, True, info
        # Chained dot on a struct-value member: ``a.b.c`` (or ``a->b.c`` where
        # the inner ``a->b`` yields a struct-value address).
        if isinstance(base, MemberPlace):
            base_type = self._place_type(base)
            if not base_type.startswith("struct ") or base_type.endswith("*"):
                message = f"'.' requires a struct value, got type '{base_type}'"
                raise CompileError(message, line=line)
            tag = base_type[7:]
            info = self._lookup_struct_field(tag, member_name, line)
            self.ax_clear()
            self.generate_expression(PlaceLoad(line=line, place=base))
            self.emit(f"        mov {self.target.bx_register}, {self.target.acc}")
            self.ax_clear()
            return self.target.bx_register, True, info
        message = "unsupported member Place base in _resolve_member_place_info"
        raise CompileError(message, line=line)

    @staticmethod
    def _reconstruct_double_index_place(base_name: str, indices: list[Node], /, *, line: int) -> SubscriptPlace:
        """Rebuild the legacy array-of-pointers ``name[i0][i1]`` node from a uniform chain.

        The parser now emits one uniform shape
        (``SubscriptPlace(SubscriptPlace(VariablePlace, i0), i1)``) for
        every 2+-subscript access.  For a NON-multidim base (an array of
        pointers or a pointer), reconstruct the deref-rooted node
        — ``SubscriptPlace(DereferencePlace(Index(Var, i0)), i1)`` — which
        :meth:`resolve_address` then lowers like any other lvalue address
        (the dereference materializes the element pointer into ESI; the
        outer subscript folds onto it).

        For an arbitrary-depth array-of-pointers chain (``grid[i0]..[iN]``,
        each level a deref+index), the pointer feeding the outermost
        dereference is the rvalue of the first ``N-1`` indices — a
        left-nested ``Index`` EXPRESSION ``Index(Index(...Index(Var, i0)...,
        i_{N-2}))``.  :meth:`generate_expression` over that nested ``Index``
        evaluates the array-of-pointers read as an rvalue (each inner
        subscript loads one pointer level); the :class:`DereferencePlace`
        materializes the result as the base register and the final
        ``[i_{N-1}]`` subscript folds on top, so any depth resolves through
        the one recursive :meth:`resolve_address` / index-expression path.
        """
        if len(indices) < 2:
            message = "array-of-pointers reconstruct requires at least two subscripts"
            raise CompileError(message, line=line)
        *outer_indices, last_index = indices
        pointer: Node = Var(line=line, name=base_name)
        for outer_index in outer_indices:
            pointer = Index(array=pointer, index=outer_index, line=line)
        return SubscriptPlace(
            base=DereferencePlace(line=line, pointer=pointer),
            index=last_index,
            line=line,
        )

    def _resolve_struct_value_base(self, name: str, /, *, line: int) -> str:
        """Return the memory operand naming the base of struct-value *name*.

        Used by the dot path of :meth:`generate_member_access` and
        :meth:`generate_member_assign` to resolve ``obj`` in ``obj.field``
        to either ``_g_<name>`` (file-scope struct global) or
        ``ebp-<frame_offset>`` (local struct value).  Raises
        :class:`CompileError` for undefined names.
        """
        if name in self.global_scalars:
            return self._local_address(name)
        if name in self.locals:
            frame_offset = self.locals[name]
            return f"ebp-{frame_offset}"
        message = f"undefined variable '{name}'"
        raise CompileError(message, line=line)

    def _select_auto_pin_candidates(self, *, body: list[Node], parameters: list, apply_liveness_elision: bool = True) -> dict[str, str]:
        """Choose locals/parameters to auto-pin and match them to registers.

        Body locals win slots before parameters — pinning a body local
        lets its initializer target the register directly (avoiding the
        store) and, when every body local fits, eliminates the frame
        allocation entirely.  Within each class, candidates are ranked
        by Var/Assign/Index/IndexAssign occurrence count in *body*,
        with declaration order as the tiebreaker.  The ranked candidate
        list is zipped with :attr:`safe_pin_registers` (already sorted
        by ascending clobber count), so the top candidate gets the
        cheapest register.  A pin is emitted only when the candidate's
        reference count strictly exceeds the matched register's
        clobber count — otherwise the ``push``/``pop`` overhead at each
        clobbering call would swallow the savings.

        Eligibility mirrors :meth:`can_auto_pin` and :meth:`scan_locals`:
        ``unsigned long`` locals, constant aliases, and call-initialized
        locals are skipped; array parameters are skipped as well.

        Returns:
            ``{name: register}`` for each selected pin.  Empty when no
            candidate beats its register's clobber cost.

        """
        if not self.safe_pin_registers:
            return {}
        economics = self._compute_pin_economics(body=body, parameters=parameters)
        counts = economics.reference_counts
        index_uses = economics.index_uses
        pre_store_clobbers = economics.pre_store_clobbers
        combined = economics.ranked
        assignments: dict[str, str] = {}
        available = list(self.safe_pin_registers)
        # ``register_holders``: register name -> list of pinned-var names
        # already assigned to that register.  Populated in the
        # primary loop below and read by the sharing pass that
        # follows it.
        register_holders: dict[str, list[str]] = {}
        deferred_for_sharing: list[tuple[str, int]] = []
        for name, _ in combined:
            if not available:
                deferred_for_sharing.append((name, counts.get(name, 0)))
                continue
            non_bp = [register for register in available if register != "bp"]
            best_other = min(non_bp, key=lambda register: self.register_clobber_counts.get(register, 0)) if non_bp else None
            # Decide BP vs the cheapest non-BP register when both are
            # still available.  BP avoids push/pop at every callee
            # that clobbers ``best_other`` (2 bytes each), but adds a
            # 2-byte ``mov si, bp`` to every subscript reference.
            # Choose whichever side wins by raw byte count.
            if "bp" in available and best_other is not None:
                bp_savings = self.register_clobber_counts.get(best_other, 0)
                bp_penalty = index_uses.get(name, 0)
                chosen = "bp" if bp_savings > bp_penalty else best_other
            elif "bp" in available:
                chosen = "bp"
            elif best_other is not None:
                chosen = best_other
            else:
                deferred_for_sharing.append((name, counts.get(name, 0)))
                continue
            refs = counts.get(name, 0)
            # Effective cost subtracts pre-first-store clobbers: PR #454's
            # liveness pre-pass elides ``push <pin>`` / ``pop <pin>``
            # around any call before the local's first store, so those
            # bytes never appear at runtime even though the raw clobber
            # count includes them.
            raw_cost = self.register_clobber_counts.get(chosen, 0)
            elided = pre_store_clobbers.get(name, {}).get(chosen, 0) if apply_liveness_elision else 0
            effective_cost = max(0, raw_cost - elided)
            if refs > effective_cost:
                assignments[name] = chosen
                available.remove(chosen)
                register_holders.setdefault(chosen, []).append(name)
            else:
                # Candidate didn't beat its matched register's
                # effective cost.  Earlier code broke here under the
                # assumption that every later candidate was lower
                # priority — but priority ranks by ref count, not by
                # matched-register cost, and a lower-ref candidate may
                # still beat a cheaper leftover register (e.g. EDI/CX
                # at zero clobber for a function with no calls).
                # Continue so those candidates get a chance.  PR #471's
                # ``ax_clear`` after the pinned-right cmp fast path is
                # the load-bearing safety belt for the extra pins this
                # produces.
                continue
        # Sharing pass: liveness-driven reuse of already-taken
        # registers for candidates whose live ranges don't overlap
        # any name on the register.  Skipped when the analyzer can't
        # safely speak about *body* (raises ``LivenessAnalysisError``
        # for a node it doesn't model); we fall through with the
        # candidate left unpinned rather than risk a miscompile.
        if deferred_for_sharing and register_holders:
            try:
                analyzer = LivenessAnalyzer(body=body, parameters=parameters)
                interference = analyzer.interference()
            except LivenessAnalysisError:
                interference = None
            if interference is not None:
                for name, refs in deferred_for_sharing:
                    neighbours = interference.get(name, set())
                    candidate_registers = [
                        register for register, holders in register_holders.items() if all(holder not in neighbours for holder in holders)
                    ]
                    if not candidate_registers:
                        continue
                    chosen = min(candidate_registers, key=lambda register: self.register_clobber_counts.get(register, 0))
                    raw_cost = self.register_clobber_counts.get(chosen, 0)
                    elided = pre_store_clobbers.get(name, {}).get(chosen, 0) if apply_liveness_elision else 0
                    effective_cost = max(0, raw_cost - elided)
                    if refs > effective_cost:
                        assignments[name] = chosen
                        register_holders[chosen].append(name)
        return assignments

    def _si_scratch_guard_begin(self, base_var: str | None = None, /) -> bool:
        """Emit ``push si`` if SI is aliased to a pinned global.

        When an ``asm_register("si")`` global is declared, SI holds the
        aliased value across the program.  Subscripts on other ``char
        *`` pointers normally lower to ``mov si, <base> ; mov al,
        [si]``, which would trash the alias.  This helper emits a
        ``push si`` guard (returns True) when:

        - an ``asm_register("si")`` global exists, and
        - the subscript base *isn't* that same global (no clobber
          happens when ``mov si, si`` is a no-op).

        The caller pairs the guard with :meth:`_si_scratch_guard_end`.
        """
        si = self.target.si_register
        if not any(register == si for register in self.register_aliased_globals.values()):
            return False
        if base_var is not None and self.register_aliased_globals.get(base_var) == si:
            return False
        self.emit(f"        push {si}")
        return True

    def _si_scratch_guard_end(self, *, guarded: bool) -> None:
        """Pair with :meth:`_si_scratch_guard_begin` — emit ``pop si``."""
        if guarded:
            self.emit(f"        pop {self.target.si_register}")

    def _store_accumulator_to_local(self, name: str, /, *, direct_register: str | None) -> None:
        """Store the accumulator into local *name* (pinned register / byte / word tail).

        The shared tail of :meth:`emit_store_local`, extracted so the native
        ``ir.Load`` terminal can run the byte-identical store + AX-tracking
        sequence after materializing an AddressPlan load.
        """
        if direct_register is not None:
            if direct_register != self.target.acc:
                # When storing into a 16-bit register from a 32-bit acc,
                # use the low-word of acc to avoid an invalid operand mix.
                source = self.target.low_word(self.target.acc) if len(direct_register) < len(self.target.acc) else self.target.acc
                self.emit(f"        mov {direct_register}, {source}")
            self.ax_is_byte = False
        elif self._is_byte_scalar(name):
            # Byte-scalar locals and globals store as a single byte;
            # the source value is either already byte-valued
            # (``ax_is_byte``) or sits in AX's low byte (wider
            # operands truncate to 8 bits on store).  Either way,
            # writing AL alone leaves the neighbouring byte untouched.
            self.emit(f"        mov [{self._local_address(name)}], al")
            # AL still holds the stored byte but AH may be stale: the
            # store is itself an AL-only consumer, which lets
            # :meth:`peephole_dead_ah` drop the zero-extend emitted by a
            # preceding byte load.  Mark AX as byte-valued so downstream
            # compare / test paths emit ``cmp al`` / ``test al`` and
            # don't read the high byte.  Any promotion to a full word
            # goes through the Var-load path which re-issues the load.
            self.ax_is_byte = True
        else:
            self.emit(f"        mov [{self._local_address(name)}], {self.target.acc}")
            self.ax_is_byte = False
        self.ax_local = name
        # ``mov ax, D / <operation> ax, ... / mov D, ax`` sequences are fused
        # by the late peephole passes into a single ``<operation> D, ...`` (or
        # into a compute-into-pinned-register form), neither of which
        # leaves AX holding the new value.  When that fusion applies,
        # the ``ax_local`` tracking we just set would let a downstream
        # read of ``name`` skip its reload and pick up the pre-sequence
        # AX contents instead.  Invalidate the tracking here so the
        # reload happens naturally.
        if self._peephole_will_strand_ax():
            self.ax_local = None

    def _tally_auto_pin_counts(self, node: Node, /, *, role: str = "other", state: AutoPinTallyState) -> None:
        """Tally per-var reference counts for :meth:`_select_auto_pin_candidates`.

        Walks *node* and writes into *state*'s dicts and sets:
        ``counts`` (combined Var + Assign + Index/IndexAssign ref total),
        ``ax_resident_uses`` (LEFT-of-cmp-against-const Vars),
        ``other_uses`` (every other Var read),
        ``init_count``/``init_expr`` (VarDecl/Assign initializer tallies),
        ``index_uses`` (Vars appearing inside subscripts),
        and ``address_taken`` (``&x`` targets).  ``body_candidates`` is
        mid-walk read-then-write — the Switch-discriminant boost path
        appends to it after consulting the existing entries.

        *role* threads the comparison-operand context: ``"cmp_left_imm"``
        for the left operand of a comparison whose right side is a
        compile-time constant, ``"other"`` everywhere else.
        """
        if isinstance(node, (Var, Assign)):
            state.counts[node.name] = state.counts.get(node.name, 0) + 1
        elif isinstance(node, (Index, IndexAssign)):
            state.counts[node.array.name] = state.counts.get(node.array.name, 0) + 1
        if isinstance(node, Switch) and isinstance(node.discriminant, Var):
            # Each case-label dispatch reads the discriminant once.  If the
            # switch is structured so that no case body falls through to the
            # next (every non-empty body always-exits, and empty multi-label
            # intermediates are followed by another case), the interleaved
            # dispatch shape in :meth:`generate_switch` can use a pinned-
            # register `cmp R, imm; jne short` per arm — a 4-byte saving
            # versus the separated `cmp al, imm; je near` form.  Boost the
            # discriminant's ref count so the pin allocator ranks it above
            # candidates whose only use is a single read.  The generic walk
            # below already counts the discriminant once, so add ``arm_count
            # - 1`` here for a total of ``arm_count`` from the switch.
            case_arms = [case for case in node.cases if case.value is not None]
            if case_arms and self._switch_can_interleave(case_arms):
                state.counts[node.discriminant.name] = state.counts.get(node.discriminant.name, 0) + len(case_arms) - 1
                # The Call-init filter in ``collect`` above excludes
                # ``int x = getchar();`` and similar from the candidate
                # list (the rationale: pinning a callee's AX return adds
                # a ``mov R, eax`` that often outweighs the per-ref save).
                # For a switch discriminant with N >= 4 always-exit arms
                # the interleaved-dispatch win (4 bytes per arm vs the
                # separated near-jump form) easily covers that move, so
                # add the discriminant here when it isn't already a body
                # candidate.
                existing_names = {body_name for body_name, _ in state.body_candidates}
                if node.discriminant.name not in existing_names and len(case_arms) >= 4:
                    state.body_candidates.append((node.discriminant.name, len(state.body_candidates)))
                # Tell ``can_auto_pin`` to honor the pin even when the
                # declaration's init is a ``Call`` — the per-arm win
                # easily covers the extra ``mov R, eax`` after the call.
                self.switch_pin_overrides.add(node.discriminant.name)
        if isinstance(node, VarDecl) and node.init is not None:
            state.init_count[node.name] = state.init_count.get(node.name, 0) + 1
            state.init_expr[node.name] = node.init
            self._tally_auto_pin_counts(node.init, state=state)
            return
        if isinstance(node, Assign):
            state.init_count[node.name] = state.init_count.get(node.name, 0) + 1
            state.init_expr[node.name] = node.expr
            self._tally_auto_pin_counts(node.expr, state=state)
            return
        if isinstance(node, Var):
            if role == "cmp_left_imm":
                state.ax_resident_uses[node.name] = state.ax_resident_uses.get(node.name, 0) + 1
            else:
                state.other_uses[node.name] = state.other_uses.get(node.name, 0) + 1
        if isinstance(node, BinaryOperation):
            if node.operation in COMPARISON_OPERATIONS:
                # Only the LEFT operand can reuse an AX-resident
                # value: the right side is loaded into CX after the
                # left's evaluation has overwritten AX.  Even on
                # the left, the fast path requires the right side
                # to be a constant (Int or NAMED_CONSTANT) so the
                # cmp can be ``cmp ax, imm`` / ``cmp ax, NAME``.
                right_is_const = isinstance(node.right, Int) or (isinstance(node.right, Var) and node.right.name in self.NAMED_CONSTANTS)
                left_role = "cmp_left_imm" if right_is_const else "other"
                self._tally_auto_pin_counts(node.left, role=left_role, state=state)
                self._tally_auto_pin_counts(node.right, role="other", state=state)
            else:
                self._tally_auto_pin_counts(node.left, role="other", state=state)
                self._tally_auto_pin_counts(node.right, role="other", state=state)
            return
        if isinstance(node, (Index, IndexAssign)):
            # The `array` Var was already tallied via the counts[] branch
            # above; recursing into it would double-count and add a
            # spurious other_uses tally.  Walk the remaining children
            # explicitly and bail before the generic walk.
            self._tally_subscript_var_uses(node.index, index_uses=state.index_uses)
            self._tally_auto_pin_counts(node.index, state=state)
            if isinstance(node, IndexAssign):
                self._tally_auto_pin_counts(node.expr, state=state)
            return
        if isinstance(node, Call):
            # ``&x`` at an ``out_register`` arg position is a fake
            # address — the callee writes the named register and
            # the caller captures it, so *x* doesn't need a memory
            # address and stays eligible for auto-pin.  Count those
            # args as a Var read (so the ref count reflects the
            # captured write that follows the call) but skip the
            # ``address_taken`` mark the generic address-of branch
            # below would record.  Real-address args (anything else)
            # fall through to that branch.
            out_regs = self.out_register_params.get(node.name, {})
            for index, arg in enumerate(node.args):
                taken_name = address_of_variable_name(arg)
                if index in out_regs and taken_name is not None:
                    self._tally_auto_pin_counts(Var(line=arg.line, name=taken_name), state=state)
                else:
                    self._tally_auto_pin_counts(arg, state=state)
            return
        if (taken_name := address_of_variable_name(node)) is not None:
            # ``&x`` computes an address, not a value read — the inner
            # ``VariablePlace`` carries ``name`` as a plain str so it never
            # tallies as a Var read; preserve that by skipping the generic
            # walk's descent into the place.  Track the name so the candidate
            # filter below can disqualify it: an auto-pinned register
            # has no memory address, and keeping the slot in sync with
            # the register across writes through the pointer would
            # require spill+reload at every access.
            state.address_taken.add(taken_name)
            return
        if isinstance(node, PlaceStore) and isinstance(node.place, DereferencePlace):
            # ``*p = v`` / ``*(T *)e = v`` on the Place tree.  The
            # named-pointer form counts only the right-hand side — the
            # pointer is read directly by name and never tallies as an
            # auto-pin candidate read; the cast / arbitrary-address form
            # walks both the address expression and the value through the
            # generic descent.
            if isinstance(node.place.pointer, Var):
                self._tally_auto_pin_counts(node.value, state=state)
            else:
                self._tally_auto_pin_counts(node.place.pointer, state=state)
                self._tally_auto_pin_counts(node.value, state=state)
            return
        if isinstance(node, PlaceLoad) and isinstance(node.place, DereferencePlace):
            # ``*(T *)e`` read on the Place tree.  Walk the pointer
            # expression generically; the Place pointer here is the
            # wrapping ``Cast`` whose inner is that expression (Cast
            # descent is a no-op that recurses into its expression).
            self._tally_auto_pin_counts(node.place.pointer, state=state)
            return
        if (
            isinstance(node, PlaceLoad)
            and isinstance(node.place, SubscriptPlace)
            and isinstance(node.place.base, DereferencePlace)
            and isinstance(node.place.base.pointer, Index)
        ):
            # ``name[i][j]`` on the Place tree.  Walk ``array`` (a Var →
            # counts[]), the outer index, and the inner index through the
            # generic descent: the inner ``Index`` covers array + outer
            # index, and the subscript index covers the inner index.
            self._tally_auto_pin_counts(node.place.base.pointer, state=state)
            self._tally_auto_pin_counts(node.place.index, state=state)
            return
        for node_field in fields(node):
            value = getattr(node, node_field.name)
            if isinstance(value, Node):
                self._tally_auto_pin_counts(value, state=state)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Node):
                        self._tally_auto_pin_counts(item, state=state)

    def _tally_pre_store_clobbers(
        self,
        node: Node,
        /,
        *,
        candidate_names: set[str],
        function_pointer_vars: set[str],
        pre_store_clobbers: dict[str, dict[str, int]],
        written: dict[str, bool],
    ) -> None:
        """Tally pre-first-store call clobbers per auto-pin candidate.

        Walks *node* and, for each ``Call`` encountered before a
        candidate's first AST-level store, adds one to that candidate's
        entry in *pre_store_clobbers* for every register the call
        clobbers.  ``DoWhile``/``While`` bodies pre-mark their assigned
        candidates as written so back-edge re-entries don't get billed
        for clobbers that the steady-state loop will spill anyway.

        *pre_store_clobbers* and *written* are mutated; *candidate_names*
        and *function_pointer_vars* are read-only inputs.
        """
        if isinstance(node, (DoWhile, While)):
            for name in self._loop_assigned_names(node.body):
                if name in candidate_names:
                    written[name] = True
            for body_statement in node.body:
                self._tally_pre_store_clobbers(
                    body_statement,
                    candidate_names=candidate_names,
                    function_pointer_vars=function_pointer_vars,
                    pre_store_clobbers=pre_store_clobbers,
                    written=written,
                )
            return
        if isinstance(node, Call):
            # Walk args first so a store inside an arg expression
            # (rare but possible) lands in `written` before the
            # call itself is counted.  Then tally clobbers for
            # candidates still pre-store.
            for arg in node.args:
                self._tally_pre_store_clobbers(
                    arg,
                    candidate_names=candidate_names,
                    function_pointer_vars=function_pointer_vars,
                    pre_store_clobbers=pre_store_clobbers,
                    written=written,
                )
            regs = self._clobbers_for_call(node, function_pointer_vars=function_pointer_vars)
            for cand_name, already_written in written.items():
                if not already_written:
                    per_reg = pre_store_clobbers[cand_name]
                    for register in regs:
                        per_reg[register] = per_reg.get(register, 0) + 1
            # ``out_register("REG")`` args capture into the named
            # local AFTER the call returns — mirror the IR pre-pass
            # by marking those candidates as written here so any
            # subsequent call counts as post-store for them.
            out_regs = self.out_register_params.get(node.name, {})
            for index, arg in enumerate(node.args):
                taken_name = address_of_variable_name(arg)
                if index in out_regs and taken_name is not None and taken_name in candidate_names:
                    written[taken_name] = True
            return
        if isinstance(node, Assign):
            self._tally_pre_store_clobbers(
                node.expr,
                candidate_names=candidate_names,
                function_pointer_vars=function_pointer_vars,
                pre_store_clobbers=pre_store_clobbers,
                written=written,
            )
            if node.name in candidate_names:
                written[node.name] = True
            return
        if isinstance(node, VarDecl):
            if node.init is not None:
                self._tally_pre_store_clobbers(
                    node.init,
                    candidate_names=candidate_names,
                    function_pointer_vars=function_pointer_vars,
                    pre_store_clobbers=pre_store_clobbers,
                    written=written,
                )
                if node.name in candidate_names:
                    written[node.name] = True
            return
        for node_field in fields(node):
            value = getattr(node, node_field.name)
            if isinstance(value, Node):
                self._tally_pre_store_clobbers(
                    value,
                    candidate_names=candidate_names,
                    function_pointer_vars=function_pointer_vars,
                    pre_store_clobbers=pre_store_clobbers,
                    written=written,
                )
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Node):
                        self._tally_pre_store_clobbers(
                            item,
                            candidate_names=candidate_names,
                            function_pointer_vars=function_pointer_vars,
                            pre_store_clobbers=pre_store_clobbers,
                            written=written,
                        )

    @staticmethod
    def _tally_subscript_var_uses(node: Node, /, *, index_uses: dict[str, int]) -> None:
        """Tally Var occurrences inside Index/IndexAssign subscripts.

        Each subscript pays a 2-byte ``mov si, bp`` penalty when its
        index variable is BP-pinned, since BP can't index DS-relative
        memory in real mode.  The auto-pin cost model uses this tally
        (mutated through *index_uses*) to decide whether a candidate's
        BP-clobber-savings outweigh that per-subscript penalty.
        """
        if isinstance(node, Var):
            index_uses[node.name] = index_uses.get(node.name, 0) + 1
        for node_field in fields(node):
            value = getattr(node, node_field.name)
            if isinstance(value, Node):
                X86CodeGenerator._tally_subscript_var_uses(value, index_uses=index_uses)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Node):
                        X86CodeGenerator._tally_subscript_var_uses(item, index_uses=index_uses)

    def _try_direct_load(self, *, argument: Node, register: str, optimize_zero: bool = False) -> bool:
        """Emit a direct load of a constant-or-address *argument* into *register*.

        Covers integer literals, string literals, named kernel
        constants, constant-aliased variables, global arrays, local
        stack arrays, and constant-folded expressions — every case
        whose source is a compile-time constant or label-relative
        address and that does not need width narrowing or AX tracking
        updates.  Returns ``True`` when a load was emitted; ``False``
        tells the caller to handle *argument* via its own path
        (pinned register, memory-resident scalar, or generic
        expression).

        *optimize_zero* lowers ``Int(0)`` to ``xor reg, reg`` instead
        of ``mov reg, 0``.  :meth:`emit_store_local` uses this for
        pinned destinations where the shorter encoding is pure win;
        argument-loader paths leave it off to keep the canonical
        ``mov reg, imm`` shape that downstream peepholes match on.
        """
        if isinstance(argument, Int):
            if optimize_zero and argument.value == 0:
                self.emit(f"        xor {register}, {register}")
            else:
                self.emit(f"        mov {register}, {argument.value}")
            return True
        if isinstance(argument, String):
            self.emit(f"        mov {register}, {self.new_string_label(argument.content)}")
            return True
        if isinstance(argument, Var):
            name = argument.name
            if name in self.NAMED_CONSTANTS:
                self.emit_constant_reference(name)
                self.emit(f"        mov {register}, {name}")
                return True
            if name in self.constant_aliases:
                self.emit(f"        mov {register}, {self.constant_aliases[name]}")
                return True
            if name in self.global_arrays:
                self.emit(f"        mov {register}, {self._global_label(name)}")
                return True
            if name in self.local_stack_arrays:
                if self.elide_frame:
                    self.emit(f"        mov {register}, _l_{name}")
                else:
                    offset = self.locals[name]
                    self.emit(f"        lea {register}, [{self.target.base_register}-{offset}]")
                return True
        if (constant_expr := self._constant_expression(argument)) is not None:
            for name in self._collect_constant_references(argument):
                self.emit_constant_reference(name)
            self.emit(f"        mov {register}, {constant_expr}")
            return True
        return False

    def _try_emit_guarded_update(self, *, expression: Conditional, name: str) -> bool:
        """Emit a tight ``cmp / Jcc / mov dest, other`` for ``dest = (...) ? dest : other``.

        Returns True when the ternary matched the guarded-update
        shape and the assignment was emitted; the caller (``emit_store_local``)
        then skips its default ternary-via-AX lowering.  Returns False
        for any ternary whose branches don't structurally mirror the
        destination — those go through the standard path.

        Recognised shapes (both produced verbatim by
        ``MAX(dest, other)`` / ``MIN(dest, other)``):

        * ``dest = C ? Var(dest) : other`` — the no-op then-branch is
          elided.  The assignment fires when ``C`` is false, so we emit
          a *true*-jump that skips it.
        * ``dest = C ? other : Var(dest)`` — the no-op else-branch is
          elided.  The assignment fires when ``C`` is true, so we emit
          a *false*-jump that skips it.

        The condition is normalised the same way ``parse_condition``
        normalises ``if`` / ``while`` heads, so bare expressions (and
        ``&&`` / ``||`` chains) work without special handling.
        """
        condition = expression.condition
        then_expr = expression.then_expr
        else_expr = expression.else_expr
        # Case 1: then-branch is the no-op (dest stays).  Skip the
        # assignment when the condition is true.
        if isinstance(then_expr, Var) and then_expr.name == name:
            other = else_expr
            skip_on = "true"
        # Case 2: else-branch is the no-op.  Skip the assignment when
        # the condition is false.
        elif isinstance(else_expr, Var) and else_expr.name == name:
            other = then_expr
            skip_on = "false"
        else:
            return False
        # Avoid double-evaluation of side-effecting "other" branches —
        # the standard ternary path would emit the call inside the
        # taken branch, so the side effect fires exactly once.  Here
        # ``other`` is emitted unguarded once if the assignment fires;
        # for a Call we have to keep the original path so the side
        # effect doesn't fire when the no-op branch was supposed to
        # win.  Simple-value branches (Int, Var, Char, String, named
        # constants, sizeof) are side-effect-free and safe.
        if not isinstance(other, (Int, Char, String, Var)):
            return False
        # Refuse the optimization when the destination would need an
        # ``unsigned long`` store, byte store, or any of the other
        # non-trivial paths in ``emit_store_local`` — the recursive
        # ``emit_store_local`` call below handles all of them, but
        # only after the AX-tracking invariants are preserved.  In
        # practice none of those cases produce a Conditional at this
        # call site, so guarding here is mostly defensive.
        if self.variable_types.get(name) == "unsigned long":
            return False
        normalised = self._normalise_ternary_condition(condition)
        label_index = self.new_label()
        skip_label = f".cond_skip_{label_index}"
        if skip_on == "true":
            self.emit_condition_true_jump(condition=normalised, context="ast", success_label=skip_label)
        else:
            self.emit_condition_false_jump(condition=normalised, context="ast", fail_label=skip_label)
        self.emit_store_local(expression=other, name=name)
        self.emit(f"{skip_label}:")
        # Control reaches the merge label from two paths (skipped and
        # not-skipped); AX-tracking accumulated by the assignment
        # path can't be promised on the skip path, so clear it.
        self.ax_clear()
        return True

    def _try_fold_bitfield_int_store(self, operand: MemoryOperand, value: Node, /) -> bool:
        """Const-fold a bitfield ``= Int`` store into a known local byte.

        Returns True (and emits a single ``mov byte``) when *operand* is a
        bitfield whose target byte is a tracked ``known_local_bytes`` slot and
        *value* is an integer literal; the resulting byte is computed at compile
        time.  Reproduces the legacy dot-store fast path, which folded without
        evaluating the rhs into a register.  Returns False otherwise (caller
        falls back to the rhs-in-register store).
        """
        info = operand.bitfield
        if info is None or not isinstance(value, Int):
            return False
        address = self._build_address(operand.base, operand.displacement, index=operand.index or "")
        slot = self._parse_local_byte_addr(address)
        if slot is None or slot not in self.known_local_bytes:
            return False
        field_mask = ((1 << info.bit_width) - 1) << info.bit_offset
        clear_mask = (~field_mask) & 0xFF
        known = self.known_local_bytes[slot]
        rhs = value.value & ((1 << info.bit_width) - 1)
        new_byte = (known & clear_mask) | (rhs << info.bit_offset)
        self.emit(f"        mov byte {address}, {new_byte}")
        return True

    def _try_fuse_word_conditions(self, leaves: list[Node], /, *, fail_label: str, context: str) -> None:
        """Emit a flattened ``&&`` chain, fusing adjacent byte comparisons.

        Scans *leaves* for consecutive pairs where both sides are
        byte-index ``==`` comparisons on the same base variable(s) with
        adjacent indices.  Fusible pairs are emitted as a single
        word-sized comparison; non-fusible leaves fall through to the
        normal ``emit_condition`` path.

        Two fusion patterns are recognized:

        1. **byte-index vs constant pair** — ``a[N] == K1 && a[N+1] == K2``
           becomes ``cmp word [bx+N], (K2<<8)|K1`` (little-endian).

        2. **byte-index vs byte-index pair** —
           ``a[N] == b[M] && a[N+1] == b[M+1]`` becomes
           ``mov ax, [bx+N] / cmp ax, [bx+M]``.
        """
        i = 0
        while i < len(leaves):
            if i + 1 < len(leaves) and self._is_byte_eq(leaves[i]) and self._is_byte_eq(leaves[i + 1]):
                a, b = leaves[i], leaves[i + 1]
                a_left, a_right = a.left, a.right
                b_left, b_right = b.left, b.right
                # Check left-side indices are adjacent on the same base
                if self._byte_index_base_key(a_left) == self._byte_index_base_key(b_left) and b_left.index.value == a_left.index.value + 1:
                    # Pattern 1: both right sides are integer constants
                    a_lit = a_right.value if isinstance(a_right, Int) else None
                    b_lit = b_right.value if isinstance(b_right, Int) else None
                    if a_lit is not None and b_lit is not None:
                        self.validate_comparison_types(a_left, a_right)
                        operand, guarded = self._emit_byte_index_si(a_left)
                        word_mem = operand.replace("byte ", "word ")
                        word_val = (b_lit << 8) | a_lit
                        self.emit(f"        cmp {word_mem}, 0x{word_val:04x}")
                        self._si_scratch_guard_end(guarded=guarded)
                        self.emit(f"        {JUMP_WHEN_FALSE['==']} {fail_label}")
                        i += 2
                        continue
                    # Pattern 2: both right sides are byte-index with adjacent indices on same base
                    if (
                        self._is_byte_index(a_right)
                        and self._is_byte_index(b_right)
                        and self._byte_index_base_key(a_right) == self._byte_index_base_key(b_right)
                        and b_right.index.value == a_right.index.value + 1
                    ):
                        self.validate_comparison_types(a_left, a_right)
                        left_operand, left_guarded = self._emit_byte_index_si(a_left)
                        left_mem = left_operand.replace("byte ", "word ")
                        self.emit(f"        mov ax, {left_mem.removeprefix('word ')}")
                        self._si_scratch_guard_end(guarded=left_guarded)
                        right_operand, right_guarded = self._emit_byte_index_si(a_right)
                        right_mem = right_operand.replace("byte ", "word ")
                        self.emit(f"        cmp ax, {right_mem.removeprefix('word ')}")
                        self._si_scratch_guard_end(guarded=right_guarded)
                        self.emit(f"        {JUMP_WHEN_FALSE['==']} {fail_label}")
                        i += 2
                        continue
            # Not fusible — emit normally
            self.emit_condition_false_jump(condition=leaves[i], context=context, fail_label=fail_label)
            i += 1

    def _type_size(self, type_name: str, /) -> int:
        """Return the byte size of *type_name* including struct types.

        Handles all primitive types via the target's ``type_sizes`` table,
        pointer-to-struct (``"struct TAG*"``) as a pointer-sized word, and
        value-struct (``"struct TAG"``) by summing the declared field sizes.
        Raises ``CompileError`` for unknown types.
        """
        if type_name in {"int", "unsigned int"} or "*" in type_name or type_name in self.target.type_sizes:
            return self.target.type_size(type_name)
        if type_name == "function_pointer":
            return self.target.int_size
        if type_name.startswith("enum "):
            # ``enum NAME`` and ``enum NAME *`` are int-sized for storage —
            # the variant set drives switch exhaustiveness, not layout.
            return self.target.int_size
        if type_name.startswith("struct "):
            tag = type_name[7:]
            if tag not in self.struct_sizes:
                message = f"unknown struct '{tag}'"
                raise CompileError(message)
            return self.struct_sizes[tag]
        if "[" in type_name:
            bracket = type_name.index("[")
            element_type = type_name[:bracket]
            count = int(type_name[bracket + 1 : -1])
            return self._type_size(element_type) * count
        message = f"unknown type '{type_name}'"
        raise CompileError(message)

    def _update_known_bytes(self, line: str) -> None:
        """Update known_local_bytes and _last_byte_store from a single emitted line.

        Tracks which frame-relative byte slots have a constant value in
        the current basic block.  Conservative invalidation is applied on
        any memory write through a non-ebp register, on function calls,
        and on labels (which mark potential jump targets).  No folding is
        performed here — that is the job of the Phase C.2/C.3/C.4 peepholes.

        Also tracks ``ax_literal``: the integer value currently held in
        EAX/AL when the immediately preceding emit was ``mov eax, <imm>``.
        Cleared conservatively on any other non-empty emit.
        """
        # ax_literal tracking: must run first so clearing EAX state does
        # not interfere with the byte-slot tracker logic below.
        if (eax_match := RE_MOV_EAX_IMMEDIATE.match(line)) is not None:
            self.ax_literal = int(eax_match.group(1))
            # Fall through — the byte-tracker may also care about this line.
        elif line.strip():
            # Any other non-empty emit may have clobbered EAX/AL.
            # Conservative: clear ax_literal on every such emit.
            self.ax_literal = None
        # mov byte [ebp-N] / [ebp-N+M], imm  →  set known value for slot K.
        match = RE_MOV_BYTE_LOCAL_IMMEDIATE.match(line)
        if match:
            base = int(match.group(1))
            offset = int(match.group(2) or 0)
            value = int(match.group(3)) & 0xFF
            slot = base - offset
            self.known_local_bytes[slot] = value
            self._last_byte_store = (slot, value)
            return
        # or byte [ebp-N] / [ebp-N+M], imm  →  fold into known value if present.
        match = RE_OR_BYTE_LOCAL_IMMEDIATE.match(line)
        if match:
            base = int(match.group(1))
            offset = int(match.group(2) or 0)
            value = int(match.group(3)) & 0xFF
            slot = base - offset
            if slot in self.known_local_bytes:
                self.known_local_bytes[slot] = (self.known_local_bytes[slot] | value) & 0xFF
            else:
                self.known_local_bytes.pop(slot, None)
            self._last_byte_store = None
            return
        # and byte [ebp-N] / [ebp-N+M], imm  →  fold into known value if present.
        match = RE_AND_BYTE_LOCAL_IMMEDIATE.match(line)
        if match:
            base = int(match.group(1))
            offset = int(match.group(2) or 0)
            value = int(match.group(3)) & 0xFF
            slot = base - offset
            if slot in self.known_local_bytes:
                self.known_local_bytes[slot] = self.known_local_bytes[slot] & value & 0xFF
            else:
                self.known_local_bytes.pop(slot, None)
            self._last_byte_store = None
            return
        # All other lines: clear the last-byte-store shadow.
        self._last_byte_store = None
        # Conservative: any mov through a non-ebp base register may alias
        # a local.  Clear everything.
        if RE_NON_BYTE_WRITE.search(line):
            self.known_local_bytes.clear()
            return
        # Function calls and software interrupts: called code may clobber
        # arbitrary memory.
        stripped = line.strip().lower()
        if stripped.startswith(("call ", "int ")):
            self.known_local_bytes.clear()
            return
        # Labels mark potential jump targets; we don't track dataflow across
        # branches, so invalidate the whole map.
        if line.rstrip().endswith(":") and not line.lstrip().startswith(";"):
            self.known_local_bytes.clear()
            return

    @staticmethod
    def _uniform_subscript_chain(place: Place, /) -> tuple[str, list[Node]] | None:
        """Flatten a uniform multi-subscript ``name[i0][i1]...`` place.

        Returns ``(base_name, [i0, i1, ...])`` when *place* is a
        left-nested :class:`SubscriptPlace` chain (2+ levels) bottoming out
        in a :class:`VariablePlace`, with NO :class:`DereferencePlace` in
        the chain — the single uniform shape the parser now emits for every
        ``name[i][j]`` access.  Returns ``None`` for any other shape
        (single subscript, member-rooted, deref-rooted) so existing
        dispatch arms keep ownership.
        """
        if not (isinstance(place, SubscriptPlace) and isinstance(place.base, SubscriptPlace)):
            return None
        indices: list[Node] = []
        current: Place = place
        while isinstance(current, SubscriptPlace):
            indices.append(current.index)
            current = current.base
        if not isinstance(current, VariablePlace):
            return None
        indices.reverse()
        return current.name, indices

    def _validate_array_init(self, elements: list[Node]) -> None:
        """Validate global array initializer elements are all constant expressions."""
        for element in elements:
            if isinstance(element, String):
                continue
            if isinstance(element, StructInitializer):
                assert element.positional is not None, "array-of-struct globals require positional initializers"
                for field in element.positional:
                    if self._constant_expression(field) is None:
                        message = "struct initializer fields must be constants"
                        raise CompileError(message, line=field.line)
                    for reference in self._collect_constant_references(field):
                        self.emit_constant_reference(reference)
                continue
            if self._constant_expression(element) is None:
                message = "global array initializer elements must be constants"
                raise CompileError(message, line=element.line)
            for reference in self._collect_constant_references(element):
                self.emit_constant_reference(reference)

    def _validate_node_comparisons(self, node: Node | None, /) -> None:
        """Recursively visit *node*, validating any comparison ``BinaryOperation``.

        Walks every :class:`Node`-typed dataclass field plus list-of-Node
        fields (e.g. ``Call.args``, ``If.body``).  Stops at literal
        leaves (``Int`` / ``Char`` / ``String``) which carry no children.
        """
        if node is None or not isinstance(node, Node):
            return
        if isinstance(node, BinaryOperation) and node.operation in COMPARISON_OPERATIONS:
            self.validate_comparison_types(node.left, node.right)
        for descriptor in fields(node):
            value = getattr(node, descriptor.name)
            if isinstance(value, Node):
                self._validate_node_comparisons(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Node):
                        self._validate_node_comparisons(item)

    def _validate_struct_global_initializer(self, declaration: VarDecl, *, name: str) -> None:
        """Check the struct global's brace initializer fields are constants."""
        init = declaration.init
        assert isinstance(init, StructInitializer)
        tag = declaration.type_name[len("struct ") :]
        layout = self.struct_layouts.get(tag)
        if layout is None:
            message = f"unknown struct '{tag}' for global '{name}'"
            raise CompileError(message, line=declaration.line)
        field_names = list(layout.keys())
        if init.designated is not None:
            field_values = init.designated.items()
        else:
            assert init.positional is not None
            if len(init.positional) > len(field_names):
                message = f"too many initializers for 'struct {tag}'"
                raise CompileError(message, line=declaration.line)
            field_values = zip(field_names, init.positional, strict=False)
        for field_name, value_node in field_values:
            if field_name not in layout:
                message = f"unknown field '{field_name}' in 'struct {tag}'"
                raise CompileError(message, line=value_node.line)
            if self._constant_expression(value_node) is None:
                message = f"struct global '{name}' field initializers must be constants"
                raise CompileError(message, line=value_node.line)
            for reference in self._collect_constant_references(value_node):
                self.emit_constant_reference(reference)

    def _variable_base(self, name: str, /, *, line: int) -> tuple[str, str]:
        """Return ``(base_kind, base)`` for the memory operand of *name*.

        Mirrors the base-string logic of _resolve_struct_value_base and
        _resolve_index_member_layout so that downstream MemoryOperand emission
        produces the same label / frame strings as the existing codegen.

        Returns ("label", "_g_<name>") for a global array or scalar, and
        ("frame", "<base_register>-<offset>") (or the elide_frame "_l_<name>"
        variant) for a local variable.  Raises CompileError for undefined names.
        """
        if name in self.global_arrays or name in self.global_scalars:
            return "label", self._global_label(name)
        if name in self.locals:
            return "frame", self._local_address(name)
        message = f"undefined variable '{name}'"
        raise CompileError(message, line=line)

    def allocate_local(self, name: str, /, *, size: int | None = None) -> int:
        """Allocate a local variable on the stack frame.

        Args:
            name: local variable name.
            size: slot size in bytes.  Defaults to the target's native
                integer width (2 on 16-bit real mode, 4 on 32-bit flat
                protected mode) so plain ``int`` / pointer locals pick up the
                right width without caller-side branching.  Pass ``1``
                explicitly for byte-typed scalars and ``4`` for
                ``unsigned long`` pairs.

        Returns:
            The current frame size after allocation.

        """
        if size is None:
            size = self.target.int_size
        self.frame_size += size
        self.locals[name] = self.frame_size
        return self.frame_size

    def ax_clear(self) -> None:
        """Clear AX tracking state."""
        self.ax_is_byte = False
        self.ax_local = None

    def can_auto_pin(self, *, following_statement: Node | None, statement: VarDecl) -> bool:
        """Decide whether *statement* should be auto-pinned to a register."""
        # The pool-size gate trips only when the candidate's chosen
        # register would be a *new* occupant: liveness-driven sharing
        # reuses an already-pinned register, so a candidate whose
        # register is already among ``pinned_register.values()`` is a
        # share, not a fresh allocation.
        candidate_register = self.auto_pin_candidates.get(statement.name)
        is_share = candidate_register is not None and candidate_register in self.pinned_register.values()
        if not is_share and len(set(self.pinned_register.values())) >= len(self.safe_pin_registers):
            return False
        init = statement.init
        if init is None:
            return True
        # Call initializers normally stay in memory so they can participate
        # in error-return fusion without clobbering a pin.  Switch
        # discriminants are an exception: the interleaved dispatch shape in
        # :meth:`generate_switch` saves 4 bytes per case arm, which on a
        # 4+-arm switch easily covers the extra ``mov R, eax`` after the
        # call (and the missed fusion opportunity, if any).
        if isinstance(init, Call):
            return statement.name in self.switch_pin_overrides
        return True

    def compute_safe_pin_registers(self, body: list[Node], /, *, parameters: list | None = None) -> tuple[str, ...]:
        """Return the pinnable register pool ordered by clobber cost.

        All registers in the pool are pinnable; :meth:`generate_call`
        wraps each call with ``push``/``pop`` for any caller pin the
        callee clobbers.  Ordering by clobber count so that the first
        (most-referenced) candidate lands on the cheapest register.
        The per-function counts are memoised on
        :attr:`register_clobber_counts` so the cost model in
        :meth:`_select_auto_pin_candidates` can reuse them.

        ``main`` (recognised by :attr:`elide_frame`) extends the base
        pool with BP — it doesn't need BP as a frame pointer and
        every callee preserves BP across calls (builtins via the
        kernel's ``pusha``/``popa`` syscall wrapper, user functions
        via the standard ``push bp`` / ``pop bp`` prologue).  That
        gives main a fifth register at zero clobber cost, perfect
        for a high-traffic flag (``dirty``) or scroll counter
        (``view_line``).

        Subscript codegen uses SI as its scratch register; SI isn't
        in the pool so no extra exclusion is needed for subscript
        presence.
        """
        pool = (*self.target.register_pool, self.target.base_register) if self.elide_frame else self.target.register_pool
        clobber_counts: dict[str, int] = dict.fromkeys(pool, 0)

        function_pointer_vars = self._collect_function_pointer_vars(body, parameters=parameters)

        def visit(node: Node) -> None:
            if isinstance(node, Call):
                if node.name in self.user_functions or node.name in function_pointer_vars:
                    # User functions and function_pointer indirect calls follow the standard
                    # cdecl prologue (``push bp / mov bp, sp / … / pop bp``) which
                    # preserves the caller's BP, so BP is omitted from the
                    # user-call clobber set even when it's pinned.
                    for register in self.target.register_pool:
                        clobber_counts[register] += 1
                elif node.name in self.libbboeos_extern_declarations:
                    # Libbboeos extern call — cdecl indirect through the
                    # shared pointer table.  Caller-saved EAX/ECX/EDX
                    # clobbered (same set the user_function path counts),
                    # so charge the full register pool.
                    for register in self.target.register_pool:
                        clobber_counts[register] += 1
                elif node.name not in self.BUILTIN_CLOBBERS:
                    pointer_constant = f"FUNCTION_{node.name.upper()}_PTR"
                    if self.target_mode == "user" and pointer_constant in self.NAMED_CONSTANT_VALUES:
                        message = (
                            f"call to libbboeos export '{node.name}' requires a prior prototype "
                            f'declaration (e.g. `#include "string.h"` or a forward decl)'
                        )
                    else:
                        message = f"unknown function: {node.name}"
                    raise CompileError(message, line=node.line)
                else:
                    for register in self._builtin_clobbers[node.name]:
                        if register in clobber_counts:
                            clobber_counts[register] += 1
            for slot in getattr(type(node), "__slots__", ()):
                child = getattr(node, slot, None)
                if isinstance(child, Node):
                    visit(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, Node):
                            visit(item)

        for statement in body:
            visit(statement)
        self.register_clobber_counts = clobber_counts
        pool_index = {register: index for index, register in enumerate(pool)}
        # Sort by clobber count, then declaration order — but force BP
        # to the tail of the list.  BP can't be used as an index
        # register for DS-relative addressing in real mode (every
        # ``buffer[bp_var]`` access pays a 2-byte ``mov si, bp``), so
        # the highest-traffic candidate (which usually IS an index
        # base) should land on a BX/DI/CX/DX slot first.  BP picks up
        # whatever lower-priority scalar candidate is left over —
        # zero-clobber across every callee makes it pure profit
        # there.
        return tuple(sorted(pool, key=lambda register: (clobber_counts[register], pool_index[register])))

    def discover_virtual_long_locals(self, statements: list[Node], /) -> None:
        """Identify ``unsigned long`` locals whose DX:AX value can stay live.

        Matches the narrow pattern:

            unsigned long NAME = <long_expr>;
            print_datetime(NAME);

        where ``NAME`` is not referenced anywhere else in the function
        body. Such locals skip the memory slot and the store/load
        round-trip; DX:AX is produced by the initializer and consumed
        directly by the next statement.
        """
        for index in range(len(statements) - 1):
            statement = statements[index]
            if not isinstance(statement, VarDecl):
                continue
            # Under --bits 32 the parser folds ``unsigned long`` into
            # ``unsigned int`` (single canonical 32-bit unsigned type),
            # so the virtual-long pattern triggers on either spelling.
            # Under --bits 16 ``unsigned int`` is 16-bit, so only the
            # original ``unsigned long`` shape is eligible.
            long_eligible = {"unsigned long"}
            if self.target.int_size == 4:
                long_eligible.add("unsigned int")
            if statement.type_name not in long_eligible or statement.init is None:
                continue
            consumer = statements[index + 1]
            if not isinstance(consumer, Call) or consumer.name != "print_datetime":
                continue
            if len(consumer.args) != 1:
                continue
            argument = consumer.args[0]
            if not isinstance(argument, Var) or argument.name != statement.name:
                continue
            other_statements = statements[:index] + statements[index + 2 :]
            name = statement.name
            if any(X86CodeGenerator._statement_references(other, name) for other in other_statements):
                continue
            self.virtual_long_locals.add(name)

    def emit(self, line: str = "") -> None:
        """Append a line of assembly and update the known-byte tracker.

        Last-write-wins collapse: if this line is a ``mov byte [ebp-K], imm``
        and the most recently emitted line was also a ``mov byte [ebp-K], imm``
        to the SAME slot K, replace the previous line rather than appending.
        This eliminates redundant sequential stores emitted by the zero-init
        prelude and the folded designated-init writes.
        """
        mov_match = RE_MOV_BYTE_LOCAL_IMMEDIATE.match(line)
        if mov_match is not None and self._last_byte_store is not None and self.lines and RE_MOV_BYTE_LOCAL_IMMEDIATE.match(self.lines[-1]):
            base = int(mov_match.group(1))
            offset = int(mov_match.group(2) or 0)
            slot = base - offset
            if self._last_byte_store[0] == slot:
                # Replace the previous emit and update tracker in place.
                self.lines[-1] = line
                value = int(mov_match.group(3)) & 0xFF
                self.known_local_bytes[slot] = value
                self._last_byte_store = (slot, value)
                return
        self.lines.append(line)
        self._update_known_bytes(line)

    def emit_accumulator_zx_from_al(self) -> None:
        """Zero-extend AL (byte result) to the target accumulator.

        16-bit real mode: ``xor ah, ah`` — clears AH, leaving AX = AL.
        32-bit flat protected mode: ``movzx eax, al`` — clears bits 8-31,
        leaving EAX = AL.  Used after syscalls and byte-returning
        builtins (``exec`` / ``chmod`` / the carry-flag normalize path
        in ``emit_error_syscall_tail``) where the kernel ABI delivers
        the result in AL but the caller's code expects a full
        accumulator-width integer.
        """
        if self.target.int_size == 2:
            self.emit("        xor ah, ah")
        else:
            self.emit(f"        movzx {self.target.acc}, al")

    def emit_argument_vector_startup(self, parameters: list[Param], /, *, body: list[Node]) -> list[Node]:
        """Emit inline startup code that loads argc/argv from the user stack.

        The kernel writes a Linux SysV i386 startup frame on the new
        program's user stack before iretd'ing into ring 3.  At entry:

            [esp + 0]                       argc
            [esp + 4 + 4*i]                 argv[i]   (0 <= i < argc)
            [esp + 4 + 4*argc]              NULL      (argv terminator)
            [esp + 4 + 4*argc + 4]          NULL      (envp terminator,
                                                       envp is currently
                                                       always empty)

        Codegen loads ``argc`` from ``[esp]`` and ``argv`` as
        ``esp + 4`` (a real ``char **`` pointing at the on-stack
        pointer array).  Both are then stored into their per-function
        locals.  The user stack frame stays live for the duration of
        ``main`` because ``main`` exits via ``jmp FUNCTION_EXIT``
        (sys_exit) — the kernel discards the user stack on exit, so
        the layout's lifetime matches the program's.

        When the first statement in *body* is
        ``if (argc != N) die(msg)``, the argc check is fused directly
        against the memory operand ``[esp]`` so the per-function
        ``argc`` local is never written.  Returns the (possibly
        trimmed) body.
        """
        argc_name = None
        argv_name = None
        for param in parameters:
            if param.is_array:
                argv_name = param.name
            elif argc_name is None:
                argc_name = param.name
        if not argv_name:
            return body

        # The function prologue ran `push ebp; mov ebp, esp; sub esp, N`
        # before this startup, so ESP has already been adjusted by the
        # locals reservation but EBP still points at the saved old EBP
        # just below the kernel-supplied argv frame:
        #     [ebp + 0] = saved EBP
        #     [ebp + 4] = argc
        #     [ebp + 8] = argv[0] pointer  (start of the on-stack argv array)
        # Use EBP-relative addressing so the offsets stay stable
        # regardless of how many local bytes the prologue reserved.
        self.emit(f"        lea {self.target.di_register}, [{self.target.base_register} + 8]")
        self.emit(f"        mov [{self._local_address(argv_name)}], {self.target.di_register}")

        # Try to fuse the first body statement: if (argc != N) die(msg)
        fused_argc = False
        if argc_name and body:
            first = body[0]
            if (
                isinstance(first, If)
                and first.else_body is None
                and len(first.body) == 1
                and isinstance(first.body[0], Call)
                and first.body[0].name == "die"
                and len(first.body[0].args) == 1
                and isinstance(first.body[0].args[0], String)
                and isinstance(first.cond, BinaryOperation)
                and first.cond.operation == "!="
                and isinstance(first.cond.left, Var)
                and first.cond.left.name == argc_name
                and isinstance(first.cond.right, Int)
            ):
                die_message = first.body[0].args[0]
                die_label = self.new_string_label(die_message.content)
                die_length = string_byte_length(die_message.content)
                expected = first.cond.right.value
                stack_word = "dword" if self.target.int_size == 4 else "word"
                self.emit(f"        cmp {stack_word} [{self.target.base_register} + 4], {expected}")
                self.emit(f"        mov {self.target.si_register}, {die_label}")
                self.emit(f"        mov {self.target.count_register}, {die_length}")
                # Flat: direct ``jne FUNCTION_DIE`` (org-relative rel32).
                # Object: no org, so a direct rel32 to the absolute libbboeos
                # entry would land at FUNCTION_DIE+PROGRAM_BASE; route through
                # the base-invariant ``jne .skip / jmp [FUNCTION_DIE_PTR]`` form.
                self._emit_libbboeos_jcc("jne", "FUNCTION_DIE")
                fused_argc = True
                body = body[1:]

        if argc_name and not fused_argc:
            self.emit(f"        mov {self.target.count_register}, [{self.target.base_register} + 4]")
            self.emit(f"        mov [{self._local_address(argc_name)}], {self.target.count_register}")
        return body

    def emit_binary_operator_operands(self, left: Node, right: Node, /) -> None:
        """Generate left into AX and right into CX.

        When the right operand is a constant or variable, loads it
        directly into CX without a push/pop round-trip.
        """
        if isinstance(right, Int):
            self.generate_expression(left)
            self.emit(f"        mov {self.target.count_register}, {right.value}")
        elif isinstance(right, Var) and right.name in self.pinned_register:
            self.generate_expression(left)
            source_register = self.pinned_register[right.name]
            if len(source_register) < len(self.target.count_register):
                source_register = self.target.low_word(source_register)
                # Use movzx to zero-extend the 16-bit source into count_register.
                self.emit(f"        movzx {self.target.count_register}, {source_register}")
            elif source_register != self.target.count_register:
                self.emit(f"        mov {self.target.count_register}, {source_register}")
        elif isinstance(right, Var) and self._is_memory_scalar(right.name) and not self._is_byte_scalar(right.name):
            self.generate_expression(left)
            self.emit(f"        mov {self.target.count_register}, [{self._local_address(right.name)}]")
        else:
            self.generate_expression(left)
            self.emit(f"        push {self.target.acc}")
            self.generate_expression(right)
            self.emit(f"        mov {self.target.count_register}, {self.target.acc}")
            self.emit(f"        pop {self.target.acc}")

    def emit_byte_load_zx(self, mem_operand: str, /) -> None:
        """Load a byte from *mem_operand* into the accumulator, zero-extended.

        On 16-bit real mode, emits ``mov al, <mem> / xor ah, ah`` — the
        cheap 3-byte + 2-byte sequence the 8086 / early peepholes
        (``peephole_dead_ah``, ``peephole_redundant_byte_mask``) expect
        and can fuse through.  On 32-bit flat protected mode, emits ``movzx eax,
        byte <mem>`` so bits 16-31 of EAX stay clean — the old
        ``mov al / xor ah, ah`` pair would leave EAX's upper word
        whatever the caller last wrote to it, and a downstream
        ``test eax, eax`` would read stale bits.

        ``mem_operand`` is the bracket-enclosed memory reference
        (``[addr]`` / ``[bp-4]`` / ``[si+12]`` / …) — callers don't
        include the ``byte`` size prefix; this helper adds it in the
        32-bit branch.
        """
        if self.target.int_size == 2:
            self.emit(f"        mov al, {mem_operand}")
            self.emit("        xor ah, ah")
        else:
            self.emit(f"        movzx {self.target.acc}, byte {mem_operand}")

    def emit_comparison(self, left: Node, right: Node, /) -> None:
        """Generate a comparison, leaving flags set for a conditional jump.

        Optimizes comparisons against integer constants by using
        ``cmp ax, imm`` directly, and ``test ax, ax`` for zero.  Pinned
        register variables compare against constants in place, skipping
        the load into AX.  ``NULL`` and other named constants are
        treated as constant immediates.
        """
        literal = None
        is_zero = False
        if isinstance(right, Int):
            literal = str(right.value)
            is_zero = right.value == 0
        elif isinstance(right, Var) and right.name in self.NAMED_CONSTANTS:
            literal = right.name
            is_zero = right.name == "NULL"
        if literal is not None:
            self._emit_comparison_against_constant(left=left, literal=literal, is_zero=is_zero)
        else:
            self._emit_comparison_general(left=left, right=right)

    def emit_condition(self, *, condition: Node, context: str) -> tuple[str, bool]:
        """Validate a condition, emit a comparison, and return ``(operator, unsigned)``.

        ``unsigned`` is True when at least one operand is an unsigned
        type (``unsigned char`` / ``unsigned short`` / ``unsigned int`` / ``unsigned
        long``, plus the corresponding pointers).  Callers pick the
        signed or unsigned jump table accordingly.

        ``carry_return`` call conditions — ``if (foo())`` / ``while
        (foo())`` / ``if (foo() == 0)`` where ``foo`` is declared with
        ``__attribute__((carry_return))`` — skip the ``cmp`` path
        entirely: the ``call`` itself leaves CF holding the truth
        value, and the caller dispatches through ``jc`` / ``jnc`` via
        the synthetic ``"carry"`` / ``"not_carry"`` operators.
        ``parse_condition`` wraps a top-level bare expression as ``expr
        != 0``, and inside ``&&`` / ``||`` this routine does the same
        wrapping for leaf operands (so ``while (foo() || x == 0)`` and
        ``if (foo() && bar())`` desugar the bare-call legs into the
        same ``BinaryOperation(left=Call, operation='!=', right=Int(value=0))`` shape the top-level form
        uses).
        """
        if not isinstance(condition, BinaryOperation) or condition.operation not in JUMP_WHEN_FALSE:
            # Wrap a bare expression (Call / Var / Index / ...) as ``expr != 0``
            # so the rest of the routine sees the same shape the top-level
            # parser already emits.  Reaches here from && / || recursion
            # where leaf operands haven't been run through parse_condition.
            condition = BinaryOperation(left=condition, line=condition.line, operation="!=", right=Int(line=condition.line, value=0))
        if (
            condition.operation in ("!=", "==")
            and isinstance(condition.right, Int)
            and condition.right.value == 0
            and isinstance(condition.left, Call)
            and condition.left.name in self.carry_return_functions
        ):
            self.generate_call(condition.left, discard_return=True)
            return ("carry" if condition.operation == "!=" else "not_carry", False)
        # Skip type validation for IR-generated conditions: the IR
        # builder rebuilds operands as bare ``Int`` (``_ir_value_to_ast``
        # does not preserve ``Char``), which would mis-flag legitimate
        # ``char_var == 'A'`` shapes here.  The AST-level walk in
        # :meth:`validate_body_comparisons` already covered the body
        # before IR construction, so this skip is safe.
        if context != "ir":
            self.validate_comparison_types(condition.left, condition.right)
        self.emit_comparison(condition.left, condition.right)
        return condition.operation, self._is_unsigned_comparison(condition.left, condition.right)

    def emit_condition_false_jump(self, *, condition: Node, fail_label: str, context: str) -> None:
        """Emit a condition that jumps to ``fail_label`` when false.

        For ``&&``, short-circuits by recursing on each operand with
        the same fail label — any false leg jumps directly to the
        failure target.  For ``||``, jumps past the right leg as soon
        as the left leg is true, otherwise re-enters the false-jump on
        the right leg.

        When the ``&&`` chain contains adjacent byte-index ``==``
        comparisons on the same base, they are fused into word-sized
        comparisons (see :meth:`_try_fuse_word_conditions`).
        """
        if isinstance(condition, LogicalAnd):
            leaves = self._flatten_and(condition)
            self._try_fuse_word_conditions(leaves, context=context, fail_label=fail_label)
            return
        if isinstance(condition, LogicalOr):
            pass_label = f".lor_{self.new_label()}"
            self.emit_condition_true_jump(condition=condition.left, context=context, success_label=pass_label)
            self.emit_condition_false_jump(condition=condition.right, context=context, fail_label=fail_label)
            self.emit(f"{pass_label}:")
            return
        operator, unsigned = self.emit_condition(condition=condition, context=context)
        table = JUMP_WHEN_FALSE_UNSIGNED if unsigned else JUMP_WHEN_FALSE
        self.emit(f"        {table[operator]} {fail_label}")

    def emit_condition_true_jump(self, *, condition: Node, success_label: str, context: str) -> None:
        """Emit a condition that jumps to ``success_label`` when true.

        Dual of :meth:`emit_condition_false_jump`; used for the ``||``
        short-circuit so that a truthy left leg can skip the right.
        """
        if isinstance(condition, LogicalOr):
            self.emit_condition_true_jump(condition=condition.left, context=context, success_label=success_label)
            self.emit_condition_true_jump(condition=condition.right, context=context, success_label=success_label)
            return
        if isinstance(condition, LogicalAnd):
            skip_label = f".land_{self.new_label()}"
            self.emit_condition_false_jump(condition=condition.left, context=context, fail_label=skip_label)
            self.emit_condition_true_jump(condition=condition.right, context=context, success_label=success_label)
            self.emit(f"{skip_label}:")
            return
        operator, unsigned = self.emit_condition(condition=condition, context=context)
        table = JUMP_WHEN_TRUE_UNSIGNED if unsigned else JUMP_WHEN_TRUE
        self.emit(f"        {table[operator]} {success_label}")

    def emit_error_syscall_tail(
        self,
        *,
        fuse_die: tuple[str, int] | None,
        fuse_exit: bool,
        preserve_al: bool,
    ) -> None:
        """Emit the shared tail for an error-returning syscall.

        - ``fuse_die=(label, length)`` → preload SI/CX and
          ``jc FUNCTION_DIE`` so the if-error-die block disappears.
        - ``fuse_exit`` → ``jnc FUNCTION_EXIT`` (for
          ``if (!err) return;`` fusion).
        - Otherwise, convert the carry flag into a 0-or-error integer
          in AX.  ``preserve_al`` keeps AL on the error path (syscalls
          that return an ERROR_* code); False hard-codes 1.
        """
        if fuse_die is not None:
            die_label, die_length = fuse_die
            self.emit(f"        mov {self.target.si_register}, {die_label}")
            self.emit(f"        mov {self.target.count_register}, {die_length}")
            self._emit_libbboeos_jcc("jc", "FUNCTION_DIE")
            return
        if fuse_exit:
            self._emit_libbboeos_jcc("jnc", "FUNCTION_EXIT")
            return
        label_index = self.new_label()
        self.emit(f"        jnc .ok_{label_index}")
        if preserve_al:
            self.emit_accumulator_zx_from_al()
        else:
            self.emit(f"        mov {self.target.acc}, 1")
        self.emit(f"        jmp .done_{label_index}")
        self.emit(f".ok_{label_index}:")
        self.emit(f"        xor {self.target.acc}, {self.target.acc}")
        self.emit(f".done_{label_index}:")

    def emit_register_from_argument(self, *, argument: Node, register: str) -> None:
        """Load an argument into a specific 16-bit register.

        Handles pinned variables, memory locals, named constants,
        integer literals, and general expressions (evaluated via AX).

        Keeps :attr:`ax_local` consistent: any path that writes AX
        (either directly because *register* is the accumulator, or
        indirectly via the byte-scalar ``mov al / xor ah, ah``
        sequence) updates the tracking so a subsequent
        ``emit_register_from_argument`` with the previously-tracked
        var name can't emit a stale ``mov <reg>, ax`` shortcut.
        """
        ax_written = register == self.target.acc
        # Default: if we end up writing AX for a load that does not
        # leave a named variable in AX (int / constant / address /
        # expression), clear the tracking.  Paths that do leave a
        # named var in AX (pinned / aliased global / memory scalar)
        # override this below.
        new_ax_local: str | None = self.ax_local
        new_ax_is_byte: bool = self.ax_is_byte
        if isinstance(argument, Var) and argument.name in self.pinned_register:
            source = self.pinned_register[argument.name]
            if len(register) < len(source):
                # Loading a 32-bit pinned reg into a narrower (16-bit) target:
                # use the low-word name.
                source = self.target.low_word(source)
                if source != register:
                    self.emit(f"        mov {register}, {source}")
            elif len(source) < len(register):
                # Loading a 16-bit pinned reg into a wider (32-bit) target:
                # zero-extend.
                self.emit(f"        movzx {register}, {source}")
            elif source != register:
                self.emit(f"        mov {register}, {source}")
            if ax_written and source != self.target.acc:
                new_ax_local = argument.name
                new_ax_is_byte = False
        elif isinstance(argument, Var) and argument.name in self.register_aliased_globals:
            source = self.register_aliased_globals[argument.name]
            if len(register) < len(source):
                source = self.target.low_word(source)
            if source != register:
                self.emit(f"        mov {register}, {source}")
            if ax_written and source != self.target.acc:
                new_ax_local = argument.name
                new_ax_is_byte = False
        elif isinstance(argument, Var) and argument.name == self.ax_local:
            self._emit_mov_from_acc(register)
            # AX unchanged in both branches: shortcut leaves tracking intact.
        elif isinstance(argument, Var) and (argument.name in self.global_arrays or argument.name in self.local_stack_arrays):
            # Arrays live in memory but get their base address loaded,
            # not their contents — dispatch through _try_direct_load
            # before _is_memory_scalar (which would otherwise match any
            # Var whose name is in ``self.locals``).
            self._try_direct_load(argument=argument, register=register)
            if ax_written:
                new_ax_local = None
                new_ax_is_byte = False
        elif isinstance(argument, Var) and self._is_memory_scalar(argument.name):
            if self._is_byte_scalar(argument.name):
                # Byte-scalar source into a word register: load via AL
                # and zero-extend so the high byte is clean, then move
                # into the target (or stop if target is already acc).
                # AX gets clobbered even when the final target is not
                # AX, so we must refresh the tracking either way.
                self.emit_byte_load_zx(f"[{self._local_address(argument.name)}]")
                self._emit_mov_from_acc(register)
                new_ax_local = argument.name
                new_ax_is_byte = True
            else:
                self.emit(f"        mov {register}, [{self._local_address(argument.name)}]")
                if ax_written:
                    new_ax_local = argument.name
                    new_ax_is_byte = False
        elif self._try_direct_load(argument=argument, register=register):
            if ax_written:
                new_ax_local = None
                new_ax_is_byte = False
        else:
            self.generate_expression(argument)
            self._emit_mov_from_acc(register)
            # generate_expression leaves its own tracking; do not
            # override new_ax_local here.
            new_ax_local = self.ax_local
            new_ax_is_byte = self.ax_is_byte
        self.ax_local = new_ax_local
        self.ax_is_byte = new_ax_is_byte

    def emit_si_from_argument(self, argument: Node, /) -> None:
        """Load a string or expression argument into SI (or ESI in 32-bit)."""
        si = self.target.si_register
        if self._try_direct_load(argument=argument, register=si):
            return
        self.generate_expression(argument)
        self.emit(f"        mov {si}, {self.target.acc}")

    def emit_store_local(self, *, expression: Node, name: str) -> None:
        """Generate an expression and store the result in a local variable.

        When ``name`` is pinned to a register, the value is written to
        that register instead of the memory frame.  Constant
        initializers — integers, string literals, or named kernel
        constants — are moved directly into the pinned register
        without going through AX, so the caller's AX tracking (e.g.
        ``arg`` left by the argv startup) survives the store.
        """
        if name in self.global_arrays:
            message = f"cannot assign to array '{name}'"
            raise CompileError(message)
        # ``dest = (cond) ? dest : other`` (and the mirror) is the
        # ternary shape produced by ``MAX(dest, other)`` / ``MIN(dest,
        # other)``.  Recognising it here lets us elide the no-op
        # ``dest = dest`` branch and emit the same tight cmp + Jcc +
        # ``mov dest, other`` sequence the hand-rolled ``if (...)``
        # pattern produces — without it the ternary lowering would
        # round-trip through AX and grow the code.
        if isinstance(expression, Conditional) and self._try_emit_guarded_update(expression=expression, name=name):
            return
        # Under --bits 32 the parser folds ``unsigned long`` into
        # ``unsigned int``; the virtual-long optimisation pattern stays
        # eligible (discover_virtual_long_locals adds the unsigned-int
        # name), so route assignments to those locals through the long
        # path too — the value is produced by datetime() in EAX and
        # consumed directly by print_datetime() with no frame spill.
        if self.variable_types.get(name) == "unsigned long" or name in self.virtual_long_locals:
            self._emit_long_store(expression=expression, name=name)
            return
        direct_register: str | None = None
        if name in self.pinned_register:
            direct_register = self.pinned_register[name]
        elif name in self.register_aliased_globals:
            direct_register = self.register_aliased_globals[name]
        if direct_register is not None and self._try_direct_load(argument=expression, register=direct_register, optimize_zero=True):
            return
        # Tell nested expression handling that the pinned destination
        # register (if any) will be overwritten at end of this store, so
        # they don't need to push/pop it to preserve the old value.
        previous_store_target = self.store_target_register
        self.store_target_register = direct_register
        self.generate_expression(expression)
        self.store_target_register = previous_store_target
        self._store_accumulator_to_local(name, direct_register=direct_register)

    def resolve_address(self, place: Place, /) -> MemoryOperand:
        """Resolve *place* to a MemoryOperand, emitting side-effect code as needed.

        Follows the GCC get_inner_reference / LLVM EmitLValue model: deref-free
        segments fold into one operand; a DereferencePlace breaks the chain into
        a fresh register base (added in a later task).  Member offsets and
        constant subscripts sum into displacement; dynamic subscripts are scaled
        and summed into a single index register.
        """
        match place:
            case DereferencePlace():
                return self._resolve_dereference(place)
            case MemberPlace(base=base, member_name=member_name):
                if self._match_struct_array_member(place) is not None:
                    # Struct-array member (arr[i].field): the layout helper
                    # consumes the SubscriptPlace base directly; fold its offset
                    # and sizes onto the recursively-resolved base operand.
                    operand = self.resolve_address(base)
                    field_offset, field_size, element_size = self._member_layout_on(base, member_name, line=place.line)
                    operand.displacement += field_offset
                    operand.field_size = field_size
                    operand.element_size = element_size
                    # A bare array-typed member (arr[i].member where member is an
                    # array) decays to its address on load (field_size !=
                    # element_size), matching the legacy ``lea`` terminal.
                    operand.decay_to_address = field_size != element_size
                    return operand
                # Dot / arrow / chained / cast scalar (or array / struct-value)
                # member: materialize the struct base byte-exactly through the
                # shared member-base resolver, then size the terminal from the
                # field info.  A bitfield member rides on operand.bitfield so the
                # load / store terminal emits the mask/shift sequence.
                const_base, base_is_register, info = self._resolve_member_place_info(place)
                is_struct_value = info.type_name.startswith("struct ") and not info.type_name.endswith("*")
                operand = MemoryOperand(
                    base=const_base,
                    base_kind="register" if base_is_register else self._member_base_kind(place),
                    decay_to_address=info.field_size != info.element_size or is_struct_value,
                    displacement=info.byte_offset,
                    element_size=info.element_size,
                    field_size=info.field_size,
                )
                if info.bit_width is not None:
                    operand.bitfield = info
                return operand
            case SubscriptPlace(base=base, index=index):
                # Multidim array member (``g.field[i][j]...`` /
                # ``p->field[i][j]...``): resolve the row-major element address
                # rooted at the struct base + field offset.
                if (multidim_member := self._match_multidim_member_chain(place)) is not None:
                    object_name, arrow, field_name, member_indices = multidim_member
                    return self._emit_multidim_member_address(
                        object_name, arrow=arrow, field_name=field_name, indices=member_indices, line=place.line
                    )
                # Uniform multi-subscript chain (``name[i][j]...``): the
                # pointer-to-array and contiguous-multidim shapes resolve the
                # row-major element address through their Horner helpers rather
                # than the per-level recursion (which would re-derive a single
                # stride per step).
                if (chain := self._uniform_subscript_chain(place)) is not None:
                    base_name, indices = chain
                    if base_name in self.pointer_array_types:
                        return self._emit_pointer_to_array_address(base_name, indices, line=place.line)
                    if self._is_multidim_array(base_name):
                        return self._emit_multidim_subscript_address(base_name, indices, line=place.line)
                operand = self.resolve_address(base)
                if operand.base_kind == "register":
                    # The base segment ended in a dereference (e.g. the
                    # ``name[outer]`` element pointer of an array of pointers):
                    # the pointer is live in the accumulator, so move it to the
                    # ESI index base and fold the outer subscript onto ESI.
                    # ``_accumulate_subscript``'s BX discipline would clobber the
                    # accumulator base while evaluating a dynamic index; the
                    # register-base folder preserves it instead.
                    self._accumulate_subscript_on_register(operand, index=index)
                else:
                    # A bare VariablePlace seed carries no element size; for the
                    # struct-array subscript (arr[i] in arr[i].member) the stride
                    # is the struct element size, supplied by the arithmetic
                    # element-size resolver.  Member / dereference seeds already
                    # carry their element size on the operand.
                    element_size = operand.element_size or operand.field_size
                    if element_size == 0 and isinstance(base, VariablePlace):
                        element_size = self._arithmetic_element_size(base.name)
                    self._accumulate_subscript(operand, index=index, element_size=element_size)
                # Subscripting reads a scalar element: the terminal is sized at
                # the element width and never decays to an address (an
                # array-typed member's decay flag, set when its base resolved,
                # is cleared here now that one element is selected).
                operand.field_size = operand.element_size
                operand.decay_to_address = False
                return operand
            case VariablePlace(name=name):
                base_kind, base = self._variable_base(name, line=place.line)
                return MemoryOperand(base_kind=base_kind, base=base)
            case _:
                message = "unsupported Place shape in resolve_address"
                raise CompileError(message, line=place.line)

    def scan_locals(self, statements: list[Node], /, *, top_level: bool = True) -> None:
        """Recursively find variable declarations.

        Plain ``int`` declarations are auto-pinned to a CPU register
        (from :data:`REGISTER_POOL`) when the declaration was chosen
        by :meth:`_select_auto_pin_candidates` and a slot is still
        available.  Slots are assigned in declaration order among
        selected candidates.  Call initializers stay in memory so
        they can participate in error-fusion optimizations without
        clobbering a pin.
        """
        for index, statement in enumerate(statements):
            if isinstance(statement, (VarDecl, ArrayDecl)) and (
                statement.name in self.global_scalars or statement.name in self.global_arrays
            ):
                message = f"local '{statement.name}' shadows a file-scope global"
                raise CompileError(message, line=statement.line)
            if isinstance(statement, VarDecl) and statement.pointer_array_dimensions is not None:
                # ``int (*p)[3]`` — pointer-to-array.  A single pointer-sized
                # slot holds the address; subscript / sizeof go through the
                # structured pointer_array_types table (Task 2).
                self._register_pointer_to_array(
                    statement.name,
                    element_type_name=statement.type_name,
                    line=statement.line,
                    pointee_dimensions=statement.pointer_array_dimensions,
                )
                self.allocate_local(statement.name, size=self.target.int_size)
                continue
            if isinstance(statement, VarDecl):
                self.variable_types[statement.name] = statement.type_name
                if statement.function_pointer_params:
                    in_regs: dict[int, str] = {}
                    for param_index, param in enumerate(statement.function_pointer_params):
                        if param.in_register is not None:
                            in_regs[param_index] = param.in_register
                    if in_regs:
                        self.function_pointer_in_registers[statement.name] = in_regs
                if statement.pinned_register is not None:
                    # Explicit pin via __attribute__((pinned_register(...))).
                    # Storage lives in the register; no stack slot allocated,
                    # so the loop continues past the slot-allocation tail.
                    self.pinned_register[statement.name] = statement.pinned_register
                    continue
                if top_level and self._is_constant_alias(body=statements, statement=statement):
                    alias = self._constant_expression(statement.init)
                    self.constant_aliases[statement.name] = alias
                    for name in self._collect_constant_references(statement.init):
                        include = self.NAMED_CONSTANT_INCLUDES.get(name)
                        if include is not None:
                            self.required_includes.add(include)
                    continue
                if (
                    statement.type_name not in ("double", "unsigned long", "function_pointer")
                    and statement.name in self.auto_pin_candidates
                ):
                    following = statements[index + 1] if index + 1 < len(statements) else None
                    if self.can_auto_pin(following_statement=following, statement=statement):
                        self.pinned_register[statement.name] = self.auto_pin_candidates[statement.name]
                        continue
                if statement.name in self.virtual_long_locals:
                    continue
                size = self._type_size(statement.type_name)
                # Byte-typed scalar body locals get a 1-byte slot; track
                # them so load / store / compare paths use the byte-wide
                # codegen shared with byte-scalar globals.  Parameters
                # arrive as words on the stack and keep their 2-byte
                # slot, so the byte-local split only fires in
                # :meth:`scan_locals`.
                if statement.type_name in self.BYTE_TYPES:
                    size = 1
                    self.byte_scalar_locals.add(statement.name)
                self.allocate_local(statement.name, size=size)
                # Skip the init store for top-level main locals with an
                # Int(0) initializer: the ``dw 0`` (or ``db 0`` for
                # byte locals) declaration already zeros the cell, and
                # main re-runs from a fresh image each exec.
                if top_level and self.elide_frame and isinstance(statement.init, Int) and statement.init.value == 0 and size in (1, 2):
                    self.zero_init_skippable.add(statement.name)
            elif isinstance(statement, ArrayDecl):
                if statement.dimensions is not None:
                    self._register_array_type(
                        statement.name,
                        dimensions=statement.dimensions,
                        line=statement.line,
                        type_name=statement.type_name,
                    )
                    array_type = self.array_types[statement.name]
                    byte_count = array_type.sizeof(pointer_width=self.target.int_size, scalar_width=self._type_size)
                    self.variable_types[statement.name] = statement.type_name
                    self.variable_arrays.add(statement.name)
                    self.allocate_local(statement.name, size=byte_count)
                    self.local_stack_arrays[statement.name] = byte_count
                    continue
                self.variable_types[statement.name] = statement.type_name
                self.variable_arrays.add(statement.name)
                stride = self._type_size(statement.type_name)
                byte_count = self._eval_local_array_size(statement.size, stride=stride) if statement.size is not None else None
                if byte_count is not None:
                    self.allocate_local(statement.name, size=byte_count)
                    self.local_stack_arrays[statement.name] = byte_count
                else:
                    self.allocate_local(statement.name)
            elif isinstance(statement, If):
                self.scan_locals(statement.body, top_level=False)
                if statement.else_body is not None:
                    self.scan_locals(statement.else_body, top_level=False)
            elif isinstance(statement, (DoWhile, While)):
                self.scan_locals(statement.body, top_level=False)
            elif isinstance(statement, For):
                self.scan_locals(statement.init, top_level=False)
                self.scan_locals(statement.body, top_level=False)
            elif isinstance(statement, Switch):
                for case in statement.cases:
                    self.scan_locals(case.body, top_level=False)
            elif isinstance(statement, Compound):
                self.scan_locals(statement.body, top_level=False)

    def validate_body_comparisons(self, statements: list[Node], /) -> None:
        """Walk a function body, validating every comparison's operand types.

        Catches the char-vs-int and pointer-vs-non-pointer shapes that
        the codegen-time check in :meth:`emit_condition` skips when its
        ``context`` is ``"ir"``.  ``_ir_value_to_ast`` reconstructs IR
        ``Value``s as bare :class:`Int` even when the original AST
        operand was a :class:`Char` literal, so type info is lost
        before codegen sees the condition; running the check up here
        on the original AST nodes preserves it.

        Call after parameters and ``scan_locals`` have populated
        ``self.variable_types`` for the current function — the
        :meth:`_type_of_operand` lookup uses that map to classify
        :class:`Var` references.
        """
        for statement in statements:
            self._validate_node_comparisons(statement)

    def validate_comparison_types(self, left: Node, right: Node, /) -> None:
        r"""Ensure ``==``/``!=``/``<``/``<=``/``>``/``>=`` operand types match.

        Pointers may only be compared to other pointers or ``NULL``;
        ``NULL`` may only appear opposite a pointer; ``char`` values
        must be compared against other ``char`` values or character
        literals (so ``c != 0`` and ``c < 32`` are rejected — use
        ``c != '\0'`` and ``c < ' '``).  Comparing a pointer to a
        non-``NULL`` integer (``if (p == 0)``) is a common C bug, so
        the compiler requires the explicit ``NULL`` spelling.  A
        ``Char`` literal may appear opposite a ``unsigned char`` / ``int``
        operand — it's just a small integer, and forcing hex spelling
        there hurts readability (``byte >= '\xC0'`` stays legal).

        Under ``--permissive`` (``self.permissive``) every check here is
        skipped: third-party C (kilo, lua, Doom) treats integer ``0`` as a
        null-pointer constant, writes ``if (p)`` and ``c != 0`` freely, and
        we accept those forms verbatim rather than rewrite upstream source.
        The strictness is bboeos house style for first-party code only.
        """
        if self.permissive:
            return
        left_type = self._type_of_operand(left)
        right_type = self._type_of_operand(right)
        line = left.line or right.line
        if left_type == "pointer" and right_type not in ("pointer", "null"):
            message = f"pointer compared to non-pointer: {left} vs {right}"
            raise CompileError(message, line=line)
        if right_type == "pointer" and left_type not in ("pointer", "null"):
            message = f"pointer compared to non-pointer: {left} vs {right}"
            raise CompileError(message, line=line)
        if left_type == "null" and right_type not in ("pointer", "null"):
            message = f"NULL compared to non-pointer: {left} vs {right}"
            raise CompileError(message, line=line)
        if right_type == "null" and left_type not in ("pointer", "null"):
            message = f"NULL compared to non-pointer: {left} vs {right}"
            raise CompileError(message, line=line)
        if left_type == "char" and right_type != "char" and not isinstance(left, Char):
            message = f"char compared to non-char: {left} vs {right}"
            raise CompileError(message, line=line)
        if right_type == "char" and left_type != "char" and not isinstance(right, Char):
            message = f"char compared to non-char: {left} vs {right}"
            raise CompileError(message, line=line)
