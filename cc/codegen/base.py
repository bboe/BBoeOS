"""Architecture-agnostic code-generator scaffolding.

:class:`CodeGeneratorBase` carries the AST/IR predicates every backend
needs — liveness, aliasing, simple-arg classification, always-exit
detection, argument-count validation — with no x86-specific
assumptions.  Backends subclass it to add instruction selection,
register pool management, peephole passes, and emit helpers.

The first wave of extractions here are the pure static methods:
no ``self`` references, no state, no emit.  A follow-up pass can
move arch-agnostic instance methods (``_is_byte_var``,
``_is_memory_scalar``, ``_dispatch_chain_var``, ``_constant_expression``,
…) once we've audited their state dependencies.
"""

from __future__ import annotations

from cc import ir
from cc.ast_nodes import (
    Assign,
    BinaryOperation,
    Break,
    Call,
    Continue,
    If,
    Index,
    Int,
    LogicalAnd,
    Node,
    Return,
    String,
    Var,
)
from cc.errors import CompileError
from cc.utils import ast_contains


class CodeGeneratorBase:
    """Architecture-agnostic base for every backend code generator.

    Holds predicates that query AST / IR shape and validate caller
    arguments.  Backends (e.g. :class:`cc.codegen.x86.X86CodeGenerator`)
    subclass this and layer on the emit / peephole / instruction-
    selection logic specific to their ISA.
    """

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

    @staticmethod
    def _statement_references(node: Node, name: str, /) -> bool:
        """Return True if ``node`` reads or writes a variable named ``name``."""
        return ast_contains(
            node,
            lambda n: (isinstance(n, Var) and n.name == name) or (isinstance(n, Assign) and n.name == name),
        )

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
