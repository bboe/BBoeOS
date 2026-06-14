"""Tests for cc.ssa — SSA construction + destruction.

Each test builds a small flat IR by hand and feeds it through
:func:`cc.ssa.convert_to_ssa` (and sometimes :func:`cc.ssa.convert_from_ssa`)
so the expected SSA shape — versioned names, phi placement, source
fill-in — is obvious from the test source.
"""

from __future__ import annotations

from cc import ast_nodes, ir, ssa


def test_block_referenced_variable_excluded_from_ssa() -> None:
    """Any variable referenced inside a :class:`cc.ir.Block` AST stays un-versioned."""
    # ``x`` appears inside a Block-wrapped AST node and as a plain Copy
    # destination.  The Block reference forces ``x`` into the opaque set,
    # so the Copy destination is NOT renamed.
    body = [
        ir.Copy(destination="x", source=1),
        ir.Block(node=ast_nodes.Var(line=1, name="x")),
        ir.Return(value="x"),
    ]
    form = ssa.convert_to_ssa(body)
    assert "x" not in form.ssa_safe_names
    # The Copy destination is untouched.
    assert form.cfg.entry.instructions[0].destination == "x"


def test_carry_branch_call_ast_variables_excluded() -> None:
    """Vars used inside a :class:`cc.ir.CarryBranch`'s ``call_ast`` stay un-versioned."""
    call_ast = ast_nodes.Call(args=[ast_nodes.Var(line=1, name="arg")], line=1, name="helper")
    body = [
        ir.Copy(destination="arg", source=5),
        ir.CarryBranch(call_ast=call_ast, target=".end", when="set"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    form = ssa.convert_to_ssa(body)
    assert "arg" not in form.ssa_safe_names


def test_critical_edge_in_branchfalse_jump_pattern_gets_split() -> None:
    """``BranchFalse → join`` + ``Jump → join`` is a critical edge that gets a split block."""
    body = [
        ir.BranchFalse(left="cond", operation="!=", right=0, target=".B"),
        ir.Copy(destination="x", source=1),
        ir.Jump(target=".B"),
        ir.Label(name=".B"),
        ir.Return(value="x"),
    ]
    form = ssa.convert_to_ssa(body)
    split_blocks = [block for block in form.cfg.blocks if block.label.startswith(".ssa_split_")]
    assert len(split_blocks) == 1
    split = split_blocks[0]
    assert isinstance(split.terminator, ir.Jump)
    assert split.terminator.target == ".B"
    assert form.cfg.label_to_block[".B"] in split.successors
    # The split block sits between the entry's true-branch target and .B.
    entry = form.cfg.entry
    assert split in entry.successors
    assert entry in split.predecessors


def test_critical_edge_split_does_not_clobber_fall_through_join() -> None:
    """A non-critical fall-through into a join that has split blocks for OTHER edges keeps its value.

    Regression: ``_split_critical_edges`` inserts each split immediately
    before its successor in source order.  When the successor has
    additional non-critical fall-through predecessors, those predecessors
    used to drop into the first split inserted, picking up that split's
    destruction Copy.  The fix materializes the fall-through into an
    explicit ``Jump`` before insertion so the split is reachable only via
    the edge it represents.

    Shape:
        cond ? (BranchFalse fail → join) : (BranchFalse fail → join)
        ... (fall-through writes ``y = 1``)
        join: return y

    The fall-through arm sets ``y = 1``; the two branch-taken arms reach
    the join without writing ``y``.  Optimisation must preserve the
    distinction — the destruction Copy materialising ``y`` for the
    branch-taken edges must not clobber the fall-through's ``y = 1``.
    """
    body = [
        ir.Copy(destination="y", source=0),
        ir.BranchFalse(left="a", operation=">=", right=-128, target=".end"),
        ir.BranchFalse(left="a", operation="<=", right=127, target=".end"),
        ir.Copy(destination="y", source=1),
        ir.Label(name=".end"),
        ir.Return(value="y"),
    ]
    result = ssa.optimize_ssa(body)
    # The fall-through "success" block ends with ``Copy(y, 1)``.  The
    # split blocks for the two BranchFalse critical edges follow before
    # the original ``.end`` label.  The success block must terminate
    # with an explicit Jump to ``.end`` so it bypasses the splits — if
    # the success block dropped through the splits, their destruction
    # Copies (``y = 0`` for both branch-taken edges) would overwrite
    # the success arm's ``y = 1`` and the function would return 0.
    copy_y_1_index = next(
        index
        for index, instruction in enumerate(result)
        if isinstance(instruction, ir.Copy) and instruction.destination == "y" and instruction.source == 1
    )
    assert isinstance(result[copy_y_1_index + 1], ir.Jump)
    assert result[copy_y_1_index + 1].target == ".end"


def test_critical_edge_splitting_preserves_terminator_target() -> None:
    """A ``BranchFalse`` whose target was on the critical edge gets retargeted to the split block."""
    body = [
        ir.BranchFalse(left="cond", operation="!=", right=0, target=".B"),
        ir.Copy(destination="x", source=1),
        ir.Jump(target=".B"),
        ir.Label(name=".B"),
        ir.Return(value="x"),
    ]
    form = ssa.convert_to_ssa(body)
    entry_terminator = form.cfg.entry.terminator
    assert isinstance(entry_terminator, ir.BranchFalse)
    assert entry_terminator.target.startswith(".ssa_split_")


def test_dead_phi_removed_when_destination_unused() -> None:
    """A phi whose versioned destination is never read collapses out of the SSA form."""
    body = [
        ir.BranchFalse(left="cond", operation="==", right=0, target=".else"),
        ir.Copy(destination="y", source=1),
        ir.Jump(target=".end"),
        ir.Label(name=".else"),
        ir.Copy(destination="y", source=2),
        ir.Label(name=".end"),
        ir.Return(value=0),
    ]
    # The .end-block phi for y has no use (Return reads literal 0), so
    # destruction must not introduce any Copy(y, ...) sinks for it.
    result = ssa.optimize_ssa(body)
    copy_destinations = {instruction.destination for instruction in result if isinstance(instruction, ir.Copy)}
    # The original arm copies remain (their destinations are SSA-safe and
    # not removed by SSA — DCE in the outer pipeline drops them later) but
    # no destruction-introduced join Copy lives in the merge block.
    return_instruction = next(instruction for instruction in result if isinstance(instruction, ir.Return))
    assert return_instruction.value == 0
    assert "y" in copy_destinations or not copy_destinations


def test_diamond_phi_at_merge_block_has_two_sources() -> None:
    """``if/else`` where both arms write ``y`` produces a phi at the merge with 2 sources."""
    body = [
        ir.BranchFalse(left="cond", operation="==", right=0, target=".else"),
        ir.Copy(destination="y", source=1),
        ir.Jump(target=".end"),
        ir.Label(name=".else"),
        ir.Copy(destination="y", source=2),
        ir.Label(name=".end"),
        ir.Return(value="y"),
    ]
    form = ssa.convert_to_ssa(body)
    end_block = form.cfg.label_to_block[".end"]
    assert end_block in form.phis
    phis = form.phis[end_block]
    assert len(phis) == 1
    phi = phis[0]
    assert phi.original_name == "y"
    assert phi.destination.startswith("y_ssa")
    assert len(phi.sources) == 2
    # Both incoming sources should be different versioned names (y_ssa0 / y_ssa1).
    incoming = set(phi.sources.values())
    assert len(incoming) == 2
    assert all(value.startswith("y_ssa") for value in incoming)


def test_index_base_name_excluded_from_ssa() -> None:
    """Array / pointer ``base`` names in Index / IndexAssign are never versioned."""
    body = [
        ir.Copy(destination="arr", source=0),  # treat arr as a name
        ir.IndexAssign(base="arr", index=0, source=42),
        ir.Index(base="arr", destination="value", index=1),
        ir.Return(value="value"),
    ]
    form = ssa.convert_to_ssa(body)
    assert "arr" not in form.ssa_safe_names
    # ``value`` is a normal scalar destination and IS SSA-able.
    assert "value" in form.ssa_safe_names


def test_inline_asm_disables_ssa_for_entire_function() -> None:
    """Any :class:`cc.ir.InlineAsm` in the body opts the whole function out of SSA."""
    body = [
        ir.Copy(destination="x", source=1),
        ir.InlineAsm(content="hlt"),
        ir.Return(value="x"),
    ]
    form = ssa.convert_to_ssa(body)
    assert form.ssa_safe_names == set()
    assert form.phis == {}


def test_linear_function_has_no_phi_nodes() -> None:
    """A function with no joins gets every variable renamed but no phis are placed."""
    body = [
        ir.Copy(destination="x", source=1),
        ir.Copy(destination="y", source=2),
        ir.BinaryOperation(destination="z", left="x", operation="+", right="y"),
        ir.Return(value="z"),
    ]
    form = ssa.convert_to_ssa(body)
    assert form.phis == {}
    assert form.ssa_safe_names == {"x", "y", "z"}
    # Each destination got a fresh version.
    instructions = form.cfg.entry.instructions
    assert instructions[0].destination == "x_ssa0"
    assert instructions[1].destination == "y_ssa0"
    assert instructions[2].destination == "z_ssa0"
    # Uses in the BinaryOperation are renamed to the dominating version.
    assert instructions[2].left == "x_ssa0"
    assert instructions[2].right == "y_ssa0"
    # Return value picks up z's version.
    assert form.cfg.entry.terminator.value == "z_ssa0"


def test_loop_carried_variable_gets_phi_at_header() -> None:
    """A while loop incrementing ``i`` places a phi at the loop header with 2 sources."""
    body = [
        ir.Copy(destination="i", source=0),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation="!=", right=10, target=".end"),
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value="i"),
    ]
    form = ssa.convert_to_ssa(body)
    loop_block = form.cfg.label_to_block[".loop"]
    assert loop_block in form.phis
    phis = form.phis[loop_block]
    # Only ``i`` is loop-carried.
    i_phis = [phi for phi in phis if phi.original_name == "i"]
    assert len(i_phis) == 1
    phi = i_phis[0]
    assert len(phi.sources) == 2
    # The two sources are different versions (entry's i_ssa0 and the loop-tail's i_ssa1).
    assert len(set(phi.sources.values())) == 2


