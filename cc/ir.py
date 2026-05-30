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

import dataclasses
from dataclasses import dataclass, field

from cc import ast_nodes
from cc.errors import CompileError
from cc.tokens import COMPARISON_OPERATIONS, INVERT_COMPARISON

Value = int | str | ast_nodes.AddressOf


def _is_constant_true(condition: ast_nodes.Node, /) -> bool:
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
class Block:
    """Escape hatch: lower this AST node via the existing statement codegen."""

    node: ast_nodes.Node


@dataclass(frozen=True, kw_only=True, slots=True)
class BranchFalse:
    """Jump to *target* when the condition ``left operation right`` is FALSE."""

    left: Value
    operation: str
    right: Value
    target: str


@dataclass(frozen=True, kw_only=True, slots=True)
class Call:
    """destination = name(args) — call expression; destination is None to discard return."""

    args: tuple[Value, ...]
    destination: str | None
    name: str


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
class Copy:
    """destination = source — scalar assignment."""

    destination: str
    source: Value


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
class InlineAsm:
    """Pass-through inline-asm block."""

    content: str


@dataclass(frozen=True, kw_only=True, slots=True)
class Jump:
    """Unconditional jump."""

    target: str


@dataclass(frozen=True, kw_only=True, slots=True)
class Label:
    """A branch target label."""

    name: str


@dataclass(frozen=True, kw_only=True, slots=True)
class LoopBoundary:
    """Push or pop loop label context for the emission layer.

    Emitted around loop bodies so that ``Continue`` / ``Break`` nodes
    inside ``Block``-wrapped AST statements (e.g. a Switch inside a
    while loop) can resolve to the correct jump targets.

    ``continue_label`` is ``None`` for switch lowerings: ``break``
    applies to the switch's end label but ``continue`` passes through
    to the enclosing loop (per C semantics).  In that case the
    push/pop affects only ``loop_end_labels``.
    """

    continue_label: str | None
    end_label: str
    push: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class Return:
    """Function return, optionally with a value."""

    value: Value | None


@dataclass(kw_only=True, slots=True)
class Switch:
    """``switch (discriminant) { case ...: ... default: ... }`` statement.

    The discriminant is kept as an AST node so the codegen's
    type-dependent paths (char-typed labels, enum-typed exhaustiveness,
    pinned-register / memory-scalar hoist) work unchanged.
    ``original_ast`` preserves the source :class:`ast_nodes.Switch` so
    enum exhaustiveness can be checked against the right variant set.
    Each :class:`SwitchCase` body is a list of IR instructions; a
    ``break`` inside an arm lowers to :class:`Jump` to ``end_label``,
    which the codegen emits once at the end of the lowered switch.
    """

    cases: list[SwitchCase]
    discriminant: ast_nodes.Node
    end_label: str
    original_ast: ast_nodes.Switch


@dataclass(kw_only=True, slots=True)
class SwitchCase:
    """A single arm of an :class:`Switch` instruction.

    ``value`` is the resolved integer constant for a ``case`` arm, or
    ``None`` for the ``default`` arm.  ``body`` is the lowered IR
    instruction list for the arm — labels and gotos inside the body are
    visible to IR-level passes.  Mutable so optimizer passes can rewrite
    the body in place.
    """

    body: list[Instruction]
    value: int | None


@dataclass(frozen=True, kw_only=True, slots=True)
class TailCall:
    """name(args) in tail position — codegen lowers to ``jmp name`` (no ``ret``).

    Produced by :func:`cc.ir_optimize.optimize` when a :class:`Call`
    whose result is consumed only by an immediately-following
    :class:`Return` is rewritten as a single control-flow terminator.
    The codegen falls back to a normal call / return sequence when the
    call site fails the runtime eligibility check (e.g. stack args,
    pinned saves required, callee is a builtin).
    """

    args: tuple[Value, ...]
    name: str


