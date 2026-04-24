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

    name: str


@dataclass(kw_only=True, slots=True)
class ArrayDecl(Node):
    """Array declaration ``T name[] = {...};`` (local or global).

    At global scope the array may also carry an explicit ``[SIZE]`` with
    no initializer — stored as ``size`` (a parser node, evaluated at
    NASM assemble time so it can reference kernel constants).
    """

    init: Node | None
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
class Continue(Node):
    """``continue;`` statement (jumps to the innermost loop's condition test)."""


@dataclass(kw_only=True, slots=True)
class DerefAssign(Node):
    """Pointer dereference assignment ``*name = expr;``."""

    expr: Node
    name: str


@dataclass(kw_only=True, slots=True)
class DoWhile(Node):
    """``do { body } while (cond);`` loop."""

    body: list[Node]
    cond: Node


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
    name: str
    params: list[Param]
    preserve_registers: list[str] = field(default_factory=list, kw_only=True)
    regparm_count: int = field(default=0, kw_only=True)


@dataclass(kw_only=True, slots=True)
class If(Node):
    """``if (cond) { body } [else { else_body }]`` statement."""

    body: list[Node]
    cond: Node
    else_body: list[Node] | None


@dataclass(kw_only=True, slots=True)
class Index(Node):
    """Subscript expression ``name[index]``."""

    index: Node
    name: str


@dataclass(kw_only=True, slots=True)
class IndexAssign(Node):
    """Indexed assignment ``name[index] = expr;``."""

    expr: Node
    index: Node
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
    """

    asm_register: str | None = field(default=None, kw_only=True)
    asm_symbol: str | None = field(default=None, kw_only=True)
    function_pointer_params: list[Param] | None = field(default=None, kw_only=True)
    init: Node | None
    name: str
    type_name: str


@dataclass(kw_only=True, slots=True)
class While(Node):
    """``while (cond) { body }`` loop."""

    body: list[Node]
    cond: Node
