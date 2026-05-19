# cc.py Stack-Local Structs + Designated Init + Const-Fold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add stack-local struct value declarations (including arrays
of struct locals), dot-access read/write on locals, address-of for
locals, sizeof, indexed-member access for local arrays, `= { 0 }` and
designated-field initializer syntax, plus a constant-fold + last-
write-wins peephole pair that collapses bitfield register init into a
single `mov byte [ebp-N], <imm>`.

**Architecture:** Four "not yet supported" sites in
`cc/codegen/x86/generator.py` gain a local-storage branch parallel to
the existing global-storage path.  The frame allocator already
supports custom-size locals.  Designated init lowers to a zero-store
prelude plus per-field writes; the const-fold + collapse peepholes
turn the literal-only case into one byte store.

**Tech Stack:** Python 3 (cc.py), NASM, QEMU, x86 32-bit asm.

**Spec:** [2026-05-19-cc-local-structs-design.md](./2026-05-19-cc-local-structs-design.md)

---

## Pre-flight notes

Implementer subagents can treat these as ground truth (verified at
plan time):

- **`StructDecl` + `struct_sizes`** already track byte sizes for any
  struct type (PR #425).  `struct_sizes[tag]` is the total byte size
  including bitfield-run packing.
- **`allocate_local(name, *, size=None)`** in `cc/codegen/x86/generator.py:2758`
  already accepts a custom byte size.  Default is `target.int_size`.
- **Four "not yet supported" rejection sites** in `generator.py`:
  - `:1011`-ish — `generate_member_access` dot path: `if object_name
    not in self.global_scalars: raise "dot member access on local
    struct values is not yet supported"`.
  - `:1143`-ish — `generate_member_address_of` dot path: same shape.
  - `:1168`-ish — `generate_member_assign` dot path: same shape.
  - `:1273`-ish — `generate_member_index` dot path: same shape.  This
    one is `ptr->field[index]` syntax where field is an array member;
    different from the `arr[i].field` case below.
- **`generate_index_member_access` / `generate_index_member_assign`**
  at `:1373`/`:1412` use `_resolve_index_member_layout` (`:1342`),
  which only accepts `name in self.global_arrays`.  Local struct
  arrays require either extending this helper or adding a parallel
  one.
- **`VarDecl`** already has `init: Node | None`.  Struct init can be
  modeled as a new AST node assigned to `init`, or as a new
  `struct_init: dict[str, Node] | None` field on `VarDecl`.  The
  plan uses a new AST node (`StructInitializer`) for clarity.
- **`COLON`, `EQUALS`, `DOT`, `LBRACE`, `RBRACE`** are already valid
  token kinds.

---

## Phase A — AST + Parser

### Task A.1: Add `StructInitializer` AST node

**Files:**
- Modify: `cc/ast_nodes.py`

- [ ] **Step 1: Add the node**

Add after `StructField` (alphabetical: `StructDecl` < `StructField` <
`StructInit` < `StructInitializer`):

```python
@dataclass(kw_only=True, slots=True)
class StructInitializer(Node):
    """Designated struct initializer ``{ .field = expr, ... }`` or
    the zero-init shorthand ``{ 0 }``.

    ``fields`` is an empty dict when the source wrote ``{ 0 }``.  For
    designated init, the dict maps each field name to its initializer
    expression; omitted fields are zero-initialized at codegen time.
    """

    fields: dict[str, Node]
```

(The existing `StructInit` node is for brace-initializers of array
elements; this is a distinct concept and gets its own name.)

- [ ] **Step 2: Export from parser**

In `cc/parser.py`, find the `from cc.ast_nodes import (...)` block and
add `StructInitializer` alphabetically (between `StructInit` and
`TailCall` or wherever alphabetical insertion sorts).

- [ ] **Step 3: Run regression suites**

```bash
cd /home/ubuntu/bboeos/.claude/worktrees/n2
tests/test_cc_compatibility.py
tests/test_cc_bitfields.py
```

Both must pass.

- [ ] **Step 4: Commit**

```bash
git add cc/ast_nodes.py cc/parser.py
git commit -m "cc(ast): add StructInitializer for designated init"
```

### Task A.2: Parser — accept `= { 0 }` and `= { .field = expr }`

**Files:**
- Modify: `cc/parser.py` (where `VarDecl` declarations are parsed —
  grep `VarDecl(` in `cc/parser.py` to find the construction sites).

- [ ] **Step 1: Locate the VarDecl init parse site**

```bash
grep -nE "VarDecl\(|_parse_var_decl|parse_init" cc/parser.py | head -10
```

Find where a `VarDecl`'s `init` is parsed (after the `=` sign).
Likely a `parse_initializer` helper or inlined inside the decl-parser.

- [ ] **Step 2: Branch on `LBRACE` after `=`**

If the next token is `LBRACE`, parse a struct initializer.  Otherwise,
fall through to the existing scalar/expression init.

```python
if self.peek()[0] == "LBRACE":
    self.eat("LBRACE")
    fields: dict[str, Node] = {}
    if self.peek()[0] == "NUMBER" and self.peek()[1] == "0":
        # Zero-init shorthand: { 0 }
        self.eat("NUMBER")
    else:
        # Designated: { .field = expr, ... }
        while self.peek()[0] != "RBRACE":
            if self.peek()[0] != "DOT":
                raise SyntaxError(
                    "positional struct initializers not supported; "
                    "use { 0 } or designated initializers at line "
                    f"{self.peek()[2]}"
                )
            self.eat("DOT")
            field_name = self.eat("IDENT")[1]
            self.eat("EQUALS")
            field_value = self.parse_assignment_expression()
            if field_name in fields:
                raise SyntaxError(
                    f"duplicate initializer for field '{field_name}' "
                    f"at line {self.peek()[2]}"
                )
            fields[field_name] = field_value
            if self.peek()[0] == "COMMA":
                self.eat("COMMA")
    self.eat("RBRACE")
    init = StructInitializer(fields=fields, line=line)
else:
    init = self.parse_expression()
```

Adapt the helper name `parse_assignment_expression` to whatever the
parser already uses for "expression that doesn't include comma at
top level" — likely `parse_expression` or `parse_ternary` if cc.py
doesn't have comma-expressions.

- [ ] **Step 3: Smoke tests**

```bash
cat > /tmp/si_zero.c <<'EOF'
struct foo { uint8_t a; };
struct foo g;
int main() { g.a = 0; return 0; }
EOF
python3 cc.py --bits 32 /tmp/si_zero.c /tmp/si_zero.asm
echo "global parse exit=$?"
```

Should still parse (no init in this file).

```bash
cat > /tmp/si_pos.c <<'EOF'
struct foo { uint8_t a; };
int main() {
    struct foo c = { 1 };
    return 0;
}
EOF
python3 cc.py --bits 32 /tmp/si_pos.c /tmp/x.asm 2>&1 | tail -2
echo "positional exit=$?"
```

Expected: non-zero exit with `positional struct initializers not
supported`.

(A positive smoke that actually exercises `{0}` and designated init
on a local can't be added until Task B.1 lands stack-local declaration
support; the parse-only smoke above is the best we can do here.)

- [ ] **Step 4: Run regression suites**

```bash
tests/test_cc_compatibility.py
tests/test_cc_bitfields.py
tests/test_cc_bits.py
```

All pass with no regression.

- [ ] **Step 5: Commit**

```bash
git add cc/parser.py
git commit -m "cc(parser): accept { 0 } and designated struct initializers"
```

---

## Phase B — Stack-local struct codegen

### Task B.1: Allocate frame slot for struct locals

**Files:**
- Modify: `cc/codegen/x86/emission.py` (`generate_statement`'s
  `VarDecl` branch around `:2338`, plus the local-scan that determines
  frame slot sizes).

- [ ] **Step 1: Locate where the local scan determines slot sizes**

```bash
grep -nE "scan_locals|allocate_local|_type_size.*VarDecl|frame_size" cc/codegen/x86/emission.py | head -15
```

Find the function (likely `_scan_locals` or similar) that walks the
function body and calls `allocate_local(name)` for each `VarDecl`.

- [ ] **Step 2: Recognize struct-typed locals**

In the local-scan, when encountering a `VarDecl` whose `type_name`
starts with `"struct "`, call `allocate_local(name,
size=self._type_size(declaration.type_name))` instead of the default
int-size slot.

For local array-of-struct (`type_name` matches
`"struct foo[N]"`), compute size as `N * self._type_size("struct foo")`
where `N` is the bracket count.

- [ ] **Step 3: Set `variable_types` for the local**

After allocation, also set `self.variable_types[name] =
declaration.type_name` so downstream member-access can look up the
tag.

- [ ] **Step 4: Smoke test**

```bash
cd /home/ubuntu/bboeos/.claude/worktrees/n2
cat > /tmp/local_alloc.c <<'EOF'
struct foo { uint8_t a; uint8_t b; };
int main() {
    struct foo c;
    return 0;
}
EOF
python3 cc.py --bits 32 /tmp/local_alloc.c /tmp/local_alloc.asm
grep -A4 "^main:" /tmp/local_alloc.asm | head -6
```

Expected: `main`'s prologue contains `sub esp, 2` (or `sub esp, N`
for whatever struct_sizes says).  No errors emitted.

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/emission.py
git commit -m "cc(codegen): allocate frame slot for struct locals"
```

### Task B.2: Dot-access read on local struct

**Files:**
- Modify: `cc/codegen/x86/generator.py` `generate_member_access`
  (around `:989-1010`).

- [ ] **Step 1: Replace the "not in global_scalars" rejection with a
       dispatch**

Find:

```python
if object_name not in self.global_scalars:
    message = "dot member access on local struct values is not yet supported; use a pointer and '->'"
    raise CompileError(message, line=expression.line)