Instruction = (
    BinaryOperation
    | Block
    | BranchFalse
    | Call
    | CarryBranch
    | Copy
    | Index
    | IndexAssign
    | InlineAsm
    | Jump
    | Label
    | LoopBoundary
    | Return
    | Switch
    | TailCall
)


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

    def build_program(self, program: ast_nodes.Program, /) -> Program:
        """Lower every function in *program* to IR."""
        # Build a table of struct_name → frozenset of bitfield field names so
        # _lower_assign_expr can reject bitfield assignment-as-expression.
        self._struct_bitfield_names: dict[str, frozenset[str]] = {}
        for node in program.globals:
            if isinstance(node, ast_nodes.StructDecl):
                bitfield_names = frozenset(
                    field.field_name
                    for field in node.fields
                    if isinstance(field, ast_nodes.StructField) and field.bit_width is not None and field.field_name is not None
                )
                if bitfield_names:
                    self._struct_bitfield_names[node.name] = bitfield_names
        return Program(functions=[self._build_function(function) for function in program.functions], globals=program.globals)

    @staticmethod
    def _assign_rhs_field_name(node: ast_nodes.Node, /) -> str:
        """Return the attribute name that holds the RHS expression for any *Assign node.

        Every ``*Assign`` dataclass in ``cc/ast_nodes.py`` exposes the RHS
        as ``expr``, except :class:`~ast_nodes.PointerDereferenceAssign`
        which uses ``value``.
        """
        if isinstance(node, ast_nodes.PointerDereferenceAssign):
            return "value"
        return "expr"

    def _build_cond_false(
        self,
        *,
        cond: ast_nodes.Node,
        out: list[Instruction],
        strings: list[tuple[str, str]],
        target: str,
    ) -> None:
        """Emit IR that jumps to *target* when *cond* evaluates to false."""
        match cond:
            case ast_nodes.LogicalAnd(left=left, right=right):
                self._build_cond_false(cond=left, out=out, strings=strings, target=target)
                self._build_cond_false(cond=right, out=out, strings=strings, target=target)
            case ast_nodes.LogicalOr(left=left, right=right):
                skip_lbl = self._label("lor")
                self._build_cond_true(cond=left, out=out, strings=strings, target=skip_lbl)
                self._build_cond_false(cond=right, out=out, strings=strings, target=target)
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
                left_value = self._build_expr(expr=left, out=out, strings=strings)
                right_value = self._build_expr(expr=right, out=out, strings=strings)
                out.append(BranchFalse(left=left_value, operation=operation, right=right_value, target=target))
            case _:
                # General case: evaluate to a temp, test non-zero.
                value = self._build_expr(expr=cond, out=out, strings=strings)
                out.append(BranchFalse(left=value, operation="!=", right=0, target=target))

    def _build_cond_true(
        self,
        *,
        cond: ast_nodes.Node,
        out: list[Instruction],
        strings: list[tuple[str, str]],
        target: str,
    ) -> None:
        """Emit IR that jumps to *target* when *cond* evaluates to true."""
        match cond:
            case ast_nodes.LogicalOr(left=left, right=right):
                self._build_cond_true(cond=left, out=out, strings=strings, target=target)
                self._build_cond_true(cond=right, out=out, strings=strings, target=target)
            case ast_nodes.LogicalAnd(left=left, right=right):
                skip_lbl = self._label("land")
                self._build_cond_false(cond=left, out=out, strings=strings, target=skip_lbl)
                self._build_cond_true(cond=right, out=out, strings=strings, target=target)
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
                left_value = self._build_expr(expr=left, out=out, strings=strings)
                right_value = self._build_expr(expr=right, out=out, strings=strings)
                # Invert the condition: true-jump means false-branch doesn't fire.
                inverted = INVERT_COMPARISON[operation]
                out.append(BranchFalse(left=left_value, operation=inverted, right=right_value, target=target))
            case _:
                value = self._build_expr(expr=cond, out=out, strings=strings)
                out.append(BranchFalse(left=value, operation="==", right=0, target=target))

    def _build_do_while(
        self,
        *,
        body: list[ast_nodes.Node],
        cond: ast_nodes.Node,
        out: list[Instruction],
        strings: list[tuple[str, str]],
    ) -> None:
        loop_lbl = self._label("dloop")
        cond_lbl = self._label("dcond")
        end_lbl = self._label("dend")
        out.extend([
            Label(name=loop_lbl),
            LoopBoundary(continue_label=cond_lbl, end_label=end_lbl, push=True),
        ])
        self._build_stmts(stmts=body, out=out, break_tgt=end_lbl, cont_tgt=cond_lbl, strings=strings)
        out.extend([
            LoopBoundary(continue_label=cond_lbl, end_label=end_lbl, push=False),
            Label(name=cond_lbl),
        ])
        self._build_cond_true(cond=cond, out=out, strings=strings, target=loop_lbl)
        out.append(Label(name=end_lbl))

    def _build_expr(
        self,
        *,
        expr: ast_nodes.Node,
        out: list[Instruction],
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
                left_value = self._build_expr(expr=left, out=out, strings=strings)
                right_value = self._build_expr(expr=right, out=out, strings=strings)
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
                arg_values = tuple(self._build_expr(expr=a, out=out, strings=strings) for a in args)
                temp = self._tmp()
                out.append(Call(args=arg_values, destination=temp, name=name))
                return temp
            case ast_nodes.Index(array=ast_nodes.Var(name=base), index=index_node):
                index_value = self._build_expr(expr=index_node, out=out, strings=strings)
                temp = self._tmp()
                out.append(Index(base=base, destination=temp, index=index_value))
                return temp
            case ast_nodes.LogicalOr() | ast_nodes.LogicalAnd():
                # Short-circuit boolean: lower to conditional set (0 or 1).
                temp = self._tmp()
                true_lbl = self._label("btrue")
                end_lbl = self._label("bend")
                self._build_cond_true(cond=expr, out=out, strings=strings, target=true_lbl)
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
            case ast_nodes.AssignExpr(inner=inner):
                return self._lower_assign_expr(inner=inner, out=out, strings=strings)
            case _:
                # Complex: use a temp + Block to let AST codegen handle it.
                temp = self._tmp()
                out.append(Block(node=ast_nodes.Assign(expr=expr, name=temp)))
                return temp

    def _build_for(
        self,
        *,
        body: list[ast_nodes.Node],
        cond: ast_nodes.Node | None,
        init: list[ast_nodes.Node],
        out: list[Instruction],
        step: list[ast_nodes.Node],
        strings: list[tuple[str, str]],
    ) -> None:
        self._build_stmts(stmts=init, out=out, break_tgt=None, cont_tgt=None, strings=strings)
        loop_lbl = self._label("floop")
        step_lbl = self._label("fstep")
        end_lbl = self._label("fend")
        out.append(Label(name=loop_lbl))
        if cond is not None and not _is_constant_true(cond):
            self._build_cond_false(cond=cond, out=out, strings=strings, target=end_lbl)
        out.append(LoopBoundary(continue_label=step_lbl, end_label=end_lbl, push=True))
        self._build_stmts(stmts=body, out=out, break_tgt=end_lbl, cont_tgt=step_lbl, strings=strings)
        out.extend([
            LoopBoundary(continue_label=step_lbl, end_label=end_lbl, push=False),
            Label(name=step_lbl),
        ])
        for step_expr in step:
            self._build_expr(expr=step_expr, out=out, strings=strings)
        out.extend([Jump(target=loop_lbl), Label(name=end_lbl)])

    def _build_function(self, function: ast_nodes.Function, /) -> Function:
        out: list[Instruction] = []
        strings: list[tuple[str, str]] = []
        # Pre-scan parameter and VarDecl types so the IR builder can
        # detect expressions whose value type doesn't fit a plain ``int``
        # temp (notably ``unsigned long``-pointee Index loads on the
        # 16-bit target) and delegate them to the AST codegen via
        # :class:`Block`.  Without this, an ``unsigned long *p; x =
        # p[0]`` lowers to ``temp = p[0]; x = temp`` where ``temp`` is
        # an int-typed slot — the long-store path then rejects ``Var
        # temp`` with ``expected 'unsigned long' expression, got 'int'``.
        self._var_types: dict[str, str] = {}
        for parameter in function.params:
            self._var_types[parameter.name] = parameter.type
        self._collect_local_types(function.body)
        # Per-function user-label bookkeeping: definitions collected as
        # ``Label`` nodes are lowered; references collected as ``Goto``
        # nodes are lowered.  Validated after the body completes so
        # forward references resolve naturally.
        self._user_labels_defined: dict[str, int] = {}
        self._user_labels_referenced: dict[str, int] = {}
        self._build_stmts(break_tgt=None, cont_tgt=None, out=out, stmts=function.body, strings=strings)
        for name, line in self._user_labels_referenced.items():
            if name not in self._user_labels_defined:
                message = f"goto target '{name}' has no matching label in function '{function.name}'"
                raise CompileError(message, line=line)
        return Function(ast_node=function, body=out, strings=strings)

    def _build_if(
        self,
        *,
        body: list[ast_nodes.Node],
        break_tgt: str | None,
        cond: ast_nodes.Node,
        cont_tgt: str | None,
        else_body: list[ast_nodes.Node] | None,
        out: list[Instruction],
        strings: list[tuple[str, str]],
    ) -> None:
        if else_body is not None:
            else_lbl = self._label("else")
            end_lbl = self._label("endif")
            self._build_cond_false(cond=cond, out=out, strings=strings, target=else_lbl)
            self._build_stmts(stmts=body, out=out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            out.extend([Jump(target=end_lbl), Label(name=else_lbl)])
            self._build_stmts(stmts=else_body, out=out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            out.append(Label(name=end_lbl))
        else:
            end_lbl = self._label("endif")
            self._build_cond_false(cond=cond, out=out, strings=strings, target=end_lbl)
            self._build_stmts(stmts=body, out=out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            out.append(Label(name=end_lbl))

    def _build_stmt(
        self,
        *,
        break_tgt: str | None,
        cont_tgt: str | None,
        out: list[Instruction],
        stmt: ast_nodes.Node,
        strings: list[tuple[str, str]],
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
                # ``unsigned long`` destinations or long-pointee Index
                # right-hand sides can't round-trip through an int-typed
                # IR temp — let the AST codegen lower the whole
                # assignment in one shot so the DX:AX (16-bit) / EAX
                # (32-bit) shape is preserved end-to-end.  ``dest = cond
                # ? dest : other`` / ``dest = cond ? other : dest``
                # (the MIN / MAX guarded-update shape) is also routed
                # to the AST path so ``_try_emit_guarded_update``'s
                # tight ``cmp / Jcc / mov dest, other`` lowering fires;
                # the IR-temp path would round-trip through AX and
                # break the fast-path test.
                if (
                    self._var_types.get(name) == "unsigned long"
                    or self._is_long_pointee_index(expr)
                    or self._is_guarded_update(expression=expr, name=name)
                ):
                    out.append(Block(node=stmt))
                elif self._is_multi_binop_constant_chain(expr):
                    # Constant-foldable shapes (``a | b | c`` of named
                    # constants / enum values) lose their fold when broken
                    # into per-binop IR temps — the codegen's
                    # ``_constant_expression`` walks the AST RHS once and
                    # emits a single ``mov reg, (A|B|C)``; the per-temp
                    # IR form forces a runtime push/pop chain.  Delegate
                    # multi-binop chains (single binops still benefit from
                    # the IR path's pinned-register fast paths) to the
                    # AST codegen to preserve the fold.
                    out.append(Block(node=stmt))
                elif self._is_inplace_self_modify(expression=expr, name=name):
                    # ``x = x op K`` — the AST ``emit_store_local``
                    # recognises this self-mod pattern and emits ``inc
                    # reg`` / ``add reg, imm`` / ``or reg, imm`` etc.
                    # The IR-temp lowering would round-trip through
                    # ``tmp = x op K; x = tmp`` (3 instructions instead
                    # of 1), so delegate to the AST codegen.
                    out.append(Block(node=stmt))
                else:
                    source = self._build_expr(expr=expr, out=out, strings=strings)
                    out.append(Copy(destination=name, source=source))
            case ast_nodes.IndexAssign(array=ast_nodes.Var(name=base), index=index_node, expr=expr):
                index_value = self._build_expr(expr=index_node, out=out, strings=strings)
                source = self._build_expr(expr=expr, out=out, strings=strings)
                out.append(IndexAssign(base=base, index=index_value, source=source))
            case ast_nodes.Call(name="asm"):
                # asm() requires raw String args; pass through as-is.
                out.append(Block(node=stmt))
            case ast_nodes.Call() as call:
                args = tuple(self._build_expr(expr=a, out=out, strings=strings) for a in call.args)
                out.append(Call(args=args, destination=None, name=call.name))
            case ast_nodes.If(cond=cond, body=body, else_body=else_body):
                self._build_if(body=body, break_tgt=break_tgt, cond=cond, cont_tgt=cont_tgt, else_body=else_body, out=out, strings=strings)
            case ast_nodes.While(cond=cond, body=body):
                self._build_while(body=body, cond=cond, out=out, strings=strings)
            case ast_nodes.DoWhile(cond=cond, body=body):
                self._build_do_while(body=body, cond=cond, out=out, strings=strings)
            case ast_nodes.For(init=init, cond=cond, step=step, body=body):
                self._build_for(body=body, cond=cond, init=init, out=out, step=step, strings=strings)
            case ast_nodes.Break():
                assert break_tgt is not None, "break outside loop"
                out.append(Jump(target=break_tgt))
            case ast_nodes.Compound(body=body):
                self._build_stmts(stmts=body, out=out, break_tgt=break_tgt, cont_tgt=cont_tgt, strings=strings)
            case ast_nodes.Continue():
                assert cont_tgt is not None, "continue outside loop"
                out.append(Jump(target=cont_tgt))
            case ast_nodes.Goto(name=name):
                self._user_labels_referenced.setdefault(name, stmt.line)
                out.append(Jump(target=f".user_{name}"))
            case ast_nodes.Label(name=name):
                if name in self._user_labels_defined:
                    message = f"duplicate label '{name}'"
                    raise CompileError(message, line=stmt.line)
                self._user_labels_defined[name] = stmt.line
                out.append(Label(name=f".user_{name}"))
            case ast_nodes.Return(value=value):
                # A long-pointee Index in a return position must be
                # produced in DX:AX (16-bit) / EAX (32-bit); the int-typed
                # IR temp would truncate it.  Delegate the whole return
                # to the AST codegen.
                if value is not None and self._is_long_pointee_index(value):
                    out.append(Block(node=stmt))
                else:
                    v = self._build_expr(expr=value, out=out, strings=strings) if value is not None else None
                    out.append(Return(value=v))
            case ast_nodes.IndexedCall():
                out.append(Block(node=stmt))
            case ast_nodes.InlineAsm(content=content):
                out.append(InlineAsm(content=content))
            case ast_nodes.Switch(cases=cases, discriminant=discriminant):
                self._build_switch(
                    cases=cases,
                    cont_tgt=cont_tgt,
                    discriminant=discriminant,
                    original_ast=stmt,
                    out=out,
                    strings=strings,
                )
            case _:
                out.append(Block(node=stmt))

    def _build_stmts(
        self,
        *,
        break_tgt: str | None,
        cont_tgt: str | None,
        out: list[Instruction],
        stmts: list[ast_nodes.Node],
        strings: list[tuple[str, str]],
    ) -> None:
        for s in stmts:
            self._build_stmt(break_tgt=break_tgt, cont_tgt=cont_tgt, out=out, stmt=s, strings=strings)

    def _build_switch(
        self,
        *,
        cases: list[ast_nodes.SwitchCase],
        cont_tgt: str | None,
        discriminant: ast_nodes.Node,
        original_ast: ast_nodes.Switch,
        out: list[Instruction],
        strings: list[tuple[str, str]],
    ) -> None:
        # ``break`` inside a case body exits the switch via this label,
        # mirroring AST codegen's loop_end_labels push.  ``continue``
        # inherits the enclosing loop's continue label (or remains None
        # outside a loop) — the AST path does the same by not pushing
        # to loop_continue_labels in generate_switch.
        end_lbl = self._label("swend")
        ir_cases: list[SwitchCase] = []
        for case in cases:
            case_body: list[Instruction] = []
            self._build_stmts(stmts=case.body, out=case_body, break_tgt=end_lbl, cont_tgt=cont_tgt, strings=strings)
            ir_cases.append(SwitchCase(body=case_body, value=case.value))
        out.append(Switch(cases=ir_cases, discriminant=discriminant, end_label=end_lbl, original_ast=original_ast))

    def _build_while(
        self,
        *,
        body: list[ast_nodes.Node],
        cond: ast_nodes.Node,
        out: list[Instruction],
        strings: list[tuple[str, str]],
    ) -> None:
        loop_lbl = self._label("wloop")
        end_lbl = self._label("wend")
        out.append(Label(name=loop_lbl))
        # ``while (1)`` (and other statically-nonzero conditions) skip
        # the condition check entirely.  ``parse_condition`` wraps the
        # bare ``1`` as ``BinaryOperation("!=", Int(1), Int(0))``, so
        # both shapes have to be recognised.
        if not _is_constant_true(cond):
            self._build_cond_false(cond=cond, out=out, strings=strings, target=end_lbl)
        out.append(LoopBoundary(continue_label=loop_lbl, end_label=end_lbl, push=True))
        self._build_stmts(stmts=body, out=out, break_tgt=end_lbl, cont_tgt=loop_lbl, strings=strings)
        out.append(LoopBoundary(continue_label=loop_lbl, end_label=end_lbl, push=False))
        out.extend([Jump(target=loop_lbl), Label(name=end_lbl)])

    def _collect_local_types(self, stmts: list[ast_nodes.Node], /) -> None:
        """Walk *stmts* and record every ``VarDecl`` name → type binding.

        Nested blocks (``if`` / ``while`` / ``do``-``while``) are
        recursed into so a long-typed local declared inside a branch
        is still recognised when the IR builder lowers an Index in the
        same branch.
        """
        for statement in stmts:
            if isinstance(statement, ast_nodes.VarDecl):
                self._var_types[statement.name] = statement.type_name
            elif isinstance(statement, ast_nodes.If):
                self._collect_local_types(statement.body)
                if statement.else_body is not None:
                    self._collect_local_types(statement.else_body)
            elif isinstance(statement, (ast_nodes.Compound, ast_nodes.DoWhile, ast_nodes.While)):
                self._collect_local_types(statement.body)
            elif isinstance(statement, ast_nodes.Switch):
                for case in statement.cases:
                    self._collect_local_types(case.body)

    def _is_carry_return_call(self, node: ast_nodes.Node, /) -> bool:
        """Return True if *node* is a :class:`ast_nodes.Call` to a carry_return function."""
        return isinstance(node, ast_nodes.Call) and node.name in self._carry_return_functions

    @staticmethod
    def _is_guarded_update(*, expression: ast_nodes.Node, name: str) -> bool:
        """Return True if *expression* is ``cond ? <name> : other`` or ``cond ? other : <name>``.

        That's the shape ``MIN(name, other)`` / ``MAX(name, other)`` produce —
        the AST-codegen fast path ``_try_emit_guarded_update`` lowers it to a
        tight ``cmp / Jcc / mov dest, other`` that bypasses an AX round-trip.
        Routing such assignments through the IR-temp path would defeat the
        fast path, so they stay on the AST path via :class:`Block`.
        """
        if not isinstance(expression, ast_nodes.Conditional):
            return False
        then_is_self = isinstance(expression.then_expr, ast_nodes.Var) and expression.then_expr.name == name
        else_is_self = isinstance(expression.else_expr, ast_nodes.Var) and expression.else_expr.name == name
        return then_is_self or else_is_self

    @staticmethod
    def _is_inplace_self_modify(*, expression: ast_nodes.Node, name: str) -> bool:
        """Return True if *expression* is ``Var(name) op X`` for an in-place-eligible op.

        Recognises the ``x = x op K`` self-modification shape that the
        AST ``emit_store_local`` / ``_generate_binary_operation_expression``
        lower to ``inc reg`` / ``add reg, imm`` / ``or reg, imm`` etc.
        Going through the IR's per-binop temp lowering rebuilds the
        same value via ``tmp = x op K; x = tmp``, which expands to 3
        instructions instead of 1.  The check is intentionally
        limited to ``Var(name)`` on the left and any operand on the
        right — the fast paths fire whenever the destination operand
        appears once on the RHS.
        """
        if not isinstance(expression, ast_nodes.BinaryOperation):
            return False
        if expression.operation not in ("+", "-", "&", "|", "^", "<<", ">>", "*"):
            return False
        return isinstance(expression.left, ast_nodes.Var) and expression.left.name == name

    def _is_long_pointee_index(self, expression: ast_nodes.Node, /) -> bool:
        """Return True if *expression* is ``base[i]`` whose pointee is a 4-byte unsigned int.

        Used by ``_build_stmt`` / ``_build_expr`` to short-circuit the
        normal ``temp = Index(...)`` lowering — those temps are int-typed
        slots and would silently truncate a 32-bit pointee to the
        target's native acc width.  Delegating to the AST codegen via
        :class:`Block` preserves the full DX:AX (16-bit) / EAX (32-bit)
        value.
        """
        if not isinstance(expression, ast_nodes.Index):
            return False
        if not isinstance(expression.array, ast_nodes.Var):
            return False
        base_type = self._var_types.get(expression.array.name)
        return base_type == "unsigned long*"

    @staticmethod
    def _is_multi_binop_constant_chain(expression: ast_nodes.Node, /) -> bool:
        """Return True for a multi-binop chain of named-constant leaves.

        Restricted to chains whose leaves are ``Var`` (potentially a
        ``NAMED_CONSTANT`` / enum value resolvable by the codegen's
        :meth:`_constant_expression`) and at least one of which is
        a ``BinaryOperation`` — i.e. ``A | B | C`` of capital-letter
        identifiers.  Chains involving ``Int`` literals or local
        variables are excluded so the IR's per-temp lowering can keep
        the pinned-register fast paths firing where the AST path
        would lose the fold and emit a push/pop scratch sequence.
        """
        if not isinstance(expression, ast_nodes.BinaryOperation):
            return False
        if expression.operation not in ("+", "-", "*", "&", "|", "^"):
            return False
        if not Builder._is_named_constant_chain(expression.left):
            return False
        if not Builder._is_named_constant_chain(expression.right):
            return False
        # Require at least one branch to be itself a BinaryOperation so the
        # chain has 2+ operators; a bare ``Var op Var`` doesn't qualify
        # (the IR path's fast paths beat the AST path's frame round-trip).
        return isinstance(expression.left, ast_nodes.BinaryOperation) or isinstance(expression.right, ast_nodes.BinaryOperation)

    @staticmethod
    def _is_named_constant_chain(expression: ast_nodes.Node, /) -> bool:
        """Return True if *expression* is a chain of likely-NAMED_CONSTANT leaves.

        The codegen's :meth:`_constant_expression` only resolves
        ``Var`` nodes when they refer to ``NAMED_CONSTANTS`` /
        ``enum_constants`` / ``constant_aliases``; a chain that mixes
        in non-constant ``Var`` names is rejected by the folder and
        falls back to a verbose push/pop scratch sequence on the AST
        path.  Restricting ``Var`` leaves to ALL_CAPS identifiers (the
        convention for those tables) keeps the Block-delegation
        conservative — local-variable ``Var`` chains stay on the IR
        path where their pinned-register fast paths still fire.
        """
        if isinstance(expression, ast_nodes.Int):
            return True
        if isinstance(expression, ast_nodes.Var):
            return expression.name == expression.name.upper()
        if isinstance(expression, ast_nodes.BinaryOperation) and expression.operation in ("+", "-", "*", "&", "|", "^"):
            return Builder._is_named_constant_chain(expression.left) and Builder._is_named_constant_chain(expression.right)
        return False

    def _label(self, tag: str = "l", /) -> str:
        name = f"._ir_{tag}{self._counter}"
        self._counter += 1
        return name

    def _lower_assign_expr(
        self,
        *,
        inner: ast_nodes.Node,
        out: list[Instruction],
        strings: list[tuple[str, str]],
    ) -> str:
        """Lower a parenthesized assignment to IR; return the temp holding its value.

        Strategy: evaluate the RHS into a temp, rewrite the wrapped
        ``*Assign`` so its RHS is ``Var(temp)``, emit the rewritten
        ``*Assign`` as a statement (reusing the existing per-lvalue store
        paths), and return the temp as the expression value.  This
        guarantees the original RHS expression is evaluated exactly once.
        """
        line = inner.line
        rhs_field = self._assign_rhs_field_name(inner)
        original_rhs = getattr(inner, rhs_field)
        # ``unsigned long`` lvalues don't round-trip cleanly through
        # int-typed temps.  Out-of-scope per the spec.
        if isinstance(inner, ast_nodes.Assign) and self._var_types.get(inner.name) == "unsigned long":
            message = "assignment-as-expression to 'unsigned long' is not supported"
            raise CompileError(message, line=line)
        # Bitfield member assignments clobber AX during the read-modify-write
        # sequence, breaking the "AX = assigned value" contract.  Reject at
        # compile time rather than silently miscompile.
        if isinstance(inner, ast_nodes.MemberAssign) and inner.base_expr is None:
            struct_type = self._var_types.get(inner.object_name, "")
            # Dot form: "struct TAG"; arrow form: "struct TAG*" — strip the "*".
            tag = struct_type[7:].rstrip("*") if struct_type.startswith("struct ") else ""
            bitfield_fields = self._struct_bitfield_names.get(tag, frozenset())
            if inner.member_name in bitfield_fields:
                message = "assignment-as-expression to bitfield fields is not supported"
                raise CompileError(message, line=line)
        rhs_value = self._build_expr(expr=original_rhs, out=out, strings=strings)
        temp = self._tmp()
        out.append(Copy(destination=temp, source=rhs_value))
        rebound = dataclasses.replace(inner, **{rhs_field: ast_nodes.Var(line=line, name=temp)})
        self._build_stmt(break_tgt=None, cont_tgt=None, out=out, stmt=rebound, strings=strings)
        return temp

    def _tmp(self) -> str:
        name = f"_ir_{self._counter}"
        self._counter += 1
        return name
