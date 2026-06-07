"""Tests for invariants on the :mod:`cc.ir` instruction classes themselves."""

from __future__ import annotations

import dataclasses
import typing

from cc import ast_nodes, ir


def _address(*, base_value: ir.Value | None = None, destination: str = "_ir_0", indices: tuple[ir.Value, ...] = ()) -> ir.Address:
    """Build an :class:`ir.Address` over a single-deref placeholder shape."""
    shape = ast_nodes.DereferencePlace(pointer=ast_nodes.VariablePlace(name="p"))
    return ir.Address(base_value=base_value, destination=destination, indices=indices, shape=shape)


def _build_function_body(source: str, /, *, name: str) -> list[ir.Instruction]:
    """Lower a single C source string to one function's flat IR instruction list."""
    from cc.lexer import tokenize  # noqa: PLC0415
    from cc.parser import Parser  # noqa: PLC0415

    program = ir.Builder().build_program(Parser(tokenize(source)).parse_program())
    return next(function for function in program.functions if function.ast_node.name == name).body


def test_access_op_destinations() -> None:
    """Address / AddressOf / Load define a destination; Store does not."""
    from cc.ir_optimize import _instruction_destination as optimize_destination  # noqa: PLC0415, PLC2701
    from cc.regalloc import _instruction_defs  # noqa: PLC0415, PLC2701
    from cc.ssa import _instruction_destination as ssa_destination  # noqa: PLC0415, PLC2701

    address = _address(destination="_ir_5")
    load = ir.Load(address="_ir_5", destination="_ir_6", signed=False, width=4)
    address_of = ir.AddressOf(address="_ir_5", destination="_ir_7")
    store = ir.Store(address="_ir_5", value="_ir_8", width=4)
    for instruction, expected in ((address, "_ir_5"), (load, "_ir_6"), (address_of, "_ir_7")):
        assert optimize_destination(instruction) == expected
        assert ssa_destination(instruction) == expected
        assert _instruction_defs(instruction=instruction) == (expected,)
    assert optimize_destination(store) is None
    assert _instruction_defs(instruction=store) == ()


def test_access_op_value_fields() -> None:
    """:class:`ir.Address` exposes exactly its two dynamic leaves."""
    assert ir.Address.VALUE_FIELDS == ("base_value", "indices")
    assert ir.AddressOf.VALUE_FIELDS == ("address",)
    assert ir.Load.VALUE_FIELDS == ("address",)
    assert ir.Store.VALUE_FIELDS == ("address", "value")


def test_address_value_operands_skip_none_leaves() -> None:
    """A symbol-rooted :class:`ir.Address` reads only its present dynamic leaves."""
    from cc.ir_optimize import _instruction_value_operands  # noqa: PLC0415, PLC2701
    from cc.regalloc import _instruction_uses  # noqa: PLC0415, PLC2701
    from cc.ssa import _iter_value_operands  # noqa: PLC0415, PLC2701

    static = _address(base_value=None, indices=())
    assert _instruction_value_operands(static) == ()
    assert tuple(_iter_value_operands(static)) == ()
    assert _instruction_uses(instruction=static) == ()

    dynamic = _address(base_value="_ir_1", indices=("_ir_2",))
    assert _instruction_value_operands(dynamic) == ("_ir_1", "_ir_2")
    assert tuple(_iter_value_operands(dynamic)) == ("_ir_1", "_ir_2")
    assert set(_instruction_uses(instruction=dynamic)) == {"_ir_1", "_ir_2"}


def test_arrow_member_address_of_lowers_to_address_of_op() -> None:
    """``&pointer->member`` migrates off the Block/Access escape hatch onto Address + AddressOf."""
    body = _build_function_body(
        "struct s { int x; };\nint *f(struct s *p) { return &p->x; }\n",
        name="f",
    )
    kinds = [type(op).__name__ for op in body]
    assert any(isinstance(op, ir.AddressOf) for op in body), f"expected an ir.AddressOf op, got {kinds}"
    assert any(isinstance(op, ir.Address) for op in body), f"expected an ir.Address op, got {kinds}"
    # The arrow-member address-of no longer rides the AST escape hatch.
    assert not any(isinstance(op, (ir.Block, ir.Access)) for op in body), f"address-of must not ride Block/Access, got {kinds}"


def test_arrow_member_increment_lowers_to_increment_decrement_op() -> None:
    """``s->len++`` in statement position migrates off Block onto Address + IncrementDecrement."""
    body = _build_function_body(
        "struct sink { int len; };\nvoid f(struct sink *s) { s->len++; }\n",
        name="f",
    )
    kinds = [type(op).__name__ for op in body]
    increments = [op for op in body if isinstance(op, ir.IncrementDecrement)]
    assert len(increments) == 1, f"expected exactly one ir.IncrementDecrement, got {kinds}"
    assert increments[0].delta == 1
    assert any(isinstance(op, ir.Address) for op in body), f"expected an ir.Address op, got {kinds}"
    # The member increment no longer rides the AST escape hatch.
    assert not any(isinstance(op, (ir.Block, ir.Access)) for op in body), f"member increment must not ride Block/Access, got {kinds}"


