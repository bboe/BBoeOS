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

from cc.ast_nodes import (
    ArrayDecl,
    Assign,
    BinaryOperation,
    Call,
    Char,
    DoWhile,
    Function,
    If,
    Index,
    IndexAssign,
    InlineAsm,
    Int,
    LogicalAnd,
    LogicalOr,
    MemberAccess,
    MemberAssign,
    MemberIndex,
    Node,
    Param,
    String,
    StructDecl,
    StructInit,
    Var,
    VarDecl,
    While,
)
from cc.codegen.base import CodeGeneratorBase
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
from cc.utils import decode_string_escapes, string_byte_length


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
        "asm": frozenset({"ax", "bx", "cx", "dx", "si", "di"}),
        "checksum": frozenset({"ax", "bx", "cx", "si"}),
        "chmod": frozenset({"ax", "si"}),
        "close": frozenset({"ax", "bx"}),
        "datetime": frozenset({"ax", "dx"}),
        "die": frozenset(),
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
        "kernel_inb": frozenset({"ax", "dx"}),
        "kernel_insw": frozenset({"ax", "cx", "di", "dx"}),
        "kernel_inw": frozenset({"ax", "dx"}),
        "kernel_outb": frozenset({"ax", "dx"}),
        "kernel_outsw": frozenset({"ax", "cx", "dx", "si"}),
        "kernel_outw": frozenset({"ax", "dx"}),
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
        "rmdir": frozenset({"ax", "si"}),
        "sendto": frozenset({"ax", "bx", "cx", "di", "dx", "si"}),
        "set_exec_arg": frozenset({"ax"}),
        "set_palette_color": frozenset({"ax", "bx", "cx", "dx"}),
        "shutdown": frozenset({"ax"}),
        "sleep": frozenset({"ax", "cx"}),
        "strlen": frozenset({"ax", "cx", "di"}),
        "unlink": frozenset({"ax", "si"}),
        "uptime": frozenset({"ax"}),
        "uptime_ms": frozenset({"ax", "dx"}),
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
        self.asm_symbol_globals: dict[str, str] = {}  # name → asm symbol (no _g_ prefix)
        self.ax_is_byte: bool = False
        self.ax_local: str | None = None
        self.bss_total: int | str = 0  # total BSS bytes; int when all literal, str EQU name otherwise
        self.bss_vars: list[tuple[str, str]] = []  # (name, byte_count_expr) for zero-init globals
        self.division_remainder: tuple | None = None
        # in_register_params / out_register_params map function name → {param_index → register}.
        # Populated during the first pass over function definitions in generate().
        self.in_register_params: dict[str, dict[int, str]] = {}
        self.out_register_params: dict[str, dict[int, str]] = {}
        self.pinned_register: dict[str, str] = {}
        self.register_aliased_globals: dict[str, str] = {}  # name → register (e.g. "si")
        self.store_target_register: str | None = None
        # struct_layouts maps struct tag name → {field_name: (byte_offset, byte_size)}.
        # Populated by _register_globals when StructDecl nodes are encountered.
        self.struct_layouts: dict[str, dict[str, tuple[int, int]]] = {}
        self.target_mode: str = target_mode

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
            if function.name == "main" or function.is_prototype:
                continue
            self.safe_pin_registers = self.compute_safe_pin_registers(function.body)
            # Fastcall (regparm(1)) param 0 lives in AX on entry and is spilled
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
                if tag in self.struct_layouts:
                    return sum(field_size for _, field_size, _element_size in self.struct_layouts[tag].values())
            return self.target.type_size(type_name)
        if type_name.endswith("*"):
            base = type_name[:-1]
            if base in ("char", "uint8_t") or base in self.BYTE_TYPES:
                return 1
            if base.startswith("struct "):
                tag = base[7:]
                if tag in self.struct_layouts:
                    return sum(field_size for _, field_size, _element_size in self.struct_layouts[tag].values())
            return self.target.type_size(base)
        return 1

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
        """Emit the 4-byte BSS trailer (``dw <size>; dw 0B055h``) just before ``_program_end``.

        Sets ``self.bss_total`` so the caller can emit ``_bss_end`` and
        the per-variable EQUs after ``_program_end:`` (avoiding forward
        references that the self-hosted assembler cannot resolve).
        """
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
            self.emit(f"        dw {total}")
        else:
            self.bss_total = "_bss_total_size"
            self.emit("        dw _bss_total_size")
        self.emit("        dw 0B055h")

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
        index: Node,
        is_byte: bool,
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

        When *preserve_ax* is True, any path that evaluates the index
        through AX pushes/pops AX so the caller's value survives.
        """
        element_size = 1 if is_byte else self.target.int_size
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
            if not is_byte:
                self._emit_scale_int_index(si)
        elif isinstance(index, Var) and self._is_memory_scalar(index.name) and not self._is_byte_scalar(index.name):
            self.emit(f"        mov {si}, [{self._local_address(index.name)}]")
            if not is_byte:
                self._emit_scale_int_index(si)
        else:
            if preserve_ax:
                self.emit(f"        push {self.target.acc}")
            self.generate_expression(index)
            if not is_byte:
                self._emit_scale_int_index(self.target.acc)
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
        zero-initialized globals are emitted inline as ``times N db 0``
        labels — the BSS-EQU model requires ``_program_end:`` which is
        absent in kernel output.
        """
        if not self.global_scalars and not self.global_arrays:
            return
        int_directive = "dd" if self.target.int_size == 4 else "dw"
        self.emit(";; --- global data ---")
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
            if declaration.init is None:
                stride = 1 if self._is_byte_scalar_global(name) else self.target.int_size
                if self.target_mode == "kernel":
                    self.emit(f"_g_{name}: times {stride} db 0")
                else:
                    self.bss_vars.append((name, str(stride)))
            else:
                init_expression = self._constant_expression(declaration.init)
                directive = "db" if self._is_byte_scalar_global(name) else int_directive
                self.emit(f"_g_{name}: {directive} {init_expression}")
        for name in sorted(self.global_arrays):
            declaration = self.global_arrays[name]
            is_byte = declaration.type_name in self.BYTE_TYPES
            is_struct = declaration.type_name.startswith("struct ")
            if is_struct:
                stride = self._type_size(declaration.type_name)
            elif is_byte:
                stride = 1
            else:
                stride = self.target.int_size
            if is_struct and declaration.init is not None:
                struct_name = declaration.type_name[len("struct ") :]
                layout = self.struct_layouts[struct_name]
                lines: list[str] = []
                for element in declaration.init.elements:
                    assert isinstance(element, StructInit)
                    for i, (field_name, (offset, field_size, _element_size)) in enumerate(layout.items()):
                        value = self._constant_expression(element.fields[i]) if i < len(element.fields) else "0"
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
                self.emit(f"_g_{name}: {lines[0]}")
                for line in lines[1:]:
                    self.emit(f"        {line}")
            elif declaration.init is not None:
                directive = "db" if is_byte else int_directive
                rendered = [
                    self.new_string_label(element.content) if isinstance(element, String) else self._constant_expression(element)
                    for element in declaration.init.elements
                ]
                self.emit(f"_g_{name}: {directive} {', '.join(rendered)}")
            else:
                size_expression = self._constant_expression(declaration.size)
                byte_count = f"({size_expression})*{stride}" if stride != 1 else size_expression
                if self.target_mode == "kernel":
                    self.emit(f"_g_{name}: times {byte_count} db 0")
                else:
                    self.bss_vars.append((name, byte_count))

    def _type_size(self, type_name: str, /) -> int:
        """Return the byte size of *type_name* including struct types.

        Handles all primitive types via the target's ``type_sizes`` table,
        pointer-to-struct (``"struct TAG*"``) as a pointer-sized word, and
        value-struct (``"struct TAG"``) by summing the declared field sizes.
        Raises ``CompileError`` for unknown types.
        """
        if type_name == "int" or "*" in type_name or type_name in self.target.type_sizes:
            return self.target.type_size(type_name)
        if type_name == "function_pointer":
            return self.target.int_size
        if type_name.startswith("struct "):
            tag = type_name[7:]
            if tag not in self.struct_layouts:
                message = f"unknown struct '{tag}'"
                raise CompileError(message)
            return sum(field_size for _, field_size, _element_size in self.struct_layouts[tag].values())
        message = f"unknown type '{type_name}'"
        raise CompileError(message)

    def _validate_array_init(self, elements: list[Node]) -> None:
        """Validate global array initializer elements are all constant expressions."""
        for element in elements:
            if isinstance(element, String):
                continue
            if isinstance(element, StructInit):
                for field in element.fields:
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
        """Generate code for ``ptr->field`` or ``obj.field`` as an rvalue."""
        if not expression.arrow:
            message = "dot member access on local struct values is not yet supported; use a pointer and '->'"
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
        offset, field_size, element_size = layout[expression.member_name]
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
        if field_size not in (1, 2):
            message = f"reading '{expression.member_name}' (size {field_size}) not yet supported; use asm()"
            raise CompileError(message, line=expression.line)
        self.ax_clear()
        if self.si_local == object_name:
            base_reg = self.target.si_register
        else:
            self._emit_load_var(object_name, register=self.target.bx_register)
            base_reg = self.target.bx_register
        addr = f"[{base_reg}+{offset}]" if offset else f"[{base_reg}]"
        if field_size == 1:
            self.emit_byte_load_zx(addr)
        else:
            self.emit(f"        mov {self.target.acc}, {addr}")
        self.ax_clear()

    def generate_member_assign(self, statement: MemberAssign, /) -> None:
        """Generate code for ``ptr->field = expr;``."""
        if not statement.arrow:
            message = "dot member assign on local struct values is not yet supported; use a pointer and '->'"
            raise CompileError(message, line=statement.line)
        object_name = statement.object_name
        struct_type = self.variable_types.get(object_name)
        if struct_type is None:
            message = f"undefined variable '{object_name}'"
            raise CompileError(message, line=statement.line)
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
        offset, field_size, _element_size = layout[statement.member_name]
        if field_size not in (1, 2):
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
        field_offset, _field_size, element_size = layout[expression.member_name]
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
            # ``_is_simple_arg`` only admits BinaryOperation(+/-, leaf, leaf), and
            # the topological scheduler in ``_emit_register_arg_moves``
            # already verified that ``target`` is not read by any other
            # pending arg.  Evaluate into AX, then move into target.
            self.generate_expression(arg)
            if target != self.target.acc:
                source = self.target.low_word(self.target.acc) if len(target) < len(self.target.acc) else self.target.acc
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
        E-registers in pmode and 16-bit aliases in real mode.
        Normalise both sides through ``target.low_word`` so the
        comparison still matches when the two halves disagree.
        """
        low_word = self.target.low_word
        normalised_clobbers = frozenset(low_word(register) for register in clobbers)
        return sorted(
            register
            for register in self.pinned_register.values()
            if low_word(register) in normalised_clobbers and low_word(register) != "ax"
        )

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
            if isinstance(declaration, StructDecl):
                # Build a packed field layout:
                # {field_name: (byte_offset, total_byte_size, element_byte_size)}.
                # For scalar fields total == element.  For array fields
                # (``uint8_t ip[4]``) total = element_size * count, while
                # element_size is the per-element width — needed by
                # ``entry->field[i]`` indexing to scale the index.
                layout: dict[str, tuple[int, int, int]] = {}
                cursor = 0
                for field in declaration.fields:
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
                    layout[field.field_name] = (cursor, field_size, element_size)
                    cursor += field_size
                self.struct_layouts[declaration.name] = layout
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
                    # width ("esi" in 32-bit pmode) so every downstream
                    # read emits the right-width register without a
                    # per-use lookup.
                    self.register_aliased_globals[name] = self.target.widen_gp(declaration.asm_register)
                if declaration.asm_symbol is not None:
                    self.asm_symbol_globals[name] = declaration.asm_symbol
                self.global_scalars[name] = declaration
            elif isinstance(declaration, ArrayDecl):
                if declaration.type_name not in ("char", "int", "uint8_t") and not declaration.type_name.startswith("struct "):
                    message = f"global array '{name}' must have element type 'char', 'int', 'uint8_t', or a struct type"
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
                self.global_arrays[name] = declaration
            else:
                message = f"unexpected top-level declaration: {type(declaration).__name__}"
                raise CompileError(message, line=declaration.line)

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

    def allocate_local(self, name: str, /, *, size: int | None = None) -> int:
        """Allocate a local variable on the stack frame.

        Args:
            name: local variable name.
            size: slot size in bytes.  Defaults to the target's native
                integer width (2 on 16-bit real mode, 4 on 32-bit flat
                pmode) so plain ``int`` / pointer locals pick up the
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
        pool = (*self.target.register_pool, self.target.base_register) if self.elide_frame else self.target.register_pool
        clobber_counts: dict[str, int] = dict.fromkeys(pool, 0)

        function_pointer_vars: set[str] = set()

        def collect_function_pointer_vars(stmts: list[Node]) -> None:
            for stmt in stmts:
                if isinstance(stmt, VarDecl) and stmt.type_name == "function_pointer":
                    function_pointer_vars.add(stmt.name)
                elif isinstance(stmt, If):
                    collect_function_pointer_vars(stmt.body)
                    if stmt.else_body:
                        collect_function_pointer_vars(stmt.else_body)
                elif isinstance(stmt, (DoWhile, While)):
                    collect_function_pointer_vars(stmt.body)

        collect_function_pointer_vars(body)

        def visit(node: Node) -> None:
            if isinstance(node, Call):
                if node.name in self.user_functions or node.name in function_pointer_vars:
                    # User functions and function_pointer indirect calls follow the standard
                    # cdecl prologue (``push bp / mov bp, sp / … / pop bp``) which
                    # preserves the caller's BP, so BP is omitted from the
                    # user-call clobber set even when it's pinned.
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

    def emit_accumulator_zx_from_al(self) -> None:
        """Zero-extend AL (byte result) to the target accumulator.

        16-bit real mode: ``xor ah, ah`` — clears AH, leaving AX = AL.
        32-bit flat pmode: ``movzx eax, al`` — clears bits 8-31,
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
        self.emit(f"        mov {self.target.di_register}, ARGV")
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
                self.emit(f"        cmp {self.target.count_register}, {expected}")
                self.emit(f"        mov {self.target.si_register}, {die_label}")
                self.emit(f"        mov {self.target.count_register}, {die_length}")
                self.emit("        jne FUNCTION_DIE")
                fused_argc = True
                body = body[1:]

        if argc_name and not fused_argc:
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
        and can fuse through.  On 32-bit flat pmode, emits ``movzx eax,
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
        # Skip type validation for IR-generated conditions — the AST was
        # already validated by the parser before IR construction.
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
            self.emit("        jc FUNCTION_DIE")
            return
        if fuse_exit:
            self.emit("        jnc FUNCTION_EXIT")
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
                stride = (
                    self._type_size(statement.type_name)
                    if statement.type_name.startswith("struct ")
                    else (1 if statement.type_name in self.BYTE_TYPES else self.target.int_size)
                )
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
