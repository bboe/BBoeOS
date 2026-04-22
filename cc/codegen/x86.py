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
from typing import ClassVar

from cc import ir
from cc.ast_nodes import (
    ArrayDecl,
    Assign,
    BinaryOperation,
    Break,
    Call,
    Char,
    Continue,
    DoWhile,
    Function,
    If,
    Index,
    IndexAssign,
    InlineAsm,
    Int,
    LogicalAnd,
    LogicalOr,
    Node,
    Param,
    Return,
    SizeofType,
    SizeofVar,
    String,
    Var,
    VarDecl,
    While,
)
from cc.codegen.base import CodeGeneratorBase
from cc.errors import CompileError
from cc.target import CodegenTarget, X86CodegenTarget16, X86CodegenTarget32
from cc.utils import decode_first_character, decode_string_escapes, string_byte_length

JUMP_INVERT = {
    "ja": "jbe",
    "jae": "jb",
    "jb": "jae",
    "jbe": "ja",
    "je": "jne",
    "jg": "jle",
    "jge": "jl",
    "jl": "jge",
    "jle": "jg",
    "jne": "je",
}

JUMP_WHEN_FALSE = {
    "!=": "je",
    "<": "jge",
    "<=": "jg",
    ">": "jle",
    ">=": "jl",
    "==": "jne",
    # Pseudo-operators for ``carry_return`` call conditions.  CF clear
    # means the call reported ``return 1`` (true); CF set means
    # ``return 0`` (false).  ``if (foo())`` dispatches through
    # ``carry`` (jump-false = ``jc``); ``if (foo() == 0)`` through
    # ``not_carry`` (jump-false = ``jnc``).  No real ``cmp`` runs —
    # the ``call`` itself leaves CF holding the result.
    "carry": "jc",
    "not_carry": "jnc",
}

JUMP_WHEN_TRUE = {
    "!=": "jne",
    "<": "jl",
    "<=": "jle",
    ">": "jg",
    ">=": "jge",
    "==": "je",
    "carry": "jnc",
    "not_carry": "jc",
}


