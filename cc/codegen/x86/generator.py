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

import re
from dataclasses import fields
from typing import ClassVar, NamedTuple

from cc import ir
from cc.ast_nodes import (
    AddressOf,
    ArrayDecl,
    Assign,
    BinaryOperation,
    Call,
    Cast,
    Char,
    Compound,
    Conditional,
    DerefAssign,
    DoubleIndex,
    DoWhile,
    EnumDecl,
    Function,
    If,
    Index,
    IndexAssign,
    IndexMemberAccess,
    IndexMemberAssign,
    IndexMemberIndex,
    IndexMemberIndexAssign,
    InlineAsm,
    Int,
    LogicalAnd,
    LogicalOr,
    MemberAccess,
    MemberAddressOf,
    MemberAssign,
    MemberIndex,
    Node,
    Param,
    String,
    StructDecl,
    StructInitializer,
    Switch,
    Var,
    VarDecl,
    While,
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
from cc.errors import CompileError
from cc.target import CodegenTarget, X86CodegenTarget16, X86CodegenTarget32
from cc.tokens import COMPARISON_OPERATIONS
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
        *,
        bits: int = 16,
        constant_values: dict[str, int] | None = None,
        defines: dict[str, str] | None = None,
        object_mode: bool = False,
        target_mode: str = "user",
    ) -> None:
        """Initialize code generator state.

        ``bits`` selects the target: 16 → ``X86CodegenTarget16``,
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
        self.libbboeos_extern_declarations: set[str] = set()
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
        self.out_register_params: dict[str, dict[int, str]] = {}
        self.param_in_register: dict[str, str] = {}
        self.pinned_register: dict[str, str] = {}
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
            self.safe_pin_registers = self.compute_safe_pin_registers(function.body)
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
        - Byte types (``char``, ``uint8_t``) always return 1 so byte-string
          arithmetic is never scaled.
        - Unknown or non-pointer scalars: return 1 (no scaling).
        """
        type_name = self.variable_types.get(var_name, "")
        if var_name in self.variable_arrays:
            # Array: element type is the stored type_name directly.
            if type_name in ("char", "uint8_t") or type_name in self.BYTE_TYPES:
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
            if base in ("char", "uint8_t") or base in self.BYTE_TYPES:
                return 1
            if base.startswith("struct "):
                tag = base[7:]
                if tag not in self.struct_sizes:
                    message = f"unknown struct '{tag}'"
                    raise CompileError(message)
                return self.struct_sizes[tag]
            return self.target.type_size(base)
        return 1

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

    def _collect_function_pointer_vars(self, body: list[Node], /) -> set[str]:
        """Return every name that names a function_pointer (locals + file-scope globals).

        Shared by :meth:`compute_safe_pin_registers` (per-call clobber
        tally) and :meth:`_select_auto_pin_candidates` (per-candidate
        pre-store clobber tally) so they classify indirect calls the
        same way.
        """
        function_pointer_vars: set[str] = set()

        def visit(statements: list[Node]) -> None:
            for statement in statements:
                if isinstance(statement, VarDecl) and statement.type_name == "function_pointer":
                    function_pointer_vars.add(statement.name)
                elif isinstance(statement, If):
                    visit(statement.body)
                    if statement.else_body is not None:
                        visit(statement.else_body)
                elif isinstance(statement, (Compound, DoWhile, While)):
                    visit(statement.body)
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
        ``UnaryOperation``, ``AddressOf``, ``Index``, etc. — so it can
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
        pinned_locals: dict[str, str] = dict(self.pinned_register)
        if not pinned_locals:
            return {}
        initial: set[str] = set(self._prologue_initialized_pinned_registers())

        def store_targets(instruction: object) -> list[str]:
            """Return every local name written by *instruction*.

            Most shapes write at most one local; ``ir.Call`` is the
            exception — beyond its (optional) ``destination``, every
            ``out_register`` arg captures into the named local AFTER
            the call returns, so all of them count as stores for the
            purposes of "is the pin live around the next call".
            """
            if isinstance(instruction, (ir.BinaryOperation, ir.Copy, ir.Index)):
                return [instruction.destination]
            if isinstance(instruction, ir.Block):
                # Block-wrapped AST escape hatch.  A VarDecl with
                # initialiser is a store to its name; ditto an
                # ``unsigned long`` Assign that the IR builder routes
                # through Block.  Pinned-to-register locals can't be
                # ``unsigned long`` (they wouldn't fit a single register),
                # so only the VarDecl case can hit a pinned target —
                # but we still extract Assign / MemberAssign destinations
                # defensively in case future IR shapes wrap them.
                node = instruction.node
                if isinstance(node, Assign):
                    return [node.name]
                if isinstance(node, VarDecl) and node.init is not None:
                    return [node.name]
                # MemberAssign / IndexAssign / inline asm write through
                # pointers or are opaque — they don't store to a single
                # named local register.  Skip.
                return []
            if isinstance(instruction, ir.Call):
                stores: list[str] = []
                if instruction.destination is not None:
                    stores.append(instruction.destination)
                out_regs = self.out_register_params.get(instruction.name, {})
                for index, arg in enumerate(instruction.args):
                    if index in out_regs and isinstance(arg, AddressOf):
                        stores.append(arg.var.name)
                return stores
            if isinstance(instruction, ir.CarryBranch):
                # ``carry_return`` callees can also have ``out_register``
                # captures — match the ir.Call handling so the pin
                # tracker sees their writes too.
                call_ast = instruction.call_ast
                stores = []
                out_regs = self.out_register_params.get(call_ast.name, {})
                for index, arg in enumerate(call_ast.args):
                    if index in out_regs and isinstance(arg, AddressOf):
                        stores.append(arg.var.name)
                return stores
            if isinstance(instruction, ir.IndexAssign):
                # IndexAssign writes through a base pointer, not to the
                # named base itself — leaves the base's register
                # contents unchanged.  Not a store to the pin.
                return []
            return []

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
                for target_name in store_targets(ir_body[k]):
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
            # Block-wrapped statements are not analysed; ``ir.Block``
            # lowering leaves :attr:`_current_call_pinned_initialized`
            # at ``None`` so any nested calls fall back to the
            # conservative full save-set.
            if isinstance(instruction, (ir.Call, ir.CarryBranch)):
                result[id(instruction)] = frozenset(defined)
            for target_name in store_targets(instruction):
                if target_name in pinned_locals:
                    defined.add(pinned_locals[target_name])
        return result

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
                self.emit(f"_g_{name} equ _program_end{suffix}")
                offset += int(size_expr)
        else:
            # Non-literal sizes: use EQU chain and define _bss_total_size.
            prev_end = "_program_end"
            for name, size_expr in self.bss_vars:
                self.emit(f"_g_{name} equ {prev_end}")
                prev_end = f"_g_{name} + {size_expr}"
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
                self.emit(f"_g_{name}: resb {size_expression}")
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
            inverse = {"jc": "jnc", "jnc": "jc"}[condition]
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
        arrays, 2 for ``uint16_t``, 4 for full-int / pointer-target on
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
        int_directive = "dd" if self.target.int_size == 4 else "dw"
        # In object mode the initialized-globals chunk belongs in
        # ``section .data`` so the linker can place writable data
        # independently of code.  The switch + comment are emitted
        # once, lazily, on the first initialized cell we actually
        # write below — purely zero-init globals end up in
        # ``self.bss_vars`` and never need ``.data``.  ``data_header_emitted``
        # tracks whether we've written the header yet within this call.
        # In flat mode the header is emitted eagerly up front, matching
        # the long-standing layout.
        data_header_emitted = False

        def _emit_data_header() -> None:
            nonlocal data_header_emitted
            if data_header_emitted:
                return
            if self.object_mode:
                self.emit()
                self.emit("section .data")
            self.emit(";; --- global data ---")
            data_header_emitted = True

        if not self.object_mode:
            _emit_data_header()
        for name in sorted(self.global_scalars):
            declaration = self.global_scalars[name]
            if name in self.register_aliased_globals:
                # Storage lives in the aliased CPU register, not memory,
                # so no ``_g_<name>`` label is emitted.
                continue
            if name in self.asm_symbol_globals:
                # Storage lives in an existing asm symbol, not here,
                # so no ``_g_<name>`` label is emitted.
                continue
            if name in self.extern_globals:
                # Storage lives in another translation unit; references
                # still resolve to ``_g_<name>`` (matching the symbol the
                # owning .c file emits).
                continue
            if declaration.init is None:
                stride = 1 if self._is_byte_scalar_global(name) else self.target.int_size
                self.bss_vars.append((name, str(stride)))
            else:
                init_expression = self._constant_expression(declaration.init)
                directive = "db" if self._is_byte_scalar_global(name) else int_directive
                _emit_data_header()
                self.emit(f"_g_{name}: {directive} {init_expression}")
        for name in sorted(self.global_arrays):
            declaration = self.global_arrays[name]
            if name in self.extern_globals:
                # Storage lives in another translation unit; references
                # to the bare ``_g_<name>`` label still resolve.
                continue
            is_byte = declaration.type_name in self.BYTE_TYPES
            is_struct = declaration.type_name.startswith("struct ")
            # Stride is sizeof(element) for every shape: structs sum
            # field widths, ``char`` / ``uint8_t`` resolve to 1,
            # ``uint16_t`` to 2, pointer / ``int`` / ``uint32_t`` to
            # ``int_size``.  Unifies what used to be a binary
            # byte-vs-int_size switch that silently miscompiled
            # ``uint16_t`` globals.
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
                _emit_data_header()
                self.emit(f"_g_{name}: {lines[0]}")
                for line in lines[1:]:
                    self.emit(f"        {line}")
            elif declaration.init is not None:
                # Match the data-cell width to the element width:
                # ``db`` for byte, ``dw`` for halfword (``uint16_t``),
                # ``dd`` / ``dw`` for full-int (``int_directive``).
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
                _emit_data_header()
                self.emit(f"_g_{name}: {directive} {', '.join(rendered)}")
            else:
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
            self.emit(f"_g_{name}: resb {size_expression}")
        self.emit("section .text")

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
        message = f"unknown type '{type_name}'"
        raise CompileError(message)

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

    def generate_member_access(self, expression: MemberAccess, /) -> None:
        """Generate code for ``ptr->field`` or ``obj.field`` as an rvalue.

        The pointer form (``ptr->field``) loads the base via the pointer
        variable.  The dot form (``obj.field``) is supported only for
        file-scope struct globals where the address of the struct is a
        compile-time symbol (``[_g_obj+offset]``); for those, no base
        register is needed.

        When ``expression.base_expr`` is set (the ``(struct T *)expr``
        cast form), the base pointer is materialised by evaluating that
        expression into BX/EBX; the field load then proceeds the same
        way as the named-variable form.
        """
        if expression.base_expr is not None:
            self._generate_member_access_via_expr(expression)
            return
        object_name = expression.object_name
        struct_type = self.variable_types.get(object_name)
        if struct_type is None:
            message = f"undefined variable '{object_name}'"
            raise CompileError(message, line=expression.line)
        # Dot-access path: ``obj.field`` on a file-scope struct global.
        # The base address resolves to the symbol literal ``_g_<obj>``
        # (or the asm_name target / extern-resolved name); the field load
        # just adds the field offset.
        if not expression.arrow:
            if struct_type.endswith("*") or not struct_type.startswith("struct "):
                message = f"'.' requires a struct value, got type '{struct_type}'"
                raise CompileError(message, line=expression.line)
            if object_name in self.global_scalars:
                base_operand = self._local_address(object_name)
            elif object_name in self.locals:
                frame_offset = self.locals[object_name]
                base_operand = f"ebp-{frame_offset}"
            else:
                message = f"undefined variable '{object_name}'"
                raise CompileError(message, line=expression.line)
            tag = struct_type[7:]
            layout = self.struct_layouts.get(tag)
            if layout is None:
                message = f"unknown struct '{tag}'"
                raise CompileError(message, line=expression.line)
            if expression.member_name not in layout:
                message = f"struct '{tag}' has no field '{expression.member_name}'"
                raise CompileError(message, line=expression.line)
            info = layout[expression.member_name]
            offset = info.byte_offset
            field_size = info.field_size
            element_size = info.element_size
            is_array_field = field_size != element_size
            self.ax_clear()
            if is_array_field:
                if object_name in self.global_scalars:
                    # Global: load address as immediate (label arithmetic).
                    if offset:
                        self.emit(f"        mov {self.target.acc}, {base_operand}+{offset}")
                    else:
                        self.emit(f"        mov {self.target.acc}, {base_operand}")
                # Local: use lea against the frame base.
                elif offset:
                    self.emit(f"        lea {self.target.acc}, [{base_operand}+{offset}]")
                else:
                    self.emit(f"        lea {self.target.acc}, [{base_operand}]")
                self.ax_clear()
                return
            allowed_sizes = (1, 2, 4) if self.target.int_size == 4 else (1, 2)
            if field_size not in allowed_sizes:
                message = f"reading '{expression.member_name}' (size {field_size}) not yet supported; use asm()"
                raise CompileError(message, line=expression.line)
            addr = f"[{base_operand}+{offset}]" if offset else f"[{base_operand}]"
            if info.bit_width is not None:
                self._emit_bitfield_read(info, addr=addr)
                return
            if field_size == 1:
                self.emit_byte_load_zx(addr)
            elif field_size == 2 and self.target.int_size == 4:
                self.emit(f"        movzx {self.target.acc}, word {addr}")
            else:
                self.emit(f"        mov {self.target.acc}, {addr}")
            self.ax_clear()
            return
        if not struct_type.startswith("struct ") or not struct_type.endswith("*"):
            message = f"'->' requires a pointer to struct, got type '{struct_type}'"
            raise CompileError(message, line=expression.line)
        tag = struct_type[7:-1]
        layout = self.struct_layouts.get(tag)
        if layout is None:
            message = f"unknown struct '{tag}'"
            raise CompileError(message, line=expression.line)
        if expression.member_name not in layout:
            message = f"struct '{tag}' has no field '{expression.member_name}'"
            raise CompileError(message, line=expression.line)
        info = layout[expression.member_name]
        offset = info.byte_offset
        field_size = info.field_size
        element_size = info.element_size
        is_array_field = field_size != element_size
        # Array fields evaluate to the field's address (so callers can pass
        # them to memcpy / memcmp / a function expecting a pointer).  Element
        # access uses the dedicated MemberIndex node.
        if is_array_field:
            self.ax_clear()
            if self.si_local == object_name:
                base_reg = self.target.si_register
            else:
                self._emit_load_var(object_name, register=self.target.bx_register)
                base_reg = self.target.bx_register
            if offset:
                self.emit(f"        lea {self.target.acc}, [{base_reg}+{offset}]")
            else:
                self.emit(f"        mov {self.target.acc}, {base_reg}")
            self.ax_clear()
            return
        allowed_sizes = (1, 2, 4) if self.target.int_size == 4 else (1, 2)
        if field_size not in allowed_sizes:
            message = f"reading '{expression.member_name}' (size {field_size}) not yet supported; use asm()"
            raise CompileError(message, line=expression.line)
        self.ax_clear()
        if self.si_local == object_name:
            base_reg = self.target.si_register
        else:
            self._emit_load_var(object_name, register=self.target.bx_register)
            base_reg = self.target.bx_register
        addr = f"[{base_reg}+{offset}]" if offset else f"[{base_reg}]"
        if info.bit_width is not None:
            self._emit_bitfield_read(info, addr=addr)
            return
        if field_size == 1:
            self.emit_byte_load_zx(addr)
        elif field_size == 2 and self.target.int_size == 4:
            # 32-bit target: clear upper bytes of EAX so downstream
            # ``test eax, eax`` / signed compares don't read stale bits
            # left behind by a wider previous load.
            self.emit(f"        movzx {self.target.acc}, word {addr}")
        else:
            self.emit(f"        mov {self.target.acc}, {addr}")
        self.ax_clear()

    def _generate_member_access_via_expr(self, expression: MemberAccess, /) -> None:
        """Generate code for ``((struct T *)expr)->field``.

        The base is an arbitrary pointer expression (today: always a
        ``Cast`` to a ``struct T *``; the cast's target type tells us
        which struct layout to use for the field offset).  Evaluates
        the cast's inner expression into BX/EBX, then loads the field
        with the same offset / bitfield / byte-width handling as the
        named-pointer form.
        """
        base = expression.base_expr
        assert base is not None
        if not isinstance(base, Cast):
            message = "'->' on a non-cast expression base is not supported"
            raise CompileError(message, line=expression.line)
        target_type = base.target_type.rstrip()
        if not (target_type.startswith("struct ") and target_type.endswith("*")):
            message = f"'->' requires a struct-pointer cast, got '{target_type}'"
            raise CompileError(message, line=expression.line)
        tag = target_type[7:-1].rstrip()
        layout = self.struct_layouts.get(tag)
        if layout is None:
            message = f"unknown struct '{tag}'"
            raise CompileError(message, line=expression.line)
        if expression.member_name not in layout:
            message = f"struct '{tag}' has no field '{expression.member_name}'"
            raise CompileError(message, line=expression.line)
        info = layout[expression.member_name]
        offset = info.byte_offset
        field_size = info.field_size
        element_size = info.element_size
        is_array_field = field_size != element_size
        # Fast path: when the cast wraps ``&local`` (the port-IO bridge
        # idiom ``((struct T *)&raw)->field``), the base pointer is a
        # known frame address — skip the ``lea + mov ebx, eax + mov al,
        # [ebx]`` indirection and load directly from ``[ebp-K+offset]``.
        # Saves ~5 bytes per call site versus going through EBX.  Falls
        # through to the general path for non-AddressOf bases or for
        # AddressOf of something other than a known local (globals,
        # parameters not in locals, etc.).
        direct_address: str | None = None
        if isinstance(base.expression, AddressOf) and base.expression.var.name in self.locals:
            direct_address = self._local_address(base.expression.var.name)
        if direct_address is not None:
            self.ax_clear()
            if is_array_field:
                if offset:
                    self.emit(f"        lea {self.target.acc}, [{direct_address}+{offset}]")
                else:
                    self.emit(f"        lea {self.target.acc}, [{direct_address}]")
                self.ax_clear()
                return
            allowed_sizes = (1, 2, 4) if self.target.int_size == 4 else (1, 2)
            if field_size not in allowed_sizes:
                message = f"reading '{expression.member_name}' (size {field_size}) not yet supported; use asm()"
                raise CompileError(message, line=expression.line)
            addr = f"[{direct_address}+{offset}]" if offset else f"[{direct_address}]"
            if info.bit_width is not None:
                self._emit_bitfield_read(info, addr=addr)
                return
            if field_size == 1:
                self.emit_byte_load_zx(addr)
            elif field_size == 2 and self.target.int_size == 4:
                self.emit(f"        movzx {self.target.acc}, word {addr}")
            else:
                self.emit(f"        mov {self.target.acc}, {addr}")
            self.ax_clear()
            return
        # General path: materialise the base pointer into BX/EBX.
        # generate_expression leaves the value in AX/EAX; move to BX so
        # the field-load addressing modes ([bx+N] / [ebx+N]) match the
        # named-pointer path below.
        self.generate_expression(base.expression)
        self.emit(f"        mov {self.target.bx_register}, {self.target.acc}")
        base_reg = self.target.bx_register
        self.ax_clear()
        if is_array_field:
            if offset:
                self.emit(f"        lea {self.target.acc}, [{base_reg}+{offset}]")
            else:
                self.emit(f"        mov {self.target.acc}, {base_reg}")
            self.ax_clear()
            return
        allowed_sizes = (1, 2, 4) if self.target.int_size == 4 else (1, 2)
        if field_size not in allowed_sizes:
            message = f"reading '{expression.member_name}' (size {field_size}) not yet supported; use asm()"
            raise CompileError(message, line=expression.line)
        addr = f"[{base_reg}+{offset}]" if offset else f"[{base_reg}]"
        if info.bit_width is not None:
            self._emit_bitfield_read(info, addr=addr)
            return
        if field_size == 1:
            self.emit_byte_load_zx(addr)
        elif field_size == 2 and self.target.int_size == 4:
            self.emit(f"        movzx {self.target.acc}, word {addr}")
        else:
            self.emit(f"        mov {self.target.acc}, {addr}")
        self.ax_clear()

    def generate_member_address_of(self, expression: MemberAddressOf, /) -> None:
        """Generate code for ``&obj.field``.

        Bitfield members have no addressable storage and are always rejected
        with a :class:`~cc.errors.CompileError`.  Both file-scope struct
        globals (``_g_obj + offset`` via ``mov``) and local struct values
        (``[ebp-N+offset]`` via ``lea``) are supported.
        """
        object_name = expression.object_name
        struct_type = self.variable_types.get(object_name)
        if struct_type is None:
            message = f"undefined variable '{object_name}'"
            raise CompileError(message, line=expression.line)
        if struct_type.endswith("*"):
            message = "'&obj.field' requires a struct value, not a pointer; use '&ptr->field' or '&(*ptr).field'"
            raise CompileError(message, line=expression.line)
        if not struct_type.startswith("struct "):
            message = f"'.' requires a struct value, got type '{struct_type}'"
            raise CompileError(message, line=expression.line)
        tag = struct_type[7:]
        layout = self.struct_layouts.get(tag)
        if layout is None:
            message = f"unknown struct '{tag}'"
            raise CompileError(message, line=expression.line)
        info = layout.get(expression.member_name)
        if info is None:
            message = f"struct '{tag}' has no field '{expression.member_name}'"
            raise CompileError(message, line=expression.line)
        if info.bit_width is not None:
            message = f"cannot take address of bitfield '{expression.member_name}'"
            raise CompileError(message, line=expression.line)
        # Non-bitfield: emit the field address.
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
            raise CompileError(message, line=expression.line)
        self.ax_clear()

    def generate_member_assign(self, statement: MemberAssign, /) -> None:
        """Generate code for ``ptr->field = expr;`` or ``obj.field = expr;``.

        The dot form is supported on file-scope struct globals; the
        target address resolves to ``[_g_obj+offset]`` directly.
        """
        object_name = statement.object_name
        struct_type = self.variable_types.get(object_name)
        if struct_type is None:
            message = f"undefined variable '{object_name}'"
            raise CompileError(message, line=statement.line)
        if not statement.arrow:
            if struct_type.endswith("*") or not struct_type.startswith("struct "):
                message = f"'.' requires a struct value, got type '{struct_type}'"
                raise CompileError(message, line=statement.line)
            if object_name in self.global_scalars:
                base_operand = self._local_address(object_name)
            elif object_name in self.locals:
                frame_offset = self.locals[object_name]
                base_operand = f"ebp-{frame_offset}"
            else:
                message = f"undefined variable '{object_name}'"
                raise CompileError(message, line=statement.line)
            tag = struct_type[7:]
            layout = self.struct_layouts.get(tag)
            if layout is None:
                message = f"unknown struct '{tag}'"
                raise CompileError(message, line=statement.line)
            if statement.member_name not in layout:
                message = f"struct '{tag}' has no field '{statement.member_name}'"
                raise CompileError(message, line=statement.line)
            info = layout[statement.member_name]
            offset = info.byte_offset
            field_size = info.field_size
            addr = f"[{base_operand}+{offset}]" if offset else f"[{base_operand}]"
            if info.bit_width is not None:
                if info.bit_width == 1 and isinstance(statement.expr, Int) and statement.expr.value in (0, 1):
                    self._emit_bitfield_write_literal(info, addr=addr, value=statement.expr.value)
                    return
                # Const-fold: literal rhs on a known-constant local byte.
                # Compute the new byte entirely at compile time and emit a
                # single mov byte without a preceding mov eax load.  This
                # keeps the store consecutive with adjacent mov-byte emits
                # so the last-write-wins peephole in emit() can fire.
                if isinstance(statement.expr, Int):
                    slot = self._parse_local_byte_addr(addr)
                    if slot is not None and slot in self.known_local_bytes:
                        field_mask = ((1 << info.bit_width) - 1) << info.bit_offset
                        clear_mask = (~field_mask) & 0xFF
                        known = self.known_local_bytes[slot]
                        rhs = statement.expr.value & ((1 << info.bit_width) - 1)
                        new_byte = (known & clear_mask) | (rhs << info.bit_offset)
                        self.emit(f"        mov byte {addr}, {new_byte}")
                        return
                self.ax_clear()
                self.generate_expression(statement.expr)  # rhs → EAX (low byte = AL)
                self._emit_bitfield_write(info, addr=addr)
                return
            allowed_sizes = (1, 2, 4) if self.target.int_size == 4 else (1, 2)
            if field_size not in allowed_sizes:
                message = f"writing '{statement.member_name}' (size {field_size}) not yet supported; use asm()"
                raise CompileError(message, line=statement.line)
            self.ax_clear()
            self.generate_expression(statement.expr)
            if field_size == 1:
                self.emit(f"        mov byte {addr}, al")
            elif field_size == 2 and self.target.int_size == 4:
                self.emit(f"        mov word {addr}, ax")
            else:
                self.emit(f"        mov {addr}, {self.target.acc}")
            return
        if not struct_type.startswith("struct ") or not struct_type.endswith("*"):
            message = f"'->' requires a pointer to struct, got type '{struct_type}'"
            raise CompileError(message, line=statement.line)
        tag = struct_type[7:-1]
        layout = self.struct_layouts.get(tag)
        if layout is None:
            message = f"unknown struct '{tag}'"
            raise CompileError(message, line=statement.line)
        if statement.member_name not in layout:
            message = f"struct '{tag}' has no field '{statement.member_name}'"
            raise CompileError(message, line=statement.line)
        info = layout[statement.member_name]
        offset = info.byte_offset
        field_size = info.field_size
        if info.bit_width is not None:
            # Peephole: 1-bit field with a literal 0 / 1 rhs — no expression
            # evaluation needed, so resolve the base register and addr first.
            if info.bit_width == 1 and isinstance(statement.expr, Int) and statement.expr.value in (0, 1):
                if self.si_local == object_name:
                    base_reg = self.target.si_register
                else:
                    self._emit_load_var(object_name, register=self.target.bx_register)
                    base_reg = self.target.bx_register
                addr = f"[{base_reg}+{offset}]" if offset else f"[{base_reg}]"
                self._emit_bitfield_write_literal(info, addr=addr, value=statement.expr.value)
                return
            # General read-modify-write.  Evaluate rhs first so that
            # generate_expression cannot clobber the base register we load next.
            self.ax_clear()
            self.generate_expression(statement.expr)  # rhs → EAX (low byte = AL)
            # If SI still holds the struct pointer (no intervening call), use it
            # directly as the base register to avoid a BX round-trip.
            if self.si_local == object_name:
                base_reg = self.target.si_register
            else:
                self._emit_load_var(object_name, register=self.target.bx_register)
                base_reg = self.target.bx_register
            addr = f"[{base_reg}+{offset}]" if offset else f"[{base_reg}]"
            self._emit_bitfield_write(info, addr=addr)
            return
        allowed_sizes = (1, 2, 4) if self.target.int_size == 4 else (1, 2)
        if field_size not in allowed_sizes:
            message = f"writing '{statement.member_name}' (size {field_size}) not yet supported; use asm()"
            raise CompileError(message, line=statement.line)
        self.ax_clear()
        self.generate_expression(statement.expr)
        # If SI still holds the struct pointer (no intervening call), use it
        # directly as the base register to avoid a BX round-trip.
        if self.si_local == object_name:
            base_reg = self.target.si_register
        else:
            self._emit_load_var(object_name, register=self.target.bx_register)
            base_reg = self.target.bx_register
        addr = f"[{base_reg}+{offset}]" if offset else f"[{base_reg}]"
        if field_size == 1:
            self.emit(f"        mov byte {addr}, al")
        elif field_size == 2 and self.target.int_size == 4:
            self.emit(f"        mov word {addr}, ax")
        else:
            self.emit(f"        mov {addr}, {self.target.acc}")

    def generate_member_index(self, expression: MemberIndex, /) -> None:
        """Generate code for ``ptr->field[index]`` as an rvalue.

        Loads one element (byte for ``element_size == 1``, word for
        ``element_size == 2``) from ``base + field_offset + index *
        element_size``.  Constant indices fold into the displacement.
        """
        if not expression.arrow:
            message = "dot member index on local struct values is not yet supported; use a pointer and '->'"
            raise CompileError(message, line=expression.line)
        object_name = expression.object_name
        struct_type = self.variable_types.get(object_name)
        if struct_type is None:
            message = f"undefined variable '{object_name}'"
            raise CompileError(message, line=expression.line)
        if not struct_type.startswith("struct ") or not struct_type.endswith("*"):
            message = f"'->' requires a pointer to struct, got type '{struct_type}'"
            raise CompileError(message, line=expression.line)
        tag = struct_type[7:-1]
        layout = self.struct_layouts.get(tag)
        if layout is None:
            message = f"unknown struct '{tag}'"
            raise CompileError(message, line=expression.line)
        if expression.member_name not in layout:
            message = f"struct '{tag}' has no field '{expression.member_name}'"
            raise CompileError(message, line=expression.line)
        info = layout[expression.member_name]
        field_offset = info.byte_offset
        element_size = info.element_size
        if element_size not in (1, 2):
            message = f"indexing '{expression.member_name}' (element size {element_size}) not supported"
            raise CompileError(message, line=expression.line)
        # Constant index: fold offset + index*element_size into a single displacement.
        if isinstance(expression.index, Int):
            total_offset = field_offset + expression.index.value * element_size
            self.ax_clear()
            if self.si_local == object_name:
                base_reg = self.target.si_register
            else:
                self._emit_load_var(object_name, register=self.target.bx_register)
                base_reg = self.target.bx_register
            addr = f"[{base_reg}+{total_offset}]" if total_offset else f"[{base_reg}]"
            if element_size == 1:
                self.emit_byte_load_zx(addr)
            else:
                self.emit(f"        mov {self.target.acc}, {addr}")
            self.ax_clear()
            return
        # Variable index: AX = index, scale, add base+offset, load.
        self.ax_clear()
        self.generate_expression(expression.index)
        if element_size == 2:
            self.emit(f"        shl {self.target.acc}, 1")
        # Save scaled index, load base.
        self.emit(f"        push {self.target.acc}")
        if self.si_local == object_name:
            self.emit(f"        mov {self.target.bx_register}, {self.target.si_register}")
        else:
            self._emit_load_var(object_name, register=self.target.bx_register)
        self.emit(f"        pop {self.target.acc}")
        self.emit(f"        add {self.target.bx_register}, {self.target.acc}")
        addr = f"[{self.target.bx_register}+{field_offset}]" if field_offset else f"[{self.target.bx_register}]"
        if element_size == 1:
            self.emit_byte_load_zx(addr)
        else:
            self.emit(f"        mov {self.target.acc}, {addr}")
        self.ax_clear()

    def _emit_struct_element_offset(self, index: Node, struct_size: int, /) -> None:
        """Emit code that leaves ``index * struct_size`` in BX (uses AX as scratch)."""
        acc = self.target.acc
        bx = self.target.bx_register
        self.generate_expression(index)  # AX = index
        self.emit(f"        imul {acc}, {struct_size}")  # AX = index * struct_size
        self.emit(f"        mov {bx}, {acc}")  # BX = byte offset

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

    def generate_index_member_access(self, expression: IndexMemberAccess, /) -> None:
        """Generate code for ``arr[i].field`` as an rvalue.

        Computes the struct element byte offset (``i * struct_size``) into BX,
        then loads the field from ``[const_base + BX + field_offset]``.
        Array-typed fields yield the field address (as for ``ptr->arr_field``).
        """
        const_base, struct_size, field_offset, field_size, element_size = self._resolve_index_member_layout(
            expression.name, expression.member_name, expression.line
        )
        acc = self.target.acc
        bx = self.target.bx_register
        self.ax_clear()
        protect_bx = self._bx_holds_pinned_var()
        if protect_bx:
            self.emit(f"        push {bx}")
        self._emit_struct_element_offset(expression.index, struct_size)  # BX = i*stride
        is_array_field = field_size != element_size
        if is_array_field:
            # Yield the address of the array member.
            if field_offset:
                self.emit(f"        lea {acc}, [{const_base}+{field_offset}+{bx}]")
            else:
                self.emit(f"        lea {acc}, [{const_base}+{bx}]")
            if protect_bx:
                self.emit(f"        pop {bx}")
            self.ax_clear()
            return
        addr = f"[{const_base}+{field_offset}+{bx}]" if field_offset else f"[{const_base}+{bx}]"
        if field_size == 1:
            self.emit_byte_load_zx(addr)
        elif field_size == 2 and self.target.int_size == 4:
            self.emit(f"        movzx {acc}, word {addr}")
        else:
            self.emit(f"        mov {acc}, {addr}")
        if protect_bx:
            self.emit(f"        pop {bx}")
        self.ax_clear()

    def generate_index_member_assign(self, statement: IndexMemberAssign, /) -> None:
        """Generate code for ``arr[i].field = expr;``.

        Evaluates the rhs into AX and saves it; computes the struct element
        offset into BX; restores AX; stores at ``[const_base + BX + field_offset]``.
        """
        const_base, struct_size, field_offset, field_size, _element_size = self._resolve_index_member_layout(
            statement.name, statement.member_name, statement.line
        )
        allowed = (1, 2, 4) if self.target.int_size == 4 else (1, 2)
        if field_size not in allowed:
            message = f"writing '{statement.member_name}' (size {field_size}) not yet supported; use asm()"
            raise CompileError(message, line=statement.line)
        acc = self.target.acc
        bx = self.target.bx_register
        self.ax_clear()
        protect_bx = self._bx_holds_pinned_var()
        # When BX holds a pinned variable, push the live BX *before* the
        # rhs.  Rhs sits on top of the stack so the post-offset pop
        # restores it directly into AX without addressing tricks (SP
        # isn't a base register in 16-bit mode, ruling out
        # ``[sp+N]``-style indexed loads).
        if protect_bx:
            self.emit(f"        push {bx}")
        self.generate_expression(statement.expr)  # AX = value
        self.emit(f"        push {acc}")  # save value (top of stack)
        self._emit_struct_element_offset(statement.index, struct_size)  # BX = i*stride
        self.emit(f"        pop {acc}")  # AX = value
        self.ax_clear()
        addr = f"[{const_base}+{field_offset}+{bx}]" if field_offset else f"[{const_base}+{bx}]"
        if field_size == 1:
            self.emit(f"        mov byte {addr}, al")
        elif field_size == 2 and self.target.int_size == 4:
            self.emit(f"        mov word {addr}, ax")
        else:
            self.emit(f"        mov {addr}, {acc}")
        if protect_bx:
            self.emit(f"        pop {bx}")  # restore pinned var

    def generate_index_member_index(self, expression: IndexMemberIndex, /) -> None:
        """Generate code for ``arr[i].field[n]`` as an rvalue.

        Computes the struct element offset in BX, scales the element index by
        element_size, adds them, then loads from
        ``[const_base + BX + field_offset]``.
        """
        const_base, struct_size, field_offset, _field_size, element_size = self._resolve_index_member_layout(
            expression.name, expression.member_name, expression.line
        )
        if element_size not in (1, 2):
            message = f"indexing '{expression.member_name}' (element size {element_size}) not supported"
            raise CompileError(message, line=expression.line)
        acc = self.target.acc
        bx = self.target.bx_register
        self.ax_clear()
        self._emit_struct_element_offset(expression.index, struct_size)  # BX = i*stride
        self.emit(f"        push {bx}")  # save struct element offset
        self.generate_expression(expression.elem_index)  # AX = n
        if element_size == 2:
            self.emit(f"        shl {acc}, 1")  # AX = n*2
        self.emit(f"        pop {bx}")  # BX = i*stride
        self.emit(f"        add {bx}, {acc}")  # BX = i*stride + n*element_size
        addr = f"[{const_base}+{field_offset}+{bx}]" if field_offset else f"[{const_base}+{bx}]"
        if element_size == 1:
            self.emit_byte_load_zx(addr)
        else:
            self.emit(f"        mov {acc}, {addr}")
        self.ax_clear()

    def generate_index_member_index_assign(self, statement: IndexMemberIndexAssign, /) -> None:
        """Generate code for ``arr[i].field[n] = expr;``.

        Saves the rhs; computes ``i*struct_size`` into BX; saves BX;
        computes ``n*element_size`` into AX; adds to BX; restores rhs;
        stores at ``[const_base + BX + field_offset]``.
        """
        const_base, struct_size, field_offset, _field_size, element_size = self._resolve_index_member_layout(
            statement.name, statement.member_name, statement.line
        )
        if element_size not in (1, 2):
            message = f"indexing '{statement.member_name}' (element size {element_size}) not supported"
            raise CompileError(message, line=statement.line)
        acc = self.target.acc
        bx = self.target.bx_register
        self.ax_clear()
        self.generate_expression(statement.expr)  # AX = value
        self.emit(f"        push {acc}")  # save value
        self._emit_struct_element_offset(statement.index, struct_size)  # BX = i*stride
        self.emit(f"        push {bx}")  # save struct element offset
        self.generate_expression(statement.elem_index)  # AX = n
        if element_size == 2:
            self.emit(f"        shl {acc}, 1")  # AX = n*2
        self.emit(f"        pop {bx}")  # BX = i*stride
        self.emit(f"        add {bx}, {acc}")  # BX = i*stride + n*element_size
        self.emit(f"        pop {acc}")  # AX = value
        self.ax_clear()
        addr = f"[{const_base}+{field_offset}+{bx}]" if field_offset else f"[{const_base}+{bx}]"
        if element_size == 1:
            self.emit(f"        mov byte {addr}, al")
        else:
            self.emit(f"        mov word {addr}, ax")

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
            return f"_g_{name}"
        message = f"no address for '{name}' (not a local or global scalar)"
        raise CompileError(message)

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
            self.emit(f"        push _g_{arg.name}")
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
            if isinstance(current, (Index, DoubleIndex)):
                # Index lowering uses SI as the base-address scratch
                # whenever the base isn't a compile-time constant — by
                # far the most common shape.  DoubleIndex always
                # parks the outer pointer in SI before the inner load.
                # Be conservative and always claim SI.
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
            self.emit(f"        mov {target}, _g_{arg.name}")
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
                if target != self.target.acc:
                    source = self.target.low_word(self.target.acc) if len(target) < len(self.target.acc) else self.target.acc
                    self.emit(f"        mov {target}, {source}")
            else:
                self.emit(f"        mov {target}, [{self._local_address(arg.name)}]")
        elif isinstance(arg, BinaryOperation):
            # ``_is_simple_arg`` admits BinaryOperation(+ - | & ^, leaf, leaf)
            # plus shifts with Int RHS — all stay in the accumulator. The
            # topological scheduler in ``_emit_register_arg_moves``
            # already verified that ``target`` is not read by any other
            # pending arg.  Evaluate into AX, then move into target.
            self.generate_expression(arg)
            if target != self.target.acc:
                source = self.target.low_word(self.target.acc) if len(target) < len(self.target.acc) else self.target.acc
                self.emit(f"        mov {target}, {source}")
        else:
            message = f"register-arg target {target} given unexpected complex node {arg!r}"
            raise CompileError(message, line=getattr(arg, "line", None))

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

    def _register_globals(self, declarations: list[Node], /) -> None:
        """Record file-scope declarations and validate their shapes.

        Scalars are stashed in :attr:`global_scalars`; arrays in
        :attr:`global_arrays`.  Byte-element arrays (``char`` or
        ``uint8_t``) are additionally tracked in
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
                # Build a packed field layout: {field_name: FieldInfo}.
                #
                # Regular fields (bit_width is None): field_size from
                # _type_size; array fields get field_size = element_size *
                # count, element_size = per-element width.
                #
                # Bitfields (bit_width 1..8 from the parser): consecutive
                # bitfields pack into a single byte run.  bit_offset tracks
                # the next free bit within the current run; LSB-first.  An
                # anonymous bitfield (field_name is None) advances run_bits
                # but isn't entered in ``layout`` since it has no name to
                # look up.  A regular field after a bitfield run closes the
                # run (advances cursor by 1) before its own byte_offset is
                # computed.
                layout: dict[str, FieldInfo] = {}
                cursor = 0
                run_bits = 0  # bits already consumed in the current bitfield run
                for field in declaration.fields:
                    if field.bit_width is not None:
                        if run_bits + field.bit_width > 8:
                            message = f"bitfield run exceeds 8 bits in struct '{declaration.name}' at line {field.line}"
                            raise CompileError(message, line=field.line)
                        if field.field_name is not None:
                            layout[field.field_name] = FieldInfo(
                                bit_offset=run_bits,
                                bit_width=field.bit_width,
                                byte_offset=cursor,
                                element_size=1,
                                field_size=1,
                            )
                        run_bits += field.bit_width
                        continue
                    # Regular field: close any open bitfield run first.
                    if run_bits > 0:
                        cursor += 1
                        run_bits = 0
                    ftype = field.type_name
                    if "[" in ftype:
                        # "char[15]" → element_type="char", count=15
                        bracket = ftype.index("[")
                        element_type = ftype[:bracket]
                        count = int(ftype[bracket + 1 : -1])
                        element_size = self._type_size(element_type)
                        field_size = element_size * count
                    else:
                        field_size = self._type_size(ftype)
                        element_size = field_size
                    layout[field.field_name] = FieldInfo(
                        bit_offset=None,
                        bit_width=None,
                        byte_offset=cursor,
                        element_size=element_size,
                        field_size=field_size,
                    )
                    cursor += field_size
                if run_bits > 0:
                    cursor += 1
                self.struct_layouts[declaration.name] = layout
                self.struct_sizes[declaration.name] = cursor
                continue
            name = declaration.name
            if name in self.NAMED_CONSTANTS:
                message = f"global '{name}' shadows a kernel constant"
                raise CompileError(message, line=declaration.line)
            if name in self.user_functions or name == "main":
                message = f"global '{name}' collides with a function name"
                raise CompileError(message, line=declaration.line)
            if name in self.global_scalars or name in self.global_arrays:
                message = f"duplicate global declaration: {name}"
                raise CompileError(message, line=declaration.line)
            if isinstance(declaration, VarDecl):
                if declaration.type_name == "unsigned long":
                    message = "unsigned long globals are not supported"
                    raise CompileError(message, line=declaration.line)
                if declaration.type_name == "void":
                    message = f"global '{name}' cannot have type void"
                    raise CompileError(message, line=declaration.line)
                if declaration.init is not None and self._constant_expression(declaration.init) is None:
                    message = f"global '{name}' initializer must be a constant expression"
                    raise CompileError(message, line=declaration.line)
                if declaration.init is not None:
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
        # Reset the switch-discriminant override set on every call: this
        # function runs once during the pre-pass that computes per-callee
        # register conventions and once per function body, and the body
        # pass's :meth:`can_auto_pin` reads the set after this call.
        self.switch_pin_overrides = set()

        param_candidates: list[tuple[str, int]] = []
        for order, param in enumerate(parameters):
            if param.is_array:
                continue
            param_candidates.append((param.name, order))

        body_candidates: list[tuple[str, int]] = []
        order = 0
        function_pointer_vars: set[str] = self._collect_function_pointer_vars(body)

        def call_clobbers(call_node: Call) -> tuple[str, ...] | frozenset[str]:
            """Mirror :meth:`compute_safe_pin_registers`'s per-call clobber set."""
            if call_node.name in self.user_functions or call_node.name in function_pointer_vars:
                return self.target.register_pool
            if call_node.name in self.libbboeos_extern_declarations:
                return self.target.register_pool
            if call_node.name in self._builtin_clobbers:
                return self._builtin_clobbers[call_node.name]
            return ()

        def collect(nodes: list[Node], *, top_level: bool) -> None:
            nonlocal order
            for statement in nodes:
                if isinstance(statement, VarDecl):
                    eligible = (
                        statement.type_name not in ("unsigned long", "function_pointer")
                        and not (top_level and self._is_constant_alias(body=nodes, statement=statement))
                        and not isinstance(statement.init, Call)
                    )
                    if eligible:
                        body_candidates.append((statement.name, order))
                        order += 1
                if isinstance(statement, If):
                    collect(statement.body, top_level=False)
                    if statement.else_body is not None:
                        collect(statement.else_body, top_level=False)
                elif isinstance(statement, (Compound, DoWhile, While)):
                    collect(statement.body, top_level=False)
                elif isinstance(statement, Switch):
                    for case in statement.cases:
                        collect(case.body, top_level=False)

        collect(body, top_level=True)

        counts: dict[str, int] = {}
        index_uses: dict[str, int] = {}
        # Per-var tallies that drive the expression-temporary check
        # below.  ``ax_resident_uses`` counts Var refs sitting on the
        # LEFT of a comparison whose right side is an integer literal
        # — those are exactly the sites ``emit_comparison`` reuses an
        # AX-resident value for via the ``ax_local`` fast path.  Right
        # operands and non-cmp uses go to ``other_uses`` because the
        # left operand's expression eval clobbers AX before they're
        # reached, so they need either a memory load or a pin.
        ax_resident_uses: dict[str, int] = {}
        other_uses: dict[str, int] = {}
        init_count: dict[str, int] = {}
        init_expr: dict[str, Node] = {}
        address_taken: set[str] = set()
        comparison_operations = {"==", "!=", "<", "<=", ">", ">="}

        def collect_index_vars(node: Node) -> None:
            """Tally Var occurrences inside Index/IndexAssign subscripts.

            Each subscript pays a 2-byte ``mov si, bp`` penalty when
            its index variable is BP-pinned, since BP can't index
            DS-relative memory in real mode.  The cost-model below
            uses this tally to decide whether a candidate's
            BP-clobber-savings outweigh that per-subscript penalty.
            """
            if isinstance(node, Var):
                index_uses[node.name] = index_uses.get(node.name, 0) + 1
            for node_field in fields(node):
                value = getattr(node, node_field.name)
                if isinstance(value, Node):
                    collect_index_vars(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, Node):
                            collect_index_vars(item)

        def count_visit(node: Node, *, role: str = "other") -> None:
            if isinstance(node, (Var, Assign)):
                counts[node.name] = counts.get(node.name, 0) + 1
            elif isinstance(node, (Index, IndexAssign)):
                counts[node.array.name] = counts.get(node.array.name, 0) + 1
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
                    counts[node.discriminant.name] = counts.get(node.discriminant.name, 0) + len(case_arms) - 1
                    # The Call-init filter in ``collect`` above excludes
                    # ``int x = getchar();`` and similar from the candidate
                    # list (the rationale: pinning a callee's AX return adds
                    # a ``mov R, eax`` that often outweighs the per-ref save).
                    # For a switch discriminant with N >= 4 always-exit arms
                    # the interleaved-dispatch win (4 bytes per arm vs the
                    # separated near-jump form) easily covers that move, so
                    # add the discriminant here when it isn't already a body
                    # candidate.
                    existing_names = {body_name for body_name, _ in body_candidates}
                    if node.discriminant.name not in existing_names and len(case_arms) >= 4:
                        body_candidates.append((node.discriminant.name, len(body_candidates)))
                    # Tell ``can_auto_pin`` to honor the pin even when the
                    # declaration's init is a ``Call`` — the per-arm win
                    # easily covers the extra ``mov R, eax`` after the call.
                    self.switch_pin_overrides.add(node.discriminant.name)
            if isinstance(node, VarDecl) and node.init is not None:
                init_count[node.name] = init_count.get(node.name, 0) + 1
                init_expr[node.name] = node.init
                count_visit(node.init)
                return
            if isinstance(node, Assign):
                init_count[node.name] = init_count.get(node.name, 0) + 1
                init_expr[node.name] = node.expr
                count_visit(node.expr)
                return
            if isinstance(node, Var):
                if role == "cmp_left_imm":
                    ax_resident_uses[node.name] = ax_resident_uses.get(node.name, 0) + 1
                else:
                    other_uses[node.name] = other_uses.get(node.name, 0) + 1
            if isinstance(node, BinaryOperation):
                if node.operation in comparison_operations:
                    # Only the LEFT operand can reuse an AX-resident
                    # value: the right side is loaded into CX after the
                    # left's evaluation has overwritten AX.  Even on
                    # the left, the fast path requires the right side
                    # to be a constant (Int or NAMED_CONSTANT) so the
                    # cmp can be ``cmp ax, imm`` / ``cmp ax, NAME``.
                    right_is_const = isinstance(node.right, Int) or (
                        isinstance(node.right, Var) and node.right.name in self.NAMED_CONSTANTS
                    )
                    left_role = "cmp_left_imm" if right_is_const else "other"
                    count_visit(node.left, role=left_role)
                    count_visit(node.right, role="other")
                else:
                    count_visit(node.left, role="other")
                    count_visit(node.right, role="other")
                return
            if isinstance(node, (Index, IndexAssign)):
                # The `array` Var was already tallied via the counts[] branch
                # above; recursing into it would double-count and add a
                # spurious other_uses tally.  Walk the remaining children
                # explicitly and bail before the generic walk.
                collect_index_vars(node.index)
                count_visit(node.index)
                if isinstance(node, IndexAssign):
                    count_visit(node.expr)
                return
            if isinstance(node, Call):
                # ``&x`` at an ``out_register`` arg position is a fake
                # address — the callee writes the named register and
                # the caller captures it, so *x* doesn't need a memory
                # address and stays eligible for auto-pin.  Count those
                # args as a Var read (so the ref count reflects the
                # captured write that follows the call) but skip the
                # ``address_taken`` mark the generic AddressOf branch
                # below would record.  Real-address args (anything else)
                # fall through to that branch.
                out_regs = self.out_register_params.get(node.name, {})
                for index, arg in enumerate(node.args):
                    if index in out_regs and isinstance(arg, AddressOf):
                        count_visit(arg.var)
                    else:
                        count_visit(arg)
                return
            if isinstance(node, AddressOf):
                # ``&x`` computes an address, not a value read — pre-refactor
                # AddressOf carried ``name`` as a plain str so the inner var
                # never tallied; preserve that by skipping the generic walk's
                # descent into ``var``.  Track the name so the candidate
                # filter below can disqualify it: an auto-pinned register
                # has no memory address, and keeping the slot in sync with
                # the register across writes through the pointer would
                # require spill+reload at every access.
                address_taken.add(node.var.name)
                return
            if isinstance(node, DerefAssign):
                # ``*p = expr`` writes through a pointer — pre-refactor the
                # pointer was a str field, so it never counted as a read.
                # Walk only the right-hand side.
                count_visit(node.expr)
                return
            for node_field in fields(node):
                value = getattr(node, node_field.name)
                if isinstance(value, Node):
                    count_visit(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, Node):
                            count_visit(item)

        for statement in body:
            count_visit(statement)

        def is_expression_temporary(name: str) -> bool:
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

        # Per-candidate-per-register count of calls that ran BEFORE the
        # candidate's first AST-level store.  PR #454's liveness pre-pass
        # elides ``push <pin>`` / ``pop <pin>`` around those calls
        # (the pin holds garbage), so the auto-pin cost gate subtracts
        # them from ``register_clobber_counts`` per-candidate.
        # Parameters are not tracked — the prologue counts as their first
        # store, so every call in the body is post-store for them.
        candidate_names = {name for name, _ in body_candidates}
        pre_store_clobbers: dict[str, dict[str, int]] = {name: {} for name in candidate_names}
        written: dict[str, bool] = dict.fromkeys(candidate_names, False)

        def loop_writes(stmts: list[Node]) -> set[str]:
            """Return the set of candidate names assigned anywhere within *stmts*.

            Pre-merge stores from loop bodies into the written set
            before walking the body, mirroring the liveness pre-pass's
            loop-pre-merge (a store inside a loop is live on every
            iteration including the first, so calls BEFORE that store
            inside the body still see a live pin).
            """
            found: set[str] = set()
            for statement in stmts:
                if isinstance(statement, Assign) or (isinstance(statement, VarDecl) and statement.init is not None):
                    found.add(statement.name)
                elif isinstance(statement, If):
                    found |= loop_writes(statement.body)
                    if statement.else_body is not None:
                        found |= loop_writes(statement.else_body)
                elif isinstance(statement, (Compound, DoWhile, While)):
                    found |= loop_writes(statement.body)
                elif isinstance(statement, Switch):
                    for case in statement.cases:
                        found |= loop_writes(case.body)
            return found

        def pre_store_visit(node: Node) -> None:
            if isinstance(node, (DoWhile, While)):
                for name in loop_writes(node.body):
                    if name in candidate_names:
                        written[name] = True
                for body_statement in node.body:
                    pre_store_visit(body_statement)
                return
            if isinstance(node, Call):
                # Walk args first so a store inside an arg expression
                # (rare but possible) lands in `written` before the
                # call itself is counted.  Then tally clobbers for
                # candidates still pre-store.
                for arg in node.args:
                    pre_store_visit(arg)
                regs = call_clobbers(node)
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
                    if index in out_regs and isinstance(arg, AddressOf) and arg.var.name in candidate_names:
                        written[arg.var.name] = True
                return
            if isinstance(node, Assign):
                pre_store_visit(node.expr)
                if node.name in candidate_names:
                    written[node.name] = True
                return
            if isinstance(node, VarDecl):
                if node.init is not None:
                    pre_store_visit(node.init)
                    if node.name in candidate_names:
                        written[node.name] = True
                return
            for node_field in fields(node):
                value = getattr(node, node_field.name)
                if isinstance(value, Node):
                    pre_store_visit(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, Node):
                            pre_store_visit(item)

        for statement in body:
            pre_store_visit(statement)

        def rank(items: list[tuple[str, int]]) -> list[tuple[str, int]]:
            return sorted(items, key=lambda item: (-counts.get(item[0], 0), item[1]))

        combined = rank(body_candidates) + rank(param_candidates)
        # Drop expression-temporary vars: pinning them adds a 2-byte
        # ``mov pin, ax`` after their single complex-expression
        # initializer without shrinking the comparisons that follow
        # (those already work against AX).
        combined = [item for item in combined if not is_expression_temporary(item[0])]
        # An auto-pinned register has no memory address; vars whose
        # address is taken (``&x``) must live in a frame slot so
        # ``_local_address`` can hand back a real pointer.
        combined = [item for item in combined if item[0] not in address_taken]
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
                # effective cost; the original logic broke out of the
                # loop here because every later candidate was lower
                # priority.  Mirror that with a single break.
                break
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
                    elided = pre_store_clobbers.get(name, {}).get(chosen, 0)
                    effective_cost = max(0, raw_cost - elided)
                    if refs > effective_cost:
                        assignments[name] = chosen
                        register_holders[chosen].append(name)
        return assignments

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
                self.emit(f"        mov {register}, _g_{name}")
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

    def compute_safe_pin_registers(self, body: list[Node], /) -> tuple[str, ...]:
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

        function_pointer_vars = self._collect_function_pointer_vars(body)

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
                self.emit("        jne FUNCTION_DIE")
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
        else:
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
                    self.generate_expression(left)
                    # Use matching-width operands for cmp: if source is
                    # narrower than acc (e.g., bp vs eax), compare ax/source.
                    cmp_acc = self.target.low_word(self.target.acc) if len(source) < len(self.target.acc) else self.target.acc
                    self.emit(f"        cmp {cmp_acc}, {source}")
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

    def emit_condition(self, *, condition: Node, context: str) -> tuple[str, bool]:
        """Validate a condition, emit a comparison, and return ``(operator, unsigned)``.

        ``unsigned`` is True when at least one operand is an unsigned
        type (``uint8_t`` / ``uint16_t`` / ``uint32_t`` / ``unsigned
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
            if register != self.target.acc:
                source = self.target.low_word(self.target.acc) if len(register) < len(self.target.acc) else self.target.acc
                self.emit(f"        mov {register}, {source}")
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
                if register != self.target.acc:
                    source = self.target.low_word(self.target.acc) if len(register) < len(self.target.acc) else self.target.acc
                    self.emit(f"        mov {register}, {source}")
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
            if register != self.target.acc:
                # In 32-bit mode, the result is in eax; narrow-register targets
                # (bx, cx, dx, si, di) need the 16-bit low word of eax.
                source = self.target.low_word(self.target.acc) if len(register) < len(self.target.acc) else self.target.acc
                self.emit(f"        mov {register}, {source}")
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
                if statement.type_name not in ("unsigned long", "function_pointer") and statement.name in self.auto_pin_candidates:
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
        ``Char`` literal may appear opposite a ``uint8_t`` / ``int``
        operand — it's just a small integer, and forcing hex spelling
        there hurts readability (``byte >= '\xC0'`` stays legal).
        """
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
