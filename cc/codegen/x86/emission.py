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

from dataclasses import fields

from cc import ir
from cc.ast_nodes import (
    AddressOf,
    ArrayDecl,
    Assign,
    BinaryOperation,
    Break,
    Call,
    Cast,
    Char,
    Compound,
    Conditional,
    Continue,
    DerefAssign,
    DoubleIndex,
    DoWhile,
    Function,
    Goto,
    If,
    Index,
    IndexAssign,
    IndexMemberAccess,
    IndexMemberAssign,
    IndexMemberIndex,
    IndexMemberIndexAssign,
    InlineAsm,
    Int,
    Label,
    LogicalAnd,
    LogicalOr,
    MemberAccess,
    MemberAddressOf,
    MemberAssign,
    MemberIndex,
    Node,
    PointerDereference,
    PointerDereferenceAssign,
    Return,
    SizeofType,
    SizeofVar,
    String,
    StructInitializer,
    Switch,
    SwitchCase,
    TailCall,
    Var,
    VarDecl,
    While,
)
from cc.codegen.x86.jumps import JUMP_WHEN_FALSE, JUMP_WHEN_FALSE_UNSIGNED, JUMP_WHEN_TRUE, JUMP_WHEN_TRUE_UNSIGNED
from cc.codegen.x86.peephole import Peepholer
from cc.errors import CompileError
from cc.target import X86CodegenTarget16
from cc.tokens import COMPARISON_OPERATIONS
from cc.utils import decode_string_escapes, string_byte_length

#: Pointer types whose pointee is a 4-byte unsigned integer.  On the
#: 16-bit target ``unsigned long`` and ``uint32_t`` are the same type
#: (both 4-byte unsigned); on the 32-bit target they are also both
#: 4-byte, and the special-case load path is harmless there.  Either
#: spelling must route through :meth:`generate_long_expression` so the
#: full DX:AX (16-bit) / EAX (32-bit) value is loaded — otherwise the
#: ``uint32_t *`` form silently reads only the low 16 bits on the
#: 16-bit target.
_LONG_POINTER_TYPES = frozenset({"uint32_t*", "unsigned long*"})


