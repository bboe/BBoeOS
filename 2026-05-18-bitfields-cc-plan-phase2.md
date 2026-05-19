# cc.py Bitfields Implementation Plan — Phase 2 (revised)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Supersedes the Phase 2 section of
[2026-05-18-bitfields-cc-plan.md](./2026-05-18-bitfields-cc-plan.md).
Phase 1 (cast expressions) shipped in PR #422; Phase 3 (driver
conversions) is unchanged.  Reconnaissance into cc.py's actual struct
layout machinery revealed that the v1 Phase 2 pseudocode assumed a
data structure shape that doesn't match the real code — the layout is
a flat `dict[str, tuple[int, int, int]]`, not a `StructLayout`
dataclass, and four call sites unpack the tuple by position.  This
revision adds an explicit data-structure migration task before
bitfield-specific codegen lands.

**Goal:** Add `uint8_t` bitfield struct members to cc.py — declarative
hardware-register layouts via `struct foo { uint8_t name : N; };`
with anonymous padding (`uint8_t : N;`), LSB-first, runs sum to ≤ 8.

**Architecture:** Three concerns layered cleanly:

1. **AST** — extend `StructField` with `bit_width` (None for regular).
2. **Layout** — migrate `struct_layouts` from a positional tuple to a
   `FieldInfo` namedtuple with named attributes; add bit-aware
   packing logic that handles consecutive bitfields as runs.
3. **Codegen** — bitfield-aware paths in `generate_member_access`
   (read) and `generate_member_assign` (write), and an explicit
   rejection of `&bitfield`.

**Spec:** [2026-05-18-bitfields-cc-design.md](./2026-05-18-bitfields-cc-design.md)

---

## Pre-flight: reconnaissance notes

These are facts established by reading cc.py at HEAD of `bboe/cc-casts`
(or wherever Phase 1 lands).  Implementer subagents should treat them
as ground truth — no need to re-research:

- **`StructField`** in `cc/ast_nodes.py`: `field_name: str`,
  `type_name: str`.  No bit_width yet.

- **Struct parser** in `cc/parser.py`, function
  `_parse_struct_declaration` starting around line 359.  After
  consuming the type, it eats either an `LPAREN` (function-pointer
  field), an `IDENT` (regular field name), and then optionally a
  `LBRACKET ... RBRACKET` for arrays.  A `COLON` is not yet
  recognised in this position.

- **`COLON`** is already a token kind in `cc/tokens.py` (used by enum
  init and labeled statements).  No lexer changes needed.

- **`struct_layouts`** in `cc/codegen/x86/generator.py:232` is typed
  as `dict[str, dict[str, tuple[int, int, int]]]` — i.e.
  `{tag: {field_name: (byte_offset, field_size, element_size)}}`.

- **Layout builder** lives in
  `cc/codegen/x86/generator.py:1826-1849` inside `_register_globals`.
  Walks `declaration.fields`, computes `field_size` via
  `_type_size`, accumulates `cursor`.  Element-size handling already
  copes with array fields (`uint8_t ip[4]`).

- **`_type_size`** for struct types
  (`cc/codegen/x86/generator.py:801-806`):
  `sum(field_size for _, field_size, _element_size in self.struct_layouts[tag].values())`.
  This sum will be wrong for bitfields (each bit-field has its own
  layout entry but the storage byte is shared) — fix by tracking
  struct size separately.

- **Four positional unpack sites** of the layout tuple, all in
  `cc/codegen/x86/generator.py`:
  - `:420` — `_type_size`-adjacent helper
  - `:429` — another `_type_size`-adjacent helper
  - `:716` — array stride calculation
  - `:806` — the `_type_size` struct branch noted above
  - `:862`, `:898`, `:1125`, `:1163`, `:1203`, `:1233` —
    `generate_member_access`, `generate_member_assign`,
    `_resolve_struct_element`, and the indexed-member variants

- **Member access codegen** at `:829` (read) and `:938`
  (`generate_member_assign`, write).  Both branch on `field_size in
  (1, 2, 4)` and emit byte / word / dword loads.  These are the two
  sites that need bitfield-aware logic.

- **AddressOf codegen**: search for `AddressOf` handler in
  `cc/codegen/x86/generator.py` or `cc/codegen/x86/emission.py` for
  the rejection point.

---

## Phase 2 tasks

### Task 2.1: Extend `StructField` with `bit_width`; allow anonymous