```

Replace the bail with:

```python
if object_name not in self.global_scalars and object_name not in self.locals:
    message = f"undefined variable '{object_name}'"
    raise CompileError(message, line=expression.line)
```

- [ ] **Step 2: Compute the base operand for either storage class**

Right after the layout resolution (line ~1019 onwards in the existing
flow):

```python
if object_name in self.global_scalars:
    base_symbol = self._local_address(object_name)
else:
    # Stack-local struct.
    frame_offset = self.locals[object_name]
    base_symbol = f"ebp-{frame_offset}"  # used inside ``[...]``
```

Adapt the formatting to match how the existing code builds the inner
of `[<addr>]`.  Verify by reading the surrounding emit lines that
construct `[base_symbol+offset]`.

- [ ] **Step 3: Smoke test (regular field read)**

```bash
cat > /tmp/local_read.c <<'EOF'
struct foo { uint8_t a; uint8_t b; };
int main() {
    struct foo c;
    c.a = 1;     /* This depends on Task B.3; for now just the read */
    return c.a;
}
EOF
```

Skip the write line for now if Task B.3 isn't done.  Use:

```bash
cat > /tmp/local_read.c <<'EOF'
struct foo { uint8_t a; uint8_t b; };
struct foo global_init = { 0 };  /* placeholder so codegen has something to read */
int main() {
    struct foo c;
    return c.a;  /* reads garbage; we just want the codegen path */
}
EOF
python3 cc.py --bits 32 /tmp/local_read.c /tmp/local_read.asm
grep -A10 "^main:" /tmp/local_read.asm
```

Expected: `main` body contains `mov al, [ebp-<N>]` (or
`movzx eax, byte [ebp-<N>]`).

- [ ] **Step 4: Run pre-existing regressions**

```bash
tests/test_cc_compatibility.py
tests/test_cc_bitfields.py
tests/test_cc_bits.py
```

Zero regressions (no existing source uses local struct values).

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): dot-access read on local struct values"
```

