"""SSA construction, optimization, and destruction over the basic-block CFG.

Takes a :class:`cc.cfg.ControlFlowGraph` and produces an :class:`SSAForm`
in which every SSA-eligible variable is defined exactly once.  Joins where
two definitions of the same variable reach the same block get a :class:`Phi`
node; the renamer assigns each definition a fresh version (``name_ssaN``)
and rewrites every reachable use to the dominating version.  Phase 3
adds SSA-form cleanup passes — trivial-phi removal, dead-phi removal,
and copy propagation — wired into :func:`optimize_ssa`.  Destruction
goes the other way: critical edges split, each phi lowered to an explicit
:class:`cc.ir.Copy` on every incoming edge, phis removed, the resulting
CFG flattens back to a flat IR list that the existing codegen consumes
unchanged.

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

:func:`optimize_ssa` is the end-to-end entry point that builds SSA, runs
the SSA-form passes, destructs back to phi-free IR, and flattens.
:class:`cc.ir_optimize.Optimizer` calls it once the classical IR-level
passes reach fixed point.
"""

from __future__ import annotations

import dataclasses
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cc import ast_nodes, ir
from cc.cfg import BasicBlock, ControlFlowGraph, build_cfg, compute_dominance_frontiers, compute_dominators, flatten_cfg

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: Label template for blocks inserted on critical edges.  Must be a
#: real IR-style label (leading ``.``) because the redirected terminator
#: in the predecessor branches to it by name and the codegen emits the
#: branch as ``jmp .ssa_split_N`` — flatten emits the corresponding
#: ``Label`` so the assembler can resolve the target.
_SPLIT_LABEL_TEMPLATE = ".ssa_split_{counter}"

#: Suffix template for fresh SSA versions.  ``x`` → ``x_ssa0``, ``x_ssa1``…
#: Chosen so the un-versioned base survives a split on ``_ssa``.
_SSA_VERSION_SEPARATOR = "_ssa"


def _address_taken_names(body: list[ir.Instruction], /) -> set[str]:
    """Return every name whose address is taken (``&name``) in *body*.

    A name whose address is taken can be mutated through the resulting
    pointer at any later call site (callee receives the pointer, or an
    :class:`cc.ir.IndexAssign` through an aliased base, or inline asm
    inside a :class:`cc.ir.Block`).  SSA conversion cannot enumerate
    those writes, so the name is unsound to rename — propagation could
    forward a value across a Call that mutated the slot through the
    leaked pointer.  Walks every value operand including ``Call`` /
    ``TailCall`` args plus nested :class:`cc.ir.Switch` case bodies.
    """
    taken: set[str] = set()
    for instruction in body:
        for operand in _iter_value_operands(instruction):
            if (taken_name := ast_nodes.address_of_variable_name(operand)) is not None:
                taken.add(taken_name)
        if isinstance(instruction, ir.Switch):
            for case in instruction.cases:
                taken.update(_address_taken_names(case.body))
    return taken


def _collect_use_counts(ssa: SSAForm, /) -> dict[str, int]:
    """Return ``{name: use_count}`` across instructions, terminators, and phi sources in *ssa*."""
    return dict(Counter(_iter_name_uses(ssa)))


def _deversion_instruction(instruction: ir.Instruction, /) -> ir.Instruction:
    """Return *instruction* with every name (destination + operands) deversioned."""
    rewritten = _map_value_operands(instruction, rewrite=lambda value: _deversion_name(value) if isinstance(value, str) else value)
    destination = _instruction_destination(rewritten)
    if isinstance(destination, str):
        deversioned_destination = _deversion_name(destination)
        if deversioned_destination != destination:
            return dataclasses.replace(rewritten, destination=deversioned_destination)
    return rewritten


def _deversion_name(name: str, /) -> str:
    """Strip a trailing ``_ssaN`` suffix from *name*, returning the un-versioned base."""
    prefix, separator, suffix = name.rpartition(_SSA_VERSION_SEPARATOR)
    if separator and prefix and suffix.isdigit():
        return prefix
    return name


