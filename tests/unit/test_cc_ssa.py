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
        ir.Block(node=ast_nodes.Var(name="x", line=1)),
        ir.Return(value="x"),
    ]
    form = ssa.convert_to_ssa(body)
    assert "x" not in form.ssa_safe_names
    # The Copy destination is untouched.
    assert form.cfg.entry.instructions[0].destination == "x"


def test_carry_branch_call_ast_variables_excluded() -> None:
    """Vars used inside a :class:`cc.ir.CarryBranch`'s ``call_ast`` stay un-versioned."""
    call_ast = ast_nodes.Call(args=[ast_nodes.Var(name="arg", line=1)], line=1, name="helper")
    body = [
        ir.Copy(destination="arg", source=5),
        ir.CarryBranch(call_ast=call_ast, target=".end", when="set"),
        ir.Label(name=".end"),
        ir.Return(value=None),
    ]
    form = ssa.convert_to_ssa(body)
    assert "arg" not in form.ssa_safe_names


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


def test_no_destinations_yields_empty_ssa_form() -> None:
    """A function whose only instruction is ``Return`` has no SSA-eligible names."""
    body = [ir.Return(value=None)]
    form = ssa.convert_to_ssa(body)
    assert form.ssa_safe_names == set()
    assert form.phis == {}


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


def test_switch_discriminant_and_case_body_vars_excluded() -> None:
    """Variables referenced inside :class:`cc.ir.Switch` discriminant or case bodies stay un-versioned."""
    ast_switch = ast_nodes.Switch(cases=[], discriminant=ast_nodes.Var(name="disc", line=1), line=1)
    case_body = [ir.Block(node=ast_nodes.Var(name="case_var", line=1))]
    body = [
        ir.Copy(destination="disc", source=0),
        ir.Copy(destination="case_var", source=0),
        ir.Switch(
            cases=[ir.SwitchCase(body=case_body, value=1)],
            discriminant=ast_nodes.Var(name="disc", line=1),
            end_label=".swend",
            original_ast=ast_switch,
        ),
        ir.Return(value=None),
    ]
    form = ssa.convert_to_ssa(body)
    assert "disc" not in form.ssa_safe_names
    assert "case_var" not in form.ssa_safe_names
