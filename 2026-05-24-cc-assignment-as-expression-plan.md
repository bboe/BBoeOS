# cc.py: assignment as expression (parens required) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach cc.py to accept parenthesized assignments (`(x = y)`, `(*p++ = ch)`, `(x += 1)`, …) as expressions whose value is the post-assignment value of the lvalue, while leaving the existing statement-form assignment paths untouched.

**Architecture:** A new `AssignExpr` AST node wraps any existing `*Assign` shape and is recognised only inside its own dedicated `( … )` in `parse_primary`. The IR builder lowers `AssignExpr` by evaluating the RHS into a temp, emitting the wrapped `*Assign` as a statement (RHS rewritten to `Var(temp)`), and yielding the temp as the expression value. Codegen for the per-lvalue stores is reused unchanged — no new emission paths.

**Tech Stack:** Python 3 (`cc/`), the existing cc.py AST / IR / x86 codegen, NASM, QEMU for the program suite.

**Spec:** [`2026-05-24-cc-assignment-as-expression-design.md`](./2026-05-24-cc-assignment-as-expression-design.md) on the `design-specs` branch.

---

## Notes for the implementing engineer

- **Branch:** create `bboe/cc-assignment-expression` off `main`. Specs live on the `design-specs` orphan branch — do not commit any spec/plan files on the feature branch.
- **Test runner:** existing cc.py tests use plain Python scripts with `subprocess` (see `tests/test_cc_casts.py` for the model). Add a new `tests/test_cc_assign_expr.py` with the same shape.
- **End-to-end check after each implementation task:** `./tests/test_cc_assign_expr.py` plus a quick `python -m compileall cc/` to catch syntax errors. After all parser/IR tasks land, run `./tests/test_programs.py` to make sure no existing C source regressed.
- **`unsigned long` lvalues are out of scope.** The IR builder already routes `unsigned long` `Assign`s through the legacy AST codegen (`ir.py:308`) because the DX:AX shape can't round-trip through an int-typed IR temp. Expression-position assignment to an `unsigned long` lvalue is rejected with a clear error; the spec's "all existing lvalue forms" requirement is satisfied for every other type.
- **Compound desugaring already exists** for the statement form in `parser.py` around line 877 (the function comment says "desugared assignment ``x = x operation rhs``"). The expression form reuses that same desugar.

---

### Task 1: Add the `AssignExpr` AST node

**Files:**
- Modify: `cc/ast_nodes.py` (insert in alphabetical order between `Assign` and `BinaryOperation`)
- Modify: `cc/parser.py` (import `AssignExpr` near the other `*Assign` imports)

- [ ] **Step 1: Add the node**

Insert in `cc/ast_nodes.py` immediately after the `Assign` class (around line 75) — keep the alphabetical ordering: `AssignExpr` sorts between `Assign` and `BinaryOperation`.

```python
class AssignExpr(Node):
    """A parenthesized assignment used as an expression.

    Wraps an underlying ``*Assign`` AST node (``Assign``, ``DerefAssign``,
    ``IndexAssign``, ``MemberAssign``, ``PointerDereferenceAssign``,
    ``DerefIncrementAssign``, ``IndexMemberAssign``, ``MemberIndexAssign``,
    ``IndexMemberIndexAssign``).  The value of the expression is the
    post-assignment value of the lvalue; the lvalue itself is not produced
    as an lvalue (chained ``a = b = c`` must be written ``a = (b = c)``).
    """

    __slots__ = ("inner", "line")

    def __init__(self, *, inner: Node, line: int) -> None:
        self.inner = inner
        self.line = line
```

- [ ] **Step 2: Wire imports in parser.py**

Add `AssignExpr` to the import block at the top of `cc/parser.py` next to `Assign` (alphabetical position).

- [ ] **Step 3: Verify the module still imports**

Run: `python3 -c "import cc.ast_nodes, cc.parser"`
Expected: no output, exit 0.

- [ ] **Step 4: Commit**