def test_every_instruction_subclass_declares_value_fields() -> None:
    """Every member of :data:`cc.ir.Instruction` declares ``VALUE_FIELDS``.

    Walkers like :func:`cc.ssa._iter_value_operands` and
    :func:`cc.ssa._map_value_operands` read ``VALUE_FIELDS`` to enumerate
    operand-bearing fields.  A new instruction added without declaring
    it would silently be skipped by every pass; this test fails-loud so
    the contract holds.
    """
    instruction_types = typing.get_args(ir.Instruction)
    missing = [cls for cls in instruction_types if not hasattr(cls, "VALUE_FIELDS")]
    assert missing == [], f"missing VALUE_FIELDS: {[cls.__name__ for cls in missing]}"


def test_increment_decrement_op_uses_and_side_effects() -> None:
    """IncrementDecrement reads its address, defines nothing, and is side-effecting."""
    from cc.ir_optimize import _has_side_effects, _instruction_destination  # noqa: PLC0415, PLC2701
    from cc.regalloc import _instruction_defs, _instruction_uses  # noqa: PLC0415, PLC2701

    increment = ir.IncrementDecrement(address="_ir_1", delta=1, is_postfix=True)
    assert ir.IncrementDecrement.VALUE_FIELDS == ("address",)
    assert _instruction_uses(instruction=increment) == ("_ir_1",)
    assert _instruction_defs(instruction=increment) == ()
    assert _instruction_destination(increment) is None
    assert _has_side_effects(increment)


def test_index_rhs_member_store_lowers_to_store() -> None:
    """``p->member = arr[i]`` lowers onto Address + Store with an Index RHS temp.

    No ir.Access producer remains for the shape (ledger class 3 re-admitted in
    phase 2).
    """
    body = _build_function_body(
        "struct s { int value; };\nint arr[8];\nvoid f(struct s *pointer, int i) { pointer->value = arr[i]; }\n",
        name="f",
    )
    kinds = [type(op).__name__ for op in body]
    index_ops = [op for op in body if isinstance(op, ir.Index)]
    addresses = [op for op in body if isinstance(op, ir.Address)]
    stores = [op for op in body if isinstance(op, ir.Store)]
    assert len(index_ops) == 1, f"expected exactly one ir.Index (the RHS load), got {kinds}"
    assert len(addresses) == 1, f"expected exactly one ir.Address (the store target), got {kinds}"
    assert len(stores) == 1, f"expected exactly one ir.Store, got {kinds}"
    assert stores[0].value == index_ops[0].destination, (
        f"the Store must consume the Index temp, got store.value={stores[0].value!r} vs index.destination={index_ops[0].destination!r}"
    )
    assert not any(isinstance(op, ir.Access) for op in body), f"Index RHS store must not ride Access, got {kinds}"


def test_load_store_uses_and_side_effects() -> None:
    """Load reads its address; Store reads address+value and is side-effecting."""
    from cc.ir_optimize import _has_side_effects  # noqa: PLC0415, PLC2701
    from cc.regalloc import _instruction_uses  # noqa: PLC0415, PLC2701

    load = ir.Load(address="_ir_1", destination="_ir_2", signed=True, width=2)
    store = ir.Store(address="_ir_1", value="_ir_3", width=1)
    assert _instruction_uses(instruction=load) == ("_ir_1",)
    assert set(_instruction_uses(instruction=store)) == {"_ir_1", "_ir_3"}
    assert not _has_side_effects(load)
    assert _has_side_effects(store)


def test_member_store_with_member_load_rhs_lowers_to_load_then_store() -> None:
    """``p->next = q->prev`` (PlaceLoad RHS) folds: the RHS Load's temp feeds the Store value."""
    body = _build_function_body(
        "struct node { struct node *next; struct node *prev; };\nvoid f(struct node *p, struct node *q) { p->next = q->prev; }\n",
        name="f",
    )
    kinds = [type(op).__name__ for op in body]
    loads = [op for op in body if isinstance(op, ir.Load)]
    stores = [op for op in body if isinstance(op, ir.Store)]
    assert len(loads) == 1, f"expected exactly one ir.Load, got {kinds}"
    assert len(stores) == 1, f"expected exactly one ir.Store, got {kinds}"
    assert stores[0].value == loads[0].destination, "the Store must consume the RHS Load's temp"
    # Neither side rides the AST escape hatch.
    assert not any(isinstance(op, (ir.Block, ir.Access)) for op in body), f"store with load RHS must not ride Block/Access, got {kinds}"