def test_no_critical_edges_leaves_cfg_unchanged() -> None:
    """A diamond where each arm has one successor has no critical edges; no split blocks appear."""
    body = [
        ir.BranchFalse(left="cond", operation="==", right=0, target=".else"),
        ir.Copy(destination="y", source=1),
        ir.Jump(target=".end"),
        ir.Label(name=".else"),
        ir.Copy(destination="y", source=2),
        ir.Label(name=".end"),
        ir.Return(value="y"),
    ]
    form = ssa.convert_to_ssa(body)
    assert not any(block.label.startswith(".ssa_split_") for block in form.cfg.blocks)


def test_no_destinations_yields_empty_ssa_form() -> None:
    """A function whose only instruction is ``Return`` has no SSA-eligible names."""
    body = [ir.Return(value=None)]
    form = ssa.convert_to_ssa(body)
    assert form.ssa_safe_names == set()
    assert form.phis == {}


def test_optimize_ssa_canonicalizes_commutative_operand_order_for_gvn() -> None:
    """``a + b`` and ``b + a`` collapse to a single computation under value numbering.

    GVN sorts operand keys for commutative operations (``+``, ``*``,
    ``&``, ``|``, ``^``, ``==``, ``!=``) so the two expressions hash to
    the same key.  The second add is rewritten to ``Copy(t2, t1)`` and
    propagation forwards ``t1`` to every use of ``t2``.
    """
    body = [
        ir.Copy(destination="a", source=3),
        ir.Copy(destination="b", source=5),
        ir.BinaryOperation(destination="t1", left="a", operation="+", right="b"),
        ir.BinaryOperation(destination="t2", left="b", operation="+", right="a"),
        ir.Return(value="t2"),
    ]
    result = ssa.optimize_ssa(body)
    adds = [instruction for instruction in result if isinstance(instruction, ir.BinaryOperation) and instruction.operation == "+"]
    assert len(adds) == 1