```bash
git add cc/ast_nodes.py cc/parser.py
git commit -m "feat(cc): add AssignExpr AST node for parenthesized assignment-as-expression"
```

---

### Task 2: Parser hook — recognise `( <lvalue> = <rhs> )` in `parse_primary`

**Files:**
- Modify: `cc/parser.py` — the LPAREN branch in `parse_primary` (currently at line 1407)
- Test: `tests/test_cc_assign_expr.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_cc_assign_expr.py`:

```python
#!/usr/bin/env python3
"""cc.py parenthesized assignment-as-expression coverage."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
LIBBBOEOS_INCLUDE = REPO_ROOT / "user" / "libbboeos" / "include"
_PREAMBLE = "#include <stdint.h>\n"


def compile_snippet(*, name: str, source: str, work: Path) -> str:
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    source_path.write_text(_PREAMBLE + source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", "-I", str(LIBBBOEOS_INCLUDE), str(source_path), str(asm_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cc.py failed:\n{result.stderr}")
    return asm_path.read_text()


def expect_reject(*, name: str, source: str, work: Path, needle: str) -> None:
    source_path = work / f"{name}.c"
    source_path.write_text(_PREAMBLE + source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", "-I", str(LIBBBOEOS_INCLUDE), str(source_path), str(work / f"{name}.asm")],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        raise AssertionError(f"{name}: expected rejection, cc.py accepted")
    if needle not in result.stderr:
        raise AssertionError(f"{name}: stderr lacks {needle!r}: {result.stderr}")


def test_simple_assign_in_while() -> None:
    with tempfile.TemporaryDirectory() as work:
        asm = compile_snippet(
            name="while_assign",
            source="int next(void); int main(void){ int p; while ((p = next())) { } return 0; }",
            work=Path(work),
        )
        # No structural assertion beyond a clean compile; the program suite
        # exercises runtime semantics.
        assert "main" in asm


def test_unparenthesised_assignment_in_condition_rejected() -> None:
    with tempfile.TemporaryDirectory() as work:
        expect_reject(
            name="bare_if_assign",
            source="int main(void){ int x; int y = 1; if (x = y) { } return 0; }",
            work=Path(work),
            needle="expected",  # Any parser-level message is fine; refine in Task 6.
        )


if __name__ == "__main__":
    test_simple_assign_in_while()
    test_unparenthesised_assignment_in_condition_rejected()
    print("OK")
```

- [ ] **Step 2: Run the test, confirm it fails**

Run: `./tests/test_cc_assign_expr.py`
Expected: `RuntimeError: cc.py failed` from `test_simple_assign_in_while` (the parser currently rejects `(p = next())` as an expression).

- [ ] **Step 3: Add the try-parse hook**

In `cc/parser.py`, modify the `LPAREN` branch of `parse_primary` (currently starting at line 1407, just before the `if self._is_type_start():` check). Insert the assignment try-parse as the FIRST sub-branch after `self.eat()` of LPAREN, but only when the next token is NOT a type-start (so casts still win) and looks like the start of an lvalue.

Add a helper method on the parser class (alphabetically positioned with the other `_parse_*` helpers):

```python
_ASSIGN_OP_TOKENS = (
    "ASSIGN",
    "PLUS_ASSIGN",
    "MINUS_ASSIGN",
    "STAR_ASSIGN",
    "SLASH_ASSIGN",
    "PERCENT_ASSIGN",
    "AMP_ASSIGN",
    "PIPE_ASSIGN",
    "CARET_ASSIGN",
    "LSHIFT_ASSIGN",
    "RSHIFT_ASSIGN",
)

def _try_parse_paren_assignment(self) -> Node | None:
    """Speculatively parse ``( <lvalue> <assign-op> <rhs> )`` from the
    position immediately after the opening ``(``.

    Returns ``AssignExpr(inner=<*Assign>)`` on success, or ``None`` if the
    upcoming tokens don't form a parenthesized assignment (in which case
    the parser state is rewound so the caller can fall through to a
    normal parenthesized expression).
    """
    saved = self.cursor
    line = self.peek()[2]
    try:
        statement = self._parse_assignment_statement_no_semicolon()
    except CompileError:
        self.cursor = saved
        return None
    if self.peek()[0] != "RPAREN":
        self.cursor = saved
        return None
    self.eat("RPAREN")
    return AssignExpr(inner=statement, line=line)
```