def _deversion_ssa_form(ssa: SSAForm, /) -> None:
    """Rewrite every SSA-versioned name back to its un-versioned base, in place.

    Phi destinations, instruction destinations, value operands, and phi
    sources are all rewritten so the codegen sees only the names the AST
    layer declared — versioned names are an internal-to-SSA artefact and
    have no frame-slot allocation outside this module.
    """
    for phi_list in ssa.phis.values():
        for phi in phi_list:
            phi.destination = _deversion_name(phi.destination)
            phi.sources = {
                predecessor: _deversion_name(source) if isinstance(source, str) else source for predecessor, source in phi.sources.items()
            }
    for block in ssa.cfg.blocks:
        block.instructions = [_deversion_instruction(instruction) for instruction in block.instructions]
        if block.terminator is not None:
            block.terminator = _deversion_instruction(block.terminator)


def _drop_redundant_copies(body: list[ir.Instruction], /) -> list[ir.Instruction]:
    """Return *body* with self-copies and Copies immediately overwritten by another Copy dropped.

    De-versioning turns destruction-introduced ``Copy(phi_dest_ssaN, src)``
    into ``Copy(name, src)``; when the preceding ``Copy(name, src)``
    (the original SSA-renamed def) deversions to the same shape, the
    two writes collapse to one.  Self-copies (``Copy(x, x)``) come from
    trivial-phi destruction merging two SSA versions of the same base.
    """
    result: list[ir.Instruction] = []
    for index, instruction in enumerate(body):
        if isinstance(instruction, ir.Copy) and instruction.destination == instruction.source:
            continue
        if isinstance(instruction, ir.Copy) and index + 1 < len(body):
            successor = body[index + 1]
            if (
                isinstance(successor, ir.Copy)
                and successor.destination == instruction.destination
                and successor.source != instruction.destination
            ):
                continue
        result.append(instruction)
    return result


def _eliminate_dead_phis(ssa: SSAForm, /) -> bool:
    changed = False
    while True:
        use_counts = _collect_use_counts(ssa)
        dead: list[tuple[BasicBlock, Phi]] = [
            (block, phi) for block, phi_list in ssa.phis.items() for phi in phi_list if use_counts.get(phi.destination, 0) == 0
        ]
        if not dead:
            return changed
        for block, phi in dead:
            ssa.phis[block].remove(phi)
            if not ssa.phis[block]:
                del ssa.phis[block]
        changed = True