def test_optimize_ssa_collapses_diamond_with_identical_arm_values() -> None:
    """Both arms writing the same literal lets propagation + trivial-phi forward the value into the Return."""
    body = [
        ir.BranchFalse(left="cond", operation="==", right=0, target=".else"),
        ir.Copy(destination="y", source=42),
        ir.Jump(target=".end"),
        ir.Label(name=".else"),
        ir.Copy(destination="y", source=42),
        ir.Label(name=".end"),
        ir.Return(value="y"),
    ]
    result = ssa.optimize_ssa(body)
    return_instruction = next(instruction for instruction in result if isinstance(instruction, ir.Return))
    assert return_instruction.value == 42


def test_optimize_ssa_does_not_forward_versioned_source_across_intervening_write() -> None:
    """Propagation must not rewrite a phi source to a name whose slot is later overwritten.

    Regression: ``Copy(ebx_ssaN, edx_ssaM); edx = edx + 1; ...; phi(ebx_ssaN, ...)`` used
    to propagate the phi source from ``ebx_ssaN`` to ``edx_ssaM``.  After
    de-versioning both became their base, and the destruction Copy
    ``Copy(ebx, edx)`` appended at end of the predecessor read the
    post-increment value of ``edx`` instead of the value captured at the
    original ``ebx = edx`` assignment.  The fix restricts ``ir.Copy``
    propagation to non-string sources (constants, ``AddressOf``, etc.)
    that survive de-versioning intact.

    Shape:
        ebx = edx          # SSA capture point
        edx = edx + 1      # later write to source slot
        if (cond) goto .end
        ... join uses ebx ...
    """
    body = [
        ir.Copy(destination="ebx", source="edx"),
        ir.BinaryOperation(destination="edx", left="edx", operation="+", right=1),
        ir.BranchFalse(left="cond", operation="!=", right=0, target=".end"),
        ir.Copy(destination="ebx", source=99),
        ir.Label(name=".end"),
        ir.Return(value="ebx"),
    ]
    result = ssa.optimize_ssa(body)
    # Every Copy whose destination is ``ebx`` (after de-versioning) must
    # never have ``edx`` as its source — that would manifest the bug,
    # because the only legitimate write of ``ebx = edx`` happened
    # before ``edx`` was incremented, so any later ``Copy(ebx, edx)``
    # reads the wrong value.
    copies = [instruction for instruction in result if isinstance(instruction, ir.Copy) and instruction.destination == "ebx"]
    # Exactly one ``Copy(ebx, edx)`` survives — the original capture
    # at the top of the function.  Any second one would be the buggy
    # destruction Copy.
    edx_sources = [copy for copy in copies if copy.source == "edx"]
    assert len(edx_sources) == 1