The helper `_parse_assignment_statement_no_semicolon` is a thin wrapper around the existing statement-form assignment dispatcher that returns the constructed `*Assign` node and does NOT consume the trailing `;`. The cleanest landing is to factor the body of today's `parse_simple_assignment` / `parse_compound_assignment` / `parse_member_assignment` / `parse_pointer_dereference_assignment` / `parse_index_assignment` into a single dispatch that returns the AST node without a trailing semicolon, and have the statement-form callers `eat("SEMICOLON")` themselves.

Token names above are the ones actually used by `cc/tokens.py`; verify and adjust if the lexer module uses different identifiers (e.g. `EQUAL`, `PLUS_EQ`). If a token isn't currently lexed (e.g. `LSHIFT_ASSIGN`) it can be omitted from `_ASSIGN_OP_TOKENS` for this task and added in Task 5; the spec requires all eleven but TDD-driving them lets us notice missing lexer support.

Then in the LPAREN branch of `parse_primary` (line 1407 area):

```python
if token[0] == "LPAREN":
    self.eat()
    # Cast still wins — type-start cannot also start an lvalue.
    if self._is_type_start():
        ...  # existing cast path, unchanged
    # Try parenthesized assignment: ``(lvalue <op>= rhs)``.
    assign_attempt = self._try_parse_paren_assignment()
    if assign_attempt is not None:
        return assign_attempt
    expression = self.parse_expression()
    self.eat("RPAREN")
    ...  # existing tail, unchanged
```

The `self.cursor` rewind assumes the parser exposes a cursor-style backtrack. If `Parser` instead tracks position via a token list and index, rewind by saving/restoring that index. Mirror the same idiom already used elsewhere in the file (search for an existing rewind site to copy the pattern).

- [ ] **Step 4: Run the parser-level tests**

Run: `./tests/test_cc_assign_expr.py`
Expected: both functions pass; `OK` printed.

- [ ] **Step 5: Smoke-test the full cc.py test surface**

