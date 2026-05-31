"""Tests for cc.loops — natural-loop detection over the basic-block CFG.

Each test builds a small flat IR by hand (avoiding the AST + Builder
roundtrip) so the expected loop shape is obvious from the test source.
"""

from __future__ import annotations

from cc import ast_nodes, cfg, ir, loops


def _function(body: list[ir.Instruction], /) -> ir.Function:
    """Wrap *body* in a minimal :class:`ir.Function` for CFG construction."""
    ast = ast_nodes.Function(
        body=[],
        line=1,
        name="f",
        params=[],
    )
    return ir.Function(ast_node=ast, body=body, strings=[])


def test_hoist_invariant_binary_operation_moves_to_preheader() -> None:
    """A BinaryOp whose operands are loop-external moves out of the body to the preheader."""
    body = [
        ir.Copy(destination="a", source=10),
        ir.Copy(destination="b", source=20),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BinaryOperation(destination="_ir_invariant", left="a", right="b", operation="+"),
        ir.BranchFalse(left="_ir_invariant", operation="==", right=0, target=".end"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = loops.hoist_loop_invariants(body)
    # The BinaryOp moved out of the loop body — the only BinaryOp in the
    # output appears before .loop in source order.
    binary_indices = [index for index, instruction in enumerate(result) if isinstance(instruction, ir.BinaryOperation)]
    loop_index = next(
        index for index, instruction in enumerate(result) if isinstance(instruction, ir.Label) and instruction.name == ".loop"
    )
    assert binary_indices == [loop_index - 1] or all(index < loop_index for index in binary_indices)


def test_hoist_invariant_load_moves_to_preheader() -> None:
    """An ``ir.Index`` whose base and index are loop-invariant hoists when no memory writer is in the loop.

    The load reads ``arr[0]`` — base is a loop-external name, index is
    a literal, no ``IndexAssign`` or ``Call`` writes through any
    pointer in the loop, and the load lives in the header block so it
    dominates every exit (the BranchFalse following the load is the
    sole exit edge).  All four safety conditions hold, so the
    instruction moves to the preheader.
    """
    body = [
        ir.Copy(destination="i", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.Index(destination="_ir_t", base="arr", index=0),
        ir.BranchFalse(left="i", operation="<", right=10, target=".end"),
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = loops.hoist_loop_invariants(body)
    load_index = next(index for index, instruction in enumerate(result) if isinstance(instruction, ir.Index))
    loop_label_index = next(
        index for index, instruction in enumerate(result) if isinstance(instruction, ir.Label) and instruction.name == ".loop"
    )
    assert load_index < loop_label_index


def test_hoist_is_deterministic_across_runs() -> None:
    """Identical input bodies produce identical output across runs (no set-iteration leak)."""
    body = [
        ir.Copy(destination="a", source=10),
        ir.Copy(destination="b", source=20),
        ir.Copy(destination="c", source=30),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BinaryOperation(destination="_ir_t1", left="a", right="b", operation="+"),
        ir.BinaryOperation(destination="_ir_t2", left="b", right="c", operation="*"),
        ir.BinaryOperation(destination="_ir_t3", left="_ir_t1", right="_ir_t2", operation="-"),
        ir.BranchFalse(left="_ir_t3", operation="==", right=0, target=".end"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    first = loops.hoist_loop_invariants(body)
    second = loops.hoist_loop_invariants(body)
    assert first == second


def test_hoist_no_loops_returns_body_unchanged() -> None:
    """A straight-line function comes back identical to its input."""
    body = [
        ir.Copy(destination="x", source=1),
        ir.BinaryOperation(destination="_ir_t", left="x", right=2, operation="+"),
        ir.Return(value="_ir_t"),
    ]
    assert loops.hoist_loop_invariants(body) is body


def test_hoist_respects_dependency_order_in_preheader() -> None:
    """A chain ``t1 = a+b; t2 = t1*2`` lands in preheader with t1 before t2."""
    body = [
        ir.Copy(destination="a", source=1),
        ir.Copy(destination="b", source=2),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BinaryOperation(destination="_ir_t2", left="_ir_t1", right=2, operation="*"),
        ir.BinaryOperation(destination="_ir_t1", left="a", right="b", operation="+"),
        ir.BranchFalse(left="_ir_t2", operation="==", right=0, target=".end"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = loops.hoist_loop_invariants(body)
    # Both invariants are hoisted; t1 must appear before t2 in the output.
    t1_index = next(
        index
        for index, instruction in enumerate(result)
        if isinstance(instruction, ir.BinaryOperation) and instruction.destination == "_ir_t1"
    )
    t2_index = next(
        index
        for index, instruction in enumerate(result)
        if isinstance(instruction, ir.BinaryOperation) and instruction.destination == "_ir_t2"
    )
    assert t1_index < t2_index


def test_hoist_skips_function_with_inline_asm() -> None:
    """A function containing ``ir.InlineAsm`` is bypassed entirely."""
    body = [
        ir.Copy(destination="a", source=1),
        ir.InlineAsm(content="nop"),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BinaryOperation(destination="_ir_t", left="a", right=1, operation="+"),
        ir.BranchFalse(left="_ir_t", operation="==", right=0, target=".end"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    assert loops.hoist_loop_invariants(body) is body


def test_hoist_skips_load_when_base_pointer_is_loop_defined() -> None:
    """A load whose base pointer is itself written inside the loop cannot be hoisted.

    ``base`` is the ``ir.Index`` field that names the pointer; the
    ``_operands_invariant`` check only walks ``VALUE_FIELDS`` (which is
    just ``index`` for ``Index``), so the load-specific safety check
    has to re-examine ``base`` directly.
    """
    body = [
        ir.Copy(destination="i", source=0),
        ir.Copy(destination="arr", source=100),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation="<", right=10, target=".end"),
        ir.Copy(destination="arr", source=200),
        ir.Index(destination="_ir_t", base="arr", index=0),
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    assert loops.hoist_loop_invariants(body) is body


def test_hoist_skips_load_when_block_does_not_dominate_every_exit() -> None:
    """A load on a conditional path cannot be lifted past a guard the body would have evaluated.

    The load sits in a block reachable only when ``other != 0``.  The
    loop's other exit edge (``BranchFalse i < 10``) leaves the loop
    without ever passing through the load's block, so speculatively
    executing the load in the preheader would introduce a fault on a
    path the original program never took.
    """
    body = [
        ir.Copy(destination="i", source=0),
        ir.Copy(destination="other", source=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation="<", right=10, target=".end"),
        ir.BranchFalse(left="other", operation="!=", right=0, target=".skip"),
        ir.Index(destination="_ir_t", base="arr", index=0),
        ir.Label(name=".skip"),
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    assert loops.hoist_loop_invariants(body) is body


def test_hoist_skips_load_when_loop_contains_call() -> None:
    """A ``Call`` in the loop body may write through any pointer; ``ir.Index`` stays in the body.

    Alias analysis is out of scope, so any call is treated as a
    possible writer of every memory location the load could see.
    """
    body = [
        ir.Copy(destination="i", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation="<", right=10, target=".end"),
        ir.Index(destination="_ir_t", base="arr", index=0),
        ir.Call(args=(), destination=None, name="side_effect"),
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = loops.hoist_loop_invariants(body)
    # The Index stays in the loop body — no preheader-stage Index appears
    # before the .loop label.
    label_index = next(
        index for index, instruction in enumerate(result) if isinstance(instruction, ir.Label) and instruction.name == ".loop"
    )
    indices_before_loop = [index for index, instruction in enumerate(result) if isinstance(instruction, ir.Index) and index < label_index]
    assert indices_before_loop == []


def test_hoist_skips_load_when_loop_contains_index_assign() -> None:
    """An ``IndexAssign`` in the loop body could alias the load through any pointer; the load stays.

    Even when the assign writes through a different base name, the
    conservative analysis treats every ``IndexAssign`` as a possible
    writer of the location the load reads.
    """
    body = [
        ir.Copy(destination="i", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation="<", right=10, target=".end"),
        ir.Index(destination="_ir_t", base="arr", index=0),
        ir.IndexAssign(base="other", index=0, source=5),
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = loops.hoist_loop_invariants(body)
    label_index = next(
        index for index, instruction in enumerate(result) if isinstance(instruction, ir.Label) and instruction.name == ".loop"
    )
    indices_before_loop = [index for index, instruction in enumerate(result) if isinstance(instruction, ir.Index) and index < label_index]
    assert indices_before_loop == []


def test_hoist_treats_block_defined_local_as_loop_defined() -> None:
    """A name declared by ``ir.Block(VarDecl)`` inside the loop is NOT mistakenly treated as invariant.

    Without this rule, a use of the Block-declared name in a
    ``BinaryOperation`` would be hoisted to the preheader, ahead of the
    Block(VarDecl) that introduces the name to the codegen's scope.
    """
    local_decl = ast_nodes.VarDecl(init=ast_nodes.Int(value=5), line=1, name="hi", type_name="int")
    body = [
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.Block(node=local_decl),
        ir.BinaryOperation(destination="_ir_t", left="hi", right=4, operation="<<"),
        ir.BranchFalse(left="_ir_t", operation="==", right=0, target=".end"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = loops.hoist_loop_invariants(body)
    # No preheader inserted (or, if inserted, it carries no BinaryOperation):
    # the BinaryOp stays in the loop body, after the Block(VarDecl).
    block_index = next(index for index, instruction in enumerate(result) if isinstance(instruction, ir.Block))
    binary_index = next(index for index, instruction in enumerate(result) if isinstance(instruction, ir.BinaryOperation))
    assert binary_index > block_index


def test_hoist_variant_binary_operation_stays_in_body() -> None:
    """A BinaryOp whose operand is defined inside the loop is not moved."""
    body = [
        ir.Copy(destination="a", source=10),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.Copy(destination="b", source=1),  # 'b' defined inside loop
        ir.BinaryOperation(destination="_ir_t", left="a", right="b", operation="+"),
        ir.BranchFalse(left="_ir_t", operation="==", right=0, target=".end"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    assert loops.hoist_loop_invariants(body) is body


def test_hoist_scans_carry_branch_terminator_for_address_taken_locals() -> None:
    """Address-taken locals passed to a ``CarryBranch``-wrapped call are loop-defined.

    Regression: ``_names_defined_in_loop`` only walked
    ``block.instructions``, missing the AST inside
    :class:`cc.ir.CarryBranch` terminators.  A local declared in the
    function and passed via :class:`cc.ast_nodes.AddressOf` to a
    carry-return callee inside the loop was therefore mis-classified
    as loop-external, and a subsequent ``BinaryOperation`` reading
    that local was hoisted ahead of the call.  Caught by
    ``kernel/fs/fd/fs.c::fd_read_file``, whose
    ``chunk = 512 - byte_offset;`` was lifted past the
    ``vfs_read_sec(&byte_offset, …)`` that fills ``byte_offset`` each
    iteration.
    """
    call_ast = ast_nodes.Call(
        line=1,
        args=[ast_nodes.AddressOf(line=1, var=ast_nodes.Var(line=1, name="byte_offset")), ast_nodes.Var(line=1, name="pointer")],
        name="vfs_read_sec",
    )
    body = [
        ir.Copy(destination="byte_offset", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.CarryBranch(call_ast=call_ast, target=".kont", when="clear"),
        ir.Return(value=0),
        ir.Label(name=".kont"),
        ir.BinaryOperation(destination="_ir_t", left=512, operation="-", right="byte_offset"),
        ir.BranchFalse(left="_ir_t", operation="==", right=0, target=".end"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value="_ir_t"),
    ]
    assert loops.hoist_loop_invariants(body) is body


def test_hoist_treats_excluded_global_as_loop_defined_across_a_call() -> None:
    """A global named in ``excluded_names`` is loop-variant when the body has a call.

    The shell's chain-execute loop reads a global ``last_exec_status``
    in a comparison and then runs ``execute_pipeline()`` — which writes
    that global through its side-effect chain.  Without this rule LICM
    would hoist ``last_exec_status == 0`` ahead of the loop, freezing
    the comparison to whatever value the global held at entry and
    breaking shell chain operators.
    """
    body = [
        ir.Copy(destination="x", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BinaryOperation(destination="_ir_t", left="last_exec_status", operation="==", right=0),
        ir.Call(args=(), destination=None, name="execute_pipeline"),
        ir.BranchFalse(left="_ir_t", operation="==", right=0, target=".end"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = loops.hoist_loop_invariants(body, excluded_names=frozenset({"last_exec_status"}))
    assert result is body


def test_hoist_treats_increment_decrement_target_as_loop_defined() -> None:
    """A name written by ``ir.Block(IncrementDecrement)`` inside the loop is NOT mistakenly treated as invariant.

    The AST node's writing-field is ``target_name``, not ``name`` — the
    invariance-defs scan must reach it via the conservative
    every-string walk, not a literal "name"-only match.  Without this,
    the qsort do-while ``do { i++; } while (cmp(a + i*size, …) < 0);``
    has ``i * size`` and ``a + i * size`` wrongly classified as
    invariant and hoisted past the i++ that defines i for each
    iteration.
    """
    incdec = ast_nodes.IncrementDecrement(delta=1, is_postfix=True, line=1, target_name="i")
    body = [
        ir.Copy(destination="a", source=0),
        ir.Copy(destination="size", source=4),
        ir.Copy(destination="i", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.Block(node=incdec),
        ir.BinaryOperation(destination="_ir_t", left="i", right="size", operation="*"),
        ir.BranchFalse(left="_ir_t", operation="==", right=0, target=".loop"),
        ir.Return(value=None),
    ]
    result = loops.hoist_loop_invariants(body)
    # The BinaryOp stays in the loop body — after the Block(IncrementDecrement)
    # and before the loop-back branch.  No preheader created (because nothing
    # was hoistable), so the function should be returned unchanged.
    assert result is body


def test_self_loop_body_does_not_swallow_entry_block() -> None:
    """A self-loop's body is just ``{header}`` — the entry block must NOT be pulled in.

    Regression: walking predecessors from a self-looping latch (where
    ``latch is header``) used to step out of the loop via the latch's
    other predecessors (the entry fall-through into the header),
    incorrectly classifying pre-loop instructions as loop-body
    candidates for LICM to hoist.  Manifested as use-before-def in
    kernel/drivers/ata.c, where ``saved_lba = lba & 0xFFFF;`` (above
    a ``while (1) { … }``) had its right-hand side hoisted past its
    own Copy use.
    """
    body = [
        ir.Copy(destination="saved", source=1),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="status", operation="==", right=0, target=".loop"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    found = loops.natural_loops(graph)
    assert len(found) == 1
    loop = found[0]
    entry = graph.entry
    header = graph.label_to_block[".loop"]
    assert header is loop.header
    assert entry is not header
    assert entry not in loop.body
    assert loop.body == frozenset({header})


def test_branch_false_target_predecessor_retargets_to_preheader() -> None:
    """A predecessor whose ``BranchFalse`` names the header has its target rewritten to the preheader."""
    body = [
        ir.BranchFalse(left="x", operation="==", right=0, target=".loop"),
        ir.Return(value=None),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="y", operation="==", right=0, target=".end"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    found = loops.natural_loops(graph)
    preheaders = loops.insert_preheaders(graph, loops=found)
    preheader = preheaders[found[0]]
    # The entry's BranchFalse(target=".loop") now names the preheader.
    entry_terminator = graph.entry.terminator
    assert isinstance(entry_terminator, ir.BranchFalse)
    assert entry_terminator.target == preheader.label


def test_do_while_loop_header_is_single_predecessor_join() -> None:
    """A do-while body's header is reached only via the back-edge after entry — single latch."""
    body = [
        ir.Label(name=".body"),
        ir.Copy(destination="x", source=1),
        ir.BranchFalse(left="x", operation="==", right=0, target=".body"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    result = loops.natural_loops(graph)
    assert len(result) == 1
    loop = result[0]
    assert loop.header is graph.label_to_block[".body"]
    assert loop.latches == frozenset({graph.label_to_block[".body"]})
    # Self-loop: body is just the header.
    assert loop.body == frozenset({graph.label_to_block[".body"]})


def test_insert_preheader_for_simple_while_loop_routes_entry_through_preheader() -> None:
    """A simple while-loop's preheader becomes the unique non-latch predecessor of the header."""
    body = [
        ir.Copy(destination="seed", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="x", operation="==", right=0, target=".end"),
        ir.Copy(destination="x", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    found = loops.natural_loops(graph)
    preheaders = loops.insert_preheaders(graph, loops=found)
    loop = found[0]
    header = graph.label_to_block[".loop"]
    preheader = preheaders[loop]
    assert preheader.successors == [header]
    # Header now has exactly two predecessors: preheader (from outside)
    # and the latch (the back-edge).
    latch = next(iter(loop.latches))
    assert set(header.predecessors) == {preheader, latch}
    # Preheader is positioned immediately before header in source order.
    assert graph.blocks.index(preheader) == graph.blocks.index(header) - 1
    # The entry's Jump(target=".loop") was retargeted to the preheader.
    entry_terminator = graph.entry.terminator
    assert isinstance(entry_terminator, ir.Jump)
    assert entry_terminator.target == preheader.label


def test_insert_preheader_latches_with_fall_through_to_header_get_explicit_jump() -> None:
    """A latch positionally adjacent to header with no terminator gets an explicit ``Jump`` before insertion.

    Otherwise its fall-through would silently route through the
    inserted preheader, causing hoisted invariants to execute every
    iteration.  The latch we construct here is an ``InlineAsm``-free
    block with no terminator that loops back via positional fall-through.
    """
    body = [
        ir.Label(name=".entry"),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.Copy(destination="x", source=1),
        # Latch block: no terminator, falls through positionally to .loop_2 then back to .loop.
        # Use a structure where header (.loop) is preceded by a fall-through-only latch.
    ]
    # Build a CFG where a block immediately before header falls through to it.
    body = [
        ir.Jump(target=".loop"),
        ir.Label(name=".prelatch"),
        # no terminator — falls through to .loop, which is the loop header below
        ir.Label(name=".loop"),
        ir.BranchFalse(left="y", operation="==", right=0, target=".end"),
        ir.Jump(target=".prelatch"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    found = loops.natural_loops(graph)
    # The latch ".prelatch" has no terminator and is positionally before .loop.
    prelatch = graph.label_to_block[".prelatch"]
    assert prelatch.terminator is None
    loops.insert_preheaders(graph, loops=found)
    # After insertion, prelatch has an explicit Jump to .loop (materialized).
    assert isinstance(prelatch.terminator, ir.Jump)
    assert prelatch.terminator.target == ".loop"


def test_insert_preheader_returns_empty_when_header_has_no_non_latch_predecessors() -> None:
    """A loop whose only predecessor of the header is a latch (e.g. infinite self-loop in entry) yields no preheader."""
    body = [
        ir.Label(name=".entry"),
        ir.Jump(target=".entry"),  # self-loop on entry
    ]
    graph = cfg.build_cfg(_function(body).body)
    found = loops.natural_loops(graph)
    preheaders = loops.insert_preheaders(graph, loops=found)
    assert preheaders == {}
    # CFG is unchanged: still one block.
    assert len(graph.blocks) == 1


def test_insert_preheaders_for_nested_loops_inserts_two_preheaders() -> None:
    """Outer and inner loops each receive their own preheader."""
    body = [
        ir.Jump(target=".outer"),  # entry block reaches the outer header from outside
        ir.Label(name=".outer"),
        ir.BranchFalse(left="x", operation="==", right=0, target=".outer_end"),
        ir.Label(name=".inner"),
        ir.BranchFalse(left="y", operation="==", right=0, target=".inner_end"),
        ir.Jump(target=".inner"),
        ir.Label(name=".inner_end"),
        ir.Jump(target=".outer"),
        ir.Label(name=".outer_end"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    found = loops.natural_loops(graph)
    preheaders = loops.insert_preheaders(graph, loops=found)
    assert len(preheaders) == 2
    # Two distinct preheaders, one per loop, in source order.
    outer_loop = next(loop for loop in found if loop.header.label == ".outer")
    inner_loop = next(loop for loop in found if loop.header.label == ".inner")
    assert preheaders[outer_loop] is not preheaders[inner_loop]


def test_irreducible_back_edge_target_does_not_dominate_source_no_loop() -> None:
    """If the back-edge target does not dominate the source, no natural loop is reported.

    Construct an irreducible region: two blocks both reached from entry,
    each with an edge to the other.  Neither dominates the other, so
    neither inter-block edge qualifies as a back-edge.
    """
    body = [
        ir.BranchFalse(left="x", operation="==", right=0, target=".B"),
        ir.Label(name=".A"),
        ir.Jump(target=".B"),
        ir.Label(name=".B"),
        ir.Jump(target=".A"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    # The .A → .B → .A cycle would be a loop, but entry → .B bypasses .A,
    # so .A does not dominate .B (the back-edge target's dominator).
    # natural_loops should detect: .B → .A is a back-edge only if .A dominates .B.
    # Since entry → .B exists without going through .A, .A does not dominate .B.
    result = loops.natural_loops(graph)
    assert result == []


def test_linear_function_has_no_loops() -> None:
    """A straight-line function produces no natural loops."""
    body = [
        ir.Copy(destination="x", source=1),
        ir.Return(value="x"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    assert loops.natural_loops(graph) == []


def test_loop_with_multiple_continues_has_multiple_latches() -> None:
    """A loop with two back-edges (multiple ``continue`` paths) coalesces into one loop with two latches."""
    body = [
        ir.Label(name=".loop"),
        ir.BranchFalse(left="x", operation="==", right=0, target=".end"),
        ir.BranchFalse(left="y", operation="==", right=0, target=".other_latch"),
        ir.Jump(target=".loop"),
        ir.Label(name=".other_latch"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    result = loops.natural_loops(graph)
    assert len(result) == 1
    loop = result[0]
    assert loop.header is graph.label_to_block[".loop"]
    # Two latches: the fall-through Jump and .other_latch.
    assert len(loop.latches) == 2
    assert graph.label_to_block[".other_latch"] in loop.latches


def test_loop_with_multiple_exits_records_each_exit_block() -> None:
    """Each body block with an out-of-body successor appears in ``exits``."""
    body = [
        ir.Label(name=".loop"),
        ir.BranchFalse(left="x", operation="==", right=0, target=".end1"),
        ir.BranchFalse(left="y", operation="==", right=0, target=".end2"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end1"),
        ir.Return(value=None),
        ir.Label(name=".end2"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    result = loops.natural_loops(graph)
    assert len(result) == 1
    loop = result[0]
    # Both BranchFalse blocks have an out-of-body successor.
    assert graph.label_to_block[".loop"] in loop.exits
    fall_through = next(succ for succ in graph.label_to_block[".loop"].successors if succ.label != ".end1")
    assert fall_through in loop.exits


def test_loops_ordered_by_header_position_in_blocks() -> None:
    """``natural_loops`` returns loops sorted by header position in ``cfg.blocks``."""
    body = [
        # Outer loop wraps inner loop.
        ir.Label(name=".outer"),
        ir.BranchFalse(left="x", operation="==", right=0, target=".outer_end"),
        ir.Label(name=".inner"),
        ir.BranchFalse(left="y", operation="==", right=0, target=".inner_end"),
        ir.Jump(target=".inner"),
        ir.Label(name=".inner_end"),
        ir.Jump(target=".outer"),
        ir.Label(name=".outer_end"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    result = loops.natural_loops(graph)
    assert [loop.header.label for loop in result] == [".outer", ".inner"]


def test_lsr_constant_times_iv_handles_commutative_form() -> None:
    """An ``i * c`` candidate triggers strength reduction with the IV on either operand side."""
    body = [
        ir.Copy(destination="i", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation="<", right=10, target=".end"),
        ir.BinaryOperation(destination="prod", left=5, operation="*", right="i"),
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = loops.reduce_loop_strength(body)
    # The ``5 * i`` multiply is replaced by a ``Copy(prod, _ir_lsr_acc_*)``.
    # No remaining ``BinaryOperation`` writes to ``prod``.
    multiplies_to_prod = [
        instruction for instruction in result if isinstance(instruction, ir.BinaryOperation) and instruction.destination == "prod"
    ]
    assert multiplies_to_prod == []
    accumulator_copies = [
        instruction
        for instruction in result
        if isinstance(instruction, ir.Copy)
        and instruction.destination == "prod"
        and isinstance(instruction.source, str)
        and instruction.source.startswith("_ir_lsr_acc_")
    ]
    assert len(accumulator_copies) == 1


def test_lsr_iv_times_constant_replaces_multiply_with_accumulator() -> None:
    """A ``T = i * k`` inside a counted loop becomes a ``Copy(T, _lsr_acc)`` driven by an accumulator.

    The preheader gets a single ``_lsr_acc = i * k`` multiply (run once
    before the loop), and the body's per-iteration multiply collapses to
    a copy.  The accumulator increments by ``step * k`` after every
    update of the IV.
    """
    body = [
        ir.Copy(destination="i", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation="<", right=10, target=".end"),
        ir.BinaryOperation(destination="prod", left="i", operation="*", right=5),
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = loops.reduce_loop_strength(body)
    multiplies = [instruction for instruction in result if isinstance(instruction, ir.BinaryOperation) and instruction.operation == "*"]
    # Exactly one multiply survives — the preheader initialization
    # ``_lsr_acc = i * 5`` — and it writes to a fresh accumulator name
    # rather than the original ``prod``.
    assert len(multiplies) == 1
    assert multiplies[0].destination.startswith("_ir_lsr_acc_")
    assert multiplies[0].left == "i"
    assert multiplies[0].right == 5
    # The body's original multiply was replaced by a Copy of the accumulator.
    prod_copies = [instruction for instruction in result if isinstance(instruction, ir.Copy) and instruction.destination == "prod"]
    assert len(prod_copies) == 1
    assert prod_copies[0].source == multiplies[0].destination
    # The accumulator increments by step * k = 1 * 5 = 5 each iteration.
    accumulator_increments = [
        instruction
        for instruction in result
        if isinstance(instruction, ir.BinaryOperation)
        and instruction.operation == "+"
        and instruction.destination.startswith("_ir_lsr_acc_")
        and instruction.right == 5
    ]
    assert len(accumulator_increments) == 1


def test_lsr_iv_update_via_block_assign_qualifies_as_induction_variable() -> None:
    """The IR builder routes ``i = i + 1`` through ``Block(node=Assign(...))`` for tighter codegen.

    LSR must recognise that AST-escape-hatch form as a self-modify IV
    update — otherwise every real for-loop misses out, since the
    builder emits the Block form whenever the assignment's
    right-hand side is ``i op K``.
    """
    iv_update = ir.Block(
        node=ast_nodes.Assign(
            expr=ast_nodes.BinaryOperation(
                left=ast_nodes.Var(line=1, name="i"),
                line=1,
                operation="+",
                right=ast_nodes.Int(line=1, value=1),
            ),
            line=1,
            name="i",
        )
    )
    body = [
        ir.Copy(destination="i", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation="<", right=10, target=".end"),
        ir.BinaryOperation(destination="prod", left="i", operation="*", right=5),
        iv_update,
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = loops.reduce_loop_strength(body)
    # The Block IV update is preserved verbatim; an accumulator increment
    # is inserted *after* it.
    block_index = next(index for index, instruction in enumerate(result) if instruction is iv_update)
    next_instruction = result[block_index + 1]
    assert isinstance(next_instruction, ir.BinaryOperation)
    assert next_instruction.left == next_instruction.destination
    assert next_instruction.destination.startswith("_ir_lsr_acc_")
    assert next_instruction.right == 5


def test_lsr_negative_step_iv_increments_accumulator_by_negative_product() -> None:
    """A downward IV (``i = i - 1``) increments the accumulator by ``-step * k``."""
    body = [
        ir.Copy(destination="i", source=10),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation=">", right=0, target=".end"),
        ir.BinaryOperation(destination="prod", left="i", operation="*", right=4),
        ir.BinaryOperation(destination="i", left="i", operation="-", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = loops.reduce_loop_strength(body)
    accumulator_updates = [
        instruction
        for instruction in result
        if isinstance(instruction, ir.BinaryOperation)
        and instruction.operation == "+"
        and instruction.destination.startswith("_ir_lsr_acc_")
        and instruction.left == instruction.destination
    ]
    assert len(accumulator_updates) == 1
    assert accumulator_updates[0].right == -4


def test_lsr_no_loops_returns_body_unchanged() -> None:
    """A straight-line function has nothing for LSR to do; the body is returned unchanged."""
    body = [
        ir.Copy(destination="x", source=5),
        ir.Return(value=None),
    ]
    assert loops.reduce_loop_strength(body) is body


def test_lsr_skips_loop_with_no_induction_variable() -> None:
    """A loop whose counter has multiple in-loop definitions has no recognized IV; LSR no-ops."""
    body = [
        ir.Copy(destination="i", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation="<", right=10, target=".end"),
        ir.BinaryOperation(destination="prod", left="i", operation="*", right=5),
        # Two writes to ``i`` inside the loop — no single-def IV.
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    assert loops.reduce_loop_strength(body) is body


def test_lsr_skips_when_multiply_destination_has_multiple_defs() -> None:
    """A multiply target with more than one definition in the function blocks LSR for that candidate."""
    body = [
        ir.Copy(destination="i", source=0),
        ir.Copy(destination="prod", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation="<", right=10, target=".end"),
        ir.BinaryOperation(destination="prod", left="i", operation="*", right=5),
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    # ``prod`` is defined twice (initial ``Copy(prod, 0)`` plus the
    # multiply), so we can't safely replace the multiply with a Copy
    # without disturbing the other write.  LSR no-ops.
    assert loops.reduce_loop_strength(body) is body


def test_nested_loops_are_detected_separately() -> None:
    """An inner loop is reported as a distinct :class:`NaturalLoop` from its outer loop."""
    body = [
        ir.Label(name=".outer"),
        ir.BranchFalse(left="x", operation="==", right=0, target=".outer_end"),
        ir.Label(name=".inner"),
        ir.BranchFalse(left="y", operation="==", right=0, target=".inner_end"),
        ir.Jump(target=".inner"),
        ir.Label(name=".inner_end"),
        ir.Jump(target=".outer"),
        ir.Label(name=".outer_end"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    result = loops.natural_loops(graph)
    assert len(result) == 2
    by_header = {loop.header.label: loop for loop in result}
    inner = by_header[".inner"]
    outer = by_header[".outer"]
    # Inner body is a subset of outer body.
    assert inner.body < outer.body
    # Inner header is in outer body but not vice versa.
    assert graph.label_to_block[".inner"] in outer.body
    assert graph.label_to_block[".outer"] not in inner.body


def test_simple_while_loop_has_one_latch_and_one_exit() -> None:
    """A ``while (cond) body;`` loop: header dominates body+exit, single latch, single exit."""
    body = [
        ir.Label(name=".loop"),
        ir.BranchFalse(left="x", operation="==", right=0, target=".end"),
        ir.Copy(destination="x", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    result = loops.natural_loops(graph)
    assert len(result) == 1
    loop = result[0]
    assert loop.header is graph.label_to_block[".loop"]
    # Latch is the body block (the fall-through after BranchFalse, which Jumps back).
    fall_through = next(succ for succ in graph.label_to_block[".loop"].successors if succ.label != ".end")
    assert loop.latches == frozenset({fall_through})
    # Body = {header, fall-through}.
    assert loop.body == frozenset({graph.label_to_block[".loop"], fall_through})
    # Header is the only exit (its BranchFalse leaves the body).
    assert loop.exits == frozenset({graph.label_to_block[".loop"]})