def _eliminate_redundant_expressions(ssa: SSAForm, /, *, idom: dict[BasicBlock, BasicBlock]) -> bool:
    """Replace each redundant ``BinaryOperation`` with a ``Copy`` of the earlier SSA destination that produced the same value.

    Dominator-tree-only GVN: walk the dominator tree in DFS order, and
    for each block maintain a map from a value-key
    ``(operation, canonicalized_operands)`` to the SSA-versioned
    destination that first produced that value within the block's
    dominance region.  When a later ``BinaryOperation`` matches an entry
    already in scope, rewrite it to ``Copy(destination, prior)`` — the
    prior destination strictly dominates the rewrite site, so the SSA
    use-def edge stays valid.  Copy propagation in the same fixed-point
    loop forwards the new copy to every later use, leaving the redundant
    computation as a self-copy that subsequent cleanup drops.

    Operands are canonicalized for commutative operations
    (``+``, ``*``, ``&``, ``|``, ``^``, ``==``, ``!=``) by sorting the
    operand-key pair so ``a + b`` and ``b + a`` collapse to one number.

    Operand safety: constants and ``&name``
    (:class:`cc.ast_nodes.PlaceAddressOf`) are
    always safe.  String operands are safe when they are SSA-versioned
    (single-def by construction) **or** when they are un-versioned names
    that never appear as a destination anywhere in the body
    (read-only — e.g. a function parameter whose address is never taken).
    An un-versioned destination is unsafe because intervening writes
    between two program points can change the value: writes to
    address-taken locals or call-clobbered globals stay un-versioned for
    soundness, and matching against them would silently miscompile.

    Entries are only recorded when the producing instruction's
    destination is SSA-versioned — otherwise the destination could be
    overwritten before a later match site reads it, leaving the rewritten
    Copy reading a stale value.
    """
    commutative_operations = frozenset({"+", "*", "&", "|", "^", "==", "!="})
    unsafe_names: set[str] = set()
    for block in ssa.cfg.blocks:
        for instruction in block.instructions:
            destination = _instruction_destination(instruction)
            if isinstance(destination, str) and _SSA_VERSION_SEPARATOR not in destination:
                unsafe_names.add(destination)

    def _value_key(value: ir.Value, /) -> tuple[str, int | str]:
        if isinstance(value, int):
            return ("i", value)
        if isinstance(value, str):
            return ("s", value)
        taken_name = ast_nodes.address_of_variable_name(value)
        assert taken_name is not None
        return ("a", taken_name)

    def _is_safe(value: ir.Value, /) -> bool:
        if isinstance(value, int) or ast_nodes.address_of_variable_name(value) is not None:
            return True
        if _SSA_VERSION_SEPARATOR in value:
            return True
        return value not in unsafe_names

    def _expression_key(instruction: ir.BinaryOperation, /) -> tuple | None:
        if not (_is_safe(instruction.left) and _is_safe(instruction.right)):
            return None
        left_key = _value_key(instruction.left)
        right_key = _value_key(instruction.right)
        if instruction.operation in commutative_operations:
            left_key, right_key = sorted((left_key, right_key))
        return (instruction.operation, left_key, right_key)

    children: dict[BasicBlock, list[BasicBlock]] = defaultdict(list)
    for block, parent in idom.items():
        if block is not parent:
            children[parent].append(block)
    changed = False
    available: dict[tuple, str] = {}
    # Iterative dominator-tree DFS so deeply-nested CFGs don't blow the
    # Python recursion limit.  Each stack entry is either ``(block, None)``
    # — visit *block* — or ``(block, added_keys)`` — leaving the subtree
    # rooted at *block*, pop the keys this block added to ``available``.
    stack: list[tuple[BasicBlock, list[tuple] | None]] = [(ssa.cfg.entry, None)]
    while stack:
        block, exit_keys = stack.pop()
        if exit_keys is not None:
            for key in exit_keys:
                del available[key]
            continue
        added: list[tuple] = []
        new_instructions: list[ir.Instruction] = []
        for instruction in block.instructions:
            replacement: ir.Instruction | None = None
            if isinstance(instruction, ir.BinaryOperation) and (key := _expression_key(instruction)) is not None:
                if (prior := available.get(key)) is not None:
                    replacement = ir.Copy(destination=instruction.destination, source=prior)
                    changed = True
                elif _SSA_VERSION_SEPARATOR in instruction.destination:
                    available[key] = instruction.destination
                    added.append(key)
            new_instructions.append(replacement if replacement is not None else instruction)
        block.instructions = new_instructions
        stack.append((block, added))
        stack.extend((child, None) for child in children.get(block, []))
    return changed


def _eliminate_trivial_phis(ssa: SSAForm, /) -> bool:
    changed = False
    while triviality := _find_trivial_phi(ssa):
        block, phi, replacement = triviality
        ssa.phis[block].remove(phi)
        if not ssa.phis[block]:
            del ssa.phis[block]
        _replace_value_uses(ssa, mapping={phi.destination: replacement})
        changed = True
    return changed


