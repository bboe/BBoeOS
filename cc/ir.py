"""Three-address code (TAC) intermediate representation.

A :data:`Value` operand is either an integer constant or a name
(variable, temp, or string label).  Every TAC instruction has at most
one operator and one destination, with simple operands on the right-
hand side.  :class:`Builder` flattens an AST :class:`cc.ast_nodes.Program`
into a flat list of instructions per function; :mod:`cc.codegen` then
lowers them to x86 assembly.

Import this module as a namespace (``from cc import ir``) so the
instruction types read as ``ir.BinaryOperation``, ``ir.Call`` etc.
Several names overlap with :mod:`cc.ast_nodes` (``BinaryOperation``,
``Call``, ``Function``, …) — the module prefix disambiguates the IR
form from the AST form.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cc import ast_nodes
from cc.tokens import COMPARISON_OPERATIONS, INVERT_COMPARISON

Value = int | str | ast_nodes.AddressOf


def _is_constant_true(condition: ast_nodes.Node) -> bool:
    """Return True if *condition* is statically nonzero.

    Recognises both the bare ``Int(value=N)`` form (not wrapped by the
    parser) and the ``BinaryOperation("!=", Int(value=N), Int(value=0))``
    form that ``Parser.parse_condition`` produces for bare expressions.
    """
    if isinstance(condition, ast_nodes.Int) and condition.value != 0:
        return True
    if not isinstance(condition, ast_nodes.BinaryOperation):
        return False
    if condition.operation != "!=":
        return False
    if condition.right != ast_nodes.Int(value=0):
        return False
    return isinstance(condition.left, ast_nodes.Int) and condition.left.value != 0


@dataclass(frozen=True, kw_only=True, slots=True)
class BinaryOperation:
    """destination = left operation right — arithmetic or bitwise binary operation."""

    destination: str
    left: Value
    operation: str
    right: Value


@dataclass(frozen=True, kw_only=True, slots=True)
class Copy:
    """destination = source — scalar assignment."""

    destination: str
    source: Value


@dataclass(frozen=True, kw_only=True, slots=True)
class Call:
    """destination = name(args) — call expression; destination is None to discard return."""

    args: tuple[Value, ...]
    destination: str | None
    name: str


@dataclass(frozen=True, kw_only=True, slots=True)
class Index:
    """destination = base[index] — array / pointer read."""

    base: str
    destination: str
    index: Value


@dataclass(frozen=True, kw_only=True, slots=True)
class IndexAssign:
    """base[index] = source — array / pointer write."""

    base: str
    index: Value
    source: Value


@dataclass(frozen=True, kw_only=True, slots=True)
class Label:
    """A branch target label."""

    name: str


@dataclass(frozen=True, kw_only=True, slots=True)
class Jump:
    """Unconditional jump."""

    target: str


@dataclass(frozen=True, kw_only=True, slots=True)
class BranchFalse:
    """Jump to *target* when the condition ``left operation right`` is FALSE."""

    left: Value
    operation: str
    right: Value
    target: str


@dataclass(frozen=True, kw_only=True, slots=True)
class CarryBranch:
    """Call a ``carry_return`` function, then branch on the carry flag.

    ``__attribute__((carry_return))`` functions report their boolean
    return in CF (clear = true, set = false).  When such a call is used
    directly as an ``if`` / ``while`` condition, lowering the call to a
    value temp and comparing it against zero would lose the CF — we'd
    test whatever happens to be in AX.  ``CarryBranch`` keeps the call
    and the branch together so the lowering emits the tight ``call X /
    jc target`` (when=``set``) or ``jnc target`` (when=``clear``) that
    the AST ``emit_condition`` shortcut produces.  ``call_ast`` holds
    the original :class:`ast_nodes.Call` so ``generate_call`` can set
    up arguments (regparm / stack) the same way a direct AST-path call
    would.
    """

    call_ast: ast_nodes.Call
    target: str
    when: str  # "set" → ``jc``, "clear" → ``jnc``


@dataclass(frozen=True, kw_only=True, slots=True)
class Return:
    """Function return, optionally with a value."""

    value: Value | None


@dataclass(frozen=True, kw_only=True, slots=True)
class InlineAsm:
    """Pass-through inline-asm block."""

    content: str


@dataclass(frozen=True, kw_only=True, slots=True)
class Block:
    """Escape hatch: lower this AST node via the existing statement codegen."""

    node: ast_nodes.Node


Instruction = BinaryOperation | Copy | Call | Index | IndexAssign | Label | Jump | BranchFalse | CarryBranch | Return | InlineAsm | Block


@dataclass(kw_only=True, slots=True)
class Function:
    """IR form of a single function; ``ast_node`` is kept for frame setup."""

    ast_node: ast_nodes.Function
    body: list[Instruction]
    strings: list[tuple[str, str]] = field(default_factory=list)


@dataclass(kw_only=True, slots=True)
class Program:
    """IR for an entire translation unit."""

    functions: list[Function]
    globals: list[ast_nodes.Node]


class Builder:
    """Convert an AST :class:`cc.ast_nodes.Program` to an :class:`Program`.

    Each function body is flattened into a linear list of
    :data:`Instruction` instructions.  Nested expressions are broken
    into sequences of temporaries (``_ir_0``, ``_ir_1``, …) so every
    instruction has at most one operator and simple operands.  Control
    flow (``if`` / ``while`` / ``do``-``while``) is linearised into
    :class:`Label` / :class:`Jump` / :class:`BranchFalse` instructions.
    Complex forms that the lowering cannot easily handle fall back to
    :class:`Block` so the existing AST-based codegen path handles them
    unchanged.
    """

    def __init__(self, *, carry_return_functions: frozenset[str] = frozenset()) -> None:
        """Initialize counters and record which callees use ``carry_return``.

        ``carry_return_functions`` is the set of function names declared
        with ``__attribute__((carry_return))``.  Conditions of the shape
        ``call(...) != 0`` / ``call(...) == 0`` where the callee is in
        this set lower to :class:`CarryBranch` instead of going through
        a value temp, preserving the CF-based return-value convention.
        """
        self._counter = 0
        self._str_counter = 0
        self._carry_return_functions = carry_return_functions

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_program(self, program: ast_nodes.Program) -> Program:
        """Lower every function in *program* to IR."""
        return Program(functions=[self._build_function(f) for f in program.functions], globals=program.globals)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_carry_return_call(self, node: ast_nodes.Node) -> bool:
        """Return True if *node* is a :class:`ast_nodes.Call` to a carry_return function."""
        return isinstance(node, ast_nodes.Call) and node.name in self._carry_return_functions

    def _tmp(self) -> str:
        name = f"_ir_{self._counter}"
        self._counter += 1
        return name

    def _lbl(self, tag: str = "l") -> str:
        name = f"._ir_{tag}{self._counter}"
        self._counter += 1
        return name

    # ------------------------------------------------------------------
    # Function / statement building
    # ------------------------------------------------------------------

    def _build_function(self, func: ast_nodes.Function) -> Function:
        out: list[Instruction] = []
        strings: list[tuple[str, str]] = []
        self._build_stmts(func.body, out, break_tgt=None, cont_tgt=None, strings=strings)
        return Function(ast_node=func, body=out, strings=strings)

    def _build_stmts(
        self,
        stmts: list[ast_nodes.Node],
        out: list[Instruction],
        *,
        strings: list[tuple[str, str]],
        break_tgt: str | None,
        cont_tgt: str | None,
    ) -> None:
        for s in stmts:
            self._build_stmt(s, out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)

    def _build_stmt(
        self,
        stmt: ast_nodes.Node,
        out: list[Instruction],
        *,
        strings: list[tuple[str, str]],
        break_tgt: str | None,
        cont_tgt: str | None,
    ) -> None:
        match stmt:
            case ast_nodes.VarDecl():
                # Preserve full VarDecl semantics (constant aliases, visibility
                # registration, byte-type tracking) via the existing AST path.
                out.append(Block(node=stmt))
            case ast_nodes.ArrayDecl():
                # Array initializers are complex; delegate to existing codegen.
                out.append(Block(node=stmt))
            case ast_nodes.Assign(name=name, expr=expr):
                source = self._build_expr(expr, out, strings=strings)
                out.append(Copy(destination=name, source=source))
            case ast_nodes.IndexAssign(array=ast_nodes.Var(name=base), index=index_node, expr=expr):
                index_value = self._build_expr(index_node, out, strings=strings)
                source = self._build_expr(expr, out, strings=strings)
                out.append(IndexAssign(base=base, index=index_value, source=source))
            case ast_nodes.Call(name="asm"):
                # asm() requires raw String args; pass through as-is.
                out.append(Block(node=stmt))
            case ast_nodes.Call() as call:
                args = tuple(self._build_expr(a, out, strings=strings) for a in call.args)
                out.append(Call(args=args, destination=None, name=call.name))
            case ast_nodes.If(cond=cond, body=body, else_body=else_body):
                self._build_if(cond, body, else_body, out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            case ast_nodes.While(cond=cond, body=body):
                self._build_while(cond, body, out, strings=strings)
            case ast_nodes.DoWhile(cond=cond, body=body):
                self._build_do_while(cond, body, out, strings=strings)
            case ast_nodes.Break():
                assert break_tgt is not None, "break outside loop"
                out.append(Jump(target=break_tgt))
            case ast_nodes.Continue():
                assert cont_tgt is not None, "continue outside loop"
                out.append(Jump(target=cont_tgt))
            case ast_nodes.Return(value=value):
                v = self._build_expr(value, out, strings=strings) if value is not None else None
                out.append(Return(value=v))
            case ast_nodes.InlineAsm(content=content):
                out.append(InlineAsm(content=content))
            case _:
                out.append(Block(node=stmt))

    def _build_if(
        self,
        cond: ast_nodes.Node,
        body: list[ast_nodes.Node],
        else_body: list[ast_nodes.Node] | None,
        out: list[Instruction],
        *,
        strings: list[tuple[str, str]],
        break_tgt: str | None,
        cont_tgt: str | None,
    ) -> None:
        if else_body is not None:
            else_lbl = self._lbl("else")
            end_lbl = self._lbl("endif")
            self._build_cond_false(cond, else_lbl, out, strings=strings)
            self._build_stmts(body, out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            out.extend([Jump(target=end_lbl), Label(name=else_lbl)])
            self._build_stmts(else_body, out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            out.append(Label(name=end_lbl))
        else:
            end_lbl = self._lbl("endif")
            self._build_cond_false(cond, end_lbl, out, strings=strings)
            self._build_stmts(body, out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            out.append(Label(name=end_lbl))

    def _build_while(
        self,
        cond: ast_nodes.Node,
        body: list[ast_nodes.Node],
        out: list[Instruction],
        *,
        strings: list[tuple[str, str]],
    ) -> None:
        loop_lbl = self._lbl("wloop")
        end_lbl = self._lbl("wend")
        out.append(Label(name=loop_lbl))
        # ``while (1)`` (and other statically-nonzero conditions) skip
        # the condition check entirely.  ``parse_condition`` wraps the
        # bare ``1`` as ``BinaryOperation("!=", Int(1), Int(0))``, so
        # both shapes have to be recognised.
        if not _is_constant_true(cond):
            self._build_cond_false(cond, end_lbl, out, strings=strings)
        self._build_stmts(body, out, break_tgt=end_lbl, cont_tgt=loop_lbl, strings=strings)
        out.extend([Jump(target=loop_lbl), Label(name=end_lbl)])

    def _build_do_while(
        self,
        cond: ast_nodes.Node,
        body: list[ast_nodes.Node],
        out: list[Instruction],
        *,
        strings: list[tuple[str, str]],
    ) -> None:
        loop_lbl = self._lbl("dloop")
        cond_lbl = self._lbl("dcond")
        end_lbl = self._lbl("dend")
        out.append(Label(name=loop_lbl))
        self._build_stmts(body, out, break_tgt=end_lbl, cont_tgt=cond_lbl, strings=strings)
        out.append(Label(name=cond_lbl))
        self._build_cond_true(cond, loop_lbl, out, strings=strings)
        out.append(Label(name=end_lbl))

    # ------------------------------------------------------------------
    # Condition helpers (emit branch-when-false / branch-when-true)
    # ------------------------------------------------------------------

    def _build_cond_false(
        self,
        cond: ast_nodes.Node,
        target: str,
        out: list[Instruction],
        *,
        strings: list[tuple[str, str]],
    ) -> None:
        """Emit IR that jumps to *target* when *cond* evaluates to false."""
        match cond:
            case ast_nodes.LogicalAnd(left=left, right=right):
                self._build_cond_false(left, target, out, strings=strings)
                self._build_cond_false(right, target, out, strings=strings)
            case ast_nodes.LogicalOr(left=left, right=right):
                skip_lbl = self._lbl("lor")
                self._build_cond_true(left, skip_lbl, out, strings=strings)
                self._build_cond_false(right, target, out, strings=strings)
                out.append(Label(name=skip_lbl))
            case ast_nodes.BinaryOperation(operation=operation, left=left, right=right) if (
                operation in ("!=", "==") and self._is_carry_return_call(left) and right == ast_nodes.Int(value=0)
            ):
                # ``if (carry_return_call() != 0)`` / ``... == 0`` — jump to
                # *target* when the condition is false, i.e. jump on CF set
                # for ``!=`` (false means the call returned 0) and on CF
                # clear for ``==``.
                when = "set" if operation == "!=" else "clear"
                out.append(CarryBranch(call_ast=left, target=target, when=when))
            case ast_nodes.BinaryOperation(operation=operation, left=left, right=right) if operation in COMPARISON_OPERATIONS:
                left_value = self._build_expr(left, out, strings=strings)
                right_value = self._build_expr(right, out, strings=strings)
                out.append(BranchFalse(left=left_value, operation=operation, right=right_value, target=target))
            case _:
                # General case: evaluate to a temp, test non-zero.
                value = self._build_expr(cond, out, strings=strings)
                out.append(BranchFalse(left=value, operation="!=", right=0, target=target))

    def _build_cond_true(
        self,
        cond: ast_nodes.Node,
        target: str,
        out: list[Instruction],
        *,
        strings: list[tuple[str, str]],
    ) -> None:
        """Emit IR that jumps to *target* when *cond* evaluates to true."""
        match cond:
            case ast_nodes.LogicalOr(left=left, right=right):
                self._build_cond_true(left, target, out, strings=strings)
                self._build_cond_true(right, target, out, strings=strings)
            case ast_nodes.LogicalAnd(left=left, right=right):
                skip_lbl = self._lbl("land")
                self._build_cond_false(left, skip_lbl, out, strings=strings)
                self._build_cond_true(right, target, out, strings=strings)
                out.append(Label(name=skip_lbl))
            case ast_nodes.BinaryOperation(operation=operation, left=left, right=right) if (
                operation in ("!=", "==") and self._is_carry_return_call(left) and right == ast_nodes.Int(value=0)
            ):
                # Dual of the false-jump shortcut in ``_build_cond_false``:
                # jump on CF clear for ``!=`` (true means the call returned 1),
                # on CF set for ``==``.
                when = "clear" if operation == "!=" else "set"
                out.append(CarryBranch(call_ast=left, target=target, when=when))
            case ast_nodes.BinaryOperation(operation=operation, left=left, right=right) if operation in COMPARISON_OPERATIONS:
                left_value = self._build_expr(left, out, strings=strings)
                right_value = self._build_expr(right, out, strings=strings)
                # Invert the condition: true-jump means false-branch doesn't fire.
                inverted = INVERT_COMPARISON[operation]
                out.append(BranchFalse(left=left_value, operation=inverted, right=right_value, target=target))
            case _:
                value = self._build_expr(cond, out, strings=strings)
                out.append(BranchFalse(left=value, operation="==", right=0, target=target))

    # ------------------------------------------------------------------
    # Expression building (returns a Value for the result)
    # ------------------------------------------------------------------

    def _build_expr(
        self,
        expr: ast_nodes.Node,
        out: list[Instruction],
        *,
        strings: list[tuple[str, str]],
    ) -> Value:
        match expr:
            case ast_nodes.Int(value=integer_value):
                return integer_value
            case ast_nodes.Var(name=variable_name):
                return variable_name
            case ast_nodes.String(content=content):
                label = f"_ir_s{self._str_counter}"
                self._str_counter += 1
                strings.append((label, content))
                return label
            case ast_nodes.BinaryOperation(operation=operation, left=left, right=right):
                left_value = self._build_expr(left, out, strings=strings)
                right_value = self._build_expr(right, out, strings=strings)
                temp = self._tmp()
                out.append(BinaryOperation(destination=temp, left=left_value, operation=operation, right=right_value))
                return temp
            case ast_nodes.Call(name=name) if name in self._carry_return_functions:
                # ``carry_return`` callees report their result via CF,
                # not AX.  The IR flow would store (garbage) AX to a
                # temp; delegate to the AST codegen (which knows how
                # to synthesise ``0``/``1`` from CF when the call's
                # return value is actually needed).
                temp = self._tmp()
                out.append(Block(node=ast_nodes.Assign(expr=expr, name=temp)))
                return temp
            case ast_nodes.Call(name=name, args=args):
                arg_values = tuple(self._build_expr(a, out, strings=strings) for a in args)
                temp = self._tmp()
                out.append(Call(args=arg_values, destination=temp, name=name))
                return temp
            case ast_nodes.Index(array=ast_nodes.Var(name=base), index=index_node):
                index_value = self._build_expr(index_node, out, strings=strings)
                temp = self._tmp()
                out.append(Index(base=base, destination=temp, index=index_value))
                return temp
            case ast_nodes.LogicalOr() | ast_nodes.LogicalAnd():
                # Short-circuit boolean: lower to conditional set (0 or 1).
                temp = self._tmp()
                true_lbl = self._lbl("btrue")
                end_lbl = self._lbl("bend")
                self._build_cond_true(expr, true_lbl, out, strings=strings)
                out.extend([
                    Copy(destination=temp, source=0),
                    Jump(target=end_lbl),
                    Label(name=true_lbl),
                    Copy(destination=temp, source=1),
                    Label(name=end_lbl),
                ])
                return temp
            case ast_nodes.AddressOf():
                # Pass through as-is so generate_call can detect out_register
                # arguments (&var) without the node being replaced by a temp.
                return expr
            case _:
                # Complex: use a temp + Block to let AST codegen handle it.
                temp = self._tmp()
                out.append(Block(node=ast_nodes.Assign(expr=expr, name=temp)))
                return temp
