# cc.py: `va_arg(ap, double)` + `sizeof(expression)` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach cc.py two small features: (1) `va_arg(ap, double)` advances the va-list cursor by 8 bytes instead of 4; (2) `sizeof(expression)` evaluates the type of an arbitrary expression at compile time and returns its size.

**Architecture:** Feature A adds a `VaArg` AST node that retains the type string the parser currently discards, so codegen can pick the advance size. Feature B adds a `SizeofExpr` AST node plus a codegen-time `_expression_type` helper that infers the type of an arbitrary parsed expression by walking its AST structure, then delegates to the existing `_type_size` for the size constant.

**Tech Stack:** Python 3 (`cc/`), NASM, QEMU for the program suite.

**Spec:** [`2026-05-24-va-arg-double-sizeof-expr-design.md`](https://github.com/bboe/BBoeOS/blob/design-specs/2026-05-24-va-arg-double-sizeof-expr-design.md) on the `design-specs` branch.

---

## Notes for the implementing engineer

- **Branch:** create `bboe/cc-va-arg-sizeof-expr` off `main`.
- **Test runner:** existing cc.py tests use plain Python scripts with `subprocess`. Add a new `tests/test_cc_va_arg_sizeof.py` for both features (they share a PR).
- **Key files:**
  - `cc/ast_nodes.py` — add `VaArg` and `SizeofExpr` dataclasses
  - `cc/parser.py` — modify `parse_primary` (`__builtin_va_arg` branch) and `parse_sizeof`
  - `cc/ir.py` — add `_build_expr` cases for both new nodes
  - `cc/codegen/x86/builtins.py` — modify `builtin___builtin_va_arg` to accept `VaArg` node
  - `cc/codegen/x86/emission.py` — add `SizeofExpr` expression branch and `_expression_type` helper
  - `cc/codegen/x86/generator.py` — `_expression_type` may need access to `struct_layouts`, `struct_sizes`, `variable_types` (all already on the generator mixin)

---

### Task 1: `VaArg` — AST node, parser, codegen, tests

**Files:**
- Modify: `cc/ast_nodes.py` (add `VaArg` class)
- Modify: `cc/parser.py` (change `__builtin_va_arg` branch in `parse_primary`)
- Modify: `cc/ir.py` (add `VaArg` case in `_build_expr`)
- Modify: `cc/codegen/x86/builtins.py` (update `builtin___builtin_va_arg`)
- Modify: `cc/codegen/x86/emission.py` (add `VaArg` to `generate_expression` dispatch)
- Create: `tests/test_cc_va_arg_sizeof.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cc_va_arg_sizeof.py`:

```python
#!/usr/bin/env python3
"""cc.py va_arg(ap, double) and sizeof(expression) coverage."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
LIBBBOEOS_INCLUDE = REPO_ROOT / "user" / "libbboeos" / "include"
_PREAMBLE = "#include <stdint.h>\n#include <stdarg.h>\n"


def _compile(name: str, source: str) -> str:
    with tempfile.TemporaryDirectory() as work:
        return compile_snippet(name=name, source=source, work=Path(work))


def compile_snippet(*, name: str, source: str, work: Path) -> str:
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    source_path.write_text(_PREAMBLE + source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", "-I", str(LIBBBOEOS_INCLUDE), str(source_path), str(asm_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        message = f"cc.py failed:\n{result.stderr}"
        raise RuntimeError(message)
    return asm_path.read_text()


def test_va_arg_double_advances_by_8() -> None:
    """va_arg(ap, double) must advance the cursor by 8, not 4."""
    source = """
    void f(int dummy, ...) {
        va_list ap;
        va_start(ap, dummy);
        (void)va_arg(ap, double);
        int after = va_arg(ap, int);
        va_end(ap);
    }
    """
    asm = _compile("va_double", source)
    # The double va_arg should emit "add <reg>, 8" somewhere in _f.
    f_body = asm.split("_f:", 1)[1].split("\n_", 1)[0]
    assert "8" in f_body, f"expected advance by 8 in _f body:\n{f_body}"


def test_va_arg_int_still_advances_by_4() -> None:
    """va_arg(ap, int) must still advance by 4 (regression guard)."""
    source = """
    void f(int dummy, ...) {
        va_list ap;
        va_start(ap, dummy);
        int x = va_arg(ap, int);
        va_end(ap);
    }
    """
    asm = _compile("va_int", source)
    f_body = asm.split("_f:", 1)[1].split("\n_", 1)[0]
    # Should NOT contain "add <reg>, 8" for int.
    lines_with_add_8 = [line for line in f_body.splitlines() if "8" in line and "add" in line]
    assert not lines_with_add_8, f"unexpected add-by-8 for int va_arg:\n{lines_with_add_8}"


if __name__ == "__main__":
    test_va_arg_double_advances_by_8()
    test_va_arg_int_still_advances_by_4()
    print("OK")
```

Run `chmod +x tests/test_cc_va_arg_sizeof.py` and `./tests/test_cc_va_arg_sizeof.py`. Confirm `test_va_arg_double_advances_by_8` fails (cc.py does not yet retain the `double` type).

- [ ] **Step 2: Add the `VaArg` AST node**

In `cc/ast_nodes.py`, add in alphabetical position (after `TailCall`, before `Var`):

```python
@dataclass(kw_only=True, slots=True)
class VaArg(Node):
    """``va_arg(ap, T)`` expression — read a variadic argument and advance the cursor."""

    cursor: Node
    type_name: str
```

- [ ] **Step 3: Update the parser**

In `cc/parser.py`, import `VaArg` (alphabetical position in the import block). Then modify the `__builtin_va_arg` branch inside `parse_primary` (currently around line 1397). Change:

```python
if token[1] == "__builtin_va_arg":
    self.eat("LPAREN")
    cursor_arg = self.parse_expression()
    self.eat("COMMA")
    self.parse_type()  # T parsed and discarded
    self.eat("RPAREN")
    return Call(args=[cursor_arg], line=line, name=token[1])
```

to:

```python
if token[1] == "__builtin_va_arg":
    self.eat("LPAREN")
    cursor_arg = self.parse_expression()
    self.eat("COMMA")
    type_name = self.parse_type()
    self.eat("RPAREN")
    return VaArg(cursor=cursor_arg, line=line, type_name=type_name)
```

- [ ] **Step 4: Add IR builder case**

In `cc/ir.py`, import `VaArg` (alphabetical). In `_build_expr`, add a case that delegates to the AST codegen:

```python
case ast_nodes.VaArg():
    temp = self._tmp()
    out.append(Block(node=ast_nodes.Assign(expr=stmt, name=temp)))
    return temp
```

Wait — `VaArg` is an expression, not a statement. The pattern used by other expression-like nodes that need AST codegen (e.g. `DerefIncrement`, complex `Call`) is:

```python
case ast_nodes.VaArg():
    temp = self._tmp()
    out.append(Block(node=ast_nodes.Assign(expr=expression, name=temp)))
    return temp
```

Look at how `DerefIncrement` or `Call` is handled in `_build_expr` and match the same pattern. The key is: wrap the `VaArg` expression in an `Assign(name=temp, expr=VaArg(...))` inside a `Block`, then return `temp`.

- [ ] **Step 5: Update codegen**

In `cc/codegen/x86/emission.py`, import `VaArg` (alphabetical). Add a `VaArg` branch in `generate_expression` (alphabetical position among the `elif isinstance(...)` chain):

```python
elif isinstance(expression, VaArg):
    self.builtin___builtin_va_arg([expression.cursor], advance_size=self._type_size(expression.type_name))
```

Then modify `builtin___builtin_va_arg` in `cc/codegen/x86/builtins.py` to accept an optional `advance_size` parameter. Change its signature to:

```python
def builtin___builtin_va_arg(self, arguments: list[Node], /, *, advance_size: int | None = None) -> None:
```

Inside the method, use `advance_size if advance_size is not None else int_size` wherever `int_size` is used for the advance. This preserves the legacy `Call`-based codepath (if any code still hits it) while letting the new `VaArg` path pass the correct size.

Verify that `_type_size("double")` returns 8 on the 32-bit target. Check `cc/target.py` for `type_sizes` — if `"double"` is missing, add it with size 8.

- [ ] **Step 6: Run the tests**

```bash
./tests/test_cc_va_arg_sizeof.py
```

Both tests must pass.

- [ ] **Step 7: Run existing suites**

```bash
./tests/test_cc_casts.py && ./tests/test_cc_bitfields.py && ./tests/test_cc_local_structs.py && ./tests/test_programs.py
```

All green.

- [ ] **Step 8: Commit**

```bash
git add cc/ast_nodes.py cc/parser.py cc/ir.py cc/codegen/x86/builtins.py cc/codegen/x86/emission.py cc/target.py tests/test_cc_va_arg_sizeof.py
git commit -m "feat(cc): va_arg(ap, double) advances cursor by 8 bytes"
```

---

### Task 2: `SizeofExpr` — AST node + parser

**Files:**
- Modify: `cc/ast_nodes.py` (add `SizeofExpr` class)
- Modify: `cc/parser.py` (extend `parse_sizeof`)
- Modify: `cc/ir.py` (add `SizeofExpr` case in `_build_expr`)
- Modify: `tests/test_cc_va_arg_sizeof.py` (add sizeof tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cc_va_arg_sizeof.py`:

```python
def test_sizeof_deref_pointer() -> None:
    """sizeof *p where p is int * should be 4."""
    source = """
    int main(void) {
        int x;
        int *p = &x;
        return sizeof *p;
    }
    """
    asm = _compile("sizeof_deref", source)
    # Should contain "mov eax, 4" (the size of int).
    assert "mov eax, 4" in asm, f"expected 'mov eax, 4' in asm:\n{asm}"


def test_sizeof_deref_char_pointer() -> None:
    """sizeof *p where p is char * should be 1."""
    source = """
    int main(void) {
        char *p;
        return sizeof *p;
    }
    """
    asm = _compile("sizeof_deref_char", source)
    assert "mov eax, 1" in asm, f"expected 'mov eax, 1' in asm:\n{asm}"


def test_sizeof_parenthesised_expression() -> None:
    """sizeof(p[0]) where p is int * should be 4."""
    source = """
    int main(void) {
        int *p;
        return sizeof(p[0]);
    }
    """
    asm = _compile("sizeof_paren_expr", source)
    assert "mov eax, 4" in asm, f"expected 'mov eax, 4' in asm:\n{asm}"
```

Add calls in the `__main__` block (alphabetical). Run — confirm they fail because `parse_sizeof` can't handle `*p` or `p[0]`.

- [ ] **Step 2: Add the `SizeofExpr` AST node**

In `cc/ast_nodes.py`, add in alphabetical position (after `SizeofType`, before `SizeofVar` — verify: `SizeofE` < `SizeofT` is false, so `SizeofExpr` goes between `SizeofE...` — actually `"SizeofExpr"` < `"SizeofType"` since `'E' < 'T'`. So insert between the preceding node and `SizeofType`):

```python
@dataclass(kw_only=True, slots=True)
class SizeofExpr(Node):
    """``sizeof(expression)`` — compile-time size of the expression's inferred type."""

    expression: Node
```

- [ ] **Step 3: Extend `parse_sizeof`**

In `cc/parser.py`, import `SizeofExpr` (alphabetical). Rewrite `parse_sizeof` to handle three forms:

```python
def parse_sizeof(self) -> Node:
    """Parse a sizeof expression.

    Accepted forms:
    - ``sizeof(T)``   → :class:`SizeofType`
    - ``sizeof(var)`` → :class:`SizeofVar` (preserves array-size semantics)
    - ``sizeof(expr)`` → :class:`SizeofExpr`
    - ``sizeof expr`` (unparenthesised) → :class:`SizeofExpr`
    """
    token = self.eat("SIZEOF")
    if self.peek()[0] != "LPAREN":
        # Unparenthesised: sizeof <unary-expression>.
        expression = self.parse_primary()
        return SizeofExpr(expression=expression, line=token[2])
    self.eat("LPAREN")
    if self._is_type_start():
        type_string = self.parse_type()
        self.eat("RPAREN")
        return SizeofType(line=token[2], type_name=type_string)
    # Could be sizeof(var) or sizeof(expr).  Try bare IDENT first for
    # backwards-compat with the SizeofVar path (which handles arrays
    # specially — element-count * stride, not pointer size).
    if self.peek()[0] == "IDENT" and self.peek(offset=1)[0] == "RPAREN":
        name = self.eat("IDENT")[1]
        self.eat("RPAREN")
        return SizeofVar(line=token[2], name=name)
    # General expression.
    expression = self.parse_expression()
    self.eat("RPAREN")
    return SizeofExpr(expression=expression, line=token[2])
```

- [ ] **Step 4: Add IR builder case**

In `cc/ir.py`, import `SizeofExpr` (alphabetical). In `_build_expr`, add:

```python
case ast_nodes.SizeofExpr():
    temp = self._tmp()
    out.append(Block(node=ast_nodes.Assign(expr=expression, name=temp)))
    return temp
```

Match the same pattern as `SizeofType` / `SizeofVar` in the IR builder — check how they're handled and follow suit.

- [ ] **Step 5: Run the tests**

```bash
./tests/test_cc_va_arg_sizeof.py
```

The new sizeof tests will fail at codegen (no `_expression_type` helper yet). They should at least parse successfully — if they crash at parse time, the parser change needs debugging. If they crash at codegen with "unknown expression: SizeofExpr", that's expected for this task. Adjust the tests to also accept a clean codegen error (similar to the Task 2 pattern from the assignment-as-expression plan).

- [ ] **Step 6: Commit**

```bash
git add cc/ast_nodes.py cc/parser.py cc/ir.py tests/test_cc_va_arg_sizeof.py
git commit -m "feat(cc): parse sizeof(expression) and unparenthesised sizeof expr"
```

---

### Task 3: `SizeofExpr` — codegen (`_expression_type` + handler)

**Files:**
- Modify: `cc/codegen/x86/emission.py` (add `SizeofExpr` expression branch)
- Modify: `cc/codegen/x86/generator.py` (add `_expression_type` helper)
- Modify: `tests/test_cc_va_arg_sizeof.py` (add more sizeof tests, tighten existing)

- [ ] **Step 1: Add more tests**

Append to `tests/test_cc_va_arg_sizeof.py`:

```python
def test_sizeof_struct_deref() -> None:
    """sizeof *p where p is struct S * should be the struct size."""
    source = """
    struct S { int a; int b; int c; };
    int main(void) {
        struct S *p;
        return sizeof *p;
    }
    """
    asm = _compile("sizeof_struct_deref", source)
    assert "mov eax, 12" in asm, f"expected 'mov eax, 12' in asm:\n{asm}"


def test_sizeof_member_access() -> None:
    """sizeof(p->field) should be the field's type size."""
    source = """
    struct S { int a; char b; };
    int main(void) {
        struct S s;
        struct S *p = &s;
        return sizeof(p->b);
    }
    """
    asm = _compile("sizeof_member", source)
    assert "mov eax, 1" in asm, f"expected 'mov eax, 1' in asm:\n{asm}"


def test_sizeof_cast() -> None:
    """sizeof((int *)0) should be pointer size (4)."""
    source = """
    int main(void) {
        return sizeof((int *)0);
    }
    """
    asm = _compile("sizeof_cast", source)
    assert "mov eax, 4" in asm, f"expected 'mov eax, 4' in asm:\n{asm}"


def test_sizeof_binary_op() -> None:
    """sizeof(1 + 2) should be int size (4)."""
    source = """
    int main(void) {
        return sizeof(1 + 2);
    }
    """
    asm = _compile("sizeof_binop", source)
    assert "mov eax, 4" in asm, f"expected 'mov eax, 4' in asm:\n{asm}"


def test_sizeof_array_still_returns_full_size() -> None:
    """sizeof(arr) via SizeofVar must still return full array size, not pointer size."""
    source = """
    int main(void) {
        int a[10];
        return sizeof(a);
    }
    """
    asm = _compile("sizeof_array", source)
    assert "mov eax, 40" in asm, f"expected 'mov eax, 40' in asm:\n{asm}"


def test_sizeof_dot_member() -> None:
    """sizeof(s.field) via dot access."""
    source = """
    struct S { int a; char b; };
    int main(void) {
        struct S s;
        return sizeof(s.b);
    }
    """
    asm = _compile("sizeof_dot_member", source)
    assert "mov eax, 1" in asm, f"expected 'mov eax, 1' in asm:\n{asm}"
```

Add calls in the `__main__` block (alphabetical).

- [ ] **Step 2: Add `_expression_type` helper**

In `cc/codegen/x86/generator.py`, add a new method `_expression_type` (alphabetical among `_`-prefixed methods). This method infers the compile-time type of an AST expression node:

```python
def _expression_type(self, node: Node, /) -> str:
    """Infer the compile-time type of *node* for ``sizeof(expression)``.

    Walks the AST structurally — the expression is never evaluated.
    Returns a type string compatible with :meth:`_type_size`.
    """
    if isinstance(node, Var):
        vtype = self.variable_types.get(node.name)
        if vtype is None:
            message = f"sizeof: unknown variable '{node.name}'"
            raise CompileError(message, line=node.line)
        return vtype
    if isinstance(node, Index):
        # p[i] — pointee type of the array/pointer.
        array_type = self._expression_type(node.array)
        if array_type.endswith("*"):
            return array_type[:-1].rstrip()
        if array_type.endswith("]"):
            # Local array: strip the "[N]" suffix to get element type.
            bracket_pos = array_type.index("[")
            return array_type[:bracket_pos].rstrip()
        message = f"sizeof: cannot dereference non-pointer type '{array_type}'"
        raise CompileError(message, line=node.line)
    if isinstance(node, (MemberAccess, IndexMemberAccess)):
        # p->field or p[i].field — look up the field's declared type.
        if isinstance(node, MemberAccess):
            if node.object_name:
                base_type = self.variable_types.get(node.object_name, "")
            elif node.base_expr is not None:
                base_type = self._expression_type(node.base_expr)
            else:
                message = "sizeof: cannot determine struct type for member access"
                raise CompileError(message, line=node.line)
        else:
            base_type = self.variable_types.get(node.name, "")
        if node.arrow:
            if not base_type.endswith("*"):
                message = f"sizeof: arrow on non-pointer type '{base_type}'"
                raise CompileError(message, line=node.line)
            tag = base_type[7:-1].rstrip() if base_type.startswith("struct ") else ""
        else:
            tag = base_type[7:] if base_type.startswith("struct ") else ""
        layout = self.struct_layouts.get(tag)
        if layout is None:
            message = f"sizeof: unknown struct tag '{tag}'"
            raise CompileError(message, line=node.line)
        field_info = layout.get(node.member_name)
        if field_info is None:
            message = f"sizeof: unknown field '{node.member_name}' in struct '{tag}'"
            raise CompileError(message, line=node.line)
        return field_info.type_name
    if isinstance(node, Cast):
        return node.target_type
    if isinstance(node, (Int, Char)):
        return "int" if isinstance(node, Int) else "char"
    if isinstance(node, String):
        return "char *"
    if isinstance(node, AddressOf):
        var_type = self._expression_type(node.var)
        return f"{var_type} *"
    if isinstance(node, PointerDereference):
        return node.target_type
    if isinstance(node, BinaryOperation):
        return "int"
    if isinstance(node, (SizeofType, SizeofVar, SizeofExpr)):
        return "int"
    message = f"sizeof: cannot determine type of {type(node).__name__}"
    raise CompileError(message, line=node.line)
```

Import the needed AST node types at the top of `generator.py` if not already imported. Check which are already imported and add only the missing ones.

- [ ] **Step 3: Add `SizeofExpr` to `generate_expression`**

In `cc/codegen/x86/emission.py`, import `SizeofExpr` (alphabetical). Add a branch in `generate_expression` next to the existing `SizeofType` / `SizeofVar` branches:

```python
elif isinstance(expression, SizeofExpr):
    self.ax_clear()
    inferred_type = self._expression_type(expression.expression)
    self.emit(f"        mov {self.target.acc}, {self._type_size(inferred_type)}")
```

Also add `SizeofExpr` to the `_is_pure_expression` check (line ~814) next to the existing `SizeofType, SizeofVar` entry — sizeof expressions are always pure (no side effects).

- [ ] **Step 4: Run all sizeof tests**

```bash
./tests/test_cc_va_arg_sizeof.py
```

All tests must pass. If `test_sizeof_array_still_returns_full_size` fails with 4 instead of 40, the `parse_sizeof` bare-IDENT lookahead isn't working — debug the `IDENT RPAREN` peek logic.

- [ ] **Step 5: Run existing suites**

```bash
./tests/test_cc_casts.py && ./tests/test_cc_bitfields.py && ./tests/test_cc_local_structs.py && ./tests/test_cc_assign_expr.py && ./tests/test_programs.py
```

All green.

- [ ] **Step 6: Commit**

```bash
git add cc/codegen/x86/generator.py cc/codegen/x86/emission.py tests/test_cc_va_arg_sizeof.py
git commit -m "feat(cc): sizeof(expression) via codegen-time type inference"
```

---

### Task 4: CHANGELOG + full CI matrix + PR

**Files:**
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Add CHANGELOG entries under Unreleased**

```markdown
- cc.py now accepts `va_arg(ap, double)` and advances the va-list
  cursor by 8 bytes (correct i386 cdecl double-slot size).
  Previously `__builtin_va_arg` always advanced by 4 regardless of
  type.  Unblocks `user/libbboeos/stdio.c`.
- cc.py now accepts `sizeof(expression)` and `sizeof expr`
  (unparenthesised).  A new codegen-time `_expression_type` helper
  infers the compile-time type of the expression by walking the AST
  and delegates to `_type_size` for the constant.  Covers pointer
  dereference (`sizeof *p`), subscript (`sizeof p[0]`), member
  access (`sizeof p->f`), casts, binary ops, and literals.
  `sizeof(array_name)` retains its existing full-array-size
  semantics via the `SizeofVar` path.  Unblocks
  `user/libbboeos/dirent.c`.
```

- [ ] **Step 2: Reflow**

```bash
tools/wrap_md.py docs/CHANGELOG.md
```

- [ ] **Step 3: Commit**

```bash
git add docs/CHANGELOG.md
git commit -m "docs(cc): note va_arg double + sizeof expression support"
```

- [ ] **Step 4: Run the full local CI matrix**

At a minimum:
- `./tests/test_asm.py`
- `./tests/test_bboefs.py`
- `./tests/test_programs.py`
- `./tests/test_programs.py --filesystem ext2`
- `./tests/test_cc_compatibility.py`
- `./tests/test_cc_casts.py`
- `./tests/test_cc_local_structs.py`
- `./tests/test_cc_bitfields.py`
- `./tests/test_cc_bits.py`
- `./tests/test_cc_assign_expr.py`
- `./tests/test_cc_va_arg_sizeof.py` (new)
- `python3 -m pytest tests/unit/`
- `./tests/test_archive.py`
- `./tests/test_kernel_archive.py`

All green.

- [ ] **Step 5: Push the branch and open a PR**

PR description should link to the spec on `design-specs` and mention the unblocked `stdio.c` and `dirent.c` files.

---

## Self-review checklist

- **Spec coverage:**
  - `VaArg` AST node → Task 1 step 2.
  - Parser retains type string → Task 1 step 3.
  - Codegen advances by `_type_size(type_name)` → Task 1 step 5.
  - `double` → 8, `int` → 4 → Task 1 tests.
  - IR fallback for `VaArg` → Task 1 step 4.
  - `SizeofExpr` AST node → Task 2 step 2.
  - Unparenthesised `sizeof expr` → Task 2 step 3.
  - Parenthesised `sizeof(expr)` with expression fallback → Task 2 step 3.
  - `SizeofVar` preserved for bare-IDENT (array semantics) → Task 2 step 3 + Task 3 test `test_sizeof_array_still_returns_full_size`.
  - `_expression_type` helper with type inference table → Task 3 step 2.
  - `SizeofExpr` codegen branch → Task 3 step 3.
  - Tests for `*p`, `p[0]`, `p->f`, `s.f`, `(int *)0`, `1+2`, array → Tasks 2–3.
  - Out of scope items: no FP register load, no VLA, no `_Alignof`.

- **Placeholder scan:** None found.

- **Type consistency:** `_expression_type`, `SizeofExpr`, `VaArg` used consistently across tasks.
