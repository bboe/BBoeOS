"""x86 emission: program / function / statement / expression dispatchers and IR lowering.

Houses the ``generate`` top-level orchestrator, every ``generate_*``
statement handler (``generate_body``, ``generate_call``,
``generate_do_while``, ``generate_function``, ``generate_if``,
``generate_index_assign``, ``generate_return``, ``generate_statement``,
``generate_while``), the expression dispatchers (``generate_expression``
/ ``generate_long_expression``), the tail-call eligibility check, and
the IR lowering helpers (``_ir_value_to_ast``, ``lower_ir_body``,
``_lower_ir_instruction``).

Everything in this module reads arch-specific register names and
x86 mnemonics, so it stays inside the ``cc.codegen.x86`` package.
The mixin only relies on methods supplied by ``CodeGeneratorBase``
and ``BuiltinsMixin`` (for ``builtin_*`` dispatch), so composition
order in ``X86CodeGenerator`` isn't load-bearing.  The peephole pass
runs as a post-processing stage via :class:`cc.codegen.x86.peephole.Peepholer`
and is invoked from :meth:`generate` after all functions have been
emitted.
"""

from __future__ import annotations

import re
from dataclasses import fields
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Callable

from cc import ir
from cc.ast_nodes import (
    ArrayDecl,
    Assign,
    AssignExpr,
    BinaryOperation,
    Break,
    Call,
    Cast,
    Char,
    Compound,
    Conditional,
    Continue,
    DereferencePlace,
    DerefIncrement,
    DerefIncrementAssign,
    DoWhile,
    ExtendedAsm,
    For,
    Function,
    Goto,
    If,
    Index,
    IndexAssign,
    InlineAsm,
    Int,
    Label,
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
    Return,
    SizeofExpr,
    SizeofType,
    SizeofVar,
    String,
    StructInitializer,
    SubscriptPlace,
    Switch,
    SwitchCase,
    TailCall,
    VaArg,
    Var,
    VarDecl,
    VariablePlace,
    While,
    address_of_variable_name,
)
from cc.codegen.x86.jumps import (
    CMOV_WHEN_FALSE,
    CMOV_WHEN_TRUE,
    JUMP_WHEN_FALSE,
    JUMP_WHEN_FALSE_UNSIGNED,
    JUMP_WHEN_TRUE,
    JUMP_WHEN_TRUE_UNSIGNED,
)
from cc.codegen.x86.peephole import Peepholer
from cc.errors import CompileError
from cc.ir_optimize import Optimizer
from cc.target import CodegenTarget, X86CodegenTarget16
from cc.tokens import COMPARISON_OPERATIONS
from cc.types import ArrayType
from cc.utils import decode_string_escapes, string_byte_length

_ATT_REGISTERS = "eax|ebx|ecx|edx|esi|edi|esp|ebp|ax|bx|cx|dx|si|di|sp|bp|ah|al|bh|bl|ch|cl|dh|dl"
_ATT_REGISTER = re.compile(rf"%({_ATT_REGISTERS})\b")
_ATT_IMMEDIATE = re.compile(r"\$((?:0x[0-9a-fA-F]+|[0-9]+))\b")

#: Lines that look like a function label: bare identifier at column 0
#: followed by ``:`` and nothing else.  Used by :func:`_elide_dead_frames`
#: to split the global line stream into per-function ranges.  Anchors,
#: directives, and inline comments don't match.
_FUNCTION_LABEL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*:$")

_SINGLE_OPERAND_MNEMONICS = frozenset({
    "call",
    "dec",
    "inc",
    "int",
    "ja",
    "jae",
    "jb",
    "jbe",
    "jc",
    "je",
    "jg",
    "jge",
    "jl",
    "jle",
    "jmp",
    "jnc",
    "jne",
    "jno",
    "jnp",
    "jns",
    "jnz",
    "jo",
    "jp",
    "js",
    "jz",
    "neg",
    "not",
    "pop",
    "push",
    "ret",
    "seta",
    "setae",
    "setb",
    "setbe",
    "setc",
    "sete",
    "setg",
    "setge",
    "setl",
    "setle",
    "setnc",
    "setne",
    "setno",
    "setnp",
    "setns",
    "setnz",
    "seto",
    "setp",
    "sets",
    "setz",
})


def _att_to_intel(line: str) -> str:
    """Convert one AT&T-syntax asm line to Intel syntax (NASM).

    Only fires when the line contains AT&T markers (``%reg`` or
    ``$imm``); Intel-syntax lines pass through unchanged.
    """
    stripped = line.lstrip()
    if not stripped or stripped.startswith(";"):
        return line
    if not _ATT_REGISTER.search(stripped) and not _ATT_IMMEDIATE.search(stripped):
        return line
    indent = line[: len(line) - len(stripped)]
    parts = stripped.split(None, 1)
    mnemonic = parts[0].rstrip(",")
    operand_text = parts[1] if len(parts) > 1 else ""
    operand_text = _ATT_REGISTER.sub(r"\1", operand_text)
    operand_text = _ATT_IMMEDIATE.sub(r"\1", operand_text)
    if not operand_text:
        return f"{indent}{mnemonic}"
    operands = [operand.strip() for operand in operand_text.split(",")]
    if len(operands) == 2 and mnemonic not in _SINGLE_OPERAND_MNEMONICS:
        operands.reverse()
    return f"{indent}{mnemonic} {', '.join(operands)}"


def _elide_dead_frames(*, lines: list[str], target: CodegenTarget) -> list[str]:
    """Drop ``sub <sp>, N`` + ``mov <sp>, <bp>`` lines in functions whose body never touches the frame.

    Functions allocate a stack frame for every declared local even
    when later peephole passes fold every use to an immediate — the
    common case for one-shot bitfield-register structs:

        struct foo s = {.bit = 1};
        kernel_outb(port, *(u8 *)&s);

    The const-fold + dead-store peepholes collapse both the init and
    the read to ``mov al, <const>``, leaving the prologue's
    ``sub esp, N`` and the matching ``mov esp, ebp`` epilogue paying
    for storage that's never referenced.  This sweep walks each
    emitted function's line range and, when no ``[<bp>+N]`` /
    ``[<bp>-N]`` reference survived peepholing, drops those two
    instruction shapes (and any duplicates from multiple-return
    paths).  ``push <bp>`` / ``pop <bp>`` stay — leaving them keeps
    the caller's frame chain undisturbed at zero extra cost relative
    to a hand-written asm equivalent.
    """
    base = target.base_register
    stack = target.stack_register
    bracket_base = f"[{base}"
    sub_prefix = f"        sub {stack}, "
    mov_unwind = f"        mov {stack}, {base}"
    result: list[str] = []
    # Buffer each function's body so we can decide whether to drop the
    # frame-management lines before flushing.  Anything outside a
    # function (preamble, file-scope storage, trailing %include) passes
    # through unchanged.
    pending: list[str] | None = None
    pending_has_frame_ref = False

    def flush(*, buffer: list[str] | None, has_frame_ref: bool) -> None:
        if buffer is None:
            return
        if has_frame_ref:
            result.extend(buffer)
            return
        for line in buffer:
            if line.startswith(sub_prefix) or line == mov_unwind:
                continue
            result.append(line)

    for line in lines:
        if _FUNCTION_LABEL_PATTERN.match(line):
            flush(buffer=pending, has_frame_ref=pending_has_frame_ref)
            pending = [line]
            pending_has_frame_ref = False
            continue
        if pending is None:
            result.append(line)
            continue
        if bracket_base in line:
            pending_has_frame_ref = True
        pending.append(line)
    flush(buffer=pending, has_frame_ref=pending_has_frame_ref)
    return result


