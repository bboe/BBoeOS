"""Liveness analyzer tests.

Synthetic AST inputs exercise specific control-flow shapes and assert
the resulting interference graph matches what a hand-trace would
predict.  The full integration (allocator wiring) is covered by the
existing cc.py codegen tests; these tests target the liveness pass
in isolation so any failure points at the analyzer, not at downstream
allocation choices.
"""

from __future__ import annotations

from cc.ast_nodes import (
    Assign,
    BinaryOperation,
    Break,
    Compound,
    If,
    Int,
    Param,
    Return,
    Switch,
    SwitchCase,
    Var,
    VarDecl,
    While,
)
from cc.codegen.liveness import LivenessAnalyzer


def _assign(name: str, expression: object) -> Assign:
    return Assign(line=1, name=name, expr=expression)


def _declaration(name: str, init: int) -> VarDecl:
    return VarDecl(line=1, name=name, init=_int(init), type_name="int")


def _int(value: int) -> Int:
    return Int(line=1, value=value)


def _var(name: str) -> Var:
    return Var(line=1, name=name)


def test_liveness_disjoint_if_else_arms_do_not_interfere() -> None:
    """Locals in the then-arm and else-arm of an if don't interfere.

    Control reaches at most one arm per evaluation, so the two
    locals are never live simultaneously.
    """
    body = [
        VarDecl(line=1, name="cond", init=_int(1), type_name="int"),
        If(
            body=[
                _declaration("then_local", 1),
                _assign("result", _var("then_local")),
            ],
            cond=_var("cond"),
            else_body=[
                _declaration("else_local", 2),
                _assign("result", _var("else_local")),
            ],
            line=1,
        ),
        Return(line=1, value=_var("result")),
    ]
    interference = LivenessAnalyzer(body=body).interference()
    then_neighbors = interference.get("then_local", set())
    assert "else_local" not in then_neighbors, f"then/else locals should NOT interfere:\n{interference}"


def test_liveness_disjoint_switch_cases_do_not_interfere() -> None:
    """Locals declared in different switch case bodies don't interfere.

    Only one case body runs per dispatch, so two locals declared in
    separate cases never both live at the same program point.
    """
    body = [
        VarDecl(line=1, name="d", init=_int(1), type_name="int"),
        VarDecl(line=1, name="result", init=_int(0), type_name="int"),
        Switch(
            cases=[
                SwitchCase(
                    body=[
                        _declaration("local_a", 10),
                        _assign("result", _var("local_a")),
                        Break(line=1),
                    ],
                    line=1,
                    value=1,
                ),
                SwitchCase(
                    body=[
                        _declaration("local_b", 20),
                        _assign("result", _var("local_b")),
                        Break(line=1),
                    ],
                    line=1,
                    value=2,
                ),
            ],
            discriminant=_var("d"),
            line=1,
        ),
        Return(line=1, value=_var("result")),
    ]
    interference = LivenessAnalyzer(body=body).interference()
    a_neighbors = interference.get("local_a", set())
    b_neighbors = interference.get("local_b", set())
    assert "local_b" not in a_neighbors, f"local_a and local_b should NOT interfere:\n{interference}"
    assert "local_a" not in b_neighbors, f"local_b and local_a should NOT interfere:\n{interference}"


def test_liveness_linear_uses_create_interference() -> None:
    """Two locals read at the same point interfere with each other.

    ``int x = 1; int y = 2; result = x + y;`` — when ``x + y`` is
    evaluated, both x and y must be live, so the interference graph
    records the pair.
    """
    body = [
        _declaration("x", 1),
        _declaration("y", 2),
        _assign("result", BinaryOperation(line=1, left=_var("x"), operation="+", right=_var("y"))),
        Return(line=1, value=_var("result")),
    ]
    interference = LivenessAnalyzer(body=body).interference()
    assert "y" in interference.get("x", set()), f"x and y should interfere:\n{interference}"
    assert "x" in interference.get("y", set()), f"y and x should interfere:\n{interference}"


def test_liveness_loop_body_locals_interfere_across_iterations() -> None:
    """A local defined inside a loop body interferes with itself's live edge.

    Specifically: a loop-body local read on a later iteration must
    not share a register with another local whose value the body
    also reads.  Modeled here: an outer accumulator + an inner
    loop-body counter — the accumulator is live across the loop
    (back-edge), so it interferes with the counter.
    """
    body = [
        _declaration("accumulator", 0),
        While(
            body=[
                _declaration("counter", 0),
                _assign("accumulator", BinaryOperation(line=1, left=_var("accumulator"), operation="+", right=_var("counter"))),
            ],
            cond=BinaryOperation(line=1, left=_var("accumulator"), operation="<", right=_int(10)),
            line=1,
        ),
        Return(line=1, value=_var("accumulator")),
    ]
    interference = LivenessAnalyzer(body=body).interference()
    assert "counter" in interference.get("accumulator", set()), f"loop-body counter must interfere with outer accumulator:\n{interference}"


def test_liveness_nested_blocks_interfere_with_outer_when_overlapping() -> None:
    """Outer local read inside an inner block interferes with inner locals.

    The outer local is live across the inner block; any local
    declared inside that's still live when the outer is read
    interferes.
    """
    body = [
        _declaration("outer", 1),
        Compound(
            body=[
                _declaration("inner", 2),
                _assign("result", BinaryOperation(line=1, left=_var("outer"), operation="+", right=_var("inner"))),
            ],
            line=1,
        ),
        Return(line=1, value=_var("result")),
    ]
    interference = LivenessAnalyzer(body=body).interference()
    assert "inner" in interference.get("outer", set()), f"outer/inner overlap should interfere:\n{interference}"


def test_liveness_parameter_interferes_with_unrelated_unused_parameter() -> None:
    """Two parameters where only one is used: both reserved at entry interfere.

    ``int f(int a, int b) { return a; }`` — ``a`` is live at the
    function entry edge.  Because ``b`` is also defined at the
    synthetic ENTRY, the def-vs-live rule says ``b`` interferes with
    ``a``.  This is the conservative direction: the allocator can
    never share the slot holding the caller-passed value of ``b``
    with the slot holding ``a``, even when ``b`` is otherwise unused
    in the body.
    """
    body = [Return(line=1, value=_var("a"))]
    parameters = [
        Param(in_register=None, is_array=False, name="a", out_register=None, type="int"),
        Param(in_register=None, is_array=False, name="b", out_register=None, type="int"),
    ]
    interference = LivenessAnalyzer(body=body, parameters=parameters).interference()
    assert "a" in interference.get("b", set()), f"unused param b should interfere with used param a:\n{interference}"
