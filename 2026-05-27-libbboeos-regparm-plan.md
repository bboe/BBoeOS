# Libbboeos regparm implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use >
superpowers:subagent-driven-development (recommended) or >
superpowers:executing-plans to implement this plan task-by-task. > Steps use
checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch libbboeos pointer-table exports from cdecl to regparm so both
cc.py call sites and libbboeos bodies pass arguments in registers (EAX/EDX/ECX)
instead of on the stack.

**Architecture:** Three coordinated changes: (1) remove the
`per_function_sections` guard in emission.py so libbboeos bodies compile with
their natural regparm convention, (2) rewrite the libbboeos-extern call-site
emission to load args into regparm registers instead of pushing to the stack,
(3) update the stub generator to emit cdecl-to-regparm shims for clang callers.

**Tech Stack:** Python (cc.py compiler), x86-32 NASM assembly, GAS
`.intel_syntax` (stubs)

**Spec:** `design-specs:2026-05-27-libbboeos-regparm-design.md`

---

### Task 1: Record param count for libbboeos externs

The prototype-registration pass at `cc/codegen/x86/emission.py:532-534` adds
libbboeos extern names to `self.libbboeos_extern_declarations` (a `set[str]`)
but discards the param count.  Change it to a dict mapping name → param count so
the call-site emitter (Task 3) can derive regparm.

**Files:**
- Modify: `cc/codegen/x86/generator.py:268` (declaration type)
- Modify: `cc/codegen/x86/emission.py:532-534` (registration)
- Modify: `cc/codegen/x86/emission.py:1204` (read site)

- [ ] **Step 1: Change the declaration from set to dict**

In `cc/codegen/x86/generator.py`, line 268, change:

```python
self.libbboeos_extern_declarations: set[str] = set()
```

to:

```python
self.libbboeos_extern_declarations: dict[str, int] = {}
```

- [ ] **Step 2: Store param count at registration**

In `cc/codegen/x86/emission.py`, line 534, change:

```python
self.libbboeos_extern_declarations.add(function.name)
```

to:

```python
self.libbboeos_extern_declarations[function.name] = len(function.params)
```

- [ ] **Step 3: Update all read sites to use dict**

In `cc/codegen/x86/emission.py`, the check at line 1204 reads:

```python
if name in self.libbboeos_extern_declarations:
```

This already works with a dict (`in` tests keys).  Grep for any other
`libbboeos_extern_declarations` references to confirm no `.add()`, `.remove()`,
or set-specific operations remain:

```bash
grep -rn 'libbboeos_extern_declarations' cc/
```

There are references in `cc/codegen/x86/generator.py` lines 3176 and 3956 that
test membership (`name in self.libbboeos_extern_declarations`); those work
unchanged with a dict.

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/unit/test_cc_codegen.py -x -q
python3 tests/test_cc_compatibility.py
```

Expected: all pass (no behavior change yet).

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/generator.py cc/codegen/x86/emission.py
git commit -m "refactor(cc): store param count for libbboeos externs"
```

---

### Task 2: Enable regparm for libbboeos bodies

Remove the `per_function_sections` guard that disables `_apply_default_regparm`
and `_analyze_user_function_conventions`. After this task, libbboeos `.o` files
compiled with `--per-function-sections` get their natural regparm convention
(`strlen` → regparm(1), `memcpy` → regparm(3)), but the call sites still use
cdecl — so the OS will crash until Task 3 lands.  That is expected; this task
validates the body-side codegen in isolation via unit tests.

**Files:**
- Modify: `cc/codegen/x86/emission.py:519-558`

- [ ] **Step 1: Remove the per_function_sections guard**

In `cc/codegen/x86/emission.py`, replace:

```python
        if not self.per_function_sections:
            self._apply_default_regparm(ast.functions)
```

with:

```python
        self._apply_default_regparm(ast.functions)
```

And replace:

```python
        if not self.per_function_sections:
            self._analyze_user_function_conventions(ast.functions)
        else:
            self.user_function_pin_params = {}
            self.register_convention_functions = set()
```

with:

```python
        self._analyze_user_function_conventions(ast.functions)
```

- [ ] **Step 2: Verify libbboeos bodies now use regparm**

```bash
rm -f build/string.cc.asm
python3 cc.py --bits 32 --object --per-function-sections \
    user/libbboeos/string.c build/string.cc.asm
grep -A5 '^strlen:' build/string.cc.asm
```

Expected: `strlen` loads its arg from a register (via `mov ecx, [ebp+8]`
pin-load then uses ECX), NOT a bare `mov eax, [ebp+8]` without pin.

- [ ] **Step 3: Run unit tests**

```bash
python3 -m pytest tests/unit/test_cc_codegen.py -x -q
python3 tests/test_cc_compatibility.py
python3 tests/test_cc_bits.py
```

