# cc.py: `va_arg(ap, double)` advance-by-8 + `sizeof(expression)`

## Motivation

Two small cc.py gaps each block one `user/libbboeos/` file:

- **`stdio.c`** uses `(void)va_arg(ap, double)` in the printf `%e`/
  `%f`/`%g` stub.  The parser already accepts `double` as a type
  token, but the codegen always advances the va-list cursor by
  `int_size` (4 bytes).  On i386 cdecl, a `double` argument occupies
  8 bytes on the stack, so `va_arg(ap, double)` must advance by 8.

- **`dirent.c`** uses `sizeof *directory` — sizeof applied to an
  expression rather than a type name or bare variable.  The parser
  today only handles `sizeof(T)` and `sizeof(var)`.

Both features are small, mechanically straightforward, and
independent.  They share a PR because neither justifies one alone.

---

## Feature A: `va_arg(ap, double)` — advance by 8

### Current state

The parser special-cases `__builtin_va_arg(ap, T)`: it parses `T`
via `parse_type()`, discards it, and emits
`Call(name="__builtin_va_arg", args=[ap])`.  Codegen
(`builtins.py:builtin___builtin_va_arg`) reads `*ap` into EAX and
advances `ap` by `int_size` (4).

### Change

**New AST node** `VaArg(cursor: Node, type_name: str)`.  Replaces
the `Call` that the parser currently emits for `__builtin_va_arg`.
Carrying the type string lets codegen pick the advance size.

**Parser** (`parse_primary`, the `__builtin_va_arg` branch).
Instead of discarding the parsed type, capture it and emit
`VaArg(cursor=ap_expr, type_name=type_string)`.

**Codegen** (`builtins.py`).  Rename the handler to match the new
node (or add a new `generate_expression` branch in `emission.py`
— whichever the codebase's dispatch style favours).  Compute the
advance size from the type:

- `"double"` → 8
- everything else → `int_size` (4 on 32-bit)

The loaded value stays in EAX.  For `double`, only the low 4 bytes
are accessible — this is correct because the only consumer today is
Doom's printf stub, which discards the value with `(void)`.

**IR builder.**  `_build_expr` needs a case for `VaArg` — the
simplest lowering is `Block(node=VaArg(...))` so the existing AST
codegen runs unchanged.

### Out of scope

- Loading a `double` into FP registers or producing a 64-bit value.
- `va_arg(ap, long long)` or other >4-byte integer types.
- Any change to how cc.py pushes variadic arguments at call sites.

---

## Feature B: `sizeof(expression)`

### Current state

`parse_sizeof` handles two forms:

- `sizeof(T)` where `T` is a type → `SizeofType(type_name=T)`.
- `sizeof(var)` where `var` is an identifier → `SizeofVar(name=var)`.

Both forms require parentheses.  `SizeofType` codegen calls
`_type_size(type_name)`.  `SizeofVar` codegen looks up the
variable's type and dispatches: arrays get element-count × stride,
structs get `struct_sizes[tag]`, everything else gets `int_size`.

### Change

**New AST node** `SizeofExpr(expression: Node)`.  The parser emits
this when the sizeof operand is a full expression (not a type and
not a bare identifier).

**Parser** (`parse_sizeof`).  Extend the fallback branch: when the
first token after `(` is not a type-start AND the tokens don't form
a bare `IDENT RPAREN`, parse a full expression via
`self.parse_expression()`, eat `RPAREN`, and emit
`SizeofExpr(expression=node)`.  The expression is never evaluated
at runtime — it exists only so codegen can infer its type.

Also accept the **unparenthesised** form: `sizeof *p`, `sizeof p`,
etc.  Standard C allows `sizeof unary-expression` without parens.
When the token after `SIZEOF` is not `LPAREN`, parse a unary
expression and emit `SizeofExpr`.  The existing `SizeofVar` path
(bare identifier in parens) is kept for backwards compatibility and
because its codegen handles arrays specially (element count × stride)
— that logic doesn't apply to the expression form.

**Codegen** — new helper `_expression_type(node: Node) -> str` on
the emission/generator mixin.  Walks the AST node structurally:

| Expression form | Inferred type |
|---|---|
| `Var(name)` | `self.variable_types[name]` |
| `Int` / `Char` | `"int"` / `"int"` (C promotes char in sizeof context — actually `"char"` for sizeof, since `sizeof(char)` is 1; but cc.py's existing `_type_size` handles both) |
| `String` | `"char *"` |
| `Index(array, index)` | pointee type of array's type (strip one `*`) |
| `MemberAccess` / `IndexMemberAccess` | field's declared type from struct layout |
| `Cast(target_type, expr)` | `target_type` |
| `AddressOf(var)` | var's type + `" *"` |
| `Call(name, args)` | function's return type |
| `PointerDereference` | strip one `*` from the pointer's type |
| `BinaryOperation` | `"int"` |
| `SizeofType` / `SizeofVar` / `SizeofExpr` | `"int"` (sizeof yields `size_t`, which is `int` on cc.py's 32-bit target) |
| `AssignExpr` | type of the inner assignment's lvalue |

Anything not in the table raises `CompileError("cannot determine
type of expression for sizeof")`.

The `SizeofExpr` codegen branch calls `_expression_type(expr)` to
get the type string, then delegates to `_type_size(type_string)` —
reusing the same path as `SizeofType`.

**IR builder.**  `_build_expr` needs a case for `SizeofExpr` — the
simplest lowering is `Block(node=SizeofExpr(...))`.

### Interaction with `SizeofVar`

`SizeofVar` is NOT retired.  It handles the `sizeof(array_name)`
idiom where the result is the full array size (element-count ×
stride), not a pointer size.  If the parser instead emitted
`SizeofExpr(Var("array_name"))`, codegen would infer the variable's
type as a pointer (arrays decay) and return pointer-size, which is
wrong.  So the bare-IDENT-in-parens path keeps producing
`SizeofVar` and only the expression path produces `SizeofExpr`.

### Out of scope

- `sizeof` on VLA or runtime-computed sizes.
- `_Alignof` / `_Alignas`.
- Changing how `SizeofVar` works for non-expression operands.

---

## Test plan

### Feature A tests

- Compile `va_arg(ap, double)` in a variadic function; verify the
  emitted asm contains `add <reg>, 8` (not 4).
- Compile `va_arg(ap, int)` in the same function; verify `add
  <reg>, 4` (regression guard).
- Compile `va_arg(ap, unsigned int)` — still 4.

### Feature B tests

- `sizeof *p` where `p` is `int *` → expect 4.
- `sizeof *p` where `p` is `char *` → expect 1.
- `sizeof *p` where `p` is `struct S *` → expect struct size.
- `sizeof(p[0])` — same as `sizeof *p`.
- `sizeof(p->field)` — field's type size.
- `sizeof(s.field)` — same via dot access.
- `sizeof((int *)0)` — pointer size (4).
- `sizeof(1 + 2)` — int size (4).
- Existing `sizeof(T)` and `sizeof(var)` tests still pass
  (regression).
- `sizeof(array)` where `array` is `int a[10]` — must still return
  40 (via `SizeofVar`), not 4.

### Integration

- Re-attempt `cc.py` compilation of `user/libbboeos/dirent.c` and
  `user/libbboeos/stdio.c` to confirm the blockers are resolved (or
  identify the next blocker if one remains).
- Full CI matrix.
