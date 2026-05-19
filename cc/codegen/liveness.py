"""Liveness analysis for cc.py's register allocator.

Computes per-statement live-in and live-out sets for every local in a
function body, then derives the interference graph the allocator
consumes to decide which locals can share registers.

The analyzer walks the AST in source order, computing a successor
relation that mirrors the control-flow shape of each statement
(``if`` / ``while`` / ``do``-``while`` / ``switch`` / ``Compound``
/ ``break`` / ``continue`` / ``goto`` / ``return``).  Backward
dataflow then iterates to a fixed point, producing live sets that
the interference computation reads.

Public API:

    analyzer = LivenessAnalyzer(body=function.body, parameters=function.params)
    interference = analyzer.interference()
    # interference: dict[str, set[str]] — name -> names that overlap

The analyzer raises ``LivenessAnalysisError`` for any AST node shape it
does not model explicitly so a silent miscompile cannot result from a
new AST kind being added without updating the use/def coverage here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cc.ast_nodes import (
    AddressOf,
    ArrayDecl,
    ArrayInit,
    Assign,
    BinaryOperation,
    Break,
    Call,
    Char,
    Compound,
    Conditional,
    Continue,
    DerefAssign,
    DoubleIndex,
    DoWhile,
    Goto,
    If,
    Index,
    IndexAssign,
    InlineAsm,
    Int,
    Label,
    LogicalAnd,
    LogicalOr,
    MemberAccess,
    MemberAssign,
    MemberIndex,
    Node,
    Param,
    PointerDereferenceAssign,
    Return,
    SizeofType,
    SizeofVar,
    String,
    Switch,
    TailCall,
    Var,
    VarDecl,
    While,
)

#: Sentinel statement id for the function exit (return / fall-off
#: end-of-body).  Successor edges that should leave the function
#: target this id; it carries no use/def state of its own.
EXIT_ID = -1


class LivenessAnalysisError(Exception):
    """Raised when the analyzer encounters an AST node it does not model.

    The allocator relies on use/def coverage being exhaustive for
    every node type that may appear in a function body.  A silent
    fallback that recorded zero definitions and walked sub-nodes for uses
    risks understating interference (which can cause a miscompile
    when the allocator shares registers between two locals that the
    analyzer thought were dead).  Failing loud forces the analyzer
    to be updated when a new AST shape lands.
    """


@dataclass(slots=True)
class StatementInfo:
    """Per-statement liveness state."""

    definitions: set[str] = field(default_factory=set)
    live_in: set[str] = field(default_factory=set)
    live_out: set[str] = field(default_factory=set)
    successors: list[int] = field(default_factory=list)
    uses: set[str] = field(default_factory=set)


class LivenessAnalyzer:
    """Compute liveness and interference for a function body.

    ``body`` is the function's top-level statement list.
    ``parameters`` are treated as defined at function entry (live-in
    of the first statement) so a parameter read before being
    overwritten still records correct liveness.

    Construction does not run the analysis.  Call :meth:`analyze`
    to populate ``statements`` and :meth:`interference` to derive
    the interference adjacency map.
    """

    def __init__(self, *, body: list[Node], parameters: list[Param] | None = None) -> None:
        """Store *body* / *parameters* and initialise the empty state maps."""
        # Were we asked to compute already?
        self._analyzed = False
        # Loop context for break/continue: stack of
        # (continue_target_id, break_target_id).
        self._loop_stack: list[tuple[int, int]] = []
        # Monotonic statement-id counter.
        self._next_id = 0
        # Unresolved goto: (statement_id, target_label).
        self._pending_gotos: list[tuple[int, str]] = []
        # Switch context for break: stack of break_target_id.
        self._switch_break_stack: list[int] = []
        self.body = body
        # statement_id -> AST node (reverse of node_to_id).
        self.id_to_node: dict[int, Node] = {}
        # Label name -> statement_id of the labelled statement.
        self.labels: dict[str, int] = {}
        # id(AST node) -> statement_id.
        self.node_to_id: dict[int, int] = {}
        self.parameters: list[Param] = list(parameters or [])
        # statement_id -> StatementInfo.
        self.statements: dict[int, StatementInfo] = {}

    def _add_expression_uses(self, expression: object, accumulator: set[str]) -> None:
        """Walk *expression* recording every ``Var`` name as a use.

        Raises:
            LivenessAnalysisError: when *expression* is a ``Node`` type
                the analyzer does not model.  Plain Python values that
                appear as expression operands (None, str, etc.) are
                ignored so callers can pass through optional fields.

        """
        if expression is None:
            return
        if isinstance(expression, (Int, Char, String, SizeofType, SizeofVar)):
            # Leaf literals — no Var uses.
            return
        if isinstance(expression, Var):
            accumulator.add(expression.name)
            return
        if isinstance(expression, AddressOf):
            # ``&x`` references x's address — record the use; the
            # allocator separately disqualifies address-taken locals
            # from register pinning.
            if isinstance(expression.var, Var):
                accumulator.add(expression.var.name)
            return
        if isinstance(expression, Index):
            accumulator.add(expression.array.name)
            self._add_expression_uses(expression.index, accumulator)
            return
        if isinstance(expression, DoubleIndex):
            accumulator.add(expression.array.name)
            self._add_expression_uses(expression.outer_index, accumulator)
            self._add_expression_uses(expression.inner_index, accumulator)
            return
        if isinstance(expression, BinaryOperation):
            self._add_expression_uses(expression.left, accumulator)
            self._add_expression_uses(expression.right, accumulator)
            return
        if isinstance(expression, (LogicalAnd, LogicalOr)):
            self._add_expression_uses(expression.left, accumulator)
            self._add_expression_uses(expression.right, accumulator)
            return
        if isinstance(expression, Conditional):
            self._add_expression_uses(expression.condition, accumulator)
            self._add_expression_uses(expression.then_expr, accumulator)
            self._add_expression_uses(expression.else_expr, accumulator)
            return
        if isinstance(expression, Call):
            for argument in expression.args:
                self._add_expression_uses(argument, accumulator)
            return
        if isinstance(expression, (MemberAccess, MemberIndex)):
            accumulator.add(expression.object_name)
            if isinstance(expression, MemberIndex):
                self._add_expression_uses(expression.index, accumulator)
            return
        if isinstance(expression, Node):
            message = f"liveness: unhandled expression node {type(expression).__name__}"
            raise LivenessAnalysisError(message)

    def _build_control_flow_graph(self, body: list[Node], *, fallthrough: int) -> int:
        """Wire successor edges across *body*.

        Returns the entry statement id of *body* (the first
        statement), or *fallthrough* when the body is empty.  Each
        statement's ``successors`` field is populated in place.
        """
        if not body:
            return fallthrough
        entry = self.node_to_id[id(body[0])]
        for index, statement in enumerate(body):
            next_id = self.node_to_id[id(body[index + 1])] if index + 1 < len(body) else fallthrough
            self._wire_statement(statement, fallthrough=next_id)
            self._collect_use_def(statement)
        return entry

    def _build_control_flow_graph_loop(self, body: list[Node], *, continue_id: int, break_id: int) -> int:
        """Wire the control-flow graph for a loop body, handling break/continue context."""
        self._loop_stack.append((continue_id, break_id))
        try:
            return self._build_control_flow_graph(body, fallthrough=continue_id)
        finally:
            self._loop_stack.pop()

    def _collect_labels(self, body: list[Node]) -> None:
        """First pass: number every statement and record Label name → id.

        Labels need stable ids before any goto edge can be added,
        so this pre-walk assigns ids in source order.  Other CFG
        edges are populated by :meth:`_build_control_flow_graph`,
        which re-walks the same nodes and reuses the ids set here.
        """
        for statement in body:
            self._number_statement(statement)

    def _collect_use_def(self, statement: Node) -> None:
        """Populate ``uses`` and ``definitions`` for *statement* — non-control-flow part.

        Control-flow successors are wired separately (above); this
        method just walks *statement*'s expression / value subtree to
        record which names it reads and writes.

        Raises:
            LivenessAnalysisError: when *statement*'s node type is not
                explicitly handled.  Forces the analyzer to be updated
                when a new AST shape lands rather than silently treating
                the node as use/def-free.

        """
        statement_id = self.node_to_id[id(statement)]
        statement_info = self.statements[statement_id]
        if isinstance(statement, VarDecl):
            statement_info.definitions.add(statement.name)
            if statement.init is not None:
                self._add_expression_uses(statement.init, statement_info.uses)
            return
        if isinstance(statement, ArrayDecl):
            statement_info.definitions.add(statement.name)
            if isinstance(statement.init, ArrayInit):
                for element in statement.init.elements:
                    self._add_expression_uses(element, statement_info.uses)
            return
        if isinstance(statement, Assign):
            statement_info.definitions.add(statement.name)
            self._add_expression_uses(statement.expr, statement_info.uses)
            return
        if isinstance(statement, IndexAssign):
            # The array name is *read* (its address used as base) and
            # the slot it points at is written — we treat this as a
            # read of the var (no def of the local) so subsequent uses
            # of the array name continue to be live.
            statement_info.uses.add(statement.array.name)
            self._add_expression_uses(statement.index, statement_info.uses)
            self._add_expression_uses(statement.expr, statement_info.uses)
            return
        if isinstance(statement, DerefAssign):
            statement_info.uses.add(statement.pointer.name)
            self._add_expression_uses(statement.expr, statement_info.uses)
            return
        if isinstance(statement, PointerDereferenceAssign):
            self._add_expression_uses(statement.address, statement_info.uses)
            self._add_expression_uses(statement.value, statement_info.uses)
            return
        if isinstance(statement, MemberAssign):
            statement_info.uses.add(statement.object_name)
            self._add_expression_uses(statement.expr, statement_info.uses)
            return
        if isinstance(statement, Return):
            if statement.value is not None:
                self._add_expression_uses(statement.value, statement_info.uses)
            return
        if isinstance(statement, (DoWhile, If, While)):
            self._add_expression_uses(statement.cond, statement_info.uses)
            return
        if isinstance(statement, Switch):
            self._add_expression_uses(statement.discriminant, statement_info.uses)
            return
        if isinstance(statement, (Call, TailCall)):
            for argument in statement.args:
                self._add_expression_uses(argument, statement_info.uses)
            return
        if isinstance(statement, (Break, Compound, Continue, Goto, InlineAsm, Label)):
            # No expression operands at the statement level.
            return
        message = f"liveness: unhandled statement node {type(statement).__name__}"
        raise LivenessAnalysisError(message)

    def _fixed_point(self) -> None:
        """Backward dataflow until live-in / live-out sets stabilize."""
        # Reverse postorder is friendlier for backward dataflow but
        # cc.py functions are small enough that any order converges
        # quickly.  Iterate while any set changed.
        changed = True
        while changed:
            changed = False
            for statement_info in self.statements.values():
                new_out: set[str] = set()
                for successor in statement_info.successors:
                    if successor == EXIT_ID:
                        continue
                    new_out |= self.statements[successor].live_in
                new_in = (new_out - statement_info.definitions) | statement_info.uses
                if new_in != statement_info.live_in or new_out != statement_info.live_out:
                    statement_info.live_in = new_in
                    statement_info.live_out = new_out
                    changed = True

    def _new_id(self, node: Node) -> int:
        """Assign a fresh statement id to *node* and create its statement-info record."""
        statement_id = self._next_id
        self._next_id += 1
        self.node_to_id[id(node)] = statement_id
        self.id_to_node[statement_id] = node
        self.statements[statement_id] = StatementInfo()
        return statement_id

    def _new_id_for_entry(self) -> int:
        """Allocate a statement id for the synthetic ENTRY pseudo-statement.

        Mirrors :meth:`_new_id` but takes no AST node — ENTRY has no
        corresponding source-level statement; it only carries
        ``definitions`` (the parameter set) and a single successor edge
        into the real body entry.
        """
        statement_id = self._next_id
        self._next_id += 1
        self.statements[statement_id] = StatementInfo()
        return statement_id

    def _number_statement(self, statement: Node) -> None:
        """Recursively assign ids to *statement* and any nested statements."""
        if id(statement) in self.node_to_id:
            return
        statement_id = self._new_id(statement)
        if isinstance(statement, Label):
            self.labels[statement.name] = statement_id
        if isinstance(statement, If):
            for child in statement.body:
                self._number_statement(child)
            if statement.else_body is not None:
                for child in statement.else_body:
                    self._number_statement(child)
        elif isinstance(statement, (DoWhile, While)):
            for child in statement.body:
                self._number_statement(child)
        elif isinstance(statement, Switch):
            for case in statement.cases:
                for child in case.body:
                    self._number_statement(child)
        elif isinstance(statement, Compound):
            for child in statement.body:
                self._number_statement(child)

    def _wire_statement(self, statement: Node, *, fallthrough: int) -> None:
        """Populate successor edges + control flow for *statement*."""
        statement_id = self.node_to_id[id(statement)]
        statement_info = self.statements[statement_id]
        if isinstance(statement, Return):
            statement_info.successors = [EXIT_ID]
            return
        if isinstance(statement, Break):
            if self._switch_break_stack and (not self._loop_stack or self._switch_break_stack[-1] > self._loop_stack[-1][1]):
                statement_info.successors = [self._switch_break_stack[-1]]
            elif self._loop_stack:
                statement_info.successors = [self._loop_stack[-1][1]]
            else:
                statement_info.successors = [EXIT_ID]
            return
        if isinstance(statement, Continue):
            if self._loop_stack:
                statement_info.successors = [self._loop_stack[-1][0]]
            else:
                statement_info.successors = [EXIT_ID]
            return
        if isinstance(statement, Goto):
            self._pending_gotos.append((statement_id, statement.name))
            return
        if isinstance(statement, If):
            body_entry = self._build_control_flow_graph(statement.body, fallthrough=fallthrough)
            if statement.else_body is not None:
                else_entry = self._build_control_flow_graph(statement.else_body, fallthrough=fallthrough)
                statement_info.successors = [body_entry, else_entry]
            else:
                statement_info.successors = [body_entry, fallthrough]
            return
        if isinstance(statement, While):
            # cond evaluates → body OR after-loop.  Last body statement
            # falls back to the cond (back-edge).
            body_entry = self._build_control_flow_graph_loop(statement.body, continue_id=statement_id, break_id=fallthrough)
            statement_info.successors = [body_entry, fallthrough]
            return
        if isinstance(statement, DoWhile):
            # body runs first; its last statement falls into the cond
            # (which is *this* statement's id).  cond → body_entry
            # OR after-loop.
            body_entry = self._build_control_flow_graph_loop(statement.body, continue_id=statement_id, break_id=fallthrough)
            statement_info.successors = [body_entry, fallthrough]
            return
        if isinstance(statement, Switch):
            # Each case entry is reachable from the dispatch.  If
            # no default, the dispatch can also fall through to
            # after-switch (when no case matches).
            self._switch_break_stack.append(fallthrough)
            try:
                case_entries: list[int] = []
                for index, case in enumerate(statement.cases):
                    next_case_first = (
                        self.node_to_id[id(statement.cases[index + 1].body[0])]
                        if index + 1 < len(statement.cases) and statement.cases[index + 1].body
                        else fallthrough
                    )
                    case_fallthrough = next_case_first
                    body_entry = self._build_control_flow_graph(case.body, fallthrough=case_fallthrough)
                    case_entries.append(body_entry)
                has_default = any(case.value is None for case in statement.cases)
                successors = list(case_entries)
                if not has_default:
                    successors.append(fallthrough)
                statement_info.successors = successors
            finally:
                self._switch_break_stack.pop()
            return
        if isinstance(statement, Compound):
            body_entry = self._build_control_flow_graph(statement.body, fallthrough=fallthrough)
            statement_info.successors = [body_entry]
            return
        if isinstance(statement, Label):
            # Label itself just falls through to whatever follows.
            statement_info.successors = [fallthrough]
            return
        # Default: linear fall-through.  Unknown statement kinds are
        # still wired with a single fallthrough edge — the
        # ``_collect_use_def`` pass raises ``LivenessAnalysisError`` on
        # the same nodes, so the unknown shape is caught loudly there.
        statement_info.successors = [fallthrough]

    def analyze(self) -> dict[int, StatementInfo]:
        """Run the analysis; return the per-statement state map.

        Idempotent: repeated calls return the cached map.
        """
        if self._analyzed:
            return self.statements
        self._collect_labels(self.body)
        body_entry = self._build_control_flow_graph(self.body, fallthrough=EXIT_ID)
        for statement_id, label in self._pending_gotos:
            target = self.labels.get(label, EXIT_ID)
            self.statements[statement_id].successors = [target]
        # Synthetic ENTRY: parameters are defined here so liveness sees
        # them as live-out at the function entry edge.  Without this, a
        # parameter only used (never re-assigned) would never appear in
        # any ``definitions`` set, and so wouldn't interfere with body
        # locals that are also live at the same program point — leading
        # the allocator to share a register between a parameter and an
        # unrelated body local.
        entry_id = self._new_id_for_entry()
        entry_info = self.statements[entry_id]
        entry_info.definitions = {parameter.name for parameter in self.parameters}
        entry_info.successors = [body_entry]
        self._fixed_point()
        self._analyzed = True
        return self.statements

    def interference(self) -> dict[str, set[str]]:
        """Return ``{var: set(overlapping_vars)}``.

        Two vars interfere if there is any program point at which
        both are live (live_out), or one is defined while the other
        is live (classic Chaitin formulation).
        """
        self.analyze()
        adjacency: dict[str, set[str]] = {}
        for statement_info in self.statements.values():
            live = statement_info.live_out
            # Pairwise interference across the live-out set.
            live_list = list(live)
            for index, name_a in enumerate(live_list):
                for name_b in live_list[index + 1 :]:
                    adjacency.setdefault(name_a, set()).add(name_b)
                    adjacency.setdefault(name_b, set()).add(name_a)
            # Def vs live-out: a def while another var is live-out
            # makes them interfere (the live var spans the def site).
            for defined in statement_info.definitions:
                for other in live:
                    if other == defined:
                        continue
                    adjacency.setdefault(defined, set()).add(other)
                    adjacency.setdefault(other, set()).add(defined)
        return adjacency