Expected: all pass.  The codegen tests exercise libbboeos call sites that still
emit cdecl (updated in Task 3), so they pass because the test harness doesn't
link or run the resulting asm.

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/emission.py
git commit -m "feat(cc): enable regparm for per-function-sections bodies"
```

---

### Task 3: Emit regparm args at libbboeos call sites

Rewrite the libbboeos-extern call emission (emission.py lines 1198-1225) to load
args into regparm registers instead of pushing to the stack.  The pattern
mirrors the existing fastcall path at lines 1037-1116 but simplified: no
out_register/in_register params, no register_convention/pin dispatch, no tail
calls — just regparm(N) with the standard EAX/EDX/ECX slots.

**Files:**
- Modify: `cc/codegen/x86/emission.py:1198-1225`

- [ ] **Step 1: Rewrite the libbboeos extern call emission**

Replace the block at `cc/codegen/x86/emission.py` lines 1198-1225 (the `if name
in self.libbboeos_extern_declarations:` branch) with:

```python
            # Libbboeos extern call via pointer table.  Derive regparm
            # from the prototype's param count — both this call site
            # and the libbboeos body agree because they see the same
            # header.
            if name in self.libbboeos_extern_declarations:
                pointer_constant = f"FUNCTION_{name.upper()}_PTR"
                param_count = self.libbboeos_extern_declarations[name]
                regparm_count = min(3, param_count)
                regparm_registers = (self.target.acc, self.target.dx_register, self.target.count_register)
                register_args: list[tuple[str, Node]] = []
                stack_args: list[Node] = []
                for index, arg in enumerate(arguments):
                    if index < regparm_count:
                        if index == 0:
                            # Arg 0 → AX, loaded last (after other
                            # register args) so evaluation of args 1-2
                            # can't trash the accumulator.
                            fastcall_ax_arg = arg
                        else:
                            register_args.append((regparm_registers[index], arg))
                    else:
                        stack_args.append(arg)
                clobbers: frozenset[str] = frozenset(self.target.register_pool)
                saved = self._pinned_registers_to_save(clobbers)
                use_pusha = discard_return and len(saved) >= 3
                if use_pusha:
                    self.emit("        pusha")
                else:
                    for register in saved:
                        self.emit(f"        push {register}")
                for arg in reversed(stack_args):
                    self._emit_push_arg(arg)
                self._emit_register_arg_moves(register_args)
                if regparm_count > 0:
                    self.emit_register_from_argument(argument=fastcall_ax_arg, register=self.target.acc)
                self.emit(f"        call [{pointer_constant}]")
                if stack_args:
                    self.emit(f"        add {self.target.stack_register}, {len(stack_args) * self.target.int_size}")
                if use_pusha:
                    self.emit("        popa")
                else:
                    for register in reversed(saved):
                        self.emit(f"        pop {register}")
                self.ax_clear()
                return
```

Note: `fastcall_ax_arg` must be initialized before the loop.  Add
`fastcall_ax_arg: Node | None = None` before the `for index, arg` loop (or
initialize it to `arguments[0]` when `regparm_count > 0`, guarding the 0-param
case).

- [ ] **Step 2: Build and run QEMU tests**

```bash
./make_os.sh
python3 tests/test_programs.py
```

Expected: 90 passed, 0 failed.  This is the primary correctness gate — every
test exercises libbboeos calls from cc.py-compiled programs.

- [ ] **Step 3: Run the full cc.py test suite**

```bash
python3 tests/test_cc_compatibility.py
python3 tests/test_cc_bits.py
python3 -m pytest tests/unit/test_cc_codegen.py -x -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add cc/codegen/x86/emission.py
git commit -m "feat(cc): emit regparm args for libbboeos indirect calls"
```

---

### Task 4: Update stubs for clang callers

Update `tools/generate_libbboeos_stubs.py` to parse libbboeos C headers for
param counts and emit cdecl-to-regparm shims (stack → EAX/EDX/ECX) instead of
bare `jmp` thunks.

**Files:**
- Modify: `tools/generate_libbboeos_stubs.py`
- Regenerated: `user/libbboeos/libbboeos_stubs.S`

- [ ] **Step 1: Add header parsing to the stub generator**

Add a function that scans `user/libbboeos/include/*.h` for function prototypes
matching the export names and returns a dict of name → param_count.  The parser
should:

- Match lines of the form `<type> <name>(<params>);`
- Count params by splitting on `,` and counting (0 for `void`)
- Flag variadic prototypes (containing `...`) as `None` param count (these stay
  cdecl — bare `jmp`)

Add this function to `tools/generate_libbboeos_stubs.py`:

```python
INCLUDE_DIRECTORY = REPO / "user" / "libbboeos" / "include"
PROTOTYPE = re.compile(
    r"^[\w\s\*]+?\b(\w+)\s*\(([^)]*)\)\s*;",
    re.MULTILINE,
)


def _collect_prototype_param_counts() -> dict[str, int | None]:
    """Return {function_name: param_count} from libbboeos headers.

    Variadic prototypes (``...``) map to None — they stay cdecl.
    ``(void)`` maps to 0.
    """
    result: dict[str, int | None] = {}
    for header in sorted(INCLUDE_DIRECTORY.glob("*.h")):
        for match in PROTOTYPE.finditer(header.read_text()):
            name = match.group(1)
            params = match.group(2).strip()
            if "..." in params:
                result[name] = None
            elif params == "" or params == "void":
                result[name] = 0
            else:
                result[name] = params.count(",") + 1
    return result
```

- [ ] **Step 2: Update stub rendering to emit register shuffles**

Change `_render_stubs` to accept param counts and emit the cdecl-to-regparm
shim.  Update its signature:

```python
def _render_stubs(*, exports: list[tuple[str, int, int | None]]) -> str:
```

Where each tuple is `(EXPORT_NAME, pointer_address, param_count)`.
`param_count=None` means variadic → bare `jmp`.

Update the per-export loop body:

```python
    regparm_registers = ["eax", "edx", "ecx"]
    for name, address, param_count in exports:
        symbol = name.lower()
        lines.extend([
            f"        .globl {symbol}",
            f"        .type  {symbol}, @function",
            f"{symbol}:",
        ])
        if param_count is not None and param_count > 0:
            regparm_count = min(3, param_count)
            for i in range(regparm_count):
                offset = 4 + i * 4
                lines.append(
                    f"        mov {regparm_registers[i]}, "
                    f"[esp+{offset}]"
                )
        lines.extend([
            f"        jmp [0x{address:08x}]"
            f"    /* FUNCTION_{name}_PTR */",
            f"        .size {symbol}, . - {symbol}",
            "",
        ])