def _find_trivial_phi(ssa: SSAForm, /) -> tuple[BasicBlock, Phi, ir.Value] | None:
    """Return the next phi whose operands (minus self-references) reduce to one value."""
    for block, phi_list in ssa.phis.items():
        for phi in phi_list:
            unique: ir.Value | None = None
            ambiguous = False
            for source in phi.sources.values():
                if source == phi.destination:
                    continue
                if unique is None:
                    unique = source
                elif source != unique:
                    ambiguous = True
                    break
            if not ambiguous and unique is not None:
                return block, phi, unique
    return None


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
    if isinstance(node, ast_nodes.VariablePlace):
        # A ``VariablePlace`` names a variable through its ``name`` string
        # field (not a Var node), so the Var-only recursion below cannot
        # see it.  After the Place fold, a for-loop's ``i++`` step is
        # ``PlaceIncrementDecrement(VariablePlace(i))`` whose sole mention of ``i`` is
        # this string — missing it lets the SSA eligibility filter version
        # ``i`` as loop-invariant and fold the guard to ``cmp 0, n``.
        yield node.name
        return
    # Lvalue targets and member/deref bases the AST stores as a bare string
    # rather than a Var node: ``target_name`` (DerefIncrement{,Assign}) and
    # ``object_name`` (the Member* family).
    # The Var-only recursion below cannot see these, so a variable
    # referenced *only* through one of them is invisible to callers that
    # must enumerate every name an opaque region touches.  Both field names
    # appear only on variable-bearing nodes, so reading them by attribute
    # is unambiguous.
    for bare_name_field in ("target_name", "object_name"):
        bare_name = getattr(node, bare_name_field, None)
        if isinstance(bare_name, str):
            yield bare_name
    if dataclasses.is_dataclass(node):
        for declared_field in dataclasses.fields(node):
            yield from _iter_ast_var_names(getattr(node, declared_field.name))
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_ast_var_names(item)


def _iter_name_uses(ssa: SSAForm, /) -> Iterator[str]:
    """Yield every string-name use across instructions, terminators, and phi sources in *ssa*.

    Yields:
        Each string-typed operand encountered, in basic-block iteration
        order followed by phi-source iteration order.

    """
    for block in ssa.cfg.blocks:
        terminator = () if block.terminator is None else (block.terminator,)
        for instruction in (*block.instructions, *terminator):
            for operand in _iter_value_operands(instruction):
                if isinstance(operand, str):
                    yield operand
    for phi_list in ssa.phis.values():
        for phi in phi_list:
            for source in phi.sources.values():
                if isinstance(source, str):
                    yield source


def _iter_value_operands(instruction: ir.Instruction, /) -> Iterator[ir.Value]:
    """Yield each non-destination :data:`cc.ir.Value` operand read by *instruction*.

    Walks the :attr:`cc.ir.Instruction.VALUE_FIELDS` class-level field list
    so new instruction kinds participate by declaration alone — no isinstance
    ladder here to fall out of sync with the IR definitions.

    Yields:
        Each ``Value`` operand in declaration order.  Tuple-typed fields
        (``Call.args``, ``TailCall.args``) are flattened element-wise.

    """
    for field_name in instruction.VALUE_FIELDS:
        value = getattr(instruction, field_name)
        if value is None:
            continue
        if isinstance(value, tuple):
            yield from value
        else:
            yield value