class EmissionMixin:
    """Emission dispatchers, mixed into :class:`X86CodeGenerator`.

    The mixin expects the mixing class to provide the arch-agnostic
    state and helpers from :class:`cc.codegen.base.CodeGeneratorBase`
    (``self.lines``, ``self.emit``, ``self.target``, symbol tables,
    frame state) plus the x86-specific ``emit_*`` helpers (``emit_*``
    methods that still live on the generator class) and the
    ``builtin_*`` / ``peephole`` dispatchers from sibling mixins.
    """

    def _allocate_function_parameters(
        self,
        *,
        function_line: int,
        is_fastcall: bool,
        name: str,
        parameters: list[Param],
        regparm_count: int,
    ) -> None:
        """Record parameter types / array flags and assign caller-pushed stack offsets.

        Extracted from :meth:`generate_function`.  ``main`` is special-
        cased (its parameters are handled by
        :meth:`emit_argument_vector_startup`).  For non-main functions
        this also rejects parameter names that shadow a file-scope global
        and populates ``self.out_register_locals`` for output-only
        register params; ``in_register`` and regparm params get their
        stack slots allocated later (after this returns, in
        ``generate_function``).
        """
        for param in parameters:
            if param.name in self.global_scalars or param.name in self.global_arrays:
                message = f"parameter '{param.name}' shadows a file-scope global"
                raise CompileError(message, line=function_line)
        if name == "main":
            # main parameters are handled by emit_argument_vector_startup.
            for param in parameters:
                self.allocate_local(param.name)
                self.variable_types[param.name] = param.type
                if param.is_array:
                    self.variable_arrays.add(param.name)
            return
        # Non-main: record parameter types; stack offsets are kept
        # as fallbacks but parameters will be pinned to registers
        # when safe_pin_registers has room.
        caller_push_index = 0
        for i, param in enumerate(parameters):
            self.variable_types[param.name] = param.type
            # A multidimensional array parameter (``int m[][3]`` →
            # dimensions ``[None, Int(3)]``) decays to a pointer-to-array
            # ``int (*)[3]`` (drop the leading outer dim).  An explicit
            # ``int (*m)[3]`` parameter carries its pointee dims directly.
            # Both become a pointer-sized slot driven by pointer_array_types.
            pointee_dimensions: list | None = None
            if param.pointer_array_dimensions is not None:
                pointee_dimensions = param.pointer_array_dimensions
            elif param.dimensions is not None and len(param.dimensions) > 1:
                pointee_dimensions = param.dimensions[1:]
            if pointee_dimensions is not None:
                self._register_pointer_to_array(
                    param.name,
                    element_type_name=param.type,
                    line=function_line,
                    pointee_dimensions=pointee_dimensions,
                )
            if param.is_array:
                self.variable_arrays.add(param.name)
            if param.out_register is not None:
                # Output-only register param: no caller-pushed stack slot.
                # Track it so a ``*p = v`` store in the body emits
                # mov <reg>, <val> instead of a pointer write.
                self.out_register_locals[param.name] = param.out_register
                continue
            if param.in_register is not None:
                # Input register param: caller puts arg in named register (no push).
                # Allocate a local slot below; spilled after sub sp,N in prologue.
                continue
            if is_fastcall and i < regparm_count:
                # Register-passed params get local slots allocated
                # below; they have no caller-pushed address.
                continue
            self.locals[param.name] = -(self.target.param_slot_base + caller_push_index * self.target.int_size)  # negative = above bp
            caller_push_index += 1

    def _apply_default_regparm(self, functions: list[Node], /) -> None:
        """Stamp the implicit register-passing convention on eligible callees.

        Sets ``regparm_count = min(3, len(params))`` so args 0..2 land
        in EAX/EDX/ECX with any remaining args caller-pushed.  Eligible:
        not ``main`` (the loader pushes argc/argv on the stack), not
        ``naked`` (no prologue spill), takes at least one parameter,
        and none of the parameters use ``in_register`` / ``out_register``
        (those define their own slot mapping).  Prototypes are eligible
        too — both ends of a cross-TU pair derive the same default so
        their ABIs agree without per-site annotation.  Falls back to
        cdecl when any call site passes a complex argument; lifting
        that limit requires extending the call-site register-arg
        scheduler (see docs/cc_future_work.md).
        """
        user_names = {function.name for function in functions if function.name != "main"}
        has_complex_call: dict[str, bool] = dict.fromkeys(user_names, False)

        def visit(node: Node) -> None:
            if (
                isinstance(node, Call)
                and node.name in user_names
                and len(node.args) > 1
                and any(not self._is_simple_arg(arg) for arg in node.args)
            ):
                # 1-arg fastcall calls route through ``emit_register_from_argument``,
                # which already handles arbitrary expressions via AX; there is
                # no inter-arg target to clobber, so complexity is harmless.
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

        for function in functions:
            if (
                function.name != "main"
                and not function.naked
                and function.params
                and not has_complex_call.get(function.name)
                and all(parameter.out_register is None and parameter.in_register is None for parameter in function.params)
            ):
                function.regparm_count = min(3, len(function.params))

    def _classify_switch_arms(self, statement: Switch, /, *, cases_override: list | None = None) -> tuple:
        """Split a switch's cases into ``(default_case, case_arms)`` and check enum exhaustiveness.

        Raises :class:`CompileError` when the discriminant has an
        ``enum NAME`` static type, no ``default`` arm is present, and one or
        more declared variants are missing from the case labels.  Arbitrary
        integer discriminants (calls returning int, arithmetic, etc.) are
        treated as plain int and skip the check.

        ``cases_override`` lets the IR-lowering path supply
        :class:`ir.SwitchCase` instances (which share the ``.value`` /
        ``.body`` shape with :class:`ast_nodes.SwitchCase`) in place of
        ``statement.cases`` — the IR builder rewrites case bodies into
        IR instruction lists, so the codegen needs the IR view here.
        """
        source_cases = cases_override if cases_override is not None else statement.cases
        default_case = None
        case_arms: list = []
        for case in source_cases:
            if case.value is None:
                default_case = case
            else:
                case_arms.append(case)
        enum_tag: str | None = None
        if isinstance(statement.discriminant, Var):
            discriminant_type = self.variable_types.get(statement.discriminant.name)
            if discriminant_type is not None and discriminant_type.startswith("enum "):
                enum_tag = discriminant_type[5:]
        if enum_tag is not None and default_case is None:
            line = statement.line
            declaration = self.enum_decls.get(enum_tag)
            if declaration is None:
                message = f"switch on undeclared enum '{enum_tag}'"
                raise CompileError(message, line=line)
            covered_values = {case.value for case in case_arms}
            missing = [variant_name for variant_name, value in declaration.variants if value not in covered_values]
            if missing:
                missing_list = ", ".join(f"'{name}'" for name in missing)
                # Match the spec's headline wording exactly so users searching for
                # the error in the codebase find this site.
                message = f"switch on enum '{enum_tag}' missing case for {missing_list}"
                raise CompileError(message, line=line)
        return default_case, case_arms

    def _emit_function_pointer_call(
        self,
        *,
        arguments: list[Node],
        discard_return: bool,
        line: int,
        name: str,
    ) -> None:
        """Emit a call through a function-pointer variable.

        Two argument-passing shapes, selected by whether the function
        pointer carries an ``in_register`` map (declared via
        ``__attribute__((in_register(...)))`` on each inner parameter):

        * with map: every argument routes through its named register;
          the per-pointer arg count must match the map.
        * without map (qsort/bsearch shape): standard cdecl stack
          convention; any arg count is accepted because the parser
          discards the inner-parameter list for fn-pointer params and
          locals don't enforce arity here.

        Pushes saved pinned registers (or ``pusha`` when ≥3 saves are
        needed AND the return value is discarded — saves 2 bytes per
        save beyond 3 but clobbers AX, hence the discard guard), loads
        the function pointer into the accumulator, ``call`` through it,
        cleans up any cdecl stack args, and restores the saved
        registers.
        """
        function_pointer_in_regs = self.function_pointer_in_registers.get(name, {})
        if function_pointer_in_regs and len(arguments) != len(function_pointer_in_regs):
            message = f"function_pointer '{name}' expects {len(function_pointer_in_regs)} argument(s), got {len(arguments)}"
            raise CompileError(message, line=line)
        clobbers: frozenset[str] = frozenset(self.target.register_pool)
        saved = self._pinned_registers_to_save(clobbers)
        use_pusha = discard_return and len(saved) >= 3
        if use_pusha:
            self.emit("        pusha")
        else:
            for register in saved:
                self.emit(f"        push {register}")
        stack_arguments: list[Node] = []
        if function_pointer_in_regs:
            register_args = [(function_pointer_in_regs[i], arg) for i, arg in enumerate(arguments)]
            self._emit_register_arg_moves(register_args)
        else:
            stack_arguments = list(arguments)
            for arg in reversed(stack_arguments):
                self._emit_push_arg(arg)
        self._emit_load_var(name, register=self.target.acc)
        self.emit(f"        call {self.target.acc}")
        if stack_arguments:
            self.emit(f"        add {self.target.stack_register}, {len(stack_arguments) * self.target.int_size}")
        if use_pusha:
            self.emit("        popa")
        else:
            for register in reversed(saved):
                self.emit(f"        pop {register}")
        self.ax_clear()

    def _emit_function_prologue(
        self,
        *,
        body: list[Node],
        function_line: int,
        is_fastcall: bool,
        parameters: list[Param],
        regparm_count: int,
        regparm_registers: tuple[str, ...],
        register_convention: bool,
    ) -> None:
        """Emit the per-function prologue (push bp / mov bp,esp / sub sp,N).

        Spills caller-supplied fastcall regparm registers and ``in_register``
        params into their local stack slots, then loads any pinned-but-not-
        in_register parameters from the caller-pushed cdecl slots into their
        target registers.  Extracted from :meth:`generate_function`; called
        only when ``self.elide_frame`` is False.
        """
        for reg in self.current_preserve_registers:
            self.emit(f"        push {reg}")
        self.emit(f"        push {self.target.base_register}")
        self.emit(f"        mov {self.target.base_register}, {self.target.stack_register}")
        if self.frame_size > 0:
            self.emit(f"        sub {self.target.stack_register}, {self.frame_size}")
        if is_fastcall:
            # Spill the caller-supplied regparm registers into their
            # local slots so the body can read them through the normal
            # local path.
            for i, register in enumerate(regparm_registers):
                slot = self.locals[parameters[i].name]
                self.emit(f"        mov [{self.target.base_register}-{slot}], {register}")
        for param in parameters:
            if param.in_register is not None:
                if not self._param_slot_is_read(body, param.name):
                    continue  # named register holds the value; skip the dead spill
                slot = self.locals[param.name]
                # Zero-extend narrower in_register values into the
                # full int-width slot so subsequent reads (which load
                # the whole slot via the accumulator) don't pick up
                # uninitialised stack bytes.  For full-width E-register
                # pins the named register already covers the slot.
                #
                # Byte-typed parameters (``char`` / ``unsigned char``) treat
                # the named register as the *byte* alias — only AL is
                # the value, AH is undefined per the asm-side calling
                # convention (e.g. ``lodsb; call f``).  Widening from
                # the byte alias scrubs AH-garbage out of the spilled
                # slot.  Pinning a byte-typed parameter to a register
                # without a byte alias (esi / edi / ebp / esp) is
                # rejected at codegen time.
                if param.type in self.BYTE_TYPES:
                    source = self.target.low_byte(param.in_register)
                    if source is None:
                        message = (
                            f"byte-typed parameter '{param.name}' cannot be pinned to register "
                            f"'{param.in_register}' — no low-byte alias in the target encoding"
                        )
                        raise CompileError(message, line=function_line)
                    self.emit(f"        movzx {self.target.acc}, {source}")
                    self.emit(f"        mov [{self.target.base_register}-{slot}], {self.target.acc}")
                    continue
                widened = self.target.widen_gp(param.in_register)
                if widened != param.in_register:
                    self.emit(f"        movzx {widened}, {param.in_register}")
                    self.emit(f"        mov [{self.target.base_register}-{slot}], {widened}")
                else:
                    self.emit(f"        mov [{self.target.base_register}-{slot}], {param.in_register}")
        if not register_convention:
            # Load pinned parameters from caller-pushed stack slots
            # into their registers.
            caller_push_index = 0
            for i, param in enumerate(parameters):
                if is_fastcall and i < regparm_count:
                    continue
                if param.out_register is not None:
                    continue
                if param.name in self.pinned_register:
                    register = self.pinned_register[param.name]
                    offset = self.target.param_slot_base + caller_push_index * self.target.int_size
                    self.emit(f"        mov {register}, [{self.target.base_register}+{offset}]")
                caller_push_index += 1

    def _emit_main_exit_tail(self) -> None:
        """Emit ``main``'s implicit-exit tail and any elided-frame local BSS cells.

        Called from :meth:`generate_function` at the bottom of ``main``.
        Sets the exit code to 0 (an explicit ``return N;`` earlier in the
        body has already loaded the accumulator and jumped, so reaching
        here means control fell off without a return), jumps to
        ``FUNCTION_EXIT`` in libbboeos, and — when the frame is elided —
        lays down each local's storage cell.

        In flat mode the cells are emitted inline at the tail of the
        function (zeros sit in ``.text`` under ``org 08048000h`` and the
        program loader skips them).  In object mode they're collected
        into ``self.elided_local_bss_vars`` and laid down later in
        ``section .bss`` via ``resb`` reservations, so ``.text`` stays
        code-only and the linker can pack ``.text`` from multiple
        objects without dragging zero pads between them.
        """
        self.emit(f"        xor {self.target.acc}, {self.target.acc}")
        self._emit_libbboeos_jmp("FUNCTION_EXIT")
        if not self.elide_frame:
            return
        # Plain int / pointer locals get the target's native integer
        # width (``dw`` / ``dd``); ``unsigned long`` always stays 4
        # bytes (``dd``) regardless of mode; byte-scalar locals always
        # stay 1 byte (``db``); local stack arrays reserve their full
        # byte count.
        int_directive = "dd 0" if self.target.int_size == 4 else "dw 0"
        for vname in sorted(self.locals):
            if vname in self.local_stack_arrays:
                byte_count = self.local_stack_arrays[vname]
                if self.object_mode:
                    self.elided_local_bss_vars.append((vname, str(byte_count)))
                else:
                    self.emit(f"_l_{vname}: times {byte_count} db 0")
            elif self.variable_types.get(vname) == "unsigned long":
                if self.object_mode:
                    self.elided_local_bss_vars.append((vname, "4"))
                else:
                    self.emit(f"_l_{vname}: dd 0")
            elif vname in self.byte_scalar_locals:
                if self.object_mode:
                    self.elided_local_bss_vars.append((vname, "1"))
                else:
                    self.emit(f"_l_{vname}: db 0")
            elif self.variable_types.get(vname, "").startswith("struct ") and not self.variable_types[vname].endswith("*"):
                type_name = self.variable_types[vname]
                tag = type_name[7:]
                struct_byte_count = self.struct_sizes[tag]
                if self.object_mode:
                    self.elided_local_bss_vars.append((vname, str(struct_byte_count)))
                else:
                    self.emit(f"_l_{vname}: times {struct_byte_count} db 0")
            elif self.object_mode:
                self.elided_local_bss_vars.append((vname, str(self.target.int_size)))
            else:
                self.emit(f"_l_{vname}: {int_directive}")

    def _emit_pointer_bump(self, *, delta: int, line: int, name: str) -> None:
        """Advance pointer variable ``name`` by ``delta * sizeof(*name)`` bytes in place.

        Used by :class:`DerefIncrement` / :class:`DerefIncrementAssign`
        codegen to bump the pointer *without* touching the accumulator —
        either ``inc reg`` / ``add reg, N`` for a pinned-register
        pointer, or ``inc dword [ebp-N]`` / ``add dword [ebp-N], N`` for
        a frame-slot pointer.  Pointee width comes from the recorded
        pointer type (``char *`` → 1, ``unsigned short *`` → 2, anything else
        → ``int_size``).  Skips the accumulator-tracking invalidation
        the caller needs (so the freshly-loaded ``*p`` value remains
        valid in acc); the caller decides whether to ``ax_clear()``.
        """
        holder_type = self.variable_types.get(name)
        if not holder_type or not holder_type.endswith("*"):
            message = f"postfix '*{name}++' / '*{name}--' requires a pointer; got '{holder_type}'"
            raise CompileError(message, line=line)
        pointee_type = holder_type[:-1].rstrip()
        try:
            pointee_size = self.target.type_size(pointee_type) if pointee_type else 1
        except KeyError:
            pointee_size = self.target.int_size
        bump = pointee_size * delta
        operation = "add" if bump >= 0 else "sub"
        amount = abs(bump)
        if name in self.pinned_register:
            register = self.pinned_register[name]
            if amount == 1:
                self.emit(f"        {'inc' if bump > 0 else 'dec'} {register}")
            else:
                self.emit(f"        {operation} {register}, {amount}")
        else:
            address = self._local_address(name)
            width = self.target.word_size
            if amount == 1:
                self.emit(f"        {'inc' if bump > 0 else 'dec'} {width} [{address}]")
            else:
                self.emit(f"        {operation} {width} [{address}], {amount}")

    def _emit_rep_fill(self, *, element_size: int) -> None:
        """Emit cld + rep stos{b,w,d}. EDI/EAX/ECX preloaded by caller."""
        self.emit("        cld")
        self.emit(f"        rep stos{self._rep_width_suffix(element_size)}")

    def _emit_rep_move(self, *, element_size: int) -> None:
        """Emit cld + rep movs{b,w,d}. EDI/ESI/ECX preloaded by caller."""
        self.emit("        cld")
        self.emit(f"        rep movs{self._rep_width_suffix(element_size)}")

    def _emit_scale_index(self, register: str, /, *, scale: int) -> None:
        """Multiply *register* by *scale* (1, 2, or 4) in place.

        Scale 1 is a no-op (byte stride); 2 emits ``add reg, reg``; 4
        emits ``shl reg, 2``.  Other widths fall back to ``imul``.

        The fallback uses the two-operand ``imul reg, imm`` form (NASM
        encodes it identically to the three-operand ``imul reg, reg, imm``
        — e.g. both ``6B C0 26`` for stride 38).  The self-hosted assembler
        in ``user/programs/asm.c`` only parses the two-operand spelling, so
        emitting the three-operand form here breaks the asm.c self-host when
        a struct-array stride (such as ``sizeof(struct Symbol)`` = 38) lands
        in this branch.
        """
        if scale == 1:
            return
        if scale == 2:
            self.emit(f"        add {register}, {register}")
        elif scale == 4:
            self.emit(f"        shl {register}, 2")
        else:
            self.emit(f"        imul {register}, {scale}")

    def _emit_scale_int_index(self, register: str, /) -> None:
        """Multiply *register* by ``self.target.int_size`` (2 or 4) in place.

        Converts an integer subscript into a byte offset when stepping
        through an array of word- or dword-sized elements.  16-bit
        doubles via ``add reg, reg``; 32-bit uses ``shl reg, 2`` so the
        4x stride lands in one instruction instead of two.
        """
        self._emit_scale_index(register, scale=self.target.int_size)

    def _emit_struct_initializer(self, name: str, init: StructInitializer) -> None:
        """Emit zero-store prelude + per-field assignments for a struct local.

        Accepts both the designated form (``{.field = expr, ...}``) and
        the positional form (``{a, b, c}``); positional values are
        matched to fields in declaration order via the struct layout.
        """
        type_name = self.variable_types[name]
        if not type_name.startswith("struct ") or "[" in type_name:
            message = f"initializer on non-struct or array local '{name}' is not supported"
            raise CompileError(message, line=init.line)
        tag = type_name[7:]
        size = self.struct_sizes[tag]
        frame_offset = self.locals[name]
        # Zero-store prelude: one ``mov byte [ebp-K], 0`` per byte of the slot.
        for byte_index in range(size):
            if byte_index == 0:
                address = f"[ebp-{frame_offset}]"
            else:
                address = f"[ebp-{frame_offset}+{byte_index}]"
            self.emit(f"        mov byte {address}, 0")
        if init.designated is not None:
            assignments = list(init.designated.items())
        else:
            assert init.positional is not None
            field_names = list(self.struct_layouts[tag].keys())
            if len(init.positional) > len(field_names):
                message = f"too many initializers for 'struct {tag}'"
                raise CompileError(message, line=init.line)
            assignments = list(zip(field_names, init.positional, strict=False))
        # Per-field assignments via the Place store codegen path.
        # Synthesize ``name.field = value`` as a PlaceStore over a
        # MemberPlace and emit it directly.
        for field_name, value_node in assignments:
            place = MemberPlace(
                base=VariablePlace(line=init.line, name=name),
                line=init.line,
                member_name=field_name,
            )
            self._emit_place_store(place, value_node)

    def _emit_switch_interleaved_arms(
        self,
        *,
        case_label_node: type[Char | Int],
        default_case: SwitchCase | ir.SwitchCase | None,
        discriminant: Node,
        discriminant_line: int,
        emit_body: Callable[[list[Node]], None],
        groups: list[list],
        label_index: int,
    ) -> None:
        """Emit interleaved-dispatch switch bodies (cmp/jne/body per group)."""
        for group_index, group in enumerate(groups):
            body_label = f".switch_{label_index}_case_{group_index}"
            next_label = f".switch_{label_index}_next_{group_index}"
            for case in group[:-1]:
                # Leading multi-label entries: jump TO the shared body
                # on match (the terminal case's jne will fall through
                # to .next if all labels miss).
                true_jump = BinaryOperation(
                    left=discriminant,
                    line=discriminant_line,
                    operation="==",
                    right=case_label_node(line=discriminant_line, value=case.value),
                )
                self.emit_condition_true_jump(condition=true_jump, context="switch", success_label=body_label)
            # Terminal label of the group: jne to next group on mismatch.
            terminal = group[-1]
            skip_jump = BinaryOperation(
                left=discriminant,
                line=discriminant_line,
                operation="!=",
                right=case_label_node(line=discriminant_line, value=terminal.value),
            )
            self.emit_condition_true_jump(condition=skip_jump, context="switch", success_label=next_label)
            if len(group) > 1:
                self.emit(f"{body_label}:")
            self.ax_clear()
            emit_body(terminal.body)
            self.emit(f"{next_label}:")
        if default_case is not None:
            self.ax_clear()
            emit_body(default_case.body)

    def _emit_switch_separated_arms(
        self,
        *,
        case_arms: list,
        case_labels: list[str],
        default_case: SwitchCase | ir.SwitchCase | None,
        default_label: str,
        emit_body: Callable[[list[Node]], None],
    ) -> None:
        """Emit separated-dispatch switch bodies (each arm at its label)."""
        for case, arm_label in zip(case_arms, case_labels, strict=True):
            self.emit(f"{arm_label}:")
            self.ax_clear()
            emit_body(case.body)
        if default_case is not None:
            self.emit(f"{default_label}:")
            self.ax_clear()
            emit_body(default_case.body)

    def _generate_assign_expr(self, expression: AssignExpr, /) -> None:
        """Lower an :class:'AssignExpr' (parenthesised assignment as an rvalue).

        Extracted from :meth:`generate_expression` to keep that method readable.
        """
        # Parenthesized assignment used as an expression (AST path — only
        # reached for ``main`` and other functions that bypass the IR
        # builder).  The IR path handles ``AssignExpr`` correctly via
        # ``cc.ir.Builder._lower_assign_expr`` (evaluates the RHS into a
        # temp, rewrites the *Assign, emits it as a statement).
        #
        # For the AST path: every store function evaluates the RHS into AX
        # first and writes AX to the destination.  After the store, AX
        # physically holds the stored value even when ``ax_clear()`` was
        # called internally (that call only clears the *tracking* metadata,
        # not the register).  Calling the store function directly (not
        # through ``generate_statement``, which appends another
        # ``ax_clear()``) leaves AX = assigned value as required.
        # Plain ``Assign`` is a special case: ``emit_store_local`` does the
        # evaluation + store and tracks ``ax_local``; reading the variable
        # via ``generate_expression(Var(...))`` is then a no-op.
        inner = expression.inner
        if isinstance(inner, Assign):
            self._check_defined(inner.name, line=inner.line)
            self.emit_store_local(expression=inner.expr, name=inner.name)
            self.generate_expression(Var(line=inner.line, name=inner.name))
        elif isinstance(inner, DerefIncrementAssign):
            # The statement path evaluates expr → AX, stores, bumps the
            # pointer, then clears AX tracking.  The
            # assigned value (the expr value, not the post-bump pointer) is
            # still physically in AX.  No re-evaluation needed.
            self.generate_statement(inner)
        elif isinstance(inner, IndexAssign):
            self.generate_index_assign(inner)
        elif isinstance(inner, PlaceStore):
            if self._place_targets_bitfield(inner.place):
                message = "assignment-as-expression to bitfield fields is not supported"
                raise CompileError(message, line=expression.line)
            self._emit_place_store(inner.place, inner.value)
            # ``(*p = v)`` (standalone DereferencePlace of a named pointer)
            # re-evaluates a trivial (Int / Var) RHS after the store to
            # re-establish the accumulator-tracking metadata.  Member-shape
            # PlaceStores never did this, so it is scoped to the deref case
            # to stay byte-identical to legacy.
            if isinstance(inner.place, DereferencePlace) and isinstance(inner.place.pointer, Var) and isinstance(inner.value, (Int, Var)):
                self.generate_expression(inner.value)
        else:
            message = f"AssignExpr: unsupported inner node type '{type(inner).__name__}'"
            raise CompileError(message, line=expression.line)

    def _generate_binary_operation_expression(self, expression: BinaryOperation, /) -> None:
        """Lower a :class:`BinaryOperation` expression into the accumulator.

        Extracted from :meth:`generate_expression` to keep that method
        readable.  Handles constant-fold short-circuit, pointer-arithmetic
        scaling, immediate / pinned-register fast paths for ``+``/``-``/
        ``&``/``|``/``^``/``<<``/``>>``, the byte-scalar split for ``+``/
        ``-``, and the general CX-scratch path with optional save/restore
        plus the comparison / division / multiplication tails.
        """
        # Fold an entirely-constant subtree (named constants and
        # integer literals) into a single ``mov ax, <expr>`` so the
        # assembler does the arithmetic.  Without this, expressions
        # like ``O_WRONLY + O_CREAT + O_TRUNC`` build the value at
        # runtime via push/pop chains.
        if (constant_expr := self._constant_expression(expression)) is not None:
            for name in self._collect_constant_references(expression):
                self.emit_constant_reference(name)
            self.emit(f"        mov {self.target.acc}, {constant_expr}")
            self.ax_clear()
            return
        operator, left, right = expression.operation, expression.left, expression.right
        # x86 SIB-addressing fast path for ``ptr + i`` / ``&arr[i]``:
        # when the index is a Var pinned to a non-acc register and the
        # element size is x86-encodable (1, 2, 4, 8), one ``lea`` collapses
        # the whole base-plus-scaled-index computation.  Without this the
        # codegen builds the address through ``generate_expression(left) /
        # push acc / generate_expression(right) / shl acc, k / mov cx, acc /
        # pop acc / add acc, cx`` — 7 instructions where ``mov acc, base /
        # lea acc, [acc + idx*k]`` does the same in 2.  Gated on 32-bit-or-
        # wider (16-bit addressing forms reject SIB).
        if (
            operator == "+"
            and isinstance(left, Var)
            and isinstance(right, Var)
            and right.name in self.pinned_register
            and self.pinned_register[right.name] != self.target.acc
            and self.target.int_size >= 4
        ):
            # Restrict to element_size >= 2: the element_size == 1 case
            # already lowers to ``add acc, idx`` (2 bytes) via the pinned-
            # register fast path below, which beats lea's 3-byte SIB encoding.
            element_size = self._arithmetic_element_size(left.name)
            if element_size in (2, 4, 8):
                self.generate_expression(left)
                idx_reg = self.pinned_register[right.name]
                self.emit(f"        lea {self.target.acc}, [{self.target.acc}+{idx_reg}*{element_size}]")
                self.ax_clear()
                return
        # Pointer arithmetic: scale the right operand by the element size when
        # the left side is a pointer or array variable.  ptr + N → ptr + N*sizeof(*ptr).
        # For byte pointers (char*, unsigned char*) element_size is 1 so nothing changes.
        if operator in ("+", "-") and isinstance(left, Var):
            element_size = self._arithmetic_element_size(left.name)
            if element_size > 1:
                right = BinaryOperation(left=right, operation="*", right=Int(value=element_size))
        if operator == "%" and self._has_remainder(left, right):
            self.emit(f"        mov {self.target.acc}, {self.target.dx_register}")
            self.ax_clear()
            return
        if operator in ("+", "-", "&", "|", "^") and isinstance(right, Int):
            # Fast path: reg operation imm uses the immediate form, skipping
            # the mov-into-cx scratch step.  Saves 2-3 bytes per site.
            self.generate_expression(left)
            # +1 and -1 fit in a 1-byte inc/dec.
            if operator == "+" and right.value == 1:
                self.emit(f"        inc {self.target.acc}")
            elif operator == "-" and right.value == 1:
                self.emit(f"        dec {self.target.acc}")
            elif operator == "^" and (right.value & 0xFFFF) == 0xFFFF and isinstance(self.target, X86CodegenTarget16):
                # ``x ^ 0xFFFF`` is the ``~x`` lowering — ``not ax``
                # is 2 bytes vs. 3 for ``xor ax, 0xFFFF``.
                self.emit(f"        not {self.target.acc}")
            else:
                mnemonic = {"+": "add", "-": "sub", "&": "and", "|": "or", "^": "xor"}[operator]
                self.emit(f"        {mnemonic} {self.target.acc}, {right.value}")
            self.ax_clear()
            return
        if operator == "<<" and isinstance(right, Int):
            shift = right.value & 0x1F
            # Fast path: shl r, imm — one instruction, no CX scratch.
            self.generate_expression(left)
            if shift == 0:
                pass
            elif shift >= self.target.int_size * 8:
                self.emit(f"        xor {self.target.acc}, {self.target.acc}")
            else:
                self.emit(f"        shl {self.target.acc}, {shift}")
            self.ax_clear()
            return
        if operator == ">>" and isinstance(right, Int):
            shift = right.value & 0x1F
            # Special case: `local >> 8` when ``local`` lives in memory.
            # Loading the high byte directly avoids one instruction
            # over `mov ax, [local]` + `shr ax, 8`, and doesn't waste
            # an ALU operation on a shift that's really a byte-select.
            # Byte-scalar locals / globals have no high byte — their
            # storage is a single ``db`` cell, so bail to the general
            # shift path (which loads zero).
            if (
                shift == 8
                and isinstance(self.target, X86CodegenTarget16)
                and isinstance(left, Var)
                and self._is_memory_scalar(left.name)
                and left.name not in self.pinned_register
                and left.name not in self.array_labels
                and not self._is_byte_scalar(left.name)
            ):
                self.emit_byte_load_zx(f"[{self._local_address(left.name)}+1]")
                self.ax_clear()
                return
            # Fast path: shr r, imm — one instruction, no CX scratch.
            self.generate_expression(left)
            if shift == 0:
                pass
            elif shift >= self.target.int_size * 8:
                self.emit(f"        xor {self.target.acc}, {self.target.acc}")
            else:
                self.emit(f"        shr {self.target.acc}, {shift}")
            self.ax_clear()
            return
        if operator == "*" and isinstance(right, Int):
            n = right.value
            self.generate_expression(left)
            if n == 0:
                self.emit(f"        xor {self.target.acc}, {self.target.acc}")
            elif n > 0 and (n & (n - 1)) == 0:
                shift = (n).bit_length() - 1
                if shift > 0:
                    self.emit(f"        shl {self.target.acc}, {shift}")
            else:
                self.emit(f"        imul {self.target.acc}, {n}")
            self.ax_clear()
            return
        # Fast path for ``+`` / ``-`` with a stack-resident right
        # operand: ``add ax, [mem]`` is shorter than ``mov cx,
        # [mem] / add ax, cx``.  Logical ops could take the same
        # shape, but expanding handle_and / handle_or / handle_xor
        # in the self-host assembler to accept ``r, [reg+disp]``
        # costs more bytes in asm.c than the ~74 bytes reclaimed
        # across the 37 eligible callsites, so those stay on the
        # CX fallback path.
        if (
            operator in ("+", "-")
            and isinstance(right, Var)
            and self._is_memory_scalar(right.name)
            and right.name not in self.pinned_register
            and right.name not in self.variable_arrays
            and self.variable_types.get(right.name) != "unsigned long"
            and not self._is_byte_scalar(right.name)
        ):
            self.generate_expression(left)
            mnemonic = "add" if operator == "+" else "sub"
            self.emit(f"        {mnemonic} {self.target.acc}, [{self._local_address(right.name)}]")
            self.ax_clear()
            return
        # Byte-scalar right operand for ``+`` / ``-``: a word-
        # sized ``add ax, [mem]`` / ``sub ax, [mem]`` would read
        # the adjacent byte into the high byte, so split into
        # ``add al, [mem] / adc ah, 0`` (or ``sub`` / ``sbb``).
        # The byte-wide operation on AL with the carry / borrow propagate
        # on AH matches word semantics for an unsigned-byte
        # operand: its high byte is known zero, so adding or
        # subtracting zero from AH and folding in the carry /
        # borrow out of AL produces the same 16-bit result as
        # the word operation would.  5 bytes vs 11+ bytes of the CX
        # fallback.
        if (
            operator in ("+", "-")
            and isinstance(right, Var)
            and self._is_byte_scalar(right.name)
            and right.name not in self.variable_arrays
        ):
            self.generate_expression(left)
            address = self._local_address(right.name)
            if operator == "+":
                self.emit(f"        add al, [{address}]")
                self.emit("        adc ah, 0")
            else:
                self.emit(f"        sub al, [{address}]")
                self.emit("        sbb ah, 0")
            self.ax_clear()
            return
        # Fast path for ``+``/``-``/``&``/``|``/``^`` with a
        # pinned-register right operand: arithmetic targets the
        # register directly, skipping the `mov cx, <reg>` load and
        # any CX save/restore.  When the pinned register is CX,
        # require ``left`` to be a leaf so generate_expression
        # can't clobber it mid-compute.
        if operator in ("+", "-", "&", "|", "^") and isinstance(right, Var) and right.name in self.pinned_register:
            source = self.pinned_register[right.name]
            if source != self.target.count_register or isinstance(left, (Int, Var, String)):
                self.generate_expression(left)
                mnemonic = {"+": "add", "-": "sub", "&": "and", "|": "or", "^": "xor"}[operator]
                if len(source) < len(self.target.acc):
                    # 16-bit pinned reg into 32-bit acc: push into count_register first.
                    self.emit(f"        movzx {self.target.count_register}, {source}")
                    self.emit(f"        {mnemonic} {self.target.acc}, {self.target.count_register}")
                else:
                    self.emit(f"        {mnemonic} {self.target.acc}, {source}")
                self.ax_clear()
                return
        count_pinned_var = next(
            (name for name, register in self.pinned_register.items() if register == self.target.count_register),
            None,
        )
        # Skip the CX save when an enclosing store is about to
        # overwrite CX anyway — its original value is dead.
        protect_count = count_pinned_var is not None and self.store_target_register != self.target.count_register
        if protect_count:
            self.emit(f"        push {self.target.count_register}")
        self.emit_binary_operator_operands(left, right)  # AX = left, CX = right
        if operator == "+":
            self.emit(f"        add {self.target.acc}, {self.target.count_register}")
        elif operator == "-":
            self.emit(f"        sub {self.target.acc}, {self.target.count_register}")
        elif operator == "&":
            self.emit(f"        and {self.target.acc}, {self.target.count_register}")
        elif operator == "|":
            self.emit(f"        or {self.target.acc}, {self.target.count_register}")
        elif operator == "^":
            self.emit(f"        xor {self.target.acc}, {self.target.count_register}")
        elif operator == "<<":
            self.emit(f"        shl {self.target.acc}, cl")
        elif operator == ">>":
            self.emit(f"        shr {self.target.acc}, cl")
        elif operator == "*":
            protect_dx = (
                any(register == self.target.dx_register for register in self.pinned_register.values())
                and self.store_target_register != self.target.dx_register
            )
            if protect_dx:
                self.emit(f"        push {self.target.dx_register}")
            self.emit(f"        mul {self.target.count_register}")
            if protect_dx:
                self.emit(f"        pop {self.target.dx_register}")
            self.division_remainder = None
        elif operator in {"/", "%"}:
            dx_pinned = any(register == self.target.dx_register for register in self.pinned_register.values())
            protect_dx = dx_pinned and self.store_target_register != self.target.dx_register
            if protect_dx:
                self.emit(f"        push {self.target.dx_register}")
            self.emit(f"        xor {self.target.dx_register}, {self.target.dx_register}")
            self.emit(f"        div {self.target.count_register}")
            if operator == "%":
                self.emit(f"        mov {self.target.acc}, {self.target.dx_register}")
            if protect_dx:
                self.emit(f"        pop {self.target.dx_register}")
            if dx_pinned:
                self.division_remainder = None
            else:
                self.division_remainder = (left, right)
        elif operator in JUMP_WHEN_FALSE:
            # Booleanize the comparison: AX = 1 if ``left <operation> right``,
            # else 0.  ``mov ax, 0`` preserves the flags set by ``cmp``
            # (unlike ``xor ax, ax``), so the jump-when-false branch
            # reads the right condition.
            skip_label = f".bool_{self.new_label()}"
            self.emit(f"        cmp {self.target.acc}, {self.target.count_register}")
            self.emit(f"        mov {self.target.acc}, 0")
            table = JUMP_WHEN_FALSE_UNSIGNED if self._is_unsigned_comparison(left, right) else JUMP_WHEN_FALSE
            self.emit(f"        {table[operator]} {skip_label}")
            self.emit(f"        inc {self.target.acc}")
            self.emit(f"{skip_label}:")
        else:
            message = f"unknown operator: {operator}"
            raise CompileError(message, line=expression.line)
        if protect_count:
            self.emit(f"        pop {self.target.count_register}")
        self.ax_clear()

    def _generate_conditional(self, expression: Conditional, /) -> None:
        """Lower a ternary ``c ? t : e`` to a conditional branch.

        Evaluates the condition; jumps over the then-branch when false;
        evaluates the chosen branch (only one fires) and leaves the
        result in AX/EAX.  The condition is normalised the same way
        ``parse_condition`` normalises ``if`` / ``while`` heads — bare
        expressions become ``expr != 0`` so the shared
        :meth:`emit_condition_false_jump` machinery handles short-
        circuit ``&&`` / ``||`` and carry-flag callees uniformly.

        Both branches reach the same end label, so AX-tracking
        (``ax_local`` / ``ax_is_byte``) is cleared after the merge:
        whichever branch the actual control flow took, the merge
        point can't promise that AX still holds the then-branch's
        value tag.

        Fast path for ``MAX(a, b)`` / ``MIN(a, b)`` macro expansion:
        when the then-branch is structurally identical to the
        comparison's left operand (and pure — no calls), ``AX`` will
        already hold the desired value after :meth:`emit_condition`,
        so the then-branch's re-evaluation is elided and we jump
        directly to the end label on cond-true.  This collapses
        ``MIN(total_length - logical_offset, 512)`` to the same
        compact ``cmp / Jcc / mov ax, 512`` that the hand-written
        ``if`` saturation would emit, without a redundant
        ``sub`` second time around.
        """
        condition = self._normalise_ternary_condition(expression.condition)
        if self._try_emit_conditional_via_cond_value(condition=condition, expression=expression):
            return
        if self._try_emit_conditional_via_cmov(condition=condition, expression=expression):
            return
        label_index = self.new_label()
        else_label = f".cond_else_{label_index}"
        end_label = f".cond_end_{label_index}"
        self.emit_condition_false_jump(condition=condition, context="ast", fail_label=else_label)
        self.generate_expression(expression.then_expr)
        self.emit(f"        jmp {end_label}")
        self.emit(f"{else_label}:")
        # Else-branch enters from the conditional jump; AX state
        # accumulated by the then-branch is invalid here.
        self.ax_clear()
        self.generate_expression(expression.else_expr)
        self.emit(f"{end_label}:")
        # At the merge, AX holds the result of whichever branch ran;
        # neither branch's variable-tracking is guaranteed.
        self.ax_clear()

    def _generate_index_expression(self, expression: Index, /) -> None:
        """Lower an :class: (array subscript) rvalue load into the accumulator.

        Extracted from :meth:`generate_expression` to keep that method readable.
        """
        # Defer ``ax_clear`` until each emit path's tail so that an
        # index expression naming the AX-resident var (the
        # ``temp = X; arr[temp]`` IR-temp pattern, where the prior
        # store left ``ax_local`` == ``temp``) can short-circuit
        # its reload via :meth:`generate_expression`'s
        # ``Var(name=ax_local)`` fast path.
        # Nested-Index array base: an array-of-pointers read whose own array
        # is itself a subscript expression (e.g. ``grid[i0][i1]`` feeding the
        # next ``[i2]``), produced by the arbitrary-depth array-of-pointers
        # reconstruct.  The inner Index evaluates to a pointer in the
        # accumulator; index off it with the pointee stride.  No ``vname``
        # exists (there is no Var base), so this MUST precede the name-based
        # paths below.
        if isinstance(expression.array, Index):
            self._generate_nested_index_expression(expression)
            return
        vname = expression.array.name
        if vname in self.pointer_array_types:
            # A single subscript of a pointer-to-array ``int (*p)[3]`` is a
            # partial subscript (it would yield a row pointer, not an element).
            # Only a FULL subscript ``p[i][j]...`` is supported — that shape
            # arrives as a uniform SubscriptPlace chain, not a bare Index.
            message = f"unsupported partial subscript of pointer-to-array '{vname}'"
            raise CompileError(message, line=expression.line)
        index_expression = expression.index
        self._check_defined(vname, line=expression.line)
        # Pointee / element width selects the load encoding.  For
        # ``unsigned short *p`` on the 32-bit target ``mov eax, [esi]``
        # would read 4 bytes; we must emit ``movzx eax, word [esi]``
        # to read exactly the 2-byte element.  Constant-index
        # offsets also scale by the pointee width, not the target
        # int_size.  Byte loads stay on their dedicated fast path
        # (``emit_byte_load_zx``) because that also clears AH.
        is_byte = self._is_byte_var(vname)
        if vname in self.array_labels:
            pointee_size = self.target.int_size
        else:
            pointee_size = self._index_pointee_size(vname)
        # 4-byte pointee on a 16-bit target needs DX:AX; that's the
        # long-pointee case the IR routes through generate_long_expression
        # — fall back to the historical full-acc load here and let the
        # caller diagnose the type mismatch.  Otherwise, clamp the
        # load width to min(pointee_size, int_size).
        narrow_word = (not is_byte) and 1 < pointee_size < self.target.int_size

        emitter = self

        def _word_load(address: str) -> None:
            # Use the 16-bit alias of acc (``ax`` on 32-bit) to emit
            # ``movzx eax, word [...]``; on 16-bit acc is already 2
            # bytes so a plain ``mov ax, [...]`` is correct.
            if emitter.target.int_size > 2:
                emitter.emit(f"        movzx {emitter.target.acc}, word [{address}]")
            else:
                emitter.emit(f"        mov {emitter.target.acc}, [{address}]")

        if isinstance(index_expression, Int) and vname in self.array_labels:
            offset = index_expression.value * self.target.int_size
            label = self.array_labels[vname]
            addr = f"{label}+{offset}" if offset else label
            self.emit(f"        mov {self.target.acc}, [{addr}]")
        elif isinstance(index_expression, Int):
            if is_byte:
                stride = 1
            else:
                stride = pointee_size if narrow_word else self.target.int_size
            offset = index_expression.value * stride
            # Direct memory access for constant/aliased bases:
            # emit `mov ax, [CONST+N]` instead of `mov bx, CONST / mov ax, [bx+N]`.
            const_base = self._resolve_constant(vname)
            if const_base is not None:
                addr = f"{const_base}+{offset}" if offset else const_base
                if is_byte:
                    self.emit_byte_load_zx(f"[{addr}]")
                elif narrow_word:
                    _word_load(addr)
                else:
                    self.emit(f"        mov {self.target.acc}, [{addr}]")
            else:
                guarded = self._si_scratch_guard_begin(vname)
                self._emit_load_var(vname, register=self.target.si_register)
                si = self.target.si_register
                mem_inner = f"{si}+{offset}" if offset else si
                if is_byte:
                    self.emit_byte_load_zx(f"[{mem_inner}]")
                elif narrow_word:
                    _word_load(mem_inner)
                else:
                    self.emit(f"        mov {self.target.acc}, [{mem_inner}]")
                self._si_scratch_guard_end(guarded=guarded)
        else:
            const_base = self._resolve_constant(vname)
            if const_base is not None:
                self.emit_constant_reference(vname)
                guarded = self._si_scratch_guard_begin(vname)
                addr = self._emit_constant_base_index_addr(
                    const_base=const_base,
                    element_size=1 if is_byte else (pointee_size if narrow_word else self.target.int_size),
                    index=index_expression,
                    preserve_ax=False,
                )
                if is_byte:
                    self.emit_byte_load_zx(f"[{addr}]")
                elif narrow_word:
                    _word_load(addr)
                else:
                    self.emit(f"        mov {self.target.acc}, [{addr}]")
                self._si_scratch_guard_end(guarded=guarded)
            else:
                si = self.target.si_register
                # Index scaling: ``p[i]`` advances by sizeof(*p)
                # bytes per ``i``, so a narrow pointee (unsigned short* on
                # the 32-bit target) needs scale=2 not the acc's 4.
                if narrow_word:
                    scale_size = pointee_size
                elif is_byte:
                    scale_size = 1
                else:
                    scale_size = self.target.int_size
                # x86 SIB-addressing fast path: when the index lives in a
                # pinned register distinct from SI, fold ``acc = idx*k;
                # si += acc`` into the load's effective address.  Same
                # gating as the IndexAssign SIB write path
                # (``generate_index_assign``).
                pinned_index_register = (
                    self.pinned_register[index_expression.name]
                    if isinstance(index_expression, Var) and index_expression.name in self.pinned_register
                    else None
                )
                if (
                    pinned_index_register is not None
                    and pinned_index_register != si
                    and scale_size in (1, 2, 4, 8)
                    and self.target.int_size >= 4
                ):
                    guarded = self._si_scratch_guard_begin(vname)
                    self._emit_load_var(vname, register=si)
                    scale_suffix = "" if scale_size == 1 else f"*{scale_size}"
                    addr = f"{si}+{pinned_index_register}{scale_suffix}"
                    if is_byte:
                        self.emit_byte_load_zx(f"[{addr}]")
                    elif narrow_word:
                        _word_load(addr)
                    else:
                        self.emit(f"        mov {self.target.acc}, [{addr}]")
                    self._si_scratch_guard_end(guarded=guarded)
                    self.ax_clear()
                    return
                guarded = self._si_scratch_guard_begin(vname)
                self._emit_load_var(vname, register=si)

                def _scale(register: str, /) -> None:
                    if scale_size == 1:
                        return
                    if scale_size == 2:
                        emitter.emit(f"        add {register}, {register}")
                    elif scale_size == 4:
                        emitter.emit(f"        shl {register}, 2")
                    else:
                        # Two-operand imul (NASM-identical encoding to the
                        # three-operand form); the asm.c self-hosted assembler
                        # only parses ``imul reg, imm``.
                        emitter.emit(f"        imul {register}, {scale_size}")

                # If the index is a pinned variable and the access is
                # byte-sized, load it without clobbering SI.
                if is_byte and isinstance(index_expression, Var) and index_expression.name in self.pinned_register:
                    ireg = self.pinned_register[index_expression.name]
                    self.emit(f"        add {si}, {ireg}")
                elif isinstance(index_expression, (Var, Int)):
                    # Simple Var/Int load doesn't touch SI, so skip the
                    # push/pop round-trip.
                    self.generate_expression(index_expression)
                    if not is_byte:
                        _scale(self.target.acc)
                    self.emit(f"        add {si}, {self.target.acc}")
                else:
                    self.emit(f"        push {si}")
                    self.generate_expression(index_expression)
                    if not is_byte:
                        _scale(self.target.acc)
                    self.emit(f"        pop {si}")
                    self.emit(f"        add {si}, {self.target.acc}")
                if is_byte:
                    self.emit_byte_load_zx(f"[{si}]")
                elif narrow_word:
                    _word_load(si)
                else:
                    self.emit(f"        mov {self.target.acc}, [{si}]")
                self._si_scratch_guard_end(guarded=guarded)
        # AX now holds the subscript result regardless of branch — the
        # constant-index paths emit a direct load, the dynamic-index
        # paths consume the index via ``generate_expression`` (which
        # may set ``ax_local`` to the index var) and overwrite AX with
        # the loaded value.  Either way the pre-call ``ax_local`` is
        # stale and any tracking ``generate_expression`` set above is
        # for the index, not the result.
        self.ax_clear()

    def _generate_ir_switch(self, instruction: ir.Switch, /) -> None:
        """Lower an :class:`ir.Switch` instruction.

        Delegates to :meth:`generate_switch` with overrides for the
        case list (IR :class:`ir.SwitchCase` instances carrying lowered
        IR bodies), the body emitter (so each arm is lowered via
        :meth:`lower_ir_body`), and the end label (so the ``break``
        jumps the IR builder already emitted resolve to the correct
        target).  All optimizations (enum exhaustiveness, interleaved
        dispatch, discriminant hoisting, char-typed labels) carry
        over unchanged.
        """
        # ``generate_switch`` calls ``ax_clear()`` itself, but the
        # AST-path call site in ``generate_statement`` also clears AX
        # before invoking it.  Mirror that here so peephole / tracking
        # state is identical between the two paths.
        self.ax_clear()
        self.generate_switch(
            instruction.original_ast,
            cases_override=instruction.cases,
            emit_body=self.lower_ir_body,
            end_label_override=instruction.end_label,
        )

    def _generate_logical_value(self, expression: Node, /) -> None:
        """Materialize a ``LogicalAnd`` / ``LogicalOr`` into the accumulator as 0 or 1.

        cc.py used to handle short-circuit operators only in condition
        position (inside ``if`` / ``while`` / ``? :`` heads), so any
        expression-position use like ``int same = a && b;`` raised
        ``unknown expression: LogicalAnd``.  This helper reuses the
        existing :meth:`emit_condition_false_jump` /
        :meth:`emit_condition_true_jump` short-circuit machinery to
        leave the accumulator holding the C boolean value: 1 when the
        operand evaluates true, 0 otherwise.

        For ``&&`` we false-jump every leaf to a shared zero-label
        (matching how condition-position lowering already works), then
        fall through to set the accumulator to 1.  For ``||`` we
        true-jump every leaf to a shared one-label, falling through to
        set the accumulator to 0.
        """
        label_index = self.new_label()
        end_label = f".lbool_{label_index}_end"
        if isinstance(expression, LogicalAnd):
            zero_label = f".lbool_{label_index}_zero"
            self.emit_condition_false_jump(condition=expression, context="expr", fail_label=zero_label)
            self.emit(f"        mov {self.target.acc}, 1")
            self.emit(f"        jmp {end_label}")
            self.emit(f"{zero_label}:")
            self.emit(f"        xor {self.target.acc}, {self.target.acc}")
        else:
            one_label = f".lbool_{label_index}_one"
            self.emit_condition_true_jump(condition=expression, context="expr", success_label=one_label)
            self.emit(f"        xor {self.target.acc}, {self.target.acc}")
            self.emit(f"        jmp {end_label}")
            self.emit(f"{one_label}:")
            self.emit(f"        mov {self.target.acc}, 1")
        self.emit(f"{end_label}:")
        self.ax_clear()

    def _generate_nested_index_expression(self, expression: Index, /) -> None:
        """Lower ``<inner-index-expr>[i]`` where the array is itself an ``Index``.

        Produced by the arbitrary-depth array-of-pointers reconstruct
        (``_reconstruct_double_index_place``): the inner ``Index`` reads one
        pointer level of an array-of-pointers chain (``grid[i0][i1]``), and
        this outer subscript indexes the resulting pointer.  Evaluate the
        inner expression to a pointer in the accumulator, move it to SI, then
        add ``index * sizeof(*pointer)`` and load the element.  Element width
        derives from the inner expression's pointee type so a narrow or byte
        pointee sizes its scale and load correctly.
        """
        pointer_type = self._expression_type(expression.array)
        pointee_type = pointer_type[:-1].rstrip() if pointer_type.endswith("*") else pointer_type
        is_byte = pointee_type in self.BYTE_TYPES
        if is_byte:
            stride = 1
        elif pointee_type == "unsigned short" and self.target.int_size > 2:
            stride = 2
        else:
            stride = self.target.int_size
        si = self.target.si_register
        # Evaluate the inner array-of-pointers read (the base pointer).
        self.generate_expression(expression.array)
        self.emit(f"        mov {si}, {self.target.acc}")
        index_expression = expression.index
        if isinstance(index_expression, Int):
            offset = index_expression.value * stride
            address = f"{si}+{offset}" if offset else si
        else:
            self.emit(f"        push {si}")
            self.generate_expression(index_expression)
            if stride == 2:
                self.emit(f"        add {self.target.acc}, {self.target.acc}")
            elif stride == 4:
                self.emit(f"        shl {self.target.acc}, 2")
            elif stride not in (0, 1):
                self.emit(f"        imul {self.target.acc}, {self.target.acc}, {stride}")
            self.emit(f"        pop {si}")
            self.emit(f"        add {si}, {self.target.acc}")
            address = si
        if is_byte:
            self.emit_byte_load_zx(f"[{address}]")
        elif stride == 2:
            if self.target.int_size > 2:
                self.emit(f"        movzx {self.target.acc}, word [{address}]")
            else:
                self.emit(f"        mov {self.target.acc}, [{address}]")
        else:
            self.emit(f"        mov {self.target.acc}, [{address}]")
        self.ax_clear()

    def _generate_tail_dispatch_if(self, statement: If, /) -> None:
        """Emit an ``if/else`` where each branch's last call is a tail jmp.

        Used for ``naked`` dispatchers: both branches end the function
        via ``jmp <target>``, so the only labels needed are the else
        entry point.  No common end label, no fall-through ``jmp``
        skip-around, no ``ret`` after the structure.
        """
        label_index = self.new_label()
        self.emit_condition_false_jump(condition=statement.cond, context="if", fail_label=f".if_{label_index}_else")
        self.generate_body(statement.body[:-1], scoped=True)
        self.generate_call(statement.body[-1], tail_call=True)
        self.emit(f".if_{label_index}_else:")
        self.generate_body(statement.else_body[:-1], scoped=True)
        self.generate_call(statement.else_body[-1], tail_call=True)

    def _has_tail_dispatch_shape(self, body: list[Node], /) -> bool:
        """``body[-1]`` is an ``If/else`` whose branches both tail-call.

        Each branch's last statement must be a tail-call-eligible
        ``Call``; the whole ``if`` then becomes a register-preserving
        dispatcher (``cmp ... ; jcc .else ; ... ; jmp fn1 ; .else: ... ; jmp fn2``).
        Used for ``naked`` dispatchers like ``read_sector`` that pick
        between two drivers based on a flag byte.
        """
        if not body or not isinstance(body[-1], If):
            return False
        if_stmt = body[-1]
        if if_stmt.else_body is None:
            return False
        return (
            bool(if_stmt.body)
            and isinstance(if_stmt.body[-1], Call)
            and self._is_tail_call_eligible(if_stmt.body[-1])
            and bool(if_stmt.else_body)
            and isinstance(if_stmt.else_body[-1], Call)
            and self._is_tail_call_eligible(if_stmt.else_body[-1])
        )

    def _ir_value_to_ast(self, value: ir.Value) -> Node:
        """Convert an :data:`ir.Value` to the equivalent simple AST leaf node."""
        if isinstance(value, int):
            return Int(value=value)
        if isinstance(value, PlaceAddressOf):
            return value
        if value.startswith("_ir_s"):
            content = self._ir_string_map.get(value)
            if content is not None:
                return String(content=content)
        return Var(name=value)

    def _is_pure_expression(self, node: Node, /) -> bool:
        """Return True if evaluating *node* has no observable side effect.

        Conservative: only literals, variable / named-constant reads,
        struct-member reads, array indexing, address-of, sizeof, and
        arithmetic / comparison / logical / bitwise binary operations
        over pure operands qualify.  Anything that could ``call`` user
        code (``Call``, ``TailCall``) or that mutates state is
        rejected.  Used by :meth:`_try_emit_conditional_via_cond_value`
        to decide whether eliding the then-branch (which by the textual
        macro semantics would otherwise be re-evaluated) is safe.
        """
        if isinstance(node, (Int, SizeofExpr, SizeofType, SizeofVar, String, Var, PlaceAddressOf)):
            return True
        if isinstance(node, BinaryOperation):
            return self._is_pure_expression(node.left) and self._is_pure_expression(node.right)
        if isinstance(node, (LogicalAnd, LogicalOr)):
            return self._is_pure_expression(node.left) and self._is_pure_expression(node.right)
        if isinstance(node, Index):
            # ``arr[i]`` reads from memory but doesn't write; the index
            # itself must also be pure.
            return self._is_pure_expression(node.index)
        if isinstance(node, PlaceLoad):
            return self._place_is_pure(node.place)
        if isinstance(node, Conditional):
            return (
                self._is_pure_expression(node.condition)
                and self._is_pure_expression(node.then_expr)
                and self._is_pure_expression(node.else_expr)
            )
        return False

    def _is_tail_call_eligible(self, call: Call, /) -> bool:
        """Check whether a tail-call replacement (``jmp`` for ``call; ret``) is safe.

        Safe when:
        - ``elide_frame`` is True (no ``pop bp; ret`` teardown to emit).
        - callee is a user function (not a builtin with its own shape).
        - callee isn't an inline-asm splice target (we'd need the body
          inlined, not a jmp).
        - no pinned registers need saving at this call site — we'd
          never get a chance to restore them after the jmp.
        - no stack args — we can't ``add sp, N`` after a jmp either.
        """
        if not self.elide_frame:
            return False
        if call.name not in self.user_functions:
            return False
        if call.name in self.inline_bodies:
            return False
        clobbers: frozenset[str] = frozenset(self.target.register_pool)
        if self._pinned_registers_to_save(clobbers):
            return False
        callee_pins = self.user_function_pin_params.get(call.name, {}) if call.name in self.register_convention_functions else {}
        is_fastcall = call.name in self.fastcall_functions
        in_regs = self.in_register_params.get(call.name, {})
        out_regs = self.out_register_params.get(call.name, {})
        for index in range(len(call.args)):
            if is_fastcall and index == 0:
                continue
            if index in callee_pins:
                continue
            if index in in_regs or index in out_regs:
                continue
            return False  # stack arg — can't clean up after a jmp
        return True

    #: Canonical (16-bit-name) clobber sets for a recognized rep-string
    #: loop, keyed by operation.  Mirrors ``BUILTIN_CLOBBERS["memcpy"]`` /
    #: ``["memset"]`` exactly — a ``rep movs`` touches DI/SI/CX/AX (AX is
    #: cleared by the trailing ``ax_clear``), a ``rep stos`` touches
    #: DI/CX/AX.  ``_pinned_registers_to_save`` normalises both sides
    #: through ``target.low_word`` so these match E-register pins too.
    REP_STRING_CLOBBERS: ClassVar[dict[str, frozenset[str]]] = {
        "copy": frozenset({"ax", "cx", "di", "si"}),
        "fill": frozenset({"ax", "cx", "di"}),
    }

    def _lower_ir_instruction(self, instruction: ir.Instruction) -> None:
        match instruction:
            case ir.BinaryOperation(destination=destination, operation=operation, left=left, right=right):
                expression = BinaryOperation(left=self._ir_value_to_ast(left), operation=operation, right=self._ir_value_to_ast(right))
                self.emit_store_local(expression=expression, name=destination)
            case ir.Copy(destination=destination, source=source):
                self.emit_store_local(expression=self._ir_value_to_ast(source), name=destination)
            case ir.Call(destination=None, name=name, args=args):
                call = Call(args=[self._ir_value_to_ast(a) for a in args], name=name)
                self._current_call_pinned_initialized = self._ir_call_pinned_initialized.get(id(instruction))
                try:
                    self.generate_call(call, discard_return=True)
                finally:
                    self._current_call_pinned_initialized = None
                self.ax_clear()
            case ir.Call(destination=destination, name=name, args=args):
                call = Call(args=[self._ir_value_to_ast(a) for a in args], name=name)
                self._current_call_pinned_initialized = self._ir_call_pinned_initialized.get(id(instruction))
                try:
                    self.emit_store_local(expression=call, name=destination)
                finally:
                    self._current_call_pinned_initialized = None
            case ir.Index(destination=destination, base=base, index=index):
                expression = Index(array=Var(name=base), index=self._ir_value_to_ast(index))
                self.emit_store_local(expression=expression, name=destination)
            case ir.IndexAssign(base=base, index=index, source=source):
                stmt = IndexAssign(array=Var(name=base), expr=self._ir_value_to_ast(source), index=self._ir_value_to_ast(index))
                self.generate_index_assign(stmt)
            case ir.Label(name=name):
                # Control can arrive at an IR label from any preceding
                # branch / jump, so AX-tracking state (``ax_local`` /
                # ``ax_is_byte``) and SI-tracking (``si_local``)
                # accumulated on the fall-through path are not guaranteed
                # on the jump path.  Clear both.
                self.ax_clear()
                self.si_local = None
                self.emit(f"{name}:")
            case ir.Jump(target=target):
                self.emit(f"        jmp {target}")
            case ir.BranchFalse(left=left, operation=operation, right=right, target=target):
                condition = BinaryOperation(left=self._ir_value_to_ast(left), operation=operation, right=self._ir_value_to_ast(right))
                self.emit_condition_false_jump(condition=condition, context="ir", fail_label=target)
            case ir.CarryBranch(call_ast=call_ast, target=target, when=when):
                # Tight ``call X / jc target`` (when="set") or ``jnc``
                # (when="clear") for ``carry_return`` callees used in an
                # ``if`` / ``while`` condition.  ``generate_call`` sets
                # up args (regparm / stack) the same way a direct call
                # would.
                self._current_call_pinned_initialized = self._ir_call_pinned_initialized.get(id(instruction))
                try:
                    self.generate_call(call_ast, discard_return=True)
                finally:
                    self._current_call_pinned_initialized = None
                self.emit(f"        {'jc' if when == 'set' else 'jnc'} {target}")
                self.ax_clear()
            case ir.Return(value=value):
                stmt = Return(value=self._ir_value_to_ast(value) if value is not None else None)
                self.generate_return(stmt)
            case ir.TailCall(name=name, args=args):
                call = Call(args=[self._ir_value_to_ast(a) for a in args], name=name)
                self._current_call_pinned_initialized = self._ir_call_pinned_initialized.get(id(instruction))
                try:
                    if self._is_tail_call_eligible(call):
                        self.generate_call(call, tail_call=True)
                    else:
                        # Fall back to a regular ``call`` followed by a
                        # ``Return`` whose value is already in AX.
                        # ``_is_tail_call_eligible`` rejects calls with
                        # stack args, pinned saves, or non-user callees
                        # — the tail ``jmp`` would skip teardown those
                        # cases need, so let the normal call shape
                        # handle them.
                        self.generate_call(call)
                        self.generate_return(Return(value=None))
                finally:
                    self._current_call_pinned_initialized = None
            case ir.InlineAsm(content=content):
                for line in decode_string_escapes(content).splitlines():
                    self.emit(line)
            case ir.LoopBoundary(continue_label=continue_label, end_label=end_label, push=push):
                # ``continue_label=None`` marks a switch boundary: ``break``
                # targets the switch's end but ``continue`` keeps applying
                # to the enclosing loop (per C semantics), so we leave
                # the continue stack alone.
                if push:
                    if continue_label is not None:
                        self.loop_continue_labels.append(continue_label)
                    self.loop_end_labels.append(end_label)
                else:
                    if continue_label is not None:
                        self.loop_continue_labels.pop()
                    self.loop_end_labels.pop()
            case ir.RepString():
                self._current_call_pinned_initialized = self._ir_call_pinned_initialized.get(id(instruction))
                try:
                    self.generate_rep_string(instruction)
                finally:
                    self._current_call_pinned_initialized = None
            case ir.Switch():
                self._generate_ir_switch(instruction)
            case ir.Access(node=node) | ir.Block(node=node):
                self.generate_statement(node)

    def _node_contains_var(self, node: Node, name: str, /) -> bool:
        """Return True if node or any descendant is Var(name).

        Conservative: any str field equal to name is treated as a possible
        variable read so that nodes like Assign (which store the
        target's name as a plain str rather than a Var) are not silently
        missed.
        """
        if isinstance(node, Var):
            return node.name == name
        for field in fields(node):
            value = getattr(node, field.name)
            if isinstance(value, str) and value == name:
                return True
            if isinstance(value, Node) and self._node_contains_var(value, name):
                return True
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, Node) and self._node_contains_var(item, name):
                        return True
        return False

    @staticmethod
    def _normalise_ternary_condition(condition: Node) -> Node:
        """Wrap a ternary condition as ``expr != 0`` unless it's already a comparison.

        Mirrors :meth:`cc.parser.Parser.parse_condition`: ``&&`` / ``||``
        and explicit comparisons (``==`` / ``<`` / etc.) are passed
        through; everything else (a bare variable, an arithmetic
        expression, a call) is normalised to ``expr != 0`` so the
        downstream :meth:`emit_condition_false_jump` always sees a
        comparison-shaped node.
        """
        if isinstance(condition, (LogicalAnd, LogicalOr)):
            return condition
        if isinstance(condition, BinaryOperation) and condition.operation in COMPARISON_OPERATIONS:
            return condition
        return BinaryOperation(left=condition, line=condition.line, operation="!=", right=Int(line=condition.line, value=0))

    def _param_slot_is_read(self, body: list[Node], param_name: str, /) -> bool:
        """Return True if the local slot for param_name is read anywhere in body.

        Var refs that appear as direct TailCall arguments are excluded because
        change 3a sources those from the named in_register directly rather than
        loading from the stack slot.  Non-Var TailCall args are still walked.
        Conservative: any Var(param_name) in a non-TailCall-arg position is
        treated as a slot read and the spill is kept.
        """
        # Pure thunk: the body is exactly one TailCall.  Simple Var args
        # will be sourced from the named register (param_in_register), so
        # they do NOT require the slot.  Non-Var args are checked
        # conservatively — if any contain the param, keep the spill.
        if len(body) == 1 and isinstance(body[0], TailCall):
            return any(not isinstance(arg, Var) and self._node_contains_var(arg, param_name) for arg in body[0].args)
        # Non-pure-thunk: every reference to param_name — including
        # TailCall args — keeps the slot alive so the reload before
        # the tail jmp is valid and the named register's stale value
        # is never used.
        return any(self._node_contains_var(stmt, param_name) for stmt in body)

    def _place_is_pure(self, place: Place, /) -> bool:
        """Return True when reading *place* has no observable side effect.

        A standalone :class:`DereferencePlace` (a pointer read through an
        arbitrary address) is treated as IMPURE: the pointer reads and
        chained subscripts it models all report impure, and the
        conditional / guarded-update elision in
        :meth:`_try_emit_conditional_via_cond_value` relies on that to
        avoid eliding a re-evaluated branch.  Member shapes (dot, arrow,
        chained, struct-array, member-index) stay PURE, matching the
        legacy ``MemberAccess`` behavior — ``p->f`` is a ``MemberPlace``
        whose base is a ``DereferencePlace`` and stays pure.  A
        ``SubscriptPlace`` is pure only when its base chain does not
        bottom out at a standalone ``DereferencePlace`` (``name[i][j]``
        is impure; ``arr[i].field[j]`` / ``ptr->field[i]`` are pure).
        """
        if isinstance(place, VariablePlace):
            return True
        if isinstance(place, DereferencePlace):
            return False
        if isinstance(place, MemberPlace):
            return True
        if isinstance(place, SubscriptPlace):
            return self._place_is_pure(place.base)
        return False

    def _place_targets_bitfield(self, place: Place, /) -> bool:
        """Return True if *place*'s terminal member is a bitfield.

        Used to reject assignment-as-expression to a bitfield (the
        read-modify-write sequence clobbers AX, breaking the "AX holds
        the assigned value" contract).  Only a terminal member access
        can name a bitfield; any other place shape (subscript, plain
        variable, dereference) returns False.  Resolution is
        compile-time only (no code emitted) and any layout it cannot
        resolve is treated conservatively as non-bitfield.
        """
        if not isinstance(place, MemberPlace):
            return False
        try:
            base_type = self._place_type(place.base)
        except CompileError:
            return False
        if base_type.startswith("struct ") and base_type.endswith("*"):
            tag = base_type[7:-1].rstrip()
        elif base_type.startswith("struct "):
            tag = base_type[7:]
        else:
            return False
        layout = self.struct_layouts.get(tag)
        if layout is None:
            return False
        info = layout.get(place.member_name)
        if info is None:
            return False
        return info.bit_width is not None

    @staticmethod
    def _rep_width_suffix(element_size: int, /) -> str:
        """Map element size 1/2/4 to the string-op mnemonic suffix."""
        return {1: "b", 2: "w", 4: "d"}[element_size]

    @staticmethod
    def _substitute_extended_asm_template(
        text: str,
        /,
        *,
        name_to_index: dict[str, int],
        operand_byte_locations: list[str],
        operand_locations: list[str],
    ) -> str:
        """Substitute ``%%``/``%N``/``%[name]``/``%bN``/``%b[name]`` in a template string.

        Pulled out of :meth:`generate_extended_asm` (was a 74-line nested
        closure) so the operand-resolution algorithm can be read and
        modified independently of the surrounding parsing / register-
        spill orchestration.

        Args:
            text: the template string from the ``asm("...")`` first
                operand.
            name_to_index: maps a symbolic operand name (the
                ``[symname]`` form in the operand list) to its index
                in ``operand_locations`` / ``operand_byte_locations``.
            operand_locations: full-width register/memory operand for
                each declared output / input (outputs first, then
                inputs).
            operand_byte_locations: byte-register alias for the same
                operands; used for ``%b...`` substitutions.

        Recognised forms:
            ``%%``       → literal ``%``
            ``%N``       → positional full-width substitution
            ``%[name]``  → named full-width substitution
            ``%bN``      → positional byte-alias substitution
            ``%b[name]`` → named byte-alias substitution
        Any unrecognised ``%`` sequence is passed through verbatim so
        the underlying assembler can flag it.

        """
        result_parts: list[str] = []
        position = 0
        length = len(text)
        while position < length:
            character = text[position]
            if character != "%":
                result_parts.append(character)
                position += 1
                continue
            # We are at a '%' — look ahead.
            position += 1
            if position >= length:
                result_parts.append("%")
                break
            next_character = text[position]
            if next_character == "%":
                # %% -> literal %
                result_parts.append("%")
                position += 1
            elif next_character == "b":
                # Possible %b[name] or %bN (byte sub-register form).
                position += 1
                if position < length and text[position] == "[":
                    # %b[name] form
                    close_bracket = text.find("]", position + 1)
                    if close_bracket == -1:
                        result_parts.append("%b[")
                        position += 1
                    else:
                        operand_name = text[position + 1 : close_bracket]
                        position = close_bracket + 1
                        operand_index = name_to_index.get(operand_name)
                        if operand_index is not None and operand_index < len(operand_byte_locations):
                            result_parts.append(operand_byte_locations[operand_index])
                        else:
                            result_parts.append(f"%b[{operand_name}]")
                elif position < length and text[position].isdigit():
                    # %bN form
                    operand_index = int(text[position])
                    position += 1
                    if operand_index < len(operand_byte_locations):
                        result_parts.append(operand_byte_locations[operand_index])
                    else:
                        result_parts.append(f"%b{operand_index}")
                else:
                    result_parts.append("%b")
            elif next_character == "[":
                # %[name] form
                close_bracket = text.find("]", position + 1)
                if close_bracket == -1:
                    result_parts.append("%[")
                    position += 1
                else:
                    operand_name = text[position + 1 : close_bracket]
                    position = close_bracket + 1
                    operand_index = name_to_index.get(operand_name)
                    if operand_index is not None and operand_index < len(operand_locations):
                        result_parts.append(operand_locations[operand_index])
                    else:
                        result_parts.append(f"%[{operand_name}]")
            elif next_character.isdigit():
                # %N positional form
                operand_index = int(next_character)
                position += 1
                if operand_index < len(operand_locations):
                    result_parts.append(operand_locations[operand_index])
                else:
                    result_parts.append(f"%{operand_index}")
            else:
                # Not a recognized escape — emit literally.
                result_parts.append("%")
                # Do not advance past next_character; it will be processed next iteration.
        return "".join(result_parts)

    def _try_emit_conditional_via_cond_value(self, *, condition: Node, expression: Conditional) -> bool:
        """Elide the then-branch when it duplicates the comparison's left operand.

        Returns True when the ternary matched the pure-then-equals-cond.left
        shape and the lowering was emitted; the caller (``_generate_conditional``)
        then skips its default cond-jump / then / jmp / else / end layout.

        Recognised shape (verbatim output of ``MAX(a, b)`` / ``MIN(a, b)``
        after function-like macro expansion):

            Conditional(
                condition=BinaryOperation(left=X, op=COMP, right=Y),
                then_expr=X,                 # structurally equal to cond.left
                else_expr=anything,
            )

        :meth:`emit_condition` ends with ``cmp ax, <right>`` and leaves
        AX = X.  A *true*-jump to the merge label therefore skips the
        else branch with no re-evaluation of X — which is exactly the
        savings the textual macro pattern needs (``MIN(a-b, K)`` would
        otherwise emit ``a-b`` twice).

        Refused for impure ``then_expr`` (calls, address-of, etc.) — the
        textual macro semantics require evaluating the chosen branch in
        full, side effects included.  Refused too for ``&&`` / ``||``
        condition shapes (those go through the general
        :meth:`emit_condition_false_jump` short-circuit machinery, which
        doesn't leave a single representative value in AX), for unsigned
        long destinations (32-bit accumulator handling differs), and for
        byte-byte comparisons (AL holds the left byte but AH is stale,
        so falling through with AX as the result needs a zero-extend
        the standard path already issues separately).
        """
        if not isinstance(condition, BinaryOperation) or condition.operation not in COMPARISON_OPERATIONS:
            return False
        if expression.then_expr != condition.left:
            return False
        if not self._is_pure_expression(expression.then_expr):
            return False
        if self._is_byte_index(condition.left) and self._is_byte_index(condition.right):
            return False
        operator, unsigned = self.emit_condition(condition=condition, context="ast")
        table = JUMP_WHEN_TRUE_UNSIGNED if unsigned else JUMP_WHEN_TRUE
        # ``emit_condition`` may have returned the synthetic "carry" /
        # "not_carry" operator for a ``carry_return`` callee — there's
        # no entry in JUMP_WHEN_TRUE for those, and the cmp path that
        # this fast track depends on wasn't taken.  Bail.
        if operator not in table:
            return False
        end_label = f".cond_end_{self.new_label()}"
        self.emit(f"        {table[operator]} {end_label}")
        # Cond is false here — load else_expr into AX.  Clear ax_local
        # first so a Var(then_expr.name) shape inside else_expr doesn't
        # short-circuit on stale tracking.
        self.ax_clear()
        self.generate_expression(expression.else_expr)
        self.emit(f"{end_label}:")
        # Merge: AX holds whichever branch's value ran, but the
        # cross-path variable tracking is no longer guaranteed.
        self.ax_clear()
        return True

    def _try_emit_conditional_via_cmov(self, *, condition: Node, expression: Conditional) -> bool:
        """Lower a ternary to ``cmov`` when at least one branch is a pinned register.

        Returns True when the lowering matched and was emitted.  Fires
        only when the cmov sequence is *strictly shorter* than the
        diamond — i.e. when at least one branch is a ``Var`` pinned to
        a non-acc register, so cmov can use that register directly and
        skip the ``mov cx, acc`` staging that would otherwise burn the
        cmov's 1-byte advantage over ``jcc + jmp``.

        Two shapes emit::

            # then is pinned: load else first, cmov<cc> picks then
            mov acc, else_expr
            cmp X, Y
            cmov<cc-true> acc, then_reg

            # else is pinned: load then first, cmov<inverted-cc> picks else
            mov acc, then_expr
            cmp X, Y
            cmov<cc-false> acc, else_reg

        Either way: 3-byte ``cmov`` replaces 4-byte ``jcc + jmp``,
        saving 1 byte per ternary.  No branch in the output.

        Restrictions:
          - 32-bit target only (cmov is i686+).
          - Condition must be a comparison with ``Var`` / ``Int``
            operands (complex sub-expressions like ``arr[i]`` would
            need ECX as scratch and clobber our register-tracking).
          - Both branches must be ``Var`` / ``Int`` (single-mov loads).
          - At least one branch must be a ``Var`` pinned to a register
            other than the accumulator.
          - Unsigned ``<`` / ``<=`` / ``>`` / ``>=`` are bailed: the
            unsigned cmov mnemonics (``cmovb`` / ``cmova`` / ``cmovbe``
            / ``cmovae``) are not yet wired up in the self-host
            assembler.  ``==`` / ``!=`` work for both signs.
        """
        if self.target.int_size < 4:
            return False
        if not isinstance(condition, BinaryOperation) or condition.operation not in COMPARISON_OPERATIONS:
            return False
        if not isinstance(condition.left, (Var, Int)) or not isinstance(condition.right, (Var, Int)):
            return False
        if not isinstance(expression.then_expr, (Var, Int)) or not isinstance(expression.else_expr, (Var, Int)):
            return False
        if condition.operation not in ("==", "!=") and self._is_unsigned_comparison(condition.left, condition.right):
            return False
        acc = self.target.acc

        def pinned_reg(expr: Node) -> str | None:
            if not isinstance(expr, Var):
                return None
            register = self.pinned_register.get(expr.name)
            if register is None or register == acc:
                return None
            return register

        then_reg = pinned_reg(expression.then_expr)
        else_reg = pinned_reg(expression.else_expr)
        if then_reg is None and else_reg is None:
            # Neither branch is pinned — diamond is at least as short.
            return False
        # Prefer the ``then is pinned`` shape (more natural reading
        # order).  Either shape saves the same 1 byte vs. the diamond.
        self.validate_comparison_types(condition.left, condition.right)
        if then_reg is not None:
            # Load else into acc, then cmov<true> from then_reg.
            self.generate_expression(expression.else_expr)
            operator, _unsigned = self.emit_condition(condition=condition, context="ast")
            if operator not in CMOV_WHEN_TRUE:
                return False  # carry_return / other — already emitted else, can't bail cleanly
            self.emit(f"        {CMOV_WHEN_TRUE[operator]} {acc}, {then_reg}")
        else:
            # else is pinned (else_reg != None).  Load then into acc,
            # then cmov<inverted-cc> from else_reg — inverted so the
            # cmov fires when the original condition would be FALSE.
            self.generate_expression(expression.then_expr)
            operator, _unsigned = self.emit_condition(condition=condition, context="ast")
            if operator not in CMOV_WHEN_FALSE:
                return False
            self.emit(f"        {CMOV_WHEN_FALSE[operator]} {acc}, {else_reg}")
        self.ax_clear()
        return True

    def _va_arg_advance_size(self, type_name: str, /) -> int:
        """Return the number of bytes to advance a ``va_list`` cursor for *type_name*.

        Delegates to :meth:`_type_size` for all known types.  On i386
        cdecl, ``double`` occupies 8 bytes on the caller's stack; every
        other currently-supported type fits in one native word (4 bytes
        under ``--bits 32``, 2 under ``--bits 16``).
        """
        return self._type_size(type_name)

    def generate(self, ast: Node, /) -> str:
        """Generate assembly for an entire program AST.

        Returns:
            The complete assembly source as a string.

        """
        if self.object_mode and self.target_mode == "kernel":
            message = "--object is not supported with --target kernel"
            raise CompileError(message)
        for line in self.target.preamble_lines():
            self.emit(line)
        if self.target_mode == "user":
            if self.object_mode:
                self.emit('%include "constants.asm"')
                self.emit('%include "ccobj_markers.inc"')
                self.emit()
                self.emit("section .text")
            else:
                self.emit("        org 08048000h")
                self.emit()
                self.emit('%include "constants.asm"')
        if self.defines:
            self.emit()
            for name in sorted(self.defines):
                self.emit(f"%define {name} {self.defines[name]}")
        self.emit()
        self._apply_default_regparm(ast.functions)
        for function in ast.functions:
            if function.name == "main":
                if self.target_mode == "kernel":
                    message = "kernel-mode source may not define 'main'"
                    raise CompileError(message)
                continue
            # Prototypes whose name has a matching FUNCTION_<NAME>_PTR in
            # constants.asm are libbboeos exports — keep them out of
            # user_functions / extern_functions so the Call visitor
            # routes them through the cdecl indirect path rather than a
            # direct/CCREL call.
            pointer_constant = f"FUNCTION_{function.name.upper()}_PTR"
            if function.is_prototype and self.target_mode == "user" and pointer_constant in self.NAMED_CONSTANT_VALUES:
                self.libbboeos_extern_declarations[function.name] = len(function.params)
                continue
            self.user_functions[function.name] = len(function.params)
            if function.is_variadic:
                self.variadic_functions.add(function.name)
            if function.is_prototype:
                self.extern_functions.add(function.name)
            if function.regparm_count > 0:
                self.fastcall_functions.add(function.name)
                self.function_regparm_count[function.name] = function.regparm_count
            if function.carry_return:
                self.carry_return_functions.add(function.name)
            if function.always_inline:
                self._register_inline_body(function)
            for index, param in enumerate(function.params):
                if param.out_register is not None:
                    self.out_register_params.setdefault(function.name, {})[index] = param.out_register
                if param.in_register is not None:
                    self.in_register_params.setdefault(function.name, {})[index] = param.in_register
        self._register_globals(ast.globals)
        self._analyze_user_function_conventions(ast.functions)
        if self.object_mode and self.extern_globals:
            for name in sorted(self.extern_globals):
                if name in self.NASM_RESERVED_WORDS:
                    alias = f"__nasm_extern_{name}"
                    self.emit(f'%deftok {alias} "{name}"')
                    self.emit(f"extern {alias}")
                else:
                    self.emit(f"extern {name}")

        # Build IR for all non-main, non-always-inline functions.  The IR
        # is consumed by generate_function; main keeps the AST path because
        # the IR codegen path doesn't yet match the AST codegen's register
        # tracking (ax_local) and fast-path shape recognition
        # (_try_emit_guarded_update) on the patterns main typically holds.
        # Moving main through IR regressed real bytes across archived
        # programs without a clean win, so defer until the IR codegen
        # closes the parity gap.
        ir_program = ir.Builder(carry_return_functions=frozenset(self.carry_return_functions)).build_program(ast)
        # IR-level optimization: dead-code elimination + copy / constant
        # propagation + control-flow simplification + automatic tail-call
        # elimination across single-definition ``_ir_*`` temps.  Runs
        # before codegen so every backend (current x86, future ARM /
        # x86-64) benefits without having to re-implement equivalent
        # peephole rewrites at the asm-text level.
        ir_program = Optimizer(target=self.target).optimize(ir_program)
        ir_by_name = {
            f.ast_node.name: f
            for f in ir_program.functions
            if not f.ast_node.always_inline and not f.ast_node.is_prototype and not f.ast_node.naked
        }

        if self.target_mode == "user":
            # Emit main first so execution starts at PROGRAM_BASE.
            main_func = None
            helpers: list[Node] = []
            for function in ast.functions:
                if function.is_prototype:
                    continue
                if function.name == "main":
                    main_func = function
                else:
                    helpers.append(function)
            if main_func is not None:
                self.generate_function(main_func)
            for function in helpers:
                ir_func = ir_by_name.get(function.name)
                if ir_func is not None:
                    self.generate_function(ir_func)
                else:
                    self.generate_function(function)
        else:
            # Kernel mode: emit all functions in source order (no main allowed).
            for function in ast.functions:
                if function.is_prototype:
                    continue
                ir_func = ir_by_name.get(function.name)
                if ir_func is not None:
                    self.generate_function(ir_func)
                else:
                    self.generate_function(function)

        self.lines = Peepholer(lines=self.lines, target=self.target).run()
        self.lines = _elide_dead_frames(lines=self.lines, target=self.target)
        for include in sorted(self.required_includes):
            self.emit(f'%include "{include}"')
        # File-scope ``asm("...")`` blocks are emitted BEFORE globals /
        # strings / array data.  When the block holds code (for example
        # the assembler in user/programs/asm.c), this keeps the mutable global-
        # variable section away from the same 4K page as frequently-
        # executed instructions — QEMU's TCG invalidates per page on
        # stores, and mixing the two caused a 2x runtime slowdown on
        # the self-hosted assembler's pass loop.
        file_scope_asm = [decl for decl in ast.globals if isinstance(decl, InlineAsm)]
        if file_scope_asm:
            self.emit(";; --- inline asm ---")
            for decl in file_scope_asm:
                for line in decode_string_escapes(decl.content).splitlines():
                    self.emit(line)
        # In object mode each data category lands in its own section so
        # the linker can place them independently.  ``_emit_global_storage``
        # itself handles the ``section .data`` switch when it has anything
        # to emit (it can short-circuit before emitting anything when no
        # initialized globals exist, which is why the switch lives inside
        # the helper rather than out here).  Strings → .rodata; local
        # array literals → .rodata (read-only constant pool); zero-init
        # globals + elided locals → .bss via ``_emit_bss_trailer``.
        self._emit_global_storage()
        if self.strings:
            if self.object_mode:
                self.emit()
                self.emit("section .rodata")
            self.emit(";; --- string literals ---")
            for label, content in self.strings:
                self.emit(f"{label}: db `{content}\\0`")
        if self.arrays:
            code = "\n".join(self.lines)
            live = [(label, elements) for label, elements in self.arrays if label in code]
            if live:
                if self.object_mode and not self.strings:
                    self.emit()
                    self.emit("section .rodata")
                self.emit(";; --- array data ---")
                int_directive = "dd" if self.target.int_size == 4 else "dw"
                for label, elements in live:
                    self.emit(f"{label}: {int_directive} {', '.join(elements)}")
        if self.target_mode == "user":
            self._emit_bss_trailer()
            if not self.object_mode:
                # Sentinel label at the very end so inline asm can address the
                # first byte past the loaded image (scratch buffers, heap bases,
                # etc.).  Zero bytes, so it does not affect programs that ignore
                # it.
                self.emit("_program_end:")
                # BSS EQUs and _bss_end come *after* _program_end: so they are
                # never forward references — the self-hosted assembler cannot
                # resolve forward EQU references.
                self._emit_bss_equs()
        else:
            self._emit_kernel_bss_trailer()
        return "\n".join(self.lines) + "\n"

    def generate_body(self, statements: list[Node], /, *, scoped: bool = False) -> None:
        """Generate code for a sequence of statements.

        When *scoped* is True, variables declared inside the block are
        removed from ``visible_vars`` when the block ends.

        Applies several fusions:
        - ``printf(msg); exit();`` → ``die(msg)`` (when msg has no ``%``)
        - ``int err = syscall(...); if (err == 0) { exit(); }`` →
          syscall with ``jnc FUNCTION_EXIT`` (skip error-code conversion)
        - ``int err = syscall(...); if (err != 0) { die(msg); }`` →
          syscall with pre-loaded SI and ``jc FUNCTION_DIE`` (skip sbb)
        - ``if (cond) { die(msg); }`` → evaluate condition, inline die block
          on true-path, skip label on false-path
        """
        saved = self.visible_vars.copy() if scoped else None
        i = 0
        while i < len(statements):
            statement = statements[i]
            # Fuse simple printf() + exit() into die().  ``Return`` is
            # deliberately NOT fused here: a non-main function's
            # ``printf("err\n"); return -1;`` reports an error to a
            # caller that branches on the return value — turning the
            # whole pair into ``die(...)`` would never return and the
            # caller's recovery path would silently disappear.  main's
            # trailing ``printf+return`` is fused separately via
            # ``fuse_trailing_printf``, which only runs for main.
            next_is_exit = i + 1 < len(statements) and statements[i + 1] == Call(args=[], name="exit")
            if self._is_simple_printf(statement) and next_is_exit:
                self.builtin_die(statement.args)
                i += 2
                continue
            # Fuse die-on-error syscall + if-(non)zero-die.
            if isinstance(statement, VarDecl):
                init = statement.init
            elif isinstance(statement, Assign) and isinstance(statement.expr, Call):
                init = statement.expr
            else:
                init = None
            # Fuse `if (cond) { die(msg); }`: evaluate the condition, skip over
            # an inline die block when false, otherwise load SI+CX and jump.
            # Condition is emitted first so live registers are not clobbered by
            # the die-argument setup before the comparison runs.
            # AX tracking is preserved because the die path doesn't fall through.
            # Skipped in kernel mode — FUNCTION_DIE is a user-space jump-table slot.
            if self.target_mode == "user" and isinstance(statement, If) and statement.else_body is None and len(statement.body) == 1:
                inner = statement.body[0]
                if (
                    isinstance(inner, Call)
                    and inner.name == "die"
                    and isinstance(statement.cond, BinaryOperation)
                    and statement.cond.operation in JUMP_WHEN_FALSE
                ):
                    die_message = inner.args[0]
                    die_label = self.new_string_label(die_message.content)
                    die_length = string_byte_length(die_message.content)
                    operator, unsigned = self.emit_condition(condition=statement.cond, context="if")
                    false_jump = (JUMP_WHEN_FALSE_UNSIGNED if unsigned else JUMP_WHEN_FALSE)[operator]
                    skip_label = f".if_{self.new_label()}"
                    self.emit(f"        {false_jump} {skip_label}")
                    self.emit(f"        mov {self.target.si_register}, {die_label}")
                    self.emit(f"        mov {self.target.count_register}, {die_length}")
                    self._emit_libbboeos_jmp("FUNCTION_DIE")
                    self.emit(f"{skip_label}:")
                    i += 1
                    continue
            # Fuse error-returning syscall + if-truthy-die:
            #     int err = syscall(...);
            #     if (err) { die(msg); }
            # Emit the syscall, then preload SI/CX with the die
            # message and a single `jc FUNCTION_DIE` — no memory
            # round-trip for err, no CF->integer normalization.  Only
            # fires when `err` is never read after the if.
            if init is not None and isinstance(init, Call) and init.name in self.ERROR_RETURNING_BUILTINS and i + 1 < len(statements):
                next_stmt = statements[i + 1]
                die_call = None
                # Match cond: `err` (BinaryOperation != 0) or `!err` (BinaryOperation == 0)
                cond = next_stmt.cond if isinstance(next_stmt, If) else None
                is_truthy_cond = (
                    isinstance(cond, BinaryOperation)
                    and cond.operation == "!="
                    and isinstance(cond.left, Var)
                    and cond.left.name == statement.name
                    and cond.right == Int(value=0)
                )
                if (
                    is_truthy_cond
                    and next_stmt.else_body is None
                    and len(next_stmt.body) == 1
                    and isinstance(next_stmt.body[0], Call)
                    and next_stmt.body[0].name == "die"
                    and len(next_stmt.body[0].args) == 1
                    and isinstance(next_stmt.body[0].args[0], String)
                ):
                    die_call = next_stmt.body[0]
                if die_call is not None and not self._is_live_after(name=statement.name, statements=statements[i + 2 :]):
                    die_message = die_call.args[0]
                    die_label = self.new_string_label(die_message.content)
                    die_length = string_byte_length(die_message.content)
                    self.visible_vars.add(statement.name)
                    handler = getattr(self, f"builtin_{init.name}")
                    handler(init.args, fuse_die=(die_label, die_length))
                    self.ax_clear()
                    i += 2
                    continue
            # Fuse error-returning syscall + if-zero-exit:
            #     int err = syscall(...);
            #     if (err == 0) return;
            # becomes a single `jnc FUNCTION_EXIT` after the syscall,
            # leaving AL = error code on the CF=1 fall-through so any
            # subsequent `if (err == N)` chain reads the right byte.
            if init is not None and isinstance(init, Call) and init.name in self.ERROR_RETURNING_BUILTINS and i + 1 < len(statements):
                next_stmt = statements[i + 1]
                if self._is_zero_exit_if(next_stmt):
                    self.visible_vars.add(statement.name)
                    handler = getattr(self, f"builtin_{init.name}")
                    handler(init.args, fuse_exit=True)
                    self.ax_is_byte = True
                    self.ax_local = statement.name
                    i += 2
                    continue
            self.generate_statement(statement)
            i += 1
        if saved is not None:
            self.visible_vars = saved

    def generate_call(self, statement: Call, /, *, discard_return: bool = False, tail_call: bool = False) -> None:
        """Generate code for a function call statement.

        When *discard_return* is True (the call is at statement level
        with its return value unused) and three or more pinned
        registers need preserving, swaps the per-register
        ``push``/``pop`` pair for a single byte ``pusha``/``popa`` —
        2 bytes instead of 2 * N.  Pusha/popa restores AX too, so the
        return value would be lost; only the discard case can take
        this shortcut.

        When *tail_call* is True, the call is in tail position (the
        last statement of a frameless function body).  Emits ``jmp
        name`` instead of ``call name; ret`` and skips the
        register-save wrappers (the caller is about to return so
        there's nothing to restore).  Tail-call eligibility is
        pre-validated by ``_is_tail_call_eligible``; assumes no stack
        args, no inline-splice target, and no pinned registers would
        need saving at this call site.

        Raises:
            CompileError: If the called function is not a known builtin
                or user-defined function.

        """
        name = statement.name
        arguments = statement.args
        # Any call invalidates SI (callee may clobber it).
        self.si_local = None
        # Indirect call through a function pointer variable.
        if name in self.variable_types and self.variable_types[name] == "function_pointer":
            self._emit_function_pointer_call(
                arguments=arguments,
                discard_return=discard_return,
                line=statement.line,
                name=name,
            )
            return
        if name in self.user_functions:
            expected = self.user_functions[name]
            is_variadic = name in self.variadic_functions
            if (is_variadic and len(arguments) < expected) or (not is_variadic and len(arguments) != expected):
                comparator = "at least" if is_variadic else "exactly"
                message = f"{name}() expects {comparator} {expected} argument{'s' if expected != 1 else ''}"
                raise CompileError(message, line=statement.line)
            callee_pins = self.user_function_pin_params.get(name, {}) if name in self.register_convention_functions else {}
            is_fastcall = name in self.fastcall_functions
            callee_regparm_count = self.function_regparm_count.get(name, 0)
            # Fastcall: args 0..N-1 map to fixed registers (acc, dx,
            # count_register)[0..N-1].  Arg 0 (AX) is loaded LAST so
            # earlier register-arg evaluation can't trash it via the
            # parallel-move scheduler.
            regparm_registers = (self.target.acc, self.target.dx_register, self.target.count_register)
            out_regs = self.out_register_params.get(name, {})
            in_regs = self.in_register_params.get(name, {})
            fastcall_ax_arg: Node | None = None
            out_reg_captures: list[tuple[str, Node]] = []
            register_args: list[tuple[str, Node]] = []
            stack_args: list[Node] = []
            for index, arg in enumerate(arguments):
                if index in out_regs:
                    out_reg_captures.append((out_regs[index], arg))
                elif index in in_regs:
                    register_args.append((in_regs[index], arg))
                elif is_fastcall and index == 0:
                    fastcall_ax_arg = arg
                elif is_fastcall and index < callee_regparm_count:
                    register_args.append((regparm_registers[index], arg))
                elif index in callee_pins:
                    register_args.append((callee_pins[index], arg))
                else:
                    stack_args.append(arg)
            # Pinned registers whose locals get overwritten by an
            # out_register capture have no live pre-call value worth
            # preserving — push/pop around the call would clobber the
            # captured value.  Exclude them from saved before the push
            # loop; the pop loop then has nothing to restore for them.
            captured_pinned_registers: set[str] = set()
            for _, capture_arg in out_reg_captures:
                capture_name = address_of_variable_name(capture_arg)
                if capture_name is not None and capture_name in self.pinned_register:
                    captured_pinned_registers.add(self.pinned_register[capture_name])
            clobbers: frozenset[str] = frozenset(self.target.register_pool)
            saved = [r for r in self._pinned_registers_to_save(clobbers) if r not in captured_pinned_registers]
            use_pusha = discard_return and len(saved) >= 3
            if not tail_call:
                if use_pusha:
                    self.emit("        pusha")
                else:
                    for register in saved:
                        self.emit(f"        push {register}")
            # Push stack-bound arguments right-to-left (C convention).
            for arg in reversed(stack_args):
                self._emit_push_arg(arg)
            # Load register-bound arguments with topological ordering.
            self._emit_register_arg_moves(register_args)
            # Fastcall arg 0 is loaded last so earlier arg evaluation can't
            # trash AX while we're assembling the other parameters.
            if fastcall_ax_arg is not None:
                self.emit_register_from_argument(argument=fastcall_ax_arg, register=self.target.acc)
            if tail_call:
                # Tail call: jmp instead of call; no stack cleanup (ruled
                # out by _is_tail_call_eligible) and no register restore
                # (skipped above).  Function's own ``ret`` is elided at
                # generate_function's epilogue.
                if self.object_mode and name in self.extern_functions:
                    self.emit(f"        CCREL_JMP {name}")
                else:
                    self.emit(f"        jmp {name}")
                self.ax_clear()
                return
            if name in self.inline_bodies:
                self._emit_inline_body(name)
            elif self.object_mode and name in self.extern_functions:
                self.emit(f"        CCREL_CALL {name}")
            else:
                self.emit(f"        call {name}")
            if stack_args:
                self.emit(f"        add {self.target.stack_register}, {len(stack_args) * self.target.int_size}")
            # Capture out_register outputs before any register restores so the
            # callee-written registers haven't been overwritten by the pops yet.
            #
            # Width handling mirrors the in_register prologue: when the
            # callee returned via a 16-bit name (e.g. ``out_register("bx")``)
            # but the destination spans a wider slot (32-bit local or pinned
            # E-register), zero-extend so the upper bytes are clean.
            si_captured: str | None = None
            # Order captures topologically so a capture whose source
            # register is another capture's destination is emitted
            # FIRST.  Without this, ``mov ecx, edi; mov edx, ecx`` (when
            # both ECX and EDX are pinned destinations) reads the
            # already-overwritten ECX into EDX.  We assume the
            # underlying source registers are distinct (the prototype
            # would be malformed otherwise), so no cycles can form —
            # just a strict partial order.
            pending = []
            pinned_dest = {}
            for reg, arg in out_reg_captures:
                dest_name = address_of_variable_name(arg)
                if dest_name is None:
                    message = "out_register argument must be an address-of expression (&var)"
                    raise CompileError(message, line=statement.line)
                dest_reg = self.pinned_register.get(dest_name) if dest_name in self.pinned_register else None
                pending.append((reg, arg, dest_reg))
                if dest_reg is not None:
                    pinned_dest[dest_reg] = True
            ordered = []
            while pending:
                progress = None
                for index, (reg, _arg, dest_reg) in enumerate(pending):
                    # Safe to emit if no OTHER pending capture's source
                    # register equals this one's pinned destination.
                    if dest_reg is None or not any(j != index and other_reg == dest_reg for j, (other_reg, _, _) in enumerate(pending)):
                        progress = index
                        break
                if progress is None:
                    message = "out_register captures form a register cycle"
                    raise CompileError(message, line=statement.line)
                ordered.append(pending.pop(progress))
            for reg, arg, _dest_reg in ordered:
                dest_name = address_of_variable_name(arg)
                widened = self.target.widen_gp(reg)
                if dest_name in self.pinned_register:
                    dest_reg = self.pinned_register[dest_name]
                    if dest_reg == reg:
                        pass
                    elif len(dest_reg) > len(reg):
                        # Pinned destination is wider than the returned
                        # register (e.g. ECX pinned, callee returned in
                        # BX) — zero-extend so the upper bytes don't
                        # carry pre-call garbage.  Covers both
                        # ``dest_reg == widened`` and the cross-register
                        # widening case where auto-pin landed on a
                        # different E-register than ``widen_gp(reg)``.
                        self.emit(f"        movzx {dest_reg}, {reg}")
                    else:
                        self.emit(f"        mov {dest_reg}, {reg}")
                else:
                    dest = self._local_address(dest_name)
                    if widened != reg:
                        self.emit(f"        movzx {widened}, {reg}")
                        self.emit(f"        mov [{dest}], {widened}")
                    else:
                        self.emit(f"        mov [{dest}], {reg}")
                    if reg == self.target.si_register:
                        si_captured = dest_name
            if use_pusha:
                self.emit("        popa")
                si_captured = None  # popa restores all regs including SI
            else:
                for register in reversed(saved):
                    self.emit(f"        pop {register}")
            self.ax_clear()
            # Track SI as holding the captured variable until the next call.
            # The stack slot is authoritative; this is a pure read-optimisation.
            if si_captured is not None:
                self.si_local = si_captured
            return
        handler = getattr(self, f"builtin_{name}", None)
        if handler is None:
            # Libbboeos extern call.  The prototype-registration pass put
            # the name in libbboeos_extern_declarations after seeing
            # `int strcmp(const char *, const char *);` (or equivalent
            # via `#include "string.h"`).  Emit a regparm(3) indirect
            # call through the pointer table — first 3 args in
            # EAX/EDX/ECX, remainder pushed right-to-left,
            # `call [FUNCTION_<NAME>_PTR]`, caller pops stack args.
            if name in self.libbboeos_extern_declarations:
                pointer_constant = f"FUNCTION_{name.upper()}_PTR"
                param_count = self.libbboeos_extern_declarations[name]
                regparm_count = min(3, param_count)
                regparm_registers = (self.target.acc, self.target.dx_register, self.target.count_register)
                fastcall_ax_arg: Node | None = None
                register_args: list[tuple[str, Node]] = []
                stack_args: list[Node] = []
                for index, arg in enumerate(arguments):
                    if index < regparm_count:
                        if index == 0:
                            fastcall_ax_arg = arg
                        else:
                            register_args.append((regparm_registers[index], arg))
                    else:
                        stack_args.append(arg)
                clobbers: frozenset[str] = frozenset(self.target.register_pool)
                saved = self._pinned_registers_to_save(clobbers)
                use_pusha = discard_return and len(saved) >= 3
                if use_pusha:
                    self.emit("        pusha")
                else:
                    for register in saved:
                        self.emit(f"        push {register}")
                for arg in reversed(stack_args):
                    self._emit_push_arg(arg)
                self._emit_register_arg_moves(register_args)
                if fastcall_ax_arg is not None:
                    self.emit_register_from_argument(argument=fastcall_ax_arg, register=self.target.acc)
                self.emit(f"        call [{pointer_constant}]")
                if stack_args:
                    self.emit(f"        add {self.target.stack_register}, {len(stack_args) * self.target.int_size}")
                if use_pusha:
                    self.emit("        popa")
                else:
                    for register in reversed(saved):
                        self.emit(f"        pop {register}")
                self.ax_clear()
                return
            # Strict-on-libbboeos: if the name HAS a FUNCTION_<NAME>_PTR
            # constant but no prior prototype, demand the declaration
            # instead of silently emitting an indirect call.  Encourages
            # `#include "string.h"` (etc.) at every call site so the
            # arg-count check below applies.
            pointer_constant = f"FUNCTION_{name.upper()}_PTR"
            if self.target_mode == "user" and pointer_constant in self.NAMED_CONSTANT_VALUES:
                message = (
                    f"call to libbboeos export '{name}' requires a prior prototype declaration "
                    f'(e.g. `#include "string.h"` or a forward decl)'
                )
                raise CompileError(message, line=statement.line)
            message = f"unknown function: {name}"
            raise CompileError(message, line=statement.line)
        clobbers = self._builtin_clobbers[name]
        saved = self._pinned_registers_to_save(clobbers)
        use_pusha = discard_return and len(saved) >= 3
        if use_pusha:
            self.emit("        pusha")
        else:
            for register in saved:
                self.emit(f"        push {register}")
        handler(arguments)
        if use_pusha:
            self.emit("        popa")
        else:
            for register in reversed(saved):
                self.emit(f"        pop {register}")

    def generate_do_while(self, statement: DoWhile, /) -> None:
        """Generate assembly for a do...while loop.

        The body executes unconditionally once, then the condition is
        tested at the bottom.  ``break`` inside the body jumps to the
        end label, same as in a ``while`` loop.  ``continue`` jumps to
        the condition test so the loop can re-evaluate and restart.
        """
        condition, body = statement.cond, statement.body
        label_index = self.new_label()
        end_label = f".do_{label_index}_end"
        continue_label = f".do_{label_index}_continue"
        self.emit(f".do_{label_index}:")
        self.loop_end_labels.append(end_label)
        self.loop_continue_labels.append(continue_label)
        self.generate_body(body, scoped=True)
        self.emit(f"{continue_label}:")
        # Short-circuit any false operand straight to end; otherwise
        # fall through to the unconditional jump back to the top.  The
        # ``jfalse end_label; jmp top; end_label:`` pattern is collapsed
        # by peephole_double_jump into ``jtrue top`` for single
        # comparisons.
        self.emit_condition_false_jump(condition=condition, context="do_while", fail_label=end_label)
        self.emit(f"        jmp .do_{label_index}")
        self.emit(f"{end_label}:")
        self.loop_continue_labels.pop()
        self.loop_end_labels.pop()

    def generate_expression(self, expression: Node, /) -> None:
        """Generate code for an expression, leaving the result in AX.

        Raises:
            CompileError: If an unknown expression kind or operator is encountered.

        """
        # Skip load if AX already holds this variable's value.
        if isinstance(expression, Var) and expression.name == self.ax_local:
            return
        if isinstance(expression, AssignExpr):
            self._generate_assign_expr(expression)
        elif isinstance(expression, BinaryOperation):
            self._generate_binary_operation_expression(expression)
        elif isinstance(expression, Call):
            self.generate_call(expression)
        elif isinstance(expression, Cast):
            # Identity codegen: evaluate the inner expression; the target type
            # is tracked in the AST node but cc.py's loose type system treats
            # all register-sized values uniformly so no truncation is emitted.
            self.generate_expression(expression.expression)
        elif isinstance(expression, Conditional):
            self._generate_conditional(expression)
        elif isinstance(expression, DerefIncrement):
            # ``*p++`` / ``*p--`` (postfix) as an rvalue: load ``*p``
            # (pre-update value) into the accumulator first, then bump
            # ``p`` by sizeof(*p) bytes *without* touching the
            # accumulator.  ``*++p`` / ``*--p`` (prefix) reverses the
            # order — bump first, then load through the post-incremented
            # pointer.  The pointee read goes through the recursive Place
            # core (``DereferencePlace`` of the pointer ``Var``), the same
            # path a parser-emitted ``*p`` read uses.  Both paths share
            # :meth:`_emit_pointer_bump`, which operates directly on the
            # pinned register / frame slot.  After a prefix bump
            # :meth:`ax_clear` is invoked so the subsequent load reloads
            # from the updated slot.
            target = expression.target_name
            self._check_defined(target, line=expression.line)
            pointee_place = DereferencePlace(
                line=expression.line,
                pointer=Var(line=expression.line, name=target),
            )
            if expression.is_postfix:
                self._emit_place_load(pointee_place)
                self._emit_pointer_bump(delta=expression.delta, line=expression.line, name=target)
            else:
                self._emit_pointer_bump(delta=expression.delta, line=expression.line, name=target)
                self.ax_clear()
                self._emit_place_load(pointee_place)
        elif isinstance(expression, Index):
            self._generate_index_expression(expression)
        elif isinstance(expression, Int):
            self.ax_clear()
            if expression.value == 0:
                self.emit(f"        xor {self.target.acc}, {self.target.acc}")
            else:
                self.emit(f"        mov {self.target.acc}, {expression.value}")
        elif isinstance(expression, (LogicalAnd, LogicalOr)):
            self._generate_logical_value(expression)
        elif isinstance(expression, PlaceAddressOf):
            self._emit_place_address_of(expression.place)
        elif isinstance(expression, PlaceCall):
            self._emit_place_call(expression)
        elif isinstance(expression, PlaceIncrementDecrement):
            self._emit_place_increment_decrement(expression)
        elif isinstance(expression, PlaceLoad):
            self._emit_place_load(expression.place)
        elif isinstance(expression, SizeofExpr):
            self.ax_clear()
            inferred_type = self._expression_type(expression.expression)
            self.emit(f"        mov {self.target.acc}, {self._type_size(inferred_type)}")
        elif isinstance(expression, SizeofType):
            self.ax_clear()
            self.emit(f"        mov {self.target.acc}, {self._type_size(expression.type_name)}")
        elif isinstance(expression, SizeofVar):
            self.ax_clear()
            vname = expression.name
            if vname in self.global_arrays:
                declaration = self.global_arrays[vname]
                # Multidimensional arrays: use the registered ArrayType which
                # knows all dimensions and the real element size.
                if vname in self.array_types:
                    size = self.array_types[vname].sizeof(
                        pointer_width=self.target.int_size,
                        scalar_width=self._type_size,
                    )
                    self.emit(f"        mov {self.target.acc}, {size}")
                else:
                    # Single-dimension array: use the real element stride so
                    # ``unsigned short`` uses 2 bytes, not ``int_size`` (4).
                    stride = self._type_size(declaration.type_name)
                    if declaration.init is not None:
                        size = len(declaration.init.elements) * stride
                        self.emit(f"        mov {self.target.acc}, {size}")
                    else:
                        size_expression = self._constant_expression(declaration.size)
                        if size_expression is not None and size_expression.isdigit():
                            self.emit(f"        mov {self.target.acc}, {int(size_expression) * stride}")
                        else:
                            self.emit(f"        mov {self.target.acc}, ({size_expression})*{stride}")
            elif vname in self.local_stack_arrays:
                size = self.local_stack_arrays[vname]
                self.emit(f"        mov {self.target.acc}, {size}")
            elif vname in self.array_sizes:
                # array_sizes stores element count; use the real element stride
                # so ``unsigned short`` uses 2 bytes, not ``int_size`` (4).
                stride = self._type_size(self.variable_types.get(vname, "int"))
                size = self.array_sizes[vname] * stride
                self.emit(f"        mov {self.target.acc}, {size}")
            elif (
                vname in self.variable_types
                and self.variable_types[vname].startswith("struct ")
                and not self.variable_types[vname].endswith("]")
            ):
                tag = self.variable_types[vname][7:]
                size = self.struct_sizes[tag]
                self.emit(f"        mov {self.target.acc}, {size}")
            else:
                size = self.target.int_size  # all non-array variables are word-sized
                self.emit(f"        mov {self.target.acc}, {size}")
        elif isinstance(expression, String):
            self.ax_clear()
            self.emit(f"        mov {self.target.acc}, {self.new_string_label(expression.content)}")
        elif isinstance(expression, VaArg):
            self.builtin___builtin_va_arg(
                [expression.cursor],
                advance_size=self._va_arg_advance_size(expression.type_name),
            )
        elif isinstance(expression, Var):
            vname = expression.name
            if vname in self.NAMED_CONSTANTS:
                self.emit_constant_reference(vname)
                self.emit(f"        mov {self.target.acc}, {vname}")
                self.ax_clear()
                return
            if vname in self.constant_aliases:
                self.emit(f"        mov {self.target.acc}, {self.constant_aliases[vname]}")
                self.ax_clear()
                return
            if vname in self.user_functions:
                # A bare function name as an rvalue decays to the
                # function's address (a link-time constant), so the
                # value can be assigned to a function_pointer global,
                # passed as an argument, etc.
                self.emit(f"        mov {self.target.acc}, {vname}")
                self.ax_clear()
                return
            if vname in self.global_arrays:
                # A global array name decays to its base address — the
                # file-scope label (``_g_<name>`` in flat mode or
                # ``<name>`` in object mode, per ``_global_label``).
                # Load it as an immediate, not as a memory fetch from
                # that address.
                self.emit(f"        mov {self.target.acc}, {self._global_label(vname)}")
                self.ax_clear()
                return
            if vname in self.local_stack_arrays:
                # Local stack array decays to its base address.
                if self.elide_frame:
                    self.emit(f"        mov {self.target.acc}, _l_{vname}")
                else:
                    offset = self.locals[vname]
                    self.emit(f"        lea {self.target.acc}, [{self.target.base_register}-{offset}]")
                self.ax_clear()
                return
            self._check_defined(vname, line=expression.line)
            if self.variable_types.get(vname) == "unsigned long":
                message = f"'unsigned long' variable {vname!r} cannot be used in a 16-bit expression context"
                raise CompileError(message, line=expression.line)
            if vname in self.pinned_register:
                source = self.pinned_register[vname]
                if len(source) < len(self.target.acc):
                    # 16-bit pinned register into 32-bit acc: zero-extend.
                    self.emit(f"        movzx {self.target.acc}, {source}")
                else:
                    self.emit(f"        mov {self.target.acc}, {source}")
                self.ax_is_byte = False
            elif vname in self.register_aliased_globals:
                source = self.register_aliased_globals[vname]
                if len(source) < len(self.target.acc):
                    self.emit(f"        movzx {self.target.acc}, {source}")
                else:
                    self.emit(f"        mov {self.target.acc}, {source}")
                self.ax_is_byte = False
            elif self._is_byte_scalar(vname):
                # Byte-scalar locals and globals store as a single
                # byte; load only the low byte, then zero-extend so
                # any downstream arithmetic on AX reads a clean word.
                # The compare fast path still picks up ``ax_is_byte``
                # to use ``cmp al`` / ``test al`` and skip the
                # redundant high-byte compare; a peephole later
                # collapses the paired ``xor ah, ah`` before a ``cmp
                # al`` (or any other AL-only consumer) when the high
                # byte is provably unused.
                self.emit_byte_load_zx(f"[{self._local_address(vname)}]")
                self.ax_is_byte = True
            else:
                self.emit(f"        mov {self.target.acc}, [{self._local_address(vname)}]")
                self.ax_is_byte = False
            self.ax_local = vname
        else:
            message = f"unknown expression: {type(expression).__name__}"
            raise CompileError(message, line=expression.line)

    def generate_extended_asm(self, statement: ExtendedAsm, /) -> None:
        """Generate assembly for a GCC extended inline asm statement.

        Handles integer GP register constraints (a/b/c/d), byte-register
        constraints (q/qm), memory constraints (g/m), x87 FP constraints
        (t/u), tied operands (0), and named operand references (%[name] /
        %bN / %N).

        Phase 1 — build operand location map.
        Phase 2 — pre-template: load inputs into registers where needed,
                  including x87 FP stack pushes.
        Phase 3 — substitute operand tokens and emit template lines.
        Phase 4 — post-template: store register outputs back to memory,
                  including x87 ST0 pop for =t outputs.
        Phase 5 — invalidate AX tracking.
        """
        constraint_register_32 = {"a": "eax", "b": "ebx", "c": "ecx", "d": "edx"}
        constraint_register_byte = {"a": "al", "b": "bl", "c": "cl", "d": "dl"}

        def _operand_location_for_var(name: str) -> str:
            """Return the storage location for a variable name.

            For register-aliased globals and auto-pinned locals, returns
            the register name directly.  For constant-aliased locals (int
            x = 10 with no later writes), returns the immediate value
            string.  For frame-allocated locals and globals, returns a
            bracketed memory operand.
            """
            if (register_alias := self.register_aliased_globals.get(name)) is not None:
                return register_alias
            if (pinned := self.pinned_register.get(name)) is not None:
                return pinned
            if (constant_value := self.constant_aliases.get(name)) is not None:
                return str(constant_value)
            return f"[{self._local_address(name)}]"

        def _operand_memory_address(expression: Node) -> str:
            """Return the storage location (register or memory) for a variable expression."""
            name = _unwrap_var_name(expression)
            return _operand_location_for_var(name)

        def _unwrap_var_name(expression: Node) -> str:
            """Return the variable name from a Var or Cast(Var) node."""
            if isinstance(expression, Cast):
                return _unwrap_var_name(expression.expression)
            return expression.name  # type: ignore[attr-defined]

        # --- Phase 1: build operand location lists ---
        # All operands: outputs first, then inputs.
        all_operands = list(statement.outputs) + list(statement.inputs)

        operand_locations: list[str] = []
        operand_byte_locations: list[str] = []
        name_to_index: dict[str, int] = {}

        # Track which byte registers have been claimed by q/qm constraints
        # so we can avoid assigning the same register twice.
        claimed_byte_registers: list[str] = []

        for index, operand in enumerate(all_operands):
            if operand.name is not None:
                name_to_index[operand.name] = index

            constraint = operand.constraint
            # Strip leading modifiers: =, +, &, combinations thereof
            core = constraint.lstrip("=+&")

            if core in constraint_register_32:
                reg32 = constraint_register_32[core]
                reg8 = constraint_register_byte[core]
                operand_locations.append(reg32)
                operand_byte_locations.append(reg8)
            elif core in ("q", "qm"):
                # Pick the first available byte register not yet claimed.
                # Default preference: cl, al, bl, dl.
                for candidate_byte in ("cl", "al", "bl", "dl"):
                    if candidate_byte not in claimed_byte_registers:
                        chosen_byte = candidate_byte
                        break
                else:
                    chosen_byte = "cl"
                claimed_byte_registers.append(chosen_byte)
                # The 32-bit parent of the chosen byte register.
                byte_to_32 = {"al": "eax", "bl": "ebx", "cl": "ecx", "dl": "edx"}
                chosen_32 = byte_to_32[chosen_byte]
                operand_locations.append(chosen_32)
                operand_byte_locations.append(chosen_byte)
            elif core == "g":
                memory_address = _operand_memory_address(operand.expression)
                operand_locations.append(memory_address)
                operand_byte_locations.append(memory_address)
            elif core == "m":
                # Memory operand: the template uses %N directly as a memory
                # address, so the substituted text must include brackets.
                # _local_address returns ``ebp-N`` or ``_g_name`` (no
                # brackets); wrap here so NASM sees ``[ebp-N]`` /
                # ``[_g_name]`` in the substituted template.
                mem_name = _unwrap_var_name(operand.expression)
                bracketed = f"[{self._local_address(mem_name)}]"
                operand_locations.append(bracketed)
                operand_byte_locations.append(bracketed)
            elif core == "0":
                # Tied to output operand 0 — share its location.
                if operand_locations:
                    operand_locations.append(operand_locations[0])
                    operand_byte_locations.append(operand_byte_locations[0])
                else:
                    operand_locations.append("eax")
                    operand_byte_locations.append("al")
            elif core in ("t", "u"):
                # x87 FP stack slots: implicit (no template substitution).
                # Use a sentinel; the template does not reference these
                # operands with % tokens directly — the FP stack is managed
                # by the pre/post fld/fstp sequences emitted in phases 2/4.
                operand_locations.append("__x87_st0__" if core == "t" else "__x87_st1__")
                operand_byte_locations.append("__x87_st0__" if core == "t" else "__x87_st1__")
            else:
                # Unknown constraint: emit a placeholder rather than crash.
                operand_locations.append(f"__constraint_{core}__")
                operand_byte_locations.append(f"__constraint_{core}__")

        # --- Phase 2: pre-template loads ---
        for output_index, output_operand in enumerate(statement.outputs):
            constraint = output_operand.constraint
            if constraint.startswith("+"):
                # Read-modify-write: load variable into its designated register.
                location = operand_locations[output_index]
                memory_address = _operand_memory_address(output_operand.expression)
                self.emit(f"        mov {location}, {memory_address}")

        for input_operand in statement.inputs:
            constraint = input_operand.constraint
            core = constraint.lstrip("=+&")
            if core == "0":
                # Tied to output 0: load into output 0's register only when
                # output 0 is a GP register constraint (not x87 "=t").
                output_0_core = statement.outputs[0].constraint.lstrip("=+&") if statement.outputs else ""
                if output_0_core != "t":
                    location = operand_locations[0] if operand_locations else "eax"
                    memory_address = _operand_memory_address(input_operand.expression)
                    self.emit(f"        mov {location}, {memory_address}")
            # g/m constraints need no pre-load (template accesses memory directly).

        # Pre-template x87 FP loads: "u" first (pushes to ST0, becomes ST1
        # after the "0"-tied load), then "0"-tied-to-"=t" (pushes to ST0).
        # This ordering ensures ST0 = "0"-tied value, ST1 = "u" value —
        # the arrangement expected by fpatan and similar two-operand x87
        # instructions.
        for input_operand in statement.inputs:
            core = input_operand.constraint.lstrip("=+&")
            if core == "u":
                fp_name = _unwrap_var_name(input_operand.expression)
                self.emit(f"        fld qword [{self._local_address(fp_name)}]")

        for input_operand in statement.inputs:
            core = input_operand.constraint.lstrip("=+&")
            if core == "0":
                output_0_core = statement.outputs[0].constraint.lstrip("=+&") if statement.outputs else ""
                if output_0_core == "t":
                    fp_name = _unwrap_var_name(input_operand.expression)
                    self.emit(f"        fld qword [{self._local_address(fp_name)}]")

        # --- Phase 3: substitute template and emit ---
        template_text = decode_string_escapes(statement.template)
        substituted = EmissionMixin._substitute_extended_asm_template(
            template_text,
            name_to_index=name_to_index,
            operand_byte_locations=operand_byte_locations,
            operand_locations=operand_locations,
        )
        # GCC inline asm uses AT&T x87 register syntax (%st(N));
        # convert to NASM's stN form.
        substituted = re.sub(r"%st\((\d)\)", r"st\1", substituted)
        for line in substituted.splitlines():
            self.emit(_att_to_intel(line))

        # --- Phase 4: post-template: store register outputs back to memory ---
        for output_index, output_operand in enumerate(statement.outputs):
            constraint = output_operand.constraint
            core = constraint.lstrip("=+&")
            location = operand_locations[output_index]
            if core in constraint_register_32 or (constraint.startswith("+") and core in constraint_register_32):
                memory_address = _operand_memory_address(output_operand.expression)
                self.emit(f"        mov {memory_address}, {location}")
            elif core in ("q", "qm"):
                byte_location = operand_byte_locations[output_index]
                memory_address = _operand_memory_address(output_operand.expression)
                self.emit(f"        movzx eax, {byte_location}")
                self.emit(f"        mov {memory_address}, eax")
            elif core == "t":
                # =t: ST0 holds the result — pop it to the variable's memory slot.
                fp_name = _unwrap_var_name(output_operand.expression)
                self.emit(f"        fstp qword [{self._local_address(fp_name)}]")
            # g/m: template wrote directly to memory — no post-store needed.
            # u: x87 ST1 — caller manages (the template consumed it).

        # --- Phase 5: invalidate AX tracking ---
        self.ax_clear()

    # ------------------------------------------------------------------
    # IR lowering
    # ------------------------------------------------------------------

    def generate_for(self, statement: For, /) -> None:
        """Generate assembly for a ``for (init; cond; step) { body }`` loop.

        ``continue`` jumps to the step label (not the condition test) so
        that the step expressions are always executed.
        """
        label_index = self.new_label()
        top_label = f".for_{label_index}"
        step_label = f".for_{label_index}_step"
        end_label = f".for_{label_index}_end"
        for init_statement in statement.init:
            self.generate_statement(init_statement)
        self.emit(f"{top_label}:")
        self.loop_end_labels.append(end_label)
        self.loop_continue_labels.append(step_label)
        if statement.cond is not None:
            self.emit_condition_false_jump(condition=statement.cond, context="for", fail_label=end_label)
        self.generate_body(statement.body, scoped=True)
        self.emit(f"{step_label}:")
        for step_expression in statement.step:
            self.generate_expression(step_expression)
            self.ax_clear()
        self.emit(f"        jmp {top_label}")
        self.emit(f"{end_label}:")
        self.loop_continue_labels.pop()
        self.loop_end_labels.pop()
        self.ax_clear()

    def generate_function(self, function: Function | ir.Function, /) -> None:
        """Generate assembly for a single function definition."""
        # Unpack ir.Function: keep the IR body for code generation but use
        # the original AST node for all frame-setup analysis.
        ir_body: list[ir.Instruction] | None = None
        ir_strings: list[tuple[str, str]] = []
        if isinstance(function, ir.Function):
            ir_body = function.body
            ir_strings = function.strings
            function = function.ast_node
        name = function.name
        if function.always_inline:
            # No free-standing body; the function has been recorded in
            # ``inline_bodies`` and will be spliced at each call site.
            return
        parameters = function.params
        body = function.body
        self.array_labels = {}
        self.array_sizes = {}
        self.auto_pin_candidates: dict[str, str] = {}
        self.switch_pin_overrides: set[str] = set()
        self.ax_clear()
        self.constant_aliases = {}
        self.current_carry_return = function.carry_return
        self.current_function_is_main = name == "main"
        self.current_function_is_naked = function.naked
        self.current_function_is_variadic = function.is_variadic
        self.current_function_regparm_count = function.regparm_count
        self._current_function_parameter_names: tuple[str, ...] = tuple(parameter.name for parameter in parameters)
        self._ir_call_pinned_initialized = {}
        self._current_call_pinned_initialized = None
        # Per-function user-label bookkeeping for the AST codegen path.
        # The IR path validates inside ir.Builder; main() and other AST-
        # path functions validate here after generate_body completes.
        self.user_labels_defined: dict[str, int] = {}
        self.user_labels_referenced: dict[str, int] = {}
        self.elide_frame = name == "main"
        # Frame-elide criteria for non-main functions.  The bp frame
        # becomes dead weight whenever the body makes no BP-relative
        # accesses: no parameters (no ``[bp+N]`` reads), no locals
        # (no ``[bp-N]`` slots), and no cc.py codegen path that
        # touches BP.  Inside a function body, ``asm("...")`` parses
        # as a ``Call`` to the ``asm`` builtin (not an InlineAsm
        # node — that's only used for file-scope ``asm(...)``
        # directives).
        #
        # ``naked_asm`` covers the hand-coded inline-asm helpers
        # (``abort_unknown`` / ``restore_es`` / ``close_source`` /
        # ``read_source_sector``).  ``frameless_calls`` covers pure-C
        # dispatch helpers — handlers like ``handle_clc`` whose
        # body is ``emit_byte(0xF8);`` or ``handle_aam`` whose body
        # is two ``emit_byte(...)`` calls.  For those, cc.py's call
        # codegen emits ``mov ax, N ; call fn`` with no pin save
        # (no locals means no pinned registers) and no stack-arg
        # math, so BP is genuinely unused.
        naked_asm = name != "main" and not parameters and len(body) == 1 and isinstance(body[0], Call) and body[0].name == "asm"
        frameless_calls = (
            name != "main" and not parameters and len(body) >= 1 and all(isinstance(stmt, Call) and stmt.name != "asm" for stmt in body)
        )
        if naked_asm or frameless_calls or function.naked:
            self.elide_frame = True
        if function.naked:
            for param in parameters:
                if param.in_register is None and param.out_register is None:
                    message = f"naked function '{name}': parameter '{param.name}' must have in_register or out_register"
                    raise CompileError(message, line=function.line)
            for stmt in body:
                if isinstance(stmt, (VarDecl, ArrayDecl)):
                    message = f"naked function '{name}': body must not declare locals (found '{stmt.name}')"
                    raise CompileError(message, line=function.line)
        self.byte_scalar_locals = set()
        self.current_preserve_registers: list[str] = list(function.preserve_registers)
        self.frame_size = 0
        self.function_pointer_in_registers: dict[str, dict[int, str]] = {}
        self.ax_literal = None
        self.known_local_bytes.clear()
        self._last_byte_store = None
        self.live_long_local = None
        self.local_stack_arrays = {}
        self.locals = {}
        self.out_register_locals: dict[str, str] = {}
        self.param_in_register: dict[str, str] = {}
        self.pinned_register = {}
        self.si_local: str | None = None
        self.variable_arrays = set()
        self.variable_types = {}
        self.virtual_long_locals = set()
        self.zero_init_skippable: set[str] = set()

        # Pre-scan: detect local stack arrays before compute_safe_pin_registers
        # so bp is excluded from the pin pool when it's needed as a frame
        # pointer.  compute_safe_pin_registers adds bp to the pool only when
        # elide_frame is True; if we discover arrays here and flip the flag
        # early, the pool will correctly omit bp and no variable will be
        # pinned to the frame-pointer register.
        if name == "main":

            def _body_has_stack_arrays(stmts: list[Node]) -> bool:
                for stmt in stmts:
                    if isinstance(stmt, ArrayDecl) and stmt.size is not None:
                        stride = 1 if stmt.type_name in self.BYTE_TYPES else self.target.int_size
                        if self._eval_local_array_size(stmt.size, stride=stride) is not None:
                            return True
                    if isinstance(stmt, If) and (
                        _body_has_stack_arrays(stmt.body) or (stmt.else_body is not None and _body_has_stack_arrays(stmt.else_body))
                    ):
                        return True
                    if isinstance(stmt, (DoWhile, While)) and _body_has_stack_arrays(stmt.body):
                        return True
                return False

            if _body_has_stack_arrays(body):
                self.elide_frame = False
            # main(argc, argv) reads its parameters off the kernel-supplied
            # SysV i386 startup frame at [ebp + 4] / [ebp + 8] (see
            # emit_argument_vector_startup).  EBP must point at the saved
            # entry-ESP — keep the prologue so push ebp / mov ebp, esp runs.
            if parameters:
                self.elide_frame = False

        # Globals are visible in every function.  Scalars get a
        # ``_g_<name>`` memory slot; arrays are resolved via the
        # ``_resolve_constant`` path (they behave like a fixed base
        # address, word-strided for ``int`` and byte-strided for
        # ``char``).
        for global_name, declaration in self.global_scalars.items():
            self.variable_types[global_name] = declaration.type_name
            self.visible_vars.add(global_name)
            # File-scope function_pointer globals carry a per-param
            # in_register map.  Re-publish it into the per-function
            # ``function_pointer_in_registers`` dict so indirect call
            # sites and ``__tail_call`` can marshal arguments — the
            # dict is reset to ``{}`` above for each function body.
            if declaration.type_name == "function_pointer" and declaration.function_pointer_params:
                in_regs: dict[int, str] = {}
                for param_index, param in enumerate(declaration.function_pointer_params):
                    if param.in_register is not None:
                        in_regs[param_index] = param.in_register
                if in_regs:
                    self.function_pointer_in_registers[global_name] = in_regs
        for global_name, declaration in self.global_arrays.items():
            self.variable_types[global_name] = declaration.type_name
            self.variable_arrays.add(global_name)
            self.visible_vars.add(global_name)

        # Fastcall routing.  Params 0..N-1 arrive in the
        # fixed register slots (acc, dx, count_register)[0..N-1] and
        # are spilled to local stack slots during the prologue; params
        # N..end use the standard caller-pushed cdecl layout, shifted
        # down by N slots (caller didn't push args 0..N-1).
        is_fastcall = name != "main" and function.regparm_count > 0
        regparm_count = function.regparm_count if is_fastcall else 0
        regparm_registers = (self.target.acc, self.target.dx_register, self.target.count_register)[:regparm_count]
        self._allocate_function_parameters(
            function_line=function.line,
            is_fastcall=is_fastcall,
            name=name,
            parameters=parameters,
            regparm_count=regparm_count,
        )

        self.discover_virtual_long_locals(body)
        self.safe_pin_registers = self.compute_safe_pin_registers(body, parameters=parameters)
        # Exclude regparm params from auto-pin candidates — they're spilled
        # to the stack at prologue entry and the body accesses them through
        # those slots like any other local.
        if name == "main":
            param_candidates = []
        elif is_fastcall:
            param_candidates = [p for p in parameters[regparm_count:] if p.out_register is None and p.in_register is None]
        else:
            param_candidates = [p for p in parameters if p.out_register is None and p.in_register is None]
        # main uses the AST codegen path, which doesn't consult the
        # IR-level liveness pre-pass — its pinned-register saves are
        # always emitted at every call.  Don't subtract pre-store
        # clobbers from the cost gate here or auto-pin will think
        # those saves are free and over-pin.
        self.auto_pin_candidates = self._select_auto_pin_candidates(
            body=body, parameters=param_candidates, apply_liveness_elision=name != "main"
        )

        # Reserve local stack slots for regparm params before scan_locals
        # runs so their offsets are stable against body-local allocations.
        if is_fastcall:
            for i in range(regparm_count):
                self.allocate_local(parameters[i].name)
        # Reserve local slots for in_register params (spilled at prologue entry).
        # Naked functions skip the spill: in_register params are pinned to
        # their register and the body reads them directly without a stack slot.
        #
        # Register-direct TailCall sourcing (param_in_register) is only safe
        # for pure thunks — functions whose entire body is a single TailCall.
        # For any other body shape, intermediate code may clobber the named
        # register between function entry and the tail jump, so the slot
        # reload is still required.
        is_pure_thunk = len(body) == 1 and isinstance(body[0], TailCall)
        for param in parameters:
            if param.in_register is not None:
                if function.naked:
                    self.pinned_register[param.name] = param.in_register
                else:
                    self.allocate_local(param.name)
                    if is_pure_thunk:
                        self.param_in_register[param.name] = param.in_register

        self.scan_locals(body)
        # Type-check every comparison in the body now that ``variable_types``
        # is populated.  The codegen-level check in ``emit_condition`` skips
        # IR-lowered conditions because ``_ir_value_to_ast`` reconstructs
        # operands as bare ``Int`` even when the source was a ``Char``
        # literal; the AST-level walk preserves the original types.
        self.validate_body_comparisons(body)

        # IR path: pre-allocate compiler-generated temporaries so the
        # frame size is correct before the prologue is emitted.
        if ir_body is not None:
            for temp in self._collect_ir_temps(ir_body):
                if temp not in self.locals:
                    self.allocate_local(temp)

        # Non-main: pin parameters that won a candidate slot but weren't
        # claimed during scan_locals.  Parameters that don't fit stay on
        # the stack at [bp+N].
        if name != "main":
            for i, param in enumerate(parameters):
                if is_fastcall and i < regparm_count:
                    continue
                if param.out_register is not None:
                    continue
                if param.in_register is not None:
                    continue
                if param.name not in self.auto_pin_candidates or param.name in self.pinned_register:
                    continue
                self.pinned_register[param.name] = self.auto_pin_candidates[param.name]

        # Seed visible_vars with parameters and pinned variables.
        # Block-scoped locals become visible when their declaration
        # is reached during code generation.
        for param in parameters:
            self.visible_vars.add(param.name)
        self.visible_vars.update(self.pinned_register)
        # IR temps are visible throughout the function and typed as int.
        if ir_body is not None:
            for temp in self._collect_ir_temps(ir_body):
                self.visible_vars.add(temp)
                if temp not in self.variable_types:
                    self.variable_types[temp] = "int"

        # Register calling convention: pinned parameters arrive in their
        # target register (caller loaded them before the call), and
        # non-pinned parameters keep compact [bp+N] offsets that skip
        # register-passed slots.
        register_convention = name != "main" and name in self.register_convention_functions
        if register_convention:
            stack_position = 0
            for param in parameters:
                if param.name in self.pinned_register:
                    continue
                self.locals[param.name] = -(self.target.param_slot_base + stack_position * self.target.int_size)
                stack_position += 1

        if self.object_mode and self.per_function_sections:
            # Per-function section (analog of clang's ``-ffunction-
            # sections``) so ``ld --gc-sections`` can drop unreferenced
            # functions individually.  Required for the libbboeos blob:
            # ctype's ``isalpha`` / ``isdigit`` / etc. are not in
            # ``FUNCTION_POINTER_TABLE`` and must be GC'd to keep
            # ``.libbboeos.rodata`` below the fixed pointer-table
            # anchor at FUNCTION_TABLE + 0xE00.  Off by default because
            # the cc.py ccld pipeline (user programs) expects a
            # monolithic ``.text`` and gets confused by per-function
            # sections.
            self.emit(f"section .text.{name} exec")
        if self.object_mode:
            symbol = self._nasm_symbol(name)
            self._emit_global_export(name)
            self.emit(f"{symbol}:")
        else:
            self.emit(f"{name}:")
        if not self.elide_frame:
            self._emit_function_prologue(
                body=body,
                function_line=function.line,
                is_fastcall=is_fastcall,
                parameters=parameters,
                regparm_count=regparm_count,
                regparm_registers=regparm_registers,
                register_convention=register_convention,
            )

        # IR path: register string literals discovered during IR building.
        self._ir_string_map: dict[str, str] = {}
        if ir_strings:
            for label, content in ir_strings:
                self.strings.append((label, content))
                self._ir_string_map[label] = content

        # Emit argc/argv startup for main with parameters.
        if name == "main" and parameters:
            body = self.emit_argument_vector_startup(parameters, body=body)

        # Fuse trailing printf() calls into die() since main exits implicitly.
        if name == "main":
            body = self.fuse_trailing_printf(body)

        if ir_body is not None:
            # IR path: lower the flat instruction list directly.
            self._ir_call_pinned_initialized = self._compute_pinned_initialized_per_call(ir_body)
            self.lower_ir_body(ir_body)
        else:
            # Tail-call: if the last statement is a statement-level user-
            # function call that qualifies, emit everything before it as
            # usual and lower the trailing call as ``jmp`` (no ``ret``).
            tail_call_last = name != "main" and body and isinstance(body[-1], Call) and self._is_tail_call_eligible(body[-1])
            tail_dispatch_last = name != "main" and not tail_call_last and self._has_tail_dispatch_shape(body)
            if tail_call_last:
                self.generate_body(body[:-1])
                self.generate_call(body[-1], tail_call=True)
            elif tail_dispatch_last:
                self.generate_body(body[:-1])
                self._generate_tail_dispatch_if(body[-1])
            else:
                self.generate_body(body)

        for label_name, ref_line in self.user_labels_referenced.items():
            if label_name not in self.user_labels_defined:
                message = f"goto target '{label_name}' has no matching label in function '{name}'"
                raise CompileError(message, line=ref_line)

        if name == "main":
            self._emit_main_exit_tail()
        elif ir_body is not None:
            # IR path: generate epilogue unless the body always exits.
            # Tail-call optimization is not yet applied on the IR path.
            if not self.elide_frame and not self._always_exits_ir(ir_body):
                if self.frame_size > 0:
                    self.emit(f"        mov {self.target.stack_register}, {self.target.base_register}")
                self.emit(f"        pop {self.target.base_register}")
                for reg in reversed(self.current_preserve_registers):
                    self.emit(f"        pop {reg}")
                self.emit("        ret")
            elif self.elide_frame:
                self.emit("        ret")
        elif tail_call_last or tail_dispatch_last:
            # The tail ``jmp`` already transferred control; no ``ret`` needed.
            pass
        elif self.elide_frame:
            # naked_asm and frameless_calls both skip the prologue, so
            # the epilogue is just ``ret`` — no ``pop bp`` because we
            # didn't push it.
            self.emit("        ret")
        elif not self.always_exits(body):
            if self.frame_size > 0:
                self.emit(f"        mov {self.target.stack_register}, {self.target.base_register}")
            self.emit(f"        pop {self.target.base_register}")
            for reg in reversed(self.current_preserve_registers):
                self.emit(f"        pop {reg}")
            self.emit("        ret")
        self.emit()

    def generate_if(self, statement: If, /) -> None:
        """Generate assembly for an if statement.

        Before emitting anything, checks whether this if begins a
        ``var operation literal`` dispatch chain over a memory-resident local
        (e.g. ``if (c == 1) … else if (c == 2) …``).  If so and AX
        does not already hold the local, hoists a single
        ``mov ax, [_l_var]`` so every subsequent comparison along the
        chain uses the 3-byte ``cmp ax, imm`` form instead of a
        6-byte ``cmp word [mem], imm``.  The else-label snapshot logic
        below preserves AX-tracking through each branch so the chain
        keeps reusing the same load.
        """
        condition, body, else_body = statement.cond, statement.body, statement.else_body
        chain_var = self._dispatch_chain_var(statement)
        if chain_var is not None and chain_var != self.ax_local:
            if self._is_byte_scalar(chain_var):
                self.emit_byte_load_zx(f"[{self._local_address(chain_var)}]")
                self.ax_is_byte = True
            else:
                self.emit(f"        mov {self.target.acc}, [{self._local_address(chain_var)}]")
                self.ax_is_byte = False
            self.ax_local = chain_var
        label_index = self.new_label()
        if else_body is not None:
            self.emit_condition_false_jump(condition=condition, context="if", fail_label=f".if_{label_index}_else")
            # Snapshot AX tracking at the point the fall-through (else)
            # path actually resumes — before body generation disturbs it.
            post_condition_ax = (self.ax_local, self.ax_is_byte)
            self.generate_body(body, scoped=True)
            if_exits = self.always_exits(body)
            if not if_exits:
                self.emit(f"        jmp .if_{label_index}_end")
            self.emit(f".if_{label_index}_else:")
            self.ax_local, self.ax_is_byte = post_condition_ax
            self.generate_body(else_body, scoped=True)
            if not if_exits or not self.always_exits(else_body):
                self.emit(f".if_{label_index}_end:")
            self.ax_clear()
        else:
            self.emit_condition_false_jump(condition=condition, context="if", fail_label=f".if_{label_index}_end")
            post_condition_ax = (self.ax_local, self.ax_is_byte)
            self.generate_body(body, scoped=True)
            self.emit(f".if_{label_index}_end:")
            # If the body always exits its enclosing block (via die,
            # exit, return, or break), the fall-through path resumes
            # with AX tracking as of the end of condition evaluation.
            if self.always_exits(body):
                self.ax_local, self.ax_is_byte = post_condition_ax
            else:
                self.ax_clear()

    def generate_index_assign(self, statement: IndexAssign, /) -> None:
        """Generate assembly for ``array[index] = expr;``.

        When the base pointer lives in memory (not a named constant) and
        a different ``asm_register("si")`` global is active, loading the
        base into SI would clobber that alias — the SI scratch guard
        wraps the store with ``push si`` / ``pop si`` to preserve the
        pinned value.  Matches the read-side guard in generate_expression.
        """
        self.ax_clear()
        name = statement.array.name
        is_byte = self._is_byte_var(name)
        self._check_defined(name, line=statement.line)
        # Pick element width.  Byte arrays / pointers stay on the byte
        # fast path; otherwise consult ``_index_pointee_size`` so
        # halfword (``unsigned short``) targets get a 2-byte store instead of
        # the historical full ``int_size`` store that silently overwrote
        # the next element.  Clamp to ``int_size`` because pointee
        # widths > acc width are handled by ``generate_long_expression``.
        if is_byte:
            element_size = 1
        else:
            element_size = min(self._index_pointee_size(name), self.target.int_size)
        is_halfword = element_size == 2 and element_size < self.target.int_size
        store_width = "byte" if is_byte else ("word" if is_halfword else self.target.word_size)
        store_acc = "al" if is_byte else ("ax" if is_halfword else self.target.acc)
        # Evaluate value into AX, then store at base+index.
        if isinstance(statement.index, Int) and isinstance(statement.expr, Int):
            # Both index and value are constants: direct store.
            offset = statement.index.value * element_size
            const_base = self._resolve_constant(name)
            if const_base is not None:
                addr = f"{const_base}+{offset}" if offset else const_base
                guarded = False
            else:
                guarded = self._si_scratch_guard_begin(name)
                self._emit_load_var(name, register=self.target.si_register)
                si = self.target.si_register
                addr = f"{si}+{offset}" if offset else si
            self.emit(f"        mov {store_width} [{addr}], {statement.expr.value}")
            self._si_scratch_guard_end(guarded=guarded)
        elif isinstance(statement.index, Int):
            # Constant index, variable value.
            offset = statement.index.value * element_size
            self.generate_expression(statement.expr)
            const_base = self._resolve_constant(name)
            if const_base is not None:
                addr = f"{const_base}+{offset}" if offset else const_base
                guarded = False
            else:
                guarded = self._si_scratch_guard_begin(name)
                self._emit_load_var(name, register=self.target.si_register)
                si = self.target.si_register
                addr = f"{si}+{offset}" if offset else si
            self.emit(f"        mov [{addr}], {store_acc}")
            self._si_scratch_guard_end(guarded=guarded)
        else:
            const_base = self._resolve_constant(name)
            if const_base is not None:
                self.emit_constant_reference(name)
                self.generate_expression(statement.expr)
                guarded = self._si_scratch_guard_begin(name)
                addr = self._emit_constant_base_index_addr(
                    const_base=const_base,
                    element_size=element_size,
                    index=statement.index,
                    preserve_ax=True,
                )
                self.emit(f"        mov [{addr}], {store_acc}")
                self._si_scratch_guard_end(guarded=guarded)
                self.ax_clear()
            else:
                # Variable index: compute address in SI, then store.
                # Guard goes OUTSIDE the push/pop ax pair so the pop
                # order matches the push order (push ax..., pop ax, pop si).
                si = self.target.si_register
                pinned_index_register = (
                    self.pinned_register[statement.index.name]
                    if isinstance(statement.index, Var) and statement.index.name in self.pinned_register
                    else None
                )
                # SIB addressing (``[si + idx*k]``) is a 32-bit-mode feature;
                # the 8086/80286 16-bit addressing forms only permit specific
                # BX/BP x SI/DI register pairs, so emitting ``[si+edx]`` in a
                # 16-bit build is rejected by NASM.  Gate the fast path on a
                # 32-bit-or-wider target.
                if (
                    pinned_index_register is not None
                    and pinned_index_register != si
                    and element_size in (1, 2, 4, 8)
                    and self.target.int_size >= 4
                ):
                    # x86 SIB-addressing fast path: when the index lives in a
                    # pinned register distinct from SI, fold the ``add si,
                    # idx*k`` into the store's effective address so the body
                    # is just ``mov acc, expr / mov [si + idx*k], acc`` — no
                    # push/pop, no scratch add, no scale instruction.
                    guarded = self._si_scratch_guard_begin(name)
                    self.generate_expression(statement.expr)
                    self._emit_load_var(name, register=si)
                    scale_suffix = "" if element_size == 1 else f"*{element_size}"
                    self.emit(f"        mov [{si}+{pinned_index_register}{scale_suffix}], {store_acc}")
                    self._si_scratch_guard_end(guarded=guarded)
                    return
                guarded = self._si_scratch_guard_begin(name)
                self.generate_expression(statement.expr)
                self.emit(f"        push {self.target.acc}")
                self._emit_load_var(name, register=si)
                # If the index is a simple Var/Int, evaluating it doesn't
                # clobber SI, so we can skip the push/pop round-trip.
                if isinstance(statement.index, (Var, Int)):
                    self.generate_expression(statement.index)
                    self._emit_scale_index(self.target.acc, scale=element_size)
                    self.emit(f"        add {si}, {self.target.acc}")
                else:
                    self.emit(f"        push {si}")
                    self.generate_expression(statement.index)
                    self._emit_scale_index(self.target.acc, scale=element_size)
                    self.emit(f"        pop {si}")
                    self.emit(f"        add {si}, {self.target.acc}")
                self.emit(f"        pop {self.target.acc}")
                # After pop, AX holds the value being stored, not the index —
                # invalidate the ax_local tracking that generate_expression set.
                self.ax_clear()
                self.emit(f"        mov [{si}], {store_acc}")
                self._si_scratch_guard_end(guarded=guarded)

    def generate_indexed_call(
        self, *, array_name: str, arguments: list[Node], index: Node, line: int, discard_return: bool = False
    ) -> None:
        """Generate assembly for a call through a function-pointer array element.

        Mirrors the indirect-call path in :meth:`generate_call` (the
        ``function_pointer`` variable case) but computes the callee
        address as ``base + index * int_size`` instead of loading a
        named scalar.

        Address computation strategy (mirrors :meth:`generate_index_assign`):

        - Global array: ``lea acc, [_g_name + index*int_size]`` then
          ``mov acc, [acc]`` to load the function pointer.
        - Local stack array: ``lea acc, [bp-offset + index*int_size]``
          then ``mov acc, [acc]``.
        - Either: push args cdecl right-to-left, load the callee
          address into ``acc``, ``call acc``, caller pops args.

        The accumulator is used as the scratch pointer so the call
        sequence matches the existing ``function_pointer`` variable
        path in :meth:`generate_call` and avoids any SI-alias
        interactions.  Args are pushed before the element-address
        computation to free up the accumulator for the address load.
        """
        name = array_name
        self._check_defined(name, line=line)
        self.si_local = None
        clobbers: frozenset[str] = frozenset(self.target.register_pool)
        saved = self._pinned_registers_to_save(clobbers)
        use_pusha = discard_return and len(saved) >= 3
        if use_pusha:
            self.emit("        pusha")
        else:
            for register in saved:
                self.emit(f"        push {register}")
        # Push stack arguments right-to-left (cdecl convention).
        for arg in reversed(arguments):
            self._emit_push_arg(arg)
        # Compute element address into acc (EAX/AX): base + index * int_size.
        # The acc register is free here — all args have been pushed already.
        acc = self.target.acc
        si = self.target.si_register
        index_expression = index
        if name in self.global_arrays:
            global_base = self._global_label(name)
            if isinstance(index_expression, Int):
                element_offset = index_expression.value * self.target.int_size
                addr = f"{global_base}+{element_offset}" if element_offset else global_base
                self.emit(f"        mov {acc}, [{addr}]")
            else:
                # Variable index: evaluate into acc, scale, add to base address.
                # Use SI as base scratch so generate_expression can use acc freely.
                guarded = self._si_scratch_guard_begin(name)
                self.emit(f"        lea {si}, [{global_base}]")
                self.generate_expression(index_expression)
                self._emit_scale_index(acc, scale=self.target.int_size)
                self.emit(f"        add {acc}, {si}")
                self.emit(f"        mov {acc}, [{acc}]")
                self._si_scratch_guard_end(guarded=guarded)
                self.emit(f"        call {acc}")
                if arguments:
                    self.emit(f"        add {self.target.stack_register}, {len(arguments) * self.target.int_size}")
                if use_pusha:
                    self.emit("        popa")
                else:
                    for register in reversed(saved):
                        self.emit(f"        pop {register}")
                self.ax_clear()
                return
        elif name in self.local_stack_arrays:
            if self.elide_frame:
                base_operand = f"_l_{name}"
            else:
                offset_from_bp = self.locals[name]
                base_operand = f"{self.target.base_register}-{offset_from_bp}"
            if isinstance(index_expression, Int):
                element_offset = index_expression.value * self.target.int_size
                addr = f"{base_operand}+{element_offset}" if element_offset else base_operand
                self.emit(f"        mov {acc}, [{addr}]")
            else:
                guarded = self._si_scratch_guard_begin(name)
                self.emit(f"        lea {si}, [{base_operand}]")
                self.generate_expression(index_expression)
                self._emit_scale_index(acc, scale=self.target.int_size)
                self.emit(f"        add {acc}, {si}")
                self.emit(f"        mov {acc}, [{acc}]")
                self._si_scratch_guard_end(guarded=guarded)
                self.emit(f"        call {acc}")
                if arguments:
                    self.emit(f"        add {self.target.stack_register}, {len(arguments) * self.target.int_size}")
                if use_pusha:
                    self.emit("        popa")
                else:
                    for register in reversed(saved):
                        self.emit(f"        pop {register}")
                self.ax_clear()
                return
        # Pointer variable: load base pointer, add scaled index, load function pointer.
        elif isinstance(index_expression, Int):
            element_offset = index_expression.value * self.target.int_size
            self._emit_load_var(name, register=acc)
            if element_offset:
                self.emit(f"        add {acc}, {element_offset}")
            self.emit(f"        mov {acc}, [{acc}]")
        else:
            guarded = self._si_scratch_guard_begin(name)
            self._emit_load_var(name, register=si)
            self.generate_expression(index_expression)
            self._emit_scale_index(acc, scale=self.target.int_size)
            self.emit(f"        add {acc}, {si}")
            self.emit(f"        mov {acc}, [{acc}]")
            self._si_scratch_guard_end(guarded=guarded)
            self.emit(f"        call {acc}")
            if arguments:
                self.emit(f"        add {self.target.stack_register}, {len(arguments) * self.target.int_size}")
            if use_pusha:
                self.emit("        popa")
            else:
                for register in reversed(saved):
                    self.emit(f"        pop {register}")
            self.ax_clear()
            return
        # Simple constant-index case: acc already holds the loaded function pointer.
        self.emit(f"        call {acc}")
        if arguments:
            self.emit(f"        add {self.target.stack_register}, {len(arguments) * self.target.int_size}")
        if use_pusha:
            self.emit("        popa")
        else:
            for register in reversed(saved):
                self.emit(f"        pop {register}")
        self.ax_clear()

    def generate_long_expression(self, expression: Node, /) -> None:
        """Generate code for an ``unsigned long`` expression, leaving the result in DX:AX.

        Only the minimal forms needed by current callers are supported:
        a call to the zero-arg ``datetime()`` builtin, or a reference
        to a local variable of type ``unsigned long``. Anything else
        raises :class:`SyntaxError`.
        """
        if isinstance(expression, Call) and expression.name == "datetime":
            self.generate_call(expression)
            return
        if isinstance(expression, Index):
            # ``unsigned long *p; ... = p[i];`` — read the 32-bit pointee
            # into DX:AX (16-bit) / EAX (32-bit).  The base must be a
            # plain pointer Var.  Constant and simple Var subscripts are
            # supported; more complex index expressions fall through to
            # the unsupported-shape error below.
            base = expression.array
            if isinstance(base, Var) and self.variable_types.get(base.name) == "unsigned long*":
                vname = base.name
                self._check_defined(vname, line=expression.line)
                guarded = self._si_scratch_guard_begin(vname)
                self._emit_load_var(vname, register=self.target.si_register)
                si = self.target.si_register
                # Compute the byte offset from the start of the array.
                index_expression = expression.index
                if isinstance(index_expression, Int):
                    offset = index_expression.value * 4
                    base_address = f"{si}+{offset}" if offset else si
                    if isinstance(self.target, X86CodegenTarget16):
                        self.emit(f"        mov {self.target.acc}, [{base_address}]")
                        self.emit(f"        mov {self.target.dx_register}, [{base_address}+2]")
                    else:
                        self.emit(f"        mov {self.target.acc}, [{base_address}]")
                    self._si_scratch_guard_end(guarded=guarded)
                    self.ax_is_byte = False
                    self.ax_local = None
                    return
                # Non-constant index: scale by 4 then add to SI.
                if isinstance(index_expression, (Var, Int)):
                    self.generate_expression(index_expression)
                    if self.target.int_size == 4:
                        self.emit(f"        shl {self.target.acc}, 2")
                    else:
                        # 16-bit: scale=4 via two add-self operations.
                        self.emit(f"        add {self.target.acc}, {self.target.acc}")
                        self.emit(f"        add {self.target.acc}, {self.target.acc}")
                    self.emit(f"        add {si}, {self.target.acc}")
                    if isinstance(self.target, X86CodegenTarget16):
                        self.emit(f"        mov {self.target.acc}, [{si}]")
                        self.emit(f"        mov {self.target.dx_register}, [{si}+2]")
                    else:
                        self.emit(f"        mov {self.target.acc}, [{si}]")
                    self._si_scratch_guard_end(guarded=guarded)
                    self.ax_is_byte = False
                    self.ax_local = None
                    return
                # Anything fancier (BinaryOperation index, etc.) falls
                # through to the unsupported-shape error.
        if isinstance(expression, Var):
            vname = expression.name
            # Under --bits 32 the parser folds ``unsigned long`` into
            # ``unsigned int`` — same width, single
            # codegen path.  Accept either spelling here so long-returning
            # builtins (``datetime`` / ``print_datetime`` / ``time``) can
            # consume a normal int local just as well as the legacy
            # DX:AX-pair shape that --bits 16 needs.
            actual_type = self.variable_types.get(vname)
            long_compatible = {"unsigned long"}
            if not isinstance(self.target, X86CodegenTarget16):
                long_compatible.add("unsigned int")
            if actual_type not in long_compatible:
                message = f"expected 'unsigned long' expression, got '{actual_type or 'int'}' variable {vname!r}"
                raise CompileError(message, line=expression.line)
            if vname in self.virtual_long_locals:
                if self.live_long_local != vname:
                    message = f"internal: virtual long {vname!r} consumed when not live"
                    raise CompileError(message, line=expression.line)
                self.live_long_local = None
                return
            address = self._local_address(vname)
            if self.elide_frame:
                self.emit(f"        mov {self.target.acc}, [{address}]")
                if isinstance(self.target, X86CodegenTarget16):
                    self.emit(f"        mov {self.target.dx_register}, [{address}+2]")
            else:
                low_offset = self.locals[vname]
                self.emit(f"        mov {self.target.acc}, [{self.target.base_register}-{low_offset}]")
                if isinstance(self.target, X86CodegenTarget16):
                    self.emit(f"        mov {self.target.dx_register}, [{self.target.base_register}-{low_offset - 2}]")
            self.ax_is_byte = False
            self.ax_local = None
            return
        message = f"unsupported 'unsigned long' expression: {type(expression).__name__}"
        raise CompileError(message, line=expression.line)

    def generate_rep_string(self, instruction: ir.RepString) -> None:
        """Lower :class:`ir.RepString` to ``rep movs{b,w,d}`` / ``rep stos{b,w,d}``.

        Loads the destination base into DI, the source base (copy) into
        SI or the fill value (fill) into the accumulator, and the
        iteration count into the count register.  When the loop counter
        is signed (``counter_signed``) a ``test``/``jle`` guard skips the
        ``rep`` for a non-positive count — ``rep`` with ECX interpreted
        as unsigned would otherwise run up to 4 G iterations on a
        negative count.  ``final_iv``, when present, materializes the
        induction variable's post-loop value via the same store path as
        :class:`ir.Copy`.

        The three operand loads (DI=dest, SI=source / AX=fill_value,
        CX=count) are routed through :meth:`_emit_builtin_arg_moves` — the
        same topological scheduler memcpy / memset use — so loading one
        operand into DI/SI can't clobber a pinned-register source another
        operand still needs (e.g. ``count`` pinned to a register the
        ``dest`` load would overwrite).

        Because a rep-string clobbers EDI/ESI/ECX/EAX (see
        :attr:`REP_STRING_CLOBBERS`), any caller pin living in that set is
        push/pop-saved around the ``rep`` exactly as :meth:`generate_call`
        wraps a clobbering builtin.  ``_current_call_pinned_initialized``
        (set by the IR lowering dispatch from
        :meth:`_compute_pinned_initialized_per_call`) filters out pins
        whose local isn't written yet so we never save garbage.
        """
        clobbers = self.REP_STRING_CLOBBERS[instruction.operation]
        saved = self._pinned_registers_to_save(clobbers)
        for register in saved:
            self.emit(f"        push {register}")
        count_register = self.target.count_register
        register_args: list[tuple[str, Node]] = [(self.target.di_register, Var(name=instruction.dest))]
        if instruction.operation == "copy":
            register_args.append((self.target.si_register, Var(name=instruction.source)))
        else:
            register_args.append((self.target.acc, self._ir_value_to_ast(instruction.fill_value)))
        register_args.append((count_register, self._ir_value_to_ast(instruction.count)))
        self._emit_builtin_arg_moves(register_args)
        skip_label: str | None = None
        if instruction.counter_signed:
            skip_label = f".rep_skip_{self.new_label()}"
            self.emit(f"        test {count_register}, {count_register}")
            self.emit(f"        jle {skip_label}")
        if instruction.operation == "copy":
            self._emit_rep_move(element_size=instruction.element_size)
        else:
            self._emit_rep_fill(element_size=instruction.element_size)
        if skip_label is not None:
            self.emit(f"{skip_label}:")
        for register in reversed(saved):
            self.emit(f"        pop {register}")
        if instruction.final_iv is not None:
            iv_name, iv_value = instruction.final_iv
            self.emit_store_local(expression=self._ir_value_to_ast(iv_value), name=iv_name)
        self.ax_clear()

    def generate_return(self, statement: Return, /) -> None:
        """Generate assembly for a return statement.

        In ``main``, ``return`` maps to ``jmp FUNCTION_EXIT`` regardless
        of whether the frame was elided — main has no caller, so a normal
        ``pop bp; ret`` would jump to a garbage address.  In other
        functions it evaluates the return expression into AX, tears down
        the stack frame, and emits ``ret``.  For ``carry_return``
        functions, ``return 1`` / ``return 0`` bypass AX entirely and
        set CF instead (``clc`` / ``stc``); any other return value is
        rejected at codegen time.
        """
        if self.current_function_is_main:
            # main: return [expr]; → SYS_SYS_EXIT.  Evaluate the return
            # expression into AL so the kernel sees the requested exit
            # code (the syscall reads AL).  Bare `return;` defaults to
            # 0 so chains (`cmd && next`) work.  SYS_EXIT discards the
            # program's stack entirely, so the bp frame is left as-is.
            if statement.value is not None:
                self.generate_expression(statement.value)
            else:
                self.emit(f"        xor {self.target.acc}, {self.target.acc}")
            self._emit_libbboeos_jmp("FUNCTION_EXIT")
            return
        if self.current_carry_return:
            value = statement.value
            if isinstance(value, Int) and value.value in (0, 1):
                self.emit("        clc" if value.value == 1 else "        stc")
                if self.frame_size > 0:
                    self.emit(f"        mov {self.target.stack_register}, {self.target.base_register}")
                self.emit(f"        pop {self.target.base_register}")
                for reg in reversed(self.current_preserve_registers):
                    self.emit(f"        pop {reg}")
                self.emit("        ret")
                return
            # Bool-valued expression: evaluate it into the CF via the
            # condition machinery, then tear down the frame.  ``return
            # a || b`` and similar desugar to ``if (expr) { clc; ret; }
            # stc; ret;`` — same two-leg shape the hand-written if
            # pattern produces.
            true_label = f".cret_{self.new_label()}"
            self.emit_condition_true_jump(condition=value, context="return", success_label=true_label)
            self.emit("        stc")
            if self.frame_size > 0:
                self.emit(f"        mov {self.target.stack_register}, {self.target.base_register}")
            self.emit(f"        pop {self.target.base_register}")
            for reg in reversed(self.current_preserve_registers):
                self.emit(f"        pop {reg}")
            self.emit("        ret")
            self.emit(f"{true_label}:")
            self.emit("        clc")
            if self.frame_size > 0:
                self.emit(f"        mov {self.target.stack_register}, {self.target.base_register}")
            self.emit(f"        pop {self.target.base_register}")
            for reg in reversed(self.current_preserve_registers):
                self.emit(f"        pop {reg}")
            self.emit("        ret")
            return
        if statement.value is not None:
            # ``unsigned long *p; return p[0];`` — the pointee is 32
            # bits, so produce the full DX:AX (16-bit) / EAX (32-bit)
            # value via :meth:`generate_long_expression`.  Without this,
            # :meth:`generate_expression` would load only the acc-width
            # low bits and silently truncate the return value on 16-bit.
            if (
                isinstance(statement.value, Index)
                and isinstance(statement.value.array, Var)
                and self.variable_types.get(statement.value.array.name) == "unsigned long*"
            ):
                self.generate_long_expression(statement.value)
            else:
                self.generate_expression(statement.value)
        if self.frame_size > 0:
            self.emit(f"        mov {self.target.stack_register}, {self.target.base_register}")
        self.emit(f"        pop {self.target.base_register}")
        for reg in reversed(self.current_preserve_registers):
            self.emit(f"        pop {reg}")
        self.emit("        ret")

    def generate_statement(self, statement: Node, /) -> None:
        """Generate assembly for a single statement.

        Raises:
            CompileError: If an unknown statement kind is encountered.

        """
        if isinstance(statement, ArrayDecl):
            self.visible_vars.add(statement.name)
            self.variable_types[statement.name] = statement.type_name
            if statement.init is not None and statement.name in self.array_types:
                # Multidimensional scalar array: inline contiguous stack
                # storage (allocated in scan_locals).  Flatten the row-major
                # initializer and store each constant element directly at a
                # constant displacement off the frame slot, then zero-fill
                # the remaining slots (the local stack is NOT pre-zeroed).
                array_type = self.array_types[statement.name]
                total_elements = 1
                dimension = array_type
                while isinstance(dimension, ArrayType):
                    total_elements *= dimension.count
                    dimension = dimension.pointee
                element_size = self._type_size(statement.type_name)
                if element_size == 1:
                    directive = "byte"
                elif element_size == 2:
                    directive = "word"
                else:
                    directive = self.target.word_size
                base = self._local_address(statement.name)
                flat = self._flatten_array_init(statement.init, name=statement.name, total=total_elements, line=statement.line)
                for index in range(total_elements):
                    offset = index * element_size
                    address = f"{base}+{offset}" if offset else base
                    if index < len(flat):
                        element = flat[index]
                        if not isinstance(element, Int):
                            message = "array initializer elements must be constants"
                            raise CompileError(message, line=element.line)
                        value = element.value
                    else:
                        value = 0
                    self.emit(f"        mov {directive} [{address}], {value}")
                self.ax_clear()
            elif statement.init is not None:
                elem_labels = []
                for elem in statement.init.elements:
                    if isinstance(elem, String):
                        elem_labels.append(self.new_string_label(elem.content))
                    elif isinstance(elem, Int):
                        elem_labels.append(str(elem.value))
                    else:
                        message = "array initializer elements must be constants"
                        raise CompileError(message, line=elem.line)
                array_label = f"_arr_{len(self.arrays)}"
                self.arrays.append((array_label, elem_labels))
                self.array_labels[statement.name] = array_label
                self.array_sizes[statement.name] = len(elem_labels)
                self.emit(f"        mov {self.target.word_size} [{self._local_address(statement.name)}], {array_label}")
        elif isinstance(statement, Assign):
            self._check_defined(statement.name, line=statement.line)
            self.emit_store_local(expression=statement.expr, name=statement.name)
        elif isinstance(statement, Break):
            if not self.loop_end_labels:
                message = "break outside of a loop"
                raise CompileError(message, line=statement.line)
            self.emit(f"        jmp {self.loop_end_labels[-1]}")
        elif isinstance(statement, Call):
            self.generate_call(statement, discard_return=True)
            self.ax_clear()
        elif isinstance(statement, Compound):
            self.generate_body(statement.body, scoped=True)
        elif isinstance(statement, Continue):
            if not self.loop_continue_labels:
                message = "continue outside of a loop"
                raise CompileError(message, line=statement.line)
            self.emit(f"        jmp {self.loop_continue_labels[-1]}")
        elif isinstance(statement, DerefIncrementAssign):
            # ``*p++ = expr;`` / ``*p-- = expr;`` (postfix) — evaluate
            # ``expr`` into the accumulator, store through ``p`` at
            # pointee width, then bump ``p`` by ``sizeof(*p)`` bytes.
            # ``*++p = expr;``
            # / ``*--p = expr;`` (prefix) bumps ``p`` *first*, then
            # evaluates and stores through the updated pointer.  Both
            # use the in-place pointer-bump helper (no accumulator
            # touch); the store itself goes via the ESI scratch
            # register so the accumulator survives intact.
            target = statement.target_name
            self._check_defined(target, line=statement.line)
            holder_type = self.variable_types.get(target)
            if not holder_type or not holder_type.endswith("*"):
                message = f"'*{target}++' / '*{target}--' write requires a pointer; got '{holder_type}'"
                raise CompileError(message, line=statement.line)
            pointee_type = holder_type[:-1].rstrip()
            if not statement.is_postfix:
                self._emit_pointer_bump(delta=statement.delta, line=statement.line, name=target)
            self.generate_expression(statement.expr)
            self._emit_load_var(target, register=self.target.si_register)
            if pointee_type in self.BYTE_TYPES:
                width = 1
            elif pointee_type == "unsigned short" and self.target.int_size > 2:
                width = 2
            else:
                width = self.target.int_size
            self._emit_store_accumulator_at_width(destination=f"[{self.target.si_register}]", width=width)
            if statement.is_postfix:
                self._emit_pointer_bump(delta=statement.delta, line=statement.line, name=target)
            self.ax_clear()
        elif isinstance(statement, DoWhile):
            self.ax_clear()
            self.generate_do_while(statement)
        elif isinstance(statement, ExtendedAsm):
            self.generate_extended_asm(statement)
        elif isinstance(statement, For):
            self.ax_clear()
            self.generate_for(statement)
        elif isinstance(statement, Goto):
            self.user_labels_referenced.setdefault(statement.name, statement.line)
            self.emit(f"        jmp .user_{statement.name}")
        elif isinstance(statement, If):
            self.generate_if(statement)
        elif isinstance(statement, IndexAssign):
            self.generate_index_assign(statement)
        elif isinstance(statement, InlineAsm):
            # Empty / inline-asm statement (produced by ``(void)expr;``
            # discard sites and any future statement-level asm escape).
            # Splits on ``\n`` so multi-line content emits one ``emit``
            # per line; empty content emits nothing.
            for line in decode_string_escapes(statement.content).splitlines():
                self.emit(line)
        elif isinstance(statement, Label):
            if statement.name in self.user_labels_defined:
                message = f"duplicate label '{statement.name}'"
                raise CompileError(message, line=statement.line)
            self.user_labels_defined[statement.name] = statement.line
            # A label is a basic-block boundary: any prior fall-through
            # AX / SI tracking is invalid on the jump-arrival path.
            self.ax_clear()
            self.si_local = None
            self.emit(f".user_{statement.name}:")
        elif isinstance(statement, PlaceCall):
            # ``place(args);`` at statement scope — return value discarded.
            self._emit_place_call(statement, discard_return=True)
            self.ax_clear()
        elif isinstance(statement, PlaceIncrementDecrement):
            # ``place++;`` / ``--place;`` at statement scope — value
            # discarded; route through the expression-form codegen.
            self.generate_expression(statement)
            self.ax_clear()
        elif isinstance(statement, PlaceStore):
            self._emit_place_store(statement.place, statement.value)
            self.ax_clear()
        elif isinstance(statement, Return):
            self.generate_return(statement)
        elif isinstance(statement, Switch):
            self.ax_clear()
            self.generate_switch(statement)
        elif isinstance(statement, TailCall):
            self.generate_tail_call(statement)
        elif isinstance(statement, VaArg):
            # ``va_arg(ap, T);`` at statement scope — advance the cursor
            # (side effect) and discard the loaded value.
            self.generate_expression(statement)
            self.ax_clear()
        elif isinstance(statement, VarDecl):
            self.visible_vars.add(statement.name)
            if statement.pointer_array_dimensions is not None:
                # Pointer-to-array: keep the flat pointer string set in
                # scan_locals (the element type would lose the pointer-ness).
                # The init (``int (*p)[3] = g;``) decays the array rvalue to its
                # base address via the normal store path.
                self.variable_types.setdefault(statement.name, f"{statement.type_name}*")
                if statement.init is not None:
                    self.emit_store_local(expression=statement.init, name=statement.name)
                return
            self.variable_types[statement.name] = statement.type_name
            if statement.name in self.constant_aliases:
                return
            if statement.init is not None:
                if isinstance(statement.init, StructInitializer):
                    self._emit_struct_initializer(statement.name, statement.init)
                elif statement.name in self.zero_init_skippable:
                    self.zero_init_skippable.discard(statement.name)
                else:
                    self.emit_store_local(expression=statement.init, name=statement.name)
        elif isinstance(statement, While):
            self.ax_clear()
            self.generate_while(statement)
        else:
            message = f"unknown statement: {type(statement).__name__}"
            raise CompileError(message, line=statement.line)

    def generate_switch(
        self,
        statement: Switch,
        /,
        *,
        cases_override: list | None = None,
        emit_body: Callable[[list[Node]], None] | None = None,
        end_label_override: str | None = None,
    ) -> None:
        """Generate assembly for a ``switch`` statement (compare/jump chain).

        Lowering is intentionally minimal — no jump table.  The
        discriminant is evaluated into the accumulator once; each
        ``case`` arm gets a label, and the prologue emits one
        ``cmp acc, value`` / ``je arm_label`` pair per arm.  After the
        compare chain control falls into the ``default`` arm (if any)
        or jumps past the entire switch.  Each arm's body is then
        emitted sequentially, so omitting ``break`` between adjacent
        arms makes control flow straight into the next one — matching
        standard C fall-through.

        ``break`` inside the switch jumps to the switch's end label
        because we push it onto :attr:`loop_end_labels`.  ``continue``
        does *not* receive a switch entry, so it still applies to the
        enclosing loop (as in C).

        When the discriminant's static type is ``enum NAME`` and no
        ``default`` arm exists, every variant declared for that enum
        must appear as a ``case`` — missing variants raise a compile
        error.  Adding a new enum variant later then flags every
        switch site that forgot it, at compile time, which is the
        whole motivation for fusing the two features.

        ``emit_body`` lets the IR-lowering path substitute a custom
        function for emitting each arm's body (lowering IR
        instructions instead of AST nodes).  The default is
        ``self.generate_body(arm_body, scoped=True)``.

        ``end_label_override`` lets the IR-lowering path supply the
        label that ``break``-derived jumps inside arms already target
        (the IR builder picks it before lowering so each
        :class:`ir.Jump` in the case bodies can reference it).
        """
        if emit_body is None:
            emit_body = lambda arm_body: self.generate_body(arm_body, scoped=True)  # noqa: E731
        default_case, case_arms = self._classify_switch_arms(statement, cases_override=cases_override)
        # Build the compare/jump chain via the existing condition
        # machinery: each ``case CONST:`` is lowered as a synthetic
        # ``discriminant == CONST`` true-jump.  Going through
        # :meth:`emit_condition_true_jump` reuses the byte-vs-word /
        # pinned-register / constant-alias handling already in place,
        # and crucially re-emits the discriminant load before each
        # compare so the per-arm load isn't elided by a peephole pass
        # that assumes the accumulator is dead after the first ``je``.
        label_index = self.new_label()
        end_label = end_label_override if end_label_override is not None else f".switch_{label_index}_end"
        case_labels = [f".switch_{label_index}_case_{index}" for index, _ in enumerate(case_arms)]
        default_label = f".switch_{label_index}_default" if default_case is not None else end_label
        discriminant_line = statement.discriminant.line
        # If the discriminant classifies as char, lower each case
        # label as a Char rather than Int so the comparison validator
        # sees char-vs-char.  The parser's constant-folding pass
        # collapses every case-label expression to Int (Char-ness is
        # only preserved on bare ``'x'`` literals reachable from
        # ``parse_primary``), so the wrap has to happen here.
        case_label_node = Char if self._type_of_operand(statement.discriminant) == "char" else Int
        # Hoist a memory-backed scalar discriminant into AX before the
        # dispatch chain when there are 2+ arms.  Without the hoist,
        # ``emit_comparison``'s "memory scalar compared to constant"
        # fast path emits ``cmp byte [addr], imm`` for every arm —
        # 5-7 bytes each.  With the discriminant resident in AX (and
        # ``ax_local`` set to its name so generate_expression skips a
        # reload), every arm becomes a 2-3 byte ``cmp al, imm`` /
        # ``cmp eax, imm``.  Self-paying for N >= 2 arms.  Pinned-
        # register discriminants already get the register form via the
        # pinned fast path, so no hoist needed for them.
        self.ax_clear()
        discriminant = statement.discriminant
        hoist_eligible = (
            len(case_arms) >= 2
            and isinstance(discriminant, Var)
            and self._is_memory_scalar(discriminant.name)
            and discriminant.name not in self.pinned_register
            and discriminant.name not in self.variable_arrays
            and self.variable_types.get(discriminant.name) != "unsigned long"
        )
        # Interleaved dispatch: when the discriminant is pinned and every arm
        # always-exits (no body-to-body fall-through possible), emit the dispatch
        # and the case body together per arm instead of all dispatches up front.
        # Each arm group becomes ``cmp R, K; jne .next; <body>; jmp .end;
        # .next:`` — the ``jne`` only has to skip the current body (almost
        # always short jump distance), saving 4 bytes per arm versus the
        # separated form's near ``je`` that has to skip every preceding case
        # body.  Multi-label arms (``case A: case B: body;`` represented as
        # adjacent SwitchCases where the earlier one has an empty body) collapse
        # into a single group: each leading label emits ``cmp R, K; je .body``
        # and the terminal label emits ``cmp R, K; jne .next`` so dispatch
        # falls into the shared body.
        interleave_eligible = (
            isinstance(discriminant, Var) and discriminant.name in self.pinned_register and self._switch_can_interleave(case_arms)
        )
        if interleave_eligible:
            # Group cases on body-carrying boundaries: each group is a run of
            # zero-or-more empty-body labels followed by one body-carrying
            # case.  ``_switch_can_interleave`` guarantees the last case has a
            # non-empty body, so every group terminates.
            groups: list[list] = []
            current: list = []
            for case in case_arms:
                current.append(case)
                if case.body:
                    groups.append(current)
                    current = []
            self.loop_end_labels.append(end_label)
            try:
                self._emit_switch_interleaved_arms(
                    case_label_node=case_label_node,
                    default_case=default_case,
                    discriminant=discriminant,
                    discriminant_line=discriminant_line,
                    emit_body=emit_body,
                    groups=groups,
                    label_index=label_index,
                )
            finally:
                self.loop_end_labels.pop()
            self.emit(f"{end_label}:")
            self.ax_clear()
            return
        if hoist_eligible:
            self.generate_expression(discriminant)
        for case, arm_label in zip(case_arms, case_labels, strict=True):
            condition = BinaryOperation(
                left=discriminant,
                line=discriminant_line,
                operation="==",
                right=case_label_node(line=discriminant_line, value=case.value),
            )
            self.emit_condition_true_jump(condition=condition, context="switch", success_label=arm_label)
        self.emit(f"        jmp {default_label}")
        # Push the end label onto the break-target stack so nested
        # ``break`` statements jump out of the switch.  ``continue``
        # falls through to whatever loop encloses the switch (if any)
        # — we don't push a continue label here.
        self.loop_end_labels.append(end_label)
        try:
            self._emit_switch_separated_arms(
                case_arms=case_arms,
                case_labels=case_labels,
                default_case=default_case,
                default_label=default_label,
                emit_body=emit_body,
            )
        finally:
            self.loop_end_labels.pop()
        self.emit(f"{end_label}:")
        self.ax_clear()

    def generate_tail_call(self, statement: TailCall, /) -> None:
        """Generate a ``__tail_call`` tail-dispatch statement.

        Tears down the current frame, loads each argument into its
        declared ``in_register``, loads the function pointer into the
        target register, and emits ``jmp <reg>`` so the callee returns
        directly to the current function's caller — AX and CF flow
        through unchanged.

        The default target is EAX/AX.  A function_pointer local
        declared with ``__attribute__((pinned_register("REG")))``
        already lives in REG; the load is elided and the jump uses REG
        directly.  This lets dispatchers preserve EAX/AL through to
        the handler when AL carries an actual argument (fd_ioctl's
        cmd byte).
        """
        fn = statement.fn
        if fn not in self.variable_types or self.variable_types[fn] != "function_pointer":
            message = f"__tail_call: '{fn}' is not a function_pointer variable"
            raise CompileError(message, line=statement.line)
        function_pointer_in_regs = self.function_pointer_in_registers.get(fn, {})
        if len(statement.args) != len(function_pointer_in_regs):
            message = f"__tail_call: '{fn}' expects {len(function_pointer_in_regs)} argument(s), got {len(statement.args)}"
            raise CompileError(message, line=statement.line)
        if function_pointer_in_regs:
            register_args = [(function_pointer_in_regs[i], arg) for i, arg in enumerate(statement.args)]
            self._emit_register_arg_moves(register_args)
        if fn in self.pinned_register:
            target_register = self.pinned_register[fn]
        else:
            target_register = self.target.acc
            self._emit_load_var(fn, register=target_register)
        if not self.elide_frame:
            if self.frame_size > 0:
                self.emit(f"        mov {self.target.stack_register}, {self.target.base_register}")
            self.emit(f"        pop {self.target.base_register}")
            for reg in reversed(self.current_preserve_registers):
                self.emit(f"        pop {reg}")
        self.emit(f"        jmp {target_register}")

    def generate_while(self, statement: While, /) -> None:
        """Generate assembly for a while loop.

        ``while (1)`` and other statically-nonzero conditions skip the
        header check entirely.  The end label is still emitted so a
        ``break`` statement inside the body has a target; when no
        ``break`` is present the label is dead and costs nothing.
        ``continue`` jumps back to the loop header, re-running the
        condition test.
        """
        condition, body = statement.cond, statement.body
        label_index = self.new_label()
        end_label = f".while_{label_index}_end"
        top_label = f".while_{label_index}"
        self.emit(f"{top_label}:")
        self.loop_end_labels.append(end_label)
        self.loop_continue_labels.append(top_label)
        if self._is_constant_true_condition(condition):
            self.generate_body(body, scoped=True)
        else:
            self.emit_condition_false_jump(condition=condition, context="while", fail_label=end_label)
            self.generate_body(body, scoped=True)
        self.emit(f"        jmp {top_label}")
        self.emit(f"{end_label}:")
        self.loop_continue_labels.pop()
        self.loop_end_labels.pop()
        # A ``break`` can exit the loop with AX holding a value other
        # than the one the final iteration's ``ax_local`` tracking
        # would predict (e.g. ``break`` inside ``char *prev = end - 1;
        # if (prev[0] != ' ') break;`` leaves AX = prev, not end).
        # Invalidate ax_local so downstream code reloads from memory.
        self.ax_clear()

    def lower_ir_body(self, body: list[ir.Instruction]) -> None:
        """Generate x86 assembly from a flat IR instruction list."""
        for instruction in body:
            self._lower_ir_instruction(instruction)