class EmissionMixin:
    """Emission dispatchers, mixed into :class:`X86CodeGenerator`.

    The mixin expects the mixing class to provide the arch-agnostic
    state and helpers from :class:`cc.codegen.base.CodeGeneratorBase`
    (``self.lines``, ``self.emit``, ``self.target``, symbol tables,
    frame state) plus the x86-specific ``emit_*`` helpers (``emit_*``
    methods that still live on the generator class) and the
    ``builtin_*`` / ``peephole`` dispatchers from sibling mixins.
    """

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

    def _emit_pointer_dereference(self, expression: PointerDereference) -> None:
        """Read through a pointer expression at ``target_type`` width.

        Codegen: evaluate ``expression.expression`` into the accumulator
        (an address), then load through it.  Width is chosen by
        ``target_type``: ``uint8_t`` → byte load with zero-extension;
        anything else → full int_size load.

        Shortcut: when the inner expression is ``AddressOf(Var)`` of a
        local, fold the ``lea + load`` pair into a single frame-relative
        load.  This is the hot path for the port-IO bridge idiom
        ``*(uint8_t *)&local_struct``.
        """
        inner = expression.expression
        if isinstance(inner, AddressOf) and inner.var.name in self.locals:
            address = f"[{self._local_address(inner.var.name)}]"
            if expression.target_type == "uint8_t":
                self.emit_byte_load_zx(address)
            elif expression.target_type == "uint16_t" and self.target.int_size > 2:
                self.emit(f"        movzx {self.target.acc}, word {address}")
            else:
                self.emit(f"        mov {self.target.acc}, {address}")
            self.ax_clear()
            return
        self.generate_expression(inner)
        address_register = self.target.acc
        if expression.target_type == "uint8_t":
            self.emit(f"        mov al, [{address_register}]")
            self.emit_accumulator_zx_from_al()
        elif expression.target_type == "uint16_t" and self.target.int_size > 2:
            self.emit(f"        movzx {address_register}, word [{address_register}]")
        else:
            self.emit(f"        mov {address_register}, [{address_register}]")
        self.ax_clear()

    def _emit_pointer_dereference_assign(self, statement: PointerDereferenceAssign) -> None:
        """Write ``value`` through ``*(target_type *)address``.

        Evaluates the RHS into the accumulator, then the address
        expression into the SI scratch register, and stores at
        ``target_type`` width.  Shortcut: when ``address`` is
        ``AddressOf(Var)`` of a local, fold the store directly to
        ``[ebp-N]`` (no lea / scratch register).  This is the hot path
        for the port-IO bridge idiom ``*(uint8_t *)&local = inb(...);``.
        """
        self.generate_expression(statement.value)
        accumulator = self.target.acc
        if isinstance(statement.address, AddressOf) and statement.address.var.name in self.locals:
            destination = f"[{self._local_address(statement.address.var.name)}]"
            if statement.target_type == "uint8_t":
                self.emit(f"        mov {destination}, {self.target.low_byte(accumulator)}")
            elif statement.target_type == "uint16_t" and self.target.int_size > 2:
                self.emit(f"        mov word {destination}, {self.target.low_word(accumulator)}")
            else:
                self.emit(f"        mov {destination}, {accumulator}")
            return
        # General path: stash the value, evaluate address into SI, store.
        scratch = self.target.si_register
        self.emit(f"        push {accumulator}")
        self.generate_expression(statement.address)
        self.emit(f"        mov {scratch}, {accumulator}")
        self.emit(f"        pop {accumulator}")
        if statement.target_type == "uint8_t":
            self.emit(f"        mov [{scratch}], {self.target.low_byte(accumulator)}")
        elif statement.target_type == "uint16_t" and self.target.int_size > 2:
            self.emit(f"        mov word [{scratch}], {self.target.low_word(accumulator)}")
        else:
            self.emit(f"        mov [{scratch}], {accumulator}")

    def _emit_scale_index(self, register: str, /, *, scale: int) -> None:
        """Multiply *register* by *scale* (1, 2, or 4) in place.

        Scale 1 is a no-op (byte stride); 2 emits ``add reg, reg``; 4
        emits ``shl reg, 2``.  Other widths fall back to ``imul``.
        """
        if scale == 1:
            return
        if scale == 2:
            self.emit(f"        add {register}, {register}")
        elif scale == 4:
            self.emit(f"        shl {register}, 2")
        else:
            self.emit(f"        imul {register}, {register}, {scale}")

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

        Expects the designated form (``init.designated`` populated); the
        positional form is for array element initializers and is handled by
        the global-array path in the generator.
        """
        if init.designated is None:
            message = f"positional struct initializer on local '{name}' is not supported"
            raise CompileError(message, line=init.line)
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
        # Per-field designated assignments via the existing member-assign
        # codegen path.  Synthesize MemberAssign nodes and dispatch.
        for field_name, value_node in init.designated.items():
            synthetic = MemberAssign(
                arrow=False,
                expr=value_node,
                line=init.line,
                member_name=field_name,
                object_name=name,
            )
            self.generate_member_assign(synthetic)

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
                self.libbboeos_extern_declarations.add(function.name)
                continue
            self.user_functions[function.name] = len(function.params)
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

        # Build IR for all non-main, non-always-inline functions.  The IR
        # is consumed by generate_function; main keeps the AST path because
        # its special handling (argc/argv startup, printf fusion, frame-
        # elide data labels) is deeply tied to the AST shape.
        ir_program = ir.Builder(carry_return_functions=frozenset(self.carry_return_functions)).build_program(ast)
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
                    self._emit_vdso_jmp("FUNCTION_DIE")
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
        if isinstance(node, (Int, String, Var, SizeofType, SizeofVar, AddressOf, MemberAddressOf)):
            return True
        if isinstance(node, BinaryOperation):
            return self._is_pure_expression(node.left) and self._is_pure_expression(node.right)
        if isinstance(node, (LogicalAnd, LogicalOr)):
            return self._is_pure_expression(node.left) and self._is_pure_expression(node.right)
        if isinstance(node, Index):
            # ``arr[i]`` reads from memory but doesn't write; the index
            # itself must also be pure.
            return self._is_pure_expression(node.index)
        if isinstance(node, (MemberAccess, MemberIndex, IndexMemberAccess, IndexMemberIndex)):
            return True
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
            function_pointer_in_regs = self.function_pointer_in_registers.get(name, {})
            if len(arguments) != len(function_pointer_in_regs):
                message = f"function_pointer '{name}' expects {len(function_pointer_in_regs)} argument(s), got {len(arguments)}"
                raise CompileError(message, line=statement.line)
            clobbers: frozenset[str] = frozenset(self.target.register_pool)
            saved = self._pinned_registers_to_save(clobbers)
            use_pusha = discard_return and len(saved) >= 3
            if use_pusha:
                self.emit("        pusha")
            else:
                for register in saved:
                    self.emit(f"        push {register}")
            if function_pointer_in_regs:
                register_args = [(function_pointer_in_regs[i], arg) for i, arg in enumerate(arguments)]
                self._emit_register_arg_moves(register_args)
            self._emit_load_var(name, register=self.target.acc)
            self.emit(f"        call {self.target.acc}")
            if use_pusha:
                self.emit("        popa")
            else:
                for register in reversed(saved):
                    self.emit(f"        pop {register}")
            self.ax_clear()
            return
        if name in self.user_functions:
            expected = self.user_functions[name]
            if len(arguments) != expected:
                message = f"{name}() expects exactly {expected} argument{'s' if expected != 1 else ''}"
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
                if isinstance(capture_arg, AddressOf) and capture_arg.var.name in self.pinned_register:
                    captured_pinned_registers.add(self.pinned_register[capture_arg.var.name])
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
                if not isinstance(arg, AddressOf):
                    message = "out_register argument must be an address-of expression (&var)"
                    raise CompileError(message, line=statement.line)
                dest_name = arg.var.name
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
                dest_name = arg.var.name
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
            # via `#include "string.h"`).  Emit a cdecl indirect call
            # through the pointer table — args pushed right-to-left,
            # `call [FUNCTION_<NAME>_PTR]`, caller pops args.
            if name in self.libbboeos_extern_declarations:
                pointer_constant = f"FUNCTION_{name.upper()}_PTR"
                clobbers: frozenset[str] = frozenset(self.target.register_pool)
                saved = self._pinned_registers_to_save(clobbers)
                use_pusha = discard_return and len(saved) >= 3
                if use_pusha:
                    self.emit("        pusha")
                else:
                    for register in saved:
                        self.emit(f"        push {register}")
                for arg in reversed(arguments):
                    self._emit_push_arg(arg)
                self.emit(f"        call [{pointer_constant}]")
                if arguments:
                    self.emit(f"        add {self.target.stack_register}, {len(arguments) * self.target.int_size}")
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
                # ``_g_<name>`` label.  Load it as an immediate, not as a
                # memory fetch from that address.
                self.emit(f"        mov {self.target.acc}, _g_{vname}")
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
        elif isinstance(expression, Index):
            self.ax_clear()
            vname = expression.array.name
            index_expression = expression.index
            self._check_defined(vname, line=expression.line)
            # Pointee / element width selects the load encoding.  For
            # ``uint16_t *p`` on the 32-bit target ``mov eax, [esi]``
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
                    self.ax_clear()
                else:
                    guarded = self._si_scratch_guard_begin(vname)
                    self._emit_load_var(vname, register=self.target.si_register)
                    si = self.target.si_register
                    # Index scaling: ``p[i]`` advances by sizeof(*p)
                    # bytes per ``i``, so a narrow pointee (uint16_t* on
                    # the 32-bit target) needs scale=2 not the acc's 4.
                    if narrow_word:
                        scale_size = pointee_size
                    elif is_byte:
                        scale_size = 1
                    else:
                        scale_size = self.target.int_size

                    def _scale(register: str, /) -> None:
                        if scale_size == 1:
                            return
                        if scale_size == 2:
                            emitter.emit(f"        add {register}, {register}")
                        elif scale_size == 4:
                            emitter.emit(f"        shl {register}, 2")
                        else:
                            emitter.emit(f"        imul {register}, {register}, {scale_size}")

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
                    # AX now holds the subscript result, not the index —
                    # invalidate the tracking that generate_expression set.
                    self.ax_clear()
        elif isinstance(expression, DoubleIndex):
            self.ax_clear()
            vname = expression.array.name
            self._check_defined(vname, line=expression.line)
            # Stage 1: load name[outer_index] into AX via the existing
            # Index path (handles constant-base, pinned-SI, byte vs word
            # widths uniformly).  For ``char *foo[N]`` this is a 4-byte
            # pointer load; for ``int *foo[N]`` likewise.
            outer_load = Index(array=expression.array, index=expression.outer_index, line=expression.line)
            self.generate_expression(outer_load)
            # Stage 2: index into the pointer in AX.  Element width is
            # the pointee of the outer-array's element type — i.e.
            # ``sizeof(*element)``.  For ``char *arr[N]`` the element
            # type is ``"char*"`` so the inner stride is
            # ``sizeof(char) == 1``.  ``_index_pointee_size`` is
            # array-aware (returns sizeof(element)), so for DoubleIndex
            # we strip the recorded element type's trailing ``*`` and
            # consult ``target.type_size`` directly.
            si = self.target.si_register
            self.emit(f"        mov {si}, {self.target.acc}")
            element_type = self.variable_types.get(vname, "")
            if element_type.endswith("*"):
                pointee = element_type[:-1].rstrip()
                try:
                    inner_size = self.target.type_size(pointee)
                except KeyError:
                    inner_size = self.target.int_size
            else:
                inner_size = self.target.int_size
            is_byte_inner = inner_size == 1
            inner = expression.inner_index
            if isinstance(inner, Int):
                offset = inner.value * (1 if is_byte_inner else inner_size)
                mem = f"{si}+{offset}" if offset else si
                if is_byte_inner:
                    self.emit_byte_load_zx(f"[{mem}]")
                else:
                    self.emit(f"        mov {self.target.acc}, [{mem}]")
            elif isinstance(inner, Var):
                # Var load doesn't touch SI, so no push/pop round-trip.
                self.generate_expression(inner)
                if not is_byte_inner:
                    if inner_size == self.target.int_size:
                        self._emit_scale_int_index(self.target.acc)
                    else:
                        self.emit(f"        imul {self.target.acc}, {self.target.acc}, {inner_size}")
                self.emit(f"        add {si}, {self.target.acc}")
                if is_byte_inner:
                    self.emit_byte_load_zx(f"[{si}]")
                else:
                    self.emit(f"        mov {self.target.acc}, [{si}]")
            else:
                # General inner expression — preserve SI across evaluation.
                self.emit(f"        push {si}")
                self.generate_expression(inner)
                if not is_byte_inner:
                    if inner_size == self.target.int_size:
                        self._emit_scale_int_index(self.target.acc)
                    else:
                        self.emit(f"        imul {self.target.acc}, {self.target.acc}, {inner_size}")
                self.emit(f"        pop {si}")
                self.emit(f"        add {si}, {self.target.acc}")
                if is_byte_inner:
                    self.emit_byte_load_zx(f"[{si}]")
                else:
                    self.emit(f"        mov {self.target.acc}, [{si}]")
            self.ax_clear()
        elif isinstance(expression, SizeofType):
            self.ax_clear()
            self.emit(f"        mov {self.target.acc}, {self._type_size(expression.type_name)}")
        elif isinstance(expression, SizeofVar):
            self.ax_clear()
            vname = expression.name
            if vname in self.global_arrays:
                declaration = self.global_arrays[vname]
                stride = 1 if declaration.type_name in self.BYTE_TYPES else self.target.int_size
                if declaration.init is not None:
                    size = len(declaration.init.elements) * stride
                    self.emit(f"        mov {self.target.acc}, {size}")
                else:
                    size_expression = self._constant_expression(declaration.size)
                    self.emit(f"        mov {self.target.acc}, ({size_expression})*{stride}")
            elif vname in self.array_sizes:
                size = self.array_sizes[vname] * self.target.int_size  # word-sized elements
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
            # Pointer arithmetic: scale the right operand by the element size when
            # the left side is a pointer or array variable.  ptr + N → ptr + N*sizeof(*ptr).
            # For byte pointers (char*, uint8_t*) element_size is 1 so nothing changes.
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
        elif isinstance(expression, AddressOf):
            name = expression.var.name
            if name in self.out_register_locals:
                message = f"cannot take address of out_register parameter '{name}'"
                raise CompileError(message, line=expression.line)
            addr = self._local_address(name)
            if name in self.locals:
                self.emit(f"        lea {self.target.acc}, [{addr}]")
            else:
                self.emit(f"        mov {self.target.acc}, {addr}")
            self.ax_local = None
            self.ax_is_byte = False
        elif isinstance(expression, MemberAccess):
            self.generate_member_access(expression)
        elif isinstance(expression, MemberAddressOf):
            self.generate_member_address_of(expression)
        elif isinstance(expression, MemberIndex):
            self.generate_member_index(expression)
        elif isinstance(expression, IndexMemberAccess):
            self.generate_index_member_access(expression)
        elif isinstance(expression, IndexMemberIndex):
            self.generate_index_member_index(expression)
        elif isinstance(expression, Conditional):
            self._generate_conditional(expression)
        elif isinstance(expression, (LogicalAnd, LogicalOr)):
            self._generate_logical_value(expression)
        elif isinstance(expression, Cast):
            # Identity codegen: evaluate the inner expression; the target type
            # is tracked in the AST node but cc.py's loose type system treats
            # all register-sized values uniformly so no truncation is emitted.
            self.generate_expression(expression.expression)
        elif isinstance(expression, PointerDereference):
            self._emit_pointer_dereference(expression)
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
        if isinstance(value, AddressOf):
            return value
        if value.startswith("_ir_s"):
            content = self._ir_string_map.get(value)
            if content is not None:
                return String(content=content)
        return Var(name=value)

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

    def _node_contains_var(self, node: Node, name: str, /) -> bool:
        """Return True if node or any descendant is Var(name).

        Conservative: any str field equal to name is treated as a possible
        variable read so that nodes like Assign, MemberAssign, and the
        IndexMember* family (which still store the target's name as a plain
        str rather than a Var) are not silently missed.
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
            caller_push_index = 0
            for i, param in enumerate(parameters):
                self.variable_types[param.name] = param.type
                if param.is_array:
                    self.variable_arrays.add(param.name)
                if param.out_register is not None:
                    # Output-only register param: no caller-pushed stack slot.
                    # Track it so DerefAssign in the body emits mov <reg>, <val>.
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

        self.discover_virtual_long_locals(body)
        self.safe_pin_registers = self.compute_safe_pin_registers(body)
        # Exclude regparm params from auto-pin candidates — they're spilled
        # to the stack at prologue entry and the body accesses them through
        # those slots like any other local.
        if name == "main":
            param_candidates = []
        elif is_fastcall:
            param_candidates = [p for p in parameters[regparm_count:] if p.out_register is None and p.in_register is None]
        else:
            param_candidates = [p for p in parameters if p.out_register is None and p.in_register is None]
        self.auto_pin_candidates = self._select_auto_pin_candidates(body=body, parameters=param_candidates)

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

        if self.object_mode:
            self.emit(f"global {name}")
        self.emit(f"{name}:")
        if not self.elide_frame:
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
                    # Byte-typed parameters (``char`` / ``uint8_t``) treat
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
                            raise CompileError(message, line=function.line)
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
            # Implicit fall-off end of main: default the exit code to 0
            # so chained shells (`cmd && next`) behave as expected.
            # An explicit `return N;` earlier in the body has already
            # set EAX via generate_return; reaching this point means
            # control fell off without one, hence the zero default.
            self.emit(f"        xor {self.target.acc}, {self.target.acc}")
            self._emit_vdso_jmp("FUNCTION_EXIT")
            if self.elide_frame:
                # Plain int / pointer locals get the target's native
                # integer width (``dw`` / ``dd``); ``unsigned long``
                # always stays 4 bytes (``dd``) regardless of mode;
                # byte-scalar locals always stay 1 byte (``db``);
                # local stack arrays reserve their full byte count.
                #
                # In flat mode these cells are emitted inline at the
                # tail of the function (zeros sit in .text under
                # ``org 08048000h`` and the program loader skips them).
                # In object mode they instead get collected into
                # ``self.elided_local_bss_vars`` and laid down later in
                # ``section .bss`` via ``resb`` reservations, so the
                # .text section stays code-only and the linker can
                # pack .text from multiple objects without dragging
                # zero pads between them.
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
        # halfword (``uint16_t``) targets get a 2-byte store instead of
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
                guarded = self._si_scratch_guard_begin(name)
                self.generate_expression(statement.expr)
                self.emit(f"        push {self.target.acc}")
                self._emit_load_var(name, register=self.target.si_register)
                si = self.target.si_register
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
            if isinstance(base, Var) and self.variable_types.get(base.name) in _LONG_POINTER_TYPES:
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
                self.emit(f"        mov {self.target.acc}, [{self.target.base_register}-{low_offset}]")
                if isinstance(self.target, X86CodegenTarget16):
                    self.emit(f"        mov {self.target.dx_register}, [{self.target.base_register}-{low_offset - 2}]")
            self.ax_is_byte = False
            self.ax_local = None
            return
        message = f"unsupported 'unsigned long' expression: {type(expression).__name__}"
        raise CompileError(message, line=expression.line)

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
            self._emit_vdso_jmp("FUNCTION_EXIT")
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
                and self.variable_types.get(statement.value.array.name) in _LONG_POINTER_TYPES
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
        if isinstance(statement, VarDecl):
            self.visible_vars.add(statement.name)
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
        elif isinstance(statement, Compound):
            self.generate_body(statement.body, scoped=True)
        elif isinstance(statement, Continue):
            if not self.loop_continue_labels:
                message = "continue outside of a loop"
                raise CompileError(message, line=statement.line)
            self.emit(f"        jmp {self.loop_continue_labels[-1]}")
        elif isinstance(statement, Goto):
            self.user_labels_referenced.setdefault(statement.name, statement.line)
            self.emit(f"        jmp .user_{statement.name}")
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
        elif isinstance(statement, DoWhile):
            self.ax_clear()
            self.generate_do_while(statement)
        elif isinstance(statement, If):
            self.generate_if(statement)
        elif isinstance(statement, Switch):
            self.ax_clear()
            self.generate_switch(statement)
        elif isinstance(statement, While):
            self.ax_clear()
            self.generate_while(statement)
        elif isinstance(statement, Return):
            self.generate_return(statement)
        elif isinstance(statement, Call):
            self.generate_call(statement, discard_return=True)
            self.ax_clear()
        elif isinstance(statement, DerefAssign):
            if statement.pointer.name in self.out_register_locals:
                reg = self.out_register_locals[statement.pointer.name]
                self.generate_expression(statement.expr)
                # Source defaults to the accumulator; if the out_register
                # target is narrower (e.g. 16-bit ``dx`` against 32-bit
                # ``eax``) take the matching low-width alias so NASM doesn't
                # reject the size-mismatched ``mov dx, eax``.
                source = self.target.acc
                if len(reg) < len(source):
                    source = self.target.low_word(source)
                if reg != source:
                    self.emit(f"        mov {reg}, {source}")
                self.ax_clear()
            else:
                # Generic ``*holder = expr`` where *holder* is a plain
                # pointer local/param.  Pointed-to width comes from
                # stripping one ``*`` off holder's declared type — e.g.
                # ``char**`` writes a 4-byte ``char*`` slot; ``char*``
                # writes a 1-byte ``char``.
                holder_type = self.variable_types.get(statement.pointer.name)
                if not holder_type or not holder_type.endswith("*"):
                    message = f"pointer dereference write to non-pointer variable '{statement.pointer.name}'"
                    raise CompileError(message, line=statement.line)
                pointee_type = holder_type[:-1]
                self.generate_expression(statement.expr)
                self._emit_load_var(statement.pointer.name, register=self.target.si_register)
                if pointee_type in ("char", "uint8_t"):
                    self.emit(f"        mov [{self.target.si_register}], {self.target.low_byte(self.target.acc)}")
                else:
                    self.emit(f"        mov [{self.target.si_register}], {self.target.acc}")
                self.ax_clear()
        elif isinstance(statement, PointerDereferenceAssign):
            self._emit_pointer_dereference_assign(statement)
            self.ax_clear()
        elif isinstance(statement, MemberAssign):
            self.generate_member_assign(statement)
            self.ax_clear()
        elif isinstance(statement, IndexMemberAssign):
            self.generate_index_member_assign(statement)
            self.ax_clear()
        elif isinstance(statement, IndexMemberIndexAssign):
            self.generate_index_member_index_assign(statement)
            self.ax_clear()
        elif isinstance(statement, TailCall):
            self.generate_tail_call(statement)
        elif isinstance(statement, InlineAsm):
            # Empty / inline-asm statement (produced by ``(void)expr;``
            # discard sites and any future statement-level asm escape).
            # Splits on ``\n`` so multi-line content emits one ``emit``
            # per line; empty content emits nothing.
            for line in decode_string_escapes(statement.content).splitlines():
                self.emit(line)
        else:
            message = f"unknown statement: {type(statement).__name__}"
            raise CompileError(message, line=statement.line)

    def generate_switch(self, statement: Switch, /) -> None:
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
        """
        line = statement.line
        # Determine the discriminant's enum tag, if any, for the
        # exhaustiveness check.  Only ``Var`` discriminants whose type
        # was declared ``enum NAME`` qualify; arbitrary integer
        # expressions (calls returning int, arithmetic, etc.) are
        # treated as plain int — no check fires.
        enum_tag: str | None = None
        if isinstance(statement.discriminant, Var):
            discriminant_type = self.variable_types.get(statement.discriminant.name)
            if discriminant_type is not None and discriminant_type.startswith("enum "):
                enum_tag = discriminant_type[5:]
        default_case: SwitchCase | None = None
        case_arms: list[SwitchCase] = []
        for case in statement.cases:
            if case.value is None:
                default_case = case
            else:
                case_arms.append(case)
        if enum_tag is not None and default_case is None:
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
        # Build the compare/jump chain via the existing condition
        # machinery: each ``case CONST:`` is lowered as a synthetic
        # ``discriminant == CONST`` true-jump.  Going through
        # :meth:`emit_condition_true_jump` reuses the byte-vs-word /
        # pinned-register / constant-alias handling already in place,
        # and crucially re-emits the discriminant load before each
        # compare so the per-arm load isn't elided by a peephole pass
        # that assumes the accumulator is dead after the first ``je``.
        label_index = self.new_label()
        end_label = f".switch_{label_index}_end"
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
            groups: list[list[SwitchCase]] = []
            current: list[SwitchCase] = []
            for case in case_arms:
                current.append(case)
                if case.body:
                    groups.append(current)
                    current = []
            self.loop_end_labels.append(end_label)
            try:
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
                    self.generate_body(terminal.body, scoped=True)
                    self.emit(f"{next_label}:")
                if default_case is not None:
                    self.ax_clear()
                    self.generate_body(default_case.body, scoped=True)
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
            for case, arm_label in zip(case_arms, case_labels, strict=True):
                self.emit(f"{arm_label}:")
                self.ax_clear()
                self.generate_body(case.body, scoped=True)
            if default_case is not None:
                self.emit(f"{default_label}:")
                self.ax_clear()
                self.generate_body(default_case.body, scoped=True)
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
