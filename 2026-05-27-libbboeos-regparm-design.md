# Regparm calling convention for libbboeos exports

## Problem

cc.py compiles libbboeos source files (string.c, stdio.c, etc.) with regparm and
register-convention optimizations disabled because the function pointer table
(`FUNCTION_*_PTR`) uses a flat cdecl ABI.  Both call sites (`call
[FUNCTION_STRLEN_PTR]`) and bodies load/store args via the stack, missing the
register-passing optimization cc.py applies to all other intra-program calls.

The regparm convention is deterministic — `min(3, len(params))` derived from the
prototype — so both sides can agree without any metadata beyond the header
declaration they already share.

## Design

### Libbboeos bodies

Remove the `per_function_sections` guard that disables `_apply_default_regparm`
and `_analyze_user_function_conventions`. Functions compiled with
`--per-function-sections` get their natural regparm convention: `strlen(const
char *s)` → regparm(1), arg in EAX; `memcpy(void *d, const void *s, size_t n)` →
regparm(3), args in EAX/EDX/ECX.

### cc.py call sites

When emitting a `call [FUNCTION_*_PTR]` for a libbboeos extern, derive the
regparm count from the prototype's parameter list (already parsed and stored
during the prototype-registration pass at line 532-534 of emission.py).  Load
the first min(3, N) args into EAX/EDX/ECX; push any remaining args on the stack
in right-to-left order.  After the call, pop only the stack-passed args.

Variadic prototypes (`printf`, `snprintf`, etc.) remain cdecl — all args on the
stack.  Today no variadic function is exported through the cc.py-compiled
pointer table (printf is the asm `shared_printf`), but the guard is future-safe.

### Stubs for clang callers

`tools/generate_libbboeos_stubs.py` currently emits a bare `jmp
[FUNCTION_*_PTR]` per export.  Clang-compiled programs (doom, test fixtures)
call through these stubs with cdecl (args on stack). Update the stubs to shuffle
stack args into regparm registers:

```asm
strlen:
    mov eax, [esp+4]
    jmp [FUNCTION_STRLEN_PTR]
```

For a 2-arg function like `strcmp`:

```asm
strcmp:
    mov eax, [esp+4]
    mov edx, [esp+8]
    jmp [FUNCTION_STRCMP_PTR]
```

For a 3-arg function like `memcpy`:

```asm
memcpy:
    mov eax, [esp+4]
    mov edx, [esp+8]
    mov ecx, [esp+12]
    jmp [FUNCTION_MEMCPY_PTR]
```

The stubs are currently GAS `.intel_syntax`.  The `mov` + `jmp` sequence adds
3-9 bytes per stub (vs. the current 6-byte `jmp`).

### Param count source

The stub generator parses the libbboeos `include/*.h` headers to extract param
counts for each export.  It already reads `constants.asm` for `FUNCTION_*_PTR`
addresses; it will additionally scan the C headers for matching prototypes.
Parsing is lightweight: match `<return_type> <name>(<params>);` lines and count
commas + 1 (or 0 for `void`).  Variadic prototypes (containing `...`) are
flagged and get a bare `jmp` (no register shuffle).

### What stays cdecl

- The 13 legacy asm exports (`shared_die`, `shared_printf`, etc.) — written in
  hand-tuned asm with their own conventions, not compiled by cc.py.
- Any future variadic libbboeos export.

### Test plan

- `test_cc_compatibility.py` — verifies cc.py compiles all .c files without
  error.
- `test_cc_bits.py` — verifies 16/32-bit codegen.
- `test_cc_codegen.py` — unit tests for codegen paths.
- `test_programs.py` — QEMU runtime tests exercising libbboeos calls from
  cc.py-compiled programs (the primary correctness gate).
- `test_asm.py` — self-hosted assembler byte-equivalence.
- Manual: build doom, verify it boots and runs (clang stub path).

## Files changed

| File | Change |
|------|--------|
| `cc/codegen/x86/emission.py` | Remove per_function_sections guard; emit regparm args at libbboeos call sites |
| `cc/codegen/x86/generator.py` | Store param count for libbboeos externs |
| `tools/generate_libbboeos_stubs.py` | Parse headers for param counts; emit cdecl-to-regparm shims |
| `make_os.sh` | No change (already wires all 8 files) |

## Risks

- **Stub correctness**: a wrong param count in the stub silently corrupts
  registers.  Mitigated by deriving counts from the same headers both cc.py and
  clang see.
- **Future variadic exports**: if a variadic function is added to the pointer
  table, the guard must emit cdecl for it.  The stub generator already detects
  `...` in the prototype.
