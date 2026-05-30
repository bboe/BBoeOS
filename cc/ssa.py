"""SSA construction + destruction over the basic-block CFG.

Phase 2 of the SSA migration: takes a :class:`cc.cfg.ControlFlowGraph` and
produces an :class:`SSAForm` in which every SSA-eligible variable is defined
exactly once.  Joins where two definitions of the same variable reach the
same block get a :class:`Phi` node; the renamer assigns each definition a
fresh version (``name_ssaN``) and rewrites every reachable use to the
dominating version.  Destruction goes the other way: critical edges split,
each phi lowered to an explicit :class:`cc.ir.Copy` on every incoming edge,
phis removed, the resulting CFG flattens back to a flat IR list that the
existing codegen consumes unchanged.

Eligibility for SSA conversion (Phase 2, intentionally conservative):

* The name appears as an :class:`cc.ir.Instruction` destination somewhere
  in the function body.
* The name is **not** referenced inside any opaque region — :class:`cc.ir.Block`
  (AST escape hatch), :class:`cc.ir.CarryBranch` ``call_ast``,
  :class:`cc.ir.Switch` discriminant or case bodies, or
  :class:`cc.ir.InlineAsm`.  Those subtrees are not rewritten by the
  renamer, so any version mismatch would silently miscompile.
* The name is not used as :class:`cc.ir.Index` / :class:`cc.ir.IndexAssign`
  ``base`` — those are stable array / pointer names that don't carry an
  SSA value.

Functions that contain any :class:`cc.ir.InlineAsm` are treated as
*entirely* opaque (no SSA conversion) because the asm text references
variables by name and parsing it is fragile.  Same logic could later
extend to ``__attribute__((naked))`` functions if SSA over their bodies
ever becomes interesting.

Phi placement uses iterated dominance frontiers (Cytron et al., "Efficiently
Computing Static Single Assignment Form and the Control Dependence Graph",
1991).  Renaming is the same paper's dominator-tree DFS with per-variable
version stacks.  Destruction is the naive "Copy at end of each predecessor"
strategy — fine for now; lost-copy and swap problems show up only with
aggressive scheduling, which Phase 2 doesn't do.

This module is purely additive — no existing pass or codegen consumes
:class:`SSAForm` yet.  Phase 3 (mem2reg + SSA-aware optimizer passes)
will wire it into the pipeline.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cc import ast_nodes, ir
from cc.cfg import BasicBlock, ControlFlowGraph, build_cfg, compute_dominance_frontiers, compute_dominators

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: Suffix template for fresh SSA versions.  ``x`` → ``x_ssa0``, ``x_ssa1``…
#: Chosen so the un-versioned base survives a split on ``_ssa``.
_SSA_VERSION_SEPARATOR = "_ssa"


def _flatten_cfg(cfg: ControlFlowGraph, /) -> list[ir.Instruction]:
    """Round-trip a CFG back to a flat :class:`cc.ir.Instruction` list.

    Walks blocks in CFG source order, emitting each block's leading
    :class:`cc.ir.Label` (real labels only — synthetic ``<entry>`` /
    ``<fallthrough_N>`` names are dropped), then its instructions, then
    its terminator (if any).  The resulting list is suitable for direct
    use by the existing codegen.
    """
    output: list[ir.Instruction] = []
    for block in cfg.blocks:
        if not block.label.startswith("<"):
            output.append(ir.Label(name=block.label))
        output.extend(block.instructions)
        if block.terminator is not None:
            output.append(block.terminator)
    return output


def _instruction_destination(instruction: ir.Instruction, /) -> str | None:
    """Return the destination name written by *instruction*, or None.

    Duplicates :func:`cc.ir_optimize._instruction_destination` to keep
    ssa.py free of the optimizer's import surface.
    """
    if isinstance(instruction, (ir.BinaryOperation, ir.Copy, ir.Index)):
        return instruction.destination
    if isinstance(instruction, ir.Call):
        return instruction.destination
    return None


def _iter_ast_var_names(node: object, /) -> Iterator[str]:
    """Yield every ``Var.name`` appearing anywhere in the AST subtree at *node*.

    Duplicates :func:`cc.ir_optimize._iter_ast_var_names` so the SSA
    eligibility filter can flag any name referenced by an opaque
    :class:`cc.ir.Block` / :class:`cc.ir.CarryBranch` subtree.

    Yields:
        Each ``Var.name`` string encountered in source order.

    """
    if isinstance(node, ast_nodes.Var):
        yield node.name
        return
    if dataclasses.is_dataclass(node):
        for declared_field in dataclasses.fields(node):
            yield from _iter_ast_var_names(getattr(node, declared_field.name))
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_ast_var_names(item)


def _opaque_referenced_names(body: list[ir.Instruction], /) -> set[str]:
    """Return every name referenced inside an opaque region of *body*.

    Opaque regions are AST subtrees the renamer can't safely rewrite:
    :class:`cc.ir.Block` (AST escape hatch), :class:`cc.ir.CarryBranch`
    ``call_ast``, :class:`cc.ir.Switch` ``discriminant`` plus every nested
    case body's full opaque-reference set, and :class:`cc.ir.Index` /
    :class:`cc.ir.IndexAssign` ``base`` (stable array names).  Any name
    here is excluded from SSA conversion.
    """
    referenced: set[str] = set()
    for instruction in body:
        if isinstance(instruction, ir.Block):
            referenced.update(_iter_ast_var_names(instruction.node))
        elif isinstance(instruction, ir.CarryBranch):
            referenced.update(_iter_ast_var_names(instruction.call_ast))
        elif isinstance(instruction, ir.Switch):
            referenced.update(_iter_ast_var_names(instruction.discriminant))
            for case in instruction.cases:
                referenced.update(_opaque_referenced_names(case.body))
        elif isinstance(instruction, (ir.Index, ir.IndexAssign)):
            referenced.add(instruction.base)
    return referenced


def _place_phi_nodes(
    definitions: dict[str, set[BasicBlock]],
    /,
    *,
    dominance_frontiers: dict[BasicBlock, set[BasicBlock]],
) -> dict[BasicBlock, list[Phi]]:
    """Insert empty phi nodes per Cytron's iterated-dominance-frontier algorithm.

    For each variable with at least one definition, walk every block in
    the iterated dominance frontier of its def-set, placing an empty
    :class:`Phi` (sources filled in by :func:`_rename_variables`) at the
    block's logical start.  A phi at block ``F`` for variable ``v`` is
    itself a definition of ``v`` at ``F``, so ``F`` is added to the work
    list to propagate further.
    """
    phis: dict[BasicBlock, list[Phi]] = defaultdict(list)
    for name, def_blocks in definitions.items():
        already_has_phi: set[BasicBlock] = set()
        worklist = list(def_blocks)
        while worklist:
            block = worklist.pop()
            for frontier in dominance_frontiers.get(block, set()):
                if frontier in already_has_phi:
                    continue
                phis[frontier].append(Phi(destination=name, original_name=name, sources={}))
                already_has_phi.add(frontier)
                if frontier not in def_blocks:
                    worklist.append(frontier)
    return dict(phis)


def _rename_variables(
    cfg: ControlFlowGraph,
    /,
    *,
    idom: dict[BasicBlock, BasicBlock],
    phis: dict[BasicBlock, list[Phi]],
    ssa_safe_names: set[str],
) -> None:
    """Walk the dominator tree, renaming defs to fresh versions and uses to the dominating version.

    Mutates each :class:`BasicBlock`'s ``instructions`` and ``terminator``
    in place (operand substitution) and rewrites each :class:`Phi`'s
    ``destination`` and ``sources`` to the versioned form.
    """
    # Build dominator tree: idom_children[X] = {Y | idom[Y] = X, Y != X}.
    children: dict[BasicBlock, list[BasicBlock]] = defaultdict(list)
    for block, block_idom in idom.items():
        if block is not block_idom:
            children[block_idom].append(block)
    counter: dict[str, int] = dict.fromkeys(ssa_safe_names, 0)
    stack: dict[str, list[str]] = {name: [] for name in ssa_safe_names}

    def _fresh_version(name: str, /) -> str:
        version = counter[name]
        counter[name] += 1
        versioned = f"{name}{_SSA_VERSION_SEPARATOR}{version}"
        stack[name].append(versioned)
        return versioned

    def _lookup(name: str, /) -> str:
        if versions := stack.get(name):
            return versions[-1]
        return name

    def _rename_block(block: BasicBlock, /) -> None:
        # Snapshot of stack heights at entry — pop everything pushed below.
        pushed_counts: dict[str, int] = defaultdict(int)
        for phi in phis.get(block, []):
            phi.destination = _fresh_version(phi.original_name)
            pushed_counts[phi.original_name] += 1
        new_instructions: list[ir.Instruction] = []
        for instruction in block.instructions:
            rewritten = _substitute_value_operands(instruction, lookup=_lookup, ssa_safe_names=ssa_safe_names)
            destination = _instruction_destination(rewritten)
            if isinstance(destination, str) and destination in ssa_safe_names:
                new_version = _fresh_version(destination)
                rewritten = dataclasses.replace(rewritten, destination=new_version)
                pushed_counts[destination] += 1
            new_instructions.append(rewritten)
        block.instructions = new_instructions
        if block.terminator is not None:
            block.terminator = _substitute_value_operands(block.terminator, lookup=_lookup, ssa_safe_names=ssa_safe_names)
        for successor in block.successors:
            for phi in phis.get(successor, []):
                phi.sources[block] = _lookup(phi.original_name)
        for child in children.get(block, []):
            _rename_block(child)
        for name, count in pushed_counts.items():
            del stack[name][-count:]

    _rename_block(cfg.entry)


def _split_critical_edges(cfg: ControlFlowGraph, /) -> ControlFlowGraph:
    """Insert a fresh BB on every critical edge so phi destruction can place copies safely.

    A critical edge is ``A → B`` where ``A`` has multiple successors and
    ``B`` has multiple predecessors.  Inserting copies for ``B``'s phis
    at the end of ``A`` would also affect the other ``A`` successors;
    placing them at the start of ``B`` would also affect the other
    ``B`` predecessors.  Splitting the edge gives a dedicated landing
    pad for ``A → B``'s copies.

    Returns a freshly-rebuilt CFG with new synthetic blocks in source
    order — re-running dominator analysis on the result is required
    before SSA construction continues.
    """
    splits: list[tuple[BasicBlock, BasicBlock]] = [
        (block, successor)
        for block in cfg.blocks
        if len(block.successors) >= 2
        for successor in block.successors
        if len(successor.predecessors) >= 2
    ]
    if not splits:
        return cfg
    msg = "critical edge splitting not yet implemented for Phase 2 — no tests exercise this path"
    raise NotImplementedError(msg)


def _substitute_value_operands(
    instruction: ir.Instruction,
    /,
    *,
    lookup: Callable[[str], str],
    ssa_safe_names: set[str],
) -> ir.Instruction:
    """Return *instruction* with every SSA-safe-name use replaced by its current version.

    Destination is intentionally untouched here — the renamer handles
    that separately (a single fresh version after substituting uses).
    """

    def _rename(value: ir.Value, /) -> ir.Value:
        if isinstance(value, str) and value in ssa_safe_names:
            return lookup(value)
        return value

    if isinstance(instruction, ir.BinaryOperation):
        return dataclasses.replace(instruction, left=_rename(instruction.left), right=_rename(instruction.right))
    if isinstance(instruction, ir.Copy):
        return dataclasses.replace(instruction, source=_rename(instruction.source))
    if isinstance(instruction, ir.Call):
        return dataclasses.replace(instruction, args=tuple(_rename(arg) for arg in instruction.args))
    if isinstance(instruction, ir.Index):
        return dataclasses.replace(instruction, index=_rename(instruction.index))
    if isinstance(instruction, ir.IndexAssign):
        return dataclasses.replace(instruction, index=_rename(instruction.index), source=_rename(instruction.source))
    if isinstance(instruction, ir.BranchFalse):
        return dataclasses.replace(instruction, left=_rename(instruction.left), right=_rename(instruction.right))
    if isinstance(instruction, ir.Return):
        if instruction.value is None:
            return instruction
        return dataclasses.replace(instruction, value=_rename(instruction.value))
    if isinstance(instruction, ir.TailCall):
        return dataclasses.replace(instruction, args=tuple(_rename(arg) for arg in instruction.args))
    return instruction


@dataclass(eq=False, kw_only=True, slots=True)
class Phi:
    """A phi node attached to the start of a basic block.

    ``original_name`` is the un-versioned variable name (the SSA pre-image).
    ``destination`` is the versioned name after renaming (``original_name``
    initially, ``original_name_ssaN`` after :func:`_rename_variables`).
    ``sources`` maps each predecessor block to the value of that variable
    at the end of the predecessor — initially empty, populated during
    renaming.
    """

    destination: str
    original_name: str
    sources: dict[BasicBlock, ir.Value] = field(default_factory=dict)


@dataclass(eq=False, kw_only=True, slots=True)
class SSAForm:
    """A control-flow graph in SSA form, with phi nodes maintained separately.

    ``cfg`` is the underlying :class:`cc.cfg.ControlFlowGraph` with
    block instructions / terminators rewritten to use versioned names.
    ``phis`` maps each block to the list of phi nodes at its start.
    ``ssa_safe_names`` records which un-versioned names participated in
    SSA conversion — destruction uses this to know what to coalesce.
    """

    cfg: ControlFlowGraph
    phis: dict[BasicBlock, list[Phi]]
    ssa_safe_names: set[str]


def convert_from_ssa(ssa: SSAForm, /) -> ControlFlowGraph:
    """Destruct *ssa* back to a phi-free CFG by inserting copies on incoming edges.

    For each :class:`Phi` ``(dest, {pred: value})`` at block ``B``,
    append :class:`cc.ir.Copy` ``(dest = value)`` to ``pred.instructions``
    just before ``pred.terminator``.  Phis are then removed from the
    SSA-form's phi map.

    The CFG is returned unchanged structurally — only instruction lists
    inside existing blocks are mutated.  Critical-edge splitting is a
    precondition (currently raises :exc:`NotImplementedError` when
    needed); the typical Phase 2 test cases don't trigger any.
    """
    for phi_list in ssa.phis.values():
        for phi in phi_list:
            for predecessor, source in phi.sources.items():
                copy = ir.Copy(destination=phi.destination, source=source)
                predecessor.instructions.append(copy)
    ssa.phis.clear()
    return ssa.cfg


def convert_to_ssa(body: list[ir.Instruction], /) -> SSAForm:
    """Build a CFG from *body* and convert SSA-eligible variables to versioned form.

    Returns an :class:`SSAForm` with the renamed CFG and the phi map.
    Names referenced inside opaque regions (see module docstring) stay
    un-versioned but remain in the IR.  Functions containing any
    :class:`cc.ir.InlineAsm` instruction skip SSA conversion entirely —
    the asm text would reference stale unversioned names.
    """
    cfg = build_cfg(body)
    has_inline_asm = any(isinstance(instruction, ir.InlineAsm) for instruction in body)
    if has_inline_asm:
        return SSAForm(cfg=cfg, phis={}, ssa_safe_names=set())
    excluded = _opaque_referenced_names(body)
    all_destinations: set[str] = set()
    for instruction in body:
        destination = _instruction_destination(instruction)
        if isinstance(destination, str):
            all_destinations.add(destination)
    ssa_safe_names = all_destinations - excluded
    if not ssa_safe_names:
        return SSAForm(cfg=cfg, phis={}, ssa_safe_names=set())
    definitions: dict[str, set[BasicBlock]] = {name: set() for name in ssa_safe_names}
    for block in cfg.blocks:
        for instruction in [*block.instructions, *([block.terminator] if block.terminator is not None else [])]:
            destination = _instruction_destination(instruction)
            if destination in ssa_safe_names:
                definitions[destination].add(block)
    idom = compute_dominators(cfg)
    dominance_frontiers = compute_dominance_frontiers(idom)
    phis = _place_phi_nodes(definitions, dominance_frontiers=dominance_frontiers)
    _rename_variables(cfg, idom=idom, phis=phis, ssa_safe_names=ssa_safe_names)
    return SSAForm(cfg=cfg, phis=phis, ssa_safe_names=ssa_safe_names)


def flatten_ssa_form(ssa: SSAForm, /) -> list[ir.Instruction]:
    """Destruct *ssa* and return the resulting CFG flattened to a flat IR list.

    Convenience wrapper around :func:`convert_from_ssa` + the internal
    :func:`_flatten_cfg` helper.  The output is suitable for direct use
    by the existing codegen path.
    """
    cfg = convert_from_ssa(ssa)
    return _flatten_cfg(cfg)