def test_mixed_subscript_member_chain_store_lowers_to_address_and_store() -> None:
    """``table[i].name[j] = src`` folds onto one Address carrying both chain indices + Store."""
    body = _build_function_body(
        "struct entry { char name[8]; int value; };\nstruct entry table[4];\nvoid f(int i, int j, int src) { table[i].name[j] = src; }\n",
        name="f",
    )
    kinds = [type(op).__name__ for op in body]
    addresses = [op for op in body if isinstance(op, ir.Address)]
    assert len(addresses) == 1, f"expected exactly one ir.Address, got {kinds}"
    assert addresses[0].indices == ("i", "j"), f"expected source-order index tuple, got {addresses[0].indices}"
    assert any(isinstance(op, ir.Store) for op in body), f"expected an ir.Store op, got {kinds}"
    # The mixed-chain store no longer rides the AST escape hatch.
    assert not any(isinstance(op, (ir.Block, ir.Access)) for op in body), f"mixed-chain store must not ride Block/Access, got {kinds}"


def test_multidim_subscript_load_lowers_to_address_with_index_tuple() -> None:
    """``m[i][j]`` migrates off Access onto one Address carrying a 2-index tuple + Load."""
    body = _build_function_body(
        "int m[2][3];\nint f(int i, int j) { return m[i][j]; }\n",
        name="f",
    )
    kinds = [type(op).__name__ for op in body]
    addresses = [op for op in body if isinstance(op, ir.Address)]
    assert len(addresses) == 1, f"expected exactly one ir.Address, got {kinds}"
    assert addresses[0].indices == ("i", "j"), f"expected outer-first index tuple, got {addresses[0].indices}"
    assert addresses[0].base_value is None
    assert any(isinstance(op, ir.Load) for op in body), f"expected an ir.Load op, got {kinds}"
    # The multidim read no longer rides the AST escape hatch.
    assert not any(isinstance(op, (ir.Block, ir.Access)) for op in body), f"multidim load must not ride Block/Access, got {kinds}"


def test_struct_field_multidim_load_lowers_to_member_rooted_address() -> None:
    """``p->cells[i][j]`` folds onto an Address whose nested-subscript shape roots at a MemberPlace."""
    body = _build_function_body(
        "struct g { int cells[2][3]; };\nint f(struct g *p, int i, int j) { return p->cells[i][j]; }\n",
        name="f",
    )
    kinds = [type(op).__name__ for op in body]
    addresses = [op for op in body if isinstance(op, ir.Address)]
    assert len(addresses) == 1, f"expected exactly one ir.Address, got {kinds}"
    assert addresses[0].indices == ("i", "j"), f"expected outer-first index tuple, got {addresses[0].indices}"
    root: ast_nodes.Node = addresses[0].shape
    while isinstance(root, ast_nodes.SubscriptPlace):
        root = root.base
    assert isinstance(root, ast_nodes.MemberPlace), f"expected a MemberPlace root, got {type(root).__name__}"
    assert not any(isinstance(op, (ir.Block, ir.Access)) for op in body), (
        f"struct-field multidim load must not ride Block/Access, got {kinds}"
    )


def test_subscript_call_lowers_to_address_and_indirect_call() -> None:
    """``handlers[--count]();`` folds onto Address + IndirectCall and leaves the Access hatch."""
    body = _build_function_body(
        "void (*handlers[4])(void);\nint count;\nvoid f(void) { handlers[--count](); }\n",
        name="f",
    )
    kinds = [type(op).__name__ for op in body]
    addresses = [op for op in body if isinstance(op, ir.Address)]
    indirect_calls = [op for op in body if isinstance(op, ir.IndirectCall)]
    assert len(addresses) == 1, f"expected exactly one ir.Address, got {kinds}"
    assert len(addresses[0].indices) == 1, f"expected a single pre-lowered index leaf, got {addresses[0].indices}"
    assert len(indirect_calls) == 1, f"expected exactly one ir.IndirectCall, got {kinds}"
    assert indirect_calls[0].address == addresses[0].destination
    # The call itself no longer rides the AST escape hatch (the compound
    # index pre-lowering may still emit a Block for the -- assign).
    assert not any(isinstance(op, ir.Access) for op in body), f"subscript call must not ride Access, got {kinds}"


def test_substitute_value_rewrites_access_ops() -> None:
    """Copy-propagation rewrites the new ops' value operands by name."""
    from cc.ir_optimize import _substitute_value  # noqa: PLC0415, PLC2701

    address = _address(base_value="_ir_1", indices=("_ir_2",))
    rewritten = _substitute_value(address, source="_ir_9", target="_ir_2")
    assert rewritten.indices == ("_ir_9",)
    assert rewritten.base_value == "_ir_1"

    store = ir.Store(address="_ir_1", value="_ir_2", width=4)
    rewritten_store = _substitute_value(store, source="_ir_9", target="_ir_1")
    assert rewritten_store.address == "_ir_9"
    assert rewritten_store.value == "_ir_2"


def test_value_fields_name_real_dataclass_fields() -> None:
    """Each ``VALUE_FIELDS`` entry names an actual field on its dataclass.

    Catches typos that would survive at import time but blow up later
    inside :func:`getattr` calls during optimization.
    """
    for cls in typing.get_args(ir.Instruction):
        declared = {field.name for field in dataclasses.fields(cls)}
        for field_name in cls.VALUE_FIELDS:
            assert field_name in declared, f"{cls.__name__}.VALUE_FIELDS names unknown field {field_name!r}"