### Task B.3: Dot-access write on local struct

**Files:**
- Modify: `cc/codegen/x86/generator.py` `generate_member_assign`
  (around `:1153-1170`).

- [ ] **Step 1: Same dispatch as Task B.2**

Replace the `not in global_scalars` rejection with the
global-or-local dispatch.  Build `base_symbol` the same way.

- [ ] **Step 2: Make sure the bitfield write helpers work**

`_emit_bitfield_write_literal` and `_emit_bitfield_write` take an
`addr` string.  Once `base_symbol = "ebp-N"`, `addr` becomes
`[ebp-N+field_offset]` or `[ebp-N]` for field_offset == 0.  Verify
the helpers don't make assumptions about `addr` starting with
`[_g_...]` or `[<reg>+...]`.  They shouldn't — they just stringify
into `mov al, {addr}` etc.

- [ ] **Step 3: Smoke test**

```bash
cat > /tmp/local_write.c <<'EOF'
struct cr { uint8_t a : 1; uint8_t b : 1; };
int main() {
    struct cr c;
    c.a = 1;
    return 0;
}
EOF
python3 cc.py --bits 32 /tmp/local_write.c /tmp/local_write.asm
grep -A8 "^main:" /tmp/local_write.asm
```

Expected: body contains `or byte [ebp-<N>], 1` (the 1-bit literal-1
peephole, now firing on a local byte).

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): dot-access write on local struct values"
```

### Task B.4: Address-of on local struct (and regular field)

**Files:**
- Modify: `cc/codegen/x86/generator.py` `generate_member_address_of`
  (around `:1110-1145`).

- [ ] **Step 1: Replace the rejection with a local-storage branch**

Old:

```python
if object_name not in self.global_scalars:
    message = "cannot take address of member on local struct value; use a pointer and '->'"
    raise CompileError(message, line=expression.line)
```

New: dispatch on global vs local; for locals, emit:

```python
frame_offset = self.locals[object_name]
if info.byte_offset:
    self.emit(f"        lea {self.target.acc}, [ebp-{frame_offset}+{info.byte_offset}]")
else:
    self.emit(f"        lea {self.target.acc}, [ebp-{frame_offset}]")
self.ax_clear()
```

The bitfield-rejection branch (which fires before address-of would
proceed) stays unchanged so `&local.bitfield` still errors.

- [ ] **Step 2: Smoke test**

```bash
cat > /tmp/local_addr.c <<'EOF'
struct foo { uint8_t a; uint8_t b; };
int main() {
    struct foo c;
    uint8_t *p = &c.b;
    return *p;
}
EOF
python3 cc.py --bits 32 /tmp/local_addr.c /tmp/local_addr.asm
grep -A8 "^main:" /tmp/local_addr.asm
```

Expected: contains `lea <reg>, [ebp-<N>+1]` (b's byte_offset is 1
since a is at 0).

Also test `&local` (struct's base address):

```bash
cat > /tmp/local_addr2.c <<'EOF'
struct foo { uint8_t a; };
int main() {
    struct foo c;
    return *(uint8_t *)&c;
}
EOF
python3 cc.py --bits 32 /tmp/local_addr2.c /tmp/local_addr2.asm
grep -A6 "^main:" /tmp/local_addr2.asm
```

Expected: contains `lea <reg>, [ebp-<N>]` followed by a byte load.

(This case may already work via existing AddressOf code paths if `&c`
parses to `AddressOf(Var(c))` rather than `MemberAddressOf`.  Verify
by tracing the parser.  If `&local_struct_value` falls into a
different AddressOf path, ensure that path also handles the new
local-struct case.)

- [ ] **Step 3: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): &local_struct and &local.regular_field"
```

### Task B.5: Indexed access on local struct arrays

**Files:**
- Modify: `cc/codegen/x86/generator.py` `_resolve_index_member_layout`
  (around `:1342`) plus `generate_index_member_access` and
  `generate_index_member_assign`.

- [ ] **Step 1: Extend `_resolve_index_member_layout` to accept locals**

Old (`:1351-1354`):

```python
declaration = self.global_arrays.get(name)
if declaration is None:
    message = f"'{name}' is not a global struct array"
    raise CompileError(message, line=line)
```

New: try `global_arrays` first; if not found, look the name up in
`self.local_arrays` (or whatever cc.py uses to track local-array
types).  Return a different shape so callers can distinguish
global-symbol base from frame-relative base — or, simpler, have
`const_base` be a string that works in either case.

A simple approach: for local struct arrays, set `const_base =
f"ebp-{self.locals[name]}"` (used inside `[const_base + field_offset
+ bx]` → becomes `[ebp-N+field_offset+bx]`).  NASM accepts this
without quoting.

Verify NASM's lexer accepts `[ebp-N+M+ebx]` when N and M are
integer literals.  It should — NASM's address-mode parser folds
constant adds.

