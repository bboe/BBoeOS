"""Tests for cc.cfg — basic-block construction + dominator analysis.

Each test builds a small flat IR by hand (avoiding the AST + Builder
roundtrip) so the expected CFG shape is obvious from the test source.
"""

from __future__ import annotations

from cc import ast_nodes, cfg, ir


def _function(body: list[ir.Instruction]) -> ir.Function:
    """Wrap *body* in a minimal :class:`ir.Function` for CFG construction."""
    ast = ast_nodes.Function(
        body=[],
        line=1,
        name="f",
        params=[],
    )
    return ir.Function(ast_node=ast, body=body, strings=[])


def test_branch_false_has_target_and_fall_through_successors() -> None:
    """``BranchFalse`` makes the block have two successors: branch target + fall-through."""
    body = [
        ir.BranchFalse(left="x", operation="==", right=0, target=".else"),
        ir.Copy(destination="y", source=1),
        ir.Jump(target=".end"),
        ir.Label(name=".else"),
        ir.Copy(destination="y", source=2),
        ir.Label(name=".end"),
        ir.Return(value="y"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    # Entry block ends with BranchFalse → successors = [.else, <fallthrough>]
    entry = graph.entry
    assert isinstance(entry.terminator, ir.BranchFalse)
    assert len(entry.successors) == 2
    target_names = {block.label for block in entry.successors}
    assert ".else" in target_names


def test_compute_dominance_frontiers_at_diamond_join() -> None:
    """At the merge block of an if/else diamond, both arm blocks have the merge in their frontier."""
    body = [
        ir.BranchFalse(left="x", operation="==", right=0, target=".else"),
        ir.Copy(destination="y", source=1),
        ir.Jump(target=".end"),
        ir.Label(name=".else"),
        ir.Copy(destination="y", source=2),
        ir.Label(name=".end"),
        ir.Return(value="y"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    idom = cfg.compute_dominators(graph)
    frontiers = cfg.compute_dominance_frontiers(idom)
    end_block = graph.label_to_block[".end"]
    then_block = graph.entry.successors[1]  # fall-through arm (the BranchFalse's else target is index 0)
    else_block = graph.label_to_block[".else"]
    # Both arm blocks have .end on their dominance frontier (control re-converges there).
    assert end_block in frontiers[then_block]
    assert end_block in frontiers[else_block]


def test_compute_dominators_diamond_entry_dominates_join() -> None:
    """In a diamond (if/else), the entry dominates the merge block; neither arm does."""
    body = [
        ir.BranchFalse(left="x", operation="==", right=0, target=".else"),
        ir.Copy(destination="y", source=1),
        ir.Jump(target=".end"),
        ir.Label(name=".else"),
        ir.Copy(destination="y", source=2),
        ir.Label(name=".end"),
        ir.Return(value="y"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    idom = cfg.compute_dominators(graph)
    end_block = graph.label_to_block[".end"]
    assert idom[end_block] is graph.entry


def test_compute_dominators_entry_is_self_dominator() -> None:
    """The entry block is its own immediate dominator by the CHK convention."""
    body = [ir.Return(value=None)]
    graph = cfg.build_cfg(_function(body).body)
    idom = cfg.compute_dominators(graph)
    assert idom[graph.entry] is graph.entry


def test_compute_dominators_loop_header_dominates_body() -> None:
    """In a while loop, the loop header dominates the body and exit blocks."""
    body = [
        ir.Label(name=".loop"),
        ir.BranchFalse(left="x", operation="==", right=0, target=".end"),
        ir.Copy(destination="x", source=0),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    idom = cfg.compute_dominators(graph)
    loop_block = graph.label_to_block[".loop"]
    end_block = graph.label_to_block[".end"]
    # Loop header dominates its body (the fall-through of the BranchFalse)
    # and the exit block.
    body_block = next(succ for succ in loop_block.successors if succ is not end_block)
    assert idom[body_block] is loop_block
    assert idom[end_block] is loop_block


def test_dead_code_after_jump_becomes_its_own_unreachable_block() -> None:
    """Instructions after an unconditional ``Jump`` start a new BB even with no Label."""
    body = [
        ir.Copy(destination="x", source=1),
        ir.Jump(target=".target"),
        ir.Copy(destination="dead", source=42),
        ir.Label(name=".target"),
        ir.Return(value="x"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    # 3 blocks: entry (ends in Jump), dead block (synthetic label), target.
    assert len(graph.blocks) == 3
    dead = graph.blocks[1]
    assert dead.label.startswith("<fallthrough_")
    assert dead.predecessors == []  # unreachable


def test_function_starting_with_label_uses_label_as_entry_name() -> None:
    """An IR function whose first instruction is a Label uses that name for the entry BB."""
    body = [
        ir.Label(name=".start"),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    assert graph.entry.label == ".start"


def test_linear_function_is_single_basic_block() -> None:
    """A function with no branches lowers to one BB containing every instruction."""
    body = [
        ir.Copy(destination="x", source=1),
        ir.Copy(destination="y", source=2),
        ir.Return(value="x"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    assert len(graph.blocks) == 1
    only = graph.entry
    assert only.label == "<entry>"
    assert len(only.instructions) == 2
    assert isinstance(only.terminator, ir.Return)
    assert only.successors == []


def test_loop_boundary_metadata_stays_inside_block_no_edge_effect() -> None:
    """:class:`cc.ir.LoopBoundary` is preserved as a regular instruction; no successor changes."""
    body = [
        ir.Label(name=".loop"),
        ir.LoopBoundary(continue_label=".loop", end_label=".end", push=True),
        ir.Copy(destination="x", source=1),
        ir.LoopBoundary(continue_label=".loop", end_label=".end", push=False),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value="x"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    loop_block = graph.label_to_block[".loop"]
    # Both LoopBoundary markers + the Copy live in the BB; the Jump is the terminator.
    assert len(loop_block.instructions) == 3
    assert isinstance(loop_block.terminator, ir.Jump)
    assert loop_block.successors == [loop_block]


def test_predecessors_are_set_inversely_to_successors() -> None:
    """For every edge ``A → B`` populated as a successor, ``B`` lists ``A`` as a predecessor."""
    body = [
        ir.BranchFalse(left="x", operation="==", right=0, target=".target"),
        ir.Copy(destination="y", source=1),
        ir.Label(name=".target"),
        ir.Return(value="y"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    for block in graph.blocks:
        for successor in block.successors:
            assert block in successor.predecessors


def test_return_block_has_no_successors() -> None:
    """A block ending in ``Return`` exits the function; no successors are recorded."""
    body = [
        ir.Copy(destination="x", source=1),
        ir.Return(value="x"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    assert graph.entry.successors == []


def test_switch_treated_as_opaque_falls_through_to_next_block() -> None:
    """A :class:`cc.ir.Switch` instruction is treated as straight-line; control falls through."""
    ast_switch = ast_nodes.Switch(cases=[], discriminant=ast_nodes.Int(value=0), line=1)
    body = [
        ir.Switch(cases=[], discriminant=ast_nodes.Int(value=0), end_label=".swend", original_ast=ast_switch),
        ir.Return(value=None),
    ]
    graph = cfg.build_cfg(_function(body).body)
    # Single BB: Switch (in instructions) + Return (terminator).
    assert len(graph.blocks) == 1
    assert len(graph.entry.instructions) == 1
    assert isinstance(graph.entry.instructions[0], ir.Switch)


def test_tail_call_block_has_no_successors() -> None:
    """A block ending in ``TailCall`` exits the function; no successors are recorded."""
    body = [
        ir.TailCall(args=(1, 2), name="helper"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    assert isinstance(graph.entry.terminator, ir.TailCall)
    assert graph.entry.successors == []


def test_unreachable_block_excluded_from_dominator_map() -> None:
    """A block with no path from entry doesn't get an idom entry."""
    body = [
        ir.Copy(destination="x", source=1),
        ir.Jump(target=".target"),
        ir.Copy(destination="dead", source=42),  # unreachable BB
        ir.Label(name=".target"),
        ir.Return(value="x"),
    ]
    graph = cfg.build_cfg(_function(body).body)
    idom = cfg.compute_dominators(graph)
    dead = graph.blocks[1]
    assert dead not in idom
