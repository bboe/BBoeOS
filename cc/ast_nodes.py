"""Abstract syntax tree node dataclasses.

Every parsed C construct becomes one of these nodes.  ``Node`` is the
base class; all fields are keyword-only so constructors stay explicit
(``BinaryOperation(left=…, operation=…, right=…)``).

Module named ``ast_nodes`` rather than ``ast`` to avoid shadowing the
Python standard-library :mod:`ast` module.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(kw_only=True, slots=True)
class Node:
    """Base class for every AST node.

    ``line`` is the 1-based source line where the construct begins; it
    defaults to 0 for nodes synthesized by the compiler (e.g. constant
    folding) and is set by the parser to the first token's line for
    everything else.  Excluded from ``__eq__`` so two AST nodes with
    the same shape compare equal regardless of source location —
    several peephole / fusion passes rely on structural equality
    (``cond.right == Int(value=0)`` etc.).
    """

    line: int = field(compare=False, default=0)


@dataclass(kw_only=True, slots=True)
class AddressOf(Node):
    """Address-of expression ``&name``."""

    var: Var


@dataclass(kw_only=True, slots=True)
class ArrayDecl(Node):
    """Array declaration ``T name[] = {...};`` (local or global).

    At global scope the array may also carry an explicit ``[SIZE]`` with
    no initializer — stored as ``size`` (a parser node, evaluated at
    NASM assemble time so it can reference kernel constants).

    ``is_extern`` is set for ``extern T name[N];`` file-scope declarations
    that name a symbol whose storage lives in another translation unit;
    the generator skips emitting ``_g_<name>:`` storage for these.
    """

    init: Node | None
    is_extern: bool = field(default=False, kw_only=True)
    name: str
    size: Node | None = field(default=None, kw_only=True)
    type_name: str


@dataclass(kw_only=True, slots=True)
class ArrayInit(Node):
    """Brace-initializer ``{a, b, c}`` for an array declaration."""

    elements: list[Node]


@dataclass(kw_only=True, slots=True)
class Assign(Node):
    """Assignment ``name = expr;`` or ``name += expr;`` (the latter lowers to ``name = name + expr``)."""

    expr: Node
    name: str


@dataclass(kw_only=True, slots=True)
class BinaryOperation(Node):
    """Binary operator expression ``left OPERATION right``."""

    left: Node
    operation: str
    right: Node


@dataclass(kw_only=True, slots=True)
class Break(Node):
    """``break;`` statement (exits the innermost loop)."""


@dataclass(kw_only=True, slots=True)
class Call(Node):
    """Function/builtin call ``name(args...)``."""

    args: list[Node]
    name: str


@dataclass(kw_only=True, slots=True)
class Conditional(Node):
    """Ternary conditional expression ``condition ? then_expr : else_expr``.

    Right-associative: ``a ? b : c ? d : e`` parses as
    ``a ? b : (c ? d : e)``.  Lower precedence than ``||``, higher than
    assignment (which is a statement, not an expression, in cc.py).
    The chosen branch is the only one evaluated at runtime — the
    codegen lowers to a conditional jump over the unchosen branch.
    """

    condition: Node
    else_expr: Node
    then_expr: Node


@dataclass(kw_only=True, slots=True)
class Continue(Node):
    """``continue;`` statement (jumps to the innermost loop's condition test)."""


@dataclass(kw_only=True, slots=True)
class DerefAssign(Node):
    """Pointer dereference assignment ``*pointer = expr;``."""

    expr: Node
    pointer: Var


@dataclass(kw_only=True, slots=True)
class DoubleIndex(Node):
    """Chained subscript ``name[outer][inner]``.

    Used when *name* is an array of pointers (``char *foo[N]``,
    ``uint8_t *bar[N]``, etc.).  The outer subscript loads a pointer
    from the array, the inner subscript indexes into the pointee.
    Codegen consults :meth:`_index_pointee_size` to size the inner
    load.  Assignment to a double-subscript LHS is not (yet) supported.
    """

    array: Var
    outer_index: Node
    inner_index: Node


@dataclass(kw_only=True, slots=True)
class DoWhile(Node):
    """``do { body } while (cond);`` loop."""

    body: list[Node]
    cond: Node


@dataclass(kw_only=True, slots=True)
class EnumDecl(Node):
    """Enum type declaration ``enum NAME { A, B = 5, C, ... };`` at file scope.

    Each variant is recorded as a ``(name, value)`` tuple in source
    order.  Values auto-increment from 0 (or from the most recent
    explicit value + 1).  Variants are registered as integer-valued
    named constants so any subsequent expression that references the
    bare variant name resolves to the corresponding integer literal —
    the same path :class:`Var` references to ``#define`` constants
    already take.  The variant list is also retained so the switch
    exhaustiveness check can iterate over every value declared for a
    given enum tag.
    """

    name: str
    variants: list[tuple[str, int]]


@dataclass(kw_only=True, slots=True)
class Function(Node):
    """Function definition: name, parameter list, and body.

    ``regparm_count`` captures the ``__attribute__((regparm(N)))``
    annotation on the definition (0 = standard cdecl, 1 = first arg
    arrives in AX and is spilled to a local stack slot in the
    prologue).  Only regparm(1) is currently supported.

    ``carry_return`` captures ``__attribute__((carry_return))`` —
    the function reports its int return via the carry flag instead
    of AX: ``return 1`` → ``clc`` then epilogue, ``return 0`` →
    ``stc`` then epilogue.  Callers use it in ``if`` / ``while``
    conditions, where cc.py dispatches on ``jnc`` (true) / ``jc``
    (false) directly — no AX round-trip.

    ``always_inline`` captures ``__attribute__((always_inline))`` —
    the function must have a single ``asm("...")`` body and zero or
    one (regparm(1)) parameter.  At each C-level call site, cc.py
    splices the body text in place of ``call X`` (with local label
    uniquification); no free-standing function body is emitted, so
    there's nothing for inline-asm ``call X`` to resolve against.

    ``preserve_registers`` captures zero or more
    ``__attribute__((preserve_register("REG")))`` annotations.  Each
    named register is pushed immediately before the BP frame setup and
    popped (in reverse order) just before every ``ret``.  ``pop REG``
    does not affect flags, so CF from ``carry_return`` functions
    survives.

    ``naked`` captures ``__attribute__((naked))`` — the function emits
    no prologue or epilogue.  ``in_register`` parameters are pinned to
    their declared register (no slot, no spill); the body must not
    declare locals or take stack-passed parameters.  Useful for thin
    register-preserving dispatchers that tail-jump to another routine.

    ``is_prototype`` is ``True`` for forward declarations (no body) —
    the node is retained in ``Program.functions`` so the generator can
    register calling-convention info (e.g., ``out_register`` params and
    ``carry_return``) for call sites that reference an externally-defined
    function.  No code is emitted for prototype nodes.
    """

    always_inline: bool = field(default=False, kw_only=True)
    body: list[Node]
    carry_return: bool = field(default=False, kw_only=True)
    is_prototype: bool = field(default=False, kw_only=True)
    naked: bool = field(default=False, kw_only=True)
    name: str
    params: list[Param]
    preserve_registers: list[str] = field(default_factory=list, kw_only=True)
    regparm_count: int = field(default=0, kw_only=True)


@dataclass(kw_only=True, slots=True)
class Goto(Node):
    """``goto label;`` unconditional jump to a labelled statement in the same function."""

    name: str


@dataclass(kw_only=True, slots=True)
class If(Node):
    """``if (cond) { body } [else { else_body }]`` statement."""

    body: list[Node]
    cond: Node
    else_body: list[Node] | None


@dataclass(kw_only=True, slots=True)
class Index(Node):
    """Subscript expression ``array[index]``."""

    array: Var
    index: Node


@dataclass(kw_only=True, slots=True)
class IndexAssign(Node):
    """Indexed assignment ``array[index] = expr;``."""

    array: Var
    expr: Node
    index: Node


@dataclass(kw_only=True, slots=True)
class IndexMemberAccess(Node):
    """Rvalue ``arr[i].field`` or ``arr[i]->field`` (struct array element member read)."""

    arrow: bool
    index: Node
    member_name: str
    name: str


@dataclass(kw_only=True, slots=True)
class IndexMemberAssign(Node):
    """Statement ``arr[i].field = expr;`` or ``arr[i]->field = expr;``."""

    arrow: bool
    expr: Node
    index: Node
    member_name: str
    name: str


@dataclass(kw_only=True, slots=True)
class IndexMemberIndex(Node):
    """Rvalue ``arr[i].field[n]`` (element of an array-typed struct member)."""

    arrow: bool
    elem_index: Node
    index: Node
    member_name: str
    name: str


@dataclass(kw_only=True, slots=True)
class IndexMemberIndexAssign(Node):
    """Statement ``arr[i].field[n] = expr;``."""

    arrow: bool
    elem_index: Node
    expr: Node
    index: Node
    member_name: str
    name: str


@dataclass(kw_only=True, slots=True)
class InlineAsm(Node):
    """File-scope ``asm("...");`` directive.

    The content is the raw string literal text (still carrying C
    escape sequences); ``builtin_asm`` decodes and emits it at tail.
    """

    content: str


@dataclass(kw_only=True, slots=True)
class Int(Node):
    """Integer literal."""

    value: int


class Char(Int):
    """A ``char`` literal (``'x'``).

    Subclasses :class:`Int` so existing ``isinstance(..., Int)`` checks
    continue to treat char literals as integer constants for codegen,
    while type-checking (e.g., equality operand validation) can still
    distinguish char literals from plain integers.

    Placed directly after :class:`Int` because the subclass relationship
    requires its base to be defined first.
    """

    __slots__ = ()


@dataclass(kw_only=True, slots=True)
class Label(Node):
    """``name:`` labelled statement — branch target for :class:`Goto`."""

    name: str


@dataclass(kw_only=True, slots=True)
class LogicalAnd(Node):
    """Short-circuit ``left && right`` expression."""

    left: Node
    right: Node


@dataclass(kw_only=True, slots=True)
class LogicalOr(Node):
    """Short-circuit ``left || right`` expression."""

    left: Node
    right: Node


@dataclass(kw_only=True, slots=True)
class MemberAccess(Node):
    """Member access expression: ``ptr->field`` or ``obj.field``.

    ``arrow=True`` for ``->``, ``False`` for ``.``.  Only the
    ``arrow=True`` form (pointer dereference) is fully supported in the
    first cycle; ``arrow=False`` is parsed but may raise CompileError
    in codegen if the base is not a pointer.
    """

    arrow: bool
    member_name: str
    object_name: str


@dataclass(kw_only=True, slots=True)
class MemberAssign(Node):
    """Member assignment statement: ``ptr->field = expr;``.

    Like :class:`MemberAccess` but with an ``expr`` to store.
    """

    arrow: bool
    expr: Node
    member_name: str
    object_name: str


@dataclass(kw_only=True, slots=True)
class MemberIndex(Node):
    """Indexed access into a struct's array-typed member: ``ptr->field[i]``.

    Loads one element (byte or word, per the field's element type) from
    ``base + field_offset + index * element_size``.  ``ptr->field`` (no
    index) is :class:`MemberAccess`, which yields the field's address for
    array fields.
    """

    arrow: bool
    index: Node
    member_name: str
    object_name: str


@dataclass(kw_only=True, slots=True)
class Param:
    """A function parameter: type, name, and whether it was declared with ``[]``.

    ``in_register`` captures ``__attribute__((in_register("REG")))`` — the
    caller loads the argument into the named register instead of pushing it;
    the callee spills the register to a local slot at function entry and reads
    it from there like any other local.

    ``out_register`` captures ``__attribute__((out_register("REG")))`` — the
    parameter is an output-only register: the caller passes ``&local`` but no
    push is emitted; after the call the named register is captured into the
    local.  In the callee body, ``*param = expr`` emits ``mov REG, expr``
    rather than a pointer write.
    """

    in_register: str | None = field(default=None, kw_only=True)
    is_array: bool
    name: str
    out_register: str | None = field(default=None, kw_only=True)
    type: str


@dataclass(kw_only=True, slots=True)
class Program(Node):
    """Top-level AST: functions and file-scope global declarations.

    ``globals`` holds :class:`VarDecl` / :class:`ArrayDecl` nodes
    declared at file scope.  Scalars become ``_g_<name>`` cells in the
    tail data block; arrays become ``_g_<name>`` labels that user code
    references by name just like a local ``int arr[] = {...};``.
    """

    functions: list[Node]
    globals: list[Node] = field(default_factory=list, kw_only=True)


@dataclass(kw_only=True, slots=True)
class Return(Node):
    """``return [expr];`` statement."""

    value: Node | None


@dataclass(kw_only=True, slots=True)
class SizeofType(Node):
    """``sizeof(type_name)`` expression."""

    type_name: str


@dataclass(kw_only=True, slots=True)
class SizeofVar(Node):
    """``sizeof(name)`` expression (size of a declared variable)."""

    name: str


@dataclass(kw_only=True, slots=True)
class StructDecl(Node):
    """Struct type declaration ``struct NAME { fields... };`` at file scope.

    Carries no storage: the generator builds a layout table from it and
    emits nothing.  Lives in ``Program.globals`` before any variable
    that uses the struct type.
    """

    fields: list  # list[StructField]
    name: str


@dataclass(kw_only=True, slots=True)
class StructField(Node):
    """A single field declaration inside a struct body."""

    field_name: str
    type_name: str


@dataclass(kw_only=True, slots=True)
class StructInit(Node):
    """Brace-initializer ``{a, b}`` for one struct element within an array initializer.

    Fields are positional; unspecified trailing fields are zero-filled by
    the code generator.  Nested struct-of-struct initializers are not
    supported.
    """

    fields: list[Node]


@dataclass(kw_only=True, slots=True)
class String(Node):
    """String literal."""

    content: str


@dataclass(kw_only=True, slots=True)
class Switch(Node):
    """``switch (discriminant) { case ...: ... default: ... }`` statement.

    Lowering is a simple compare/jump chain (no jump table): each
    ``case`` arm gets a label, the prologue emits ``cmp disc, value``
    against each constant, jumps to the matching arm, and falls into
    ``default`` (or past the switch's end label) if nothing matches.

    When ``discriminant``'s static type is ``enum NAME`` and no
    ``default`` arm is present, the codegen verifies that every variant
    declared for that enum is named by some ``case``; missing variants
    are reported as a compile error.  This is the headline reason
    switch on enum exists in cc.py — adding a new enum variant later
    flags every dispatch site that forgot it.
    """

    cases: list[SwitchCase]
    discriminant: Node


@dataclass(kw_only=True, slots=True)
class SwitchCase(Node):
    """A single ``case CONST: body...`` or ``default: body...`` arm of a switch.

    ``value`` is the resolved integer constant for a ``case`` arm, or
    ``None`` for the ``default`` arm.  ``body`` is the list of statements
    associated with the arm; fall-through (no ``break``) is represented
    by an arm that simply doesn't end in :class:`Break`, identical to
    standard C — the codegen emits each arm's body sequentially, and a
    missing ``break`` means control flows straight into the next arm's
    body just as in a hand-written compare-and-jump chain.
    """

    body: list[Node]
    value: int | None


@dataclass(kw_only=True, slots=True)
class TailCall(Node):
    """``__tail_call(fn_ptr, arg1, arg2, ...)`` statement.

    Tears down the current stack frame and jumps to ``fn_ptr`` (a
    function-pointer local) with the given arguments loaded into their
    declared registers.  The callee returns directly to the current
    function's caller — AX and CF flow through unmodified.
    """

    args: list[Node]
    fn: str


@dataclass(kw_only=True, slots=True)
class Var(Node):
    """Reference to a named variable or named constant."""

    name: str


@dataclass(kw_only=True, slots=True)
class VarDecl(Node):
    """Scalar local declaration ``T name [= init];``.

    ``asm_register`` captures the ``__attribute__((asm_register("REG")))``
    annotation on a file-scope declaration — the declared variable is
    aliased to the named CPU register, so reads compile as the register
    itself (no ``[_g_name]`` load) and writes compile as a direct
    ``mov REG, ...``.  ``None`` for ordinary scalars / locals.

    ``function_pointer_params`` is set when the declaration uses function-pointer
    syntax ``type (*name)(params)``.  The list carries each parameter's
    ``in_register`` annotation so call sites know which CPU registers to
    load before ``call ax``.  ``None`` for ordinary (non-function_pointer) scalars.

    ``is_extern`` is set for ``extern T name;`` file-scope declarations
    that name a symbol whose storage lives in another translation unit;
    the generator skips emitting ``_g_<name>:`` storage for these.

    ``pinned_register`` carries the ``__attribute__((pinned_register("REG")))``
    annotation on a function_pointer local — the variable's value lives
    in the named CPU register and ``__tail_call`` jumps via that
    register instead of EAX.  Useful when EAX/AL holds an actual
    syscall argument (e.g. fd_ioctl's cmd byte) that the dispatcher
    must preserve through to the handler.
    """

    asm_register: str | None = field(default=None, kw_only=True)
    asm_symbol: str | None = field(default=None, kw_only=True)
    function_pointer_params: list[Param] | None = field(default=None, kw_only=True)
    init: Node | None
    is_extern: bool = field(default=False, kw_only=True)
    name: str
    pinned_register: str | None = field(default=None, kw_only=True)
    type_name: str


@dataclass(kw_only=True, slots=True)
class While(Node):
    """``while (cond) { body }`` loop."""

    body: list[Node]
    cond: Node
