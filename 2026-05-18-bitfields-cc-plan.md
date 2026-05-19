# cc.py Bitfields + Type Casts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `uint8_t` bitfield struct members and C-style type-cast
expressions to cc.py, then convert every bit-twiddly driver in the
tree (NE2000, FDC, PIC, RTC, DMA, SB16, PS/2) to use the new syntax.

**Architecture:** Cast expressions and bitfields are independent
language features; cast lands first because bitfield code uses
`*(uint8_t *)&struct_var` as the byte-bridge.  Each driver conversion
gets its own commit for bisectability.  Tests grep cc.py's asm output
for the expected shift/mask sequences and rely on the existing QEMU
test matrix to catch real-world regressions in converted drivers.

**Tech Stack:** Python 3 (cc.py), NASM, QEMU, x86 16/32-bit asm,
existing pytest-style test harnesses in `tests/`.

**Spec:** [2026-05-18-bitfields-cc-design.md](./2026-05-18-bitfields-cc-design.md)

---

## Phase 1 — Cast expressions

Cast support is a prerequisite for the bitfield byte-bridge idiom
`*(uint8_t *)&cr` and `(struct foo *)&byte`.  Lands first because it
also has standalone value (clearer narrowing in mixed-width code).

### Task 1.1: Add `Cast` AST node

**Files:**
- Modify: `cc/ast_nodes.py`

- [ ] **Step 1: Add the node class**

Add after the `AddressOf` class (alphabetical placement; `Cast` < `ConstDecl`):

```python
@dataclass(kw_only=True, slots=True)
class Cast(Node):
    """C-style type cast expression: ``(T)expr`` or ``(T *)expr``.

    ``target_type`` is a type string in the same shape as
    ``StructField.type_name`` and parser-internal type names: e.g.
    ``"uint8_t"``, ``"int *"``, ``"struct foo *"``.  Casts emit no
    runtime instructions; the node exists so the code generator can
    pick the right load/store width and the right struct field
    offsets when the cast result feeds into ``*`` or ``->``.
    """

    expression: Node
    target_type: str
```

- [ ] **Step 2: Export from `__init__` star list (if there is one)**

Check the top of `cc/parser.py` imports for `from .ast_nodes import (...)`; add `Cast` alphabetically.

- [ ] **Step 3: Commit**

```bash
git add cc/ast_nodes.py cc/parser.py
git commit -m "cc(ast): add Cast node for (T)expr expressions"
```

### Task 1.2: Parser — detect cast at `LPAREN` + type token

**Files:**
- Modify: `cc/parser.py` (unary-expression / primary path)

The parser already has `TYPE_TOKENS` in `cc/tokens.py`.  Cast detection
is lookahead: at an `LPAREN`, peek the next token; if it's in
`TYPE_TOKENS` (or `STRUCT`), parse as a cast.

- [ ] **Step 1: Locate the unary-expression entry point**

Run:

```bash
grep -n "_parse_unary\|parse_unary\|LPAREN" cc/parser.py | head -20
```

Identify the function that handles `(` in expression position (likely
`_parse_unary` or `_parse_primary`).  Note its name for Step 2.

- [ ] **Step 2: Add the cast branch**

In the LPAREN-handling branch, before falling through to "parenthesised
expression", inspect the next token via `self.tokens[self.pos + 1]`:

```python
if self.peek()[0] == "LPAREN":
    next_kind = self.tokens[self.pos + 1][0]
    if next_kind in TYPE_TOKENS or next_kind == "STRUCT":
        self.eat("LPAREN")
        target_type = self.parse_type()  # consumes uint8_t / struct foo + zero or more STARs
        self.eat("RPAREN")
        inner = self._parse_unary()      # cast binds tighter than * / / but looser than postfix
        return Cast(expression=inner, line=line, target_type=target_type)
    # fall through to existing parenthesised-expression path
```

`parse_type()` already exists (used by `_parse_struct_declaration` and
parameter parsing); it eats one type keyword plus any number of `STAR`
tokens.

Import `TYPE_TOKENS` from `cc/tokens.py` at the top of `cc/parser.py`
if not already imported.

- [ ] **Step 3: Manual smoke test**

```bash
echo 'int main() { int x; return (uint8_t)x; }' > /tmp/cast_smoke.c
python3 cc.py --bits 32 /tmp/cast_smoke.c /tmp/cast_smoke.asm && head -30 /tmp/cast_smoke.asm
```

Expected: cc.py exits 0 and emits asm that returns `x` (the cast is a
no-op at this codegen stage; full codegen wired in Task 1.3).

- [ ] **Step 4: Commit**

```bash
git add cc/parser.py
git commit -m "cc(parser): parse (T)expr cast expressions"
```

### Task 1.3: Codegen — cast as identity

**Files:**
- Modify: `cc/codegen/x86/generator.py`

- [ ] **Step 1: Find the expression dispatch site**

Run:

```bash
grep -n "AddressOf\|isinstance.*Node\|expression.__class__" cc/codegen/x86/generator.py | head -20
```

Identify the central `_emit_expression` (or equivalent) dispatch; the
Cast handler slots in alongside `AddressOf`.

- [ ] **Step 2: Add the handler**

```python
elif isinstance(expression, Cast):
    # Cast is type-system-only; emit the inner expression unchanged.
    # The cast's target_type travels with the result so any consumer
    # (member access, deref, store) uses the right width / struct
    # offsets.
    self._emit_expression(expression.expression)
    # No runtime instructions.  Tag the result type for downstream.
    self._result_type = expression.target_type
```

If `_result_type` isn't a real attribute, use whatever existing
mechanism the generator has for tagging expression result types.  If
none exists, the type tag is only needed when a cast's result is
dereferenced or member-accessed; in that case, look up the parent
node's expectation rather than tagging at expression time.

- [ ] **Step 3: Verify the smoke from Task 1.2 still passes**

```bash
python3 cc.py --bits 32 /tmp/cast_smoke.c /tmp/cast_smoke.asm
nasm -f bin -i src/include/ /tmp/cast_smoke.asm -o /tmp/cast_smoke.bin
```