class X86CodeGenerator(CodeGeneratorBase):
    """Generates NASM x86 assembly from the parsed AST."""

    BUILTIN_CLOBBERS: ClassVar[dict[str, frozenset[str]]] = {
        "asm": frozenset({"ax", "bx", "cx", "dx", "si", "di"}),
        "checksum": frozenset({"ax", "bx", "cx", "si"}),
        "chmod": frozenset({"ax", "si"}),
        "close": frozenset({"ax", "bx"}),
        "datetime": frozenset({"ax", "dx"}),
        "die": frozenset(),
        "exec": frozenset({"ax", "si"}),
        "exit": frozenset(),
        "far_read16": frozenset({"ax", "bx"}),
        "far_read8": frozenset({"ax", "bx"}),
        "far_write16": frozenset({"ax", "bx"}),
        "far_write8": frozenset({"ax", "bx"}),
        "fstat": frozenset({"ax", "bx", "cx", "dx"}),
        "getchar": frozenset({"ax"}),
        "mac": frozenset({"ax", "di"}),
        "memcpy": frozenset({"ax", "cx", "di", "si"}),
        "mkdir": frozenset({"ax", "si"}),
        "net_open": frozenset({"ax", "dx"}),
        "open": frozenset({"ax", "dx", "si"}),
        "parse_ip": frozenset({"ax", "di", "si"}),
        "print_datetime": frozenset({"ax", "bx", "cx", "dx", "si"}),
        "print_ip": frozenset({"ax", "cx", "si"}),
        "print_mac": frozenset({"ax", "cx", "si"}),
        "printf": frozenset({"ax", "bx", "cx", "dx", "si", "di"}),
        "putchar": frozenset({"ax"}),
        "read": frozenset({"ax", "bx", "cx", "di"}),
        "reboot": frozenset({"ax"}),
        "recvfrom": frozenset({"ax", "bx", "cx", "di", "dx"}),
        "rename": frozenset({"ax", "di", "si"}),
        "sendto": frozenset({"ax", "bx", "cx", "di", "dx", "si"}),
        "set_exec_arg": frozenset({"ax"}),
        "shutdown": frozenset({"ax"}),
        "sleep": frozenset({"ax", "cx"}),
        "strlen": frozenset({"ax", "cx", "di"}),
        "ticks": frozenset({"ax", "cx", "dx"}),
        "uptime": frozenset({"ax"}),
        "video_mode": frozenset({"ax"}),
        "write": frozenset({"ax", "bx", "cx", "si"}),
    }

    ERROR_RETURNING_BUILTINS: ClassVar[frozenset[str]] = frozenset({"chmod", "mac", "mkdir", "parse_ip", "rename"})

    #: Identifiers that resolve to NASM kernel constants rather than
    #: user-defined variables.  Emitted verbatim so NASM can resolve
    #: them from ``constants.asm``.
    NAMED_CONSTANTS: ClassVar[frozenset[str]] = frozenset({
        "ARGV",
        "arp_frame",
        "BUFFER",
        "DIRECTORY_ENTRY_SIZE",
        "DIRECTORY_NAME_LENGTH",
        "DIRECTORY_OFFSET_FLAGS",
        "EDIT_BUFFER_BASE",
        "EDIT_BUFFER_SIZE",
        "EDIT_KILL_BUFFER",
        "EDIT_KILL_BUFFER_SIZE",
        "ERROR_EXISTS",
        "ERROR_NOT_EXECUTE",
        "ERROR_NOT_FOUND",
        "ERROR_PROTECTED",
        "EXEC_ARG",
        "_program_end",
        "FLAG_DIRECTORY",
        "FLAG_EXECUTE",
        "IPPROTO_ICMP",
        "IPPROTO_UDP",
        "MAX_INPUT",
        "NULL",
        "O_CREAT",
        "O_RDONLY",
        "O_TRUNC",
        "O_WRONLY",
        "register_table",
        "SECTOR_BUFFER",
        "SOCK_DGRAM",
        "SOCK_RAW",
        "STDERR",
        "STDIN",
        "STDOUT",
        "STR_ASSIGN",
        "STR_BYTE",
        "STR_DB",
        "STR_DD",
        "STR_DEFINE",
        "STR_DW",
        "STR_DWORD",
        "STR_EQU",
        "STR_INCLUDE",
        "STR_ORG",
        "STR_SHORT",
        "STR_TIMES",
        "STR_WORD",
        "VIDEO_MODE_CGA_320x200",
        "VIDEO_MODE_CGA_640x200",
        "VIDEO_MODE_EGA_320x200_16",
        "VIDEO_MODE_EGA_640x200_16",
        "VIDEO_MODE_EGA_640x350_16",
        "VIDEO_MODE_TEXT_40x25",
        "VIDEO_MODE_TEXT_80x25",
        "VIDEO_MODE_VGA_320x200_256",
        "VIDEO_MODE_VGA_640x480_16",
    })

    #: Named constants that, when referenced, require a NASM %include
    #: directive in the generated output to provide their symbol.
    NAMED_CONSTANT_INCLUDES: ClassVar[dict[str, str]] = {
        "arp_frame": "arp_frame.asm",
    }

    #: Registers available for auto-pinning, in allocation order.  Kept
    #: at the 16-bit names in both modes for now — in 32-bit mode these
    #: refer to the lower halves of the corresponding E-registers, which
    #: works transparently with the existing ``mov ax, <pool>`` accumulator
    #: patterns.  Widening to E-registers would require rewriting those
    #: patterns to use EAX throughout; deferred to the later pass that
    #: widens arithmetic and stack frames together.
    REGISTER_POOL: ClassVar[tuple[str, ...]] = ("dx", "cx", "bx", "di")

    #: Byte-element type names.  ``uint8_t`` shares the ``char``
    #: codegen path (byte array stride, ``mov al`` / ``xor ah, ah``
    #: zero-extend load) but is classified as ``integer`` for
    #: comparison type-checking, so ``uint8_t b; if (b == 0x45)``
    #: works without pretending the literal is a character.
    BYTE_TYPES: ClassVar[frozenset[str]] = frozenset({"char", "uint8_t"})
    BYTE_SCALAR_TYPES: ClassVar[frozenset[str]] = frozenset({"char", "char*", "uint8_t", "uint8_t*"})

    def __init__(self, *, defines: dict[str, str] | None = None, bits: int = 16) -> None:
        """Initialize code generator state.

        ``defines`` is the ``#define`` table the preprocessor collected.
        Each entry is re-emitted as a NASM ``%define NAME VALUE`` at the
        top of the output so inline-asm strings (which cc.py does not
        scan for C macros) can reference the same symbolic names that
        C code uses — otherwise every use inside an ``asm(...)`` string
        would have to spell the literal.

        ``bits`` selects the target: 16 → ``X86CodegenTarget16``,
        32 → ``X86CodegenTarget32``.  All mode-dependent decisions
        (register names, operand widths, type sizes, kernel ABI) live
        in the target object.
        """
        if bits not in (16, 32):
            message = f"unsupported bits={bits}; expected 16 or 32"
            raise ValueError(message)
        self.target: CodegenTarget = X86CodegenTarget32() if bits == 32 else X86CodegenTarget16()
        self.array_labels: dict[str, str] = {}
        self.array_sizes: dict[str, int] = {}
        self.arrays: list[tuple[str, list[str]]] = []
        self.ax_is_byte: bool = False
        self.ax_local: str | None = None
        self.byte_scalar_locals: set[str] = set()
        self.constant_aliases: dict[str, str] = {}
        self.defines: dict[str, str] = dict(defines) if defines else {}

        self.division_remainder: tuple | None = None
        self.elide_frame: bool = False
        self.frame_size: int = 0
        self.global_arrays: dict[str, ArrayDecl] = {}
        self.global_byte_arrays: set[str] = set()
        self.global_scalars: dict[str, VarDecl] = {}
        self.label_id: int = 0
        self.lines: list[str] = []
        self.live_long_local: str | None = None
        self.locals: dict[str, int] = {}
        self.loop_continue_labels: list[str] = []
        self.loop_end_labels: list[str] = []
        self.pinned_register: dict[str, str] = {}
        self.required_includes: set[str] = set()
        self.store_target_register: str | None = None
        self.strings: list[tuple[str, str]] = []
        self.user_functions: dict[str, int] = {}  # name → param count
        self.fastcall_functions: set[str] = set()  # regparm(1) callees: arg 0 in AX
        self.carry_return_functions: set[str] = set()  # callees that return via CF
        self.inline_bodies: dict[str, str] = {}  # always_inline callees: name → raw asm body
        self.inline_call_counter: int = 0  # per-inline-site label-uniquification suffix
        self.register_aliased_globals: dict[str, str] = {}  # name → register (e.g. "si")
        self.variable_arrays: set[str] = set()
        self.variable_types: dict[str, str] = {}
        self.virtual_long_locals: set[str] = set()
        self.visible_vars: set[str] = set()

    def _register_inline_body(self, function: Function, /) -> None:
        """Record an ``always_inline`` function's asm body for splicing.

        The function must have a single ``asm("...")`` statement as its
        entire body.  The raw string (unescaped) is stored; each call
        site pastes it in place of ``call <name>``.  Stack parameters
        are already blocked at parse time (``always_inline`` requires
        0 or regparm(1) params), so callers never need a ``add sp, N``
        cleanup that would fall between the inlined body and the
        following code.
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
            if function.name == "main":
                continue
            self.safe_pin_registers = self.compute_safe_pin_registers(function.body)
            # Fastcall (regparm(1)) param 0 lives in AX on entry and is spilled
            # to a local stack slot in the prologue; it never becomes a pin
            # candidate so auto-pin selection skips it entirely.  Params 1..N
            # of a fastcall function keep the standard stack convention in the
            # MVP — they don't mix with register_convention.
            pin_params = function.params[1:] if function.regparm_count > 0 else function.params
            assignments = self._select_auto_pin_candidates(body=function.body, parameters=pin_params)
            param_pins: dict[int, str] = {}
            for index, param in enumerate(function.params):
                if function.regparm_count > 0 and index == 0:
                    continue
                if param.name in assignments:
                    param_pins[index] = assignments[param.name]
            self.user_function_pin_params[function.name] = param_pins

        has_complex_call: dict[str, bool] = dict.fromkeys(self.user_functions, False)

        def visit(node: Node) -> None:
            if isinstance(node, Call) and node.name in self.user_functions and any(not self._is_simple_arg(arg) for arg in node.args):
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
            return set()
        if isinstance(arg, BinaryOperation):
            return self._arg_pinned_sources(arg.left) | self._arg_pinned_sources(arg.right)
        return set()

    def _byte_index_direct(self, node: Index, /) -> str | None:
        """Return a direct NASM memory operand for a constant-base Index.

        When the base is a named constant or constant alias, returns
        e.g. ``"BUFFER+128+12"`` without emitting any instructions.
        Returns ``None`` for runtime (non-constant) bases.
        """
        vname = node.name
        const_base = self._resolve_constant(vname)
        if const_base is None:
            return None
        offset = node.index.value
        return f"{const_base}+{offset}" if offset else const_base

    def _check_defined(self, name: str, /, *, line: int | None = None) -> None:
        """Raise CompileError if a variable is not in scope."""
        if name in self.NAMED_CONSTANTS:
            return
        if name not in self.visible_vars:
            message = f"undefined variable: {name}"
            raise CompileError(message, line=line)

    def _collect_constant_references(self, node: Node, /) -> set[str]:
        """Return every NAMED_CONSTANT name referenced inside *node*.

        Used by callers of :meth:`_constant_expression` to register
        ``%include`` requirements for constants that need them.  Only
        descends through the same node shapes :meth:`_constant_expression`
        accepts, so the result matches what was actually inlined.
        """
        if isinstance(node, Var) and node.name in self.NAMED_CONSTANTS:
            return {node.name}
        if isinstance(node, BinaryOperation):
            return self._collect_constant_references(node.left) | self._collect_constant_references(node.right)
        return set()

    def _constant_expression(self, init: Node, /) -> str | None:
        """Return a NASM constant expression if *init* is compile-time resolvable.

        Recognizes integer literals, ``NAMED_CONSTANT`` references,
        constant aliases, and arbitrarily-nested ``+``/``-``/``*``
        combinations of those.  Returns a NASM expression string (e.g.
        ``"BUFFER"``, ``"(BUFFER+128)"``, or
        ``"((O_WRONLY+O_CREAT)+O_TRUNC)"``) that the assembler folds at
        link time, or ``None`` if any leaf is not constant.
        """
        if isinstance(init, Int):
            return str(init.value)
        if isinstance(init, Var):
            if init.name in self.NAMED_CONSTANTS:
                return init.name
            if init.name in self.constant_aliases:
                return self.constant_aliases[init.name]
            return None
        if isinstance(init, BinaryOperation) and init.operation in ("+", "-", "*", "&", "|", "^"):
            left = self._constant_expression(init.left)
            right = self._constant_expression(init.right)
            if left is not None and right is not None:
                return f"({left}{init.operation}{right})"
        return None

    def _dispatch_chain_var(self, statement: If, /) -> str | None:
        """Return the local var name shared by an if-else dispatch chain.

        A chain is two or more nested ``if (var operation literal) … else if
        (var operation literal) …`` clauses on the same memory-resident
        local, where each comparison is one of ``==``/``!=``/``<``/
        ``<=``/``>``/``>=`` and the variable always sits on the left.
        Pinned vars, constant aliases, and array bases are excluded —
        their compares already avoid the memory operand.

        Returns the variable's name when hoisting it into AX would
        let two or more comparisons collapse to ``cmp ax, imm``.
        Returns ``None`` for unrelated ifs and single comparisons
        (where the hoist would only break even).
        """
        target: str | None = None
        chain_length = 0
        current: Node | None = statement
        while isinstance(current, If):
            condition = current.cond
            if not (isinstance(condition, BinaryOperation) and condition.operation in ("==", "!=", "<", "<=", ">", ">=")):
                break
            if not (isinstance(condition.left, Var) and isinstance(condition.right, Int)):
                break
            name = condition.left.name
            if not self._is_memory_scalar(name):
                break
            if name in self.pinned_register or name in self.constant_aliases or name in self.variable_arrays:
                break
            if self.variable_types.get(name) == "unsigned long":
                break
            if target is None:
                target = name
            elif target != name:
                break
            chain_length += 1
            else_body = current.else_body
            if else_body is None or len(else_body) != 1:
                break
            current = else_body[0]
        return target if chain_length >= 2 else None

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
        direct = self._byte_index_direct(node)
        if direct is not None:
            return (f"byte [{direct}]", False)
        vname = node.name
        offset = node.index.value
        guarded = self._si_scratch_guard_begin(vname)
        self._emit_load_var(vname, register="si")
        operand = f"byte [si+{offset}]" if offset else "byte [si]"
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
        if not any(register == "si" for register in self.register_aliased_globals.values()):
            return False
        if base_var is not None and self.register_aliased_globals.get(base_var) == "si":
            return False
        self.emit("        push si")
        return True

    def _si_scratch_guard_end(self, *, guarded: bool) -> None:
        """Pair with :meth:`_si_scratch_guard_begin` — emit ``pop si``."""
        if guarded:
            self.emit("        pop si")

    def _emit_constant_base_index_addr(
        self,
        *,
        const_base: str,
        index: Node,
        is_byte: bool,
        preserve_ax: bool,
    ) -> str:
        """Set up ``[CONST + disp + si]`` addressing for a constant-base index.

        Folds a trailing ``±Int`` off a ``Var ± Int`` index into the
        displacement so ``buf[gap_start - 1]`` becomes
        ``[EDIT_BUFFER_BASE-1+si]`` after a single
        ``mov si, [_l_gap_start]``.  Byte-indexed references skip the
        load entirely when the index variable is pinned to DI or BX
        (``[CONST+di]`` / ``[CONST+bx]`` are valid 8086 addressing);
        BP-pinned vars don't qualify because BP would resolve through
        SS, not DS, and CX/DX aren't general index registers in real
        mode either.  This BX/DI restriction is what
        :meth:`_select_auto_pin_candidates` reads via
        ``index_uses`` to keep heavily-subscripted vars off BP.

        When *preserve_ax* is True, any path that evaluates the index
        through AX pushes/pops AX so the caller's value survives.
        """
        element_size = 1 if is_byte else self.target.int_size
        displacement = 0
        if isinstance(index, BinaryOperation) and index.operation in ("+", "-") and isinstance(index.right, Int):
            sign = 1 if index.operation == "+" else -1
            displacement = sign * index.right.value * element_size
            index = index.left
        base_register = "si"
        if isinstance(index, Int):
            displacement += index.value * element_size
            self.emit("        xor si, si")
        elif is_byte and isinstance(index, Var) and index.name in self.pinned_register and self.pinned_register[index.name] in ("di", "bx"):
            base_register = self.pinned_register[index.name]
        elif isinstance(index, Var) and index.name in self.pinned_register:
            self.emit(f"        mov si, {self.target.loword(self.pinned_register[index.name])}")
            if not is_byte:
                if self.target.int_size == 4:
                    self.emit("        shl si, 2")
                else:
                    self.emit("        add si, si")
        elif isinstance(index, Var) and self._is_memory_scalar(index.name) and not self._is_byte_scalar(index.name):
            self.emit(f"        mov si, [{self._local_address(index.name)}]")
            if not is_byte:
                if self.target.int_size == 4:
                    self.emit("        shl si, 2")
                else:
                    self.emit("        add si, si")
        else:
            if preserve_ax:
                self.emit(f"        push {self.target.acc}")
            self.generate_expression(index)
            if not is_byte:
                if self.target.int_size == 4:
                    self.emit(f"        shl {self.target.acc}, 2")
                else:
                    self.emit(f"        add {self.target.acc}, {self.target.acc}")
            self.emit(f"        mov si, {self.target.loword(self.target.acc)}")
            if preserve_ax:
                self.emit(f"        pop {self.target.acc}")
        addr = const_base
        if displacement != 0:
            addr += f"{displacement:+d}"
        addr += f"+{base_register}"
        return addr

    def _emit_global_storage(self) -> None:
        """Emit ``_g_<name>`` data cells for every global, once at tail.

        Scalars lay out as a single ``dw`` with either the constant
        initializer or zero.  Initialized arrays use ``db``/``dw``
        literals matching the element type.  Uninitialized arrays use
        ``times <byte_count> db 0``; byte-granular output keeps the
        self-hosted assembler happy (it only implements the ``times N
        db ...`` form).  Size expressions can reference named constants
        so NASM folds the arithmetic at assemble time.
        """
        if not self.global_scalars and not self.global_arrays:
            return
        self.emit(";; --- global data ---")
        for name in sorted(self.global_scalars):
            declaration = self.global_scalars[name]
            if name in self.register_aliased_globals:
                # Storage lives in the aliased CPU register, not memory,
                # so no ``_g_<name>`` label is emitted.
                continue
            init_expression = "0"
            if declaration.init is not None:
                init_expression = self._constant_expression(declaration.init)
            directive = "db" if self._is_byte_scalar_global(name) else "dw"
            self.emit(f"_g_{name}: {directive} {init_expression}")
        for name in sorted(self.global_arrays):
            declaration = self.global_arrays[name]
            stride = 1 if declaration.type_name in self.BYTE_TYPES else 2
            if declaration.init is not None:
                directive = "db" if declaration.type_name in self.BYTE_TYPES else "dw"
                rendered = [
                    self.new_string_label(element.content) if isinstance(element, String) else self._constant_expression(element)
                    for element in declaration.init.elements
                ]
                self.emit(f"_g_{name}: {directive} {', '.join(rendered)}")
            else:
                size_expression = self._constant_expression(declaration.size)
                byte_count = f"({size_expression})*{stride}" if stride != 1 else size_expression
                self.emit(f"_g_{name}: times {byte_count} db 0")

    def _emit_load_var(self, name: str, /, *, register: str = "bx") -> None:
        """Load a variable's value into *register*.

        Checks pinned registers first, then constant aliases, then
        falls back to the memory frame slot.
        """
        if name in self.pinned_register:
            source = self.pinned_register[name]
            if len(register) < len(source):
                source = self.target.loword(source)
            self.emit(f"        mov {register}, {source}")
        elif name in self.register_aliased_globals:
            source = self.register_aliased_globals[name]
            if len(register) < len(source):
                source = self.target.loword(source)
            if source != register:
                self.emit(f"        mov {register}, {source}")
        elif name in self.constant_aliases:
            self.emit(f"        mov {register}, {self.constant_aliases[name]}")
        else:
            self.emit(f"        mov {register}, [{self._local_address(name)}]")

    def _emit_syscall(self, name: str, /) -> None:
        """Emit the invocation sequence for a named kernel syscall.

        Looks up :attr:`SYSCALL_SEQUENCES` and emits one instruction per
        entry.  This is the only path by which cc.py-generated C code
        reaches the kernel, so retargeting the OS to a different ABI
        (e.g., protected-mode ``syscall`` / ``sysenter``) is done by
        editing that table — no per-builtin edits required.
        """
        if name not in self.target.syscall_sequences:
            message = f"unknown syscall: {name!r}"
            raise CompileError(message)
        for instruction in self.target.syscall_sequences[name]:
            self.emit(f"        {instruction}")

    @staticmethod
    def _extract_local_label(line: str, /) -> str | None:
        """Return the _l_ label from a store or declaration, or None.

        Stops at the first non-identifier byte so a byte-offset store
        like ``mov [_l_sum+1], al`` still resolves to ``_l_sum`` — the
        same way peephole_dead_stores resolves reads.
        """
        # Store: mov [_l_NAME], ... or mov word [_l_NAME], ...
        if line.startswith("mov") and "[_l_" in line and "], " in line:
            start = line.index("[_l_") + 1
            end = start
            while end < len(line) and (line[end].isalnum() or line[end] == "_"):
                end += 1
            return line[start:end]
        # Declaration: _l_NAME: dw 0
        if line.startswith("_l_") and line.endswith(": dw 0"):
            return line[: line.index(":")]
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

    def _index_cache_key(self, expression: Node, /) -> tuple[str, int] | None:
        """Return the register cache key for an index expression, or None."""
        if isinstance(expression, Index) and isinstance(expression.index, Int) and expression.name in self.array_labels:
            return (self.array_labels[expression.name], expression.index.value * 2)
        return None

    def _is_byte_eq(self, node: Node, /) -> bool:
        """Check if a node is ``byte_index == <something>``."""
        return isinstance(node, BinaryOperation) and node.operation == "==" and self._is_byte_index(node.left)

    def _is_byte_index(self, node: Node, /) -> bool:
        """Check if a node is a constant-subscript byte index."""
        return (
            isinstance(node, Index)
            and isinstance(node.index, Int)
            and node.name not in self.array_labels
            and node.name in self.visible_vars
            and self.variable_types.get(node.name) in self.BYTE_SCALAR_TYPES
        )

    def _is_byte_var(self, name: str, /) -> bool:
        """Return True if *name* is a byte-sized element source.

        Covers ``char`` / ``uint8_t`` scalars and pointers, and
        file-scope byte-element arrays (``char NAME[SIZE];`` or
        ``uint8_t NAME[SIZE];``).  Locally-declared arrays and
        ``int``-typed globals keep word-sized element access — only
        the explicit byte-typed path widens to byte semantics.
        """
        if name in self.global_byte_arrays:
            return True
        return name not in self.variable_arrays and self.variable_types.get(name) in self.BYTE_SCALAR_TYPES

    def _is_byte_scalar_global(self, name: str, /) -> bool:
        """Return True if *name* is a byte-typed (``char`` / ``uint8_t``) scalar global.

        Byte-scalar globals store as a single ``db`` cell at
        ``_g_<name>``; load and store go through the byte-wide path
        shared with byte-scalar locals (see :meth:`_is_byte_scalar`).
        Register-aliased globals live in a CPU register rather than
        memory, so they stay out of this set — storage is not a
        ``db`` cell.
        """
        if name not in self.global_scalars:
            return False
        if name in self.register_aliased_globals:
            return False
        return self.variable_types.get(name) in self.BYTE_TYPES

    def _is_byte_scalar_local(self, name: str, /) -> bool:
        """Return True if *name* is a byte-typed scalar local with a 1-byte slot.

        Populated by :meth:`scan_locals` — only non-parameter body
        locals whose type is in :attr:`BYTE_TYPES` and which weren't
        routed into a pinned register qualify.  Parameters keep their
        word slots (caller pushes a full word) and pinned locals
        aren't in :attr:`locals` at all.
        """
        return name in self.byte_scalar_locals

    def _is_byte_scalar(self, name: str, /) -> bool:
        """Return True for any byte-wide memory scalar (global or local).

        Collapses the global / local check used by every fast-path
        gate that must bail when the operand's storage is a single
        byte — the word-sized forms (``cmp ax, [addr]``,
        ``cmp word [addr], imm``, ``add ax, [addr]``, ``mov <r16>,
        [addr]``) would read the adjacent byte into the high byte
        and silently corrupt the result.
        """
        return self._is_byte_scalar_global(name) or self._is_byte_scalar_local(name)

    def _is_constant_alias(self, *, body: list[Node], statement: VarDecl) -> bool:
        """Check if ``statement`` is a compile-time constant alias.

        True when the local is initialized from a ``NAMED_CONSTANT``
        or ``NAMED_CONSTANT +/- Int`` and never reassigned in ``body``.
        Such locals can be replaced with the underlying constant
        expression directly, skipping the memory slot, the initial
        store, and every reload.
        """
        if statement.type_name == "unsigned long":
            return False
        if self._constant_expression(statement.init) is None:
            return False
        return not any(self._name_is_reassigned(name=statement.name, node=stmt) for stmt in body)

    def _is_memory_scalar(self, name: str, /) -> bool:
        """Return True when *name* is a memory-resident scalar.

        Covers frame-based locals (``self.locals``) and file-scope
        scalars (``self.global_scalars``).  Call sites use this to
        decide whether a ``Var`` can be addressed directly via ``[mem]``
        instead of being loaded into AX first.  Register-aliased globals
        (``asm_register``) report False because their storage is a CPU
        register, not a memory slot.
        """
        if name in self.register_aliased_globals:
            return False
        return name in self.locals or name in self.global_scalars

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
                return f"{self.target.bp_register}-{offset}"
            return f"{self.target.bp_register}+{-offset}"
        if name in self.register_aliased_globals:
            message = f"register-aliased global '{name}' has no memory address"
            raise CompileError(message)
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
        elif isinstance(arg, Var) and arg.name in self.pinned_register:
            self.emit(f"        push {self.pinned_register[arg.name]}")
        else:
            self.generate_expression(arg)
            self.emit("        push ax")

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
                    self.emit(f"        mov {target}, {self.target.loword(source)}")
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
        elif isinstance(arg, Var):
            if self._is_byte_scalar(arg.name):
                # Byte-scalar source into a word target: byte-load +
                # zero-extend, then shuttle into the target if it
                # isn't acc already.
                self.emit(f"        mov al, [{self._local_address(arg.name)}]")
                self.emit("        xor ah, ah")
                if target != self.target.acc:
                    source = self.target.loword(self.target.acc) if len(target) < len(self.target.acc) else self.target.acc
                    self.emit(f"        mov {target}, {source}")
            else:
                self.emit(f"        mov {target}, [{self._local_address(arg.name)}]")
        elif isinstance(arg, BinaryOperation):
            # ``_is_simple_arg`` only admits BinaryOperation(+/-, leaf, leaf), and
            # the topological scheduler in ``_emit_register_arg_moves``
            # already verified that ``target`` is not read by any other
            # pending arg.  Evaluate into AX, then move into target.
            self.generate_expression(arg)
            if target != self.target.acc:
                source = self.target.loword(self.target.acc) if len(target) < len(self.target.acc) else self.target.acc
                self.emit(f"        mov {target}, {source}")
        else:
            message = f"register-arg target {target} given unexpected complex node {arg!r}"
            raise CompileError(message, line=getattr(arg, "line", None))

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
        All three leave AX holding something other than the new
        stored value, so the ``ax_local`` tracking the caller just
        set (pointing at the store's destination local) would
        mislead later reads into skipping a reload and picking up
        stale contents.

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
        ``bx``, etc.).  In 32-bit mode the register pool holds
        E-register names, so we normalise both sides through
        ``target.loword`` before comparing.
        """
        loword = self.target.loword
        return sorted(register for register in self.pinned_register.values() if loword(register) in clobbers and loword(register) != "ax")

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
                    self.register_aliased_globals[name] = declaration.asm_register
                self.global_scalars[name] = declaration
            elif isinstance(declaration, ArrayDecl):
                if declaration.type_name not in ("char", "int", "uint8_t"):
                    message = f"global array '{name}' must have element type 'char', 'int', or 'uint8_t'"
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
                    for element in declaration.init.elements:
                        if isinstance(element, String):
                            continue
                        if self._constant_expression(element) is None:
                            message = "global array initializer elements must be constants"
                            raise CompileError(message, line=element.line)
                        for reference in self._collect_constant_references(element):
                            self.emit_constant_reference(reference)
                self.global_arrays[name] = declaration
            else:
                message = f"unexpected top-level declaration: {type(declaration).__name__}"
                raise CompileError(message, line=declaration.line)

    def _resolve_constant(self, name: str, /) -> str | None:
        """Return the NASM constant expression for *name*, or ``None``.

        Checks :attr:`constant_aliases` first, then
        :attr:`NAMED_CONSTANTS`, then file-scope arrays (whose
        ``_g_<name>`` label is itself a fixed address).  Used wherever
        the code needs to distinguish compile-time-constant bases from
        runtime variables.
        """
        alias = self.constant_aliases.get(name)
        if alias is not None:
            return alias
        if name in self.NAMED_CONSTANTS:
            return name
        if name in self.global_arrays:
            return f"_g_{name}"
        return None

    def _select_auto_pin_candidates(self, *, body: list[Node], parameters: list) -> dict[str, str]:
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

        param_candidates: list[tuple[str, int]] = []
        for order, param in enumerate(parameters):
            if param.is_array:
                continue
            param_candidates.append((param.name, order))

        body_candidates: list[tuple[str, int]] = []
        order = 0

        def collect(nodes: list[Node], *, top_level: bool) -> None:
            nonlocal order
            for statement in nodes:
                if isinstance(statement, VarDecl):
                    eligible = (
                        statement.type_name != "unsigned long"
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
                elif isinstance(statement, (DoWhile, While)):
                    collect(statement.body, top_level=False)

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
            if isinstance(node, (Var, Assign, Index, IndexAssign)):
                counts[node.name] = counts.get(node.name, 0) + 1
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
                collect_index_vars(node.index)
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

        def rank(items: list[tuple[str, int]]) -> list[tuple[str, int]]:
            return sorted(items, key=lambda item: (-counts.get(item[0], 0), item[1]))

        combined = rank(body_candidates) + rank(param_candidates)
        # Drop expression-temporary vars: pinning them adds a 2-byte
        # ``mov pin, ax`` after their single complex-expression
        # initializer without shrinking the comparisons that follow
        # (those already work against AX).
        combined = [item for item in combined if not is_expression_temporary(item[0])]
        assignments: dict[str, str] = {}
        available = list(self.safe_pin_registers)
        for name, _ in combined:
            if not available:
                break
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
                break
            refs = counts.get(name, 0)
            if refs > self.register_clobber_counts.get(chosen, 0):
                assignments[name] = chosen
                available.remove(chosen)
            else:
                break
        return assignments

    def _transform_branch_printf(self, body: list[Node], /) -> list[Node]:
        """Replace trailing simple printf(msg) with die(msg) in a branch body."""
        if body and self._is_simple_printf(body[-1]):
            last = body[-1]
            return [*body[:-1], Call(args=last.args, line=last.line, name="die")]
        return body

    def _transform_if_printf(self, statement: If, /) -> If:
        """Transform simple printf() at end of if-else branches into die()."""
        condition, if_body, else_body = statement.cond, statement.body, statement.else_body
        new_if = self._transform_branch_printf(if_body)
        new_else = else_body
        if else_body is not None:
            if len(else_body) == 1 and isinstance(else_body[0], If):
                transformed = self._transform_if_printf(else_body[0])
                if transformed is not else_body[0]:
                    new_else = [transformed]
            else:
                new_else = self._transform_branch_printf(else_body)
        if new_if is if_body and new_else is else_body:
            return statement
        return If(body=new_if, cond=condition, else_body=new_else, line=statement.line)

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

    def _type_of_operand(self, node: Node, /) -> str:
        """Classify an operand for comparison type-checking.

        Returns one of ``"pointer"``, ``"null"``, ``"char"``, or
        ``"integer"``.  Every AST node that can legally appear inside
        a comparison must classify into one of the four buckets;
        anything else raises ``CompileError`` so no operand silently
        slips through the type check.  ``uint8_t`` values are byte-sized
        like ``char`` but classify as ``integer`` so they compose freely
        with integer literals — ``char`` stays restricted to catch
        ``c == 0`` typos.
        """
        if isinstance(node, Char):
            return "char"
        if isinstance(node, Int):
            return "integer"
        if isinstance(node, String):
            return "pointer"
        if isinstance(node, Index):
            variable_type = self.variable_types.get(node.name)
            if variable_type in ("char", "char*"):
                return "char"
            return "integer"
        if isinstance(node, Var):
            if node.name == "NULL":
                return "null"
            variable_type = self.variable_types.get(node.name)
            if variable_type in ("char*", "uint8_t*"):
                return "pointer"
            if variable_type == "char":
                return "char"
            if node.name in self.variable_types or node.name in self.NAMED_CONSTANTS:
                return "integer"
            message = f"undefined operand: {node.name}"
            raise CompileError(message, line=node.line)
        if isinstance(node, (BinaryOperation, Call, LogicalAnd, LogicalOr, SizeofType, SizeofVar)):
            return "integer"
        message = f"cannot classify operand type for comparison: {type(node).__name__}"
        raise CompileError(message, line=node.line)

    def allocate_local(self, name: str, /, *, size: int = 2) -> int:
        """Allocate a local variable on the stack frame.

        Args:
            name: local variable name.
            size: slot size in bytes (2 for ints/pointers, 4 for unsigned long).

        Returns:
            The current frame size after allocation.

        """
        self.frame_size += size
        self.locals[name] = self.frame_size
        return self.frame_size

    def ax_clear(self) -> None:
        """Clear AX tracking state."""
        self.ax_is_byte = False
        self.ax_local = None

    def builtin_asm(self, arguments: list[Node], /) -> None:
        r"""Emit an inline-asm string literal verbatim.

        Takes one string literal; C escape sequences (``\n``, ``\t``,
        ``\\``, ``\x??``) are decoded, and the result is split on
        newlines and emitted as individual lines so multi-instruction
        blocks can be written as ``asm("mov ax, 0\nmov es, ax");``.
        Pinned register values are conservatively assumed clobbered
        (see ``BUILTIN_CLOBBERS``); AX tracking is invalidated.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="asm")
        argument = arguments[0]
        if not isinstance(argument, String):
            message = "asm() argument must be a string literal"
            raise CompileError(message, line=argument.line)
        for line in decode_string_escapes(argument.content).splitlines():
            self.emit(line)
        self.ax_clear()

    def builtin_checksum(self, arguments: list[Node], /) -> None:
        """Generate code for the checksum(buf, len) builtin.

        Computes the 1's-complement 16-bit checksum used by IP and ICMP.
        ``len`` must be even; caller is responsible for zero-padding
        odd-length buffers.  Returns the folded, complemented checksum
        in AX, ready to store in the header field.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="checksum")
        buffer_argument, length_argument = arguments
        self.emit_si_from_argument(buffer_argument)
        self.emit_register_from_argument(argument=length_argument, register="cx")
        label_index = self.new_label()
        self.emit("        cld")
        self.emit("        xor bx, bx")
        self.emit("        shr cx, 1")
        self.emit(f".ck_loop_{label_index}:")
        self.emit("        lodsw")
        self.emit("        add bx, ax")
        self.emit("        adc bx, 0")
        self.emit(f"        loop .ck_loop_{label_index}")
        self.emit("        not bx")
        self.emit("        mov ax, bx")
        self.ax_clear()

    def builtin_chmod(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the chmod() builtin.

        Returns 0 on success or an ERR_* code on failure.  When
        *fuse_exit* is True, emits ``jnc FUNCTION_EXIT`` instead of
        converting the carry flag to a 0-or-error integer.  When
        *fuse_die* is set, emits a direct ``jc FUNCTION_DIE`` with the
        given message preloaded in SI/CX.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="chmod")
        self.emit_si_from_argument(arguments[0])
        self.generate_expression(arguments[1])
        self._emit_syscall("FS_CHMOD")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=True)

    def builtin_close(self, arguments: list[Node], /) -> None:
        """Generate code for the close() builtin.

        Closes a file descriptor.  ``close(fd)`` emits
        ``mov bx, <fd> / mov ah, SYS_IO_CLOSE / int 30h``.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="close")
        self.emit_register_from_argument(argument=arguments[0], register="bx")
        self._emit_syscall("IO_CLOSE")

    def builtin_datetime(self, arguments: list[Node], /) -> None:
        """Generate code for the datetime() builtin.

        Returns unsigned seconds since 1970-01-01 UTC in DX:AX. Valid
        through the year 2106 (32-bit epoch overflow).
        """
        self._check_argument_count(arguments=arguments, expected=0, name="datetime")
        self._emit_syscall("RTC_DATETIME")

    def builtin_die(self, arguments: list[Node], /) -> None:
        """Generate code for the die() builtin.

        Pre-loads SI and CX (string + length) and jumps to a shared
        ``.die`` label that calls ``write_stdout`` then exits.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="die")
        argument = arguments[0]
        if not isinstance(argument, String):
            message = "die() requires a string literal"
            raise CompileError(message, line=argument.line)
        label = self.new_string_label(argument.content)
        length = string_byte_length(argument.content)
        self.emit(f"        mov si, {label}")
        self.emit(f"        mov {self.target.count_register}, {length}")
        self.emit("        jmp FUNCTION_DIE")

    def builtin_exec(self, arguments: list[Node], /) -> None:
        """Generate code for the exec(name) builtin.

        Emits ``mov si, <name> / mov ah, SYS_EXEC / int 30h``.  On
        success, control is transferred to the loaded program and never
        returns here.  On failure (CF set), AL contains an ``ERROR_*``
        code; ``xor ah, ah`` zero-extends it for comparison against
        ``ERROR_NOT_EXECUTE`` etc.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="exec")
        self.emit_si_from_argument(arguments[0])
        self._emit_syscall("EXEC")
        self.emit("        xor ah, ah")
        self.ax_clear()

    def builtin_exit(self, arguments: list[Node], /) -> None:
        """Generate code for the exit() builtin."""
        self._check_argument_count(arguments=arguments, expected=0, name="exit")
        self.emit("        jmp FUNCTION_EXIT")

    def builtin_far_read16(self, arguments: list[Node], /) -> None:
        """Generate code for the ``far_read16(offset)`` builtin.

        Reads a 16-bit word at ``ES:offset``.  In real mode, emits
        ``mov bx, <offset> / mov ax, [es:bx]``.  This is the paired
        read half of the far-memory accessors used by asm.c's symbol
        table (which lives in SYMBOL_SEGMENT rather than DS).  When
        the OS later moves to protected mode with a flat address
        space, this builtin can emit a plain ``mov ax, [<offset>]``
        without touching C callers.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="far_read16")
        self.emit_register_from_argument(argument=arguments[0], register="bx")
        self.emit(f"        mov {self.target.acc}, {self.target.far_ref('bx')}")
        self.ax_clear()

    def builtin_far_read8(self, arguments: list[Node], /) -> None:
        """Generate code for the ``far_read8(offset)`` builtin.

        Reads a byte at ``ES:offset`` zero-extended into AX.  Emits
        ``mov bx, <offset> / mov al, [es:bx] / xor ah, ah`` in real
        mode; protected-mode retargeting would drop the ES prefix
        and leave the byte load unchanged.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="far_read8")
        self.emit_register_from_argument(argument=arguments[0], register="bx")
        self.emit(f"        mov al, {self.target.far_ref('bx')}")
        self.emit("        xor ah, ah")
        self.ax_clear()

    def builtin_far_write16(self, arguments: list[Node], /) -> None:
        """Generate code for the ``far_write16(offset, value)`` builtin.

        Stores a 16-bit word to ``ES:offset``.  When the value is a
        constant, emits ``mov bx, <offset> / mov word [es:bx],
        <value>`` (single store).  For register / local / expression
        values, the value lands in AX first (pushed if the offset
        eval could clobber it) and stores via ``mov [es:bx], ax``.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="far_write16")
        offset_argument, value_argument = arguments
        if isinstance(value_argument, Int):
            self.emit_register_from_argument(argument=offset_argument, register="bx")
            self.emit(f"        mov {self.target.word_size} {self.target.far_ref('bx')}, {value_argument.value & 0xFFFF}")
        else:
            self.emit_register_from_argument(argument=value_argument, register=self.target.acc)
            self.emit(f"        push {self.target.acc}")
            self.emit_register_from_argument(argument=offset_argument, register="bx")
            self.emit(f"        pop {self.target.acc}")
            self.emit(f"        mov {self.target.far_ref('bx')}, {self.target.acc}")
        self.ax_clear()

    def builtin_far_write8(self, arguments: list[Node], /) -> None:
        """Generate code for the ``far_write8(offset, value)`` builtin.

        Stores a byte to ``ES:offset``.  Shape mirrors
        :meth:`builtin_far_write16`: constant values compile to a
        single ``mov byte [es:bx], <value>`` store; non-constant
        values route through AX with a push/pop guard around the
        offset evaluation.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="far_write8")
        offset_argument, value_argument = arguments
        if isinstance(value_argument, Int):
            self.emit_register_from_argument(argument=offset_argument, register="bx")
            self.emit(f"        mov byte {self.target.far_ref('bx')}, {value_argument.value & 0xFF}")
        else:
            self.emit_register_from_argument(argument=value_argument, register=self.target.acc)
            self.emit(f"        push {self.target.acc}")
            self.emit_register_from_argument(argument=offset_argument, register="bx")
            self.emit(f"        pop {self.target.acc}")
            self.emit(f"        mov {self.target.far_ref('bx')}, al")
        self.ax_clear()

    def builtin_fstat(self, arguments: list[Node], /) -> None:
        """Generate code for the fstat() builtin.

        ``fstat(fd)`` emits ``mov bx, <fd> / mov ah, SYS_IO_FSTAT /
        int 30h``.  Returns the file mode (flags byte) in AX.
        The syscall also returns CX:DX = file size, but those are
        discarded here.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="fstat")
        self.emit_register_from_argument(argument=arguments[0], register="bx")
        self._emit_syscall("IO_FSTAT")
        self.emit("        xor ah, ah")
        self.ax_clear()

    def builtin_getchar(self, arguments: list[Node], /) -> None:
        """Generate code for the getchar() builtin.

        Reads a single byte from stdin (blocking) via
        FUNCTION_GET_CHARACTER.  Returns the byte zero-extended in AX.
        """
        self._check_argument_count(arguments=arguments, expected=0, name="getchar")
        self.emit("        call FUNCTION_GET_CHARACTER")
        self.emit("        xor ah, ah")
        self.ax_clear()

    def builtin_mac(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the mac(buffer) builtin.

        Reads the cached NIC MAC address (6 bytes) into ``buffer``.
        Returns 0 on success, 1 if no NIC is present.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="mac")
        self.emit_register_from_argument(argument=arguments[0], register="di")
        self._emit_syscall("NET_MAC")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=False)

    def builtin_memcpy(self, arguments: list[Node], /) -> None:
        """Generate code for the memcpy(destination, source, n) builtin.

        Emits ``mov di, <destination> / mov si, <source> / mov cx, <n>
        / cld / rep movsb``.  Byte-wise copy; caller's DI, SI, CX are
        clobbered.
        """
        self._check_argument_count(arguments=arguments, expected=3, name="memcpy")
        destination_argument, source_argument, count_argument = arguments
        self.emit_register_from_argument(argument=destination_argument, register="di")
        self.emit_register_from_argument(argument=source_argument, register="si")
        self.emit_register_from_argument(argument=count_argument, register="cx")
        self.emit("        cld")
        self.emit("        rep movsb")
        self.ax_clear()

    def builtin_mkdir(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the mkdir() builtin.

        Returns 0 on success or an ERR_* code on failure.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="mkdir")
        self.emit_si_from_argument(arguments[0])
        self._emit_syscall("FS_MKDIR")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=True)

    def builtin_net_open(self, arguments: list[Node], /) -> None:
        """Generate code for the net_open(type, protocol) builtin.

        ``net_open(type, protocol)`` emits ``mov al, <type> /
        mov dl, <protocol> / mov ah, SYS_NET_OPEN / int 30h`` where type
        is SOCK_RAW (0) or SOCK_DGRAM (1) and protocol is IPPROTO_UDP (17)
        or IPPROTO_ICMP (1) for datagram sockets (ignored for raw
        Ethernet sockets — pass 0).  Returns fd in AX on success, or -1
        if no NIC is present.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="net_open")
        type_argument, protocol_argument = arguments
        if isinstance(type_argument, Int) or (isinstance(type_argument, Var) and type_argument.name in self.NAMED_CONSTANTS):
            self.emit(f"        mov al, {type_argument.value if isinstance(type_argument, Int) else type_argument.name}")
        else:
            self.generate_expression(type_argument)
        self.emit_register_from_argument(argument=protocol_argument, register="dl")
        self._emit_syscall("NET_OPEN")
        label_index = self.new_label()
        self.emit(f"        jnc .ok_{label_index}")
        self.emit("        mov ax, -1")
        self.emit(f".ok_{label_index}:")
        self.ax_clear()

    def builtin_open(self, arguments: list[Node], /) -> None:
        """Generate code for the open() builtin.

        ``open(name, flags)`` or ``open(name, flags, mode)`` emits
        ``mov si, <name> / mov al, <flags> / [mov dl, <mode>] /
        mov ah, SYS_IO_OPEN / int 30h``.  The optional *mode*
        parameter sets the file permission flags (e.g. ``FLAG_EXECUTE``)
        when ``O_CREAT`` creates a new file.  Returns the fd number
        in AX, or -1 on error (CF set).
        """
        if len(arguments) < 2 or len(arguments) > 3:
            message = "open() expects 2 or 3 arguments"
            raise CompileError(message, line=arguments[0].line if arguments else None)
        name_argument = arguments[0]
        flags_argument = arguments[1]
        self.emit_si_from_argument(name_argument)
        if (flags_expr := self._constant_expression(flags_argument)) is not None:
            for name in self._collect_constant_references(flags_argument):
                self.emit_constant_reference(name)
            self.emit(f"        mov al, {flags_expr}")
        else:
            self.generate_expression(flags_argument)
        if len(arguments) == 3:
            self.emit_register_from_argument(argument=arguments[2], register="dl")
        self._emit_syscall("IO_OPEN")
        self.ax_clear()

    def builtin_parse_ip(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the parse_ip(string, buffer) builtin.

        Parses a dotted-decimal IP string into a 4-byte buffer.
        Returns 0 on success, 1 on parse error.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="parse_ip")
        self.emit_si_from_argument(arguments[0])
        self.emit_register_from_argument(argument=arguments[1], register="di")
        self.emit("        call parse_ip")
        self.required_includes.add("parse_ip.asm")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=False)

    def builtin_print_datetime(self, arguments: list[Node], /) -> None:
        """Generate code for the print_datetime(unsigned long) builtin.

        Prints the epoch value as ``YYYY-MM-DD HH:MM:SS`` (no newline).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="print_datetime")
        self.generate_long_expression(arguments[0])
        self.emit("        call FUNCTION_PRINT_DATETIME")

    def builtin_print_ip(self, arguments: list[Node], /) -> None:
        """Generate code for the print_ip(buffer) builtin.

        Prints a 4-byte IP address as ``A.B.C.D`` (no newline).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="print_ip")
        self.emit_si_from_argument(arguments[0])
        self.emit("        call FUNCTION_PRINT_IP")

    def builtin_print_mac(self, arguments: list[Node], /) -> None:
        """Generate code for the print_mac(buffer) builtin.

        Prints a 6-byte MAC address as ``XX:XX:XX:XX:XX:XX`` (no newline).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="print_mac")
        self.emit_si_from_argument(arguments[0])
        self.emit("        call FUNCTION_PRINT_MAC")

    def builtin_printf(self, arguments: list[Node], /) -> None:
        """Generate code for the printf() builtin.

        First argument must be a string literal.  Remaining arguments
        are pushed right-to-left onto the stack, followed by the format
        string pointer.  Uses cdecl calling convention (caller cleans).

        Optimization: when the format string contains no ``%`` at all
        (no format specifiers, no ``%%`` escapes), emits a direct
        ``call FUNCTION_PRINT_STRING`` instead of the full printf
        machinery.
        """
        if not arguments or not isinstance(arguments[0], String):
            message = "printf() requires a string literal as the first argument"
            raise CompileError(message, line=arguments[0].line if arguments else None)
        fmt = arguments[0].content
        # Fast path: no '%' at all → emit print_string directly.
        if "%" not in fmt and len(arguments) == 1:
            label = self.new_string_label(fmt)
            self.emit(f"        mov di, {label}")
            self.emit("        call FUNCTION_PRINT_STRING")
            return
        # Count format specifiers (excluding %%) to validate argument count.
        expected_args = 0
        i = 0
        while i < len(fmt):
            if fmt[i] == "%" and i + 1 < len(fmt):
                if fmt[i + 1] != "%":
                    expected_args += 1
                i += 2
            else:
                i += 1
        if len(arguments) - 1 != expected_args:
            message = f"printf() format expects {expected_args} argument{'s' if expected_args != 1 else ''}, got {len(arguments) - 1}"
            raise CompileError(message, line=arguments[0].line)
        # Push arguments right-to-left.
        for arg in reversed(arguments[1:]):
            self.generate_expression(arg)
            self.emit("        push ax")
        # Push format string pointer.
        label = self.new_string_label(fmt)
        self.emit(f"        push {label}")
        self.emit("        call FUNCTION_PRINTF")
        stack_size = len(arguments) * 2
        self.emit(f"        add sp, {stack_size}")

    def builtin_putchar(self, arguments: list[Node], /) -> None:
        """Generate code for the putchar() builtin."""
        self._check_argument_count(arguments=arguments, expected=1, name="putchar")
        argument = arguments[0]
        if isinstance(argument, String):
            byte_val = decode_first_character(argument.content)
            self.emit(f"        mov al, {byte_val}")
        elif isinstance(argument, Int):
            self.emit(f"        mov al, {argument.value}")
        else:
            self.generate_expression(argument)
        self.emit("        call FUNCTION_PRINT_CHARACTER")

    def builtin_read(self, arguments: list[Node], /) -> None:
        """Generate code for the read() builtin.

        ``read(fd, buffer, count)`` emits ``mov bx, <fd> /
        mov di, <buffer> / mov cx, <count> / mov ah, SYS_IO_READ /
        int 30h``.  Returns bytes read in AX (0 = EOF, -1 = error).
        """
        self._check_argument_count(arguments=arguments, expected=3, name="read")
        fd_argument, buffer_argument, count_argument = arguments
        self.emit_register_from_argument(argument=fd_argument, register="bx")
        self.emit_register_from_argument(argument=buffer_argument, register="di")
        self.emit_register_from_argument(argument=count_argument, register="cx")
        self._emit_syscall("IO_READ")
        self.ax_clear()

    def builtin_reboot(self, arguments: list[Node], /) -> None:
        """Generate code for the reboot() builtin.

        Emits ``mov ah, SYS_REBOOT / int 30h``.  Does not return on
        success; the kernel triggers a warm reboot via the keyboard
        controller.
        """
        self._check_argument_count(arguments=arguments, expected=0, name="reboot")
        self._emit_syscall("REBOOT")

    def builtin_recvfrom(self, arguments: list[Node], /) -> None:
        """Generate code for the recvfrom() builtin.

        ``recvfrom(fd, buf, len, port)`` emits ``mov bx, <fd> /
        mov di, <buf> / mov cx, <len> / mov dx, <port> /
        mov ah, SYS_NET_RECVFROM / int 30h``.
        Returns bytes received in AX (0 if no matching packet).
        """
        self._check_argument_count(arguments=arguments, expected=4, name="recvfrom")
        fd_argument, buffer_argument, len_argument, port_argument = arguments
        self.emit_register_from_argument(argument=fd_argument, register="bx")
        self.emit_register_from_argument(argument=buffer_argument, register="di")
        self.emit_register_from_argument(argument=len_argument, register="cx")
        self.emit_register_from_argument(argument=port_argument, register="dx")
        self._emit_syscall("NET_RECVFROM")
        self.ax_clear()

    def builtin_rename(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the rename() builtin.

        ``rename(oldname, newname)`` emits ``mov si, <oldname> /
        mov di, <newname> / mov ah, SYS_FS_RENAME / int 30h``.
        Returns 0 on success or an ERROR_* code on failure.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="rename")
        self.emit_si_from_argument(arguments[0])
        self.emit_register_from_argument(argument=arguments[1], register="di")
        self._emit_syscall("FS_RENAME")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=True)

    def builtin_sendto(self, arguments: list[Node], /) -> None:
        """Generate code for the sendto() builtin.

        ``sendto(fd, buf, len, ip_ptr, src_port, dst_port)`` emits
        register setup and ``mov ah, SYS_NET_SENDTO / int 30h``.
        The 6th argument (dst_port) goes in BP (saved/restored).
        Returns bytes sent in AX, or -1 on error.
        """
        self._check_argument_count(arguments=arguments, expected=6, name="sendto")
        fd_argument, buf_argument, len_argument, ip_argument, sport_argument, dport_argument = arguments
        self.emit_register_from_argument(argument=fd_argument, register="bx")
        self.emit_si_from_argument(buf_argument)
        self.emit_register_from_argument(argument=len_argument, register="cx")
        self.emit_register_from_argument(argument=ip_argument, register="di")
        self.emit_register_from_argument(argument=sport_argument, register="dx")
        self.emit(f"        push {self.target.bp_register}")
        if isinstance(dport_argument, Int):
            self.emit(f"        mov {self.target.bp_register}, {dport_argument.value}")
        elif isinstance(dport_argument, Var) and dport_argument.name in self.NAMED_CONSTANTS:
            self.emit(f"        mov {self.target.bp_register}, {dport_argument.name}")
        elif isinstance(dport_argument, Var) and dport_argument.name in self.pinned_register:
            self.emit(f"        mov {self.target.bp_register}, {self.pinned_register[dport_argument.name]}")
        elif (
            isinstance(dport_argument, Var)
            and self._is_memory_scalar(dport_argument.name)
            and not self._is_byte_scalar(dport_argument.name)
        ):
            self.emit(f"        mov {self.target.bp_register}, [{self._local_address(dport_argument.name)}]")
        else:
            self.generate_expression(dport_argument)
            self.emit(f"        mov {self.target.bp_register}, {self.target.acc}")
        self._emit_syscall("NET_SENDTO")
        self.emit(f"        pop {self.target.bp_register}")
        # Normalize the CF error signal into AX = -1 so callers can
        # check the return value with ``< 0``.
        label_index = self.new_label()
        self.emit(f"        jnc .ok_{label_index}")
        self.emit("        mov ax, -1")
        self.emit(f".ok_{label_index}:")
        self.ax_clear()

    def builtin_set_exec_arg(self, arguments: list[Node], /) -> None:
        """Generate code for the set_exec_arg(arg) builtin.

        Writes the 16-bit pointer *arg* to ``[EXEC_ARG]`` so that
        ``FUNCTION_PARSE_ARGV`` in the next exec()'d program can find
        it.  Pass NULL (0) to clear.  Used by the shell to forward
        command arguments into child programs.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="set_exec_arg")
        self.generate_expression(arguments[0])
        self.emit("        mov [EXEC_ARG], ax")

    def builtin_shutdown(self, arguments: list[Node], /) -> None:
        """Generate code for the shutdown() builtin.

        Emits ``mov ah, SYS_SHUTDOWN / int 30h``.  Does not return on
        success.  On APM failure the syscall returns, letting the caller
        print a diagnostic and continue.
        """
        self._check_argument_count(arguments=arguments, expected=0, name="shutdown")
        self._emit_syscall("SHUTDOWN")

    def builtin_sleep(self, arguments: list[Node], /) -> None:
        """Generate code for the sleep(milliseconds) builtin.

        ``sleep(ms)`` emits ``mov cx, <ms> / mov ah, SYS_RTC_SLEEP /
        int 30h``.  Busy-waits for the requested duration.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="sleep")
        self.emit_register_from_argument(argument=arguments[0], register="cx")
        self._emit_syscall("RTC_SLEEP")

    def builtin_strlen(self, arguments: list[Node], /) -> None:
        """Generate code for the strlen() builtin.

        ``strlen(ptr)`` scans for a null terminator and returns the
        string length in AX.  Uses ``repne scasb`` (clobbers CX, DI).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="strlen")
        self.emit_register_from_argument(argument=arguments[0], register="di")
        self.emit("        xor al, al")
        self.emit(f"        mov {self.target.count_register}, 0FFFFh")
        self.emit("        cld")
        self.emit("        repne scasb")
        self.emit(f"        mov {self.target.acc}, 0FFFEh")
        self.emit(f"        sub {self.target.acc}, {self.target.count_register}")
        self.ax_clear()

    def builtin_ticks(self, arguments: list[Node], /) -> None:
        """Generate code for the ticks() builtin.

        Returns the low 16 bits of the BIOS timer tick counter
        (~18.2 Hz).  Suitable for measuring short elapsed intervals —
        subtract two readings to get a tick count in [0, 65535].  The
        counter wraps roughly once an hour.
        """
        self._check_argument_count(arguments=arguments, expected=0, name="ticks")
        self.emit("        xor ah, ah")
        self.emit("        int 1Ah")
        self.emit(f"        mov {self.target.acc}, {self.target.dx_register}")
        self.ax_clear()

    def builtin_uptime(self, arguments: list[Node], /) -> None:
        """Generate code for the uptime() builtin."""
        self._check_argument_count(arguments=arguments, expected=0, name="uptime")
        self._emit_syscall("RTC_UPTIME")

    def builtin_video_mode(self, arguments: list[Node], /) -> None:
        """Generate code for the video_mode(mode) builtin.

        Invokes SYS_VIDEO_MODE to switch video mode; also clears the
        screen and serial terminal.  AL = mode.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="video_mode")
        self.emit_register_from_argument(argument=arguments[0], register=self.target.acc)
        self._emit_syscall("VIDEO_MODE")
        self.ax_clear()

    def builtin_write(self, arguments: list[Node], /) -> None:
        """Generate code for the write() builtin.

        ``write(fd, buffer, count)`` emits ``mov bx, <fd> /
        mov si, <buffer> / mov cx, <count> / mov ah, SYS_IO_WRITE /
        int 30h``.  Returns bytes written in AX (-1 on error).
        """
        self._check_argument_count(arguments=arguments, expected=3, name="write")
        fd_argument, buffer_argument, count_argument = arguments
        self.emit_register_from_argument(argument=buffer_argument, register="si")
        self.emit_register_from_argument(argument=count_argument, register="cx")
        self.emit_register_from_argument(argument=fd_argument, register="bx")
        self._emit_syscall("IO_WRITE")
        self.ax_clear()

    def can_auto_pin(self, *, following_statement: Node | None, statement: VarDecl) -> bool:
        """Decide whether *statement* should be auto-pinned to a register."""
        if len(self.pinned_register) >= len(self.safe_pin_registers):
            return False
        init = statement.init
        if init is None:
            return True
        # Call initializers stay in memory so they can participate in
        # error-return fusion without clobbering a pin.
        return not isinstance(init, Call)

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
        pool = (*self.target.register_pool, "bp") if self.elide_frame else self.target.register_pool
        clobber_counts: dict[str, int] = dict.fromkeys(pool, 0)

        def visit(node: Node) -> None:
            if isinstance(node, Call):
                if node.name in self.user_functions:
                    # User functions follow the standard cdecl prologue
                    # (``push bp / mov bp, sp / … / pop bp``) which
                    # preserves the caller's BP, so BP is omitted from
                    # the user-call clobber set even when it's pinned.
                    for register in self.target.register_pool:
                        clobber_counts[register] += 1
                else:
                    if node.name not in self.BUILTIN_CLOBBERS:
                        message = f"unknown function: {node.name}"
                        raise CompileError(message, line=node.line)
                    for register in self.BUILTIN_CLOBBERS[node.name]:
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
            if statement.type_name != "unsigned long" or statement.init is None:
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
        """Append a line of assembly to the output buffer."""
        self.lines.append(line)

    def emit_argument_vector_startup(self, parameters: list[Param], /, *, body: list[Node]) -> list[Node]:
        """Emit inline startup code that parses EXEC_ARG into argc/argv.

        Registers ``argv`` as a constant alias to ``ARGV`` so all
        subsequent accesses use the kernel constant directly, avoiding
        a memory local and store/reload traffic.

        When the first statement in *body* is ``if (argc != N) die(msg)``,
        the argc check is fused directly into the startup using
        ``cmp cx, N`` (before CX is clobbered), eliminating the
        ``_l_argc`` memory local entirely.  Returns the (possibly
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

        # argv is always the fixed ARGV address — register as constant alias.
        self.constant_aliases[argv_name] = "ARGV"

        self.emit("        cld")
        self.emit("        mov di, ARGV")
        self.emit("        call FUNCTION_PARSE_ARGV")

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
                self.emit(f"        cmp cx, {expected}")
                self.emit(f"        mov si, {die_label}")
                self.emit(f"        mov cx, {die_length}")
                self.emit("        jne FUNCTION_DIE")
                fused_argc = True
                body = body[1:]

        if argc_name and not fused_argc:
            self.emit(f"        mov [{self._local_address(argc_name)}], cx")
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
                source_register = self.target.loword(source_register)
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
                    cmp_acc = self.target.loword(self.target.acc) if len(source) < len(self.target.acc) else self.target.acc
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
                self.generate_expression(left)
                self.emit(f"        cmp {self.target.acc}, [{self._local_address(right.name)}]")
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

    def emit_condition(self, *, condition: Node, context: str) -> str:
        """Validate a condition, emit a comparison, and return the operator.

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
            return "carry" if condition.operation == "!=" else "not_carry"
        # Skip type validation for IR-generated conditions — the AST was
        # already validated by the parser before IR construction.
        if context != "ir":
            self.validate_comparison_types(condition.left, condition.right)
        self.emit_comparison(condition.left, condition.right)
        return condition.operation

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
        operator = self.emit_condition(condition=condition, context=context)
        self.emit(f"        {JUMP_WHEN_FALSE[operator]} {fail_label}")

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
        operator = self.emit_condition(condition=condition, context=context)
        self.emit(f"        {JUMP_WHEN_TRUE[operator]} {success_label}")

    def emit_constant_reference(self, name: str) -> None:
        """Record a reference to a NAMED_CONSTANT.

        If the constant requires an extra NASM %include to provide its
        symbol (see :attr:`NAMED_CONSTANT_INCLUDES`), queue the include
        for emission at output time.
        """
        include = self.NAMED_CONSTANT_INCLUDES.get(name)
        if include is not None:
            self.required_includes.add(include)

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
            self.emit(f"        mov si, {die_label}")
            self.emit(f"        mov cx, {die_length}")
            self.emit("        jc FUNCTION_DIE")
            return
        if fuse_exit:
            self.emit("        jnc FUNCTION_EXIT")
            return
        label_index = self.new_label()
        self.emit(f"        jnc .ok_{label_index}")
        if preserve_al:
            self.emit("        xor ah, ah")
        else:
            self.emit("        mov ax, 1")
        self.emit(f"        jmp .done_{label_index}")
        self.emit(f".ok_{label_index}:")
        self.emit("        xor ax, ax")
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
        # named var in AX (memory scalar) override this below.
        new_ax_local: str | None = self.ax_local
        new_ax_is_byte: bool = self.ax_is_byte
        if isinstance(argument, Int):
            self.emit(f"        mov {register}, {argument.value}")
            if ax_written:
                new_ax_local = None
                new_ax_is_byte = False
        elif isinstance(argument, Var) and argument.name in self.NAMED_CONSTANTS:
            self.emit_constant_reference(argument.name)
            self.emit(f"        mov {register}, {argument.name}")
            if ax_written:
                new_ax_local = None
                new_ax_is_byte = False
        elif isinstance(argument, Var) and argument.name in self.constant_aliases:
            self.emit(f"        mov {register}, {self.constant_aliases[argument.name]}")
            if ax_written:
                new_ax_local = None
                new_ax_is_byte = False
        elif isinstance(argument, Var) and argument.name in self.pinned_register:
            source = self.pinned_register[argument.name]
            if len(register) < len(source):
                # Loading a 32-bit pinned reg into a narrower (16-bit) target:
                # use the low-word name.
                source = self.target.loword(source)
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
                source = self.target.loword(source)
            if source != register:
                self.emit(f"        mov {register}, {source}")
            if ax_written and source != self.target.acc:
                new_ax_local = argument.name
                new_ax_is_byte = False
        elif isinstance(argument, Var) and argument.name == self.ax_local:
            if register != self.target.acc:
                source = self.target.loword(self.target.acc) if len(register) < len(self.target.acc) else self.target.acc
                self.emit(f"        mov {register}, {source}")
            # AX unchanged in both branches: shortcut leaves tracking intact.
        elif isinstance(argument, Var) and argument.name in self.global_arrays:
            self.emit(f"        mov {register}, _g_{argument.name}")
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
                self.emit(f"        mov al, [{self._local_address(argument.name)}]")
                self.emit("        xor ah, ah")
                if register != self.target.acc:
                    source = self.target.loword(self.target.acc) if len(register) < len(self.target.acc) else self.target.acc
                    self.emit(f"        mov {register}, {source}")
                new_ax_local = argument.name
                new_ax_is_byte = True
            else:
                self.emit(f"        mov {register}, [{self._local_address(argument.name)}]")
                if ax_written:
                    new_ax_local = argument.name
                    new_ax_is_byte = False
        elif isinstance(argument, String):
            self.emit(f"        mov {register}, {self.new_string_label(argument.content)}")
            if ax_written:
                new_ax_local = None
                new_ax_is_byte = False
        elif (constant_expr := self._constant_expression(argument)) is not None:
            for name in self._collect_constant_references(argument):
                self.emit_constant_reference(name)
            self.emit(f"        mov {register}, {constant_expr}")
            if ax_written:
                new_ax_local = None
                new_ax_is_byte = False
        else:
            self.generate_expression(argument)
            if register != self.target.acc:
                # In 32-bit mode, the result is in eax; narrow-register targets
                # (bx, cx, dx, si, di) need the 16-bit low word of eax.
                source = self.target.loword(self.target.acc) if len(register) < len(self.target.acc) else self.target.acc
                self.emit(f"        mov {register}, {source}")
            # generate_expression leaves its own tracking; do not
            # override new_ax_local here.
            new_ax_local = self.ax_local
            new_ax_is_byte = self.ax_is_byte
        self.ax_local = new_ax_local
        self.ax_is_byte = new_ax_is_byte

    def emit_si_from_argument(self, argument: Node, /) -> None:
        """Load a string or expression argument into SI."""
        if isinstance(argument, String):
            self.emit(f"        mov si, {self.new_string_label(argument.content)}")
        elif isinstance(argument, Var) and argument.name in self.constant_aliases:
            self.emit(f"        mov si, {self.constant_aliases[argument.name]}")
        elif isinstance(argument, Var) and argument.name in self.global_arrays:
            self.emit(f"        mov si, _g_{argument.name}")
        elif (constant_expr := self._constant_expression(argument)) is not None:
            for name in self._collect_constant_references(argument):
                self.emit_constant_reference(name)
            self.emit(f"        mov si, {constant_expr}")
        else:
            self.generate_expression(argument)
            self.emit("        mov si, ax")

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
        if self.variable_types.get(name) == "unsigned long":
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
                self.emit(f"        mov [{self.target.bp_register}-{low_offset}], {self.target.acc}")
                if isinstance(self.target, X86CodegenTarget16):
                    self.emit(f"        mov [{self.target.bp_register}-{low_offset - 2}], {self.target.dx_register}")
            self.ax_is_byte = False
            self.ax_local = None
            return
        direct_register: str | None = None
        if name in self.pinned_register:
            direct_register = self.pinned_register[name]
        elif name in self.register_aliased_globals:
            direct_register = self.register_aliased_globals[name]
        if direct_register is not None:
            if isinstance(expression, Int):
                if expression.value == 0:
                    self.emit(f"        xor {direct_register}, {direct_register}")
                else:
                    self.emit(f"        mov {direct_register}, {expression.value}")
                return
            if isinstance(expression, String):
                label = self.new_string_label(expression.content)
                self.emit(f"        mov {direct_register}, {label}")
                return
            if isinstance(expression, Var) and expression.name in self.NAMED_CONSTANTS:
                self.emit(f"        mov {direct_register}, {expression.name}")
                return
            if isinstance(expression, Var) and expression.name in self.global_arrays:
                self.emit(f"        mov {direct_register}, _g_{expression.name}")
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
                source = self.target.loword(self.target.acc) if len(direct_register) < len(self.target.acc) else self.target.acc
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

    def fuse_trailing_printf(self, body: list[Node], /) -> list[Node]:
        """Transform trailing simple printf() calls into die() for main.

        Handles both a direct trailing ``printf(msg)`` and ``printf(msg)``
        at the end of branches in a trailing if-else chain.
        """
        if not body:
            return body
        last = body[-1]
        if self._is_simple_printf(last):
            return [*body[:-1], Call(args=last.args, name="die")]
        if isinstance(last, If):
            transformed = self._transform_if_printf(last)
            if transformed is not last:
                return [*body[:-1], transformed]
        return body

    def generate(self, ast: Node, /) -> str:
        """Generate assembly for an entire program AST.

        Returns:
            The complete assembly source as a string.

        """
        for line in self.target.preamble_lines():
            self.emit(line)
        self.emit("        org 0600h")
        self.emit()
        self.emit('%include "constants.asm"')
        if self.defines:
            self.emit()
            for name in sorted(self.defines):
                self.emit(f"%define {name} {self.defines[name]}")
        self.emit()
        for function in ast.functions:
            if function.name != "main":
                self.user_functions[function.name] = len(function.params)
                if function.regparm_count > 0:
                    self.fastcall_functions.add(function.name)
                if function.carry_return:
                    self.carry_return_functions.add(function.name)
                if function.always_inline:
                    self._register_inline_body(function)
        self._register_globals(ast.globals)
        self._analyze_user_function_conventions(ast.functions)

        # Build IR for all non-main, non-always-inline functions.  The IR
        # is consumed by generate_function; main keeps the AST path because
        # its special handling (argc/argv startup, printf fusion, frame-
        # elide data labels) is deeply tied to the AST shape.
        ir_program = ir.Builder(carry_return_functions=frozenset(self.carry_return_functions)).build_program(ast)
        ir_by_name = {f.ast_node.name: f for f in ir_program.functions if not f.ast_node.always_inline}

        # Emit main first so execution starts at PROGRAM_BASE.
        main_func = None
        helpers: list[Node] = []
        for function in ast.functions:
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
        self.peephole()
        for include in sorted(self.required_includes):
            self.emit(f'%include "{include}"')
        # File-scope ``asm("...")`` blocks are emitted BEFORE globals /
        # strings / array data.  When the block holds code (for example
        # the assembler in src/c/asm.c), this keeps the mutable global-
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
        self._emit_global_storage()
        if self.strings:
            self.emit(";; --- string literals ---")
            for label, content in self.strings:
                self.emit(f"{label}: db `{content}\\0`")
        if self.arrays:
            code = "\n".join(self.lines)
            live = [(label, elements) for label, elements in self.arrays if label in code]
            if live:
                self.emit(";; --- array data ---")
                for label, elements in live:
                    self.emit(f"{label}: dw {', '.join(elements)}")
        # Sentinel label at the very end so inline asm can address the
        # first byte past the loaded image (scratch buffers, heap bases,
        # etc.).  Zero bytes, so it does not affect programs that ignore
        # it.
        self.emit("_program_end:")
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
        - ``if (cond) { die(msg); }`` → pre-load SI and emit a direct
          conditional jump to ``FUNCTION_DIE``, skipping the if-body dance
        """
        saved = self.visible_vars.copy() if scoped else None
        i = 0
        while i < len(statements):
            statement = statements[i]
            # Fuse simple printf() + exit() into die().
            next_is_exit = i + 1 < len(statements) and (
                statements[i + 1] == Call(args=[], name="exit") or isinstance(statements[i + 1], Return)
            )
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
            # Fuse `if (cond) { die(msg); }` into pre-load SI+CX + jCC .die.
            # AX tracking is preserved because the die path doesn't fall
            # through — the continuation path sees AX unchanged from before.
            if isinstance(statement, If) and statement.else_body is None and len(statement.body) == 1:
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
                    self.emit(f"        mov si, {die_label}")
                    self.emit(f"        mov cx, {die_length}")
                    operator = self.emit_condition(condition=statement.cond, context="if")
                    true_jump = JUMP_INVERT[JUMP_WHEN_FALSE[operator]]
                    self.emit(f"        {true_jump} FUNCTION_DIE")
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
        for index in range(len(call.args)):
            if is_fastcall and index == 0:
                continue
            if index in callee_pins:
                continue
            return False  # stack arg — can't clean up after a jmp
        return True

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
        if name in self.user_functions:
            expected = self.user_functions[name]
            if len(arguments) != expected:
                message = f"{name}() expects exactly {expected} argument{'s' if expected != 1 else ''}"
                raise CompileError(message, line=statement.line)
            clobbers: frozenset[str] = frozenset(self.target.register_pool)
            saved = self._pinned_registers_to_save(clobbers)
            use_pusha = discard_return and len(saved) >= 3
            if not tail_call:
                if use_pusha:
                    self.emit("        pusha")
                else:
                    for register in saved:
                        self.emit(f"        push {register}")
            callee_pins = self.user_function_pin_params.get(name, {}) if name in self.register_convention_functions else {}
            is_fastcall = name in self.fastcall_functions
            fastcall_ax_arg: Node | None = None
            register_args: list[tuple[str, Node]] = []
            stack_args: list[Node] = []
            for index, arg in enumerate(arguments):
                if is_fastcall and index == 0:
                    fastcall_ax_arg = arg
                elif index in callee_pins:
                    register_args.append((callee_pins[index], arg))
                else:
                    stack_args.append(arg)
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
                self.emit(f"        jmp {name}")
                self.ax_clear()
                return
            if name in self.inline_bodies:
                self._emit_inline_body(name)
            else:
                self.emit(f"        call {name}")
            if stack_args:
                self.emit(f"        add sp, {len(stack_args) * 2}")
            if use_pusha:
                self.emit("        popa")
            else:
                for register in reversed(saved):
                    self.emit(f"        pop {register}")
            self.ax_clear()
            return
        handler = getattr(self, f"builtin_{name}", None)
        if handler is None:
            message = f"unknown function: {name}"
            raise CompileError(message, line=statement.line)
        clobbers = self.BUILTIN_CLOBBERS[name]
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
        if isinstance(expression, Int):
            self.ax_clear()
            if expression.value == 0:
                self.emit(f"        xor {self.target.acc}, {self.target.acc}")
            else:
                self.emit(f"        mov {self.target.acc}, {expression.value}")
        elif isinstance(expression, String):
            self.ax_clear()
            self.emit(f"        mov {self.target.acc}, {self.new_string_label(expression.content)}")
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
            if vname in self.global_arrays:
                # A global array name decays to its base address — the
                # ``_g_<name>`` label.  Load it as an immediate, not as a
                # memory fetch from that address.
                self.emit(f"        mov {self.target.acc}, _g_{vname}")
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
                self.emit(f"        mov al, [{self._local_address(vname)}]")
                self.emit("        xor ah, ah")
                self.ax_is_byte = True
            else:
                self.emit(f"        mov {self.target.acc}, [{self._local_address(vname)}]")
                self.ax_is_byte = False
            self.ax_local = vname
        elif isinstance(expression, Index):
            self.ax_clear()
            vname = expression.name
            index_expression = expression.index
            self._check_defined(vname, line=expression.line)
            if isinstance(index_expression, Int) and vname in self.array_labels:
                offset = index_expression.value * self.target.int_size
                label = self.array_labels[vname]
                if offset:
                    self.emit(f"        mov {self.target.acc}, [{label}+{offset}]")
                else:
                    self.emit(f"        mov {self.target.acc}, [{label}]")
            elif isinstance(index_expression, Int):
                is_byte = self._is_byte_var(vname)
                offset = index_expression.value * (1 if is_byte else self.target.int_size)
                # Direct memory access for constant/aliased bases:
                # emit `mov ax, [CONST+N]` instead of `mov bx, CONST / mov ax, [bx+N]`.
                const_base = self._resolve_constant(vname)
                if const_base is not None:
                    addr = f"{const_base}+{offset}" if offset else const_base
                    if is_byte:
                        self.emit(f"        mov al, [{addr}]")
                        self.emit("        xor ah, ah")
                    else:
                        self.emit(f"        mov {self.target.acc}, [{addr}]")
                else:
                    guarded = self._si_scratch_guard_begin(vname)
                    self._emit_load_var(vname, register="si")
                    if is_byte:
                        if offset:
                            self.emit(f"        mov al, [si+{offset}]")
                        else:
                            self.emit("        mov al, [si]")
                        self.emit("        xor ah, ah")
                    elif offset:
                        self.emit(f"        mov {self.target.acc}, [si+{offset}]")
                    else:
                        self.emit(f"        mov {self.target.acc}, [si]")
                    self._si_scratch_guard_end(guarded=guarded)
            else:
                is_byte = self._is_byte_var(vname)
                const_base = self._resolve_constant(vname)
                if const_base is not None:
                    self.emit_constant_reference(vname)
                    addr = self._emit_constant_base_index_addr(
                        const_base=const_base,
                        index=index_expression,
                        is_byte=is_byte,
                        preserve_ax=False,
                    )
                    if is_byte:
                        self.emit(f"        mov al, [{addr}]")
                        self.emit("        xor ah, ah")
                    else:
                        self.emit(f"        mov {self.target.acc}, [{addr}]")
                    self.ax_clear()
                else:
                    guarded = self._si_scratch_guard_begin(vname)
                    self._emit_load_var(vname, register="si")
                    # If the index is a pinned variable and the access is
                    # byte-sized, load it without clobbering SI.
                    if is_byte and isinstance(index_expression, Var) and index_expression.name in self.pinned_register:
                        ireg = self.pinned_register[index_expression.name]
                        self.emit(f"        add si, {self.target.loword(ireg)}")
                    elif isinstance(index_expression, (Var, Int)):
                        # Simple Var/Int load doesn't touch SI, so skip the
                        # push/pop round-trip.
                        self.generate_expression(index_expression)
                        if not is_byte:
                            self.emit(f"        add {self.target.acc}, {self.target.acc}")
                        self.emit(f"        add si, {self.target.loword(self.target.acc)}")
                    else:
                        self.emit("        push si")
                        self.generate_expression(index_expression)
                        if not is_byte:
                            self.emit(f"        add {self.target.acc}, {self.target.acc}")
                        self.emit("        pop si")
                        self.emit(f"        add si, {self.target.loword(self.target.acc)}")
                    if is_byte:
                        self.emit("        mov al, [si]")
                        self.emit("        xor ah, ah")
                    else:
                        self.emit(f"        mov {self.target.acc}, [si]")
                    self._si_scratch_guard_end(guarded=guarded)
                    # AX now holds the subscript result, not the index —
                    # invalidate the tracking that generate_expression set.
                    self.ax_clear()
        elif isinstance(expression, SizeofType):
            self.ax_clear()
            self.emit(f"        mov {self.target.acc}, {self.target.type_sizes[expression.type_name]}")
        elif isinstance(expression, SizeofVar):
            self.ax_clear()
            vname = expression.name
            if vname in self.global_arrays:
                declaration = self.global_arrays[vname]
                stride = 1 if declaration.type_name in self.BYTE_TYPES else 2
                if declaration.init is not None:
                    size = len(declaration.init.elements) * stride
                    self.emit(f"        mov {self.target.acc}, {size}")
                else:
                    size_expression = self._constant_expression(declaration.size)
                    self.emit(f"        mov {self.target.acc}, ({size_expression})*{stride}")
            elif vname in self.array_sizes:
                size = self.array_sizes[vname] * self.target.int_size  # word-sized elements
                self.emit(f"        mov {self.target.acc}, {size}")
            else:
                size = self.target.int_size  # all non-array variables are word-sized
                self.emit(f"        mov {self.target.acc}, {size}")
        elif isinstance(expression, Call):
            self.generate_call(expression)
        elif isinstance(expression, BinaryOperation):
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
                    self.emit(f"        mov al, [{self._local_address(left.name)}+1]")
                    self.emit("        xor ah, ah")
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
                self.emit(f"        {JUMP_WHEN_FALSE[operator]} {skip_label}")
                self.emit(f"        inc {self.target.acc}")
                self.emit(f"{skip_label}:")
            else:
                message = f"unknown operator: {operator}"
                raise CompileError(message, line=expression.line)
            if protect_count:
                self.emit(f"        pop {self.target.count_register}")
            self.ax_clear()
        else:
            message = f"unknown expression: {type(expression).__name__}"
            raise CompileError(message, line=expression.line)

    # ------------------------------------------------------------------
    # IR lowering
    # ------------------------------------------------------------------

    def _ir_value_to_ast(self, value: ir.Value) -> Node:
        """Convert an :data:`ir.Value` to the equivalent simple AST leaf node."""
        if isinstance(value, int):
            return Int(value=value)
        if value.startswith("_ir_s"):
            content = self._ir_string_map.get(value)
            if content is not None:
                return String(content=content)
        return Var(name=value)

    @staticmethod
    def _collect_ir_temps(body: list[ir.Instruction]) -> list[str]:
        """Return IR-generated temp names (``_ir_*``) that appear as destinations."""
        seen: set[str] = set()
        result: list[str] = []
        for instruction in body:
            destination: str | None = None
            match instruction:
                case ir.BinaryOperation(destination=name) | ir.Copy(destination=name) | ir.Index(destination=name):
                    destination = name
                case ir.Call(destination=name):
                    destination = name
                case ir.Block(node=Assign(name=name)):
                    destination = name
                case _:
                    pass
            if destination is not None and destination.startswith("_ir_") and destination not in seen:
                seen.add(destination)
                result.append(destination)
        return result

    def lower_ir_body(self, body: list[ir.Instruction]) -> None:
        """Generate x86 assembly from a flat IR instruction list."""
        for instruction in body:
            self._lower_ir_instruction(instruction)

    def _lower_ir_instruction(self, instruction: ir.Instruction) -> None:
        match instruction:
            case ir.BinaryOperation(destination=destination, operation=operation, left=left, right=right):
                expression = BinaryOperation(left=self._ir_value_to_ast(left), operation=operation, right=self._ir_value_to_ast(right))
                self.emit_store_local(expression=expression, name=destination)
            case ir.Copy(destination=destination, source=source):
                self.emit_store_local(expression=self._ir_value_to_ast(source), name=destination)
            case ir.Call(destination=None, name=name, args=args):
                call = Call(args=[self._ir_value_to_ast(a) for a in args], name=name)
                self.generate_call(call, discard_return=True)
                self.ax_clear()
            case ir.Call(destination=destination, name=name, args=args):
                call = Call(args=[self._ir_value_to_ast(a) for a in args], name=name)
                self.emit_store_local(expression=call, name=destination)
            case ir.Index(destination=destination, base=base, index=index):
                expression = Index(index=self._ir_value_to_ast(index), name=base)
                self.emit_store_local(expression=expression, name=destination)
            case ir.IndexAssign(base=base, index=index, source=source):
                stmt = IndexAssign(expr=self._ir_value_to_ast(source), index=self._ir_value_to_ast(index), name=base)
                self.generate_index_assign(stmt)
            case ir.Label(name=name):
                # Control can arrive at an IR label from any preceding
                # branch / jump, so AX-tracking state (``ax_local`` /
                # ``ax_is_byte``) accumulated on the fall-through path
                # is not guaranteed on the jump path.  Clear the
                # tracking so downstream ``emit_comparison`` / similar
                # do a real load instead of reusing a stale AX.
                self.ax_clear()
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
                self.generate_call(call_ast, discard_return=True)
                self.emit(f"        {'jc' if when == 'set' else 'jnc'} {target}")
                self.ax_clear()
            case ir.Return(value=value):
                stmt = Return(value=self._ir_value_to_ast(value) if value is not None else None)
                self.generate_return(stmt)
            case ir.InlineAsm(content=content):
                for line in decode_string_escapes(content).splitlines():
                    self.emit(line)
            case ir.Block(node=node):
                self.generate_statement(node)

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
        self.ax_clear()
        self.constant_aliases = {}
        self.current_carry_return = function.carry_return
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
        if naked_asm or frameless_calls:
            self.elide_frame = True
        self.byte_scalar_locals = set()
        self.frame_size = 0
        self.live_long_local = None
        self.locals = {}
        self.pinned_register = {}
        self.variable_arrays = set()
        self.variable_types = {}
        self.virtual_long_locals = set()
        self.zero_init_skippable: set[str] = set()

        # Globals are visible in every function.  Scalars get a
        # ``_g_<name>`` memory slot; arrays are resolved via the
        # ``_resolve_constant`` path (they behave like a fixed base
        # address, word-strided for ``int`` and byte-strided for
        # ``char``).
        for global_name, declaration in self.global_scalars.items():
            self.variable_types[global_name] = declaration.type_name
            self.visible_vars.add(global_name)
        for global_name, declaration in self.global_arrays.items():
            self.variable_types[global_name] = declaration.type_name
            self.variable_arrays.add(global_name)
            self.visible_vars.add(global_name)

        # Fastcall (regparm(1)) routing.  Param 0 arrives in AX and is
        # spilled to a local stack slot during the prologue; params 1..N
        # use the standard caller-pushed cdecl layout shifted down by
        # one slot (caller didn't push arg 0).
        is_fastcall = name != "main" and function.regparm_count > 0
        # Allocate parameters and record their types.
        for param in parameters:
            if param.name in self.global_scalars or param.name in self.global_arrays:
                message = f"parameter '{param.name}' shadows a file-scope global"
                raise CompileError(message, line=function.line)
        if name == "main":
            # main parameters are handled by emit_argument_vector_startup.
            for param in parameters:
                self.allocate_local(param.name)
                self.variable_types[param.name] = param.type
                if param.is_array:
                    self.variable_arrays.add(param.name)
        else:
            # Non-main: record parameter types; stack offsets are kept
            # as fallbacks but parameters will be pinned to registers
            # when safe_pin_registers has room.
            for i, param in enumerate(parameters):
                self.variable_types[param.name] = param.type
                if param.is_array:
                    self.variable_arrays.add(param.name)
                if is_fastcall and i == 0:
                    # Param 0 gets a local slot allocated below; it has no
                    # caller-pushed address.
                    continue
                stack_index = i - 1 if is_fastcall else i
                self.locals[param.name] = -(self.target.param_slot_base + stack_index * self.target.int_size)  # negative = above bp

        self.discover_virtual_long_locals(body)
        self.safe_pin_registers = self.compute_safe_pin_registers(body)
        # Exclude fastcall param 0 from auto-pin candidates — it's spilled to
        # the stack at prologue entry and the body accesses it through that
        # slot like any other local.
        if name == "main":
            param_candidates = []
        elif is_fastcall:
            param_candidates = parameters[1:]
        else:
            param_candidates = parameters
        self.auto_pin_candidates = self._select_auto_pin_candidates(body=body, parameters=param_candidates)

        # Reserve a local stack slot for fastcall param 0 before scan_locals
        # runs so its offset is stable against body-local allocations.
        if is_fastcall:
            self.allocate_local(parameters[0].name)

        self.scan_locals(body)

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
                if is_fastcall and i == 0:
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

        self.emit(f"{name}:")
        if not self.elide_frame:
            self.emit(f"        push {self.target.bp_register}")
            self.emit(f"        mov {self.target.bp_register}, {self.target.sp_register}")
            if self.frame_size > 0:
                self.emit(f"        sub {self.target.sp_register}, {self.frame_size}")
            if is_fastcall:
                # Spill AX (the caller-supplied arg 0) into its local slot
                # so the body can read it through the normal local path.
                slot = self.locals[parameters[0].name]
                self.emit(f"        mov [{self.target.bp_register}-{slot}], {self.target.acc}")
            if not register_convention:
                # Load pinned parameters from caller-pushed stack slots
                # into their registers.
                for i, param in enumerate(parameters):
                    if is_fastcall and i == 0:
                        continue
                    if param.name in self.pinned_register:
                        register = self.pinned_register[param.name]
                        stack_index = i - 1 if is_fastcall else i
                        offset = self.target.param_slot_base + stack_index * self.target.int_size
                        self.emit(f"        mov {register}, [{self.target.bp_register}+{offset}]")

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
            self.lower_ir_body(ir_body)
        else:
            # Tail-call: if the last statement is a statement-level user-
            # function call that qualifies, emit everything before it as
            # usual and lower the trailing call as ``jmp`` (no ``ret``).
            tail_call_last = name != "main" and body and isinstance(body[-1], Call) and self._is_tail_call_eligible(body[-1])
            if tail_call_last:
                self.generate_body(body[:-1])
                self.generate_call(body[-1], tail_call=True)
            else:
                self.generate_body(body)

        if name == "main":
            self.emit("        jmp FUNCTION_EXIT")
            if self.elide_frame:
                for vname in sorted(self.locals):
                    if self.variable_types.get(vname) == "unsigned long":
                        directive = "dd 0"
                    elif vname in self.byte_scalar_locals:
                        directive = "db 0"
                    else:
                        directive = "dw 0"
                    self.emit(f"_l_{vname}: {directive}")
        elif ir_body is not None:
            # IR path: generate epilogue unless the body always exits.
            # Tail-call optimization is not yet applied on the IR path.
            if not self.elide_frame and not self._always_exits_ir(ir_body):
                if self.frame_size > 0:
                    self.emit(f"        mov {self.target.sp_register}, {self.target.bp_register}")
                self.emit(f"        pop {self.target.bp_register}")
                self.emit("        ret")
            elif self.elide_frame:
                self.emit("        ret")
        elif tail_call_last:
            # The tail ``jmp`` already transferred control; no ``ret`` needed.
            pass
        elif self.elide_frame:
            # naked_asm and frameless_calls both skip the prologue, so
            # the epilogue is just ``ret`` — no ``pop bp`` because we
            # didn't push it.
            self.emit("        ret")
        elif not self.always_exits(body):
            if self.frame_size > 0:
                self.emit(f"        mov {self.target.sp_register}, {self.target.bp_register}")
            self.emit(f"        pop {self.target.bp_register}")
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
                self.emit(f"        mov al, [{self._local_address(chain_var)}]")
                self.emit("        xor ah, ah")
                self.ax_is_byte = True
            else:
                self.emit(f"        mov ax, [{self._local_address(chain_var)}]")
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
        """Generate assembly for ``name[index] = expr;``.

        When the base pointer lives in memory (not a named constant) and
        a different ``asm_register("si")`` global is active, loading the
        base into SI would clobber that alias — the SI scratch guard
        wraps the store with ``push si`` / ``pop si`` to preserve the
        pinned value.  Matches the read-side guard in generate_expression.
        """
        self.ax_clear()
        name = statement.name
        is_byte = self._is_byte_var(name)
        self._check_defined(name, line=statement.line)
        # Evaluate value into AX, then store at base+index.
        if isinstance(statement.index, Int) and isinstance(statement.expr, Int):
            # Both index and value are constants: direct store.
            offset = statement.index.value * (1 if is_byte else self.target.int_size)
            const_base = self._resolve_constant(name)
            if const_base is not None:
                addr = f"{const_base}+{offset}" if offset else const_base
                guarded = False
            else:
                guarded = self._si_scratch_guard_begin(name)
                self._emit_load_var(name, register="si")
                addr = f"si+{offset}" if offset else "si"
            if is_byte:
                self.emit(f"        mov byte [{addr}], {statement.expr.value}")
            else:
                self.emit(f"        mov {self.target.word_size} [{addr}], {statement.expr.value}")
            self._si_scratch_guard_end(guarded=guarded)
        elif isinstance(statement.index, Int):
            # Constant index, variable value.
            offset = statement.index.value * (1 if is_byte else self.target.int_size)
            self.generate_expression(statement.expr)
            const_base = self._resolve_constant(name)
            if const_base is not None:
                addr = f"{const_base}+{offset}" if offset else const_base
                guarded = False
            else:
                guarded = self._si_scratch_guard_begin(name)
                self._emit_load_var(name, register="si")
                addr = f"si+{offset}" if offset else "si"
            if is_byte:
                self.emit(f"        mov [{addr}], al")
            else:
                self.emit(f"        mov [{addr}], ax")
            self._si_scratch_guard_end(guarded=guarded)
        else:
            const_base = self._resolve_constant(name)
            if const_base is not None:
                self.emit_constant_reference(name)
                self.generate_expression(statement.expr)
                addr = self._emit_constant_base_index_addr(
                    const_base=const_base,
                    index=statement.index,
                    is_byte=is_byte,
                    preserve_ax=True,
                )
                if is_byte:
                    self.emit(f"        mov [{addr}], al")
                else:
                    self.emit(f"        mov [{addr}], ax")
                self.ax_clear()
            else:
                # Variable index: compute address in SI, then store.
                # Guard goes OUTSIDE the push/pop ax pair so the pop
                # order matches the push order (push ax..., pop ax, pop si).
                guarded = self._si_scratch_guard_begin(name)
                self.generate_expression(statement.expr)
                self.emit(f"        push {self.target.acc}")
                self._emit_load_var(name, register="si")
                # If the index is a simple Var/Int, evaluating it doesn't
                # clobber SI, so we can skip the push/pop round-trip.
                if isinstance(statement.index, (Var, Int)):
                    self.generate_expression(statement.index)
                    if not is_byte:
                        self.emit(f"        add {self.target.acc}, {self.target.acc}")
                    self.emit(f"        add si, {self.target.loword(self.target.acc)}")
                else:
                    self.emit("        push si")
                    self.generate_expression(statement.index)
                    if not is_byte:
                        self.emit(f"        add {self.target.acc}, {self.target.acc}")
                    self.emit("        pop si")
                    self.emit(f"        add si, {self.target.loword(self.target.acc)}")
                self.emit(f"        pop {self.target.acc}")
                # After pop, AX holds the value being stored, not the index —
                # invalidate the ax_local tracking that generate_expression set.
                self.ax_clear()
                if is_byte:
                    self.emit("        mov [si], al")
                else:
                    self.emit(f"        mov [si], {self.target.acc}")
                self._si_scratch_guard_end(guarded=guarded)

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
        if isinstance(expression, Var):
            vname = expression.name
            if self.variable_types.get(vname) != "unsigned long":
                message = f"expected 'unsigned long' expression, got '{self.variable_types.get(vname, 'int')}' variable {vname!r}"
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
                self.emit(f"        mov {self.target.acc}, [{self.target.bp_register}-{low_offset}]")
                if isinstance(self.target, X86CodegenTarget16):
                    self.emit(f"        mov {self.target.dx_register}, [{self.target.bp_register}-{low_offset - 2}]")
            self.ax_is_byte = False
            self.ax_local = None
            return
        message = f"unsupported 'unsigned long' expression: {type(expression).__name__}"
        raise CompileError(message, line=expression.line)

    def generate_return(self, statement: Return, /) -> None:
        """Generate assembly for a return statement.

        In ``main``, ``return`` maps to ``jmp FUNCTION_EXIT``.  In other
        functions it evaluates the return expression into AX, tears down
        the stack frame, and emits ``ret``.  For ``carry_return``
        functions, ``return 1`` / ``return 0`` bypass AX entirely and
        set CF instead (``clc`` / ``stc``); any other return value is
        rejected at codegen time.
        """
        if self.elide_frame:
            # main: return [expr]; → exit() (discard return value)
            self.emit("        jmp FUNCTION_EXIT")
            return
        if self.current_carry_return:
            value = statement.value
            if isinstance(value, Int) and value.value in (0, 1):
                self.emit("        clc" if value.value == 1 else "        stc")
                if self.frame_size > 0:
                    self.emit(f"        mov {self.target.sp_register}, {self.target.bp_register}")
                self.emit(f"        pop {self.target.bp_register}")
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
                self.emit(f"        mov {self.target.sp_register}, {self.target.bp_register}")
            self.emit(f"        pop {self.target.bp_register}")
            self.emit("        ret")
            self.emit(f"{true_label}:")
            self.emit("        clc")
            if self.frame_size > 0:
                self.emit(f"        mov {self.target.sp_register}, {self.target.bp_register}")
            self.emit(f"        pop {self.target.bp_register}")
            self.emit("        ret")
            return
        if statement.value is not None:
            self.generate_expression(statement.value)
        if self.frame_size > 0:
            self.emit(f"        mov {self.target.sp_register}, {self.target.bp_register}")
        self.emit(f"        pop {self.target.bp_register}")
        self.emit("        ret")

    def generate_statement(self, statement: Node, /) -> None:
        """Generate assembly for a single statement.

        Raises:
            CompileError: If an unknown statement kind is encountered.

        """
        if isinstance(statement, VarDecl):
            self.visible_vars.add(statement.name)
            self.variable_types[statement.name] = statement.type_name
            if statement.name in self.constant_aliases:
                return
            if statement.init is not None:
                if statement.name in self.zero_init_skippable:
                    self.zero_init_skippable.discard(statement.name)
                else:
                    self.emit_store_local(expression=statement.init, name=statement.name)
        elif isinstance(statement, ArrayDecl):
            self.visible_vars.add(statement.name)
            self.variable_types[statement.name] = statement.type_name
            if statement.init is not None:
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
        elif isinstance(statement, IndexAssign):
            self.generate_index_assign(statement)
        elif isinstance(statement, Break):
            if not self.loop_end_labels:
                message = "break outside of a loop"
                raise CompileError(message, line=statement.line)
            self.emit(f"        jmp {self.loop_end_labels[-1]}")
        elif isinstance(statement, Continue):
            if not self.loop_continue_labels:
                message = "continue outside of a loop"
                raise CompileError(message, line=statement.line)
            self.emit(f"        jmp {self.loop_continue_labels[-1]}")
        elif isinstance(statement, DoWhile):
            self.ax_clear()
            self.generate_do_while(statement)
        elif isinstance(statement, If):
            self.generate_if(statement)
        elif isinstance(statement, While):
            self.ax_clear()
            self.generate_while(statement)
        elif isinstance(statement, Return):
            self.generate_return(statement)
        elif isinstance(statement, Call):
            self.generate_call(statement, discard_return=True)
            self.ax_clear()
        else:
            message = f"unknown statement: {type(statement).__name__}"
            raise CompileError(message, line=statement.line)

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

    def new_label(self) -> int:
        """Allocate and return a new unique label index.

        Returns:
            The allocated label index.

        """
        label_index = self.label_id
        self.label_id += 1
        return label_index

    def new_string_label(self, content: str, /) -> str:
        """Allocate a string literal and return its label name.

        Identical strings are deduplicated — subsequent calls with the
        same *content* return the existing label.
        """
        for label, existing in self.strings:
            if existing == content:
                return label
        label = f"_str_{len(self.strings)}"
        self.strings.append((label, content))
        return label

    def peephole(self) -> None:
        """Run peephole optimization passes over generated assembly.

        Ordering note: :meth:`peephole_memory_arithmetic` runs before
        :meth:`peephole_store_reload` so that load/modify/store triples
        get folded into a direct ``inc D`` (etc.) first.  Reversing the
        order lets ``store_reload`` delete a reload that ``emit_store_local``
        added as a safety net — ``memory_arithmetic`` would then fuse
        the triple and leave the downstream read picking up stale AX.
        """
        self.peephole_dead_code()
        self.peephole_double_jump()
        self.peephole_jump_next()
        self.peephole_label_forwarding()
        self.peephole_memory_arithmetic()
        self.peephole_memory_arithmetic_byte()
        self.peephole_store_reload()
        self.peephole_dx_to_memory()
        self.peephole_constant_to_register()
        self.peephole_register_arithmetic()
        self.peephole_index_through_memory()
        self.peephole_fold_zero_save()
        self.peephole_compare_through_register()
        self.peephole_dead_ah()
        self.peephole_redundant_byte_mask()
        self.peephole_unused_cld()
        self.peephole_dead_stores()
        self.peephole_dead_test_after_sbb()
        self.peephole_redundant_bx()

    def peephole_compare_through_register(self) -> None:
        """Fold ``mov ax, <reg> / cmp ax, <X>`` into ``cmp <reg>, <X>``.

        When the cmp's left operand is already in a 16-bit register,
        the rebinding through AX is just to satisfy the existing
        ``emit_comparison`` template that always lands the left
        operand in AX.  ``cmp r16, r16`` and ``cmp r16, [mem]`` are
        the same length as the AX-flavored forms, so deleting the
        2-byte ``mov ax, <reg>`` is pure win.

        Only applied when the instruction after the cmp is a
        conditional jump — that's the only context where AX's value
        is provably dead after the cmp (the cmp itself doesn't write
        AX, but subsequent fall-through code might consume the
        rebinding).
        """
        registers = self.target.non_acc_registers
        jump_prefixes = (
            "ja ",
            "jae ",
            "jb ",
            "jbe ",
            "jc ",
            "je ",
            "jg ",
            "jge ",
            "jl ",
            "jle ",
            "jnc ",
            "jne ",
            "jno ",
            "jnp ",
            "jns ",
            "jnz ",
            "jo ",
            "jp ",
            "js ",
            "jz ",
        )
        mov_acc_prefix = f"mov {self.target.acc}, "
        cmp_acc_prefix = f"cmp {self.target.acc}, "
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            source = a[len(mov_acc_prefix) :]
            if source not in registers:
                i += 1
                continue
            if not b.startswith(cmp_acc_prefix):
                i += 1
                continue
            if not any(c.startswith(prefix) for prefix in jump_prefixes):
                i += 1
                continue
            rhs = b[len(cmp_acc_prefix) :]
            self.lines[i] = f"        cmp {source}, {rhs}"
            del self.lines[i + 1]

    def peephole_constant_to_register(self) -> None:
        """Fold ``mov ax, imm / mov <reg>, ax`` into a direct load.

        Replaces the two-instruction load with ``mov <reg>, imm`` or,
        when the constant is zero, ``xor <reg>, <reg>`` (one byte
        shorter).
        """
        registers = self.target.non_acc_registers
        mov_acc_prefix = f"mov {self.target.acc}, "
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            immediate = a[len(mov_acc_prefix) :]
            if immediate.startswith("[") or immediate in registers:
                i += 1
                continue
            if not b.startswith("mov "):
                i += 1
                continue
            parts = b[len("mov ") :].split(", ")
            if len(parts) != 2 or parts[1] != self.target.acc or parts[0] not in registers:
                i += 1
                continue
            register = parts[0]
            if immediate == "0":
                self.lines[i] = f"        xor {register}, {register}"
            else:
                self.lines[i] = f"        mov {register}, {immediate}"
            del self.lines[i + 1]
            continue

    def peephole_dead_ah(self) -> None:
        """Drop ``xor ah, ah`` when no intervening instruction reads AH.

        The zero-extension after ``mov al, [mem]`` is dead whenever the
        *first non-AX-preserving* instruction after it either
        overwrites AH (``mov ah, X``) or consumes only AL (``cmp al,
        imm``, ``test al, al``, ``mov [addr], al``, ``or al, al``).
        Byte-scalar global loads unconditionally emit the ``xor`` so
        the load is safe under later word-sized arithmetic; this
        peephole reclaims the two bytes on the common
        compare-and-branch path.

        Scans forward across AX-preserving instructions (register-to-
        register moves not touching AX, pushes/pops of non-AX regs,
        ``cmp`` / ``test`` on non-AX operands, ``clc`` / ``stc`` /
        ``cld``) so that patterns like ``xor ah, ah ; pop si ;
        test ax, ax`` fold the whole trio.  Stops at any control flow
        (``jmp`` / ``call`` / Jcc / ``ret`` / label), since the
        consumer might be reached along a different path where AH
        isn't zero.

        ``xor ah, ah`` itself sets flags (ZF=1, CF=0), but any consumer
        we elide against either sets its own flags (``cmp``, ``test``,
        ``or``) or doesn't use flags (``mov``), so dropping the xor
        never changes observable control-flow.
        """
        al_only_prefixes = (
            "mov [",  # mov [addr], al
            "cmp al,",
            "test al,",
            "or al,",
            "and al,",
            "xor al,",
            "add al,",
            "sub al,",
            "mov ah, ",
        )
        # AX-preserving skip list — instructions that don't touch AX
        # (including AH) and don't transfer control.  Any instruction
        # not recognized here aborts the scan conservatively.
        ax_preserving_pushpop = {
            f"{operation} {register}" for operation in ("push", "pop") for register in ("bx", "cx", "dx", "si", "di", "bp")
        }
        ax_preserving_prefixes = ("cmp ", "test ")  # cmp/test on non-AX also fine since they don't write AX
        ax_preserving_exact = {"clc", "stc", "cld"}

        def is_ax_preserving(stmt: str) -> bool:
            if stmt in ax_preserving_pushpop or stmt in ax_preserving_exact:
                return True
            # ``mov <non-AX reg>, ...`` preserves AX.
            match = re.match(r"mov\s+(bx|cx|dx|si|di|bp|bh|bl|ch|cl|dh|dl|sp|ss|es|ds|cs|fs|gs),", stmt)
            if match:
                return True
            # ``(add|sub|and|or|xor|inc|dec|shl|shr|neg|not) <non-AX reg>``.
            match = re.match(r"(add|sub|and|or|xor|inc|dec|shl|shr|neg|not)\s+(bx|cx|dx|si|di|bp|b[hl]|c[hl]|d[hl])", stmt)
            if match:
                return True
            # ``mov [mem], <non-AX>`` — a store that doesn't read AX.
            match = re.match(r"mov\s+\[[^\]]+\],\s*(bx|cx|dx|si|di|bp|\d+|0x[0-9a-fA-F]+)", stmt)
            if match:
                return True
            # ``(inc|dec|add|sub|and|or|xor) word|byte [mem]`` — memory
            # arithmetic not involving AX.
            match = re.match(r"(add|sub|and|or|xor|inc|dec)\s+(word|byte)\s+\[", stmt)
            if match:
                return True
            if any(stmt.startswith(prefix) for prefix in ax_preserving_prefixes):
                # ``cmp al, X`` / ``test al, X`` would itself be the
                # AL-only consumer we're looking for, not a skip.  Also
                # ``cmp ax, X`` / ``test ax, X`` read AH, so the scan
                # aborts conservatively in both cases.
                return not stmt.startswith(("cmp al,", "test al,", "cmp ax", "test ax"))
            return False

        i = 0
        while i < len(self.lines) - 1:
            if self.lines[i].strip() != "xor ah, ah":
                i += 1
                continue
            # Scan forward past AX-preserving instructions to the first
            # real consumer.
            j = i + 1
            while j < len(self.lines) and is_ax_preserving(self.lines[j].strip()):
                j += 1
            if j >= len(self.lines):
                i += 1
                continue
            b = self.lines[j].strip()
            # Word operation on AX that only inspects AL because AH is known
            # zero — rewrite to the byte form so the xor becomes dead.
            # ``test ax, ax`` → ``test al, al`` and ``cmp ax, K`` →
            # ``cmp al, K`` when K fits in a byte.  Byte form is 1 byte
            # shorter; the dropped xor reclaims another 2 bytes per
            # site.
            if b == "test ax, ax":
                self.lines[j] = self.lines[j].replace("test ax, ax", "test al, al")
                b = "test al, al"
            elif b.startswith("cmp ax, "):
                operand = b[len("cmp ax, ") :]
                try:
                    value = int(operand, 0)
                except ValueError:
                    value = None
                if value is not None and 0 <= value <= 255:
                    self.lines[j] = self.lines[j].replace("cmp ax, ", "cmp al, ", 1)
                    b = f"cmp al, {operand}"
            if b.startswith(al_only_prefixes):
                # For ``mov [addr], al`` verify the source operand is
                # actually ``al`` (not ``ax``) — the prefix match would
                # otherwise catch word stores.
                if b.startswith("mov [") and not b.endswith(", al"):
                    i += 1
                    continue
                del self.lines[i]
                continue
            i += 1

    def peephole_dead_code(self) -> None:
        """Remove unreachable instructions after unconditional jumps."""
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a.startswith("jmp ") and not b.endswith(":") and ":" not in b:
                del self.lines[i + 1]
                continue
            i += 1

    def peephole_dead_stores(self) -> None:
        """Remove stores to local variables that are never loaded."""
        # Collect all _l_ labels referenced anywhere except as a store
        # destination.  Stores are "mov ... [_l_X], <source>"; reads include
        # "mov <dst>, [_l_X]", "cmp word [_l_X], ...", etc.
        loaded: set[str] = set()
        for line in self.lines:
            stripped = line.strip()
            if self._extract_local_label(stripped) is not None:
                continue
            cursor = 0
            while True:
                start = stripped.find("[_l_", cursor)
                if start < 0:
                    break
                # Extract the bare label — stop at the first non-identifier
                # byte. `[_l_sum+1]` must count as a reference to `_l_sum`,
                # not `_l_sum+1`.
                label_end = start + 1
                while label_end < len(stripped) and (stripped[label_end].isalnum() or stripped[label_end] == "_"):
                    label_end += 1
                loaded.add(stripped[start + 1 : label_end])
                cursor = stripped.index("]", label_end) + 1
        # Remove stores and declarations for labels never loaded.
        result: list[str] = []
        for line in self.lines:
            stripped = line.strip()
            label = self._extract_local_label(stripped)
            if label is not None and label not in loaded:
                continue
            result.append(line)
        self.lines = result

    def peephole_dead_test_after_sbb(self) -> None:
        """Drop ``test ax, ax`` immediately after ``sbb ax, ax``.

        The sbb produces 0 (CF clear) or -1 (CF set) and already
        sets ZF correctly, so the compiler's follow-up test is dead.
        """
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a == f"sbb {self.target.acc}, {self.target.acc}" and b == f"test {self.target.acc}, {self.target.acc}":
                del self.lines[i + 1]
                continue
            i += 1

    def peephole_double_jump(self) -> None:
        """Collapse conditional-jump-over-unconditional-jump sequences.

        Replaces ``jCC .L1 / jmp .L2 / .L1:`` with ``jCC_inv .L2``.
        The ``.L1:`` label is kept when other jumps still target it;
        deleting it would leave those references dangling.
        """
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            # Match: jCC .label1 / jmp .label2 / .label1:
            parts = a.split()
            if len(parts) == 2 and parts[0] in JUMP_INVERT and b.startswith("jmp ") and c == f"{parts[1]}:":
                target = b.split()[1]
                label = parts[1]
                self.lines[i] = f"        {JUMP_INVERT[parts[0]]} {target}"
                label_referenced_elsewhere = any(
                    j != i and j != i + 1 and j != i + 2 and (tokens := self.lines[j].split()) and len(tokens) >= 2 and tokens[-1] == label
                    for j in range(len(self.lines))
                )
                if label_referenced_elsewhere:
                    del self.lines[i + 1]
                else:
                    del self.lines[i + 1 : i + 3]
                continue
            i += 1

    def peephole_dx_to_memory(self) -> None:
        """Fold ``mov ax, dx / mov [X], ax`` into ``mov [X], dx``.

        The pair arises after a ``%`` expression whose remainder the
        ``%`` handler stages into AX just so the standard store path
        can flush it to the local — but the intermediate AX hop is
        dead if the next instruction writes that memory anyway.
        """
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a == "mov ax, dx" and b.startswith("mov [") and b.endswith("], ax"):
                self.lines[i + 1] = f"{self.lines[i + 1][:-3]}dx"
                del self.lines[i]
                continue
            i += 1

    def peephole_fold_zero_save(self) -> None:
        """Fuse ``xor reg, reg / push reg`` into ``push 0``.

        When ``cursor_column = 0`` is immediately followed by code that
        clobbers the pinned CX as scratch (and therefore needs to
        push/pop it), the compiler emits ``xor cx, cx / push cx``
        followed later by ``pop cx`` to restore zero.  The two-byte
        ``xor cx, cx`` plus one-byte ``push cx`` (3 bytes) collapses
        to a single two-byte ``push 0`` (``6A 00``) — the body and the
        eventual ``pop cx`` are unchanged, since the popped value is
        still zero.

        The xor's flag-side-effects are dead in every emission path
        cc.py produces here: ``push cx`` doesn't read flags and the
        intervening body overwrites them before the next conditional
        jump.
        """
        registers = {"ax", "bx", "cx", "dx", "si", "di", "bp"}
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a.startswith("xor ") and " " in a[4:]:
                parts = a[4:].split(", ")
                if len(parts) == 2 and parts[0] == parts[1] and parts[0] in registers and b == f"push {parts[0]}":
                    self.lines[i] = "        push 0"
                    del self.lines[i + 1]
                    continue
            i += 1

    def peephole_index_through_memory(self) -> None:
        """Use ``add si, [mem]`` instead of staging through AX.

        Recognizes::

            push ax
            mov si, [BASE]
            mov ax, [INDEX]
            add si, ax
            pop ax

        and rewrites it as::

            mov si, [BASE]
            add si, [INDEX]

        Safe because the eight-byte form ``add si, [mem]`` is a single
        8086 instruction and AX is never disturbed.  Saves the
        push/pop AX pair (2 bytes) and the redundant ``mov ax, [mem]``
        (3 bytes) for a net 3-byte gain (the new ``add si, [mem]`` is
        2 bytes longer than the old ``add si, ax``).
        """
        i = 0
        while i < len(self.lines) - 4:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            d = self.lines[i + 3].strip()
            e = self.lines[i + 4].strip()
            if a != "push ax" or e != "pop ax":
                i += 1
                continue
            if not (b.startswith("mov si, [") and b.endswith("]")):
                i += 1
                continue
            if not (c.startswith("mov ax, [") and c.endswith("]")):
                i += 1
                continue
            if d != "add si, ax":
                i += 1
                continue
            mem_operand = c[len("mov ax, ") :]
            self.lines[i] = self.lines[i + 1]
            self.lines[i + 1] = f"        add si, {mem_operand}"
            del self.lines[i + 2 : i + 5]
            continue

    def peephole_jump_next(self) -> None:
        """Remove unconditional jumps to the immediately following label."""
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a.startswith("jmp ") and b == f"{a.split()[1]}:":
                del self.lines[i]
                continue
            i += 1

    def peephole_label_forwarding(self) -> None:
        """Retarget jumps through a label that immediately trampolines.

        When an unreachable-by-fall-through label ``.L1:`` is followed
        by ``jmp .L2``, rewrite every ``jCC .L1`` in the rest of the
        function to ``jCC .L2`` and drop the label/jmp pair.  "No
        fall-through" is proven by requiring the previous line to be
        an unconditional ``jmp`` — that's the shape ``break`` at the
        end of a ``while (1)`` body produces right before the
        implicit program exit.
        """
        jumps = {
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
        }
        i = 1
        while i < len(self.lines) - 1:
            previous_line = self.lines[i - 1].strip()
            label_line = self.lines[i].strip()
            next_line = self.lines[i + 1].strip()
            if not previous_line.startswith("jmp "):
                i += 1
                continue
            if not (label_line.endswith(":") and " " not in label_line):
                i += 1
                continue
            if not next_line.startswith("jmp "):
                i += 1
                continue
            old_label = label_line[:-1]
            new_target = next_line[len("jmp ") :]
            if old_label == new_target:
                i += 1
                continue
            for j in range(len(self.lines)):
                if j == i or j == i + 1:
                    continue
                stripped = self.lines[j].strip()
                parts = stripped.split(None, 1)
                if len(parts) == 2 and parts[0] in jumps and parts[1] == old_label:
                    self.lines[j] = self.lines[j].replace(old_label, new_target)
            del self.lines[i : i + 2]
            i = max(1, i - 1)

    def peephole_memory_arithmetic(self) -> None:
        """Fuse load/modify/store sequences into direct arithmetic.

        Handles these patterns where ``D`` is either a memory operand
        ``[L]`` or a 16-bit general-purpose register:
        - ``mov ax, D / mov cx, 1 / add ax, cx / mov D, ax`` →
          ``inc D`` (or ``inc word [L]`` for memory)
        - ``mov ax, D / mov cx, 1 / sub ax, cx / mov D, ax`` →
          ``dec D`` (or ``dec word [L]`` for memory)
        - ``mov ax, D / mov cx, imm / (add|sub) ax, cx /
          mov D, ax`` → ``(add|sub) D, imm``
        """
        registers = self.target.non_acc_registers
        mov_acc_prefix = f"mov {self.target.acc}, "
        mov_cx_prefix = f"mov {self.target.count_register}, "
        add_acc_cx = f"add {self.target.acc}, {self.target.count_register}"
        sub_acc_cx = f"sub {self.target.acc}, {self.target.count_register}"
        i = 0
        while i < len(self.lines) - 3:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            d = self.lines[i + 3].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            source = a[len(mov_acc_prefix) :]
            is_memory = source.startswith("[") and source.endswith("]")
            is_register = source in registers
            if not (is_memory or is_register):
                i += 1
                continue
            if not (b.startswith(mov_cx_prefix) and not b.endswith("]")):
                i += 1
                continue
            if c not in {add_acc_cx, sub_acc_cx}:
                i += 1
                continue
            if d != f"mov {source}, {self.target.acc}":
                i += 1
                continue
            immediate = b[len(mov_cx_prefix) :]
            operator = "add" if c == add_acc_cx else "sub"
            width = f"{self.target.word_size} " if is_memory else ""
            if immediate == "1":
                instruction = "inc" if operator == "add" else "dec"
                self.lines[i] = f"        {instruction} {width}{source}"
            else:
                self.lines[i] = f"        {operator} {width}{source}, {immediate}"
            del self.lines[i + 1 : i + 4]
            continue
        # Second pass: 3-instruction pattern without CX intermediate.
        # Handles four shapes of ``D = D <operation> Y`` where D is memory or
        # a 16-bit register:
        #   mov ax, D / (add|sub|and) ax, imm  / mov D, ax → operation D, imm
        #   mov ax, D / inc ax  / mov D, ax                → inc D
        #   mov ax, D / dec ax  / mov D, ax                → dec D
        #   mov ax, D / (add|sub|and) ax, <reg> / mov D, ax → operation D, <reg>
        mnemonic_operations = {"add", "sub", "and", "or", "xor"}
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            source = a[len(mov_acc_prefix) :]
            is_memory = source.startswith("[") and source.endswith("]")
            is_register = source in registers
            if not (is_memory or is_register):
                i += 1
                continue
            operator = None
            operand = None
            if b == f"inc {self.target.acc}":
                operator = "inc"
                operand = ""
            elif b == f"dec {self.target.acc}":
                operator = "dec"
                operand = ""
            else:
                for operation in mnemonic_operations:
                    prefix = f"{operation} {self.target.acc}, "
                    if b.startswith(prefix):
                        operator = operation
                        operand = b[len(prefix) :]
                        break
            if operator is None:
                i += 1
                continue
            # Reject memory operands — would need swapping to ``mov ax, [X] /
            # operation D, ax`` and handled by the next pass instead.
            if operand.startswith("["):
                i += 1
                continue
            if c != f"mov {source}, {self.target.acc}":
                i += 1
                continue
            width = f"{self.target.word_size} " if is_memory else ""
            if operator in ("inc", "dec"):
                self.lines[i] = f"        {operator} {width}{source}"
            elif operand == "1" and operator in ("add", "sub"):
                instruction = "inc" if operator == "add" else "dec"
                self.lines[i] = f"        {instruction} {width}{source}"
            else:
                self.lines[i] = f"        {operator} {width}{source}, {operand}"
            del self.lines[i + 1 : i + 3]
            continue
        # Third pass: ``D = D <operation> [X]`` with both sides in memory.
        # ``mov ax, D / operation ax, [X] / mov D, ax`` collapses to
        # ``mov ax, [X] / operation D, ax`` (10 bytes → 7 for word operations).  Only
        # safe when D is memory (the target of ``operation D, ax`` must be
        # addressable as r/m16) and D ≠ X (overlapping would read the
        # stale value after the operation writes D).
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            source = a[len(mov_acc_prefix) :]
            if not (source.startswith("[") and source.endswith("]")):
                i += 1
                continue
            operator = None
            rhs = None
            for operation in ("add", "sub", "and", "or", "xor"):
                prefix = f"{operation} {self.target.acc}, "
                if b.startswith(prefix):
                    operator = operation
                    rhs = b[len(prefix) :]
                    break
            if operator is None:
                i += 1
                continue
            if not (rhs.startswith("[") and rhs.endswith("]")):
                i += 1
                continue
            if rhs == source:
                i += 1
                continue
            if c != f"mov {source}, {self.target.acc}":
                i += 1
                continue
            self.lines[i] = f"        mov {self.target.acc}, {rhs}"
            self.lines[i + 1] = f"        {operator} {source}, {self.target.acc}"
            del self.lines[i + 2]
            continue

    def peephole_memory_arithmetic_byte(self) -> None:
        """Fuse byte-global load / modify / store into memory-direct byte ops.

        Byte-scalar globals load via ``mov al, [_g_X] / xor ah, ah`` and
        store via ``mov [_g_X], al``; a compound-assign emits:

            mov al, [_g_X]
            xor ah, ah
            inc ax           (or: add|sub|and|or|xor ax, imm16,
                              or: mov cx, imm16 / add|sub ax, cx)
            mov [_g_X], al

        The low byte of the AX-width operation is identical to the
        corresponding AL-width operation on the same low byte (addition /
        subtraction / bitwise all ignore the high byte when the result
        is truncated to AL on store), so the whole sequence collapses
        to a single memory-direct byte instruction:

            inc byte [_g_X]              4 bytes (FE 06 xxxx)
            dec byte [_g_X]              4 bytes
            add|sub byte [_g_X], imm8    5 bytes (80 /N xxxx imm8)
            and|or|xor byte [_g_X], imm8 5 bytes

        Byte-width ops require the immediate to fit in 8 bits —
        bitwise masks wider than a byte would lose their high-byte
        effect when narrowed.  For ``add`` / ``sub`` any 16-bit
        immediate truncates cleanly to imm8 for the low-byte result
        (carry into AH is discarded on store), so those fuse
        regardless of the original ``mov cx, <imm>`` width.

        Saves 4-5 bytes per compound-assign site on a byte-scalar
        global — the reason cc.py can keep ``include_depth`` /
        ``iteration_count`` / similar arithmetic-heavy byte globals
        as ``uint8_t`` without regressing binary size.
        """

        def fits_imm8(literal: str, /) -> bool:
            try:
                value = int(literal, 0)
            except ValueError:
                return False
            return -128 <= value <= 255

        # 4-line pattern without CX intermediate:
        #   mov al, [mem] / xor ah, ah / <operation> ax, <imm|reg> / mov [mem], al
        single_immediate_operations = {"add", "sub", "and", "or", "xor"}
        i = 0
        while i < len(self.lines) - 3:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            d = self.lines[i + 3].strip()
            if not a.startswith("mov al, ["):
                i += 1
                continue
            source = a[len("mov al, ") :]
            if not (source.startswith("[") and source.endswith("]")):
                i += 1
                continue
            if b != "xor ah, ah":
                i += 1
                continue
            if d != f"mov {source}, al":
                i += 1
                continue
            if c == "inc ax":
                self.lines[i] = f"        inc byte {source}"
                del self.lines[i + 1 : i + 4]
                continue
            if c == "dec ax":
                self.lines[i] = f"        dec byte {source}"
                del self.lines[i + 1 : i + 4]
                continue
            operation_name: str | None = None
            operand: str | None = None
            for operation in single_immediate_operations:
                prefix = f"{operation} ax, "
                if c.startswith(prefix):
                    operation_name = operation
                    operand = c[len(prefix) :]
                    break
            if operation_name is None:
                i += 1
                continue
            if operand.startswith("["):
                i += 1
                continue
            # Bitwise masks narrowed to byte can silently drop
            # high-byte effect; only fuse when the literal fits in 8
            # bits.  add/sub truncate cleanly so any imm is OK.
            if operation_name in ("and", "or", "xor") and not fits_imm8(operand):
                i += 1
                continue
            if operation_name == "add" and operand == "1":
                self.lines[i] = f"        inc byte {source}"
            elif operation_name == "sub" and operand == "1":
                self.lines[i] = f"        dec byte {source}"
            else:
                # NASM accepts the wider literal for add/sub byte; it
                # assembles the low 8 bits since the destination is
                # byte-sized.
                self.lines[i] = f"        {operation_name} byte {source}, {operand}"
            del self.lines[i + 1 : i + 4]
            continue

        # 5-line pattern with CX intermediate (matches the codegen shape
        # before peephole_memory_arithmetic fuses the CX-mov):
        #   mov al, [mem] / xor ah, ah / mov cx, <imm> / (add|sub) ax, cx
        #   / mov [mem], al
        i = 0
        while i < len(self.lines) - 4:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            d = self.lines[i + 3].strip()
            e = self.lines[i + 4].strip()
            if not a.startswith("mov al, ["):
                i += 1
                continue
            source = a[len("mov al, ") :]
            if not (source.startswith("[") and source.endswith("]")):
                i += 1
                continue
            if b != "xor ah, ah":
                i += 1
                continue
            if not (c.startswith("mov cx, ") and not c.endswith("]")):
                i += 1
                continue
            if d not in {"add ax, cx", "sub ax, cx"}:
                i += 1
                continue
            if e != f"mov {source}, al":
                i += 1
                continue
            immediate = c[len("mov cx, ") :]
            operator = "add" if d == "add ax, cx" else "sub"
            if immediate == "1":
                instruction = "inc" if operator == "add" else "dec"
                self.lines[i] = f"        {instruction} byte {source}"
            else:
                self.lines[i] = f"        {operator} byte {source}, {immediate}"
            del self.lines[i + 1 : i + 5]
            continue

    def peephole_redundant_bx(self) -> None:
        """Remove redundant ``mov bx, X`` / ``mov si, X`` reloads.

        Tracks the value in each scratch register across instructions
        that don't clobber it (comparisons, conditional jumps).  Resets
        on labels, calls, interrupts, and any instruction that writes
        to the register.  BX and SI are both subscript scratch targets
        so either can linger with a useful value across sites.
        """
        self._dedup_register_reloads("bx")
        self._dedup_register_reloads("si")

    def peephole_redundant_byte_mask(self) -> None:
        """Drop ``and ax, 255`` when AX is provably zero-extended from a byte.

        The C expression ``byte_local & 0xFF`` (or any wider mask whose
        low byte saturates the byte operand) codegens as ``mov al,
        [X] / xor ah, ah / and ax, 255``.  The zero-extend has already
        cleared AH, so the mask is a no-op on the value.  Dropping it
        saves 4 bytes per site — there are 106+ sites in asm.c from
        the ``emit_byte(x & 0xFF)`` idiom alone.

        The ``and`` does set flags, though: ZF = (AL == 0), unlike the
        preceding ``xor`` which always leaves ZF=1 (AH=0).  So the
        drop is only safe when the following instruction doesn't
        consume flags — walk forward to confirm.  Conservative
        allowlist: ``mov`` / ``call`` / ``push`` / ``pop`` / ``shl`` /
        ``shr`` / ``ret`` don't read flags; conditional jumps
        (``j*`` except ``jmp``) and ``adc`` / ``sbb`` / ``rcl`` /
        ``rcr`` do.  Anything else: bail.
        """
        flag_safe_prefixes = (
            "mov ",
            "call ",
            "push ",
            "pop ",
            "shl ",
            "shr ",
            "ret",
            "int ",
            "lea ",
        )
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a == "xor ah, ah" and b == "and ax, 255":
                # Look past the mask at what actually consumes the value.
                follower = self.lines[i + 2].strip() if i + 2 < len(self.lines) else ""
                if follower.startswith(flag_safe_prefixes):
                    del self.lines[i + 1]
                    continue
            i += 1

    def peephole_register_arithmetic(self) -> None:
        """Compute directly into a pinned-local target register.

        Turns ``mov ax, X / <operation> ax, Y / mov <reg>, ax`` into
        ``mov <reg>, X / <operation> <reg>, Y`` when <reg> isn't already
        read by Y (e.g., ``sub reg, reg`` would zero it).

        Saves the trailing ``mov <reg>, ax`` (2 bytes) whenever the
        arithmetic result is being piped straight into a register
        (typically a pinned local).  After the transform AX retains
        whatever it held before the sequence, which is safe because
        pinned-register locals aren't referenced via AX tracking
        post-codegen.
        """
        registers = self.target.non_acc_registers
        operations = tuple(f"{operation} {self.target.acc}," for operation in ("add", "sub", "and", "or", "xor"))
        mov_acc_prefix = f"mov {self.target.acc}, "
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            if not any(b.startswith(operation) for operation in operations):
                i += 1
                continue
            if not c.startswith("mov "):
                i += 1
                continue
            parts = c[len("mov ") :].split(", ")
            if len(parts) != 2 or parts[1] != self.target.acc or parts[0] not in registers:
                i += 1
                continue
            target = parts[0]
            # Skip when the operand of the arithmetic references the
            # target register — rewriting would make it self-referential.
            operand = b.split(", ", 1)[1]
            if target in operand.split():
                i += 1
                continue
            source = a[len(mov_acc_prefix) :]
            if target in source.split():
                i += 1
                continue
            new_op = b.replace(f"{self.target.acc},", f"{target},", 1)
            self.lines[i] = f"        mov {target}, {source}"
            self.lines[i + 1] = f"        {new_op}"
            del self.lines[i + 2]
            continue

    def _dedup_register_reloads(self, register: str, /) -> None:
        """Skip ``mov {register}, <source>`` when ``<source>`` already reached this register.

        The tracked source goes stale on anything that changes either
        the register itself (direct clobber) or the source register
        when ``<source>`` is register-sourced — e.g. ``mov si, ax / inc
        ax / mov si, ax`` is NOT a redundant reload because ``inc ax``
        makes the second ``mov si, ax`` store a different value.
        Memory / immediate sources stay stable until the destination
        register is clobbered.
        """
        value: str | None = None
        result: list[str] = []
        # Instructions that clobber the destination register directly.
        clobber_prefixes = (
            f"add {register}",
            "call ",
            "int ",
            "lodsb",
            "lodsw",
            "movsb",
            "movsw",
            f"pop {register}",
            "rep ",
            f"sub {register}",
            "xchg",
            f"xor {register}",
        )
        # Register-modifying mnemonics we care about as SOURCE clobbers.
        # ``mov <reg>, X`` is handled below alongside the other writers.
        source_clobber_operations = (
            "add ",
            "and ",
            "dec ",
            "div ",
            "idiv ",
            "imul ",
            "inc ",
            "mov ",
            "mul ",
            "neg ",
            "not ",
            "or ",
            "rcl ",
            "rcr ",
            "rol ",
            "ror ",
            "sal ",
            "sar ",
            "shl ",
            "shr ",
            "sub ",
            "xor ",
        )
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith(f"mov {register}, "):
                source = stripped[len(f"mov {register}, ") :]
                if source == value:
                    continue  # redundant — skip
                value = source
            elif stripped.endswith(":") or stripped.startswith(clobber_prefixes):
                value = None
            elif value is not None and "[" not in value:
                # Source is a register or immediate.  Check whether this
                # instruction writes to the source register, invalidating
                # the stored value.  e.g. ``mov si, ax / inc ax`` — the
                # tracked ``ax`` in SI no longer matches the current AX.
                for operation in source_clobber_operations:
                    if not stripped.startswith(operation):
                        continue
                    target = stripped[len(operation) :].split(",", 1)[0].strip()
                    if target == value or (len(target) == 2 and target[1] in "lh" and target[0] == value[0]):
                        value = None
                    break
            result.append(line)
        self.lines = result

    def peephole_store_reload(self) -> None:
        """Remove redundant store-then-reload sequences.

        Looks for ``mov [ADDR], ax`` followed (possibly across
        AX-preserving instructions like ``cmp``, ``test``, conditional
        jumps, or pushes/pops of non-AX registers) by ``mov ax, [ADDR]``
        — the reload is dead.  Stops scanning when it hits an
        instruction that could change AX, ``[ADDR]``, or control flow
        in a way that lets a different value reach the reload.
        """
        skip_prefixes = (
            "cmp ",
            "test ",
            "ja ",
            "jae ",
            "jb ",
            "jbe ",
            "jc ",
            "je ",
            "jg ",
            "jge ",
            "jl ",
            "jle ",
            "jnc ",
            "jne ",
            "jno ",
            "jnp ",
            "jns ",
            "jnz ",
            "jo ",
            "jp ",
            "js ",
            "jz ",
        )
        non_ax_pushpop = {f"{operation} {register}" for operation in ("push", "pop") for register in ("bx", "cx", "dx", "si", "di", "bp")}
        i = 0
        while i < len(self.lines) - 1:
            line = self.lines[i].strip()
            if not (line.startswith("mov [") and line.endswith((f"], {self.target.acc}", "], al"))):
                i += 1
                continue
            address = line[4 : line.index("]") + 1]
            reload_word = f"mov {self.target.acc}, {address}"
            reload_byte = f"mov al, {address}"
            j = i + 1
            removed = False
            while j < len(self.lines):
                candidate = self.lines[j].strip()
                if candidate in (reload_word, reload_byte):
                    del self.lines[j]
                    removed = True
                    break
                # AX-preserving instructions: cmp/test/Jcc and pushes/pops
                # of registers other than AX.
                if any(candidate.startswith(prefix) for prefix in skip_prefixes) or candidate in non_ax_pushpop:
                    j += 1
                    continue
                break
            if removed:
                continue
            i += 1

    def peephole_unused_cld(self) -> None:
        """Remove or deduplicate ``cld`` instructions.

        When no string instruction is emitted, all ``cld`` instructions
        are removed.  Otherwise, redundant ``cld`` instructions are
        removed when the direction flag is already clear (no intervening
        label, call, or interrupt that could change DF).
        """
        string_operations = ("lodsb", "lodsw", "stosb", "stosw", "movsb", "movsw", "scasb", "scasw", "cmpsb", "cmpsw", "rep ")
        has_string_operations = any(any(line.strip().startswith(operation) for operation in string_operations) for line in self.lines)
        if not has_string_operations:
            self.lines = [line for line in self.lines if line.strip() != "cld"]
            return
        # Deduplicate: track whether DF is known-clear.
        df_clear = False
        result: list[str] = []
        for line in self.lines:
            stripped = line.strip()
            if stripped == "cld":
                if df_clear:
                    continue  # redundant
                df_clear = True
            elif stripped.endswith(":") or stripped.startswith(("call ", "int ")):
                df_clear = False
            result.append(line)
        self.lines = result

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
                if top_level and self._is_constant_alias(body=statements, statement=statement):
                    alias = self._constant_expression(statement.init)
                    self.constant_aliases[statement.name] = alias
                    for name in self._collect_constant_references(statement.init):
                        include = self.NAMED_CONSTANT_INCLUDES.get(name)
                        if include is not None:
                            self.required_includes.add(include)
                    continue
                if statement.type_name != "unsigned long" and statement.name in self.auto_pin_candidates:
                    following = statements[index + 1] if index + 1 < len(statements) else None
                    if self.can_auto_pin(following_statement=following, statement=statement):
                        self.pinned_register[statement.name] = self.auto_pin_candidates[statement.name]
                        continue
                if statement.name in self.virtual_long_locals:
                    continue
                size = self.target.type_sizes[statement.type_name]
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
                self.allocate_local(statement.name)
            elif isinstance(statement, If):
                self.scan_locals(statement.body, top_level=False)
                if statement.else_body is not None:
                    self.scan_locals(statement.else_body, top_level=False)
            elif isinstance(statement, (DoWhile, While)):
                self.scan_locals(statement.body, top_level=False)

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