**Files:**
- Modify: `cc/ast_nodes.py`

- [ ] **Step 1: Update the dataclass**

Replace the existing `StructField` class with:

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

Field ordering: alphabetical (`bit_width` < `field_name` <
`type_name`).  `field_name` becomes `str | None` to admit anonymous
bitfields.

- [ ] **Step 2: Run cc.py unit suites**

```bash
cd /home/ubuntu/bboeos/.claude/worktrees/n2
tests/test_cc_compatibility.py
tests/test_cc_bits.py
tests/test_cc_casts.py
```

Expected: 0 failures, same pass counts as before.  No parser or
codegen consumes `bit_width` yet, so existing structs (all of which
use `bit_width=None` implicitly) keep working.

- [ ] **Step 3: Commit**

```bash
git add cc/ast_nodes.py
git commit -m "cc(ast): add bit_width to StructField; allow anonymous"
```

### Task 2.2: Parser — accept `: N` and anonymous bitfields

**Files:**
- Modify: `cc/parser.py` (`_parse_struct_declaration`, around line 359)

- [ ] **Step 1: Replace the per-field parse loop body**

The current loop unconditionally eats an IDENT after the type.  Two
new shapes need support:

- `uint8_t name : N;` — `IDENT` then `COLON NUMBER`
- `uint8_t : N;` — `COLON NUMBER` directly after the type

Replace the body of the `while self.peek()[0] != "RBRACE":` loop
(everything from `field_type = self.parse_type()` through
`fields.append(...)`) with:

```python
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
        self.eat("COLON")
        bit_width = int(self.eat("NUMBER")[1])
if bit_width is None and self.peek()[0] == "LBRACKET":
    self.eat("LBRACKET")
    count_token = self.eat("NUMBER")
    self.eat("RBRACKET")
    field_type = f"{field_type}[{count_token[1]}]"
if bit_width is not None:
    if field_type != "uint8_t":
        message = (
            f"bitfield container must be uint8_t "
            f"(got {field_type!r}) at line {line}"
        )
        raise SyntaxError(message)
    if not 1 <= bit_width <= 8:
        message = (
            f"bitfield width must be 1..8 (got {bit_width}) at line {line}"
        )
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

- [ ] **Step 2: Smoke test parsing**

```bash
cat > /tmp/bf_smoke.c <<'EOF'
struct flags {
    uint8_t a : 1;
    uint8_t b : 1;
    uint8_t : 2;
    uint8_t c : 4;
};

int main() { return 0; }
EOF
python3 cc.py --bits 32 /tmp/bf_smoke.c /tmp/bf_smoke.asm
echo "exit=$?"
```

Expected: exit 0 (parses; no struct usage so codegen doesn't trip
yet).

- [ ] **Step 3: Negative-case smokes**

```bash
echo 'struct bad { uint16_t a : 4; }; int main() { return 0; }' > /tmp/bf_bad1.c
python3 cc.py --bits 32 /tmp/bf_bad1.c /tmp/x.asm; echo "exit=$?"

echo 'struct bad { uint8_t a : 9; }; int main() { return 0; }' > /tmp/bf_bad2.c
python3 cc.py --bits 32 /tmp/bf_bad2.c /tmp/x.asm; echo "exit=$?"
```

Expected: both non-zero with the parser error messages.

- [ ] **Step 4: Regression suites**

```bash
tests/test_cc_compatibility.py
tests/test_cc_bits.py
tests/test_cc_casts.py
```

All zero failures.

- [ ] **Step 5: Commit**

```bash
git add cc/parser.py
git commit -m "cc(parser): accept uint8_t bitfields in struct decls"
```

### Task 2.3: Migrate `struct_layouts` to `FieldInfo` namedtuple

**Files:**
- Modify: `cc/codegen/x86/generator.py`

This task is a pure refactor — no bitfield logic yet, no behavior
change.  It replaces the positional `(byte_offset, field_size,
element_size)` tuple with a `FieldInfo` namedtuple so adding
bit-related attrs in Task 2.4 doesn't require touching every unpack
site.

- [ ] **Step 1: Define the namedtuple**

At the top of `cc/codegen/x86/generator.py` (after imports, before
the class), add:

```python
class FieldInfo(NamedTuple):
    """One struct field's layout.

    ``bit_offset`` and ``bit_width`` are populated for bitfield
    members (currently always ``None`` until Task 2.4 lands the
    bitfield-aware layout builder).  ``byte_offset`` is the field's
    start byte within the struct; ``field_size`` is the field's
    total byte size (``element_size * count`` for array fields,
    ``element_size`` for scalar fields).
    """

    bit_offset: int | None
    bit_width: int | None
    byte_offset: int
    element_size: int
    field_size: int