- [ ] **Step 2: Smoke test array-of-struct local**

```bash
cat > /tmp/local_arr.c <<'EOF'
struct foo { uint8_t a; uint8_t b; };
int main() {
    struct foo arr[4];
    arr[2].b = 5;
    return arr[2].b;
}
EOF
python3 cc.py --bits 32 /tmp/local_arr.c /tmp/local_arr.asm
grep -A12 "^main:" /tmp/local_arr.asm
```

Expected: indexed addressing off `ebp` plus a `2 * struct_size`
displacement.  The exact emit shape depends on whether the index is
constant-folded.  For a constant index `2`, the compiler may fold
the whole offset into the displacement, producing
`mov byte [ebp-N+M], 5` for some N (frame offset) and M (2 * size +
field_offset).

- [ ] **Step 3: Run regression suites**

```bash
tests/test_cc_compatibility.py
tests/test_cc_bitfields.py
tests/test_cc_bits.py
tests/test_asm.py
tests/test_bboefs.py
tests/test_programs.py
```

Zero regressions.

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): indexed access on local struct arrays"
```

### Task B.6: Emit codegen for `StructInitializer`

**Files:**
- Modify: `cc/codegen/x86/emission.py` (`VarDecl` handling in
  `generate_statement` around `:2338`).

- [ ] **Step 1: Detect StructInitializer init**

In the existing branch:

```python
if isinstance(statement, VarDecl):
    self.visible_vars.add(statement.name)
    self.variable_types[statement.name] = statement.type_name
    if statement.init is not None:
        ...
```

Add a check before the existing init emission:

```python
if isinstance(statement.init, StructInitializer):
    self._emit_struct_initializer(statement.name, statement.init)
    return  # or continue, depending on the control flow
```

- [ ] **Step 2: Implement `_emit_struct_initializer`**

New method on the codegen class:

```python
def _emit_struct_initializer(self, name: str, init: StructInitializer) -> None:
    """Emit zero-store prelude + per-field assignments for a struct local."""
    type_name = self.variable_types[name]
    # Strip the trailing "[N]" for array-of-struct locals — initializer
    # applies to element 0 only (out of scope per the spec); error if
    # we see an initializer on an array decl.
    if "[" in type_name:
        message = f"initializer on array-of-struct local '{name}' is not supported"
        raise CompileError(message, line=init.line)
    tag = type_name[7:]
    size = self.struct_sizes[tag]
    frame_offset = self.locals[name]
    # Zero-store prelude: one `mov byte [ebp-K], 0` per byte of the slot.
    for byte_index in range(size):
        addr = f"[ebp-{frame_offset - byte_index}]" if byte_index < frame_offset else f"[ebp-{frame_offset}+{byte_index}]"
        self.emit(f"        mov byte {addr}, 0")
    # Per-field designated assignments via the existing member-assign
    # codegen path.  Synthesize MemberAssign nodes and dispatch.
    for field_name, value_node in init.fields.items():
        synthetic = MemberAssign(
            arrow=False,
            expr=value_node,
            line=init.line,
            member_name=field_name,
            object_name=name,
        )
        self.generate_member_assign(synthetic)
```

`MemberAssign` already exists in `cc/ast_nodes.py`; verify the
constructor shape (`arrow=False` for dot-form).  `addr` formatting
follows whatever convention the existing member-assign codegen uses
for local struct field access (built in Task B.3).

- [ ] **Step 3: Smoke test**

```bash
cat > /tmp/init_zero.c <<'EOF'
struct cr { uint8_t a : 1; uint8_t b : 4; uint8_t c : 3; };
int main() {
    struct cr c = { 0 };
    return c.a;
}
EOF
python3 cc.py --bits 32 /tmp/init_zero.c /tmp/init_zero.asm
grep -A10 "^main:" /tmp/init_zero.asm
```

Expected: `main` body starts with `mov byte [ebp-1], 0` (one byte
struct), followed by the read.

```bash
cat > /tmp/init_des.c <<'EOF'
struct cr { uint8_t a : 1; uint8_t b : 4; uint8_t c : 3; };
int main() {
    struct cr c = { .a = 1, .b = 5 };
    return c.c;
}
EOF
python3 cc.py --bits 32 /tmp/init_des.c /tmp/init_des.asm
grep -A14 "^main:" /tmp/init_des.asm
```

Expected: zero-store followed by field-write codegen.  After the
const-fold/collapse work in Phase C lands, this collapses to one
`mov byte`.  For now, just verify it parses and emits valid asm.

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/emission.py
git commit -m "cc(codegen): emit zero-store + designated field assigns"
```

---

## Phase C — Const-fold + collapse peepholes

### Task C.1: `known_local_bytes` tracker + invalidation

**Files:**
- Modify: `cc/codegen/x86/generator.py` (add the tracker as an
  instance attribute initialised per-function).

- [ ] **Step 1: Add the tracker**

In the codegen class `__init__` (or in the function-entry hook —
search for where `self.locals` is reset per function):

```python
self.known_local_bytes: dict[int, int] = {}
self._last_byte_store: tuple[int, int] | None = None  # (frame_offset, imm)
```

Reset both on function entry.

- [ ] **Step 2: Hook the emit pipeline to update the tracker**

Find the central `self.emit(line)` method.  Wrap (or fork) it so that
after emitting a line, the tracker is updated.  A simple approach:
parse the emitted line with regex.

Patterns to recognise (case-insensitive):

