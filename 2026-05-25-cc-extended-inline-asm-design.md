# cc.py: GCC extended inline asm

## Motivation

Two remaining `user/libbboeos/` files are blocked by the lack of
GCC extended inline asm support:

- **`signal.c`** uses `__asm__ volatile("..." : "=a"(out) :
  [name] "g"(in) : "ebx", "ecx")` for syscall wrappers.
- **`math.c`** uses extended asm with x87 FP constraints (`"=t"`,
  `"u"`, `"0"`) for transcendental math functions.

cc.py today only handles `asm("string");` at file scope — a raw
string emitted verbatim.  Statement-level extended asm with
output/input/clobber operand sections is entirely unsupported.

---

## New AST node: `ExtendedAsm`

```python
@dataclass(kw_only=True, slots=True)
class AsmOperand(Node):
    """A single output or input operand in an extended asm statement."""

    constraint: str       # "=a", "=&q", "+b", "g", "0", "=t", "u", "=m", ...
    expression: Node      # The C lvalue/expression bound to this operand
    name: str | None      # Optional [name] for named-operand syntax

@dataclass(kw_only=True, slots=True)
class ExtendedAsm(Node):
    """Statement-level GCC extended inline asm."""

    clobbers: list[str]
    inputs: list[AsmOperand]
    is_volatile: bool
    outputs: list[AsmOperand]
    template: str
```

The existing `InlineAsm(content: str)` is kept for file-scope
`asm("...");` directives.

---

## Parser

Recognise `asm volatile("..." : outs : ins : clobbers);` and
the `__asm__ volatile(...)` spelling in statement context.

Grammar:

```
asm-statement:
    ("asm" | "__asm__") ["volatile"] "(" template
        [ ":" output-operands ]
        [ ":" input-operands ]
        [ ":" clobber-list ]
    ")" ";"

template:
    string-literal [ string-literal ... ]   (adjacent concatenation)

output-operands / input-operands:
    operand [ "," operand ... ]

operand:
    [ "[" IDENT "]" ] string-literal "(" expression ")"

clobber-list:
    string-literal [ "," string-literal ... ]
```

Each colon-separated section is optional — trailing sections can
be omitted.  An empty section between two colons (`: :`) means
"no operands in this section".

The `volatile` keyword is accepted and stored but is a no-op for
cc.py (no instruction reordering).

---

## Codegen

### Overview

The codegen for `ExtendedAsm`:

1. **Pre-template:** load inputs and set up read-modify-write
   registers.
2. **Template emission:** substitute operand references, emit.
3. **Post-template:** store outputs to their C variables.
4. **Clobber handling:** invalidate tracking for clobbered
   registers.

### Constraint handling

| Constraint | Direction | Pre-template | Substitution | Post-template |
|---|---|---|---|---|
| `"=a"` / `"=b"` / `"=c"` / `"=d"` | Output | — | Register name (`eax`, ...) | `mov [var], reg` |
| `"+a"` / `"+b"` / `"+c"` / `"+d"` | In+Out | `mov reg, [var]` | Register name | `mov [var], reg` |
| `"=&q"` / `"=&qm"` | Output (early-clobber) | — | Byte register (`al`/`cl`/`dl`) or memory | `movzx eax, reg; mov [var], eax` |
| `"g"` | Input | — | Memory operand `[ebp-N]` / `[_g_name]` or immediate | — |
| `"0"` | Input (tied) | Same reg as output 0 | Same as output 0 | — |
| `"=t"` | Output (x87 ST0) | — | Not substituted (implicit) | `fstp [var]` (qword) |
| `"u"` | Input (x87 ST1) | `fld [var]` | Not substituted (implicit) | — |
| `"=m"` | Output (memory) | — | Memory operand `[var]` | — (template wrote directly) |

### Operand substitution

The template string uses these patterns to reference operands:

| Pattern | Replacement |
|---|---|
| `%[name]` | Named operand's location |
| `%N` | Positional operand N's location (outputs numbered first, then inputs) |
| `%b[name]` / `%bN` | Byte sub-register of the operand |
| `%%` | Literal `%` |

GCC numbers operands sequentially: outputs first (0, 1, 2, …),
then inputs continue the sequence.  A `"0"` constraint in an
input means "tied to output operand 0" — the input shares the
same register.

### x87 FP constraints

cc.py does NOT track x87 stack state.  The codegen trusts the
template to manage the FP stack correctly.  Its only jobs:

- **`"u"` (input in ST1):** emit `fld qword [var]` before the
  template to push the value onto the FP stack.  If `"0"` (tied
  to output) also appears, it expects the value already in ST0 —
  emit `fld qword [var]` for it first so the `"u"` push lands
  in ST1.
- **`"=t"` (output from ST0):** emit `fstp qword [var]` after the
  template to pop ST0 into the variable.
- **`"st(1)"` clobber:** no action (cc.py doesn't track FP stack).

The x87 loads/stores use `qword` (8-byte double) since all
math.c functions operate on `double`.

### Clobber handling

For each register in the clobber list:
- If the register is currently pinned to a variable by the
  register allocator, save it before the asm block and restore
  after.
- Clear any AX-tracking metadata (`ax_clear()`) if `"eax"` or
  `"ax"` is clobbered (or if any output writes EAX).
- `"cc"` (condition codes) — no action for cc.py.

### Read-modify-write (`+` prefix)

A `"+a"` operand means: load the variable into EAX before the
template, and store EAX back into the variable after.  It counts
as both an output and an input in the operand numbering (occupies
one slot, but appears in the output list with the `+` stripped to
`=` for substitution purposes).

---

## IR builder

`ExtendedAsm` is lowered via `Block(node=stmt)` — the AST
codegen handles it directly.  No IR-level decomposition.

---

## Test plan

- Parse + emit `signal.c`'s `alarm_ms` pattern: named inputs,
  `"=a"` output, clobbers.
- Parse + emit `signal.c`'s `signal` pattern: `"=&q"`
  early-clobber, `%b[name]` byte sub-register substitution.
- Parse + emit `syscall.c`'s read-modify-write pattern: `"+a"`,
  `"+b"`, `"+c"`, `"+d"`.
- Parse + emit `math.c`'s `cos` pattern: `"=t"` output, `"0"`
  tied input.
- Parse + emit `math.c`'s `atan2` pattern: `"=t"` output, `"u"`
  ST(1) input.
- Parse + emit `math.c`'s `fnstcw` pattern: `"=m"` memory output.
- Verify operand substitution produces correct register/memory
  names in the emitted asm.
- Full CI matrix.

## Out of scope

- `%h` (high byte), `%w` (word) sub-register modifiers — not used.
- `"r"` constraint (any GP register) — `"g"` covers the cases.
- `"i"` constraint (immediate only) — `"g"` subsumes it.
- Goto labels in asm.
- Asm expressions (only asm statements).
- Float type keyword or x87 arithmetic codegen (only the asm
  constraint interface for loading/storing FP values).
