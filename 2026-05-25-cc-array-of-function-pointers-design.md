# cc.py: array of function pointers

## Motivation

`user/libbboeos/stdlib.c` declares a file-scope array of function
pointers for the `atexit` registry:

```c
static void (*_atexit_fns[MAX_ATEXIT])(void);
```

cc.py cannot parse this declarator.  It also cannot call through
an indexed function pointer (`_atexit_fns[--_atexit_count]()`),
which stdlib.c needs to invoke the registered handlers at exit.
Three gaps block the file:

1. **Declaration** — the `void (*name[N])(params)` declarator
   syntax at file scope and local scope.
2. **Store** — `arr[i] = fn;` where the element type is a
   function pointer.
3. **Call through index** — `arr[i]()` calling the function
   pointer stored at element `i`.

---

## Feature 1: declaration

### Direct declarator syntax

Extend `parse_top_level_declaration` and
`parse_variable_declaration` to recognise the C declarator shape:

```
<return-type> ( * <identifier> [ <size> ] ) ( <param-list> )
```

When matched, emit:

```
ArrayDecl(type_name="function_pointer", name=identifier, size=size_node)
```

The function-pointer parameter metadata (the inner param-list) is
discarded, matching the existing scalar function-pointer local
pattern where cc.py tracks the `function_pointer` type marker but
not the callee's signature.

Both file-scope and local-scope arrays are supported.  File-scope
uninitialized arrays emit `resb N * int_size` (BSS).  File-scope
initialized arrays (e.g. `= { f1, f2 }`) emit `dd label1,
label2, ...` (function addresses as int-sized words).

### Typedef path

If `parse_type()` resolves a typedef alias that maps to
`"function_pointer"` (e.g. `typedef void (*handler)(void);`),
then `handler arr[N];` produces the same
`ArrayDecl(type_name="function_pointer", ...)`.  cc.py already
has function-pointer typedef aliases (commit `9d7c0543`); the
change is allowing the resolved `"function_pointer"` type to flow
into `ArrayDecl` where it was previously rejected or ignored.

### Codegen

`_type_size("function_pointer")` already returns `int_size` (4 on
32-bit, 2 on 16-bit).  No codegen change is needed for the
declaration itself — `ArrayDecl` with a known element size and an
optional initializer already works.  The only gap is ensuring that
an initializer list of function names (`{ f1, f2, ... }`) emits
each element as a label address (`dd _f1, _f2, ...`).

---

## Feature 2: store to indexed element

`_atexit_fns[_atexit_count++] = fn;` is an `IndexAssign` where
the RHS is a function-pointer value (a parameter or local typed
`"function_pointer"`).

The existing `IndexAssign` codegen computes the element address
as `base + index * stride` and stores `expr` into it.  The
stride for `"function_pointer"` elements is `int_size`, which
`_type_size` already returns.  The RHS (`fn`) is a `Var` whose
type is `"function_pointer"` — codegen resolves it to the
function's address.

**Expected to work with no codegen change** once the array is
declared correctly.  If it doesn't (e.g. the codegen rejects
`"function_pointer"` as an element type or the stride lookup
fails), fix the specific rejection site.

---

## Feature 3: call through indexed element

`_atexit_fns[--_atexit_count]()` calls the function pointer
stored at array element `--_atexit_count`.

### New AST node

`IndexedCall(array: Var, args: list[Node], index: Node)` — a
call whose callee is an array element, not a named variable.

### Parser

In `parse_primary`, after parsing `name[index]`, the parser
currently checks for `.`/`->` (member access) and `[` (double
index).  Add a new check: if the next token is `LPAREN`, parse
the argument list and emit `IndexedCall(array=Var(name),
index=index, args=args)`.

In `parse_statement`, the same pattern can appear as a
statement: `arr[i](args);`.  Detect `IDENT LBRACKET` followed
eventually by `RPAREN LPAREN` (or use the expression-statement
fallback if cc.py has one).  The simplest approach: when
`parse_statement` sees `IDENT LBRACKET`, speculatively parse as
an index expression, then check for `LPAREN` — if present,
parse as `IndexedCall` statement; otherwise fall through to the
existing `IndexAssign` / expression paths.

### Codegen

For `IndexedCall(array, index, args)`:

1. Push arguments right-to-left (cdecl).
2. Compute the callee address: load `array`, add
   `index * int_size`, load the function pointer from the
   resulting address into a scratch register.
3. `call <register>`.
4. Clean up the stack (caller pops args).

Save and restore pinned registers around the call, matching the
existing indirect-call pattern in `generate_call` for scalar
function pointers.

### IR builder

`IndexedCall` is an expression (it returns a value) and a
statement (can be used as a bare call).  Add cases in both
`_build_expr` and `_build_stmt`.  The simplest lowering is
`Block(node=...)` to delegate to the AST codegen.

---

## Test plan

- Compile `static void (*arr[8])(void);` at file scope — accept.
- Compile `void (*arr[4])(void) = { f1, f2, f3, f4 };` at file
  scope with initializer — accept.
- Compile `void (*arr[4])(void);` as a local — accept.
- Typedef path: `typedef void (*handler)(void); handler arr[8];`
  — accept.
- `arr[i] = fn;` — store a function pointer into an indexed
  element.
- `arr[i]();` — call through an indexed function pointer,
  verify correct `call` instruction.
- End-to-end: the atexit pattern from stdlib.c (declare, store,
  call-through in a loop).
- Full CI matrix.

## Out of scope

- Multi-dimensional arrays of function pointers.
- Function pointers returning non-void/non-int (cc.py tracks
  `"function_pointer"` as an opaque type — no return-type
  metadata).
- Calling through arbitrary pointer expressions (only indexed
  arrays of function pointers).
- Initialized local arrays of function pointers (only file-scope
  initialized arrays; locals are uninitialized or zero-filled).