```python
import re
_MOV_BYTE_LOCAL_IMM = re.compile(r"^\s*mov byte \[ebp-(\d+)(?:\+(\d+))?\], (\d+)\s*$")
_OR_BYTE_LOCAL_IMM = re.compile(r"^\s*or byte \[ebp-(\d+)(?:\+(\d+))?\], (\d+)\s*$")
_AND_BYTE_LOCAL_IMM = re.compile(r"^\s*and byte \[ebp-(\d+)(?:\+(\d+))?\], (\d+)\s*$")
```

After each `emit(line)`:

```python
m = _MOV_BYTE_LOCAL_IMM.match(line)
if m:
    base, off, val = int(m.group(1)), int(m.group(2) or 0), int(m.group(3))
    K = base - off  # frame slot of the targeted byte
    self.known_local_bytes[K] = val & 0xFF
    self._last_byte_store = (K, val & 0xFF)
    return
m = _OR_BYTE_LOCAL_IMM.match(line)
if m:
    base, off, val = int(m.group(1)), int(m.group(2) or 0), int(m.group(3))
    K = base - off
    if K in self.known_local_bytes:
        self.known_local_bytes[K] = (self.known_local_bytes[K] | val) & 0xFF
        self._last_byte_store = (K, self.known_local_bytes[K])
    else:
        self._last_byte_store = None
    return
# AND-byte path: same shape.
m = _AND_BYTE_LOCAL_IMM.match(line)
if m:
    base, off, val = int(m.group(1)), int(m.group(2) or 0), int(m.group(3))
    K = base - off
    if K in self.known_local_bytes:
        self.known_local_bytes[K] = self.known_local_bytes[K] & val & 0xFF
        self._last_byte_store = (K, self.known_local_bytes[K])
    else:
        self._last_byte_store = None
    return
# Any other emit invalidates _last_byte_store.
self._last_byte_store = None
# Conservative invalidation: indirect memory writes, calls, labels.
if "call " in line.lower() or line.strip().endswith(":"):
    self.known_local_bytes.clear()
elif re.search(r"^\s*mov [^,]*\[(?!ebp)", line):  # write through non-ebp register
    self.known_local_bytes.clear()
```

The patterns above match the emit shapes used in this codebase (with
the 8-space indent).  Adjust the indentation regex to match what
`emit()` actually produces.

This is the most fragile part of the work.  Validate aggressively
with the test suite.

- [ ] **Step 3: Add a unit-level regression test**

`tests/test_cc_local_structs.py` (created in Task D.1) will exercise
this.  For this commit, run all suites:

```bash
tests/test_cc_compatibility.py
tests/test_cc_bitfields.py
tests/test_cc_bits.py
tests/test_asm.py
tests/test_bboefs.py
tests/test_programs.py
```

All must pass.  This change has the highest blast-radius risk —
miscategorising an emit as an invalidation could silently break
existing code.  If any test regresses, the regex is wrong.

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): known_local_bytes tracker for const-fold"
```

### Task C.2: Const-fold in `_emit_bitfield_write_literal`

**Files:**
- Modify: `cc/codegen/x86/generator.py` `_emit_bitfield_write_literal`.

- [ ] **Step 1: Add the fold path**

Current shape (post PR #425):

```python
def _emit_bitfield_write_literal(self, info, /, *, addr, value):
    field_mask = ((1 << info.bit_width) - 1) << info.bit_offset
    clear_mask = (~field_mask) & 0xFF
    if value == 0:
        self.emit(f"        and byte {addr}, {clear_mask}")
    else:
        self.emit(f"        or byte {addr}, {field_mask}")
```

Updated:

```python
def _emit_bitfield_write_literal(self, info, /, *, addr, value):
    field_mask = ((1 << info.bit_width) - 1) << info.bit_offset
    clear_mask = (~field_mask) & 0xFF
    # Const-fold path: if addr is a known local byte, compute the
    # folded byte directly and emit a single mov.
    K = self._parse_local_byte_addr(addr)
    if K is not None and K in self.known_local_bytes:
        known = self.known_local_bytes[K]
        new = (known & clear_mask) | ((value << info.bit_offset) & field_mask)
        self.emit(f"        mov byte {addr}, {new}")
        return
    if value == 0:
        self.emit(f"        and byte {addr}, {clear_mask}")
    else:
        self.emit(f"        or byte {addr}, {field_mask}")
```

`_parse_local_byte_addr(addr)` is a tiny helper:

```python
def _parse_local_byte_addr(self, addr: str) -> int | None:
    """Return the frame slot K if addr looks like ``[ebp-K]`` or
    ``[ebp-K+M]``; otherwise None."""
    m = re.match(r"^\[ebp-(\d+)(?:\+(\d+))?\]$", addr.strip())
    if m is None:
        return None
    return int(m.group(1)) - int(m.group(2) or 0)
```

- [ ] **Step 2: Smoke test**

```bash
cat > /tmp/fold.c <<'EOF'
struct cr { uint8_t a : 1; uint8_t b : 1; uint8_t r : 3; uint8_t p : 2; };
int main() {
    struct cr c = { 0 };
    c.a = 1;
    return *(uint8_t *)&c;
}
EOF
python3 cc.py --bits 32 /tmp/fold.c /tmp/fold.asm
grep -A8 "^main:" /tmp/fold.asm
```

Expected: zero-store at `[ebp-1]`, then a folded `mov byte [ebp-1],
1` (not the `or byte` peephole).  After the collapse peephole
(Task C.4) the zero-store will be replaced; for now both stores
emit.

- [ ] **Step 3: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): const-fold literal bitfield writes on local bytes"
```

### Task C.3: Const-fold in `_emit_bitfield_write` (general path)