```

(Attributes alphabetical.)  Import `NamedTuple` from `typing` at the
top of the file if not already imported.

- [ ] **Step 2: Update the type annotation on `self.struct_layouts`**

Change `:232` to:

```python
self.struct_layouts: dict[str, dict[str, FieldInfo]] = {}
```

- [ ] **Step 3: Update the layout builder**

In `_register_globals` at `:1826-1849`, change the inner-loop body
from:

```python
layout: dict[str, tuple[int, int, int]] = {}
cursor = 0
for field in declaration.fields:
    ftype = field.type_name
    if "[" in ftype:
        bracket = ftype.index("[")
        element_type = ftype[:bracket]
        count = int(ftype[bracket + 1 : -1])
        element_size = self._type_size(element_type)
        field_size = element_size * count
    else:
        field_size = self._type_size(ftype)
        element_size = field_size
    layout[field.field_name] = (cursor, field_size, element_size)
    cursor += field_size
self.struct_layouts[declaration.name] = layout
```

to:

```python
layout: dict[str, FieldInfo] = {}
cursor = 0
for field in declaration.fields:
    ftype = field.type_name
    if "[" in ftype:
        bracket = ftype.index("[")
        element_type = ftype[:bracket]
        count = int(ftype[bracket + 1 : -1])
        element_size = self._type_size(element_type)
        field_size = element_size * count
    else:
        field_size = self._type_size(ftype)
        element_size = field_size
    layout[field.field_name] = FieldInfo(
        bit_offset=None,
        bit_width=None,
        byte_offset=cursor,
        element_size=element_size,
        field_size=field_size,
    )
    cursor += field_size
self.struct_layouts[declaration.name] = layout
```

Bitfield-specific packing (consecutive-field bit_offset tracking,
run-end detection, anonymous-field handling) lands in Task 2.4.
Right now this builder is still bitfield-blind; if the parser hands
it a bitfield, it will treat each as a 1-byte regular field — wrong,
but harmless because no consumer is bitfield-aware yet and we'll add
the proper packing logic before any test exercises a bitfield struct
codegen-side.

- [ ] **Step 4: Update the four positional-unpack sites**

For each of these sites in `cc/codegen/x86/generator.py`, replace
the positional unpack with attribute access:

- **`:420`**, **`:429`**, **`:806`** (sizeof-related):

  Old: `sum(field_size for _, field_size, _element_size in self.struct_layouts[tag].values())`

  New: `sum(info.field_size for info in self.struct_layouts[tag].values())`

  This stays wrong for bitfields (it'll double-count packed bytes)
  but matches the pre-refactor behavior bit-for-bit.  Task 2.4 will
  fix `_type_size` for struct types to use a separate
  `self.struct_sizes` dict.

- **`:716`**:

  Old likely: `offset, field_size, _ = layout[name]` or similar
  destructure.

  New: capture the `FieldInfo` and use `.byte_offset` /
  `.field_size` attributes.

- **`:862`** (inside `generate_member_access`'s dot path):

  Old: `offset, field_size, element_size = layout[expression.member_name]`

  New:
  ```python
  info = layout[expression.member_name]
  offset = info.byte_offset
  field_size = info.field_size
  element_size = info.element_size
  ```

  (Or refactor to use `info.byte_offset` etc. directly throughout the
  function body.  Either way, the runtime behavior is unchanged.)

- **`:898`** (inside `generate_member_access`'s arrow path):

  Same transformation as `:862`.

- **`:1125`**, **`:1163`**, **`:1203`**, **`:1233`** (indexed-member
  variants in `_resolve_struct_element` and friends):

  Same destructure pattern; replace with attribute access.

Use `grep -n "byte_offset\|element_size\|field_size" cc/codegen/x86/generator.py`
to spot-check that every site is migrated.

- [ ] **Step 5: Build and run the full local matrix**

```bash
cd /home/ubuntu/bboeos/.claude/worktrees/n2
./make_os.sh                           # full kernel + userland build
tests/test_cc_compatibility.py
tests/test_cc_bits.py
tests/test_cc_casts.py
tests/test_asm.py
tests/test_bboefs.py
tests/test_programs.py
```

Every suite must report zero failures.  This is a pure refactor;
any regression here means the migration missed a site or got an
attribute name wrong.

- [ ] **Step 6: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): migrate struct_layouts to FieldInfo namedtuple"
```

