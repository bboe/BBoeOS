# cc.py Place Refactor — Plan 5 / Stage 1: Foundation (ir.Access seam + byte-size gate)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a per-function byte-size differential gate and a dedicated first-class IR op (`ir.Access`) for the complex-lvalue access family (`PlaceLoad` / `PlaceStore` / `PlaceCall`), migrating them off the opaque `ir.Block` escape hatch with **byte-identical** output.

**Architecture:** Today every complex Place access (`b->n`, `*p`, `a[i].f`, `arr[i](x)`, …) rides `ir.Block(node=<ast>)` — the optimizer's escape hatch that walks the buried AST by name (`_iter_ast_var_names`) but never rewrites it. Stage 1 carves the *access subset* out of `Block` into a distinct typed op `ir.Access`, treated **identically-conservatively** at every existing `ir.Block` site (use-counting, side-effects, SSA exclusion, LICM/induction barriers, temp collection, pin-store tracking, lowering). Because `ir.Access.node` carries the *same* AST the matchers already understand and emission delegates to the *same* `generate_statement` path, output is byte-for-byte unchanged. The payoff is the seam: after Stage 1, `git grep ir.Access` enumerates exactly the access family that Stages 2–4 grow structured operands on, while `ir.Block` shrinks to the genuine non-access residents (VarDecl / ArrayDecl / struct-init / special Assigns / scalar inc-dec). A new `tests/test_cc_function_sizes.py` harness compiles all 50 userland `.c` with `--object --per-function-sections`, assembles each to ELF, and reads per-function `.text.<name>` section sizes — the gate that enforces "new size ≤ baseline" for every function from Stage 1 onward.

**Tech Stack:** Python 3.13, `cc.py` self-hosting C compiler (`cc/` package), `nasm -f elf32`, `readelf -S`, pytest-free bare-Python QEMU/diff drivers under `tests/`.

---

## Why this is the right Stage 1 (refinement of the decision doc)

The decision doc (`2026-06-02-cc-place-refactor-decision-spike.md`) imagined Stage 1 as "an `Address` *value op* carrying the symbolic `(const_base, offset, index, scale)` triple, computed at IR-build time." Implementation recon shows that triple is **not** computable at IR-build time: `_resolve_place` (`cc/codegen/x86/generator.py:3879`) runs *in codegen* — it emits `push bx` / `generate_expression(index)` / `shl` / `pop` and needs codegen-time struct-layout + type resolution. The symbolic triple legitimately lives in codegen (`PlaceAddress`, `generator.py:133`), exactly like GCC's `get_inner_reference` runs at lowering, not at GENERIC-build.

So Stage 1 is refined to the **byte-safe seam**: `ir.Access` carries the structured Place-access AST node (a *structured ref*, GCC-style) and exposes it as a distinct op the optimizer can special-case. Stages 2–3 progressively pull index operands into IR temps and make `_resolve_place` recursive where byte-safe; Stage 4 retires `Block`. This keeps the byte-efficiency gate trivially green in Stage 1 (output is identical) and defers every behavior change to a stage that owns the relevant optimizer pass. If the controller/user prefers the literal "IR-build-time address triple," stop and revise — but it cannot be byte-neutral and is not recommended.

## Scope boundary (what moves, what stays)

**Moves to `ir.Access` this stage:** `PlaceLoad`, `PlaceStore`, `PlaceCall` — in both statement and expression position. These are *always* complex (member / dereference / subscript-of-expression); a scalar read is `Var`, a scalar store is `Assign`, so none of them is ever a bare `VariablePlace`.

**Stays on `ir.Block` this stage (deliberate carve-out):** `PlaceAddressOf` and `PlaceIncrementDecrement`. Both have `VariablePlace` forms that the loop induction-variable matchers in `cc/loops.py` (lines 143, 1178, 1194) recognize as `Block(node=Assign(... PlaceIncrementDecrement(VariablePlace) ...))`. Migrating those would force touching the rep-string / strength-reduction matchers — which is **Stage 2's** designated highest-risk work. `PlaceLoad` / `PlaceStore` / `PlaceCall` are never matched by those IV recognizers (they match `Assign` whose RHS is a `BinaryOperation` or `PlaceIncrementDecrement(VariablePlace)`), so moving them needs **zero** changes to lines 143/1178/1194.