Run: `./tests/test_programs.py` and `./tests/test_cc_casts.py` and `./tests/test_cc_local_structs.py` and `./tests/test_cc_bitfields.py`.
Expected: all green. (Confirms the LPAREN hook didn't disturb existing parses, especially casts and parenthesized expressions used today.)

- [ ] **Step 6: Commit**

```bash
git add cc/parser.py tests/test_cc_assign_expr.py
git commit -m "feat(cc): parse parenthesized assignment-as-expression in parse_primary"
```

---

### Task 3: IR lowering for `AssignExpr`

**Files:**
- Modify: `cc/ir.py` — `_build_expr` (the expression-side dispatcher; find via `grep -n "def _build_expr" cc/ir.py`)
- Modify: `tests/test_cc_assign_expr.py` — add runtime-semantics tests

- [ ] **Step 1: Write the failing semantic test**

Append to `tests/test_cc_assign_expr.py`:

```python
def test_assign_expr_value_used_in_call() -> None:
    """``f((x = 7))`` must pass 7 as the argument AND leave x == 7."""
    source = """
    int last;
    void record(int v) { last = v; }
    int main(void) {
        int x;
        record((x = 7));
        return x + last;  /* expect 14 */
    }
    """
    with tempfile.TemporaryDirectory() as work:
        compile_snippet(name="value_in_call", source=source, work=Path(work))
```

(Compile-only test for now; Task 7 promotes the strcpy idiom to a real runtime test via the program suite.)

- [ ] **Step 2: Run it, confirm it fails**

Run: `./tests/test_cc_assign_expr.py`
Expected: failure in `test_assign_expr_value_used_in_call` because the IR builder doesn't know how to lower `AssignExpr`.

- [ ] **Step 3: Add the lowering**

In `cc/ir.py`, locate `_build_expr` and add a case (or `if isinstance` branch — match the dispatch style used in that function):

```python
case ast_nodes.AssignExpr(inner=inner):
    return self._lower_assign_expr(inner, out, strings=strings)
```

Then add the helper (alphabetical position):

```python
def _lower_assign_expr(
    self,
    inner: ast_nodes.Node,
    out: list[Instruction],
    *,
    strings: list[tuple[str, str]],
) -> str:
    """Lower a parenthesized assignment to IR and return the temp holding
    its value.

    Strategy: evaluate the RHS into a temp, rewrite the wrapped ``*Assign``
    so its expression is ``Var(temp)``, emit the rewritten *Assign as a
    statement (reusing existing per-lvalue store paths), and return the
    same temp as the expression value.
    """
    line = inner.line
    # Pull the original RHS off the inner node.  Each *Assign variant uses
    # the attribute name ``expr`` for its RHS (verified in ast_nodes.py).
    original_rhs = inner.expr
    # Reject unsupported destination types up front so the failure mode
    # is a clear compile error, not a silent miscompile via the legacy
    # AST-codegen fallback that expects statement context.
    if isinstance(inner, ast_nodes.Assign) and self._var_types.get(inner.name) == "unsigned long":
        message = "assignment-as-expression to 'unsigned long' is not supported"
        raise CompileError(message, line=line)
    rhs_value = self._build_expr(original_rhs, out, strings=strings)
    temp = self._fresh_temp()
    out.append(Copy(destination=temp, source=rhs_value))
    # Rewrite the inner *Assign with the temp as its RHS, then dispatch
    # it as a statement so all the existing per-lvalue store helpers
    # (DerefAssign, IndexAssign, MemberAssign, …) run unchanged.
    rewritten = self._rebind_assign_rhs(inner, ast_nodes.Var(line=line, name=temp))
    self._build_stmt(rewritten, out, break_tgt=None, cont_tgt=None, strings=strings)
    return temp

def _rebind_assign_rhs(self, node: ast_nodes.Node, new_rhs: ast_nodes.Node) -> ast_nodes.Node:
    """Return a shallow copy of *node* with its ``expr`` slot replaced.

    All ``*Assign`` AST nodes in cc.py expose their RHS under the
    attribute name ``expr``; we construct a new instance of the same
    class with that field swapped and all other fields preserved.
    """
    import dataclasses  # noqa: PLC0415  -- local import to avoid module load cost
    if dataclasses.is_dataclass(node):
        return dataclasses.replace(node, expr=new_rhs)
    # Fall-back for non-dataclass Node subclasses: copy + reassign.
    import copy  # noqa: PLC0415
    clone = copy.copy(node)
    clone.expr = new_rhs
    return clone
```

If `cc/ast_nodes.py` doesn't use `@dataclass` (verify), drop the dataclass branch and keep only the `copy.copy` form.

- [ ] **Step 4: Run the test**

Run: `./tests/test_cc_assign_expr.py`
Expected: all functions pass.

- [ ] **Step 5: Re-run the broader cc.py suite**

Run: `./tests/test_cc_casts.py && ./tests/test_cc_local_structs.py && ./tests/test_cc_bitfields.py && ./tests/test_programs.py`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add cc/ir.py tests/test_cc_assign_expr.py
git commit -m "feat(cc): lower AssignExpr through a temp + existing store paths"
```

---

### Task 4: Coverage for every lvalue shape

**Files:**
- Modify: `tests/test_cc_assign_expr.py`

- [ ] **Step 1: Add one compile-only test per lvalue shape**

Append to `tests/test_cc_assign_expr.py`. Each test compiles a snippet that uses the named shape inside `(... = ...)` in expression position; any cc.py failure surfaces as `RuntimeError`.

```python
def _compile(name: str, source: str) -> None:
    with tempfile.TemporaryDirectory() as work:
        compile_snippet(name=name, source=source, work=Path(work))


def test_var_lvalue() -> None:
    _compile("var", "int main(void){int x; return (x = 1) + 1;}")


def test_pointer_deref_lvalue() -> None:
    _compile(
        "deref",
        "int main(void){int x; int *p = &x; return (*p = 7);}",
    )


def test_pointer_deref_postinc_lvalue() -> None:
    _compile(
        "deref_postinc",
        "int main(void){char b[2]; char *p = b; (*p++ = 'a'); return b[0];}",
    )


def test_array_index_lvalue() -> None:
    _compile(
        "index",
        "int main(void){int a[4]; return (a[2] = 9);}",
    )


def test_struct_member_lvalue() -> None:
    _compile(
        "member",
        "struct S { int f; }; int main(void){struct S s; return (s.f = 3);}",
    )


def test_struct_arrow_member_lvalue() -> None:
    _compile(
        "arrow",
        "struct S { int f; }; int main(void){struct S s; struct S *p = &s; return (p->f = 5);}",
    )


def test_indexed_member_lvalue() -> None:
    _compile(
        "index_member",
        "struct S { int f; }; int main(void){struct S a[4]; return (a[1].f = 2);}",
    )


def test_member_index_lvalue() -> None:
    _compile(
        "member_index",
        "struct S { int a[4]; }; int main(void){struct S s; return (s.a[2] = 6);}",
    )
```

Add corresponding calls in the `__main__` block.

- [ ] **Step 2: Run the tests**

Run: `./tests/test_cc_assign_expr.py`
Expected: every test passes. If any single shape fails, the failure is isolated to one `*Assign` variant and the fix is in `_rebind_assign_rhs` (e.g. the attribute name for RHS isn't `expr` for that specific class — check `cc/ast_nodes.py`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_cc_assign_expr.py
git commit -m "test(cc): cover every lvalue shape for assignment-as-expression"
```

---

### Task 5: Compound assignment operators

**Files:**
- Modify: `cc/parser.py` (`_ASSIGN_OP_TOKENS` and any compound-assignment dispatch)
- Modify: `tests/test_cc_assign_expr.py`

- [ ] **Step 1: Write failing tests for each compound op**

Append:

```python
def test_compound_plus_assign() -> None:
    _compile("c_plus", "int main(void){int x = 1; return (x += 2);}")


def test_compound_minus_assign() -> None:
    _compile("c_minus", "int main(void){int x = 5; return (x -= 2);}")


def test_compound_star_assign() -> None:
    _compile("c_star", "int main(void){int x = 3; return (x *= 2);}")


def test_compound_slash_assign() -> None:
    _compile("c_slash", "int main(void){int x = 8; return (x /= 2);}")


def test_compound_percent_assign() -> None:
    _compile("c_percent", "int main(void){int x = 9; return (x %= 4);}")


def test_compound_amp_assign() -> None:
    _compile("c_amp", "int main(void){unsigned int x = 0xF0; return (int)(x &= 0x0F);}")


def test_compound_pipe_assign() -> None:
    _compile("c_pipe", "int main(void){unsigned int x = 0xF0; return (int)(x |= 0x0F);}")


def test_compound_caret_assign() -> None:
    _compile("c_caret", "int main(void){unsigned int x = 0xFF; return (int)(x ^= 0x0F);}")


def test_compound_lshift_assign() -> None:
    _compile("c_lshift", "int main(void){int x = 1; return (x <<= 3);}")


def test_compound_rshift_assign() -> None:
    _compile("c_rshift", "int main(void){int x = 8; return (x >>= 1);}")
```

- [ ] **Step 2: Run them, observe failures**

Run: `./tests/test_cc_assign_expr.py`
Expected: any failing compound op tells you which `*_ASSIGN` token is missing from `_ASSIGN_OP_TOKENS` or which compound-assignment statement parser path doesn't yet route through `_parse_assignment_statement_no_semicolon`.

- [ ] **Step 3: Extend the speculative parser**

For each failing op, ensure (a) the token name is in `_ASSIGN_OP_TOKENS`, and (b) the existing compound-assignment statement parser is reachable from `_parse_assignment_statement_no_semicolon` and returns the desugared `Assign` (or `*Assign`) node without consuming `SEMICOLON`. The existing statement form already desugars `x op= y` to `x = (x op y)` (see `parser.py` near line 877) — reuse it.

- [ ] **Step 4: Re-run and confirm**

Run: `./tests/test_cc_assign_expr.py`
Expected: all eleven assignment operators pass.

- [ ] **Step 5: Commit**

```bash
git add cc/parser.py tests/test_cc_assign_expr.py
git commit -m "feat(cc): assignment-as-expression covers all compound operators"
```

---

### Task 6: Rejection tests — diagnose unparenthesised forms cleanly

**Files:**
- Modify: `tests/test_cc_assign_expr.py`

- [ ] **Step 1: Add rejection coverage**

```python
def test_reject_bare_if_assignment() -> None:
    with tempfile.TemporaryDirectory() as work:
        expect_reject(
            name="bare_if",
            source="int main(void){int x; int y=1; if (x = y) {} return 0;}",
            work=Path(work),
            needle="",  # any non-zero exit is acceptable
        )


def test_reject_bare_call_arg_assignment() -> None:
    with tempfile.TemporaryDirectory() as work:
        expect_reject(
            name="bare_call",
            source="void f(int); int main(void){int x; f(x = 1); return 0;}",
            work=Path(work),
            needle="",
        )


def test_reject_chained_without_parens() -> None:
    with tempfile.TemporaryDirectory() as work:
        expect_reject(
            name="chain",
            source="int main(void){int a; int b; int c = 1; a = b = c; return a;}",
            work=Path(work),
            needle="",
        )


def test_accept_chained_with_parens() -> None:
    _compile("chain_ok", "int main(void){int a; int b; int c = 1; a = (b = c); return a;}")


def test_reject_assignment_as_lvalue() -> None:
    with tempfile.TemporaryDirectory() as work:
        expect_reject(
            name="lvalue",
            source="int main(void){int x; int y; int z = 1; ((x = y)) = z; return 0;}",
            work=Path(work),
            needle="",
        )
```

Loosen `expect_reject` to accept `needle == ""` meaning "any non-zero exit":

```python
def expect_reject(*, name: str, source: str, work: Path, needle: str) -> None:
    ...
    if result.returncode == 0:
        raise AssertionError(f"{name}: expected rejection, cc.py accepted")
    if needle and needle not in result.stderr:
        raise AssertionError(f"{name}: stderr lacks {needle!r}: {result.stderr}")
```

- [ ] **Step 2: Run and confirm all rejections fire**

Run: `./tests/test_cc_assign_expr.py`
Expected: every test passes. If `test_accept_chained_with_parens` fails it usually means `_try_parse_paren_assignment` consumed the outer `(b = c)` correctly but the OUTER `a = (b = c);` (a statement) isn't routed through the same RHS parser path — check the statement-form RHS parser uses `parse_expression`, which already calls into `parse_primary`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_cc_assign_expr.py
git commit -m "test(cc): negative coverage for unparenthesised assignment-as-expression"
```

---

### Task 7: Re-enable the `strcpy` idiom in `user/libc/string.c`

**Files:**
- Modify: `user/libc/string.c` (around line 210 per the task brief — confirm exact location with `grep -n "dst\|strcpy" user/libc/string.c`)
- Run: `./make_os.sh` and the program suite

- [ ] **Step 1: Read the current workaround**

Run: `rtk proxy grep -n "while" user/libc/string.c | head` (or open the file). Identify the hand-unrolled `strcpy` loop that exists today because `while ((*dst++ = *src++))` wasn't accepted.

- [ ] **Step 2: Replace with the idiomatic loop**

Edit `user/libc/string.c` to use:

```c
char *strcpy(char *dst, const char *src) {
    char *r = dst;
    while ((*dst++ = *src++)) { }
    return r;
}
```

(Adjust the surrounding signature/return-pointer plumbing to match the file's existing style.)

- [ ] **Step 3: Build the OS image**

Run: `./make_os.sh`
Expected: clean build; `os.bin` size delta is small (a handful of bytes either way).

- [ ] **Step 4: Run the regression suites**

Run, sequentially:
- `./tests/test_asm.py` — assembler self-host
- `./tests/test_programs.py` — userland programs
- `./tests/test_bboefs.py` — filesystem
- `./tests/test_programs.py --filesystem ext2` — ext2 path

Expected: green across all four.

- [ ] **Step 5: Commit**

```bash
git add user/libc/string.c
git commit -m "feat(libc): use the idiomatic ((*dst++ = *src++)) strcpy loop"
```

---

### Task 8: CHANGELOG and short docs note

**Files:**
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/cc_future_work.md` (strike the assignment-as-expression line if present)

- [ ] **Step 1: Add a CHANGELOG entry under Unreleased**

```markdown
- cc.py now accepts parenthesized assignments as expressions
  (`while ((p = next()))`, `f((x = 7))`, `a = (b = c)`).  The
  enclosing parentheses are required and dedicated to the
  assignment — `if (x = y)` and `f(x = y)` are still rejected,
  matching the GCC `-Wparentheses` idiom.  Covers all eleven
  assignment operators across every existing lvalue shape;
  `unsigned long` lvalues remain statement-only.
```

- [ ] **Step 2: Strike the future-work item (if present)**

If `docs/cc_future_work.md` mentions assignment-as-expression as a TODO, remove that bullet or move it under a "completed" / "shipped" subsection per the file's existing convention.

- [ ] **Step 3: Commit**

```bash
git add docs/CHANGELOG.md docs/cc_future_work.md
git commit -m "docs(cc): note parenthesized assignment-as-expression support"
```

---

### Task 9: Final verification

- [ ] **Step 1: Re-run the full local CI matrix**

The memory note "Run full CI matrix locally on big changes" applies to compiler frontend changes. Run every job listed in `.github/workflows/test.yml` that doesn't require external infra. At a minimum:

- `./tests/test_asm.py`
- `./tests/test_bboefs.py`
- `./tests/test_programs.py`
- `./tests/test_programs.py --filesystem ext2`
- `./tests/test_cc_compatibility.py`
- `./tests/test_cc_casts.py`
- `./tests/test_cc_local_structs.py`
- `./tests/test_cc_bitfields.py`
- `./tests/test_cc_assign_expr.py` (new)

Expected: every suite green.

- [ ] **Step 2: Push the branch and open a PR**

The PR description should link to the spec on `design-specs` and mention the unblocked `user/libc/string.c` idiom.

---

## Self-review checklist (run after writing the plan, fix inline)

- **Spec coverage:**
  - Surface rule → Task 2 (parser hook only fires inside dedicated parens).
  - All eleven operators → Task 5.
  - All existing lvalue shapes → Task 4 covers each; Task 3 establishes the rebind mechanism that scales to all of them.
  - Result value & type → Task 3 (temp holds the post-conversion value; type follows the RHS path).
  - Not-an-lvalue → Task 6 (rejection test).
  - Statement form untouched → Task 2 only adds a new branch in `parse_primary`, leaves statement-form parsers in place.
  - Tests for `string.c` idiom → Task 7.
  - Out-of-scope items called out: `unsigned long` lvalues in Task 3, comma operator / statement-expressions never mentioned.

- **Placeholder scan:** no TBDs; one verification asterisk ("verify token names against `cc/tokens.py`") which is concrete enough for the implementer to act on without guessing.

- **Type consistency:** `_try_parse_paren_assignment`, `_parse_assignment_statement_no_semicolon`, `_lower_assign_expr`, `_rebind_assign_rhs` names used consistently across tasks. `AssignExpr.inner` attribute referenced in Task 3 matches the Task 1 definition.
