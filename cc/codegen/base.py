"""Architecture-agnostic code-generator scaffolding.

:class:`CodeGeneratorBase` carries the AST/IR predicates and shared
plumbing every backend needs — liveness, aliasing, simple-arg
classification, always-exit detection, argument-count validation,
constant-expression folding, byte/memory-scalar classification,
comparison-type inference, output-buffer emit, label allocation,
string-literal dedup, ``%include`` tracking, ``printf`` → ``die``
fusion — with no x86-specific assumptions.  Backends subclass it to
add instruction selection, register pool management, peephole passes,
and the concrete emit helpers.

Instance methods assume the subclass has populated the state they
read (``self.locals``, ``self.variable_types``, ``self.constant_aliases``,
``self.NAMED_CONSTANTS``, ``self.BYTE_TYPES``, ``self.lines``,
``self.strings``, …).  Those still live on the subclass for now; a
follow-up pass can move the arch-agnostic subset up here, along with
an ``__init__`` that initializes them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from cc import ir
from cc.ast_nodes import (
    ArrayDecl,
    Assign,
    BinaryOperation,
    Break,
    Call,
    Char,
    Continue,
    If,
    Index,
    Int,
    LogicalAnd,
    LogicalOr,
    Node,
    Return,
    SizeofType,
    SizeofVar,
    String,
    Var,
    VarDecl,
)
from cc.errors import CompileError
from cc.utils import ast_contains

if TYPE_CHECKING:
    from cc.target import CodegenTarget


class CodeGeneratorBase:
    """Architecture-agnostic base for every backend code generator.

    Holds predicates that query AST / IR shape and validate caller
    arguments, plus the output buffer, label / string counters,
    frame state, symbol tables, and BBoeOS-level constant tables.
    Backends (e.g. :class:`cc.codegen.x86.X86CodeGenerator`) subclass
    this and layer on the emit / peephole / instruction-selection
    logic specific to their ISA.
    """

    #: Identifiers that resolve to NASM kernel constants rather than
    #: user-defined variables.  Emitted verbatim so NASM can resolve
    #: them from ``constants.asm``.  BBoeOS-specific but arch-agnostic:
    #: any backend running on BBoeOS needs them.
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
        "_bss_end",
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
        "STR_ALIGN",
        "STR_ASSIGN",
        "STR_BITS",
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

    #: Byte-element type names.  ``uint8_t`` shares the ``char``
    #: codegen path (byte array stride, byte-wide load with zero
    #: extend) but is classified as ``integer`` for comparison
    #: type-checking, so ``uint8_t b; if (b == 0x45)`` works without
    #: pretending the literal is a character.
    BYTE_TYPES: ClassVar[frozenset[str]] = frozenset({"char", "uint8_t"})
    BYTE_SCALAR_TYPES: ClassVar[frozenset[str]] = frozenset({"char", "char*", "uint8_t", "uint8_t*"})

    def __init__(self, *, target: CodegenTarget, defines: dict[str, str] | None = None) -> None:
        """Initialize arch-agnostic code generator state.

        *target* is the :class:`cc.target.CodegenTarget` instance the
        subclass selected (x86-16, x86-32, or a future backend).
        *defines* is the ``#define`` table from the preprocessor; each
        entry re-emits as a NASM ``%define NAME VALUE`` at the top of
        the output so inline-asm strings can reference C macro names
        directly.

        Subclasses override ``__init__`` to pick their target, call
        ``super().__init__(target=..., defines=defines)``, and then
        initialize any arch-specific state (register trackers, pinned
        aliases, etc.).
        """
        self.target: CodegenTarget = target
        self.array_labels: dict[str, str] = {}
        self.array_sizes: dict[str, int] = {}
        self.arrays: list[tuple[str, list[str]]] = []
        self.byte_scalar_locals: set[str] = set()
        self.carry_return_functions: set[str] = set()  # callees that return via CF
        self.constant_aliases: dict[str, str] = {}
        self.defines: dict[str, str] = dict(defines) if defines else {}
        self.elide_frame: bool = False
        self.fastcall_functions: set[str] = set()  # regparm(1) callees: arg 0 in acc
        self.frame_size: int = 0
        self.global_arrays: dict[str, ArrayDecl] = {}
        self.global_byte_arrays: set[str] = set()
        self.global_scalars: dict[str, VarDecl] = {}
        self.inline_bodies: dict[str, str] = {}  # always_inline callees: name → raw asm body
        self.inline_call_counter: int = 0  # per-inline-site label-uniquification suffix
        self.label_id: int = 0
        self.lines: list[str] = []
        self.live_long_local: str | None = None
        self.locals: dict[str, int] = {}
        self.loop_continue_labels: list[str] = []
        self.loop_end_labels: list[str] = []
        self.required_includes: set[str] = set()
        self.strings: list[tuple[str, str]] = []
        self.user_functions: dict[str, int] = {}  # name → param count
        self.variable_arrays: set[str] = set()
        self.variable_types: dict[str, str] = {}
        self.virtual_long_locals: set[str] = set()
        self.visible_vars: set[str] = set()

    @staticmethod
    def _always_exits_ir(body: list[ir.Instruction]) -> bool:
        """Return True if the IR body always ends with a function exit.

        Only :class:`ir.Return` and :class:`ir.Block` wrapping an
        always-exiting AST node count.  :class:`ir.Jump` does *not* —
        a jump transfers control within the IR body (e.g. a ``break``
        to a loop-end label that then falls off the function).  If we
        treated trailing ``Jump`` as "always exits", no epilogue would
        be emitted and a ``break`` in a tail ``while`` would fall into
        whatever function follows.
        """
        for instruction in reversed(body):
            match instruction:
                case ir.Label():
                    continue  # skip trailing labels
                case ir.Return():
                    return True
                case ir.Block(node=node):
                    return CodeGeneratorBase.always_exits([node])
                case _:
                    return False
        return False

    @staticmethod
    def _byte_index_base_key(node: Index, /) -> str:
        """Return a string key identifying the base pointer of an Index.

        Two byte-index nodes share a base when their keys match,
        meaning a single ``mov bx, <base>`` can serve both.
        """
        return node.name

    @staticmethod
    def _check_argument_count(*, arguments: list[Node], expected: int, name: str) -> None:
        """Raise CompileError if the argument count doesn't match expected.

        Derives the line number from the first argument when available,
        so the diagnostic points at the call site even though the
        builtin handlers are invoked with only the argument list.
        """
        line = arguments[0].line if arguments else None
        if expected == 0 and arguments:
            message = f"{name}() takes no arguments"
            raise CompileError(message, line=line)
        if expected > 0 and len(arguments) != expected:
            message = f"{name}() expects exactly {expected} argument{'s' if expected != 1 else ''}"
            raise CompileError(message, line=line)

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

        Returns the variable's name when hoisting it into a register
        would let two or more comparisons collapse to
        ``cmp <reg>, imm``.  Returns ``None`` for unrelated ifs and
        single comparisons (where the hoist would only break even).
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

    @staticmethod
    def _flatten_and(condition: Node, /) -> list[Node]:
        """Flatten a left-leaning ``&&`` tree into a list of leaves."""
        leaves: list[Node] = []
        while isinstance(condition, LogicalAnd):
            leaves.append(condition.right)
            condition = condition.left
        leaves.append(condition)
        leaves.reverse()
        return leaves

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

    @staticmethod
    def _is_constant_true_condition(condition: Node, /) -> bool:
        """Return True if *condition* is statically nonzero.

        ``parse_condition`` wraps bare expressions as ``expr != 0``,
        so ``while (1)`` reaches here as
        ``BinaryOperation(left=Int(value=1), operation="!=", right=Int(value=0))``.
        """
        if not isinstance(condition, BinaryOperation) or condition.operation != "!=":
            return False
        if condition.right != Int(value=0):
            return False
        return isinstance(condition.left, Int) and condition.left.value != 0

    @staticmethod
    def _is_live_after(*, name: str, statements: list[Node]) -> bool:
        """Check if *name* is read before being unconditionally killed.

        Scans *statements* in order.  An unconditional ``Assign`` to
        *name* (whose RHS does not read *name*) kills the old value,
        so any subsequent reads reference the new value, not the one
        from the fuse-die candidate.  Returns False (not live) if
        *name* is never read, or is killed before being read.
        """
        for stmt in statements:
            # Unconditional reassignment kills the old value — but only
            # if the RHS doesn't read the variable (e.g. `err = err + 1`
            # would read the old value).
            if isinstance(stmt, Assign) and stmt.name == name and not CodeGeneratorBase._node_references_var(name=name, node=stmt.expr):
                return False
            if CodeGeneratorBase._node_references_var(name=name, node=stmt):
                return True
        return False

    def _is_memory_scalar(self, name: str, /) -> bool:
        """Return True when *name* is a memory-resident scalar.

        Covers frame-based locals (``self.locals``) and file-scope
        scalars (``self.global_scalars``).  Call sites use this to
        decide whether a ``Var`` can be addressed directly via ``[mem]``
        instead of being loaded into a register first.  Register-
        aliased globals (``asm_register``) report False because their
        storage is a CPU register, not a memory slot.
        """
        if name in self.register_aliased_globals:
            return False
        return name in self.locals or name in self.global_scalars

    @staticmethod
    def _is_modulo_of(*, base: Node, expression: Node) -> bool:
        """Check if expression is (base % N) for some integer N."""
        return (
            isinstance(expression, BinaryOperation)
            and expression.operation == "%"
            and expression.left == base
            and isinstance(expression.right, Int)
        )

    @staticmethod
    def _is_simple_arg(node: Node, /) -> bool:
        """Return True if a call argument is safe for the register calling convention.

        "Safe" means :meth:`_emit_register_arg_single` can evaluate it
        without clobbering registers that another arg still needs.
        The base case is ``Int``/``String``/``Var`` (a single ``mov``
        from immediate/memory/pinned-reg).  ``BinaryOperation(+/-, leaf, leaf)``
        is also safe: ``generate_expression`` handles those via the
        ``add ax, [mem]``/``sub ax, imm`` fast paths, which only touch
        AX.  Inter-arg conflicts are checked separately at codegen
        time by the topological ordering in
        :meth:`_emit_register_arg_moves`.
        """
        if isinstance(node, (Int, String, Var)):
            return True
        if isinstance(node, BinaryOperation) and node.operation in ("+", "-"):
            return isinstance(node.left, (Int, String, Var)) and isinstance(node.right, (Int, String, Var))
        return False

    @staticmethod
    def _is_simple_printf(node: Node, /) -> bool:
        """Return True if *node* is ``printf(<literal with no '%'>)``.

        Such calls are semantically equivalent to a plain string print and
        can be folded into ``die()`` at end-of-main / end-of-branch.
        """
        return (
            isinstance(node, Call)
            and node.name == "printf"
            and len(node.args) == 1
            and isinstance(node.args[0], String)
            and "%" not in node.args[0].content
        )

    @staticmethod
    def _is_zero_exit_if(statement: Node, /) -> bool:
        """Check if a statement is ``if (VAR == 0) { exit(); }`` or ``if (VAR == 0) { return ...; }``."""
        return (
            isinstance(statement, If)
            and isinstance(statement.cond, BinaryOperation)
            and statement.cond.operation == "=="
            and statement.cond.right == Int(value=0)
            and len(statement.body) == 1
            and (statement.body[0] == Call(args=[], name="exit") or isinstance(statement.body[0], Return))
            and statement.else_body is None
        )

    @staticmethod
    def _name_is_reassigned(*, name: str, node: Node) -> bool:
        """Return True if an ``Assign(name=name, ...)`` occurs inside ``node``."""
        return ast_contains(node, lambda n: isinstance(n, Assign) and n.name == name)

    @staticmethod
    def _node_references_var(*, name: str, node: Node) -> bool:
        """Return True if ``Var(name)`` occurs anywhere inside ``node``."""
        return ast_contains(node, lambda n: isinstance(n, Var) and n.name == name)

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

    @staticmethod
    def _statement_references(node: Node, name: str, /) -> bool:
        """Return True if ``node`` reads or writes a variable named ``name``."""
        return ast_contains(
            node,
            lambda n: (isinstance(n, Var) and n.name == name) or (isinstance(n, Assign) and n.name == name),
        )

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

    @staticmethod
    def always_exits(body: list[Node], /) -> bool:
        """Check if a statement list always exits its enclosing block.

        Recognizes ``die(...)``/``exit()``/``return`` (program exits)
        and ``break`` (loop exit).  Used to elide dead fall-through
        code and to keep AX tracking alive across an if whose body
        never falls through.
        """
        if not body:
            return False
        last = body[-1]
        if isinstance(last, Break):
            return True
        if isinstance(last, Continue):
            return True
        if isinstance(last, Return):
            return True
        if isinstance(last, Call) and last.name in {"die", "exit"}:
            return True
        # Exhaustive if-else: both branches always exit.
        if isinstance(last, If) and last.else_body is not None:
            return CodeGeneratorBase.always_exits(last.body) and CodeGeneratorBase.always_exits(last.else_body)
        return False

    def emit(self, line: str = "") -> None:
        """Append a line of assembly to the output buffer."""
        self.lines.append(line)

    def emit_constant_reference(self, name: str) -> None:
        """Record a reference to a NAMED_CONSTANT.

        If the constant requires an extra NASM %include to provide its
        symbol (see :attr:`NAMED_CONSTANT_INCLUDES`), queue the include
        for emission at output time.
        """
        include = self.NAMED_CONSTANT_INCLUDES.get(name)
        if include is not None:
            self.required_includes.add(include)

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