**Mirror rule:** at every *other* `ir.Block` site — the conservative/opaque/use-count/side-effect/SSA/temp-collection/pin-store sites — add `ir.Access` so it is treated **identically** to `ir.Block`. The task list below enumerates every one (verified by `grep -rn "ir\.Block" cc/`).

---

## File Structure

- `tests/test_cc_function_sizes.py` *(create)* — the byte-size differential gate: compile every userland `.c`, assemble to ELF, read per-function section sizes, compare to a committed baseline; `BBOE_UPDATE_SIZES=1` regenerates.
- `tests/golden/cc_function_sizes_baseline.json` *(create)* — committed `{relpath: {function: byte_size}}` baseline.
- `cc/ir.py` *(modify)* — add `class Access`; add to the `Instruction` union; route `PlaceLoad`/`PlaceStore`/`PlaceCall` to it in `Builder._build_stmt` / `_build_expr`.
- `cc/ir_optimize.py` *(modify)* — mirror `ir.Block` at `_has_side_effects`, the goto-target scan, and `_compute_use_counts`.
- `cc/ssa.py` *(modify)* — mirror `ir.Block` in `_opaque_referenced_names`.
- `cc/loops.py` *(modify)* — mirror `ir.Block` in the three opaque-barrier tuples (NOT the IV matchers).
- `cc/codegen/base.py` *(modify)* — mirror `ir.Block` in `_collect_ir_temps`, `_always_exits_ir`, and the switch-arm exit check.
- `cc/codegen/x86/generator.py` *(modify)* — mirror `ir.Block` in the pin-store-targets method.
- `cc/codegen/x86/emission.py` *(modify)* — add the `ir.Access` lowering arm.

---

### Task 1: Per-function byte-size differential harness

**Files:**
- Create: `tests/test_cc_function_sizes.py`
- Create: `tests/golden/cc_function_sizes_baseline.json`

This is the gate tool for the whole Plan 5 program. It must exist and pass (against a freshly-generated baseline) **before** any IR change, so later tasks can prove byte-neutrality.

- [ ] **Step 1: Verify the measurement pipeline by hand**

Run, to confirm the toolchain produces per-function ELF section sizes:

```bash
tmp=$(mktemp -d)
python3 cc.py --bits 32 --object --per-function-sections user/libbboeos/ctype.c "$tmp/ctype.asm"
nasm -f elf32 -i kernel/include/ "$tmp/ctype.asm" -o "$tmp/ctype.o"
readelf -SW "$tmp/ctype.o" | grep '\.text\.'
rm -rf "$tmp"
```

Expected: lines like `[ 2] .text.isalnum PROGBITS 00000000 0004a0 000037 …` where the 6th field (`000037`) is the function's byte size in hex. Each user function gets its own `.text.<name>` section because of `--per-function-sections`.

- [ ] **Step 2: Write the harness**

Create `tests/test_cc_function_sizes.py`:

