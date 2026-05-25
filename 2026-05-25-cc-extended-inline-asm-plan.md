# cc.py: GCC extended inline asm — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach cc.py to parse and emit GCC extended inline asm statements with output/input/clobber operand sections, covering the full constraint set used by `signal.c`, `syscall.c`, and `math.c`.

**Architecture:** A new `ExtendedAsm` AST node (with `AsmOperand` sub-nodes) replaces the raw-string `InlineAsm` for statement-level extended asm. The parser recognises `asm volatile("..." : outputs : inputs : clobbers);` and `__asm__ volatile(...)`. Codegen emits pre-template register loads (for `+` and `"0"` tied operands), substitutes `%[name]`/`%N`/`%b`/`%%` in the template, emits the template, then stores outputs and invalidates clobber tracking.

**Tech Stack:** Python 3 (`cc/`), NASM, QEMU for the program suite.

**Spec:** [`2026-05-25-cc-extended-inline-asm-design.md`](https://github.com/bboe/BBoeOS/blob/design-specs/2026-05-25-cc-extended-inline-asm-design.md) on the `design-specs` branch.

---

## Notes for the implementing engineer

- **Branch:** create `bboe/cc-extended-asm` off `main`.
- **Test file:** `tests/test_cc_extended_asm.py` (new).
- **Key codebase context:**
  - File-scope asm: `cc/parser.py` line 2022 — detects `asm(` at file scope, emits `InlineAsm(content=...)`.
  - Statement-level simple asm: parsed as `Call(name="asm", args=[String(...)])` → codegen dispatches to `builtin_asm` in `cc/codegen/x86/builtins.py:193`.
  - `InlineAsm` AST node: `cc/ast_nodes.py` line 410 — just a `content: str` field.
  - Statement dispatch: `cc/codegen/x86/emission.py` line 3354 — `InlineAsm` emits decoded content verbatim.
  - Token `COLON` already exists in `cc/tokens.py:133`.
  - Token `VOLATILE` already exists in `cc/tokens.py:154`.
  - `__asm__` is tokenized as a plain `IDENT` (no special token).
  - Helper `_local_address(name)` in `cc/codegen/x86/generator.py:2332` returns the memory operand for a local/global.
  - Helper `_global_label(name)` returns `_g_<name>` for globals.

---

### Task 1: AST nodes + parser for extended asm

**Files:**
- Modify: `cc/ast_nodes.py` (add `AsmOperand` and `ExtendedAsm`)
- Modify: `cc/parser.py` (add extended asm parsing in `parse_statement`)
- Create: `tests/test_cc_extended_asm.py`

- [ ] **Step 1: Write the test file with parse-only tests**

Create `tests/test_cc_extended_asm.py`:

```python
#!/usr/bin/env python3
"""cc.py GCC extended inline asm coverage."""

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


def test_output_only() -> None:
    """asm volatile with a single =a output operand."""
    source = """
    int main(void) {
        int result;
        __asm__ volatile("mov $42, %%eax" : "=a"(result));
        return result;
    }
    """
    _compile("output_only", source)


def test_input_and_output() -> None:
    """asm volatile with named input and =a output."""
    source = """
    int main(void) {
        int x = 10;
        int result;
        __asm__ volatile("mov %[val], %%eax" : "=a"(result) : [val] "g"(x));
        return result;
    }
    """
    _compile("input_output", source)


def test_clobber_list() -> None:
    """asm volatile with clobber list."""
    source = """
    int main(void) {
        int result;
        __asm__ volatile("mov $7, %%eax" : "=a"(result) : : "ebx", "ecx");
        return result;
    }
    """
    _compile("clobber", source)


def test_read_modify_write() -> None:
    """asm volatile with +a read-modify-write operand."""
    source = """
    int main(void) {
        int x = 10;
        __asm__ volatile("add $5, %%eax" : "+a"(x));
        return x;
    }
    """
    _compile("rmw", source)


if __name__ == "__main__":
    test_clobber_list()
    test_input_and_output()
    test_output_only()
    test_read_modify_write()
    print("OK")
```

Run `chmod +x tests/test_cc_extended_asm.py`. Run it — confirm all tests fail (cc.py doesn't recognise extended asm).

- [ ] **Step 2: Add `AsmOperand` and `ExtendedAsm` AST nodes**

In `cc/ast_nodes.py`, add in alphabetical positions:

`AsmOperand` goes between `Assign` (or `AssignExpr`) and `BinaryOperation` — check exact sort position of `"AsmOperand"`:

```python
@dataclass(kw_only=True, slots=True)
class AsmOperand(Node):
    """A single output or input operand in an extended asm statement."""

    constraint: str
    expression: Node
    name: str | None = field(default=None)
```

`ExtendedAsm` goes between `EnumDecl` and `Function` (verify `"ExtendedAsm"` sorts between its neighbors):

```python
@dataclass(kw_only=True, slots=True)
class ExtendedAsm(Node):
    """Statement-level GCC extended inline asm with operand constraints."""

    clobbers: list[str]
    inputs: list[AsmOperand]
    is_volatile: bool
    outputs: list[AsmOperand]
    template: str
```

- [ ] **Step 3: Add the parser**

In `cc/parser.py`, import `AsmOperand` and `ExtendedAsm` (alphabetical).

Add a new method `_parse_extended_asm` (alphabetical among `_parse_*` methods — between `_parse_enum_declaration` and `_parse_index_assignment_no_semi`):

```python
def _parse_extended_asm(self) -> ExtendedAsm:
    """Parse an extended asm statement after the ``asm`` / ``__asm__`` keyword.

    Grammar::

        ("asm" | "__asm__") ["volatile"] "("
            template
            [ ":" output-operands ]
            [ ":" input-operands ]
            [ ":" clobber-list ]
        ")" ";"

    ``template`` is one or more adjacent string literals (concatenated).
    Each operand is ``[ "[" IDENT "]" ] STRING "(" expression ")"``.
    Clobbers are comma-separated string literals.
    """
    line = self.peek()[2]
    self.eat("IDENT")  # "asm" or "__asm__"
    is_volatile = False
    if self.peek()[0] == "VOLATILE":
        self.eat("VOLATILE")
        is_volatile = True
    self.eat("LPAREN")
    # Template: one or more adjacent string literals.
    template_token = self.eat("STRING")
    template = template_token[1][1:-1]
    while self.peek()[0] == "STRING":
        template += self.eat("STRING")[1][1:-1]
    outputs: list[AsmOperand] = []
    inputs: list[AsmOperand] = []
    clobbers: list[str] = []
    if self.peek()[0] == "COLON":
        self.eat("COLON")
        outputs = self._parse_asm_operands()
        if self.peek()[0] == "COLON":
            self.eat("COLON")
            inputs = self._parse_asm_operands()
            if self.peek()[0] == "COLON":
                self.eat("COLON")
                clobbers = self._parse_asm_clobbers()
    self.eat("RPAREN")
    self.eat("SEMI")
    return ExtendedAsm(
        clobbers=clobbers,
        inputs=inputs,
        is_volatile=is_volatile,
        line=line,
        outputs=outputs,
        template=template,
    )
```

Add two helpers (alphabetical):

```python
def _parse_asm_clobbers(self) -> list[str]:
    """Parse a comma-separated list of clobber string literals."""
    clobbers: list[str] = []
    while self.peek()[0] == "STRING":
        clobber_token = self.eat("STRING")
        clobbers.append(clobber_token[1][1:-1])
        if self.peek()[0] == "COMMA":
            self.eat("COMMA")
        else:
            break
    return clobbers

def _parse_asm_operands(self) -> list[AsmOperand]:
    """Parse a comma-separated list of asm output or input operands.

    Each operand: ``[ "[" IDENT "]" ] STRING "(" expression ")"``
    """
    operands: list[AsmOperand] = []
    while self.peek()[0] in ("LBRACKET", "STRING"):
        name: str | None = None
        if self.peek()[0] == "LBRACKET":
            self.eat("LBRACKET")
            name = self.eat("IDENT")[1]
            self.eat("RBRACKET")
        constraint_token = self.eat("STRING")
        constraint = constraint_token[1][1:-1]
        self.eat("LPAREN")
        expression = self.parse_expression()
        self.eat("RPAREN")
        operands.append(AsmOperand(
            constraint=constraint,
            expression=expression,
            line=constraint_token[2],
            name=name,
        ))
        if self.peek()[0] == "COMMA":
            self.eat("COMMA")
        else:
            break
    return operands
```

Wire the parser: in `parse_statement`, add a check BEFORE the existing `IDENT` dispatch block (around line 1841). The extended asm check must come early because `asm` and `__asm__` are IDENT tokens:

```python
if token[0] == "IDENT" and token[1] in ("asm", "__asm__"):
    # Could be statement-level asm("string") (simple) or
    # extended asm volatile("..." : outputs : inputs : clobbers).
    # Peek past optional "volatile" to find the template string.
    # If a COLON follows the template string (or closing paren for
    # multi-string), it's extended asm.
    offset = 1
    if self.peek(offset=offset)[0] == "VOLATILE":
        offset += 1
    if self.peek(offset=offset)[0] == "LPAREN":
        # Scan past the template string(s) to see if a COLON follows.
        # Simple asm: asm("string");  — RPAREN after template.
        # Extended asm: asm("string" : ...); — COLON after template.
        scan = offset + 1
        while self.peek(offset=scan)[0] == "STRING":
            scan += 1
        if self.peek(offset=scan)[0] == "COLON" or self.peek(offset=offset)[0] == "VOLATILE":
            return self._parse_extended_asm()
```

If not extended asm, fall through to the existing dispatch (which handles simple `asm("...")` via the Call→builtin_asm path).

Also add the same check in `parse_top_level_declaration` for file-scope extended asm — but the spec says asm statements only (not file-scope). However, `__asm__` at file scope should at least not crash. The existing file-scope `asm(` check at line 2022 only catches `asm`, not `__asm__`. Add `__asm__` to that check:

```python
if self.peek()[0] == "IDENT" and self.peek()[1] in ("asm", "__asm__") and self.peek(offset=1)[0] == "LPAREN":
```

- [ ] **Step 4: Add IR builder fallback**

In `cc/ir.py`, the `_build_stmt` catch-all `case _: out.append(Block(node=stmt))` at the end of the match should catch `ExtendedAsm` automatically. Verify by checking that `_build_stmt` has a wildcard/default case. If not, add an explicit `case ast_nodes.ExtendedAsm(): out.append(Block(node=stmt))`.

- [ ] **Step 5: Add a minimal codegen stub**

In `cc/codegen/x86/emission.py`, import `ExtendedAsm` and `AsmOperand` (alphabetical). In `generate_statement`, add a branch before the final `else` error:

```python
elif isinstance(statement, ExtendedAsm):
    self.generate_extended_asm(statement)
```

Add a stub `generate_extended_asm` method (alphabetical among `generate_*`):

```python
def generate_extended_asm(self, statement: ExtendedAsm, /) -> None:
    """Generate assembly for a GCC extended inline asm statement."""
    # Stub: emit the template verbatim with %% → % substitution only.
    # Full operand substitution is Task 2.
    for line in decode_string_escapes(statement.template).replace("%%", "%").splitlines():
        self.emit(line)
    self.ax_clear()
```

This stub is enough for the parse-only tests to compile (the tests check that cc.py doesn't crash, not that the output is semantically correct).

- [ ] **Step 6: Run the tests**

```bash
./tests/test_cc_extended_asm.py
```

All four tests should pass (parse + stub codegen).

- [ ] **Step 7: Regression suites**

```bash
./tests/test_cc_casts.py && ./tests/test_cc_local_structs.py && ./tests/test_cc_bitfields.py && ./tests/test_cc_assign_expr.py && ./tests/test_cc_va_arg_sizeof.py && ./tests/test_cc_fptr_array.py && ./tests/test_programs.py
```

All green.

- [ ] **Step 8: Commit**

```bash
git add cc/ast_nodes.py cc/parser.py cc/ir.py cc/codegen/x86/emission.py tests/test_cc_extended_asm.py
git commit -m "feat(cc): parse GCC extended inline asm with operand constraints"
```

---

### Task 2: Operand substitution + integer constraint codegen

**Files:**
- Modify: `cc/codegen/x86/emission.py` (replace the stub `generate_extended_asm` with full implementation)
- Modify: `tests/test_cc_extended_asm.py` (add semantic verification tests)

- [ ] **Step 1: Add semantic tests**

Append to `tests/test_cc_extended_asm.py`:

```python
def test_earlyclobber_byte_output() -> None:
    """=&q early-clobber byte-register output with %b substitution."""
    source = """
    int main(void) {
        int eax_out;
        unsigned char cf;
        __asm__ volatile(
            "mov $1, %%eax\\n\\t"
            "stc\\n\\t"
            "setc %b[cf]\\n\\t"
            : "=a"(eax_out), [cf] "=&q"(cf)
            :
            :);
        return cf;
    }
    """
    asm = _compile("earlyclobber", source)
    assert "setc" in asm


def test_named_input_substitution() -> None:
    """Named input operand substitutes as memory or register reference."""
    source = """
    int main(void) {
        int x = 42;
        int result;
        __asm__ volatile(
            "mov %[val], %%eax\\n\\t"
            : "=a"(result)
            : [val] "g"(x)
            :);
        return result;
    }
    """
    asm = _compile("named_input", source)
    main_body = asm.split("_main:", 1)[1].split("\n_", 1)[0]
    assert "mov" in main_body


def test_positional_operand_substitution() -> None:
    """Positional %0 / %1 substitution."""
    source = """
    int main(void) {
        int x = 10;
        int result;
        __asm__ volatile("mov %1, %0" : "=a"(result) : "g"(x));
        return result;
    }
    """
    asm = _compile("positional", source)
    main_body = asm.split("_main:", 1)[1].split("\n_", 1)[0]
    assert "mov" in main_body


def test_read_modify_write_codegen() -> None:
    """Read-modify-write +a loads before and stores after."""
    source = """
    int main(void) {
        int x = 10;
        __asm__ volatile("add $5, %%eax" : "+a"(x));
        return x;
    }
    """
    asm = _compile("rmw_codegen", source)
    main_body = asm.split("_main:", 1)[1].split("\n_", 1)[0]
    # Should see a load of x into eax before the template, and a store after.
    assert "add" in main_body


def test_tied_operand_zero() -> None:
    """Input constraint "0" ties to output operand 0."""
    source = """
    int double_it(int x) {
        int result;
        __asm__ volatile("shl $1, %%eax" : "=a"(result) : "0"(x));
        return result;
    }
    int main(void) { return double_it(21); }
    """
    asm = _compile("tied", source)
    assert "shl" in asm
```

Add calls in `__main__` (alphabetical with existing tests).

- [ ] **Step 2: Implement full `generate_extended_asm`**

Replace the stub in `cc/codegen/x86/emission.py`. The method needs these phases:

**Phase 1 — Build operand location map.**

For each output and input operand, determine its "location" — the string that will be substituted into the template:

```python
# Map from constraint letter to register name (32-bit).
CONSTRAINT_REGISTER_32 = {"a": "eax", "b": "ebx", "c": "ecx", "d": "edx"}
CONSTRAINT_REGISTER_BYTE = {"a": "al", "b": "bl", "c": "cl", "d": "dl"}
```

For each operand, parse the constraint:
- Strip leading `=`, `+`, `&` modifiers.
- Core letter: `a`/`b`/`c`/`d` → register name. `g` → memory address via `_local_address(name)`. `m` → memory address. `q`/`qm` → byte register (pick `cl` or `dl` as scratch — the one NOT used by other operands). `0` → same location as output 0. `t`/`u` → not substituted (x87 implicit).

Build a list: `operand_locations: list[str]` indexed by operand number (outputs first, then inputs). Also build `operand_byte_locations: list[str]` for `%b` substitution.

**Phase 2 — Pre-template: load inputs.**

For each `+` (read-modify-write) output: `mov <register>, [var_address]`.
For each `"0"` tied input: `mov <register_of_output_0>, [var_address]`.
For each `"u"` (x87 ST1) input: `fld qword [var_address]`.
For `"0"` + `"u"` combination in math.c patterns: load `"0"` first (into ST0), then `"u"` (pushes to ST1).

For `"g"` inputs: no pre-load needed — the template references them via memory operand.

**Phase 3 — Substitute and emit template.**

Process the template string character by character (after `decode_string_escapes`):
- `%%` → `%`
- `%[name]` → look up `name` in the named-operand map → get operand index → `operand_locations[index]`
- `%b[name]` → `operand_byte_locations[index]`
- `%N` (digit) → `operand_locations[N]`
- `%bN` → `operand_byte_locations[N]`

Emit each line of the substituted result.

**Phase 4 — Post-template: store outputs.**

For each output operand:
- `"=a"`/`"=b"`/`"=c"`/`"=d"`: `mov [var_address], <register>`
- `"+a"` etc.: same (the register was modified in-place by the template)
- `"=&q"`: `movzx eax, <byte_register>` then `mov [var_address], eax`
- `"=t"`: `fstp qword [var_address]`
- `"=m"`: nothing (template wrote directly to memory)

**Phase 5 — Clobber handling.**

Call `self.ax_clear()`. For any output that wrote to a pinned register, clear that pin's tracking.

**Implementation note:** The `_local_address(name)` helper (generator.py:2332) returns the memory operand for locals (`[ebp-N]`) and globals (`[_g_name]`). Use it for `"g"` and `"m"` operand substitution. For register-aliased globals, use the register name directly.

- [ ] **Step 3: Run the tests**

```bash
./tests/test_cc_extended_asm.py
```

All nine tests must pass.

- [ ] **Step 4: Regression suites**

```bash
./tests/test_cc_casts.py && ./tests/test_cc_local_structs.py && ./tests/test_cc_bitfields.py && ./tests/test_cc_assign_expr.py && ./tests/test_cc_va_arg_sizeof.py && ./tests/test_cc_fptr_array.py && ./tests/test_programs.py
```

All green.

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/emission.py tests/test_cc_extended_asm.py
git commit -m "feat(cc): extended asm operand substitution + integer constraint codegen"
```

---

### Task 3: x87 FP constraints (`=t`, `u`, `0` tied)

**Files:**
- Modify: `cc/codegen/x86/emission.py` (extend `generate_extended_asm` for x87)
- Modify: `tests/test_cc_extended_asm.py` (add x87 tests)

- [ ] **Step 1: Add x87 tests**

Append to `tests/test_cc_extended_asm.py`:

```python
def test_x87_cos() -> None:
    """x87 fcos via =t output and "0" tied input."""
    source = """
    double x;
    double result;
    void compute(void) {
        __asm__("fcos" : "=t"(result) : "0"(x));
    }
    int main(void) { compute(); return 0; }
    """
    asm = _compile("x87_cos", source)
    assert "fcos" in asm
    assert "fld" in asm or "fstp" in asm


def test_x87_atan2() -> None:
    """x87 fpatan via =t output, "0" tied input, and "u" ST(1) input."""
    source = """
    double x;
    double y;
    double result;
    void compute(void) {
        __asm__("fpatan" : "=t"(result) : "0"(x), "u"(y) : "st(1)");
    }
    int main(void) { compute(); return 0; }
    """
    asm = _compile("x87_atan2", source)
    assert "fpatan" in asm


def test_x87_fnstcw_memory_output() -> None:
    """x87 fnstcw with =m memory output — template writes directly to memory."""
    source = """
    unsigned int control_word;
    void read_cw(void) {
        __asm__("fnstcw %0" : "=m"(control_word));
    }
    int main(void) { read_cw(); return 0; }
    """
    asm = _compile("x87_fnstcw", source)
    assert "fnstcw" in asm
```

Add calls in `__main__` (alphabetical).

- [ ] **Step 2: Extend constraint handling for x87**

In `generate_extended_asm`:

- `"=t"` output: set `operand_locations[i]` to a sentinel (e.g. `"__x87_st0__"`) — this constraint is implicit and should NOT appear in the template. Post-template: emit `fstp qword [var_address]`.
- `"u"` input: pre-template: emit `fld qword [var_address]`. Set location to a sentinel — implicit, not substituted.
- `"0"` tied to a `"=t"` output: pre-template: emit `fld qword [var_address]` (loads into ST0; a subsequent `"u"` input will push this down to ST1). Location: same sentinel as the output.
- `"st(1)"` clobber: no action.

For the `fld`/`fstp` instructions, the variable must be a global or local with a memory address. Use `_local_address(name)` to get the operand. Since cc.py doesn't have a `double` type for local variables yet, these patterns work with global `double` variables where the storage is just 8 bytes at a label (`_g_name`). Verify that `_local_address` works for globals declared with type `"double"` — if not, extend the `variable_types` registration in the `VarDecl` codegen to accept `"double"` and allocate 8 bytes.

**Important:** The `fld`/`fstp` size must be `qword` (8 bytes for double). NASM syntax: `fld qword [addr]`, `fstp qword [addr]`.

- [ ] **Step 3: Run the tests**

```bash
./tests/test_cc_extended_asm.py
```

All twelve tests must pass.

- [ ] **Step 4: Regression suites**

All existing suites green.

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/emission.py tests/test_cc_extended_asm.py
git commit -m "feat(cc): x87 FP constraints (=t, u, 0-tied) for extended inline asm"
```

---

### Task 4: CHANGELOG + full CI matrix + PR

**Files:**
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Add CHANGELOG entry**

```markdown
- cc.py now supports GCC extended inline asm statements with
  output/input/clobber operand sections.  Covers the full
  constraint set used by `signal.c`, `syscall.c`, and `math.c`:
  integer GP register constraints (`=a`/`+b`/`g`/`=&q`), x87 FP
  constraints (`=t`/`u`/`0`-tied), memory output (`=m`), named
  operands (`[name]`), positional and byte-part substitution
  (`%[name]`/`%N`/`%b`/`%%`), and clobber lists.  Unblocks the
  last two `user/libbboeos/` files: `signal.c` and `math.c`.
```

- [ ] **Step 2: Reflow and commit**

```bash
tools/wrap_md.py docs/CHANGELOG.md
git add docs/CHANGELOG.md
git commit -m "docs(cc): note GCC extended inline asm support"
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
- `./tests/test_cc_fptr_array.py`
- `./tests/test_cc_extended_asm.py` (new)
- `python3 -m pytest tests/unit/`
- `./tests/test_archive.py`
- `./tests/test_kernel_archive.py`

All green.

- [ ] **Step 4: Push and open a PR**

PR description should link to the spec on `design-specs` and mention the unblocked `signal.c` and `math.c`.

---

## Self-review checklist

- **Spec coverage:**
  - `AsmOperand` + `ExtendedAsm` AST nodes → Task 1 step 2.
  - Parser for `asm volatile("..." : outs : ins : clobbers)` → Task 1 step 3.
  - `__asm__` spelling → Task 1 step 3 (IDENT check for both `"asm"` and `"__asm__"`).
  - Named operands `[name]` → Task 1 step 3 (`_parse_asm_operands`).
  - Operand substitution (`%[name]`, `%N`, `%b`, `%%`) → Task 2 step 2, Phase 3.
  - `"=a"`/`"=b"`/`"=c"`/`"=d"` output → Task 2 step 2, Phase 4.
  - `"+a"`/`"+b"`/`"+c"`/`"+d"` read-modify-write → Task 2 step 2, Phases 2+4.
  - `"=&q"`/`"=&qm"` early-clobber → Task 2 step 2, Phase 1 + Phase 4.
  - `"g"` general input → Task 2 step 2, Phase 1.
  - `"0"` tied input → Task 2 step 2, Phase 2.
  - `"=t"` x87 output → Task 3 step 2.
  - `"u"` x87 ST(1) input → Task 3 step 2.
  - `"=m"` memory output → Task 3 step 2.
  - Clobber list → Task 2 step 2, Phase 5.
  - IR builder fallback → Task 1 step 4.

- **Placeholder scan:** None.

- **Type consistency:** `AsmOperand(constraint, expression, name)` fields consistent across parser and codegen. `generate_extended_asm` name consistent in dispatch and definition. `CONSTRAINT_REGISTER_32` / `CONSTRAINT_REGISTER_BYTE` used in Tasks 2 and 3.
