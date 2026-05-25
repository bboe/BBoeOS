# cc.py: array of function pointers — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach cc.py to declare, store into, and call through arrays of function pointers, unblocking `user/libbboeos/stdlib.c`.

**Architecture:** Extend the parser's file-scope and local-scope function-pointer paths to recognise `(*name[N])` (array syntax inside the declarator). Add a new `IndexedCall` AST node for `arr[i](args)` — calling a function pointer stored in an array element. Codegen computes the element address, loads the pointer, and emits `call <register>`.

**Tech Stack:** Python 3 (`cc/`), NASM, QEMU for the program suite.

**Spec:** [`2026-05-25-cc-array-of-function-pointers-design.md`](https://github.com/bboe/BBoeOS/blob/design-specs/2026-05-25-cc-array-of-function-pointers-design.md) on the `design-specs` branch.

---

## Notes for the implementing engineer

- **Branch:** create `bboe/cc-fptr-array` off `main`.
- **Test file:** `tests/test_cc_fptr_array.py` (new). Same subprocess-driven pattern as `tests/test_cc_casts.py`.
- **Key codebase context:**
  - File-scope function-pointer scalar: `cc/parser.py` around line 2057 — checks `LPAREN STAR`, eats `(*name)(params)`, emits `VarDecl(type_name="function_pointer")`.
  - Local function-pointer scalar: `cc/parser.py` around line 674 — same pattern inside `parse_variable_declaration` / `_parse_one_declarator`.
  - Typedef aliases: `cc/parser.py` around line 1971 — `typedef void (*handler)(void)` maps alias to `"function_pointer"`.
  - `_type_size("function_pointer")` already returns `int_size` (in `cc/codegen/x86/generator.py:1251`).
  - File-scope ArrayDecl codegen: `cc/codegen/x86/emission.py` `generate_body` handles `ArrayDecl` with `init` (emits `dd` labels) and without (`resb` BSS).
  - Index expression: `cc/parser.py` `parse_primary` line 1421 — parses `name[index]` into `Index(array=Var(name), index=...)`.
  - Statement dispatch for `IDENT LBRACKET`: `cc/parser.py` `parse_statement` line 1861 — routes to `parse_index_assignment`.
  - Indirect call through scalar function pointer: `cc/codegen/x86/emission.py` `generate_call` line 901 — checks `variable_types[name] == "function_pointer"`, emits `call <register>`.

---

### Task 1: Array-of-function-pointer declaration + store

**Files:**
- Modify: `cc/parser.py` (extend file-scope and local-scope function-pointer paths)
- Modify: `cc/codegen/x86/emission.py` (potentially — verify ArrayDecl codegen handles `"function_pointer"` element type)
- Create: `tests/test_cc_fptr_array.py`

- [ ] **Step 1: Write the test file with declaration + store tests**

Create `tests/test_cc_fptr_array.py`:

```python
#!/usr/bin/env python3
"""cc.py array-of-function-pointer coverage."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
LIBBBOEOS_INCLUDE = REPO_ROOT / "user" / "libbboeos" / "include"
_PREAMBLE = "#include <stdint.h>\n"


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


def test_file_scope_uninitialized() -> None:
    """Uninitialized file-scope array of function pointers."""
    _compile(
        "fscope_uninit",
        "static void (*handlers[8])(void);\nint main(void) { return 0; }",
    )


def test_file_scope_initialized() -> None:
    """Initialized file-scope array of function pointers."""
    _compile(
        "fscope_init",
        "void f1(void) {} void f2(void) {}\n"
        "static void (*handlers[2])(void) = { f1, f2 };\n"
        "int main(void) { return 0; }",
    )


def test_local_scope() -> None:
    """Local array of function pointers."""
    _compile(
        "local",
        "void f1(void) {}\n"
        "int main(void) { void (*arr[4])(void); arr[0] = f1; return 0; }",
    )


def test_store_indexed() -> None:
    """Store a function pointer into an indexed element."""
    _compile(
        "store",
        "void handler(void) {}\n"
        "static void (*handlers[4])(void);\n"
        "int main(void) { handlers[0] = handler; return 0; }",
    )


def test_typedef_array() -> None:
    """Typedef function pointer used as array element type."""
    _compile(
        "typedef_arr",
        "typedef void (*handler_t)(void);\n"
        "static handler_t handlers[4];\n"
        "void f(void) {}\n"
        "int main(void) { handlers[0] = f; return 0; }",
    )


if __name__ == "__main__":
    test_file_scope_initialized()
    test_file_scope_uninitialized()
    test_local_scope()
    test_store_indexed()
    test_typedef_array()
    print("OK")
```

Run `chmod +x tests/test_cc_fptr_array.py` and `./tests/test_cc_fptr_array.py`. Confirm the tests fail (cc.py rejects the `(*name[N])` declarator).

- [ ] **Step 2: Extend the file-scope parser**

In `cc/parser.py`, find the file-scope function-pointer branch (around line 2057, inside `parse_top_level_declaration`):

```python
if self.peek()[0] == "LPAREN" and self.peek(offset=1)[0] == "STAR":
    self.eat("LPAREN")
    self.eat("STAR")
    name = self.eat("IDENT")[1]
    self.eat("RPAREN")
    ...
```

After eating `IDENT`, check for `LBRACKET` before eating `RPAREN`. If present, parse the array size and produce `ArrayDecl` instead of `VarDecl`:

```python
if self.peek()[0] == "LPAREN" and self.peek(offset=1)[0] == "STAR":
    self.eat("LPAREN")
    self.eat("STAR")
    name = self.eat("IDENT")[1]
    # Array of function pointers: (*name[N])(params)
    array_size: Node | None = None
    if self.peek()[0] == "LBRACKET":
        self.eat("LBRACKET")
        if self.peek()[0] != "RBRACKET":
            array_size = self.parse_expression()
        self.eat("RBRACKET")
    self.eat("RPAREN")
    self.eat("LPAREN")
    function_pointer_params_list, _ = self.parse_parameters()
    self.eat("RPAREN")
    # ... parse optional initializer ...
    if array_size is not None:
        # Array of function pointers.
        init = None
        if self.peek()[0] == "ASSIGN":
            self.eat("ASSIGN")
            init = self.parse_array_init()
        self.eat("SEMI")
        return [ArrayDecl(init=init, is_extern=is_extern, line=line, name=name, size=array_size, type_name="function_pointer")]
    # ... existing scalar VarDecl path ...
```

Preserve the existing scalar function-pointer path for the `array_size is None` case.

- [ ] **Step 3: Extend the local-scope parser**

In `cc/parser.py`, find the local function-pointer branch (around line 674, inside `_parse_one_declarator`):

```python
if self.peek()[0] == "LPAREN":
    self.eat("LPAREN")
    self.eat("STAR")
    name = self.eat("IDENT")[1]
    self.eat("RPAREN")
    ...
```

Apply the same pattern: after eating `IDENT`, check for `LBRACKET`. If present, parse size, eat `RBRACKET`, then eat `RPAREN`, parse params. Set `local_type_string = "function_pointer"`. Then the existing `is_array` / `ArrayDecl` branch at line 706 won't be reached (the `[N]` was already consumed inside the `(* ... )` syntax), so emit the `ArrayDecl` directly from within this branch:

```python
if self.peek()[0] == "LPAREN":
    self.eat("LPAREN")
    self.eat("STAR")
    name = self.eat("IDENT")[1]
    array_size: Node | None = None
    if self.peek()[0] == "LBRACKET":
        self.eat("LBRACKET")
        if self.peek()[0] != "RBRACKET":
            array_size = self.parse_expression()
        self.eat("RBRACKET")
    self.eat("RPAREN")
    self.eat("LPAREN")
    function_pointer_params_list, _ = self.parse_parameters()
    self.eat("RPAREN")
    local_type_string = "function_pointer"
    if array_size is not None:
        init = None
        if self.peek()[0] == "ASSIGN":
            self.eat("ASSIGN")
            init = self.parse_array_init()
        return ArrayDecl(init=init, line=line, name=name, size=array_size, type_name="function_pointer")
    # ... existing scalar flow continues (function_pointer_params_list, etc.) ...
```

- [ ] **Step 4: Handle the typedef path**

When `parse_type()` resolves a typedef alias to `"function_pointer"`, the returned type string is `"function_pointer"`. This string then flows into `_parse_one_declarator` or `parse_top_level_declaration`, where the `[N]` array suffix is detected. The existing flow at line 706 (local) and line 2222 (file-scope) already produce `ArrayDecl(type_name=current_type, ...)`. So `ArrayDecl(type_name="function_pointer", name=name, size=N)` should be emitted automatically by the typedef path with NO extra code change. **Verify this by running the `test_typedef_array` test.**

If the typedef path doesn't work (e.g. the file-scope path rejects `"function_pointer"` somewhere), add the necessary fix.

- [ ] **Step 5: Verify ArrayDecl codegen handles `"function_pointer"` elements**

The file-scope ArrayDecl codegen in `emission.py` needs `_type_size("function_pointer")` to succeed (it returns `int_size`, already implemented). For initialized arrays (`= { f1, f2 }`), each element in the `ArrayInit.elements` list is a `Var(name="f1")` etc. The codegen emits these as `dd _f1, _f2`. Verify by reading the asm output from `test_file_scope_initialized`.

For local arrays, the stack reservation uses `size * _type_size(type_name)`. Again, `"function_pointer"` maps to `int_size`. Verify via `test_local_scope`.

For `IndexAssign` (store), the stride computation in `generate_index_assign` calls `_type_size` on the variable's element type. Verify the variable-type tracking for function-pointer arrays is correct: the array's type in `variable_types` should map to something that yields `int_size` per element. Read the codegen's variable-type registration for `ArrayDecl` and confirm it sets `variable_types[name]` to `"function_pointer *"` or similar. If not, add the fix.

- [ ] **Step 6: Run the tests**

```bash
./tests/test_cc_fptr_array.py
```

All five tests must pass.

- [ ] **Step 7: Regression suites**

```bash
./tests/test_cc_casts.py && ./tests/test_cc_local_structs.py && ./tests/test_cc_bitfields.py && ./tests/test_cc_assign_expr.py && ./tests/test_cc_va_arg_sizeof.py && ./tests/test_programs.py
```

All green.

- [ ] **Step 8: Commit**

```bash
git add cc/parser.py cc/codegen/x86/emission.py tests/test_cc_fptr_array.py
git commit -m "feat(cc): array-of-function-pointer declarations at file and local scope"
```

(Only include `emission.py` if you modified it.)

---

### Task 2: `IndexedCall` — AST node + parser + codegen

**Files:**
- Modify: `cc/ast_nodes.py` (add `IndexedCall` class)
- Modify: `cc/parser.py` (hook in `parse_primary` and `parse_statement`)
- Modify: `cc/ir.py` (add `IndexedCall` cases in `_build_expr` and `_build_stmt`)
- Modify: `cc/codegen/x86/emission.py` (add `IndexedCall` codegen)
- Modify: `tests/test_cc_fptr_array.py` (add call-through tests)

- [ ] **Step 1: Write the failing call-through tests**

Append to `tests/test_cc_fptr_array.py`:

```python
def test_call_through_index() -> None:
    """arr[i]() calls the function pointer at element i."""
    source = """
    int result;
    void set_result(void) { result = 42; }
    static void (*handlers[4])(void);
    int main(void) {
        handlers[0] = set_result;
        handlers[0]();
        return result;
    }
    """
    _compile("call_through", source)


def test_call_through_index_with_args() -> None:
    """arr[i](arg) passes arguments through the indirect call."""
    source = """
    int result;
    void add_to_result(int x) { result = result + x; }
    static void (*handlers[4])(int);
    int main(void) {
        handlers[0] = add_to_result;
        handlers[0](7);
        return result;
    }
    """
    _compile("call_args", source)


def test_atexit_pattern() -> None:
    """End-to-end atexit pattern: declare, store, call-through in a loop."""
    source = """
    int call_count;
    void inc(void) { call_count = call_count + 1; }
    void dec(void) { call_count = call_count - 1; }
    static void (*fns[8])(void);
    int count;
    int main(void) {
        fns[0] = inc;
        fns[1] = inc;
        fns[2] = dec;
        count = 3;
        while (count > 0) {
            count = count - 1;
            fns[count]();
        }
        return call_count;
    }
    """
    _compile("atexit_pattern", source)
```

Add calls in `__main__` (alphabetical). Run — confirm they fail because `arr[0]()` is not recognised.

- [ ] **Step 2: Add the `IndexedCall` AST node**

In `cc/ast_nodes.py`, add alphabetically (between `IndexAssign` and `IndexMemberAccess`):

```python
@dataclass(kw_only=True, slots=True)
class IndexedCall(Node):
    """Call through a function-pointer array element: ``array[index](args)``."""

    args: list[Node]
    array: Var
    index: Node
```

- [ ] **Step 3: Parser hook in `parse_primary`**

In `cc/parser.py`, import `IndexedCall` (alphabetical). In `parse_primary`, after parsing `name[index]` and the member-access / double-index checks, but BEFORE the final `return Index(...)` at line 1462, add:

```python
# Call through indexed function pointer: name[index](args).
if self.peek()[0] == "LPAREN":
    self.eat("LPAREN")
    args = self.parse_arguments()
    return IndexedCall(args=args, array=Var(line=line, name=token[1]), index=index, line=line)
return Index(array=Var(line=line, name=token[1]), index=index, line=line)
```

- [ ] **Step 4: Parser hook in `parse_statement`**

In `parse_statement`, the `IDENT LBRACKET` branch at line 1861 currently routes unconditionally to `parse_index_assignment`. Change it to detect the `IndexedCall` case:

```python
if next_kind == "LBRACKET":
    # Could be index assignment (name[i] = ...) or indexed call (name[i](...)).
    # Speculatively parse the index, then check for LPAREN.
    saved_position = self.position
    self.eat("IDENT")
    self.eat("LBRACKET")
    index_expression = self.parse_expression()
    self.eat("RBRACKET")
    if self.peek()[0] == "LPAREN":
        self.eat("LPAREN")
        args = self.parse_arguments()
        self.eat("SEMI")
        return IndexedCall(args=args, array=Var(line=token[2], name=token[1]), index=index_expression, line=token[2])
    # Not a call — restore and fall through to index assignment.
    self.position = saved_position
    return self.parse_index_assignment()
```

- [ ] **Step 5: IR builder cases**

In `cc/ir.py`, import `IndexedCall` (alphabetical).

In `_build_stmt`, add a case for `IndexedCall` that delegates to AST codegen:

```python
case ast_nodes.IndexedCall():
    out.append(Block(node=stmt))
```

In `_build_expr`, add a case that wraps in a temp assignment (same pattern as `Call`):

```python
case ast_nodes.IndexedCall():
    temp = self._tmp()
    out.append(Block(node=ast_nodes.Assign(expr=expression, name=temp)))
    return temp
```

- [ ] **Step 6: Codegen for `IndexedCall`**

In `cc/codegen/x86/emission.py`, import `IndexedCall` (alphabetical).

Add a `generate_indexed_call` method (alphabetical among `generate_*` methods):

```python
def generate_indexed_call(self, statement: IndexedCall, /, *, discard_return: bool = False) -> None:
    """Generate an indirect call through a function-pointer array element."""
    array_name = statement.array.name
    # Save pinned registers across the call (standard clobber convention).
    clobbers: frozenset[str] = frozenset(self.target.register_pool)
    saved = self._pinned_registers_to_save(clobbers)
    use_pusha = discard_return and len(saved) >= 3
    if use_pusha:
        self.emit("        pusha")
    else:
        for register in saved:
            self.emit(f"        push {register}")
    # Push arguments right-to-left (cdecl).
    for arg in reversed(statement.args):
        self._emit_push_arg(arg)
    # Compute element address: base + index * int_size, load pointer.
    self.generate_expression(statement.index)
    int_size = self.target.int_size
    if int_size == 4:
        self.emit(f"        shl {self.target.acc}, 2")
    elif int_size == 2:
        self.emit(f"        shl {self.target.acc}, 1")
    bx = self.target.bx_register
    self.emit(f"        mov {bx}, {self.target.acc}")
    # Add the array base address.
    if array_name in self.locals:
        slot = self.locals[array_name]
        self.emit(f"        lea {self.target.acc}, [{self.target.base_register}{slot:+d}]")
    else:
        self.emit(f"        lea {self.target.acc}, [_{array_name}]")
    self.emit(f"        add {bx}, {self.target.acc}")
    self.emit(f"        call [{bx}]")
    # Caller pops args.
    if statement.args:
        self.emit(f"        add {self.target.stack_register}, {len(statement.args) * int_size}")
    # Restore saved registers.
    if use_pusha:
        self.emit("        popa")
    else:
        for register in reversed(saved):
            self.emit(f"        pop {register}")
    self.ax_clear()
```

Wire the dispatch: in `generate_statement`, add an `IndexedCall` branch:

```python
elif isinstance(statement, IndexedCall):
    self.generate_indexed_call(statement, discard_return=True)
```

In `generate_expression`, add an `IndexedCall` branch:

```python
elif isinstance(expression, IndexedCall):
    self.generate_indexed_call(expression)
```

**Important codegen notes:**

- The array base address for global arrays is `_<name>` (a label). For locals, it's `[bp+offset]` where offset is the local's frame slot. Read how `generate_index_assign` computes the element address for existing array types and mirror the pattern.
- The `_emit_push_arg` helper pushes a single argument. Verify it exists on the mixin; if not, inline the push logic (evaluate expression → push eax).
- `call [bx]` calls through the pointer stored at `[bx]`. This is the same pattern as existing indirect calls through scalar function pointers.

The codegen above is a starting point — adapt to the codebase's actual register-management conventions. Read `generate_call`'s indirect-call path (line 901+) for the canonical register-save / push-args / call / pop-args / restore pattern.

- [ ] **Step 7: Run all tests**

```bash
./tests/test_cc_fptr_array.py
```

All eight tests must pass (five from Task 1 + three new).

- [ ] **Step 8: Regression suites**

```bash
./tests/test_cc_casts.py && ./tests/test_cc_local_structs.py && ./tests/test_cc_bitfields.py && ./tests/test_cc_assign_expr.py && ./tests/test_cc_va_arg_sizeof.py && ./tests/test_programs.py
```

All green.

- [ ] **Step 9: Commit**

```bash
git add cc/ast_nodes.py cc/parser.py cc/ir.py cc/codegen/x86/emission.py tests/test_cc_fptr_array.py
git commit -m "feat(cc): IndexedCall — call through function-pointer array element"
```

---

### Task 3: CHANGELOG + full CI matrix + PR

**Files:**
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Add CHANGELOG entries**

```markdown
- cc.py now supports arrays of function pointers at file scope and
  local scope, using both the direct declarator syntax
  (`void (*arr[N])(void)`) and the typedef path
  (`typedef void (*handler)(void); handler arr[N]`).  A new
  `IndexedCall` AST node handles calling through an array element
  (`arr[i](args)`) via an indirect `call [reg]`.  Unblocks
  `user/libbboeos/stdlib.c`.
```

- [ ] **Step 2: Reflow and commit**

```bash
tools/wrap_md.py docs/CHANGELOG.md
git add docs/CHANGELOG.md
git commit -m "docs(cc): note array-of-function-pointer support"
```

- [ ] **Step 3: Run the full local CI matrix**

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
- `./tests/test_cc_va_arg_sizeof.py`
- `./tests/test_cc_fptr_array.py` (new)
- `python3 -m pytest tests/unit/`
- `./tests/test_archive.py`
- `./tests/test_kernel_archive.py`

All green.

- [ ] **Step 4: Push and open a PR**

PR description should link to the spec on `design-specs` and mention the unblocked `stdlib.c`.

---

## Self-review checklist

- **Spec coverage:**
  - Direct declarator syntax → Task 1 steps 2–3.
  - Typedef path → Task 1 step 4.
  - File-scope + local-scope → Task 1 steps 2–3.
  - Initialized + uninitialized arrays → Task 1 tests.
  - Store to indexed element → Task 1 `test_store_indexed`.
  - `IndexedCall` AST node → Task 2 step 2.
  - Parser for `arr[i](args)` in expression and statement → Task 2 steps 3–4.
  - Codegen: compute address, load, `call [reg]` → Task 2 step 6.
  - End-to-end atexit pattern → Task 2 `test_atexit_pattern`.
  - Out of scope items: multi-dimensional arrays, non-void/non-int return types, arbitrary pointer expressions.

- **Placeholder scan:** None.

- **Type consistency:** `IndexedCall(args, array, index)` fields consistent across Task 2 steps 2–6. `generate_indexed_call` name consistent in dispatch and definition.