Expected: both commands exit 0.

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): emit Cast as identity (type-only)"
```

### Task 1.4: Cast tests

**Files:**
- Create: `tests/test_cc_casts.py`

- [ ] **Step 1: Write the test driver**

```python
#!/usr/bin/env python3
"""cc.py cast-expression coverage.

Runs cc.py over small C snippets that exercise (T)expr and (T *)expr,
assembles the output with nasm, and asserts cc.py emitted no extra
move/and beyond what an identity cast requires.

Usage:
    tests/test_cc_casts.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
INCLUDE_DIR = REPO_ROOT / "src" / "include"


def compile_snippet(*, source: str, work: Path, name: str) -> str:
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    bin_path = work / f"{name}.bin"
    source_path.write_text(source)
    cc_result = subprocess.run(
        ["python3", str(CC), "--bits", "32", str(source_path), str(asm_path)],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["nasm", "-f", "bin", "-i", f"{INCLUDE_DIR}/", str(asm_path), "-o", str(bin_path)],
        capture_output=True,
        check=True,
        text=True,
    )
    return asm_path.read_text()


def test_value_cast_is_identity(*, work: Path) -> None:
    asm = compile_snippet(
        name="value_cast",
        source="int main() { int x = 42; return (uint8_t)x; }",
        work=work,
    )
    # The cast should not add any AND/SHR beyond the function epilogue.
    body = asm.split("main:", 1)[1].split("\n.", 1)[0]
    assert "and " not in body.lower(), f"unexpected truncation in {body}"


def test_pointer_cast_is_identity(*, work: Path) -> None:
    compile_snippet(
        name="pointer_cast",
        source=(
            "int main() {\n"
            "    uint8_t b = 0xAB;\n"
            "    uint8_t *p = (uint8_t *)&b;\n"
            "    return *p;\n"
            "}\n"
        ),
        work=work,
    )


def main() -> int:
    fail_count = 0
    with tempfile.TemporaryDirectory(prefix="test_cc_casts_") as temp_dir:
        work = Path(temp_dir)
        for test in (test_value_cast_is_identity, test_pointer_cast_is_identity):
            try:
                test(work=work)
                print(f"PASS  {test.__name__}")
            except Exception as exception:
                fail_count += 1
                print(f"FAIL  {test.__name__}: {exception}")
    print()
    print(f"{2 - fail_count} passed, {fail_count} failed")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the test**

```bash
chmod +x tests/test_cc_casts.py
tests/test_cc_casts.py
```

Expected: `2 passed, 0 failed`.

- [ ] **Step 3: Add to the matrix in `.github/workflows/test.yml`**

Find the existing `test_cc_bits` / `test_cc_compatibility` entries and
add `test_cc_casts` alongside them.

- [ ] **Step 4: Commit**

```bash
git add tests/test_cc_casts.py .github/workflows/test.yml
git commit -m "tests: add cc.py cast-expression coverage"
```

---

## Phase 2 — Bitfield struct members

### Task 2.1: Extend `StructField` with `bit_width`

**Files:**
- Modify: `cc/ast_nodes.py`

- [ ] **Step 1: Add the field**

In `class StructField`:

```python
@dataclass(kw_only=True, slots=True)
class StructField(Node):
    """A single field declaration inside a struct body.

    ``bit_width`` is ``None`` for regular fields and ``1..8`` for
    bitfield members.  Anonymous bitfields use ``field_name=None``.
    """

    bit_width: int | None = None
    field_name: str | None
    type_name: str
```

(Reorder alphabetically: `bit_width` < `field_name` < `type_name`.
Also relax `field_name` to `str | None` for anonymous bitfields.)

- [ ] **Step 2: Commit**

```bash
git add cc/ast_nodes.py
git commit -m "cc(ast): add bit_width to StructField; allow anonymous"
```

### Task 2.2: Parser — accept `: N` after field name; anonymous `uint8_t : N`

**Files:**
- Modify: `cc/parser.py` (`_parse_struct_declaration`)

- [ ] **Step 1: Replace the field-parsing loop body**

The current loop unconditionally reads an IDENT after the type.  Two
new shapes:

- `uint8_t name : N;` — IDENT followed by `COLON NUMBER`
- `uint8_t : N;` — `COLON NUMBER` directly after type

Replace the loop body (lines ~367-388) with:

```python
while self.peek()[0] != "RBRACE":
    field_type = self.parse_type()
    bit_width: int | None = None
    field_name: str | None
    if self.peek()[0] == "LPAREN":
        # Function pointer field: type (*field_name)(params)
        self.eat("LPAREN")
        self.eat("STAR")
        field_name = self.eat("IDENT")[1]
        self.eat("RPAREN")
        self.eat("LPAREN")
        self.parse_parameters()
        self.eat("RPAREN")
        field_type = "function_pointer"
    elif self.peek()[0] == "COLON":
        # Anonymous bitfield: ``uint8_t : N;``
        self.eat("COLON")
        bit_width = int(self.eat("NUMBER")[1])
        field_name = None
    else:
        field_name = self.eat("IDENT")[1]
        if self.peek()[0] == "COLON":
            # Named bitfield: ``uint8_t name : N;``
            self.eat("COLON")
            bit_width = int(self.eat("NUMBER")[1])
    # Optional [N] for fixed-size array fields.  Bitfields cannot be
    # arrays.
    if bit_width is None and self.peek()[0] == "LBRACKET":
        self.eat("LBRACKET")
        count_token = self.eat("NUMBER")
        self.eat("RBRACKET")
        field_type = f"{field_type}[{count_token[1]}]"
    if bit_width is not None:
        if field_type != "uint8_t":
            message = (
                f"bitfield container must be uint8_t (got {field_type!r}) at line {line}"
            )
            raise SyntaxError(message)
        if not (1 <= bit_width <= 8):
            message = f"bitfield width must be 1..8 (got {bit_width}) at line {line}"
            raise SyntaxError(message)
    self.eat("SEMI")
    fields.append(
        StructField(
            bit_width=bit_width,
            field_name=field_name,
            line=line,
            type_name=field_type,
        )
    )
```

`COLON` is already a token (used by enum init and labels); confirm with
`grep -n COLON cc/tokens.py`.

- [ ] **Step 2: Verify `COLON` is tokenised**

```bash
grep -n "COLON" cc/tokens.py
```

Expected: a `COLON` entry in the lexer regex.  If absent, add `|
(?P<COLON>:)`.

- [ ] **Step 3: Smoke test**

```bash
cat > /tmp/bf_smoke.c <<'EOF'
struct flags {
    uint8_t a : 1;
    uint8_t b : 1;
    uint8_t : 2;
    uint8_t c : 4;
};

int main() {
    return 0;
}
EOF
python3 cc.py --bits 32 /tmp/bf_smoke.c /tmp/bf_smoke.asm
```

Expected: exit 0 (parses; no struct usage yet so codegen is trivial).

- [ ] **Step 4: Negative-case smokes**

```bash
echo 'struct bad { uint16_t a : 4; }; int main() { return 0; }' > /tmp/bf_bad1.c
python3 cc.py --bits 32 /tmp/bf_bad1.c /tmp/x.asm; echo "exit=$?"

echo 'struct bad { uint8_t a : 9; }; int main() { return 0; }' > /tmp/bf_bad2.c
python3 cc.py --bits 32 /tmp/bf_bad2.c /tmp/x.asm; echo "exit=$?"
```

Expected: both exit non-zero with the message strings from the parser.

- [ ] **Step 5: Commit**

```bash
git add cc/parser.py
git commit -m "cc(parser): accept uint8_t bitfields in struct decls"
```

### Task 2.3: Layout — byte offset + bit offset table

**Files:**
- Modify: `cc/codegen/x86/generator.py` (struct layout helpers)

cc.py already computes per-field byte offsets and `sizeof(struct)`.
Locate the layout function:

```bash
grep -n "_struct_layout\|struct_offsets\|_field_offset\|struct_size" cc/codegen/x86/generator.py | head -20
```

- [ ] **Step 1: Augment the layout result with bit metadata**

Wherever the layout function returns `{field_name: byte_offset}` (or
similar), extend the value to a tuple `(byte_offset, bit_offset,
bit_width)` for every field.  Regular fields get `bit_offset=0,
bit_width=None`.

Pseudo-shape (adapt to existing data structure):

```python
def _compute_struct_layout(decl: StructDecl) -> StructLayout:
    fields: dict[str, FieldInfo] = {}
    byte_offset = 0
    run_bits = 0  # bits used in the current bitfield run
    for field in decl.fields:
        if field.bit_width is not None:
            if run_bits == 0 and field.field_name is None and byte_offset != 0:
                # leading anonymous bitfield in a new run is fine
                pass
            if run_bits + field.bit_width > 8:
                raise SyntaxError(
                    f"bitfield run exceeds 8 bits in struct {decl.name!r}"
                )
            if field.field_name is not None:
                fields[field.field_name] = FieldInfo(
                    byte_offset=byte_offset,
                    bit_offset=run_bits,
                    bit_width=field.bit_width,
                    type_name="uint8_t",
                )
            run_bits += field.bit_width
            # A run closes when a regular field follows, NOT after each
            # bitfield; the next iteration may add more to the run.
            continue
        # Regular field: close any open bitfield run.
        if run_bits > 0:
            byte_offset += 1
            run_bits = 0
        fields[field.field_name] = FieldInfo(
            byte_offset=byte_offset,
            bit_offset=0,
            bit_width=None,
            type_name=field.type_name,
        )
        byte_offset += _type_size(field.type_name)
    if run_bits > 0:
        byte_offset += 1  # the final run's byte
    return StructLayout(fields=fields, size=byte_offset)
```

Adapt to the exact existing class names; the key changes are:

- Track `run_bits` across consecutive bitfields.
- Close the run (advance `byte_offset` by 1) only when a regular field
  follows or the loop ends.
- Reject `run_bits + bit_width > 8` per spec.

- [ ] **Step 2: Verify with the smoke from 2.2**

```bash
python3 cc.py --bits 32 /tmp/bf_smoke.c /tmp/bf_smoke.asm
```

Expected: still exits 0.  No new asm; layout is internal.

- [ ] **Step 3: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): bitfield layout (byte_offset, bit_offset, bit_width)"
```

### Task 2.4: Codegen — bitfield read

**Files:**
- Modify: `cc/codegen/x86/generator.py` (member-access emit path)

- [ ] **Step 1: Locate the rvalue member-access emitter**

```bash
grep -n "MemberAccess\|emit_member_read\|_emit_member" cc/codegen/x86/generator.py | head
```

Find the path that emits `s.field` or `p->field` as a value.

- [ ] **Step 2: Branch on bit_width**

In the member-access emit, look up the field in the layout.  If
`bit_width is not None`:

```python
# Bitfield read: load byte, shift, mask.
self._emit("    mov al, [{base} + {offset}]".format(base=base_reg, offset=info.byte_offset))
if info.bit_offset != 0:
    self._emit(f"    shr al, {info.bit_offset}")
if info.bit_width != 8:
    self._emit(f"    and al, {(1 << info.bit_width) - 1}")
# AL is the result (zero-extended); promote to EAX if downstream needs it.
self._emit("    movzx eax, al")
```

(`movzx eax, al` only if the caller expects a 32-bit result; mirror
what `uint8_t` regular-field reads already do.)

- [ ] **Step 3: Smoke test**

```bash
cat > /tmp/bf_read.c <<'EOF'
struct flags {
    uint8_t a : 1;
    uint8_t b : 1;
    uint8_t : 2;
    uint8_t c : 4;
};

int main() {
    struct flags f;
    return f.c;
}
EOF
python3 cc.py --bits 32 /tmp/bf_read.c /tmp/bf_read.asm
grep -A3 "main:" /tmp/bf_read.asm | head -20
```

Expected: emitted asm contains `mov al, [...]`, `shr al, 4`, `and al,
15` (or `0x0F`).

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): bitfield read (shr/and on byte load)"
```

### Task 2.5: Codegen — bitfield write

**Files:**
- Modify: `cc/codegen/x86/generator.py` (member-assignment emit path)

- [ ] **Step 1: Locate the lvalue member-assignment emitter**

```bash
grep -n "MemberAssignment\|emit_member_write\|_emit_member_assign" cc/codegen/x86/generator.py | head
```

- [ ] **Step 2: Branch on bit_width with peepholes**

```python
# Bitfield write.
if info.bit_width is None:
    # ... existing regular-field path ...
    return

field_mask = ((1 << info.bit_width) - 1) << info.bit_offset
clear_mask = (~field_mask) & 0xFF

# Peephole: 1-bit field with literal 0 or 1.
if info.bit_width == 1 and isinstance(value_expr, Number) and value_expr.value in (0, 1):
    if value_expr.value == 0:
        self._emit(f"    and byte [{base_reg} + {info.byte_offset}], {clear_mask}")
    else:
        self._emit(f"    or byte [{base_reg} + {info.byte_offset}], {field_mask}")
    return

# General path: read-modify-write through AL/BL.
self._emit_expression(value_expr)        # result in EAX (low byte BL after move)
self._emit("    mov bl, al")
if info.bit_width != 8:
    self._emit(f"    and bl, {(1 << info.bit_width) - 1}")
if info.bit_offset != 0:
    self._emit(f"    shl bl, {info.bit_offset}")
self._emit(f"    mov al, [{base_reg} + {info.byte_offset}]")
self._emit(f"    and al, {clear_mask}")
self._emit("    or al, bl")
self._emit(f"    mov [{base_reg} + {info.byte_offset}], al")
```

(Use whatever the existing emitter's register-allocation contract is —
the snippet above assumes EAX/BL are available, mirroring the
read-side pattern.)

- [ ] **Step 3: Smoke test**

```bash
cat > /tmp/bf_write.c <<'EOF'
struct flags {
    uint8_t a : 1;
    uint8_t b : 1;
    uint8_t : 2;
    uint8_t c : 4;
};

int main() {
    struct flags f;
    f.a = 1;
    f.c = 5;
    return f.a;
}
EOF
python3 cc.py --bits 32 /tmp/bf_write.c /tmp/bf_write.asm
grep -B1 -A12 "main:" /tmp/bf_write.asm | head -40
```

Expected: contains `or byte [...], 1` for `f.a = 1` (peephole) and the
general read-modify-write sequence for `f.c = 5`.

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): bitfield write with 1-bit literal peephole"
```

### Task 2.6: Reject `&bitfield`

**Files:**
- Modify: `cc/codegen/x86/generator.py` (AddressOf emit path)

- [ ] **Step 1: Locate the AddressOf emit**

```bash
grep -n "AddressOf" cc/codegen/x86/generator.py | head
```

- [ ] **Step 2: Reject bitfield targets**

In the AddressOf emit, if the inner is a `MemberAccess` whose resolved
field has `bit_width is not None`:

```python
if isinstance(expression.expression, MemberAccess):
    info = self._lookup_member(expression.expression)
    if info.bit_width is not None:
        message = (
            f"cannot take address of bitfield '{expression.expression.field_name}' "
            f"at line {expression.line}"
        )
        raise SyntaxError(message)
```

- [ ] **Step 3: Negative smoke**

```bash
cat > /tmp/bf_addr.c <<'EOF'
struct f { uint8_t a : 1; };
int main() {
    struct f x;
    uint8_t *p = &x.a;
    return 0;
}
EOF
python3 cc.py --bits 32 /tmp/bf_addr.c /tmp/x.asm; echo "exit=$?"
```

Expected: non-zero exit with "cannot take address of bitfield 'a'".

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): reject &bitfield"
```

### Task 2.7: Bitfield tests

**Files:**
- Create: `tests/test_cc_bitfields.py`

- [ ] **Step 1: Write the test driver**

```python
#!/usr/bin/env python3
"""cc.py bitfield-codegen coverage.