### Task 2.4: Layout — bitfield-aware packing and `struct_sizes`

**Files:**
- Modify: `cc/codegen/x86/generator.py`

- [ ] **Step 1: Add `self.struct_sizes`**

Next to `self.struct_layouts` at `:232`, add:

```python
self.struct_sizes: dict[str, int] = {}
```

This dict mirrors `struct_layouts` and holds each struct's total
byte size including bitfield-run packing.  `_type_size` for struct
types will use this instead of summing field sizes.

- [ ] **Step 2: Rewrite the layout builder for bitfield-aware packing**

Replace the inner-loop body from Task 2.3 with:

```python
layout: dict[str, FieldInfo] = {}
cursor = 0
run_bits = 0  # bits already consumed in the current bitfield run
for field in declaration.fields:
    if field.bit_width is not None:
        if run_bits + field.bit_width > 8:
            message = (
                f"bitfield run exceeds 8 bits in struct '{declaration.name}' "
                f"at line {field.line}"
            )
            raise CompileError(message, line=field.line)
        if field.field_name is not None:
            layout[field.field_name] = FieldInfo(
                bit_offset=run_bits,
                bit_width=field.bit_width,
                byte_offset=cursor,
                element_size=1,
                field_size=1,
            )
        run_bits += field.bit_width
        continue
    # Regular field: close any open bitfield run.
    if run_bits > 0:
        cursor += 1
        run_bits = 0
    ftype = field.type_name
    if "[" in ftype:
        bracket = ftype.index("[")
        element_type = ftype[:bracket]
        count = int(ftype[bracket + 1 : -1])
        element_size = self._type_size(element_type)
        field_size = element_size * count
    else:
        field_size = self._type_size(ftype)
        element_size = field_size
    layout[field.field_name] = FieldInfo(
        bit_offset=None,
        bit_width=None,
        byte_offset=cursor,
        element_size=element_size,
        field_size=field_size,
    )
    cursor += field_size
if run_bits > 0:
    cursor += 1
self.struct_layouts[declaration.name] = layout
self.struct_sizes[declaration.name] = cursor
```

Notes:
- Anonymous bitfields (`field_name is None`) advance `run_bits` and
  consume bits in the run but never enter `layout` (the dict).
- A regular field after a bitfield run closes the run (`cursor += 1`)
  before that field's byte_offset is computed.
- The struct's total size is the final `cursor` value, including
  the trailing closing byte if any bitfields are in flight at end.

- [ ] **Step 3: Update `_type_size` for struct types**

In `_type_size` at `:801-808`, change the struct branch from:

```python
if type_name.startswith("struct "):
    tag = type_name[7:]
    if tag not in self.struct_layouts:
        message = f"unknown struct '{tag}'"
        raise CompileError(message)
    return sum(info.field_size for info in self.struct_layouts[tag].values())
```

to:

```python
if type_name.startswith("struct "):
    tag = type_name[7:]
    if tag not in self.struct_sizes:
        message = f"unknown struct '{tag}'"
        raise CompileError(message)
    return self.struct_sizes[tag]
```

Same change for any other site that sums `field_size` to compute
struct size (`:420`, `:429` if those were also struct-size paths —
verify by reading the surrounding code).

- [ ] **Step 4: Smoke test sizeof**

```bash
cat > /tmp/bf_size.c <<'EOF'
struct one_byte {
    uint8_t a : 1;
    uint8_t b : 1;
    uint8_t : 2;
    uint8_t c : 4;
};

struct two_byte {
    uint8_t a : 4;
    uint8_t : 4;
    uint8_t b;
};

int main() {
    return sizeof(struct one_byte) + sizeof(struct two_byte);
}
EOF
python3 cc.py --bits 32 /tmp/bf_size.c /tmp/bf_size.asm
grep -A2 "main:" /tmp/bf_size.asm | head
```

Expected: `main` returns `3` (one_byte = 1, two_byte = 2 because the
bitfield run is 1 byte and the regular `b` field is 1 more byte).
Inspect the emitted asm for an immediate `3` move into the accumulator.