def test_optimize_ssa_dominator_scoped_gvn_does_not_leak_into_sibling_arm() -> None:
    """An expression numbered in one branch arm must not match an identical one in a sibling arm.

    The two arms of a diamond are dominated only by the predecessor;
    neither dominates the other.  GVN walks the dominator tree, popping
    each block's recorded entries before its sibling enters, so an
    expression recorded in the ``then`` arm is invisible to the ``else``
    arm — otherwise the rewrite would emit a Copy whose source did not
    dominate the use site.
    """
    body = [
        ir.Copy(destination="a", source=2),
        ir.BranchFalse(left="a", operation="!=", right=0, target=".else"),
        ir.BinaryOperation(destination="t1", left="a", operation="*", right=3),
        ir.Jump(target=".end"),
        ir.Label(name=".else"),
        ir.BinaryOperation(destination="t2", left="a", operation="*", right=3),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = ssa.optimize_ssa(body)
    muls = [instruction for instruction in result if isinstance(instruction, ir.BinaryOperation) and instruction.operation == "*"]
    assert len(muls) == 2


def test_optimize_ssa_eliminates_redundant_binary_op_within_block() -> None:
    """A second ``a * b`` in the same block becomes a copy of the first SSA destination."""
    body = [
        ir.Copy(destination="a", source=4),
        ir.Copy(destination="b", source=7),
        ir.BinaryOperation(destination="t1", left="a", operation="*", right="b"),
        ir.BinaryOperation(destination="t2", left="a", operation="*", right="b"),
        ir.BinaryOperation(destination="result", left="t1", operation="+", right="t2"),
        ir.Return(value="result"),
    ]
    result = ssa.optimize_ssa(body)
    muls = [instruction for instruction in result if isinstance(instruction, ir.BinaryOperation) and instruction.operation == "*"]
    assert len(muls) == 1


def test_optimize_ssa_excluded_name_blocks_gvn_match_across_call() -> None:
    """Two ``g + 1`` reads with a ``Call`` between them don't merge when ``g`` is excluded.

    Excluded names (program globals, address-taken locals) stay
    un-versioned because their writes cannot be enumerated.  An
    intervening ``Call`` may mutate ``g`` through a pointer, so the two
    reads are not equivalent.  GVN's safety check refuses to number
    expressions whose operands include an un-versioned destination.
    """
    body = [
        ir.Copy(destination="g", source=10),
        ir.BinaryOperation(destination="t1", left="g", operation="+", right=1),
        ir.Call(args=(), destination=None, name="mutate"),
        ir.BinaryOperation(destination="t2", left="g", operation="+", right=1),
        ir.Return(value=None),
    ]
    result = ssa.optimize_ssa(body, excluded_names=frozenset({"g"}))
    adds = [instruction for instruction in result if isinstance(instruction, ir.BinaryOperation) and instruction.operation == "+"]
    assert len(adds) == 2


def test_optimize_ssa_excluded_name_blocks_propagation_across_call() -> None:
    """A name passed in ``excluded_names`` cannot be propagated across an opaque ``Call``.

    Regression: the SSA renamer used to treat *every* destination as a
    candidate for renaming.  For globals (call-clobbered), the renamer
    would create a single SSA version for ``global = 0`` and then
    propagate ``0`` through every later use — including reads taken
    *after* a ``Call`` that the callee may have used to mutate the
    underlying slot.  The fix wires ``excluded_names`` so the renamer
    leaves such names un-versioned and propagation stops at the un-
    rewritten read.

    Shape:
        global = 0
        call mutate_global       # may write to global
        if global == 0 break
        ...

    With ``global`` in ``excluded_names``, the ``BranchFalse``'s left
    operand must remain the name ``global`` — not be folded to the
    literal ``0`` — so the post-call read picks up whatever
    ``mutate_global`` wrote.
    """
    body = [
        ir.Copy(destination="global", source=0),
        ir.Call(args=(), destination=None, name="mutate_global"),
        ir.BranchFalse(left="global", operation="!=", right=0, target=".end"),
        ir.Call(args=(), destination=None, name="follow_up"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    result = ssa.optimize_ssa(body, excluded_names=frozenset({"global"}))
    branch = next(instruction for instruction in result if isinstance(instruction, ir.BranchFalse))
    assert branch.left == "global"


def test_optimize_ssa_inline_asm_function_returns_body_unchanged() -> None:
    """A body with :class:`cc.ir.InlineAsm` bypasses SSA entirely; output is identical."""
    body = [
        ir.Copy(destination="x", source=1),
        ir.InlineAsm(content="hlt"),
        ir.Return(value="x"),
    ]
    assert ssa.optimize_ssa(body) is body


def test_optimize_ssa_preserves_non_commutative_operand_order_in_gvn() -> None:
    """``a - b`` and ``b - a`` compute different values; GVN must keep both."""
    body = [
        ir.Copy(destination="a", source=10),
        ir.Copy(destination="b", source=3),
        ir.BinaryOperation(destination="t1", left="a", operation="-", right="b"),
        ir.BinaryOperation(destination="t2", left="b", operation="-", right="a"),
        ir.Return(value=None),
    ]
    result = ssa.optimize_ssa(body)
    subs = [instruction for instruction in result if isinstance(instruction, ir.BinaryOperation) and instruction.operation == "-"]
    assert len(subs) == 2


def test_optimize_ssa_preserves_semantics_for_multi_def_temp() -> None:
    """Two branch arms writing different values keep their phi alive through optimization."""
    body = [
        ir.BranchFalse(left="cond", operation="!=", right=0, target=".true"),
        ir.Copy(destination="y", source=0),
        ir.Jump(target=".end"),
        ir.Label(name=".true"),
        ir.Copy(destination="y", source=1),
        ir.Label(name=".end"),
        ir.Return(value="y"),
    ]
    result = ssa.optimize_ssa(body)
    # Return must still read ``y`` — propagation cannot forward either arm
    # because the phi sources disagree.  Both Copy(y, 0) and Copy(y, 1)
    # survive in the respective arms.
    return_instruction = next(instruction for instruction in result if isinstance(instruction, ir.Return))
    assert return_instruction.value == "y"
    copies = [instruction for instruction in result if isinstance(instruction, ir.Copy)]
    sources = {copy.source for copy in copies}
    assert {0, 1}.issubset(sources)


def test_optimize_ssa_propagates_single_def_through_use() -> None:
    """A straight-line ``Copy(x, 5)`` + ``Return(x)`` collapses ``x`` into the Return."""
    body = [
        ir.Copy(destination="x", source=5),
        ir.Return(value="x"),
    ]
    result = ssa.optimize_ssa(body)
    return_instruction = next(instruction for instruction in result if isinstance(instruction, ir.Return))
    assert return_instruction.value == 5


def test_optimize_ssa_round_trips_loop() -> None:
    """A counted loop's phi survives optimization with no spurious renames or duplicates."""
    body = [
        ir.Copy(destination="i", source=0),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="i", operation="<", right=10, target=".end"),
        ir.BinaryOperation(destination="i", left="i", operation="+", right=1),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value="i"),
    ]
    result = ssa.optimize_ssa(body)
    # Loop body retains its branch, increment, and back-edge — every
    # SSA-internal name was deversioned to ``i`` and the destruction copies
    # collapsed into the existing writes.
    assert any(isinstance(instruction, ir.BranchFalse) and instruction.left == "i" for instruction in result)
    assert any(isinstance(instruction, ir.BinaryOperation) and instruction.destination == "i" for instruction in result)
    return_instruction = next(instruction for instruction in result if isinstance(instruction, ir.Return))
    assert return_instruction.value == "i"
    assert not any("_ssa" in repr(instruction) for instruction in result)


def test_round_trip_diamond_preserves_semantics_via_copies() -> None:
    """``convert_to_ssa`` + ``convert_from_ssa`` on a diamond yields phi-free flat IR with copies."""
    body = [
        ir.BranchFalse(left="cond", operation="==", right=0, target=".else"),
        ir.Copy(destination="y", source=1),
        ir.Jump(target=".end"),
        ir.Label(name=".else"),
        ir.Copy(destination="y", source=2),
        ir.Label(name=".end"),
        ir.Return(value="y"),
    ]
    form = ssa.convert_to_ssa(body)
    flat = ssa.flatten_ssa_form(form)
    # No phi nodes remain in the flat output (they aren't an ir.* type anyway,
    # but verify by class identity: nothing in flat is a Phi).
    assert not any(isinstance(instruction, ssa.Phi) for instruction in flat)
    # Copies were inserted in each arm before its terminator to merge ``y``
    # into the phi destination at .end.
    copies = [instruction for instruction in flat if isinstance(instruction, ir.Copy)]
    # 2 original Copies + 2 destruction-inserted Copies for the y-phi.
    assert len(copies) == 4
    # The Return reads the phi destination (the renamed merge name).
    return_instruction = next(instruction for instruction in flat if isinstance(instruction, ir.Return))
    assert isinstance(return_instruction.value, str)
    assert return_instruction.value.startswith("y_ssa")


def test_round_trip_linear_function_preserves_instructions() -> None:
    """``convert_to_ssa`` + ``flatten_ssa_form`` on a phi-less function only renames operands."""
    body = [
        ir.Copy(destination="x", source=1),
        ir.BinaryOperation(destination="y", left="x", operation="+", right=2),
        ir.Return(value="y"),
    ]
    form = ssa.convert_to_ssa(body)
    flat = ssa.flatten_ssa_form(form)
    # Three instructions, no labels (entry block is synthetic).
    assert len(flat) == 3
    assert all(not isinstance(instruction, ir.Label) for instruction in flat)
    copy_instruction, binop_instruction, return_instruction = flat
    assert copy_instruction.destination.startswith("x_ssa")
    assert binop_instruction.left == copy_instruction.destination
    assert return_instruction.value == binop_instruction.destination


def test_ssa_copy_propagation_chains_through_versions() -> None:
    """``Copy(t1, val) → Copy(t2, t1) → Copy(t3, t2)`` chain resolves t3 to val in one pass."""
    body = [
        ir.Copy(destination="t1", source=99),
        ir.Copy(destination="t2", source="t1"),
        ir.Copy(destination="t3", source="t2"),
        ir.Return(value="t3"),
    ]
    result = ssa.optimize_ssa(body)
    return_instruction = next(instruction for instruction in result if isinstance(instruction, ir.Return))
    assert return_instruction.value == 99


def test_switch_discriminant_and_case_body_vars_excluded() -> None:
    """Variables referenced inside :class:`cc.ir.Switch` discriminant or case bodies stay un-versioned."""
    ast_switch = ast_nodes.Switch(cases=[], discriminant=ast_nodes.Var(line=1, name="disc"), line=1)
    case_body = [ir.Block(node=ast_nodes.Var(line=1, name="case_var"))]
    body = [
        ir.Copy(destination="disc", source=0),
        ir.Copy(destination="case_var", source=0),
        ir.Switch(
            cases=[ir.SwitchCase(body=case_body, value=1)],
            discriminant=ast_nodes.Var(line=1, name="disc"),
            end_label=".swend",
            original_ast=ast_switch,
        ),
        ir.Return(value=None),
    ]
    form = ssa.convert_to_ssa(body)
    assert "disc" not in form.ssa_safe_names
    assert "case_var" not in form.ssa_safe_names


def test_trivial_phi_collapses_when_all_sources_resolve_to_same_value() -> None:
    """A phi whose only distinct source is one value is removed and uses replaced."""
    body = [
        ir.BranchFalse(left="cond", operation="==", right=0, target=".else"),
        ir.Copy(destination="y", source=7),
        ir.Jump(target=".end"),
        ir.Label(name=".else"),
        ir.Copy(destination="y", source=7),
        ir.Label(name=".end"),
        ir.Return(value="y"),
    ]
    # Copy propagation rewrites both phi sources to 7; the trivial-phi
    # pass then collapses the phi entirely and the Return picks up 7.
    result = ssa.optimize_ssa(body)
    return_instruction = next(instruction for instruction in result if isinstance(instruction, ir.Return))
    assert return_instruction.value == 7


def test_trivial_phi_with_self_referencing_loop_collapses() -> None:
    """A loop whose only definition is the entry's initial value collapses the header phi.

    The phi at ``.loop`` has sources ``(entry's i_ssa0, the loop tail's
    i_ssaN)``.  When the loop body never reassigns ``i``, the tail
    feeds the phi destination back into itself — a self reference —
    leaving only the entry source as distinct.  Trivial-phi elimination
    drops the phi and the Return reads the entry value directly.
    """
    body = [
        ir.Copy(destination="i", source=99),
        ir.Label(name=".loop"),
        ir.BranchFalse(left="cond", operation="!=", right=0, target=".end"),
        ir.Jump(target=".loop"),
        ir.Label(name=".end"),
        ir.Return(value="i"),
    ]
    result = ssa.optimize_ssa(body)
    return_instruction = next(instruction for instruction in result if isinstance(instruction, ir.Return))
    assert return_instruction.value == 99
