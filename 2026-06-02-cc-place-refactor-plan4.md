# Plan 4 — Fold `AddressOf` / `IncrementDecrement` / `IndexedCall` onto the recursive `Place` core, delete them, and enable the new shapes that fall out

**Branch / PR scope:** ONE plan, ONE PR. Scope = "Cleanup + new shapes."

**Gate (non-negotiable):**
- **BYTE-EXACT** for every shape that compiles today. The `tests/test_cc_place.py` golden snapshot (`tests/golden/cc_place_index_member.asm`) stays byte-identical for its pre-existing probes, and the **userland differential** (every `user/**/*.c` compiled before vs. after) must be byte-identical.
- **NEW shapes** (`&*p`, `(*fp)(args)`, `a[i]++`, `a[i][j]++`) do not compile today; they are additive and verified by **golden coverage** + **`tests/test_programs.py` runtime correctness**, not against any legacy oracle.
- Full `tests/test_asm.py` **49/49**, `tests/test_programs.py` **bbfs + ext2 green**, full unit suite green.

**Conventions (apply to every code change):**
- No abbreviations in identifiers. Alphabetical ordering of methods and AST-node class definitions.
- `@dataclass(kw_only=True, slots=True)` for all AST nodes.
- Preserve every existing comment verbatim unless the code it describes is deleted.
- Registers via `self.target.acc`, `self.target.bx_register`, `self.target.si_register`, `self.target.stack_register`, `self.target.base_register`, `self.target.int_size`, `self.target.word_size`, `self.target.low_word(...)`. Never hard-code `eax`/`ebx`/`esi` in new code unless reproducing a legacy literal exactly (e.g. the `lea ... [ebp-...]` literals in `_emit_place_address_of`).
- Commit frequently (one commit per completed checkbox group, message bodies end with the Co-Authored-By trailer).

---

## 0. Orientation for a zero-context engineer