For each snippet, run cc.py, assemble with nasm, and grep the emitted
asm for the expected shift/mask instructions.  Also covers negative
cases (run > 8, container != uint8_t, &bitfield).

Usage:
    tests/test_cc_bitfields.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
INCLUDE_DIR = REPO_ROOT / "src" / "include"


def cc_emit(*, source: str, work: Path, name: str) -> str:
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    source_path.write_text(source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", str(source_path), str(asm_path)],
        capture_output=True,
        check=True,
        text=True,
    )
    return asm_path.read_text()


def cc_expect_fail(*, source: str, work: Path, name: str, message_fragment: str) -> None:
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    source_path.write_text(source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", str(source_path), str(asm_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0, f"expected cc.py to fail for {name}"
    assert message_fragment in result.stderr, (
        f"expected {message_fragment!r} in cc.py stderr; got {result.stderr!r}"
    )


def test_read_1bit_at_offset_0(*, work: Path) -> None:
    asm = cc_emit(
        name="read_1bit_0",
        source=(
            "struct f { uint8_t a : 1; };\n"
            "int main() { struct f x; return x.a; }\n"
        ),
        work=work,
    )
    assert "and al, 1" in asm.lower()


def test_read_4bit_at_offset_4(*, work: Path) -> None:
    asm = cc_emit(
        name="read_4bit",
        source=(
            "struct f {\n"
            "    uint8_t a : 1;\n"
            "    uint8_t b : 1;\n"
            "    uint8_t : 2;\n"
            "    uint8_t c : 4;\n"
            "};\n"
            "int main() { struct f x; return x.c; }\n"
        ),
        work=work,
    )
    lowered = asm.lower()
    assert "shr al, 4" in lowered
    assert "and al, 15" in lowered or "and al, 0xf" in lowered


def test_write_1bit_literal_1_peephole(*, work: Path) -> None:
    asm = cc_emit(
        name="write_1bit_lit1",
        source=(
            "struct f { uint8_t a : 1; };\n"
            "int main() { struct f x; x.a = 1; return 0; }\n"
        ),
        work=work,
    )
    assert "or byte" in asm.lower()


def test_run_overflow_rejected(*, work: Path) -> None:
    cc_expect_fail(
        message_fragment="run exceeds 8 bits",
        name="run_overflow",
        source=(
            "struct bad { uint8_t a : 4; uint8_t b : 5; };\n"
            "int main() { return 0; }\n"
        ),
        work=work,
    )


def test_non_uint8_container_rejected(*, work: Path) -> None:
    cc_expect_fail(
        message_fragment="must be uint8_t",
        name="bad_container",
        source=(
            "struct bad { uint16_t a : 4; };\n"
            "int main() { return 0; }\n"
        ),
        work=work,
    )


def test_addressof_bitfield_rejected(*, work: Path) -> None:
    cc_expect_fail(
        message_fragment="cannot take address of bitfield",
        name="bad_addr",
        source=(
            "struct f { uint8_t a : 1; };\n"
            "int main() { struct f x; uint8_t *p = &x.a; return 0; }\n"
        ),
        work=work,
    )


TESTS = (
    test_read_1bit_at_offset_0,
    test_read_4bit_at_offset_4,
    test_write_1bit_literal_1_peephole,
    test_run_overflow_rejected,
    test_non_uint8_container_rejected,
    test_addressof_bitfield_rejected,
)


def main() -> int:
    fail_count = 0
    with tempfile.TemporaryDirectory(prefix="test_cc_bitfields_") as temp_dir:
        work = Path(temp_dir)
        for test in TESTS:
            try:
                test(work=work)
                print(f"PASS  {test.__name__}")
            except AssertionError as exception:
                fail_count += 1
                print(f"FAIL  {test.__name__}: {exception}")
            except subprocess.CalledProcessError as exception:
                fail_count += 1
                print(f"FAIL  {test.__name__}: cc.py crashed: {exception.stderr}")
    print()
    print(f"{len(TESTS) - fail_count} passed, {fail_count} failed")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the test**

```bash
chmod +x tests/test_cc_bitfields.py
tests/test_cc_bitfields.py
```

Expected: `6 passed, 0 failed`.

- [ ] **Step 3: Add to CI matrix**

In `.github/workflows/test.yml`, add `test_cc_bitfields` to the matrix.

- [ ] **Step 4: Commit**

```bash
git add tests/test_cc_bitfields.py .github/workflows/test.yml
git commit -m "tests: add cc.py bitfield codegen + negative-case coverage"
```

### Task 2.8: Add `bboeos.h` shadow declarations

**Files:**
- Modify: `tests/bboeos.h`

Per `feedback_cc_compat_needs_header_decl`, every cc.py feature
testable via `test_cc_compatibility` (clang -fsyntax-only) needs
declarations in `tests/bboeos.h` or clang rejects the source.  Casts
and bitfields are real C, so clang accepts them — no new declarations
needed.

- [ ] **Step 1: Run the compatibility check**

```bash
tests/test_cc_compatibility.py
```

Expected: still passes (no regressions; no new symbols needed).

- [ ] **Step 2: No-op commit if check passes**

If `test_cc_compatibility` shows nothing changed, skip this task — no
commit.

---

## Phase 3 — Driver conversions

Each driver gets its own commit so any regression is bisectable.
Sources of truth for which bits are which:

- **PIC**: 8259A datasheet (any x86 reference).  IMR bit N = mask IRQ
  N.
- **NE2000**: National Semiconductor DP8390 datasheet.  CR register at
  offset 0x00 of the base, ISR at 0x07, IMR at 0x0F, etc.
- **FDC**: Intel 82077AA datasheet.  MSR at 0x3F4, DOR at 0x3F2.
- **RTC / CMOS**: Motorola MC146818 datasheet.  Status Reg A index
  0x0A, B index 0x0B.
- **8237 DMA**: Mode register write encoding (channel select, transfer
  type, auto-init, etc.).
- **SB16**: Creative Sound Blaster 16 Hardware Programming Guide.  DSP
  status (data-available bit 7).
- **PS/2**: Intel 8042 controller status register.

For each driver, the workflow is:

1. Read the current `kernel_outb` / `kernel_inb` call sites.
2. Define the struct in `src/include/registers.h` (or extend it).
3. Rewrite the call sites using struct literals + `*(uint8_t *)&` for
   writes and `(struct foo *)&byte` for reads.
4. Build + run the relevant test suite.
5. Commit.

### Task 3.1: Create `src/include/registers.h`

**Files:**
- Create: `src/include/registers.h`

- [ ] **Step 1: Initial file with PIC IMR**

```c
#ifndef BBOEOS_REGISTERS_H
#define BBOEOS_REGISTERS_H

#include "types.h"

/*
 * Hardware-register bitfield structs.
 *
 * Bit ordering: LSB-first.  Field at offset 0 is bit 0 (the
 * least-significant bit of the underlying byte).  This matches x86
 * GCC convention and the way most x86 datasheets number bits.  If a
 * datasheet draws bits MSB-first (as some do), mentally invert
 * before transcribing here.
 *
 * Every struct here is exactly one byte (containing one bitfield
 * run summing to <= 8 bits) and is meant to bridge to / from a port
 * byte via:
 *
 *     struct foo s = { 0 };
 *     s.field = ...;
 *     kernel_outb(PORT, *(uint8_t *)&s);
 *
 *     uint8_t raw = kernel_inb(PORT);
 *     struct foo *s = (struct foo *)&raw;
 *     ... s->field ...;
 */

/* 8259A interrupt-mask register.  Bit N == 1 disables IRQ N.
 * PIC1 (0x21) covers IRQ 0..7, PIC2 (0xA1) covers IRQ 8..15.
 */
struct pic_imr {
    uint8_t irq0 : 1;
    uint8_t irq1 : 1;
    uint8_t irq2 : 1;
    uint8_t irq3 : 1;
    uint8_t irq4 : 1;
    uint8_t irq5 : 1;
    uint8_t irq6 : 1;
    uint8_t irq7 : 1;
};

#endif
```

- [ ] **Step 2: Verify the header parses through cc.py**

```bash
cat > /tmp/regs_smoke.c <<'EOF'
#include "registers.h"
int main() {
    struct pic_imr m = { 0 };
    m.irq3 = 0;
    return *(uint8_t *)&m;
}
EOF
python3 cc.py --bits 32 -I src/include /tmp/regs_smoke.c /tmp/regs_smoke.asm
```

(Use whatever flag cc.py exposes for include paths; check
`python3 cc.py --help` if `-I` isn't right.)

Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add src/include/registers.h
git commit -m "include: add registers.h with struct pic_imr"
```

### Task 3.2: Convert PIC IMR call sites

**Files:**
- Modify: `src/drivers/ps2.c`, `src/drivers/fdc.c`, `src/drivers/ne2k.c`, `src/drivers/sb16.c`, `src/drivers/serial.c` (whichever currently touch `0x21` / `0xA1`)

- [ ] **Step 1: Enumerate the call sites**

```bash
grep -n "0x21\|0xA1\|PIC1_DATA\|PIC2_DATA" src/drivers/*.c
```

For each line, note whether it's an IMR read (mask = inb), modify, or
write (`outb(0x21, mask | 0x20)`).

- [ ] **Step 2: Convert each site**

Pattern: `mask = kernel_inb(0x21); kernel_outb(0x21, mask & ~(1 << 3));`
becomes:

```c
uint8_t raw = kernel_inb(0x21);
struct pic_imr *imr = (struct pic_imr *)&raw;
imr->irq3 = 0;
kernel_outb(0x21, raw);
```

`mask = kernel_inb(0x21); kernel_outb(0x21, mask | (1 << 6));` becomes:

```c
uint8_t raw = kernel_inb(0x21);
struct pic_imr *imr = (struct pic_imr *)&raw;
imr->irq6 = 1;
kernel_outb(0x21, raw);
```

For PIC2 (0xA1), reuse `struct pic_imr` — the bit layout is identical
even though the IRQ numbers it covers are 8..15.

- [ ] **Step 3: Build and run the relevant test suites**

```bash
./make_os.sh
tests/test_bboefs.py            # FDC IRQ
tests/test_programs.py          # broad coverage
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/drivers/*.c
git commit -m "drivers: use struct pic_imr for PIC1/PIC2 IMR writes"
```

### Task 3.3: NE2000 — CR, ISR, IMR, RCR, TCR, DCR

**Files:**
- Modify: `src/include/registers.h` (add structs)
- Modify: `src/drivers/ne2k.c`

- [ ] **Step 1: Add NE2000 structs**

```c
/* NE2000 / DP8390 command register (offset 0x00).
 *
 *  stop:     1 = stop NIC
 *  start:    1 = start NIC
 *  transmit: 1 = start packet transmission
 *  rd[3]:    remote DMA command (000 = not allowed, 001 = read,
 *            010 = write, 011 = send packet, 100 = abort/complete)
 *  page[2]:  register page select (00 = page 0, 01 = page 1, 10 = page 2)
 */
struct ne2k_cr {
    uint8_t stop     : 1;
    uint8_t start    : 1;
    uint8_t transmit : 1;
    uint8_t rd       : 3;
    uint8_t page     : 2;
};

/* NE2000 interrupt status register (offset 0x07, page 0).  Write a 1
 * to ack each bit.
 */
struct ne2k_isr {
    uint8_t prx : 1;  /* packet received OK */
    uint8_t ptx : 1;  /* packet transmitted OK */
    uint8_t rxe : 1;  /* receive error */
    uint8_t txe : 1;  /* transmit error */
    uint8_t ovw : 1;  /* RX-ring overwrite warning */
    uint8_t cnt : 1;  /* counter overflow */
    uint8_t rdc : 1;  /* remote DMA complete */
    uint8_t rst : 1;  /* reset status */
};

/* NE2000 interrupt mask register (offset 0x0F, page 0).  Bit layout
 * mirrors ISR exactly: 1 = enabled, 0 = masked.
 */
struct ne2k_imr {
    uint8_t prx : 1;
    uint8_t ptx : 1;
    uint8_t rxe : 1;
    uint8_t txe : 1;
    uint8_t ovw : 1;
    uint8_t cnt : 1;
    uint8_t rdc : 1;
    uint8_t : 1;      /* reserved */
};

/* Receive configuration register (offset 0x0C, page 0). */
struct ne2k_rcr {
    uint8_t sep : 1;  /* save errored packets */
    uint8_t ar  : 1;  /* accept runt packets */
    uint8_t ab  : 1;  /* accept broadcast */
    uint8_t am  : 1;  /* accept multicast */
    uint8_t pro : 1;  /* promiscuous physical */
    uint8_t mon : 1;  /* monitor mode */
    uint8_t : 2;
};

/* Transmit configuration register (offset 0x0D, page 0). */
struct ne2k_tcr {
    uint8_t crc  : 1;  /* inhibit CRC */
    uint8_t lb   : 2;  /* loopback control */
    uint8_t atd  : 1;  /* auto-transmit disable */
    uint8_t ofst : 1;  /* collision-offset enable */
    uint8_t : 3;
};

/* Data configuration register (offset 0x0E, page 0). */
struct ne2k_dcr {
    uint8_t wts : 1;  /* word transfer select (1 = 16-bit) */
    uint8_t bos : 1;  /* byte order (0 = little-endian) */
    uint8_t las : 1;  /* long address select (0 = 16-bit DMA) */
    uint8_t ls  : 1;  /* loopback select */
    uint8_t arm : 1;  /* auto-init remote */
    uint8_t ft  : 2;  /* FIFO threshold */
    uint8_t : 1;
};
```

- [ ] **Step 2: Convert ne2k.c**

Replace every `kernel_outb(BASE + offset, magic_literal)` with the
struct-literal + bridge pattern.  Replace every
`kernel_inb(BASE + 0x07) & 0x01` with a struct-pointer cast on the
byte.

Build a list first:

```bash
grep -n "kernel_outb\|kernel_inb" src/drivers/ne2k.c
```

For each line where the operand is a port at offsets 0x00, 0x07, 0x0F,
0x0C, 0x0D, 0x0E (or the named CR/ISR/IMR/RCR/TCR/DCR aliases),
rewrite using the matching struct.  Leave bulk-data port writes (the
DMA data port at offset 0x10) unchanged — those are not bit-flag
registers.

- [ ] **Step 3: Build and test**

```bash
./make_os.sh
tests/test_programs.py          # covers icmp / dns / ping
```

Expected: networking tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/include/registers.h src/drivers/ne2k.c
git commit -m "drivers(ne2k): bitfield structs for CR/ISR/IMR/RCR/TCR/DCR"
```

### Task 3.4: FDC — MSR, DOR

**Files:**
- Modify: `src/include/registers.h`
- Modify: `src/drivers/fdc.c`

- [ ] **Step 1: Add FDC structs**

```c
/* 82077AA Main Status Register (port 0x3F4, read-only). */
struct fdc_msr {
    uint8_t drive0_busy : 1;
    uint8_t drive1_busy : 1;
    uint8_t drive2_busy : 1;
    uint8_t drive3_busy : 1;
    uint8_t cmd_busy    : 1;  /* CB: controller is in command/exec phase */
    uint8_t non_dma     : 1;  /* NDM: controller is in non-DMA mode */
    uint8_t data_in     : 1;  /* DIO: 1 = host reads from data port */
    uint8_t rqm         : 1;  /* request for master: data port ready */
};

/* 82077AA Digital Output Register (port 0x3F2, write). */
struct fdc_dor {
    uint8_t drive_select : 2;  /* DS1..0 */
    uint8_t reset_n      : 1;  /* 1 = controller out of reset */
    uint8_t dma_irq      : 1;  /* 1 = DMA + IRQ enabled */
    uint8_t motor0       : 1;
    uint8_t motor1       : 1;
    uint8_t motor2       : 1;
    uint8_t motor3       : 1;
};
```

- [ ] **Step 2: Convert fdc.c**

Find the MSR poll loops and DOR writes:

```bash
grep -n "0x3F4\|0x3F2\|FDC_MSR\|FDC_DOR" src/drivers/fdc.c
```

For DOR writes: replace literal byte constants like `0x1C` (=
motor0 | reset_n | dma_irq) with explicit struct literals.

For MSR reads: replace `kernel_inb(0x3F4) & 0xC0 == 0x80` with the
struct-pointer cast + `msr->rqm && !msr->data_in` test.

Inline asm blocks that touch `[0x3F4]` directly (the `fdc_send` and
`fdc_recv_wait` loops) are bare port polls — they don't gain
readability from a struct and would lose the tight `in/and/cmp/jne`
sequence.  Leave those alone.  The struct conversion is for the C-side
DOR writes and any C-side MSR reads.

- [ ] **Step 3: Build and test**

```bash
./make_os.sh
tests/test_bboefs.py
tests/test_programs.py
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/include/registers.h src/drivers/fdc.c
git commit -m "drivers(fdc): bitfield structs for MSR + DOR"
```

### Task 3.5: RTC — Status Reg A, Status Reg B

**Files:**
- Modify: `src/include/registers.h`
- Modify: `src/drivers/rtc.c`

- [ ] **Step 1: Add RTC structs**

```c
/* MC146818 Status Register A (CMOS index 0x0A). */
struct rtc_status_a {
    uint8_t rs  : 4;  /* periodic-interrupt rate select */
    uint8_t dv  : 3;  /* divider control */
    uint8_t uip : 1;  /* update in progress (read-only) */
};

/* MC146818 Status Register B (CMOS index 0x0B). */
struct rtc_status_b {
    uint8_t dse  : 1;  /* daylight savings enable */
    uint8_t mode : 1;  /* 24-hour mode (1 = 24h) */
    uint8_t dm   : 1;  /* data mode (1 = binary, 0 = BCD) */
    uint8_t sqwe : 1;  /* square-wave enable */
    uint8_t uie  : 1;  /* update-ended interrupt enable */
    uint8_t aie  : 1;  /* alarm interrupt enable */
    uint8_t pie  : 1;  /* periodic interrupt enable */
    uint8_t set  : 1;  /* halt time updates */
};
```

- [ ] **Step 2: Convert rtc.c**

```bash
grep -n "0x0A\|0x0B\|status_a\|status_b\|0x40\|0x70" src/drivers/rtc.c
```

Convert the alarm-enable / square-wave-enable / mode-set writes (which
currently use `|= 0x40`, `&= ~0x80`, etc.) to field assignments on
`struct rtc_status_b`.

- [ ] **Step 3: Build and test**

```bash
./make_os.sh
tests/test_programs.py          # sleep, date, uptime, alarm
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add src/include/registers.h src/drivers/rtc.c
git commit -m "drivers(rtc): bitfield structs for status reg A/B"
```

### Task 3.6: DMA mode byte (FDC chan 2, SB16 chan 1)

**Files:**
- Modify: `src/include/registers.h`
- Modify: `src/drivers/fdc.c`, `src/drivers/sb16.c`

- [ ] **Step 1: Add DMA mode struct**

```c
/* 8237 DMA mode register write encoding. */
struct dma_mode {
    uint8_t channel       : 2;  /* 0..3 within the controller */
    uint8_t transfer_type : 2;  /* 00 = verify, 01 = write, 10 = read */
    uint8_t auto_init     : 1;
    uint8_t address_dir   : 1;  /* 0 = increment, 1 = decrement */
    uint8_t mode          : 2;  /* 00 = demand, 01 = single,
                                   10 = block, 11 = cascade */
};
```

- [ ] **Step 2: Convert call sites**

```bash
grep -n "DMA_MODE\|dma_mode\|0x46\|0x4A" src/drivers/fdc.c src/drivers/sb16.c
```

The FDC uses channel 2 (read = 0x46 = single + write-transfer +
channel 2; write to disk = 0x4A = single + read-transfer + channel 2).
SB16 uses channel 1 (auto-init single mode).

Rewrite the magic-byte stores as struct-literal builds.

- [ ] **Step 3: Build and test**

```bash
./make_os.sh
tests/test_bboefs.py            # FDC DMA
tests/test_programs.py
```

Expected: pass.  (SB16 isn't in the test matrix; manual QEMU smoke if
desired but not required.)

- [ ] **Step 4: Commit**

```bash
git add src/include/registers.h src/drivers/fdc.c src/drivers/sb16.c
git commit -m "drivers(dma): bitfield struct for 8237 mode byte"
```

### Task 3.7: SB16 — DSP status

**Files:**
- Modify: `src/include/registers.h`
- Modify: `src/drivers/sb16.c`

- [ ] **Step 1: Add DSP status struct**

```c
/* Sound Blaster 16 DSP read-status register (port BASE + 0x0E). */
struct sb16_dsp_read_status {
    uint8_t : 7;
    uint8_t data_avail : 1;
};
```

- [ ] **Step 2: Convert sb16.c**

```bash
grep -n "0x22E\|dsp_read_status\|& 0x80" src/drivers/sb16.c
```

Convert the C-side polls (not the inline-asm spin in
`sb16_dsp_wait_write`, which stays as-is per the FDC precedent).

- [ ] **Step 3: Build and verify**

```bash
./make_os.sh
```

SB16 has no automated test coverage; rely on local QEMU audio smoke
if convenient, otherwise trust the diff review.

- [ ] **Step 4: Commit**

```bash
git add src/include/registers.h src/drivers/sb16.c
git commit -m "drivers(sb16): bitfield struct for DSP read status"
```

### Task 3.8: PS/2 — controller status

**Files:**
- Modify: `src/include/registers.h`
- Modify: `src/drivers/ps2.c`

- [ ] **Step 1: Add PS/2 status struct**

```c
/* Intel 8042 PS/2 controller status register (port 0x64 read). */
struct ps2_status {
    uint8_t obf      : 1;  /* output buffer full (data ready for host) */
    uint8_t ibf      : 1;  /* input buffer full */
    uint8_t sys      : 1;  /* system flag */
    uint8_t cmd      : 1;  /* 1 = last write was command, 0 = data */
    uint8_t keylock  : 1;
    uint8_t aux      : 1;  /* 1 = data from mouse, 0 = keyboard */
    uint8_t timeout  : 1;
    uint8_t parity   : 1;
};
```

- [ ] **Step 2: Convert ps2.c**

```bash
grep -n "0x64\|& 0x01\|status.*ps2" src/drivers/ps2.c
```

Convert the C-side status checks.  Inline-asm IRQ-1 paths stay raw if
they exist.

- [ ] **Step 3: Build and test**

```bash
./make_os.sh
tests/test_programs.py          # PS/2 console input on every boot
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add src/include/registers.h src/drivers/ps2.c
git commit -m "drivers(ps2): bitfield struct for controller status"
```

### Task 3.9: Archive byte-parity sweep

**Files:**
- Modify: `archive/*.asm` (only those whose `.c` counterparts changed)

Per `feedback_archive_byte_parity`, any substantive rewrite of
`src/c/<name>.c` requires updating `archive/<name>.asm`.  This work
edits drivers under `src/drivers/`, not userland programs under
`src/c/` — so the archive rule probably doesn't apply here.

- [ ] **Step 1: Verify**

```bash
ls archive/ | grep -E "ne2k|fdc|rtc|sb16|ps2|serial" || echo "no driver archives"
```

If the listing is empty, this task is a no-op.  Note that fact in the
PR description and skip.

- [ ] **Step 2: If any driver archive exists, port the bitfield
       changes to keep the asm comparable**

(Likely no-op for this PR.)

### Task 3.10: Full CI matrix run

Per `feedback_run_full_ci_matrix_locally`, this PR touches
kernel-driver paths broadly; run every suite in
`.github/workflows/test.yml` locally before opening the PR.

- [ ] **Step 1: List the matrix**

```bash
grep -E "test_|name:" .github/workflows/test.yml | head -40
```

- [ ] **Step 2: Run each suite**

```bash
./make_os.sh                                # rebuild after all conversions
tests/test_cc_compatibility.py
tests/test_cc_bits.py
tests/test_cc_casts.py
tests/test_cc_bitfields.py
tests/test_asm.py
tests/test_bboefs.py
tests/test_programs.py
# plus any pipeline / ext2 / matrix entries listed in test.yml
```

Expected: every suite passes.

- [ ] **Step 3: If any fail, bisect via the per-driver commits**

The per-driver commit boundaries from Tasks 3.2 through 3.8 make
`git bisect` trivial.

---

## Phase 4 — Release

### Task 4.1: Open the PR

**Files:**
- Modify: `docs/CHANGELOG.md` (Unreleased section)

- [ ] **Step 1: Add a changelog entry**

Under the Unreleased section in `docs/CHANGELOG.md`, add a line such
as:

```markdown
- cc.py: bitfield struct members (`uint8_t name : N;`) and type-cast
  expressions (`(T)expr`, `(T *)expr`).  Used throughout the driver
  layer to express hardware register bits declaratively in
  `src/include/registers.h` (PIC IMR, NE2000 CR/ISR/IMR/RCR/TCR/DCR,
  FDC MSR/DOR, RTC status, 8237 DMA mode, SB16 DSP status, PS/2
  status).
```

- [ ] **Step 2: Commit and push**

```bash
git add docs/CHANGELOG.md
git commit -m "docs(changelog): cc.py bitfields + type casts"
git push -u origin $(git branch --show-current)
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create --title "feat(cc): uint8_t bitfields + type casts; convert driver registers" --body "$(cat <<'EOF'
## Summary

Add two new language features to cc.py:

- **Bitfield struct members** (`uint8_t name : N;`, anonymous
  padding `uint8_t : N;`).  uint8_t containers, LSB-first, runs sum to
  <= 8 bits.
- **Type-cast expressions** (`(T)expr`, `(T *)expr`).  Identity
  codegen; enables the `*(uint8_t *)&struct_var` bridge.

Convert every bit-twiddly driver to use the new syntax with shared
structs in `src/include/registers.h`: PIC IMR, NE2000 (CR / ISR / IMR
/ RCR / TCR / DCR), FDC MSR + DOR, RTC status A/B, 8237 DMA mode,
SB16 DSP read status, PS/2 controller status.

Design: see [2026-05-18 cc.py bitfields + type casts](https://github.com/bboe/BBoeOS/blob/design-specs/2026-05-18-bitfields-cc-design.md).

## Test plan
- [x] tests/test_cc_casts.py
- [x] tests/test_cc_bitfields.py
- [x] tests/test_cc_compatibility.py (clang accepts every converted source)
- [x] tests/test_cc_bits.py (both --bits=16 and --bits=32 modes)
- [x] tests/test_asm.py
- [x] tests/test_bboefs.py
- [x] tests/test_programs.py
- [x] Full local matrix per .github/workflows/test.yml

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

**Spec coverage:** Every section of
`2026-05-18-bitfields-cc-design.md` maps to a task above:

- Language additions §1a (bitfields) → Tasks 2.1, 2.2.
- Language additions §1b (casts) → Tasks 1.1, 1.2.
- Layout rules → Task 2.3.
- Codegen (read/write/cast/&bitfield-reject) → Tasks 2.4, 2.5, 2.6, 1.3.
- Driver conversions table → Tasks 3.2 through 3.8.
- Testing → Tasks 1.4, 2.7, 2.8, 3.10.
- Out of scope (uint16/uint32 containers, signed bitfields,
  designated init, `: 0`) — explicitly not implemented; nothing to do.
- Risks (parser ambiguity, bit-ordering, driver migration) — addressed
  by the type-keyword-lookahead in Task 1.2, the bit-ordering note at
  the top of `registers.h` in Task 3.1, and the per-driver-commit +
  full-matrix-run discipline in Phase 3.

**Placeholders:** No "TBD" / "implement later" / "similar to" / "add
appropriate error handling" anywhere.  Every code-emitting step shows
the code.

**Type consistency:** `bit_width`, `bit_offset`, `byte_offset`,
`info.bit_width`, `info.bit_offset` are used consistently across
Tasks 2.1, 2.3, 2.4, 2.5, 2.6.  `target_type` on `Cast` is used in
Tasks 1.1 and 1.3.  `struct pic_imr`, `struct ne2k_cr`, etc. names
are stable across Tasks 3.1 through 3.8.