```python
#!/usr/bin/env python3
"""Per-function byte-size differential gate for the cc.py Place refactor.

Compiles every userland C translation unit with cc.py's object-file +
per-function-sections pipeline, assembles each to an ELF object with nasm,
and reads the byte size of every ``.text.<function>`` section via readelf.
The result is compared against a committed baseline.

Gate policy (the Plan 5 byte-EFFICIENCY rule):
  * A function whose size GREW versus the baseline FAILS the test — a
    refactor must never make any function larger without an explicit,
    justified baseline refresh.
  * A function that SHRANK is reported but does not fail (size wins are
    welcome); refresh the baseline to capture the improvement.
  * A new or removed function is reported and fails until the baseline is
    refreshed (so additions/removals are always a deliberate baseline edit).

Refresh the baseline deliberately with BBOE_UPDATE_SIZES=1 only when a size
change is intended (a justified perf-driven increase, an accepted decrease,
or an added/removed function).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
KERNEL_INCLUDE = REPO_ROOT / "kernel" / "include"
BASELINE = REPO_ROOT / "tests" / "golden" / "cc_function_sizes_baseline.json"

# Userland translation units cc.py compiles through the object pipeline.
# Kernel .c is excluded (compiled with --target kernel, a different path);
# this gate covers the user / libbboeos surface the Place family touches.
SOURCE_GLOBS = ("user/libbboeos/*.c", "user/programs/*.c")


def discover_sources() -> list[Path]:
    """Return every userland .c path (sorted, repo-relative-stable)."""
    sources: list[Path] = []
    for glob in SOURCE_GLOBS:
        directory, pattern = glob.rsplit("/", 1)
        sources.extend(sorted((REPO_ROOT / directory).glob(pattern)))
    return sources


def function_sizes(*, source: Path, work: Path) -> dict[str, int]:
    """Compile *source* and return ``{function_name: byte_size}``."""
    asm_path = work / (source.stem + ".asm")
    object_path = work / (source.stem + ".o")
    subprocess.run(
        ["python3", str(CC), "--bits", "32", "--object", "--per-function-sections", str(source), str(asm_path)],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["nasm", "-f", "elf32", "-i", str(KERNEL_INCLUDE) + "/", str(asm_path), "-o", str(object_path)],
        capture_output=True,
        check=True,
        text=True,
    )
    readelf = subprocess.run(
        ["readelf", "-SW", str(object_path)],
        capture_output=True,
        check=True,
        text=True,
    )
    sizes: dict[str, int] = {}
    for line in readelf.stdout.splitlines():
        # readelf -SW row: "[ N] .text.<name> PROGBITS <addr> <off> <size> ..."
        stripped = line.strip()
        marker = ".text."
        if marker not in stripped or "PROGBITS" not in stripped:
            continue
        after_bracket = stripped.split("]", 1)[-1].split()
        # after_bracket = ['.text.<name>', 'PROGBITS', '<addr>', '<off>', '<size>', ...]
        if len(after_bracket) < 5 or not after_bracket[0].startswith(marker):
            continue
        name = after_bracket[0][len(marker):]
        sizes[name] = int(after_bracket[4], 16)
    return sizes


def measure_all() -> dict[str, dict[str, int]]:
    """Return ``{repo_relative_source: {function: size}}`` for every source."""
    result: dict[str, dict[str, int]] = {}
    with tempfile.TemporaryDirectory(prefix="cc_func_sizes_") as temporary:
        work = Path(temporary)
        for source in discover_sources():
            relative = str(source.relative_to(REPO_ROOT))
            result[relative] = function_sizes(source=source, work=work)
    return result


def main() -> int:
    """Run the gate, or refresh the baseline when BBOE_UPDATE_SIZES=1."""
    current = measure_all()
    if os.environ.get("BBOE_UPDATE_SIZES") == "1":
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"WROTE baseline {BASELINE}")
        return 0
    baseline = json.loads(BASELINE.read_text())
    failures: list[str] = []
    improvements: list[str] = []
    for source, functions in current.items():
        baseline_functions = baseline.get(source)
        if baseline_functions is None:
            failures.append(f"{source}: new translation unit (refresh baseline)")
            continue
        for name, size in functions.items():
            if name not in baseline_functions:
                failures.append(f"{source}:{name}: new function (refresh baseline)")
            elif size > baseline_functions[name]:
                failures.append(f"{source}:{name}: {baseline_functions[name]} -> {size} bytes (GREW)")
            elif size < baseline_functions[name]:
                improvements.append(f"{source}:{name}: {baseline_functions[name]} -> {size} bytes (shrank)")
        for name in baseline_functions:
            if name not in functions:
                failures.append(f"{source}:{name}: function removed (refresh baseline)")
    for source in baseline:
        if source not in current:
            failures.append(f"{source}: translation unit removed (refresh baseline)")
    for note in improvements:
        print(f"IMPROVED {note}")
    if failures:
        print("FAIL  per-function byte-size gate")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"PASS  per-function byte-size gate ({sum(len(f) for f in current.values())} functions, {len(current)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Generate the baseline from the current (pre-refactor) compiler**

Run:

```bash
BBOE_UPDATE_SIZES=1 python3 tests/test_cc_function_sizes.py
```

Expected: `WROTE baseline …/cc_function_sizes_baseline.json`. Confirm the JSON is non-empty and contains entries for `user/libbboeos/ctype.c` (e.g. `"isalnum": 55`) and several `user/programs/*.c` files.

- [ ] **Step 4: Verify the gate passes against its own baseline**

Run:

```bash
python3 tests/test_cc_function_sizes.py
```

Expected: `PASS  per-function byte-size gate (N functions, M files)` and exit 0.

- [ ] **Step 5: Verify the gate actually catches a regression (negative test)**

Temporarily hand-edit one size in the baseline JSON downward by 1 byte, re-run the gate, confirm it FAILS with a `GREW` line, then restore the baseline:

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("tests/golden/cc_function_sizes_baseline.json")
data = json.loads(p.read_text())
src = "user/libbboeos/ctype.c"
fn = next(iter(data[src]))
data[src][fn] -= 1
p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
print("perturbed", src, fn)
PY
python3 tests/test_cc_function_sizes.py; echo "exit=$?"
BBOE_UPDATE_SIZES=1 python3 tests/test_cc_function_sizes.py
```

Expected: the middle run prints a `GREW` failure and `exit=1`; the final run restores the true baseline.

- [ ] **Step 6: Commit**

```bash
git add tests/test_cc_function_sizes.py tests/golden/cc_function_sizes_baseline.json
git commit -m "test(cc): per-function byte-size differential gate for Place refactor"
```

---

### Task 2: Define the `ir.Access` op

**Files:**
- Modify: `cc/ir.py` (add `class Access` in alphabetical position after `class Block`; extend the `Instruction` union)

`ir.Access` mirrors `ir.Block` exactly: it carries an opaque AST `node`, reads no IR `Value` operands directly (its reads are discovered by walking `node`), and is an opaque region. The only difference from `Block` is the *type* — which is the entire point (it tags the access family for Stage 2+).

- [ ] **Step 1: Add the dataclass**

In `cc/ir.py`, the classes are in alphabetical order. `Access` sorts before `BinaryOperation`. Insert it as the first instruction dataclass, immediately after the `_is_constant_true` helper (before `class BinaryOperation` at line 63):

```python
@dataclass(frozen=True, kw_only=True, slots=True)
class Access(_NoValueFields):
    """Complex-lvalue access (PlaceLoad / PlaceStore / PlaceCall).

    Carved out of :class:`Block` so the access family is a distinct,
    optimizer-visible op (Plan 5 Stage 1).  Like ``Block`` it wraps an
    AST ``node`` lowered by the existing statement codegen and reads no
    IR ``Value`` operands directly — its reads are discovered by walking
    ``node`` (``_iter_ast_var_names``).  Treated identically to ``Block``
    at every conservative / opaque optimizer site; the type tag is what
    later stages key structured-operand handling off of.
    """

    node: ast_nodes.Node
```

- [ ] **Step 2: Add `Access` to the `Instruction` union**

In `cc/ir.py`, the `Instruction` union (currently lines 285–302) lists members alphabetically. Add `Access` first:

```python
Instruction = (
    Access
    | BinaryOperation
    | Block
    | BranchFalse
    | Call
    | CarryBranch
    | Copy
    | Index
    | IndexAssign
    | InlineAsm
    | Jump
    | Label
    | LoopBoundary
    | RepString
    | Return
    | Switch
    | TailCall
)
```

- [ ] **Step 3: Verify it imports**

Run:

```bash
python3 -c "from cc import ir; print(ir.Access(node=None))"
```

Expected: `Access(node=None)` prints without error.

- [ ] **Step 4: Commit**

```bash
git add cc/ir.py
git commit -m "feat(cc): add ir.Access op for the complex-lvalue access family"
```

---

### Task 3: Mirror `ir.Block` at every conservative IR-pass site

**Files:**
- Modify: `cc/ir_optimize.py:163` (`_has_side_effects`), `:502` (goto-target scan), `:525` (`_compute_use_counts`)
- Modify: `cc/ssa.py:447` (`_opaque_referenced_names`)
- Modify: `cc/loops.py:652`, `:707`, `:1074` (opaque-barrier tuples)
- Modify: `cc/codegen/base.py:284` (`_always_exits_ir`), `:353` (`_collect_ir_temps`), `:909` (switch-arm exit)
- Modify: `cc/codegen/x86/generator.py` pin-store-targets method (the `isinstance(instruction, ir.Block)` arm, ~line 3027)

Do this **before** Task 4 routes anything to `ir.Access`, so the moment Task 4 lands the new op is already handled everywhere `Block` is. Each edit adds `ir.Access` alongside `ir.Block` with identical behavior. **Do not** touch the loop induction-variable matchers at `cc/loops.py:143`, `:1178`, `:1194` — `PlaceLoad`/`PlaceStore`/`PlaceCall` never match them (see Scope boundary).

- [ ] **Step 1: `cc/ir_optimize.py` — side effects**

At `_has_side_effects` (line 160), add `ir.Access` to the tuple so a dead-destination access is never dropped (matching `Block`):

```python
    return isinstance(
        instruction,
        (
            ir.Access,
            ir.Block,
            ir.BranchFalse,
            ir.Call,
            ir.CarryBranch,
            ir.IndexAssign,
            ir.InlineAsm,
            ir.Jump,
```

- [ ] **Step 2: `cc/ir_optimize.py` — goto-target scan**

At the goto-target collector (line 502), the current code is:

```python
            if isinstance(instruction, ir.Block):
                targets.update(_iter_ast_goto_targets(instruction.node))
```

Replace with:

```python
            if isinstance(instruction, (ir.Access, ir.Block)):
                targets.update(_iter_ast_goto_targets(instruction.node))
```

- [ ] **Step 3: `cc/ir_optimize.py` — use counts**

At `_compute_use_counts` (line 525), the current code is:

```python
            elif isinstance(instruction, ir.Block):
                for name in _iter_ast_var_names(instruction.node):
                    counts[name] = counts.get(name, 0) + 1
```

Replace with:

```python
            elif isinstance(instruction, (ir.Access, ir.Block)):
                for name in _iter_ast_var_names(instruction.node):
                    counts[name] = counts.get(name, 0) + 1
```

- [ ] **Step 4: `cc/ssa.py` — opaque referenced names**

At `_opaque_referenced_names` (line 447), the current code is:

```python
        if isinstance(instruction, ir.Block):
            referenced.update(_iter_ast_var_names(instruction.node))
```

Replace with:

```python
        if isinstance(instruction, (ir.Access, ir.Block)):
            referenced.update(_iter_ast_var_names(instruction.node))
```

- [ ] **Step 5: `cc/loops.py` — three opaque-barrier tuples**

At lines 652, 707, and 1074 the current text is the identical tuple:

```python
            if isinstance(instruction, (ir.Block, ir.CarryBranch, ir.Switch)):
```

(line 707 continues `... and _ast_takes_address_of(...)`). In all three, change the tuple to include `ir.Access`:

```python
            if isinstance(instruction, (ir.Access, ir.Block, ir.CarryBranch, ir.Switch)):
```

Use a targeted replace per line (the three occurrences are not all byte-identical — line 707 has a trailing `and`). Verify afterward:

```bash
rtk proxy grep -n "ir.Access, ir.Block, ir.CarryBranch, ir.Switch" cc/loops.py
```

Expected: three matching lines (652, 707, 1074).

- [ ] **Step 6: `cc/codegen/base.py` — temp collection (REQUIRED for correctness)**

At `_collect_ir_temps` (line 353), a `PlaceLoad` in expression position is wrapped `Assign(expr=PlaceLoad, name=_ir_temp)`; this match collects the temp so it gets a frame slot. Current:

```python
                    case ir.Block(node=Assign(name=name)):
                        destination = name
```

Replace with:

```python
                    case ir.Block(node=Assign(name=name)) | ir.Access(node=Assign(name=name)):
                        destination = name
```

- [ ] **Step 7: `cc/codegen/base.py` — `_always_exits_ir` and switch-arm exit**

At `_always_exits_ir` (line 284):

```python
                case ir.Block(node=node):
                    return CodeGeneratorBase.always_exits([node])
```

Replace with:

```python
                case ir.Block(node=node) | ir.Access(node=node):
                    return CodeGeneratorBase.always_exits([node])
```

At the switch-arm exit check (line 909):

```python
        if isinstance(last, ir.Block):
            return CodeGeneratorBase.always_exits([last.node])
```

Replace with:

```python
        if isinstance(last, (ir.Block, ir.Access)):
            return CodeGeneratorBase.always_exits([last.node])
```

- [ ] **Step 8: `cc/codegen/x86/generator.py` — pin-store targets (REQUIRED for correctness)**

The store-targets method (the `isinstance(instruction, ir.Block)` arm near line 3027) extracts `[node.name]` from a wrapped `Assign` so the pin tracker sees the temp's definition. Current:

```python
        if isinstance(instruction, ir.Block):
            # Block-wrapped AST escape hatch.  A VarDecl with
```

Replace the guard with the two-type form (keep the comment body intact, append a note):

```python
        if isinstance(instruction, (ir.Block, ir.Access)):
            # Block / Access wrap an AST escape hatch.  A VarDecl with
```

Also extend the analysis comment at generator.py line ~756 (`# Block-wrapped statements are not analysed; ``ir.Block`` …`) to read `# Block / Access-wrapped statements are not analysed; ``ir.Block`` / ``ir.Access`` …` so the rationale stays accurate.

- [ ] **Step 9: Verify nothing routes to `ir.Access` yet (no behavior change)**

The builder does not emit `ir.Access` until Task 4, so the full suite must still pass unchanged. Run:

```bash
python3 tests/test_cc_function_sizes.py
python3 tests/test_cc_place.py
python3 tests/unit/test_cc_codegen.py
```

Expected: byte-size gate PASS, golden byte-identical PASS, codegen unit tests pass. (No `ir.Access` is constructed yet, so these confirm the mirror edits are inert.)

- [ ] **Step 10: Commit**

```bash
git add cc/ir_optimize.py cc/ssa.py cc/loops.py cc/codegen/base.py cc/codegen/x86/generator.py
git commit -m "refactor(cc): treat ir.Access identically to ir.Block at every conservative IR site"
```

---

### Task 4: Route the access family to `ir.Access` + lower it

**Files:**
- Modify: `cc/ir.py` (`Builder._build_stmt`, `Builder._build_expr`)
- Modify: `cc/codegen/x86/emission.py:1637` (`_lower_ir_instruction`)

This is the load-bearing task: it flips `PlaceLoad` / `PlaceStore` / `PlaceCall` from `ir.Block` to `ir.Access`. Because Task 3 already handles `ir.Access` everywhere and emission delegates to the same `generate_statement`, output must stay byte-identical.

- [ ] **Step 1: Add an access-shape predicate to `cc/ir.py`**

Add this module-level helper near the top of `cc/ir.py` (after `_is_constant_true`, before `class Access`):

```python
def _is_migrated_access(node: ast_nodes.Node, /) -> bool:
    """Return True for the Place-access shapes Stage 1 lowers to :class:`Access`.

    PlaceLoad / PlaceStore / PlaceCall — the always-complex (member /
    dereference / subscript-of-expression) accesses.  PlaceAddressOf and
    PlaceIncrementDecrement stay on :class:`Block` this stage (their
    ``VariablePlace`` forms back the loop induction-variable matchers in
    ``cc.loops``); see the Stage 1 plan's Scope boundary.
    """
    return isinstance(node, (ast_nodes.PlaceCall, ast_nodes.PlaceLoad, ast_nodes.PlaceStore))
```

- [ ] **Step 2: Route statement-position accesses in `_build_stmt`**

`PlaceStore` (and a discard-position `PlaceCall` / `PlaceLoad`) reaches `_build_stmt`'s default `case _:` (line 736). Add an explicit arm immediately before the default `case _:`:

```python
            case _ if _is_migrated_access(stmt):
                out.append(Access(node=stmt))
            case _:
                out.append(Block(node=stmt))
```

- [ ] **Step 3: Route expression-position accesses in `_build_expr`**

`PlaceLoad` (and value-yielding `PlaceCall` / `PlaceStore`) reaches `_build_expr`'s default `case _:` (line 536), which wraps `Block(node=Assign(expr=expr, name=temp))`. Add an explicit arm immediately before that default:

```python
            case _ if _is_migrated_access(expr):
                temp = self._tmp()
                out.append(Access(node=ast_nodes.Assign(expr=expr, name=temp)))
                return temp
            case _:
                # Complex: use a temp + Block to let AST codegen handle it.
                temp = self._tmp()
                out.append(Block(node=ast_nodes.Assign(expr=expr, name=temp)))
                return temp
```

(The `Access` payload is the *identical* `Assign(expr=<place>, name=temp)` the `Block` path would have built — only the wrapper type changes.)

- [ ] **Step 4: Lower `ir.Access` in emission**

In `cc/codegen/x86/emission.py`, `_lower_ir_instruction` ends (line 1637) with:

```python
            case ir.Block(node=node):
                self.generate_statement(node)
```

Add an identical `ir.Access` arm immediately before it (alphabetical-ish; `Access` before `Block`). Place it right after the `ir.Switch` arm (line 1636):

```python
            case ir.Access(node=node):
                self.generate_statement(node)
            case ir.Block(node=node):
                self.generate_statement(node)
```

- [ ] **Step 5: Verify byte-identity — the golden and the size gate**

Run:

```bash
python3 tests/test_cc_place.py
python3 tests/test_cc_function_sizes.py
```

Expected: `PASS index_member golden byte-identical` (the golden fixture exercises `*p`, `b->n`, `b->data[i]`, `arr[i](x)`, etc. — all now via `ir.Access`) and `PASS per-function byte-size gate`. If the golden differs, a mirror site in Task 3 was missed; if a size grew, an opaque-barrier mirror was missed.

- [ ] **Step 6: Confirm the seam actually moved the family**

Run:

```bash
rtk proxy python3 - <<'PY'
import subprocess, sys
sys.argv = ["x"]
from pathlib import Path
src = Path("user/libbboeos/string.c")
from cc import lexer, parser, ir  # adjust to cc.py's real entry points if needed
print("This step is a sanity check; if cc internals differ, instead grep the IR dump.")
PY
echo "--- functional check: a program using *p / b->n still compiles & runs is covered by test_programs ---"
```

If a quick IR-introspection entry point isn't readily importable, skip the scripted check and rely on Step 5's golden (which proves the access shapes still emit correctly) plus Task 5's runtime suites. The key invariant is byte-identity, already gated in Step 5.

- [ ] **Step 7: Commit**

```bash
git add cc/ir.py cc/codegen/x86/emission.py
git commit -m "feat(cc): lower PlaceLoad/PlaceStore/PlaceCall through ir.Access"
```

---

### Task 5: Full-suite verification gate

**Files:** none (verification only)

The byte-size gate and golden are necessary but not sufficient — the runtime suites confirm the migrated accesses still execute correctly in QEMU, and the kernel-architecture rule (memory: run-full-ci-matrix-locally) applies because this touches the IR/codegen core.

- [ ] **Step 1: cc.py unit + golden + size gate**

```bash
python3 tests/test_cc_function_sizes.py
python3 tests/test_cc_place.py
python3 tests/unit/test_cc_codegen.py
```

Expected: all pass; size gate reports zero `GREW`, ideally zero `IMPROVED` (Stage 1 is byte-neutral by design — any `IMPROVED` line is a surprise worth understanding before refreshing the baseline).

- [ ] **Step 2: cc.py compatibility (clang cross-check) if present**

```bash
ls tests/unit/test_cc_compatibility.py 2>/dev/null && python3 tests/unit/test_cc_compatibility.py || echo "no compatibility suite at that path; skip"
```

Expected: pass, or the skip message.

- [ ] **Step 3: Self-hosting assembler suite**

```bash
tests/test_asm.py
```

Expected: 49/49 (or the suite's current full count) pass.

- [ ] **Step 4: Program runtime suite, both filesystems**

```bash
tests/test_programs.py --filesystem bbfs
tests/test_programs.py --filesystem ext2
```

Expected: all program runtime checks pass on both filesystems. These exercise cc.py-compiled userland (`*p`, `b->n`, indexed calls) end-to-end in QEMU.

- [ ] **Step 5: Filesystem regression suite**

```bash
tests/test_bboefs.py
```

Expected: pass (confirms nothing in the shell / fs programs regressed).

- [ ] **Step 6: Confirm `ir.Block` shrank to non-access residents**

```bash
rtk proxy grep -rn "Block(node=" cc/ir.py
```

Expected: `Block` now wraps only VarDecl / ArrayDecl / the special-cased `Assign` forms / `Call(name="asm")` / carry-return temp / the `_build_expr` non-access default — and `Access` carries `PlaceLoad`/`PlaceStore`/`PlaceCall`. This is the Stage 1 deliverable: a clean seam for Stages 2–4.

- [ ] **Step 7: No commit needed (verification only)**

If every suite is green, Stage 1 is complete. Hand off via superpowers:finishing-a-development-branch.

---

## Self-Review

**Spec coverage (against the decision doc's Stage 1):**
- "Build the per-function byte-size differential harness (the new gate tool)" → Task 1. ✓
- "Define the uniform access IR ops … Lower the currently-Block-emitted Place access family into them" → Tasks 2 & 4 (refined: structured-ref `ir.Access` rather than an IR-build-time address triple, with the rationale documented above and the carve-out of `PlaceAddressOf`/`PlaceIncrementDecrement` to Stage 2). ✓ (deviation flagged)
- "emission folds them into tight x86 addressing" → emission delegates to the unchanged `_emit_place_*` / `_resolve_place` fold (Task 4 Step 4); the fold is preserved, not rebuilt — byte-identity proves it. ✓
- "Optimizer passes updated only enough to treat the new ops safely (conservative)" → Task 3 mirrors every `ir.Block` conservative site. ✓
- "Gate: byte-size ≤ baseline. Index/IndexAssign stay as-is this stage" → Task 1 + Task 4 Step 5 + Task 5; `Index`/`IndexAssign` untouched. ✓

**Placeholder scan:** Task 4 Step 6's scripted IR-introspection is explicitly optional with a documented fallback (it depends on cc.py internals not verified here); every other step has concrete code or exact commands. No TBD/TODO/"handle edge cases" remain.

**Type consistency:** `ir.Access(node=…)` (one field, `node`) is used identically in the dataclass (Task 2), the builder (Task 4 Steps 2–3), every mirror site (Task 3), and emission (Task 4 Step 4). `_is_migrated_access` is defined once (Task 4 Step 1) and called in both builder arms. The baseline JSON shape `{relpath: {function: size}}` is written and read by the same harness (Task 1).

## Risk register (Stage 1)

1. **A missed `ir.Block` mirror site** → the golden or size gate fails in Task 4 Step 5 (not silently). The grep-verified site list (Task 3) is exhaustive over `cc/`; the gate is the backstop.
2. **`PlaceAddressOf`/`PlaceIncrementDecrement` accidentally moved** → would break loop IV matchers (size regression caught by the gate). `_is_migrated_access` deliberately excludes them; do not widen it this stage.
3. **Harness toolchain assumptions** (`readelf -SW` column order, `--per-function-sections` naming) → pinned by Task 1 Step 1's hand-verification and Step 5's negative test before any IR change depends on it.