def _map_value_operands(instruction: ir.Instruction, /, *, rewrite: Callable[[ir.Value], ir.Value]) -> ir.Instruction:
    """Return *instruction* with each value operand replaced by ``rewrite(operand)``.

    Tuple-typed fields (``args``) are rewritten element-wise.  Returns the
    original instruction unchanged (identity-preserving) when no field
    actually changed, so callers can drive fixed-point loops with ``is``
    comparisons.  Destinations are intentionally not rewritten — callers
    that care about them (rename, deversion) handle that separately.
    """
    fields: dict[str, object] = {}
    for field_name in instruction.VALUE_FIELDS:
        value = getattr(instruction, field_name)
        if value is None:
            continue
        new_value = tuple(rewrite(item) for item in value) if isinstance(value, tuple) else rewrite(value)
        if new_value != value:
            fields[field_name] = new_value
    if not fields:
        return instruction
    return dataclasses.replace(instruction, **fields)


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
    for name in sorted(definitions):
        def_blocks = definitions[name]
        already_has_phi: set[BasicBlock] = set()
        worklist = sorted(def_blocks, key=lambda block: block.label)
        while worklist:
            block = worklist.pop()
            for frontier in sorted(dominance_frontiers.get(block, set()), key=lambda block: block.label):
                if frontier in already_has_phi:
                    continue
                phis[frontier].append(Phi(destination=name, original_name=name, sources={}))
                already_has_phi.add(frontier)
                if frontier not in def_blocks:
                    worklist.append(frontier)
    return dict(phis)


def _propagate_ssa_copies(ssa: SSAForm, /) -> bool:
    """Forward every SSA-versioned ``ir.Copy`` source through its uses.

    Every SSA-versioned destination has exactly one static def, so each
    ``Copy(dest=x_ssaN, source=y)`` lets every later use of ``x_ssaN``
    shortcut to ``y``.  Chains ``x → y`` then ``y → z`` resolve in a
    single pass before substitution so no intermediate version survives
    in operand position.  The defining ``Copy`` is **not** dropped: the
    un-versioned name may still be observable to the AST-level codegen
    (e.g. a user-declared local with a stack slot allocated outside the
    IR's accounting).  De-versioning at the end of :func:`optimize_ssa`
    plus the linear DCE pass remove any genuinely dead temps.  Returns
    True when the substitution rewrote at least one operand so the
    outer fixed-point loop knows to keep iterating.
    """
    direct_mapping: dict[str, ir.Value] = {}
    for block in ssa.cfg.blocks:
        for instruction in block.instructions:
            if isinstance(instruction, ir.Copy) and _SSA_VERSION_SEPARATOR in instruction.destination:
                source = instruction.source
                if isinstance(source, str):
                    # A bare SSA-versioned source collapses to its un-versioned
                    # slot after de-versioning.  When destruction appends
                    # ``Copy(phi_dest, source)`` at the end of the predecessor
                    # block, the slot may have been overwritten by a later
                    # ``edx_ssaM = ...`` between the captured SSA point and
                    # block end, so the de-versioned Copy reads a stale value.
                    # Restrict propagation to non-string sources (constants,
                    # PlaceAddressOf, etc.) which survive de-versioning intact.
                    continue
                direct_mapping[instruction.destination] = source
    if not direct_mapping:
        return False
    resolved: dict[str, ir.Value] = {}
    for key, initial in direct_mapping.items():
        value: ir.Value = initial
        seen: set[str] = {key}
        while isinstance(value, str) and value in direct_mapping and value not in seen:
            seen.add(value)
            value = direct_mapping[value]
        resolved[key] = value
    return _replace_value_uses(ssa, mapping=resolved)


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


def _replace_value_uses(ssa: SSAForm, /, *, mapping: dict[str, ir.Value]) -> bool:
    """Rewrite every operand listed in *mapping* to its replacement, across *ssa* in place.

    Returns True when at least one operand was actually rewritten.
    """
    if not mapping:
        return False
    changed = False
    for block in ssa.cfg.blocks:
        new_instructions = []
        for instruction in block.instructions:
            rewritten = _replace_value_uses_in_instruction(instruction, mapping=mapping)
            if rewritten is not instruction:
                changed = True
            new_instructions.append(rewritten)
        block.instructions = new_instructions
        if block.terminator is not None:
            rewritten = _replace_value_uses_in_instruction(block.terminator, mapping=mapping)
            if rewritten is not block.terminator:
                changed = True
            block.terminator = rewritten
    for phi_list in ssa.phis.values():
        for phi in phi_list:
            for predecessor in list(phi.sources):
                source = phi.sources[predecessor]
                if isinstance(source, str) and source in mapping:
                    phi.sources[predecessor] = mapping[source]
                    changed = True
    return changed