```

- [ ] **Step 3: Update the main() caller to pass param counts**

In `main()`, after building the exports list, look up each export's param count
from the parsed headers:

```python
    prototypes = _collect_prototype_param_counts()
    exports_with_params: list[tuple[str, int, int | None]] = []
    for name, address in exports:
        param_count = prototypes.get(name.lower())
        exports_with_params.append((name, address, param_count))
```

Pass `exports_with_params` to `_render_stubs`.

- [ ] **Step 4: Update the file header comment**

Change the `_render_stubs` header comment to mention the regparm shim instead of
"6-byte `jmp`":

```python
    lines = [
        "/* user/libbboeos/libbboeos_stubs.S — auto-generated."
        "  DO NOT EDIT.",
        " *",
        " * Regenerate with"
        " `python3 tools/generate_libbboeos_stubs.py`.",
        " * Each stub shuffles cdecl stack arguments into regparm",
        " * registers (EAX/EDX/ECX) then jumps to the shared",
        " * libbboeos blob via `jmp [FUNCTION_<NAME>_PTR]`.",
        " * Clang programs link this file BEFORE libbboeos.a so ld",
        " * resolves each export to the stub and never pulls the",
        " * full body out of the archive.",
        " *",
        " * Source of truth: FUNCTION_<NAME>_PTR offsets in",
        " * kernel/include/constants.asm + prototypes in",
        " * user/libbboeos/include/*.h.  Sorted alphabetically.",
        " */",
    ]
```

- [ ] **Step 5: Regenerate stubs and verify**

```bash
python3 tools/generate_libbboeos_stubs.py
cat user/libbboeos/libbboeos_stubs.S
```

Expected: each stub now has `mov eax, [esp+4]` (and edx/ecx for 2-3 param
functions) before the `jmp`.  Verify `strlen` has 1 mov, `strcmp` has 2,
`memcpy` has 3.

- [ ] **Step 6: Build and run full test suite**

```bash
./make_os.sh
python3 tests/test_programs.py
python3 tests/test_asm.py
python3 tests/test_cc_compatibility.py
python3 tests/test_cc_bits.py
python3 -m pytest tests/unit/test_cc_codegen.py -x -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add tools/generate_libbboeos_stubs.py \
        user/libbboeos/libbboeos_stubs.S
git commit -m "feat(stubs): cdecl-to-regparm shims for clang callers"
```

---

### Task 5: Update comment in emission.py call-site block

The comment at `cc/codegen/x86/emission.py` line 1198-1203 currently says "Emit
a cdecl indirect call".  Update it to reflect the new regparm convention.

**Files:**
- Modify: `cc/codegen/x86/emission.py:1198-1203`

- [ ] **Step 1: Update the comment**

Replace:

```python
            # Libbboeos extern call.  The prototype-registration pass put
            # the name in libbboeos_extern_declarations after seeing
            # `int strcmp(const char *, const char *);` (or equivalent
            # via `#include "string.h"`).  Emit a cdecl indirect call
            # through the pointer table — args pushed right-to-left,
            # `call [FUNCTION_<NAME>_PTR]`, caller pops args.
```

with the comment written in Task 3's code block (the `# Libbboeos extern call
via pointer table.  Derive regparm ...` comment).

- [ ] **Step 2: Commit**

```bash
git add cc/codegen/x86/emission.py
git commit -m "docs(cc): update libbboeos call-site comment for regparm"
```