The compiler is the self-hosting `cc.py`, which is a thin shim over the `cc/` package. The codegen lives in **two** files (the prompt's "emission.py / generator.py" map to these exact paths):

- `cc/codegen/x86/emission.py` (4392 lines) — statement/expression dispatch, `generate_call`, `generate_indexed_call`, `generate_index_assign`, `_generate_index_expression`, `_is_pure_expression`, `_ir_value_to_ast`.
- `cc/codegen/x86/generator.py` (5457 lines) — `_emit_place_address_of`, `_emit_place_increment_decrement`, `_emit_place_load`, `_emit_place_store`, `_resolve_place`, `_place_type`, `_expression_type`, `emit_store_local`, auto-pin tallying (`_tally_auto_pin_counts`, out-reg scanning).

Other affected modules:
- `cc/ast_nodes.py` — node definitions (`AddressOf` @49, `IncrementDecrement` @374, `IndexedCall` @408; the Place family @222–767).
- `cc/parser.py` — construction sites.
- `cc/codegen/liveness.py` — use/def analysis driving auto-pin.
- `cc/ir.py` — the IR builder; `Value = int | str | ast_nodes.AddressOf` (@27), `AddressOf` pass-through (@528), `IndexedCall` Block case (@722).
- `cc/codegen/base.py`, `cc/loops.py`, `cc/ssa.py` — **additional `AddressOf` consumers** the prompt under-counts. These detect `&var` for SSA address-taken exclusion, loop induction-variable address-taken, and constant init. **All must be migrated** or deleting `AddressOf` will crash.

**THE SINGLE BIGGEST RISK, not fully spelled out in the original brief:** `AddressOf` is consumed far beyond its own codegen arm. There are ~30 `isinstance(x, AddressOf)` / `x.var.name` sites across `emission.py`, `generator.py`, `base.py`, `loops.py`, `ssa.py`, `ir.py`. The hottest are the **`out_register` call-argument detection** (`emission.py` 2420/2478/2500), the **`*(T*)&local` fast paths** (`generator.py` 1319/1382/3653), the **auto-pin out-reg scans** (`generator.py` 2850/2861/4069/4187, `base.py` 786/791), the **SSA address-taken set** (`ssa.py` 87/226), and the **loop induction address detection** (`loops.py` 160/539/632/689). Folding `&var → PlaceAddressOf(VariablePlace(name))` means every one of these must learn to recognize the new shape and read `.place.name` instead of `.var.name`. Missing one silently flips auto-pin/SSA/induction decisions → output diverges → userland differential catches it (but only at the very end). We therefore introduce a **single helper** `address_of_variable_name(node) -> str | None` in `cc/ast_nodes.py` and route every site through it, so the migration is mechanical and complete.

---

## 1. Reconnaissance — verbatim legacy contracts (read-only; no edits in this section)

### 1.1 `AddressOf` — `&name` only

**Construction** (`cc/parser.py:2276`): the `AMP` branch of `parse_primary`. `&arr[i]` (line 2243) → `BinaryOperation(+)`; `&obj.field` / `&ptr->field` / `&member[i]` (2256–2275) → `PlaceAddressOf`; the bare-name fallthrough →
```python
return AddressOf(line=line, var=Var(line=line, name=name_token[1]))
```

**Codegen** (`cc/codegen/x86/emission.py:2653`, inside `generate_expression`):
```python
if isinstance(expression, AddressOf):
    name = expression.var.name
    if name in self.out_register_locals:
        message = f"cannot take address of out_register parameter '{name}'"
        raise CompileError(message, line=expression.line)
    addr = self._local_address(name)
    if name in self.locals:
        self.emit(f"        lea {self.target.acc}, [{addr}]")
    else:
        self.emit(f"        mov {self.target.acc}, {addr}")
    self.ax_local = None
    self.ax_is_byte = False
```
Byte output: locals → `lea acc, [<addr>]`; globals/constants → `mov acc, <addr>`. Out_register params are rejected.

**Type inference** (`cc/codegen/x86/generator.py:2749`, inside `_expression_type`):
```python
if isinstance(node, AddressOf):
    variable_type = self._expression_type(node.var)
    return f"{variable_type} *"
```
Note the **space** before `*`. `sizeof(&x)` and any expression-type query of `&x` must reproduce `"<type> *"` exactly.

**Liveness** (`cc/codegen/liveness.py:138`, inside `_add_expression_uses`):
```python
if isinstance(expression, AddressOf):
    if isinstance(expression.var, Var):
        accumulator.add(expression.var.name)
    return
```
`&x` records `x` as a **use**. This is the asymmetry that flips auto-pin if not reproduced after folding.

**IR** (`cc/ir.py:27` and `:528`): `Value = int | str | ast_nodes.AddressOf`; `_build_expr` has `case ast_nodes.AddressOf(): return expr` (pass-through so `generate_call` can see the `&var` for out_register). `_build_expr`'s default (`:534`) would otherwise wrap it in a temp+Block.

**`_ir_value_to_ast`** (`emission.py:1452`): `if isinstance(value, AddressOf): return value`.

**Purity** (`emission.py:1472`): `AddressOf` (and already `PlaceAddressOf`) are pure.

**Other `&var` consumers (must migrate):**
- `emission.py:2420/2478/2500` — `generate_call` out_register capture (`isinstance(arg, AddressOf)`, `arg.var.name`).
- `generator.py:1319/1382` — `_generate_cast_*`/store `*(T*)&local` fast path (`fast_path_target.var.name`).
- `generator.py:2850/2861` — auto-pin store-target scan in calls.
- `generator.py:3653/3656` — deref-store frame-direct fast path through a cast of `&local`.
- `generator.py:4069/4074/4083` — `_tally_auto_pin_counts` address-taken marking.
- `generator.py:4187` — out-reg written-set tally.
- `base.py:396/597/786/791/1027` — constant-init `&var`, address-taken-for-name, out-reg scan, leaf classification.
- `loops.py:160/539/632/689` — induction-variable address-of detection across four helpers.
- `ssa.py:87/226/230` — SSA address-taken set + operand encoding (`("a", value.var.name)`).

### 1.2 `IncrementDecrement` — `x++` / `++x` named-var only

**Construction:** postfix expr `cc/parser.py:919`; postfix stmt `:968`; prefix `:2204`, `:2511`. All build `IncrementDecrement(delta, is_postfix, target_name)`.

**Codegen — expression form** (`emission.py:2704`):
```python
elif isinstance(expression, IncrementDecrement):
    target = expression.target_name
    self._check_defined(target, line=expression.line)
    delta_value = abs(expression.delta)
    update_expression = BinaryOperation(
        left=Var(line=expression.line, name=target),
        line=expression.line,
        operation="+" if expression.delta > 0 else "-",
        right=Int(line=expression.line, value=delta_value),
    )
    self.emit_store_local(expression=update_expression, name=target)
    self.generate_expression(Var(line=expression.line, name=target))
    if expression.is_postfix:
        reverse = "sub" if expression.delta > 0 else "add"
        self.emit(f"        {reverse} {self.target.acc}, {delta_value}")
        self.ax_clear()
```
Lowers `var ± 1` through `emit_store_local`, reloads `var` into acc, and for postfix recovers the pre-update value with one `sub`/`add`.

**Codegen — statement form** (`emission.py:4089`):
```python
elif isinstance(statement, IncrementDecrement):
    self.generate_expression(statement)
    self.ax_clear()
```
Statement form = expression form + `ax_clear()`.

**Liveness:** NO handler → `_add_expression_uses` / `_collect_use_def` raise `LivenessAnalysisError` → caller swallows it → **auto-pin stays off** for any function containing `x++`. This MUST be preserved after folding (PlaceIncDec must keep raising).

**Purity:** `IncrementDecrement` is **not** in `_is_pure_expression` → treated impure (correct; it mutates).

**The already-existing `PlaceIncDec` emitter** (`generator.py:2374`, `_emit_place_increment_decrement`): synthesizes `place = place ± delta` via `_emit_place_store`, reloads with `PlaceLoad`, postfix recovers with `sub`/`add`. **This is structurally identical to the legacy `IncrementDecrement` codegen but routes through `_emit_place_store` / `PlaceLoad` instead of `emit_store_local` / `Var`.** A `VariablePlace` does NOT currently flow through `_emit_place_store` cleanly — `_emit_place_store` (`generator.py:2444`) only handles member/deref shapes then falls into `_resolve_place`, which raises on `VariablePlace`. So the `VariablePlace` arm must NOT route through the generic `_emit_place_store`; it must reproduce the legacy `emit_store_local` lowering directly.

### 1.3 `IndexedCall` — `arr[i](args)` only

**Construction:** expr `cc/parser.py:862`; stmt `:979`. Build `IndexedCall(args, array=Var(name), index)`.

**Codegen** (`emission.py:3636`, `generate_indexed_call`): full text read at lines 3636–3767. Behaviour:
- Saves pinned registers: `use_pusha = discard_return and len(saved) >= 3` → `pusha`, else `push <reg>` per saved register.
- Pushes args right-to-left (cdecl) via `_emit_push_arg`.
- Computes callee address into `acc`:
  - **Global array** (`name in self.global_arrays`): constant index → `mov acc, [<base>+<off>]` (or `[<base>]`); variable index → SI-guarded `lea si, [<base>]` / eval index / `_emit_scale_index(acc, int_size)` / `add acc, si` / `mov acc, [acc]`, then `call acc`, cleanup, **early return**.
  - **Local stack array** (`name in self.local_stack_arrays`): mirror, using `_l_<name>` (frame-elided) or `<base_register>-<offset>`.
  - **Pointer variable** (else): constant index → `_emit_load_var(name, register=acc)` / optional `add acc, off` / `mov acc, [acc]`; variable index → SI-guarded `_emit_load_var(name, register=si)` / eval index / scale / `add acc, si` / `mov acc, [acc]`, `call acc`, cleanup, early return.
- Tail (constant-index global/local path): `call acc`; if args, `add stack_register, len(args)*int_size`; restore (`popa` or `pop` per saved reversed); `ax_clear()`.

**`discard_return`:** statement form (`emission.py:4099`) calls with `discard_return=True`, then `ax_clear()`. Expression form (`emission.py:2730`) calls with the default `discard_return=False`.

**IR:** `case ast_nodes.IndexedCall(): out.append(Block(node=stmt))` (`ir.py:722`) — identical to the default Block fallthrough.

**Liveness:** NO handler → raises → auto-pin off. After folding to `PlaceCall`, that must stay raising.

**Purity:** `IndexedCall` not in `_is_pure_expression` → impure (correct).

### 1.4 The lowering target for `a[i]++` — `generate_index_assign`

`generate_index_assign` (`emission.py:3509`): full text read 3509–3635. Picks `element_size`, then handles four sub-cases: (const index, const value) → `mov <width> [addr], <value>`; (const index, var value) → eval expr, `mov [addr], <acc>`; (var index, const base) → `emit_constant_reference` + eval + `_emit_constant_base_index_addr`; (var index, mem base) → SI fast-path or push/eval/scale/add/pop/store. **`a[i]++` will synthesize `IndexAssign(array=a, index=i, expr=BinaryOperation(Index(a,i), op, Int(delta)))` and route through this method** for the *store*, then reload via `Index(a, i)` for the result value, exactly mirroring how legacy `IncrementDecrement` reloads a named var.

**Index re-evaluation caveat:** legacy `IncrementDecrement` re-evaluates `var` (cheap, a name). For `a[i]++`, the index `i` is evaluated up to **three** times (store address, store RHS `Index(a,i)`, reload `Index(a,i)`). For the supported shapes (`i` a `Var`, `Int`, or pure arithmetic), this is benign and matches C. We **document** that an index with side effects (`a[i++]++`) is unsupported / undefined and is not exercised. This matches "match C / whatever's simplest and document."

### 1.5 Auxiliary contracts read verbatim

- `_member_place_base` (`parser.py:206`): returns `VariablePlace(name)` for dot, `DereferencePlace(VariablePlace(name))` for arrow.
- `_resolve_place` (`generator.py:3679`): handles only `arr[i].member` (shape A) and `arr[i].member[j]` (shape B). **Raises on `SubscriptPlace(VariablePlace)` and `VariablePlace`** — Plan 5 territory. This is why `a[i]++` lowers through `IndexAssign` rather than `_emit_place_store`.
- `_emit_place_load` (`generator.py:2396`): member/deref dedicated emitters, then generic `_resolve_place`. **A bare `SubscriptPlace(VariablePlace)` would hit `_resolve_place` and raise** — so `PlaceIncDec(SubscriptPlace(VariablePlace))` must NOT call `_emit_place_store`/`PlaceLoad` for the named-array case; it must lower to `IndexAssign`+`Index`.
- `_emit_dereference_place_load` (`generator.py`, dispatched from `_emit_place_load` @2424): used for `(*fp)(args)` callee evaluation and `&*p`.

---

## 2. Design

### 2.1 New shared helper `address_of_variable_name`

Add to `cc/ast_nodes.py` (alphabetical placement among module-level functions; if none exist, place directly after the imports / before the first class, with a docstring). After folding, the canonical "this expression is `&named_variable`" test:

```python
def address_of_variable_name(node: object, /) -> str | None:
    """Return the variable name of an ``&name`` expression, else ``None``.

    After the Place refactor, ``&name`` is ``PlaceAddressOf`` over a
    ``VariablePlace``.  Call-argument detection (``out_register`` capture),
    SSA address-taken analysis, loop induction-variable scanning and the
    ``*(T *)&local`` fast paths all need to recognise this exact shape and
    recover the bare name; centralising the check keeps every consumer in
    lock-step and avoids a stale ``isinstance(..., AddressOf)`` lurking.
    """
    if isinstance(node, PlaceAddressOf) and isinstance(node.place, VariablePlace):
        return node.place.name
    return None
```

Every former `isinstance(x, AddressOf)` + `x.var.name` pair becomes:
```python
if (taken_name := address_of_variable_name(x)) is not None:
    ... use taken_name ...
```

### 2.2 `_emit_place_address_of` — new `VariablePlace` and `DereferencePlace` arms

Insert into `_emit_place_address_of` (`generator.py:2302`), **before** the `_is_member_index_place` check, two new arms (so the member path is untouched):

```python
def _emit_place_address_of(self, place: Place, /) -> None:
    """Emit the address of *place* into the accumulator (``&place``).

    Handles the named-variable form (``&x``), the pointee form
    (``&*p`` / ``&*(T *)e`` == the pointer value), the scalar member
    forms (``&obj.field`` / ``&ptr->field``) and the element-address
    form (``&base.field[index]``).
    """
    if isinstance(place, VariablePlace):
        # ``&x`` — reproduces the legacy AddressOf codegen byte-for-byte:
        # locals lea their frame address, globals/constants mov their
        # label, and out_register parameters have no addressable storage.
        name = place.name
        if name in self.out_register_locals:
            message = f"cannot take address of out_register parameter '{name}'"
            raise CompileError(message, line=place.line)
        address = self._local_address(name)
        if name in self.locals:
            self.emit(f"        lea {self.target.acc}, [{address}]")
        else:
            self.emit(f"        mov {self.target.acc}, {address}")
        self.ax_local = None
        self.ax_is_byte = False
        return
    if isinstance(place, DereferencePlace):
        # ``&*p`` / ``&*(T *)e`` collapses to the pointer value itself —
        # evaluate the pointer expression into the accumulator.  No load
        # through the pointer happens (that would be the rvalue ``*p``).
        self.generate_expression(place.pointer)
        return
    if self._is_member_index_place(place):
        self.ax_clear()
        self._emit_member_index_access(place, address_of=True)
        return
    assert isinstance(place, MemberPlace)
    ... (rest unchanged) ...
```

**Byte-exactness note:** the legacy arm used `self._local_address(name)` and the literal accumulator from `self.target.acc`; the `lea`/`mov` distinction is on `name in self.locals`. Reproduced verbatim. The `&*p` arm matches C semantics (`&*p == p`) and is a NEW shape (no oracle).

### 2.3 `_emit_place_increment_decrement` — new `VariablePlace` and `SubscriptPlace(VariablePlace)` arms

The existing body (`generator.py:2374`) routes through `_emit_place_store` + `PlaceLoad`, which works for member/deref places but **not** for a bare `VariablePlace` or `SubscriptPlace(VariablePlace)` (those raise in `_resolve_place`). Add two arms at the top of the method:

```python
def _emit_place_increment_decrement(self, node: PlaceIncDec, /) -> None:
    """Emit a postfix/prefix ``++`` / ``--`` over a Place.

    Named variables and named-array elements reproduce the legacy
    IncrementDecrement / IndexAssign lowering byte-for-byte; member and
    dereference places synthesize ``place = place ± delta`` through
    :meth:`_emit_place_store`, reload with a :class:`PlaceLoad`, and — for
    the postfix form — recover the pre-update value with one ``sub`` / ``add``.
    """
    place = node.place
    delta_value = abs(node.delta)
    if isinstance(place, VariablePlace):
        # ``x++`` / ``++x`` — byte-identical to the legacy IncrementDecrement
        # expression codegen: lower ``x ± 1`` through emit_store_local, reload
        # x into the accumulator, then recover the pre-update value for postfix.
        target = place.name
        self._check_defined(target, line=node.line)
        update_expression = BinaryOperation(
            left=Var(line=node.line, name=target),
            line=node.line,
            operation="+" if node.delta > 0 else "-",
            right=Int(line=node.line, value=delta_value),
        )
        self.emit_store_local(expression=update_expression, name=target)
        self.generate_expression(Var(line=node.line, name=target))
        if node.is_postfix:
            reverse = "sub" if node.delta > 0 else "add"
            self.emit(f"        {reverse} {self.target.acc}, {delta_value}")
            self.ax_clear()
        return
    if isinstance(place, SubscriptPlace) and isinstance(place.base, VariablePlace):
        # ``a[i]++`` / ``a[i]--`` on a NAMED array.  _resolve_place does not
        # model SubscriptPlace(VariablePlace) (Plan 5), so lower the store
        # through the existing IndexAssign codegen — exactly the way the
        # named-variable arm lowers through emit_store_local — and reload the
        # element with an Index read.  Postfix recovers the pre-update value.
        array_name = place.base.name
        self._check_defined(array_name, line=node.line)
        update_expression = BinaryOperation(
            left=Index(array=Var(line=node.line, name=array_name), index=place.index, line=node.line),
            line=node.line,
            operation="+" if node.delta > 0 else "-",
            right=Int(line=node.line, value=delta_value),
        )
        self.generate_index_assign(
            IndexAssign(
                array=Var(line=node.line, name=array_name),
                expr=update_expression,
                index=place.index,
                line=node.line,
            )
        )
        self.generate_expression(Index(array=Var(line=node.line, name=array_name), index=place.index, line=node.line))
        if node.is_postfix:
            reverse = "sub" if node.delta > 0 else "add"
            self.emit(f"        {reverse} {self.target.acc}, {delta_value}")
            self.ax_clear()
        return
    update_expression = BinaryOperation(
        left=PlaceLoad(line=node.line, place=place),
        line=node.line,
        operation="+" if node.delta > 0 else "-",
        right=Int(line=node.line, value=delta_value),
    )
    self._emit_place_store(place, update_expression)
    self.generate_expression(PlaceLoad(line=node.line, place=place))
    if node.is_postfix:
        reverse = "sub" if node.delta > 0 else "add"
        self.emit(f"        {reverse} {self.target.acc}, {delta_value}")
        self.ax_clear()
```

**The `VariablePlace` arm is byte-identical to legacy `IncrementDecrement`** (same `emit_store_local` + `Var` reload + `sub`/`add`). **The `SubscriptPlace(VariablePlace)` arm is a NEW shape** (no oracle); its `IndexAssign` lowering reuses the existing, well-tested store path; the postfix `sub`/`add` recovers the pre-update value the same way. Deref-rooted subscripts (`p->arr[i]++`) fall through to the existing `_emit_place_store` arm unchanged.

Required imports already present in `generator.py` (`Index`, `IndexAssign`, `BinaryOperation`, `Int`, `Var`, `SubscriptPlace`, `VariablePlace`, `PlaceLoad`) — verify before use.

### 2.4 `PlaceCall` codegen (NEW — none exists today)

Add a method `_emit_place_call` to `generator.py` (alphabetical placement: after `_emit_place_address_of`, before `_emit_place_increment_decrement`). Two place shapes:

```python
def _emit_place_call(self, node: PlaceCall, /, *, discard_return: bool = False) -> None:
    """Generate a call through a function-pointer *place*.

    Two shapes:

    - ``SubscriptPlace(VariablePlace(array), index)`` — ``array[index](args)``.
      Reproduces the legacy generate_indexed_call byte-for-byte (global vs.
      local array, constant vs. variable index, pusha/save path, cdecl arg
      push order, discard_return cleanup).
    - ``DereferencePlace(pointer)`` — ``(*fp)(args)``.  The callee is the
      pointer value: save pinned registers, push args cdecl, evaluate the
      pointer expression into the accumulator, ``call acc``, clean up.
    """
    place = node.place
    if isinstance(place, SubscriptPlace) and isinstance(place.base, VariablePlace):
        self.generate_indexed_call(
            IndexedCall(args=node.args, array=Var(line=node.line, name=place.base.name), index=place.index, line=node.line),
            discard_return=discard_return,
        )
        return
    if isinstance(place, DereferencePlace):
        self._emit_place_call_through_pointer(node, discard_return=discard_return)
        return
    message = "unsupported Place shape in _emit_place_call"
    raise CompileError(message, line=node.line)
```

> **Wait — `IndexedCall` is being deleted in step 6.** So the indexed-call body must NOT depend on the `IndexedCall` *node* surviving. Two-stage approach: (a) in step 3 we keep `IndexedCall` alive and have `_emit_place_call` delegate to `generate_indexed_call(IndexedCall(...))` (proves byte-exactness against the oracle while the legacy node still exists); (b) in step 6, when we delete `IndexedCall`, we change `generate_indexed_call`'s signature to take the array name / index / args directly (or accept the `PlaceCall` node), and `_emit_place_call` calls it without constructing an `IndexedCall`. The simplest end state: rename `generate_indexed_call(statement, *, discard_return)` to read its inputs from explicit keyword args `generate_indexed_call(*, array_name, index, arguments, line, discard_return)` and have `_emit_place_call` pass `place.base.name`, `place.index`, `node.args`, `node.line`. The statement/expression dispatch then call `_emit_place_call` directly. **Plan: do the byte-exact proof in step 3 with the node still present; refactor the signature in step 6 atomically with the deletion.**

The deref-through-pointer body (NEW shape, modelled on the pointer-variable tail of `generate_indexed_call` but evaluating an arbitrary pointer expression):

```python
def _emit_place_call_through_pointer(self, node: PlaceCall, /, *, discard_return: bool = False) -> None:
    """Call through ``(*pointer_expression)(args)``.

    The callee address is the pointer value.  Mirrors the indirect-call
    register-save / cdecl-push sequence of generate_indexed_call but
    evaluates an arbitrary pointer expression into the accumulator
    instead of computing a base+index*stride element address.
    """
    self.si_local = None
    clobbers: frozenset[str] = frozenset(self.target.register_pool)
    saved = self._pinned_registers_to_save(clobbers)
    use_pusha = discard_return and len(saved) >= 3
    if use_pusha:
        self.emit("        pusha")
    else:
        for register in saved:
            self.emit(f"        push {register}")
    for argument in reversed(node.args):
        self._emit_push_arg(argument)
    self.generate_expression(node.place.pointer)  # acc = callee address
    self.emit(f"        call {self.target.acc}")
    if node.args:
        self.emit(f"        add {self.target.stack_register}, {len(node.args) * self.target.int_size}")
    if use_pusha:
        self.emit("        popa")
    else:
        for register in reversed(saved):
            self.emit(f"        pop {register}")
    self.ax_clear()
```

Dispatch wiring (replacing the `IndexedCall` arms in step 5/6):
- Expression form, `emission.py:2730` region: `elif isinstance(expression, PlaceCall): self._emit_place_call(expression)`.
- Statement form, `emission.py:4099` region: `elif isinstance(statement, PlaceCall): self._emit_place_call(statement, discard_return=True); self.ax_clear()`.

### 2.5 `_expression_type` — `PlaceAddressOf` arm

Add to `_expression_type` (`generator.py:2742`, alphabetical among the `isinstance` arms — place after the `Place` arm or wherever `PlaceAddressOf` sorts; the arms are ordered by node name, so `PlaceAddressOf` goes right before `PlaceLoad`):

```python
if isinstance(node, PlaceAddressOf):
    # ``&place`` — a pointer to the place's declared type.  Reproduces the
    # legacy AddressOf("<type> *") result so sizeof(&x) is unchanged.
    return f"{self._place_type(node.place)} *"
```

For `&x`, `_place_type(VariablePlace(x))` returns `variable_types[x]`, so the result is `"<type> *"` — byte-identical to legacy `AddressOf` which produced `f"{self._expression_type(node.var)} *"` (`_expression_type(Var)` and `_place_type(VariablePlace)` both read `variable_types`). Confirmed equivalent. The existing `if isinstance(node, Place):` arm does NOT catch `PlaceAddressOf` (it's a `Node`, not a `Place`), so this new arm is required and is not shadowed.

### 2.6 Liveness — `PlaceAddressOf` handler (the highest-risk change)

In `cc/codegen/liveness.py`, `_add_expression_uses` (alphabetical: `PlaceAddressOf` before `PlaceLoad` @169):

```python
if isinstance(expression, PlaceAddressOf):
    # ``&place`` references the place's addressing Vars.  ``&x`` records x
    # exactly as the legacy AddressOf handler did (so auto-pin's
    # address-taken disqualification is unchanged); deref-rooted places
    # record their pointer/index Vars.  Member-rooted places fall through
    # to _add_place_uses' raise, leaving auto-pin off for them exactly as
    # before this plan (legacy PlaceAddressOf had no liveness handler).
    if isinstance(expression.place, VariablePlace):
        accumulator.add(expression.place.name)
        return
    self._add_place_uses(expression.place, accumulator)
    return
```

**Why this exact shape:** legacy `AddressOf(VariablePlace-equivalent)` added the name. Legacy `&obj.field` was already `PlaceAddressOf` with NO liveness handler → it raised → auto-pin off. So:
- `PlaceAddressOf(VariablePlace)` → add name (reproduces `AddressOf` use). ✔
- `PlaceAddressOf(DereferencePlace ...)` (the NEW `&*p`) → `_add_place_uses` records pointer Vars (consistent with how `*p` reads already model their uses; this is a NEW shape so no oracle, but it must not raise spuriously for shapes that should be analyzable).
- `PlaceAddressOf(MemberPlace ...)` / `PlaceAddressOf(SubscriptPlace(MemberPlace))` → `_add_place_uses` **raises** (member-rooted) → auto-pin off, **exactly preserving** the pre-plan behaviour where `&obj.field` raised. ✔

**DO NOT add a `PlaceIncDec` or `PlaceCall` liveness handler.** Legacy `IncrementDecrement` and `IndexedCall` had none and raised → auto-pin off for those functions. After folding, `PlaceIncDec(VariablePlace)` and `PlaceCall(...)` must keep raising to preserve that. The `_add_expression_uses` / `_collect_use_def` final `raise` already covers them since no arm matches. **Verify** no accidental arm catches them.

> **Risk callout:** the folded `&x` is the only node whose liveness use/def behaviour must change-to-stay-the-same. If the `PlaceAddressOf(VariablePlace)` arm is missing or records the wrong name, auto-pin flips on a function that previously had it off (or vice versa), and output diverges. Plans 2/3 demonstrated this exact failure mode. **This is verified empirically by the userland differential in step 7.**

### 2.7 IR — pass-through, Value alias, `_ir_value_to_ast`, IndexedCall case

- `cc/ir.py:27`: `Value = int | str | ast_nodes.PlaceAddressOf` (was `ast_nodes.AddressOf`).
- `cc/ir.py:528`: `case ast_nodes.AddressOf(): return expr` → `case ast_nodes.PlaceAddressOf(place=ast_nodes.VariablePlace()): return expr`. **Only `&name` should pass through** (so `generate_call` sees it for out_register detection); other `PlaceAddressOf` shapes (`&obj.field`, `&*p`) should fall to the default temp+Block (they already do today as `PlaceAddressOf`). Verify by reading the current behaviour: today `PlaceAddressOf(MemberPlace)` hits the default `case _` and becomes a temp+Block — preserve that. So the guard `place=ast_nodes.VariablePlace()` is correct.
- `cc/ir.py:722`: delete the explicit `case ast_nodes.IndexedCall(): out.append(Block(node=stmt))` (redundant with the default `case _: out.append(Block(node=stmt))`; after folding, `PlaceCall` statements fall to the default Block — verify the default still wraps them). Actually `PlaceCall` as a *statement* must reach the AST codegen via Block — the default `case _` does that. As an *expression* (rvalue `x = fp[i]()`), `_build_expr`'s default (`:534`) wraps it in a temp+Block. Both correct. **Confirm** `PlaceCall` is never matched by an earlier case.
- `emission.py:1452` `_ir_value_to_ast`: `if isinstance(value, AddressOf): return value` → `if isinstance(value, PlaceAddressOf): return value`.

### 2.8 Migrate every other `AddressOf` consumer to `address_of_variable_name`

Mechanical, one site at a time, each guarded by `address_of_variable_name(...) is not None` and reading the returned name. Full enumerated list in step 6. The fast paths in `generator.py:1319/1382/3653` test `isinstance(fast_path_target, AddressOf) and fast_path_target.var.name in self.locals` → `(_n := address_of_variable_name(fast_path_target)) is not None and _n in self.locals`. SSA `ssa.py:226` `("a", value.var.name)` → `("a", address_of_variable_name(value))`. `loops.py` four helpers → same pattern.

### 2.9 Parser — fold construction + enable new shapes

**Fold (existing shapes, byte-identical AST → byte-identical codegen):**
- `parser.py:2276`: `return AddressOf(...)` → `return PlaceAddressOf(line=line, place=VariablePlace(line=line, name=name_token[1]))`.
- `parser.py:919` (postfix expr): `return IncrementDecrement(...)` → `return PlaceIncDec(delta=delta, is_postfix=True, line=line, place=VariablePlace(line=line, name=name))`.
- `parser.py:968` (postfix stmt): → `PlaceIncDec(delta=delta, is_postfix=True, line=token[2], place=VariablePlace(line=token[2], name=token[1]))`.
- `parser.py:2204` (prefix): → `PlaceIncDec(delta=delta, is_postfix=False, line=line, place=VariablePlace(line=line, name=name_token[1]))`.
- `parser.py:2511` (prefix, second site): same shape.
- `parser.py:862` (indexed call expr): → `PlaceCall(args=arguments, line=line, place=SubscriptPlace(line=line, base=VariablePlace(line=line, name=name), index=index))`.
- `parser.py:979` (indexed call stmt): → `PlaceCall(args=arguments, line=token[2], place=SubscriptPlace(line=token[2], base=VariablePlace(line=token[2], name=token[1]), index=index_expression))`.

**Enable new shapes:**
- **`a[i]++` / `a[i]--` expression** — in `_parse_ident_primary` (`parser.py:810` LBRACKET branch), after `self.eat("RBRACKET")` and before the `DOT/ARROW` check at 814, OR specifically as a new branch before the `LPAREN`/`Index` fallthrough at 859/863, add:
  ```python
  if self.peek()[0] in ("PLUS_PLUS", "MINUS_MINUS"):
      operator_token = self.eat()
      delta = self._delta_from_operator(operator_token[0])
      return PlaceIncDec(
          delta=delta,
          is_postfix=True,
          line=line,
          place=SubscriptPlace(line=line, base=VariablePlace(line=line, name=name), index=index),
      )
  ```
  Placement: after the inner `if self.peek()[0] == "LBRACKET":` chained-subscript handling (so `a[i][j]++` is handled by adding the same check after the chained `RBRACKET`), and before `if self.peek()[0] == "LPAREN":`.
- **`a[i][j]++`** — in the chained-subscript branch (`parser.py:841`), after building the `SubscriptPlace(DereferencePlace(Index(...)), inner_index)`, check for `PLUS_PLUS`/`MINUS_MINUS` before returning the `PlaceLoad`; if present, return `PlaceIncDec(... place=SubscriptPlace(DereferencePlace(Index(Var(name), index)), inner_index))`. This is a deref-rooted subscript → flows through the existing `_emit_place_store` arm of `_emit_place_increment_decrement` (NOT the named-array `IndexAssign` lowering).
- **`a[i]++;` / `a[i][j]++;` statement** — in the LBRACKET statement branch (`parser.py:969`), after `self.eat("RBRACKET")` (line 974) and before the `LPAREN` indexed-call check (975), peek for `PLUS_PLUS`/`MINUS_MINUS`; if present, eat operator + `SEMI` and return the matching `PlaceIncDec`. The chained `[j]` statement variant requires peeking for a second `LBRACKET` first.
- **`(*fp)(args)`** — in `parse_primary`'s parenthesized-expression tail (`parser.py:2315`, after `expression = self.parse_expression(); self.eat("RPAREN")`), add a check: if `self.peek()[0] == "LPAREN"` and `expression` is a `PlaceLoad` over a `DereferencePlace` (the `*fp` shape, parsed via `_parse_star_primary` → `Index(fp, 0)`... **caveat:** `*fp` currently desugars to `Index(fp, 0)` at `parser.py:1437`, NOT a `DereferencePlace`). For `(*fp)(args)` to become `PlaceCall(DereferencePlace(Var(fp)))`, we need the parenthesized `*fp` to be recognizable. Cleanest: detect `expression` being `Index(array=Var, index=Int(0))` OR a `PlaceLoad(DereferencePlace(...))`, and reconstruct the callee pointer expression. **Decision:** handle the common `(*fp)(args)` where `fp` is a named function pointer: if `self.peek()[0] == "LPAREN"` and `expression` is `Index(array=Var(name), index=Int(value=0))`, emit `PlaceCall(args=self.parse_arguments-after-LPAREN, place=DereferencePlace(pointer=Var(name)))`. Document that more complex callee expressions (`(*(arr+1))(args)`) are out of scope.

**`&*p` (NEW):** the `AMP` branch (`parser.py:2237`) currently does `self.eat("IDENT")` immediately after `AMP`, so `&*p` (where the next token is `STAR`) is NOT parsable today (it would error). Add, before the `name_token = self.eat("IDENT")` at 2239: if `self.peek()[0] == "STAR"`, parse the dereference primary and wrap: `self.eat("STAR"); ... build DereferencePlace from the pointer ...; return PlaceAddressOf(place=DereferencePlace(pointer=<pointer expr>))`. For the simple `&*p` and `&*(T*)e` cases, reuse `_parse_star_primary`'s logic to extract the pointer expression, then take `PlaceAddressOf(DereferencePlace(pointer))`. **Simplest correct form:** `&*p` → `PlaceAddressOf(DereferencePlace(Var(p)))`; `&*(T*)e` → `PlaceAddressOf(DereferencePlace(Cast(...)))`. The `_emit_place_address_of` DereferencePlace arm then just evaluates the pointer expression.

---

## 3. TDD task order

> Mirrors Plans 1–3: capture legacy output FIRST, build construction tests, extend codegen and prove against a hand-built-AST oracle BEFORE the parser flip, add new-shape golden coverage, flip the parser, delete the legacy nodes, final gate.

### Task group A — Baseline & legacy capture

- [ ] **A1.** Confirm the repo is green at the starting commit. Run:
  ```
  python3 tests/test_cc_place.py
  python3 -m pytest tests/unit/test_cc_liveness.py tests/unit/test_cc_codegen.py tests/unit/test_cc_ir.py tests/unit/test_cc_ssa.py tests/unit/test_cc_loops.py -q
  ```
  Expected: `PASS  index_member golden byte-identical`; pytest all-pass. Record the exact pass counts.

- [ ] **A2.** Build the userland-differential baseline. Compile **every** `user/**/*.c` with the current compiler and save the emitted asm under a scratch dir keyed by source path. Use a throwaway shell loop (no committed file needed for the baseline — it lives only in the work tree during execution):
  ```
  python3 - <<'PY'
  import subprocess, pathlib, hashlib, json
  root = pathlib.Path(".")
  cc = root / "cc.py"
  inc = root / "user" / "libbboeos" / "include"
  out = {}
  for src in sorted(root.glob("user/**/*.c")):
      asm = src.with_suffix(".baseline.asm")
      r = subprocess.run(["python3", str(cc), "--bits", "32", "-I", str(inc), str(src), "/dev/stdout"],
                         capture_output=True, text=True)
      out[str(src)] = (r.returncode, hashlib.sha256(r.stdout.encode()).hexdigest(), r.stdout)
  pathlib.Path("/tmp/userland_baseline.json").write_text(json.dumps({k:(v[0],v[1]) for k,v in out.items()}))
  for k,v in out.items():
      pathlib.Path(f"/tmp/ub_{hashlib.sha1(k.encode()).hexdigest()}.asm").write_text(v[2])
  print("captured", len(out), "files")
  PY
  ```
  (This writes only to `/tmp` — read-only constraint on the *repo* is respected; the diff harness is allowed to write scratch under `/tmp` during execution. If `/dev/stdout` is unsupported by the CLI, write to a temp `.asm` and read it back.)
  Expected: `captured 50 files` (or current count); every returncode 0. **Record the count.**

- [ ] **A3.** Extend the `FIXTURE` in `tests/test_cc_place.py` with the EXISTING shapes about to be folded, captured from the LEGACY compiler. Append a new comment block and probes:
  ```c
  /* --- Plan 4 fold probes (captured from the legacy compiler) --- */
  int g_counter;
  int (*g_fptable[4])(int);
  int probe_addr_of_global(void) { return (int)&g_counter; }
  int probe_addr_of_local(void) { int local; local = 0; return (int)&local; }
  int probe_postinc_expr(int n) { int a = n++; return a + n; }
  int probe_preinc_expr(int n) { int a = ++n; return a + n; }
  int probe_postdec_expr(int n) { int a = n--; return a + n; }
  int probe_predec_expr(int n) { int a = --n; return a + n; }
  void probe_postinc_stmt(void) { g_counter++; }
  void probe_predec_stmt(void) { --g_counter; }
  int probe_indexed_call_global_const(int x) { return g_fptable[1](x); }
  int probe_indexed_call_global_var(int i, int x) { return g_fptable[i](x); }
  int probe_indexed_call_local_const(int (*t[4])(int), int x) { return t[2](x); }
  int probe_indexed_call_local_var(int (*t[4])(int), int i, int x) { return t[i](x); }
  void probe_indexed_call_stmt(int x) { g_fptable[0](x); }
  int probe_sizeof_addr(int n) { return sizeof(&n); }
  ```
  Then regenerate the golden against the **legacy** compiler:
  ```
  BBOE_UPDATE_GOLDEN=1 python3 tests/test_cc_place.py
  python3 tests/test_cc_place.py
  ```
  Expected: `WROTE golden ...` then `PASS  index_member golden byte-identical`. **Commit:** "test(cc): capture legacy &x / x++ / arr[i]() output in the Place golden".
  (Adjust probe shapes if the fixture's function-pointer-array syntax needs the libbboeos header forms — verify each probe compiles under the legacy compiler before committing. If `int (*t[4])(int)` as a *parameter* is unsupported, fall back to `g_fptable`-only local-array probes via a local `int (*t[4])(int);` declaration assigned from globals.)

### Task group B — Place-node construction (parser) unit tests, NOT yet flipped

- [ ] **B1.** Add `tests/unit/test_cc_parser_place_fold.py` (pytest-style, mirroring `test_cc_liveness.py`'s import + assertion style). For each NEW shape that the parser will eventually produce, assert the constructed AST is the expected Place tree. **These tests are written now but xfail/skip until the parser flip (step 5)** — mark them `@pytest.mark.skip(reason="enabled in step 5 parser flip")` initially and unskip in B-final. Cover: `&x`, `x++`, `++x`, `arr[i](args)`, `a[i]++`, `a[i][j]++`, `(*fp)(x)`, `&*p`. Use `cc.parser.Parser` directly (read how existing parser unit tests instantiate it — check `tests/unit/` for a parser test harness; if none, parse via the lexer pipeline as `test_cc_codegen.py` does).
  Expected (when unskipped): all assert the exact dataclass shapes from §2.9.
  **Commit:** "test(cc): Place-fold parser construction tests (skipped pending flip)".

### Task group C — Codegen extension + hand-built-AST oracle (BEFORE parser flip)

The strategy: build the new emitter arms, then prove byte-exactness using a **temporary oracle test** that hand-builds the folded Place AST and compiles it, asserting byte-identity against the legacy golden probes. Because the parser is not yet flipped, we exercise the new code paths via a direct AST → codegen harness.

- [ ] **C1.** Add the shared helper `address_of_variable_name` to `cc/ast_nodes.py` (§2.1). Run `python3 -c "import cc.ast_nodes"`. Expected: no error. **Commit:** "feat(cc): add address_of_variable_name Place helper".

- [ ] **C2.** Add the `VariablePlace` and `DereferencePlace` arms to `_emit_place_address_of` (`generator.py:2302`, §2.2). Add the `PlaceAddressOf` arm to `_expression_type` (`generator.py:2742`, §2.5). Run `python3 -c "import cc.codegen.x86.generator"`. Expected: no error.

- [ ] **C3.** Add the `VariablePlace` and `SubscriptPlace(VariablePlace)` arms to `_emit_place_increment_decrement` (`generator.py:2374`, §2.3). Run import check.

- [ ] **C4.** Add `_emit_place_call` and `_emit_place_call_through_pointer` to `generator.py` (§2.4), delegating the indexed-call shape to the existing `generate_indexed_call(IndexedCall(...))` (legacy node still alive). Run import check.

- [ ] **C5.** Add the liveness `PlaceAddressOf` handler to `cc/codegen/liveness.py` (§2.6). Run `python3 -m pytest tests/unit/test_cc_liveness.py -q`. Expected: all pass (no regression — the new arm only adds coverage for a node that previously raised in `&obj.field` and now still raises for member shapes).

- [ ] **C6.** **Oracle test (temporary).** Add `tests/unit/test_cc_place_fold_oracle.py`: for each legacy probe in §1.1–1.3 (e.g. `&g`, `&local`, `x++`, `++x`, `arr[i](args)`), build BOTH the legacy node AST and the folded Place AST for the same function, compile each through the codegen, and `assert legacy_asm == folded_asm`. This proves the new emitter arms reproduce the legacy output byte-for-byte WITHOUT touching the parser. Use the codegen entry point used by `tests/unit/test_cc_codegen.py` (read it for the exact API to build a `Program` and emit asm).
  Run: `python3 -m pytest tests/unit/test_cc_place_fold_oracle.py -q`.
  Expected: all pass. If any byte differs, fix the emitter arm (do NOT touch the golden). **Commit:** "feat(cc): Place codegen arms for &x / x++ / arr[i]() + byte-exact oracle".

- [ ] **C7.** Wire the dispatch for `PlaceCall` (NOT yet removing `IndexedCall`): add `elif isinstance(expression, PlaceCall): self._emit_place_call(expression)` at `emission.py:2730` region and `elif isinstance(statement, PlaceCall): self._emit_place_call(statement, discard_return=True); self.ax_clear()` at `emission.py:4099` region. (Import `PlaceCall` into `emission.py` if not already.) Run import check + the oracle test again. Expected: pass.

### Task group D — New-shape golden coverage (capture NEW output, eyeball correctness)

- [ ] **D1.** Append NEW-shape probes to the `FIXTURE` (these do NOT compile today, so they are added AFTER the parser flip is staged — but we capture them via the same golden once the parser is flipped in step 5). **Sequencing note:** D1 probes require the parser flip, so D is interleaved: write the probes here as a comment block but regenerate the golden only after E (parser flip). Probes:
  ```c
  /* --- Plan 4 new-shape probes (no legacy oracle; runtime-verified) --- */
  int probe_addr_deref(int *p) { return (int)&*p; }
  int probe_array_elem_postinc(int *a, int i) { int pre = a[i]++; return pre + a[i]; }  /* if a[i] on a pointer param; for a named array use a local */
  int g_arr[8];
  int probe_named_array_postinc(int i) { g_arr[i] = 5; int pre = g_arr[i]++; return pre + g_arr[i]; }
  int probe_named_array_predec(int i) { g_arr[i] = 5; return --g_arr[i]; }
  int *g_rows[4];
  int probe_double_index_postinc(int i, int j) { return g_rows[i][j]++; }
  int probe_call_through_ptr(int (*fp)(int), int x) { return (*fp)(x); }
  ```
  (Reconcile `a[i]++` probes so the named-array `SubscriptPlace(VariablePlace)` path AND the deref-rooted `SubscriptPlace(DereferencePlace)` / pointer-param path are each exercised; the named-array path goes through the `IndexAssign` lowering, the pointer/double-index path through `_emit_place_store`.)

### Task group E — Flip the parser

- [ ] **E1.** Apply all fold edits in `parser.py` (§2.9 fold list): `&name`, postfix/prefix `++`/`--` (all four sites), indexed-call expr+stmt. Run the **oracle test** and `tests/test_cc_place.py` (golden still legacy-captured):
  ```
  python3 -m pytest tests/unit/test_cc_place_fold_oracle.py -q
  python3 tests/test_cc_place.py
  ```
  Expected: oracle pass; **`PASS  index_member golden byte-identical`** (the folded shapes must produce byte-identical output to the legacy golden captured in A3). If the golden FAILS, a fold path is not byte-exact — debug via the failing-line diff the test prints. **Commit:** "feat(cc): fold &x / x++ / arr[i]() construction onto Place in the parser".

- [ ] **E2.** Add the new-shape parser paths (§2.9 enable list): `a[i]++` / `a[i][j]++` (expr + stmt), `(*fp)(args)`, `&*p`. Unskip the B1 construction tests. Run:
  ```
  python3 -m pytest tests/unit/test_cc_parser_place_fold.py -q
  ```
  Expected: all pass.

- [ ] **E3.** Regenerate the golden to capture the NEW-shape probes (D1) and re-assert:
  ```
  BBOE_UPDATE_GOLDEN=1 python3 tests/test_cc_place.py
  python3 tests/test_cc_place.py
  ```
  **Eyeball the new asm for the new probes** (read the regenerated `tests/golden/cc_place_index_member.asm` diff): verify `&*p` emits just the pointer load (no extra deref); `a[i]++` emits a store-then-reload sequence; `(*fp)(x)` emits the pointer-value `call acc`. Expected: `PASS`. **Commit:** "feat(cc): enable &*p / (*fp)(args) / a[i]++ / a[i][j]++ + golden coverage".

- [ ] **E4.** **Userland differential (interim).** Re-run the A2 harness comparing against `/tmp/userland_baseline.json`. Expected: **every file's (returncode, sha256) identical** to baseline (only folds applied so far; new shapes don't appear in userland). Any mismatch = a fold path diverged → debug before proceeding. **This is the gate that catches a liveness/auto-pin flip.**

### Task group F — Delete the legacy nodes and migrate remaining consumers

- [ ] **F1.** Migrate every remaining `AddressOf` consumer to `address_of_variable_name` (§2.8), file by file, running the relevant unit test after each file:
  - `cc/codegen/x86/emission.py`: 1452 (`_ir_value_to_ast` → `PlaceAddressOf`), 1472 (purity tuple: drop `AddressOf`, keep `PlaceAddressOf`), 2420/2421/2478/2481/2500 (out_register capture → `address_of_variable_name`). Remove the `AddressOf` import only after the `:2653` arm is deleted.
  - `cc/codegen/x86/generator.py`: 1319/1320, 1382/1383, 2749 (delete the `AddressOf` arm — `PlaceAddressOf` arm added in C2 now covers it), 2850/2851, 2861/2862, 3653/3654/3656, 4069/4070, 4074/4076/4083, 4187/4188.
  - `cc/codegen/base.py`: 396/397, 597, 786, 791, 1027.
  - `cc/loops.py`: 160/161/163, 539/540, 632/633, 689.
  - `cc/ssa.py`: 87/88, 226/227, 230.
  After each file: `python3 -c "import <module>"` then the matching unit test (`test_cc_codegen`, `test_cc_loops`, `test_cc_ssa`). Expected: green.

- [ ] **F2.** Delete the legacy codegen arms and the `IndexedCall` signature refactor:
  - `emission.py:2653` (`AddressOf` expression arm) — delete.
  - `emission.py:2704` (`IncrementDecrement` expression arm) — delete.
  - `emission.py:2730` (`IndexedCall` expression arm) — delete (replaced by the `PlaceCall` arm from C7).
  - `emission.py:4089` (`IncrementDecrement` statement arm) — delete.
  - `emission.py:4099` (`IndexedCall` statement arm) — delete (replaced by `PlaceCall` statement arm).
  - Refactor `generate_indexed_call(self, statement: IndexedCall, ...)` → `generate_indexed_call(self, *, array_name: str, index: Node, arguments: list[Node], line: int, discard_return: bool = False)`. Replace every `statement.array.name` → `array_name`, `statement.index` → `index`, `statement.args` → `arguments`, `statement.line` → `line`. Update `_emit_place_call` (§2.4) to call it with explicit kwargs from `place.base.name` / `place.index` / `node.args` / `node.line` (no `IndexedCall` construction).
  - Remove `AddressOf`, `IncrementDecrement`, `IndexedCall` from `emission.py` imports.

- [ ] **F3.** `cc/ir.py`: line 27 Value alias → `PlaceAddressOf`; line 528 `case ast_nodes.AddressOf()` → `case ast_nodes.PlaceAddressOf(place=ast_nodes.VariablePlace())`; delete line 722 `case ast_nodes.IndexedCall()`. Remove now-unused `IndexedCall` references. Run `python3 -m pytest tests/unit/test_cc_ir.py tests/unit/test_cc_ir_optimize.py -q`. Expected: green.

- [ ] **F4.** `cc/codegen/liveness.py`: delete the `AddressOf` arm at line 138 (now covered by the `PlaceAddressOf` arm). Remove the `AddressOf` import. Run `python3 -m pytest tests/unit/test_cc_liveness.py -q`. Expected: green.

- [ ] **F5.** `cc/parser.py`: remove the `AddressOf`, `IncrementDecrement`, `IndexedCall` imports (all construction sites flipped in E1).

- [ ] **F6.** `cc/ast_nodes.py`: delete the `AddressOf` (@49), `IncrementDecrement` (@374), `IndexedCall` (@408) class definitions. Run a global grep to confirm zero remaining references:
  ```
  grep -rn "AddressOf\b\|IncrementDecrement\|IndexedCall" cc/ tests/ | grep -v "PlaceAddressOf"
  ```
  Expected: only `address_of_variable_name` (the helper) and `PlaceAddressOf` (different token) remain; **no bare `AddressOf`, `IncrementDecrement`, `IndexedCall`**. **Commit:** "refactor(cc): delete AddressOf / IncrementDecrement / IndexedCall; migrate all consumers to Place".

- [ ] **F7.** Delete the temporary oracle test `tests/unit/test_cc_place_fold_oracle.py` (it imports the now-deleted legacy nodes). The byte-exactness it proved is now permanently locked by the golden + userland differential. **Commit:** "test(cc): drop temporary fold oracle (superseded by golden + userland diff)".

### Task group G — Final gate

- [ ] **G1.** Golden: `python3 tests/test_cc_place.py` → `PASS  index_member golden byte-identical`.

- [ ] **G2.** Full unit suite:
  ```
  python3 -m pytest tests/unit/ -q
  ```
  Expected: all pass; record count.

- [ ] **G3.** **Userland differential (final).** Re-run the A2 harness against `/tmp/userland_baseline.json`. Expected: **every pre-existing file byte-identical** (returncode + sha256). New shapes do not appear in baseline userland, so all 50 must match. **Zero diffs allowed.**

- [ ] **G4.** Assembler self-host: `python3 tests/test_asm.py`. Expected: **49/49**. (This is slow; allow up to the 600000ms timeout, run in background if needed and Monitor for exit.)

- [ ] **G5.** Program runtime correctness: run `tests/test_programs.py` for the bbfs and ext2 targets per its CLI (read the file header for the exact invocation; typically `python3 tests/test_programs.py` or a `--fs` selector). Expected: **bbfs green, ext2 green**.

- [ ] **G6.** Self-review pass (§4 below), then final commit if any review fixes landed.

---

## 4. Self-review checklist (run before declaring done)

- [ ] Every `isinstance(..., AddressOf)` site (the ~30 enumerated in §1.1 / step F1) is migrated; `grep -rn "AddressOf\b" cc/ | grep -v PlaceAddressOf | grep -v address_of_variable_name` returns nothing.
- [ ] The `PlaceAddressOf(VariablePlace)` liveness arm adds the name; member-rooted `PlaceAddressOf` still raises (auto-pin unchanged). No `PlaceIncDec` / `PlaceCall` liveness arm was added (they must keep raising).
- [ ] `_expression_type(PlaceAddressOf)` returns `"<type> *"` with the space; `sizeof(&x)` byte-identical (golden `probe_sizeof_addr`).
- [ ] `_emit_place_increment_decrement` `VariablePlace` arm is byte-identical to legacy `IncrementDecrement` (emit_store_local + Var reload + sub/add); the `SubscriptPlace(VariablePlace)` arm reuses `generate_index_assign` and does NOT route through `_resolve_place`.
- [ ] `_emit_place_call` indexed shape reproduces `generate_indexed_call` byte-for-byte (global/local/pointer × const/var index × pusha/save × discard_return); the deref shape is the new `(*fp)(args)` pointer-value call.
- [ ] IR `Value` alias, `_build_expr` pass-through guard (`PlaceAddressOf(place=VariablePlace())` only), and `_ir_value_to_ast` all reference `PlaceAddressOf`; the redundant `IndexedCall` IR case is gone.
- [ ] Alphabetical method ordering preserved in `generator.py` (`_emit_place_address_of` < `_emit_place_call` < `_emit_place_call_through_pointer` < `_emit_place_increment_decrement` < `_emit_place_load` < `_emit_place_store`).
- [ ] No new hard-coded registers (except the verbatim legacy `[ebp-...]` literals already in `_emit_place_address_of`).
- [ ] `a[i]++` index re-evaluation caveat documented in the `_emit_place_increment_decrement` `SubscriptPlace` arm comment.
- [ ] Golden regenerated only at A3 (legacy) and E3 (new shapes added); never to mask a fold divergence.
- [ ] Userland differential: 0 diffs (G3).
- [ ] `test_asm` 49/49 (G4); `test_programs` bbfs+ext2 green (G5).

---

### Critical Files for Implementation
- /home/ubuntu/bboeos/.claude/worktrees/parser/cc/codegen/x86/generator.py
- /home/ubuntu/bboeos/.claude/worktrees/parser/cc/codegen/x86/emission.py
- /home/ubuntu/bboeos/.claude/worktrees/parser/cc/parser.py
- /home/ubuntu/bboeos/.claude/worktrees/parser/cc/codegen/liveness.py
- /home/ubuntu/bboeos/.claude/worktrees/parser/cc/ast_nodes.py

(Plus the secondary migration surface — `cc/ir.py`, `cc/codegen/base.py`, `cc/loops.py`, `cc/ssa.py` — and the test files `tests/test_cc_place.py` + `tests/golden/cc_place_index_member.asm`.)