- [ ] **Step 5: Full local matrix**

```bash
./make_os.sh
tests/test_cc_compatibility.py
tests/test_cc_bits.py
tests/test_cc_casts.py
tests/test_asm.py
tests/test_bboefs.py
tests/test_programs.py
```

Zero failures.  All pre-existing structs have no bitfields, so the
new code path is dormant for them.

- [ ] **Step 6: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): bitfield-aware struct layout + struct_sizes"
```

### Task 2.5: Codegen — bitfield read in `generate_member_access`

**Files:**
- Modify: `cc/codegen/x86/generator.py` (function `generate_member_access` at `:829`)

- [ ] **Step 1: Branch on `info.bit_width is not None`**

In `generate_member_access`, both the dot path (`:843-886`) and the
arrow path (`:887` onward) currently emit one of:

- byte load (`emit_byte_load_zx(addr)`)
- word load (`movzx ..., word ...`)
- dword load (`mov ..., ...`)

…based on `field_size`.  After the layout lookup yields `info`,
add a branch before the size-based dispatch:

```python
if info.bit_width is not None:
    # Bitfield read: load byte, shift, mask.
    self.emit(f"        mov al, {addr}")
    if info.bit_offset != 0:
        self.emit(f"        shr al, {info.bit_offset}")
    if info.bit_width != 8:
        self.emit(f"        and al, {(1 << info.bit_width) - 1}")
    self.emit(f"        movzx {self.target.acc}, al")
    self.ax_clear()
    return
```

`addr` is the same `[base_symbol+offset]` / `[base_reg+offset]`
expression the byte/word/dword loads use; reuse the existing
formatting.  Apply the same branch in both the dot-path and the
arrow-path.

- [ ] **Step 2: Smoke test bitfield read**

```bash
cat > /tmp/bf_read.c <<'EOF'
struct flags {
    uint8_t a : 1;
    uint8_t b : 1;
    uint8_t : 2;
    uint8_t c : 4;
};

int main() {
    struct flags *f;
    return f->c;
}
EOF
python3 cc.py --bits 32 /tmp/bf_read.c /tmp/bf_read.asm
grep -B1 -A6 "main:" /tmp/bf_read.asm
```

Expected: asm body contains `mov al, [...]`, `shr al, 4`, `and al,
15`, `movzx <reg>, al`.

- [ ] **Step 3: Full local matrix**

```bash
./make_os.sh
tests/test_cc_compatibility.py
tests/test_cc_bits.py
tests/test_cc_casts.py
tests/test_asm.py
tests/test_bboefs.py
tests/test_programs.py
```

Zero regressions.

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): bitfield read (shr/and on byte load)"
```

### Task 2.6: Codegen — bitfield write in `generate_member_assign`

**Files:**
- Modify: `cc/codegen/x86/generator.py` (function `generate_member_assign` at `:938`)

- [ ] **Step 1: Locate the write path**

`generate_member_assign` parallels `generate_member_access`: it has a
dot-path (file-scope struct global) and an arrow-path (struct
pointer).  Both unpack the `FieldInfo` and then emit a size-based
store.

- [ ] **Step 2: Add the bitfield branch**

Before the size-based dispatch, insert:

```python
if info.bit_width is not None:
    field_mask = ((1 << info.bit_width) - 1) << info.bit_offset
    clear_mask = (~field_mask) & 0xFF
    # Peephole: 1-bit field with a literal value.
    if (
        info.bit_width == 1
        and isinstance(value_expression, Int)
        and value_expression.value in (0, 1)
    ):
        if value_expression.value == 0:
            self.emit(f"        and byte {addr}, {clear_mask}")
        else:
            self.emit(f"        or byte {addr}, {field_mask}")
        return
    # General path: read-modify-write via AL/BL.
    self._emit_expression(value_expression)        # value in EAX
    self.emit("        mov bl, al")
    if info.bit_width != 8:
        self.emit(f"        and bl, {(1 << info.bit_width) - 1}")
    if info.bit_offset != 0:
        self.emit(f"        shl bl, {info.bit_offset}")
    self.emit(f"        mov al, {addr}")
    self.emit(f"        and al, {clear_mask}")
    self.emit("        or al, bl")
    self.emit(f"        mov {addr}, al")
    return
```

`addr` is the same memory operand the existing store path uses.
`value_expression` is whatever the surrounding function calls the
rhs node — reuse the existing variable name.  `self._emit_expression`
is the existing generator entry point for evaluating an expression
into the accumulator; verify the exact name in context.