def _replace_value_uses_in_instruction(instruction: ir.Instruction, /, *, mapping: dict[str, ir.Value]) -> ir.Instruction:
    """Return *instruction* (identity-preserved when untouched) with every mapped operand rewritten."""
    return _map_value_operands(instruction, rewrite=lambda value: mapping[value] if isinstance(value, str) and value in mapping else value)


def _split_critical_edges(cfg: ControlFlowGraph, /) -> ControlFlowGraph:
    """Insert a fresh BB on every critical edge so phi destruction can place copies safely.

    A critical edge is ``A → B`` where ``A`` has multiple successors and
    ``B`` has multiple predecessors.  Inserting copies for ``B``'s phis
    at the end of ``A`` would also affect the other ``A`` successors;
    placing them at the start of ``B`` would also affect the other
    ``B`` predecessors.  Splitting the edge gives a dedicated landing
    pad for ``A → B``'s copies.

    Mutates *cfg* in place.  Before inserting splits, every block that
    has no terminator and falls through to a join with multiple
    predecessors gets an explicit ``Jump`` to that successor — without
    this, inserting a split before the successor in source order would
    silently redirect those fall-throughs into the split (which carries
    destruction Copies for a *different* incoming edge).  The split
    block is then inserted immediately before ``B`` in source order,
    and ``A``'s terminator is rewritten to name the split.
    Predecessor / successor edges are rewired so dominator analysis on
    the result sees the split block in place of the original ``A → B``
    link.
    """
    critical: list[tuple[BasicBlock, BasicBlock]] = [
        (block, successor)
        for block in cfg.blocks
        if len(block.successors) >= 2
        for successor in block.successors
        if len(successor.predecessors) >= 2
    ]
    if not critical:
        return cfg
    # Materialize fall-through Jumps before any splits are inserted: a
    # block that drops into the next source-order block will, after
    # split insertion, drop into the split instead and pick up its
    # destruction Copy — even though the source-order successor was
    # something else entirely.  An explicit Jump bypasses the split.
    successors_with_splits = {successor.label for _, successor in critical}
    for index, block in enumerate(cfg.blocks):
        if block.terminator is not None:
            continue
        if index + 1 >= len(cfg.blocks):
            continue
        fall_through = cfg.blocks[index + 1]
        if fall_through.label in successors_with_splits and fall_through in block.successors:
            block.terminator = ir.Jump(target=fall_through.label)
    for counter, (predecessor, successor) in enumerate(critical):
        label = _SPLIT_LABEL_TEMPLATE.format(counter=counter)
        split = BasicBlock(label=label, terminator=ir.Jump(target=successor.label))
        split.predecessors.append(predecessor)
        split.successors.append(successor)
        cfg.label_to_block[label] = split
        successor_index = predecessor.successors.index(successor)
        predecessor.successors[successor_index] = split
        predecessor_index = successor.predecessors.index(predecessor)
        successor.predecessors[predecessor_index] = split
        terminator = predecessor.terminator
        if isinstance(terminator, (ir.BranchFalse, ir.CarryBranch, ir.Jump)) and terminator.target == successor.label:
            predecessor.terminator = dataclasses.replace(terminator, target=label)
        cfg.blocks.insert(cfg.blocks.index(successor), split)
    return cfg


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
    return _map_value_operands(
        instruction, rewrite=lambda value: lookup(value) if isinstance(value, str) and value in ssa_safe_names else value
    )


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
    SSA-form's phi map.  Critical edges were split during
    :func:`convert_to_ssa`, so every predecessor of a phi-bearing block
    has exactly one successor — copies cannot bleed into sibling arms.
    """
    for phi_list in ssa.phis.values():
        for phi in phi_list:
            for predecessor, source in phi.sources.items():
                copy = ir.Copy(destination=phi.destination, source=source)
                predecessor.instructions.append(copy)
    ssa.phis.clear()
    return ssa.cfg


def convert_to_ssa(body: list[ir.Instruction], /, *, excluded_names: frozenset[str] = frozenset()) -> SSAForm:
    """Build a CFG from *body* and convert SSA-eligible variables to versioned form.

    Returns an :class:`SSAForm` with the renamed CFG and the phi map.
    Names referenced inside opaque regions (see module docstring) stay
    un-versioned but remain in the IR.  Functions containing any
    :class:`cc.ir.InlineAsm` instruction skip SSA conversion entirely —
    the asm text would reference stale unversioned names.  Critical
    edges are split before phi placement so :func:`convert_from_ssa`
    can lower phis to copies without disturbing sibling control flow.

    ``excluded_names`` carries names the caller knows are unsafe to
    rename — typically program-level globals plus any address-taken
    locals.  Function calls may write to those names through the
    pointer or directly, so the SSA renamer cannot prove the SSA
    versioning chain stays in sync with the actual slot value.
    Excluding them keeps the original un-versioned reads / writes in
    the IR, so propagation through them cannot bypass an intervening
    Call that mutated the slot.
    """
    cfg = build_cfg(body)
    has_inline_asm = any(isinstance(instruction, ir.InlineAsm) for instruction in body)
    if has_inline_asm:
        return SSAForm(cfg=cfg, phis={}, ssa_safe_names=set())
    excluded = _opaque_referenced_names(body) | excluded_names | _address_taken_names(body)
    all_destinations: set[str] = set()
    for instruction in body:
        destination = _instruction_destination(instruction)
        if isinstance(destination, str):
            all_destinations.add(destination)
    ssa_safe_names = all_destinations - excluded
    if not ssa_safe_names:
        return SSAForm(cfg=cfg, phis={}, ssa_safe_names=set())
    cfg = _split_critical_edges(cfg)
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

    Convenience wrapper around :func:`convert_from_ssa` +
    :func:`cc.cfg.flatten_cfg`.  The output is suitable for direct use
    by the existing codegen path.
    """
    return flatten_cfg(convert_from_ssa(ssa))