**Files:**
- Modify: `cc/codegen/x86/generator.py` `_emit_bitfield_write`.

- [ ] **Step 1: Track AX literal value**

Add an instance attribute `self.ax_literal: int | None`.  Reset on
function entry and on any emit that doesn't unambiguously set EAX to
a literal.

The simplest implementation: in the central `emit()` wrapper (Task
C.1), recognise `mov eax, <imm>` patterns:

```python
_MOV_EAX_IMM = re.compile(r"^\s*mov eax, (\d+)\s*$")
m = _MOV_EAX_IMM.match(line)
if m:
    self.ax_literal = int(m.group(1)) & 0xFFFFFFFF
elif line.strip():  # any non-empty emit may have clobbered EAX
    self.ax_literal = None
```

Cheaper alternative: only check whether `_emit_bitfield_write`'s
caller just emitted a literal load.  Skip this task entirely if AL is
not provably literal.  The const-fold then only fires for the
`_literal` helper (Task C.2), which already covers the driver
init case.

If the cheaper path is taken, skip the rest of this task and add a
note to the commit message.

- [ ] **Step 2: Add the fold path**

In `_emit_bitfield_write` after the field_mask / clear_mask
computation:

```python
K = self._parse_local_byte_addr(addr)
if K is not None and K in self.known_local_bytes and self.ax_literal is not None:
    known = self.known_local_bytes[K]
    rhs = self.ax_literal & ((1 << info.bit_width) - 1)
    new = (known & clear_mask) | (rhs << info.bit_offset)
    self.emit(f"        mov byte {addr}, {new}")
    return
```

- [ ] **Step 3: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): const-fold non-literal bitfield writes when AX is literal"
```

### Task C.4: Last-write-wins collapse peephole

**Files:**
- Modify: `cc/codegen/x86/generator.py` central `emit()` wrapper.

- [ ] **Step 1: Add collapse logic to `emit()`**

When the caller is about to emit a `mov byte [ebp-K], <imm>`:

- If `self._last_byte_store == (K, _)` (any prior store to the same
  slot was the most recent emit), replace the previous line in the
  emit buffer rather than appending.

Implementation depends on how `emit()` stores lines.  If it appends
to a list, replace the last element.  If it streams to a file,
buffer the last emit and either flush or replace.

```python
def emit(self, line: str, /) -> None:
    # Pre-process: collapse sequential mov byte stores at same address.
    m = _MOV_BYTE_LOCAL_IMM.match(line)
    if m and self._last_byte_store is not None:
        base, off, val = int(m.group(1)), int(m.group(2) or 0), int(m.group(3))
        K = base - off
        if self._last_byte_store[0] == K and self.lines and _MOV_BYTE_LOCAL_IMM.match(self.lines[-1]):
            self.lines[-1] = line
            self.known_local_bytes[K] = val & 0xFF
            self._last_byte_store = (K, val & 0xFF)
            return
    self.lines.append(line)
    # ... existing post-emit tracker updates from Task C.1 ...
```

`self.lines` is the existing emit buffer — find its actual name
in the codebase (likely `self.lines` or `self.output` — grep
`self.lines.append` or similar).

- [ ] **Step 2: Verify the worked example collapses**

```bash
cat > /tmp/collapse.c <<'EOF'
struct cr { uint8_t a : 1; uint8_t b : 1; uint8_t r : 3; uint8_t p : 2; };
int main() {
    struct cr c = { .a = 1, .r = 4, .p = 2 };
    return *(uint8_t *)&c;
}
EOF
python3 cc.py --bits 32 /tmp/collapse.c /tmp/collapse.asm
grep -A6 "^main:" /tmp/collapse.asm | head -8
```

Expected: exactly ONE `mov byte [ebp-1], <imm>` line in main's body
before the byte read.  The folded byte is `0 | (1<<0) | (4<<2) |
(2<<5) = 1 | 16 | 64 = 81`.  Actually with bitfields the order is
declared-order: a is at bit 0 (1<<0 = 1), b is at bit 1 (unset = 0),
r is at bits 2..4 (4 = 100b, shifted = 16), p is at bits 5..6 (2 =
10b, shifted = 64).  Total: 1 + 16 + 64 = 81.  Verify in the emit.

- [ ] **Step 3: Full local matrix**

```bash
./make_os.sh
tests/test_cc_compatibility.py
tests/test_cc_bitfields.py
tests/test_cc_casts.py
tests/test_cc_bits.py
tests/test_asm.py
tests/test_bboefs.py
tests/test_programs.py
```

Zero regressions.  The collapse peephole is the highest-impact part
of the optimization; if it ever fires incorrectly (collapsing two
stores at different addresses, or skipping a needed store), it'll
manifest as a wrong byte written to a port — catastrophic for
drivers.  The full matrix is non-optional.

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): collapse sequential byte stores at same frame slot"
```

---

## Phase D — Tests, NE2000 rewrite, PR

### Task D.1: `tests/test_cc_local_structs.py`

**Files:**
- Create: `tests/test_cc_local_structs.py`
- Modify: `.github/workflows/test.yml`

- [ ] **Step 1: Write the test driver**

Mirror `tests/test_cc_bitfields.py`'s structure.  Required tests
(alphabetical):