- [ ] **Step 3: Smoke test bitfield write**

```bash
cat > /tmp/bf_write.c <<'EOF'
struct flags {
    uint8_t a : 1;
    uint8_t b : 1;
    uint8_t : 2;
    uint8_t c : 4;
};

int main() {
    struct flags *f;
    f->a = 1;
    f->c = 5;
    return 0;
}
EOF
python3 cc.py --bits 32 /tmp/bf_write.c /tmp/bf_write.asm
grep -B1 -A18 "main:" /tmp/bf_write.asm
```

Expected: for `f->a = 1`, an `or byte [...], 1` peephole.  For
`f->c = 5`, the general read-modify-write sequence with `and bl, 15`,
`shl bl, 4`, `and al, 15` (clear_mask=0x0F), `or al, bl`, store.

- [ ] **Step 4: Full local matrix**

```bash
./make_os.sh
tests/test_cc_compatibility.py
tests/test_cc_bits.py
tests/test_cc_casts.py
tests/test_asm.py
tests/test_bboefs.py
tests/test_programs.py
```

Zero regressions.

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "cc(codegen): bitfield write with 1-bit literal peephole"
```

### Task 2.7: Reject `&bitfield`

**Files:**
- Modify: wherever `AddressOf` is handled in `cc/codegen/x86/` (likely `generator.py` or `emission.py` — grep `AddressOf`)

- [ ] **Step 1: Locate the AddressOf emit path**

```bash
grep -n "AddressOf" cc/codegen/x86/*.py | head
```

- [ ] **Step 2: Reject bitfield targets**

In the AddressOf handler, after the inner expression is identified
as a `MemberAccess`, look up the field in the struct layout.  If
`info.bit_width is not None`, raise:

```python
if isinstance(inner, MemberAccess):
    # ... existing layout resolution ...
    if info.bit_width is not None:
        message = (
            f"cannot take address of bitfield "
            f"'{inner.member_name}' at line {inner.line}"
        )
        raise CompileError(message, line=inner.line)
```

Match the surrounding error-handling style (`CompileError` vs.
`SyntaxError` — likely `CompileError`).

- [ ] **Step 3: Negative smoke**

```bash
cat > /tmp/bf_addr.c <<'EOF'
struct flags { uint8_t a : 1; };
int main() {
    struct flags x;
    uint8_t *p = &x.a;
    return 0;
}
EOF
python3 cc.py --bits 32 /tmp/bf_addr.c /tmp/x.asm
echo "exit=$?"
```

Expected: non-zero exit with "cannot take address of bitfield 'a'".

- [ ] **Step 4: Full local matrix**

Same suite list as the prior tasks.  Zero regressions.

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/generator.py  # or emission.py, whichever
git commit -m "cc(codegen): reject &bitfield"
```

### Task 2.8: Bitfield tests

**Files:**
- Create: `tests/test_cc_bitfields.py`
- Modify: `.github/workflows/test.yml`

- [ ] **Step 1: Write the test driver**

Model on `tests/test_cc_casts.py` (the Phase 1 sibling).  Use
`compile_snippet` to run cc.py + nasm on each source, and a
`compile_expect_fail` helper to assert compile errors with a
message-fragment match for negative cases.

Required tests:

1. `test_read_1bit_at_offset_0` — `f.a` with `a:1` at bit 0; assert
   emitted asm contains `mov al, [...]` and `and al, 1` (no `shr`
   needed since bit_offset==0).
2. `test_read_4bit_at_offset_4` — `f.c` with `c:4` at bit 4; assert
   `shr al, 4` and (`and al, 15` or `and al, 0xf`).
3. `test_write_1bit_literal_1_peephole` — `f.a = 1`; assert `or
   byte` peephole.
4. `test_write_1bit_literal_0_peephole` — `f.a = 0`; assert `and
   byte ..., <clear_mask>` peephole.
5. `test_sizeof_packed_byte` — sizeof of a struct with 8 bits worth
   of bitfields is 1.
6. `test_sizeof_mixed_run` — sizeof of `{a:4; b;}` is 2.
7. `test_run_overflow_rejected` — `{a:4; b:5;}` fails with message
   containing "run exceeds 8 bits".
8. `test_non_uint8_container_rejected` — `uint16_t a:4` fails with
   "must be uint8_t".
9. `test_width_too_large_rejected` — `uint8_t a:9` fails with
   "width must be 1..8".
10. `test_addressof_bitfield_rejected` — `&x.a` on a bitfield fails
    with "cannot take address of bitfield".

Style: kw-only sorted args, no abbreviations, alphabetical function
ordering, `sys.exit(main())` at the bottom.

- [ ] **Step 2: Run the test**

```bash
chmod +x tests/test_cc_bitfields.py
tests/test_cc_bitfields.py
```

Expected: `10 passed, 0 failed`.

- [ ] **Step 3: Add to CI matrix**

In `.github/workflows/test.yml`, add `test_cc_bitfields` alphabetically
alongside the existing `test_cc_*` entries.

- [ ] **Step 4: Commit**

```bash
git add tests/test_cc_bitfields.py .github/workflows/test.yml
git commit -m "tests: add cc.py bitfield codegen + negative-case coverage"
```

### Task 2.9: Open the PR

**Files:**
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Add a changelog entry**

Under Unreleased:

```markdown
- cc.py: bitfield struct members.  ``uint8_t name : N;`` for named
  bits, ``uint8_t : N;`` for anonymous padding.  LSB-first, runs sum
  to <= 8 bits; ``uint8_t`` containers only.  Read emits ``shr``+``and``
  on a byte load; write emits a read-modify-write sequence (with a
  one-instruction peephole for literal 0/1 stores on 1-bit fields).
  ``&bitfield`` is a compile error.
```

- [ ] **Step 2: Push and open the PR**

```bash
git add docs/CHANGELOG.md
git commit -m "docs(changelog): cc.py bitfield struct members"
git push -u origin $(git branch --show-current)
gh pr create --title "feat(cc): uint8_t bitfield struct members" --body "..."
```

PR body: link the design + plan from design-specs; include the test
plan checklist with the full local suite results.

---

## Self-review

**Spec coverage:**
- Bitfield grammar (`uint8_t name : N;`, anonymous `uint8_t : N;`) →
  Task 2.2.
- Container restriction (uint8_t only) → Task 2.2 (parser error).
- Width restriction (1..8) → Task 2.2 (parser error).
- Run constraint (sum ≤ 8) → Task 2.4 (layout error).
- LSB-first ordering → Task 2.4 (`bit_offset = run_bits` starts at 0
  for first field in run).
- Anonymous-bitfield layout participation → Task 2.4 (advances
  `run_bits` without entering `layout`).
- `sizeof` accounting → Task 2.4 (`struct_sizes` populated; `_type_size`
  uses it).
- `&bitfield` rejection → Task 2.7.
- Bitfield read codegen → Task 2.5.
- Bitfield write codegen + peepholes → Task 2.6.

**Calibration vs. v1 plan:**
- v1 Task 2.3 ("byte offset + bit offset table") was pseudocode
  against a non-existent `StructLayout` dataclass.  Split into
  v2 Task 2.3 (pure data-structure migration to `FieldInfo`
  namedtuple, no behavior change) and v2 Task 2.4 (bitfield-aware
  packing + `struct_sizes`).  This isolates the data-structure
  change as a single bisectable refactor commit.
- v1 Task 2.5 / 2.6 (read / write codegen) had pseudocode that
  assumed an `_emit` method and a `value_expr` variable name.  v2
  notes the actual `generate_member_access` / `generate_member_assign`
  entry points with line numbers and reuses the existing `addr`
  variable that the size-based dispatch already builds.
- v1 Task 2.8 ("add `bboeos.h` shadow declarations") was a no-op
  because clang accepts bitfields as written.  Dropped from v2.

**Placeholders:** None.  Every code-emitting step shows the code.
The negative-case error messages are spelled out so the tests in
Task 2.8 can match on them.

**Type consistency:** `FieldInfo` attribute names (`bit_offset`,
`bit_width`, `byte_offset`, `element_size`, `field_size`) are used
consistently across Tasks 2.3, 2.4, 2.5, 2.6.  `struct_sizes` is
defined in Task 2.4 and used by `_type_size` in the same task.

**Risk:** Task 2.3 is the highest-blast-radius commit (touches every
struct-layout consumer in cc.py).  Run the full local matrix before
commit; if anything fails, the refactor missed a site or got an
attribute name wrong.  Per the `feedback_run_full_ci_matrix_locally`
rule, this is non-optional.