def optimize_ssa(body: list[ir.Instruction], /, *, excluded_names: frozenset[str] = frozenset()) -> list[ir.Instruction]:
    """Round-trip *body* through SSA, run the SSA-form passes, and flatten back.

    Bypassed for functions containing :class:`cc.ir.InlineAsm` (no
    SSA-eligible names) and for bodies that produce no SSA work — both
    cases return *body* unchanged so the optimizer pipeline keeps the
    original instruction list.  Active passes: trivial-phi removal,
    copy propagation, and dead-phi cleanup.  Before flattening, every
    SSA-versioned name is rewritten back to its un-versioned base so
    the codegen sees the original frame-slot names; the destruction
    Copies introduced for phi joins collapse to self-copies in that
    rename and are dropped.

    ``excluded_names`` is forwarded to :func:`convert_to_ssa` as the
    caller's set of names that must not participate in SSA renaming —
    typically program globals plus address-taken locals, so call-site
    propagation cannot forward a stale value across a Call that may
    have mutated the underlying slot.
    """
    ssa = convert_to_ssa(body, excluded_names=excluded_names)
    if not ssa.ssa_safe_names:
        return body
    idom = compute_dominators(ssa.cfg)
    propagated = collapsed = numbered = True
    while propagated or collapsed or numbered:
        propagated = _propagate_ssa_copies(ssa)
        collapsed = _eliminate_trivial_phis(ssa)
        numbered = _eliminate_redundant_expressions(ssa, idom=idom)
    _eliminate_dead_phis(ssa)
    _deversion_ssa_form(ssa)
    return _drop_redundant_copies(flatten_ssa_form(ssa))
