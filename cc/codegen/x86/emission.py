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
and the other x86 mixins (``BuiltinsMixin`` for ``builtin_*``
dispatch, ``PeepholeMixin`` for the ``peephole()`` pass), so
composition order in ``X86CodeGenerator`` isn't load-bearing.
"""

from __future__ import annotations

from cc import ir
from cc.ast_nodes import (
    ArrayDecl,
    Assign,
    BinaryOperation,
    Break,
    Call,
    Continue,
    DoWhile,
    Function,
    If,
    Index,
    IndexAssign,
    InlineAsm,
    Int,
    Node,
    Return,
    SizeofType,
    SizeofVar,
    String,
    Var,
    VarDecl,
    While,
)
from cc.codegen.x86.jumps import JUMP_INVERT, JUMP_WHEN_FALSE
from cc.errors import CompileError
from cc.target import X86CodegenTarget16
from cc.utils import decode_string_escapes, string_byte_length


class EmissionMixin:
    """Emission dispatchers, mixed into :class:`X86CodeGenerator`.

    The mixin expects the mixing class to provide the arch-agnostic
    state and helpers from :class:`cc.codegen.base.CodeGeneratorBase`
    (``self.lines``, ``self.emit``, ``self.target``, symbol tables,
    frame state) plus the x86-specific ``emit_*`` helpers (``emit_*``
    methods that still live on the generator class) and the
    ``builtin_*`` / ``peephole`` dispatchers from sibling mixins.
    """

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
        self._emit_bss_trailer()
        # Sentinel label at the very end so inline asm can address the
        # first byte past the loaded image (scratch buffers, heap bases,
        # etc.).  Zero bytes, so it does not affect programs that ignore
        # it.
        self.emit("_program_end:")
        # BSS EQUs and _bss_end come *after* _program_end: so they are
        # never forward references — the self-hosted assembler cannot
        # resolve forward EQU references.
        self._emit_bss_equs()
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
                    self.emit(f"        mov {self.target.si_register}, {die_label}")
                    self.emit(f"        mov {self.target.count_register}, {die_length}")
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
                self.emit(f"        add {self.target.sp_register}, {len(stack_args) * self.target.int_size}")
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
                self.emit_byte_load_zx(f"[{self._local_address(vname)}]")
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
                        self.emit_byte_load_zx(f"[{addr}]")
                    else:
                        self.emit(f"        mov {self.target.acc}, [{addr}]")
                else:
                    guarded = self._si_scratch_guard_begin(vname)
                    self._emit_load_var(vname, register=self.target.si_register)
                    si = self.target.si_register
                    if is_byte:
                        mem = f"[{si}+{offset}]" if offset else f"[{si}]"
                        self.emit_byte_load_zx(mem)
                    elif offset:
                        self.emit(f"        mov {self.target.acc}, [{si}+{offset}]")
                    else:
                        self.emit(f"        mov {self.target.acc}, [{si}]")
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
                        self.emit_byte_load_zx(f"[{addr}]")
                    else:
                        self.emit(f"        mov {self.target.acc}, [{addr}]")
                    self.ax_clear()
                else:
                    guarded = self._si_scratch_guard_begin(vname)
                    self._emit_load_var(vname, register=self.target.si_register)
                    si = self.target.si_register
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
                            self.emit(f"        add {self.target.acc}, {self.target.acc}")
                        self.emit(f"        add {si}, {self.target.acc}")
                    else:
                        self.emit(f"        push {si}")
                        self.generate_expression(index_expression)
                        if not is_byte:
                            self.emit(f"        add {self.target.acc}, {self.target.acc}")
                        self.emit(f"        pop {si}")
                        self.emit(f"        add {si}, {self.target.acc}")
                    if is_byte:
                        self.emit_byte_load_zx(f"[{si}]")
                    else:
                        self.emit(f"        mov {self.target.acc}, [{si}]")
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
                self._emit_load_var(name, register=self.target.si_register)
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
                self._emit_load_var(name, register=self.target.si_register)
                si = self.target.si_register
                addr = f"{si}+{offset}" if offset else si
            if is_byte:
                self.emit(f"        mov [{addr}], al")
            else:
                self.emit(f"        mov [{addr}], {self.target.acc}")
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
                    self.emit(f"        mov [{addr}], {self.target.acc}")
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
                    if not is_byte:
                        self.emit(f"        add {self.target.acc}, {self.target.acc}")
                    self.emit(f"        add {si}, {self.target.acc}")
                else:
                    self.emit(f"        push {si}")
                    self.generate_expression(statement.index)
                    if not is_byte:
                        self.emit(f"        add {self.target.acc}, {self.target.acc}")
                    self.emit(f"        pop {si}")
                    self.emit(f"        add {si}, {self.target.acc}")
                self.emit(f"        pop {self.target.acc}")
                # After pop, AX holds the value being stored, not the index —
                # invalidate the ax_local tracking that generate_expression set.
                self.ax_clear()
                if is_byte:
                    self.emit(f"        mov [{si}], al")
                else:
                    self.emit(f"        mov [{si}], {self.target.acc}")
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