```python
def test_addressof_local_field(*, work: Path) -> None:
    """&local.regular_field emits lea against the frame."""
    asm = compile_snippet(
        name="addressof_local_field",
        source=(
            "struct foo { uint8_t a; uint8_t b; };\n"
            "int main() { struct foo c; uint8_t *p = &c.b; return *p; }\n"
        ),
        work=work,
    )
    body = asm.split("main:", 1)[1].split("\n_", 1)[0].lower()
    assert "lea " in body and "ebp" in body, f"expected lea against ebp in:\n{body}"


def test_array_of_struct_locals_indexed(*, work: Path) -> None:
    """arr[2].field reads from frame + 2*struct_size + field_offset."""
    asm = compile_snippet(
        name="array_struct_local",
        source=(
            "struct foo { uint8_t a; uint8_t b; };\n"
            "int main() {\n"
            "    struct foo arr[4];\n"
            "    arr[2].b = 5;\n"
            "    return arr[2].b;\n"
            "}\n"
        ),
        work=work,
    )
    body = asm.split("main:", 1)[1].split("\n_", 1)[0].lower()
    # Element 2 of struct foo (size 2) field b (offset 1) → byte 5 of arr.
    # Expect a [ebp-N+5] or equivalent disp in the store + load.
    assert "ebp" in body, f"expected ebp-relative addressing in:\n{body}"


def test_designated_init_collapses(*, work: Path) -> None:
    """Designated init with literal values collapses to one byte store."""
    asm = compile_snippet(
        name="designated_collapse",
        source=(
            "struct cr { uint8_t a : 1; uint8_t b : 1; uint8_t r : 3; uint8_t p : 2; };\n"
            "int main() {\n"
            "    struct cr c = { .a = 1, .r = 4, .p = 2 };\n"
            "    return *(uint8_t *)&c;\n"
            "}\n"
        ),
        work=work,
    )
    body = asm.split("main:", 1)[1].split("\n_", 1)[0].lower()
    mov_byte_count = body.count("mov byte [ebp")
    assert mov_byte_count == 1, (
        f"expected exactly 1 mov-byte store after collapse; saw "
        f"{mov_byte_count} in:\n{body}"
    )
    # Folded byte: a=1 (bit 0), r=4 (bits 2..4 → <<2 = 16), p=2 (bits 5..6 → <<5 = 64).
    # Total = 1 + 16 + 64 = 81.
    assert ", 81" in body, f"expected folded byte 81 in:\n{body}"


def test_dot_read_local_field(*, work: Path) -> None:
    """f.field on a local struct reads from [ebp-N+field_offset]."""
    asm = compile_snippet(
        name="dot_read_local",
        source=(
            "struct foo { uint8_t a; uint8_t b; };\n"
            "struct foo g = { 0 };\n"
            "int main() { struct foo c; c.a = g.a; return c.b; }\n"
        ),
        work=work,
    )
    body = asm.split("main:", 1)[1].split("\n_", 1)[0].lower()
    assert "[ebp-" in body, f"expected [ebp-...] addressing in:\n{body}"


def test_dot_write_local_bitfield(*, work: Path) -> None:
    """f.bitfield = 1 on a local emits the literal peephole or fold."""
    asm = compile_snippet(
        name="dot_write_local_bf",
        source=(
            "struct cr { uint8_t a : 1; uint8_t b : 1; };\n"
            "int main() { struct cr c; c.a = 1; return *(uint8_t *)&c; }\n"
        ),
        work=work,
    )
    body = asm.split("main:", 1)[1].split("\n_", 1)[0].lower()
    # Either the 1-bit peephole (`or byte [ebp-N], 1`) or the folded
    # `mov byte` is acceptable.  Both indicate the local-byte write
    # path is reached.
    assert "[ebp-" in body, f"expected ebp-relative store in:\n{body}"


def test_mixed_init_rejected(*, work: Path) -> None:
    """`{ 0, .x = 1 }` is rejected with a clear message."""
    compile_expect_fail(
        message_fragment="positional struct initializers not supported",
        name="mixed_init",
        source=(
            "struct foo { uint8_t a; };\n"
            "int main() { struct foo c = { 0, .a = 1 }; return 0; }\n"
        ),
        work=work,
    )


def test_positional_init_rejected(*, work: Path) -> None:
    """`{ 1 }` (non-zero positional) is rejected."""
    compile_expect_fail(
        message_fragment="positional struct initializers not supported",
        name="positional_init",
        source=(
            "struct foo { uint8_t a; };\n"
            "int main() { struct foo c = { 1 }; return 0; }\n"
        ),
        work=work,
    )


def test_sizeof_local_struct(*, work: Path) -> None:
    """sizeof(local_struct) matches struct_sizes[tag]."""
    asm = compile_snippet(
        name="sizeof_local",
        source=(
            "struct two_byte { uint8_t a; uint8_t b; };\n"
            "int main() {\n"
            "    struct two_byte c;\n"
            "    return sizeof(c);\n"
            "}\n"
        ),
        work=work,
    )
    body = asm.split("main:", 1)[1].split("\n_", 1)[0].lower()
    assert "mov eax, 2" in body or "mov ax, 2" in body, (
        f"expected sizeof to yield 2 in:\n{body}"
    )


def test_zero_init_emits_byte_store(*, work: Path) -> None:
    """`= { 0 }` emits a mov byte zero-store for each byte of the slot."""
    asm = compile_snippet(
        name="zero_init",
        source=(
            "struct two_byte { uint8_t a; uint8_t b; };\n"
            "int main() { struct two_byte c = { 0 }; return c.a; }\n"
        ),
        work=work,
    )
    body = asm.split("main:", 1)[1].split("\n_", 1)[0].lower()
    # Two zero-stores: one per byte of the struct.
    mov_byte_zero = body.count("mov byte [ebp")
    assert mov_byte_zero == 2 or mov_byte_zero == 1, (
        f"expected 1 or 2 zero-stores (collapse may fire); saw "
        f"{mov_byte_zero} in:\n{body}"
    )
```

Plus the standard test-driver scaffolding (compile_snippet,
compile_expect_fail, main, TESTS tuple, sys.exit at bottom) following
the `tests/test_cc_bitfields.py` style.

Strict alphabetical ordering of functions: `compile_expect_fail` <
`compile_snippet` < `main` < `test_*` (each test alphabetical).

- [ ] **Step 2: Run the suite**

```bash
chmod +x tests/test_cc_local_structs.py
tests/test_cc_local_structs.py
```

Expected: `9 passed, 0 failed`.

- [ ] **Step 3: Add to CI matrix**

In `.github/workflows/test.yml`, add `test_cc_local_structs` to the
matrix alphabetically (between `test_cc_compatibility` and
`test_cc_bits` or wherever sort puts it).

- [ ] **Step 4: Commit**

```bash
git add tests/test_cc_local_structs.py .github/workflows/test.yml
git commit -m "tests: add cc.py stack-local struct coverage"
```

### Task D.2: Optional — rewrite ne2k.c init to use stack-local structs

**Files:**
- Modify: `src/drivers/ne2k.c`

Optional task that demonstrates the new pattern and recovers kernel
size.  If skipped, leave for a follow-up PR.

- [ ] **Step 1: Convert one site**

Pick the `ne2k_init` Page 0 stop+abort CR write.  Current shape:

```c
raw = 0;
cr = (struct ne2k_cr *)&raw;
cr->stop = 1;
raw = raw | 0x20;  /* workaround from PR #428 */
kernel_outb(0x300, raw);
```

(Or after the EBX allocator fix lands, the natural struct form
without the manual OR.)

Convert to:

```c
struct ne2k_cr c = { .stop = 1, .rd = 4 };
kernel_outb(0x300, *(uint8_t *)&c);
```

- [ ] **Step 2: Measure kernel-size delta**

```bash
ls -la kernel.bin
# Run make_os.sh, note the new size, compare to PR-#428 baseline of 42072.
```

Document the delta in the PR description.

- [ ] **Step 3: Repeat for the other CR / RCR / TCR / DCR / IMR write
       sites in ne2k.c**

Each conversion is a small diff.  Land them in one commit or split
per register if the diff is noisy.

- [ ] **Step 4: Run NE2000 smoke tests**

```bash
./make_os.sh
tests/test_programs.py    # icmp/dns/ping cover NE2000
```

Zero regressions.

- [ ] **Step 5: Commit**

```bash
git add src/drivers/ne2k.c
git commit -m "drivers(ne2k): rewrite init/probe/send with stack-local CR structs"
```

### Task D.3: Changelog + PR

**Files:**
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Add a changelog entry**

Under Unreleased:

```markdown
- **cc.py: stack-local struct values + designated init + bitfield
  constant folding.**  `struct foo c;` and `struct foo arr[N];` are
  now valid local declarations.  Dot-access read/write, address-of,
  sizeof, and indexed access (`arr[i].field`) work for locals exactly
  as they do for file-scope globals.  Two new initializer forms:
  `struct foo c = { 0 };` (zero-init) and `struct foo c = { .field
  = X };` (designated; omitted fields zero).  A const-fold peephole
  collapses bitfield-write sequences into a single `mov byte` when
  the storage is a known local and every value is literal; a paired
  collapse peephole replaces the prior `mov byte` at the same slot
  rather than emitting redundant stores.  Net effect: hardware-
  register init patterns like `struct ne2k_cr c = { .start = 1, .rd
  = 2, .page = 1 }; outb(p, *(uint8_t *)&c);` emit one `mov byte
  [ebp-N], <imm>` plus the byte load.
```

- [ ] **Step 2: Push and open PR**

```bash
git add docs/CHANGELOG.md
git commit -m "docs(changelog): cc.py stack-local structs + const-fold"
git push -u origin $(git branch --show-current)
gh pr create --title "feat(cc): stack-local struct values + designated init + bitfield const-fold" --body "..."
```

PR body: link the design + plan from design-specs.  Include the kernel-
size measurement from Task D.2 (delta vs. PR #428 baseline).

---

## Self-review

**Spec coverage:**

- Stack-local struct value declarations → Tasks B.1.
- Local struct arrays → Task B.5.
- Dot-access read/write → Tasks B.2, B.3.
- Address-of on locals → Task B.4.
- Sizeof on locals → covered by existing `_type_size` machinery; the
  `tests/test_cc_local_structs.py` `test_sizeof_local_struct` verifies.
- `{ 0 }` and designated init → Tasks A.1, A.2, B.6.
- Const-fold peephole → Tasks C.1, C.2, C.3.
- Collapse peephole → Task C.4.
- Tests → Task D.1.

**Placeholders:** None.  Every code-emitting step shows the code or
the regex/algorithm.  The one explicit "may skip" — Task C.3's AX-
literal tracking — has a documented fallback (skip it; const-fold
only fires for `_literal`, which still covers the driver case).

**Type consistency:** `known_local_bytes`, `_last_byte_store`,
`ax_literal`, `_parse_local_byte_addr`, `_emit_struct_initializer`,
`StructInitializer` are used consistently across tasks.

**Risk:** Task C.1 (emit pipeline regex hooks) is the highest-blast-
radius commit because misclassifying an emit could silently miscompile
drivers.  Full local CI matrix per the
`feedback_run_full_ci_matrix_locally` rule is non-optional after that
task and after Task C.4.
