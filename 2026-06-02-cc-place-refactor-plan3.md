# Plan 3 — Convert the Full Dereference Node Family onto the Recursive `Place` AST

**Branch:** `bboe/cc-place-plan3` (off `main`) · **One plan, one PR**
**Gate:** byte-exact. A golden snapshot of every converted shape must stay byte-identical. Any change in emitted assembly is a bug, never blessed. Full `tests/test_asm.py` (49/49) and `tests/test_programs.py` (bbfs + ext2) green. A before/after differential of all 50 userland `.c` files must be byte-identical.

This plan is written for an engineer with **zero prior context**. Every step has the exact file path, the exact line region, complete code (no placeholders, no `...`), an exact command, and the expected output. Commit after every green step.

---

## 0. Orientation (read this once, do not skip)

`cc.py` is a self-hosting C compiler for BBoeOS living in the `cc/` package. The relevant modules:

| File | Role |
|---|---|
| `cc/ast_nodes.py` | `@dataclass(kw_only=True, slots=True)` AST node definitions, **alphabetical** |
| `cc/parser.py` | recursive-descent parser; builds AST nodes |
| `cc/ir.py` | optional IR/SSA lowering. Has an **escape hatch**: any statement/expression it does not explicitly match falls through `case _:` to `Block(node=stmt)` (statement) or a `temp = Block(Assign(...))` (expression), which hands the node straight to the AST code generator. The six legacy nodes we convert all flow through this hatch today, so this work stays in **parser + generator + emission + liveness** — confirmed below. |
| `cc/codegen/x86/generator.py` | the `Place`-aware core: `_resolve_place`, `_emit_place_load`, `_emit_place_store`, `_emit_place_address_of`, `_emit_place_increment_decrement`, `_place_type`, `_expression_type`, member helpers |
| `cc/codegen/x86/emission.py` | legacy per-shape emitters and the big statement/expression dispatch `if/elif` chains; `_is_pure_expression`; `_emit_pointer_bump` |
| `cc/codegen/liveness.py` | use/def analysis for the auto-pin optimization; raises `LivenessAnalysisError` on unmodeled node shapes (caller swallows it and skips the optimization) |

### The IR escape hatch — confirmed

`cc/ir.py:534` (`_build_expr`) ends with:
```python
            case _:
                # Complex: use a temp + Block to let AST codegen handle it.
                temp = self._tmp()
                out.append(Block(node=ast_nodes.Assign(expr=expr, name=temp)))
                return temp
```
and `_build_stmt` has no arms for `DerefAssign`, `DerefIncrement`, `DerefIncrementAssign`, `PointerDereference`, `PointerDereferenceAssign`, or `DoubleIndex`, so they all hit its `case _:` → `Block(node=stmt)` (the statement default appends `Block(node=stmt)`). **No IR/SSA changes are needed in this plan.** The one IR reference is the cosmetic helper `_assign_rhs_field_name` (`cc/ir.py:366-375`), which special-cases `PointerDereferenceAssign` — it will be removed in the deletion step.

### What already exists (Plans 1 & 2, merged)

- The recursive `Place` tree: `Place` (base), `VariablePlace(name)`, `DereferencePlace(pointer: Node)`, `SubscriptPlace(base: Place, index: Node)`, `MemberPlace(base: Place, member_name)`.
- Operation nodes: `PlaceLoad(place)` (IntegerOperand), `PlaceStore(place, value)`, `PlaceAddressOf(place)`, `PlaceIncDec(delta, is_postfix, place)` (IntegerOperand), `PlaceCall(args, place)`.
- `_resolve_place` (`generator.py:3473-3520`) handles **only** two struct-array shapes today (`arr[i].member`, `arr[i].member[j]`); every other shape raises `"unsupported Place shape in _resolve_place"`.
- `DereferencePlace` already exists but is consumed **only** nested inside `MemberPlace` for arrow access: `p->f` parses to `MemberPlace(base=DereferencePlace(pointer=VariablePlace("p")), member_name="f")`. A **standalone** `DereferencePlace` reaching `_resolve_place` / `_emit_place_load` / `_emit_place_store` is **not** handled yet — that is the heart of this plan.

### What Plan 3 converts, then deletes (from `cc/ast_nodes.py`)

| Legacy node | C syntax |
|---|---|
| `DerefAssign(expr, pointer: Var)` | `*p = expr;` (named pointer) |
| `DerefIncrement(delta, is_postfix, target_name)` | `*p++` / `*++p` etc. as an rvalue |
| `DerefIncrementAssign(delta, expr, is_postfix, target_name)` | `*p++ = expr;` etc. |
| `PointerDereference(expression, target_type)` | `*(T *)expr` read |
| `PointerDereferenceAssign(address, target_type, value)` | `*(T *)expr = value;` |
| `DoubleIndex(array: Var, outer_index, inner_index)` | `name[i][j]` (array of pointers) |

---

## 1. Reconnaissance findings — verbatim legacy contracts (byte-output per shape)

These are reproduced **verbatim** from the current tree. The conversion must produce byte-identical assembly to each. Quote them in the PR description.

### 1.1 `*(T *)e` read — `PointerDereference` — `emission.py:656-689`

```python
    def _emit_pointer_dereference(self, expression: PointerDereference) -> None:
        inner = expression.expression
        if isinstance(inner, AddressOf) and inner.var.name in self.locals:
            address = f"[{self._local_address(inner.var.name)}]"
            if expression.target_type == "unsigned char":
                self.emit_byte_load_zx(address)
            elif expression.target_type == "unsigned short" and self.target.int_size > 2:
                self.emit(f"        movzx {self.target.acc}, word {address}")
            else:
                self.emit(f"        mov {self.target.acc}, {address}")
            self.ax_clear()
            return
        self.generate_expression(inner)
        address_register = self.target.acc
        if expression.target_type == "unsigned char":
            self.emit(f"        mov al, [{address_register}]")
            self.emit_accumulator_zx_from_al()
        elif expression.target_type == "unsigned short" and self.target.int_size > 2:
            self.emit(f"        movzx {address_register}, word [{address_register}]")
        else:
            self.emit(f"        mov {address_register}, [{address_register}]")
        self.ax_clear()
```

**Contract.**
- **Fast path** (`AddressOf(Var)` of a local): fold `*(T*)&local` to a direct frame load `[ebp-N]`, no `lea`. Width: `unsigned char` → `emit_byte_load_zx`; `unsigned short` (when `int_size > 2`) → `movzx acc, word [..]`; else `mov acc, [..]`. Trailing `ax_clear()`.
- **General path**: evaluate inner into `acc` (an address), then load through `acc`. `unsigned char` → `mov al, [acc]` + `emit_accumulator_zx_from_al()`; `unsigned short` (int_size>2) → `movzx acc, word [acc]`; else `mov acc, [acc]`. Trailing `ax_clear()`.

Note the byte-path asymmetry: fast path uses `emit_byte_load_zx`, general path uses `mov al,[r]` + `emit_accumulator_zx_from_al`. **Both must be reproduced exactly** — they can differ in emitted bytes.

### 1.2 `*(T *)e = v` write — `PointerDereferenceAssign` — `emission.py:691-723`

```python
    def _emit_pointer_dereference_assign(self, statement: PointerDereferenceAssign) -> None:
        self.generate_expression(statement.value)
        accumulator = self.target.acc
        if isinstance(statement.address, AddressOf) and statement.address.var.name in self.locals:
            destination = f"[{self._local_address(statement.address.var.name)}]"
            if statement.target_type == "unsigned char":
                self.emit(f"        mov {destination}, {self.target.low_byte(accumulator)}")
            elif statement.target_type == "unsigned short" and self.target.int_size > 2:
                self.emit(f"        mov word {destination}, {self.target.low_word(accumulator)}")
            else:
                self.emit(f"        mov {destination}, {accumulator}")
            return
        scratch = self.target.si_register
        self.emit(f"        push {accumulator}")
        self.generate_expression(statement.address)
        self.emit(f"        mov {scratch}, {accumulator}")
        self.emit(f"        pop {accumulator}")
        if statement.target_type == "unsigned char":
            self.emit(f"        mov [{scratch}], {self.target.low_byte(accumulator)}")
        elif statement.target_type == "unsigned short" and self.target.int_size > 2:
            self.emit(f"        mov word [{scratch}], {self.target.low_word(accumulator)}")
        else:
            self.emit(f"        mov [{scratch}], {accumulator}")
```

**Contract.** Evaluate value into `acc` FIRST. Fast path (`AddressOf(Var)` local): store directly to `[ebp-N]` at width, **no trailing `ax_clear()`** (note: the statement dispatch arm appends `ax_clear()` after; the assign-expr path does not). General path: `push acc`; eval address into `acc`; `mov si, acc`; `pop acc`; store through `[si]` at width.

### 1.3 `name[i][j]` — `DoubleIndex` — `emission.py:1244-1308`

```python
    def _generate_double_index_expression(self, expression: DoubleIndex, /) -> None:
        self.ax_clear()
        vname = expression.array.name
        self._check_defined(vname, line=expression.line)
        outer_load = Index(array=expression.array, index=expression.outer_index, line=expression.line)
        self.generate_expression(outer_load)
        si = self.target.si_register
        self.emit(f"        mov {si}, {self.target.acc}")
        element_type = self.variable_types.get(vname, "")
        if element_type.endswith("*"):
            pointee = element_type[:-1].rstrip()
            try:
                inner_size = self.target.type_size(pointee)
            except KeyError:
                inner_size = self.target.int_size
        else:
            inner_size = self.target.int_size
        is_byte_inner = inner_size == 1
        inner = expression.inner_index
        if isinstance(inner, Int):
            offset = inner.value * (1 if is_byte_inner else inner_size)
            mem = f"{si}+{offset}" if offset else si
            if is_byte_inner:
                self.emit_byte_load_zx(f"[{mem}]")
            else:
                self.emit(f"        mov {self.target.acc}, [{mem}]")
        elif isinstance(inner, Var):
            self.generate_expression(inner)
            if not is_byte_inner:
                self._emit_scale_index(self.target.acc, scale=inner_size)
            self.emit(f"        add {si}, {self.target.acc}")
            if is_byte_inner:
                self.emit_byte_load_zx(f"[{si}]")
            else:
                self.emit(f"        mov {self.target.acc}, [{si}]")
        else:
            self.emit(f"        push {si}")
            self.generate_expression(inner)
            if not is_byte_inner:
                self._emit_scale_index(self.target.acc, scale=inner_size)
            self.emit(f"        pop {si}")
            self.emit(f"        add {si}, {self.target.acc}")
            if is_byte_inner:
                self.emit_byte_load_zx(f"[{si}]")
            else:
                self.emit(f"        mov {self.target.acc}, [{si}]")
        self.ax_clear()
```

**Contract.** Two-stage. Stage 1: `outer = Index(array, outer_index)` loaded into `acc` (the pointer). Stage 2: `mov si, acc`; inner stride = stripping one `*` off `variable_types[vname]` and consulting `target.type_size(pointee)` (NOT `_index_pointee_size`, which is array-aware), fallback `int_size`. Three sub-paths: `Int` (fold offset into displacement, no SI push), `Var` (no push, scale acc then `add si, acc`), general (`push si` / eval / scale / `pop si` / `add si, acc`). Byte-inner uses `emit_byte_load_zx([..])`, else full `mov acc, [..]`. Leading and trailing `ax_clear()`.

### 1.4 `*p = v` statement — `DerefAssign` — `emission.py:4174-4205`

```python
        elif isinstance(statement, DerefAssign):
            if statement.pointer.name in self.out_register_locals:
                reg = self.out_register_locals[statement.pointer.name]
                self.generate_expression(statement.expr)
                source = self.target.acc
                if len(reg) < len(source):
                    source = self.target.low_word(source)
                if reg != source:
                    self.emit(f"        mov {reg}, {source}")
                self.ax_clear()
            else:
                holder_type = self.variable_types.get(statement.pointer.name)
                if not holder_type or not holder_type.endswith("*"):
                    message = f"pointer dereference write to non-pointer variable '{statement.pointer.name}'"
                    raise CompileError(message, line=statement.line)
                pointee_type = holder_type[:-1]
                self.generate_expression(statement.expr)
                self._emit_load_var(statement.pointer.name, register=self.target.si_register)
                if pointee_type in self.BYTE_TYPES:
                    self.emit(f"        mov [{self.target.si_register}], {self.target.low_byte(self.target.acc)}")
                else:
                    self.emit(f"        mov [{self.target.si_register}], {self.target.acc}")
                self.ax_clear()
```

**Contract.**
- **`out_register_locals` path** (p is an out-register param): eval expr → `acc`; if `reg` narrower than `acc` use `low_word(acc)` as source; `mov reg, source` (skip if equal); `ax_clear()`. **No pointer write at all** — register aliasing.
- **Generic path**: pointee width = strip one `*` off `holder_type` (note: `holder_type[:-1]`, **not** `.rstrip()` — so `"char *"[:-1]` = `"char "`; the `in self.BYTE_TYPES` membership test must still match — see trap #2). eval expr → `acc`; `_emit_load_var(name, register=si)`; store `low_byte` if byte pointee, else full `acc`, through `[si]`; `ax_clear()`.

### 1.5 `*p++ = v` statement — `DerefIncrementAssign` — `emission.py:4206-4235`

```python
        elif isinstance(statement, DerefIncrementAssign):
            target = statement.target_name
            self._check_defined(target, line=statement.line)
            holder_type = self.variable_types.get(target)
            if not holder_type or not holder_type.endswith("*"):
                message = f"'*{target}++' / '*{target}--' write requires a pointer; got '{holder_type}'"
                raise CompileError(message, line=statement.line)
            pointee_type = holder_type[:-1].rstrip()
            if not statement.is_postfix:
                self._emit_pointer_bump(delta=statement.delta, line=statement.line, name=target)
            self.generate_expression(statement.expr)
            self._emit_load_var(target, register=self.target.si_register)
            if pointee_type in self.BYTE_TYPES:
                self.emit(f"        mov [{self.target.si_register}], {self.target.low_byte(self.target.acc)}")
            elif pointee_type == "unsigned short" and self.target.int_size > 2:
                self.emit(f"        mov word [{self.target.si_register}], {self.target.low_word(self.target.acc)}")
            else:
                self.emit(f"        mov [{self.target.si_register}], {self.target.acc}")
            if statement.is_postfix:
                self._emit_pointer_bump(delta=statement.delta, line=statement.line, name=target)
            self.ax_clear()
```

**Contract.** Prefix: bump **first**, then eval+store. Postfix: eval+store, then bump. Store goes through `si` (acc survives). Pointee width: byte → `low_byte`; `unsigned short` (int_size>2) → `mov word [si], low_word`; else full. `pointee_type` here uses `.rstrip()`. Trailing `ax_clear()`.

### 1.6 `*p++` rvalue — `DerefIncrement` — `emission.py:2798-2828`

```python
        elif isinstance(expression, DerefIncrement):
            target = expression.target_name
            self._check_defined(target, line=expression.line)
            if expression.is_postfix:
                self.generate_expression(
                    Index(
                        array=Var(line=expression.line, name=target),
                        index=Int(line=expression.line, value=0),
                        line=expression.line,
                    )
                )
                self._emit_pointer_bump(delta=expression.delta, line=expression.line, name=target)
            else:
                self._emit_pointer_bump(delta=expression.delta, line=expression.line, name=target)
                self.ax_clear()
                self.generate_expression(
                    Index(
                        array=Var(line=expression.line, name=target),
                        index=Int(line=expression.line, value=0),
                        line=expression.line,
                    )
                )
```

**Contract.** Desugars `*p` to `Index(Var(target), Int(0))`. Postfix: load `*p`, then bump. Prefix: bump, `ax_clear()`, then load `*p`. **No trailing `ax_clear()` in the postfix branch** (the load already cleared); prefix relies on the `Index` load's own clears.

### 1.7 `_emit_pointer_bump` — `emission.py:617-654`

```python
    def _emit_pointer_bump(self, *, delta: int, line: int, name: str) -> None:
        holder_type = self.variable_types.get(name)
        if not holder_type or not holder_type.endswith("*"):
            message = f"postfix '*{name}++' / '*{name}--' requires a pointer; got '{holder_type}'"
            raise CompileError(message, line=line)
        pointee_type = holder_type[:-1].rstrip()
        try:
            pointee_size = self.target.type_size(pointee_type) if pointee_type else 1
        except KeyError:
            pointee_size = self.target.int_size
        bump = pointee_size * delta
        operation = "add" if bump >= 0 else "sub"
        amount = abs(bump)
        if name in self.pinned_register:
            register = self.pinned_register[name]
            if amount == 1:
                self.emit(f"        {'inc' if bump > 0 else 'dec'} {register}")
            else:
                self.emit(f"        {operation} {register}, {amount}")
        else:
            address = self._local_address(name)
            width = self.target.word_size
            if amount == 1:
                self.emit(f"        {'inc' if bump > 0 else 'dec'} {width} [{address}]")
            else:
                self.emit(f"        {operation} {width} [{address}], {amount}")
```

This helper is **kept** (it operates on the pointer variable by name and never touches `acc`). The increment forms continue to call it.

### 1.8 AssignExpr inner chain — `emission.py:864-922`

Arms for `DerefAssign` (890-904), `DerefIncrementAssign` (905-910), `PointerDereferenceAssign` (918-919), and the existing `PlaceStore` arm (913-917, which routes to `_emit_place_store` with a bitfield guard). The three legacy arms must be **subsumed** by the existing `PlaceStore` arm after conversion — verify the value-in-AX behavior is byte-identical (trap #7).

Critically the `DerefAssign` AssignExpr arm re-evaluates `inner.expr` only when it is `(Int, Var)`:
```python
            self.generate_statement(inner)
            if isinstance(inner.expr, (Int, Var)):
                self.generate_expression(inner.expr)
```
and `DerefIncrementAssign` does NOT re-eval. The `PlaceStore` arm calls `_emit_place_store(inner.place, inner.value)` directly. We must confirm `(*p=v)` and `(*p++=v)` as expressions stay byte-identical under the PlaceStore arm (trap #7); if they diverge, special-case in the PlaceStore arm.

### 1.9 Supporting helpers (read-only references, no edits)

- `_place_type` (`generator.py:3000-3047`) — `DereferencePlace` arm (3029-3034) strips one `*`:
  ```python
        if isinstance(place, DereferencePlace):
            pointer_type = self._expression_type(place.pointer)
            if not pointer_type.endswith("*"):
                message = f"sizeof: cannot dereference non-pointer type '{pointer_type}'"
                raise CompileError(message, line=place.line)
            return pointer_type[:-1].rstrip()
  ```
  Already correct for standalone `DereferencePlace` — `sizeof(*p)` works.
- `_expression_type` (`generator.py:2534-2584`) — `PlaceLoad` arm (2562-2566) returns `_place_type(node.place)`; bare `Place` arm (2567-2570) returns `_place_type(node)`; `PointerDereference` arm (2571-2572) returns `target_type`. After conversion `*(T*)e` becomes `PlaceLoad(DereferencePlace(Cast(...)))`; `_place_type` strips the cast's `*` → identical type string. **No edit needed** if widths match (verify in trap #2).
- `_resolve_member_place_info` (`generator.py:3385-3471`) — its `DereferencePlace`-of-`VariablePlace` arm (3424-3433) and `DereferencePlace`-of-other arm (3437-3455, with the `Cast(AddressOf(local))` fast path at 3445-3451). **Unchanged**; arrow member access keeps using it.
- `_emit_load_var` (`generator.py:1637-1665`), `emit_byte_load_zx` (`generator.py:4716+`), `_local_address` (`generator.py:2731-2756`), `_index_pointee_size` (`base.py:480-522`), `_emit_scale_index` (`emission.py:735+`), `_build_address` / `_emit_field_load` / `_emit_field_store` (`generator.py:512`, `1286`, `1302`) — read-only references.

### 1.10 Place mapping the parser must produce

| C | Place tree |
|---|---|
| `*p = v` | `PlaceStore(DereferencePlace(pointer=Var("p")), v)` |
| `*p` read | **today parses to `Index(Var("p"), Int(0))`** (see `_parse_star_primary` `parser.py:1436-1452`). Convert to `PlaceLoad(DereferencePlace(pointer=Var("p")))`. |
| `*(T*)e` read | `PlaceLoad(DereferencePlace(pointer=<inner expr, e.g. Cast or AddressOf>))` |
| `*(T*)e = v` | `PlaceStore(DereferencePlace(pointer=<inner expr>), v)` |
| `name[i][j]` | `PlaceLoad(SubscriptPlace(base=DereferencePlace(pointer=Index(array=Var(name), index=i)), index=j))` — **`Index` stays a plain expression; do not convert it.** |
| `*p++` / `*++p` | `PlaceLoad(DereferencePlace(Var("p")))` + `_emit_pointer_bump`, postfix load-then-bump / prefix bump-then-load |
| `*p++ = v` etc. | `PlaceStore(DereferencePlace(Var("p")), v)` + `_emit_pointer_bump`, same ordering |

**Decision (locked):** the increment forms are NOT modeled as `PlaceIncDec` (that synthesizes `place = place ± delta`, which is pointer arithmetic on the *pointee*, wrong). They are modeled as a `PlaceLoad`/`PlaceStore` over `DereferencePlace(Var)` **plus** a direct `_emit_pointer_bump` call on the pointer variable, in the exact order the legacy code used. We keep dedicated dispatch arms (`DerefIncrement` / `DerefIncrementAssign` stay as AST nodes that the parser produces, OR we introduce thin new nodes). **Re-decision below in §3.5** — to keep the deletion clean we retain the two increment nodes' *parser output shape* but route their codegen through the Place core; see §3.5 for the exact mechanism.

---

## 2. The byte-exact traps (design around each — call out explicitly)

**Trap 1 — Purity / `_is_pure_expression`.** Today `PlaceLoad` returns `True` unconditionally (`emission.py:1628-1629`); but legacy `PointerDereference`, `DoubleIndex`, and `DerefIncrement` returned `False` (they fall through to the final `return False`). After conversion these all become `PlaceLoad(...)`. If purity flips to `True`, `_try_emit_conditional_via_cond_value` may elide a then-branch it should re-evaluate, changing condition/guarded-update codegen. **Design:** make `PlaceLoad` purity precise — pure for member shapes (today's behavior: `MemberPlace`, including arrow `MemberPlace(DereferencePlace(Var), f)`, and `SubscriptPlace` over a `MemberPlace`, and the struct-array `arr[i].f`), **impure** when the place's outermost addressing terminal is a standalone `DereferencePlace` or a `SubscriptPlace` whose base chain bottoms out at a standalone `DereferencePlace`. Lock with golden coverage (a deref read inside an `if`).

**Trap 2 — Width selection for `DereferencePlace`.** Must reproduce: `*(unsigned char*)e` → byte zero-extend; `*(unsigned short*)e` (int_size>2) → `movzx word`; else full. For `*p` named-pointer, width = strip-one-`*` of holder type (`char*`→byte). The new `_resolve_place` `DereferencePlace` arm computes `field_size` from `_place_type(place)` (the pointee type string) via the **same** byte/word/full decision used by `_emit_pointer_dereference`. Note the legacy `DerefAssign` generic path uses `holder_type[:-1]` (no rstrip) for its `in BYTE_TYPES` test, while `_place_type` uses `.rstrip()`; verify `BYTE_TYPES` membership and `type_size` agree for `"char"`, `"unsigned char"`, `"unsigned short"`. Confirm `_place_type(DereferencePlace(...))` yields the identical width the legacy path used for each shape via the oracle test.

**Trap 3 — `AddressOf(Var)`-local FAST PATHS.** Both reads and writes fold `*(T*)&local` directly to `[ebp-N]` (no `lea`/scratch). Reproduce inside the new `DereferencePlace` load/store path keyed on `DereferencePlace.pointer` being `AddressOf(Var in self.locals)` (and the cast-wrapped `Cast(AddressOf(...))` form — note `_emit_pointer_dereference` checks the *inner* of the cast because the parser stores `operand.expression`, i.e. the cast already stripped; verify by inspecting what the parser produces for `*(uchar*)&local`). Match the exact isinstance checks. Also reproduce the general path's `push acc` / eval / `mov si, acc` / `pop acc` ordering for the write, and the `mov acc, [acc]` self-load for the read.

**Trap 4 — `out_register_locals` path in `DerefAssign`.** `*p = v` where `p` is an out-register param writes the register directly (narrowing alias, no pointer store). The new `_emit_place_store` `DereferencePlace(VariablePlace)` arm must check `self.out_register_locals` FIRST and reproduce byte-for-byte.

**Trap 5 — `DoubleIndex` inner-size + 3 sub-paths.** Pointee stride from stripping element-type `*` and `target.type_size(pointee)` (NOT `_index_pointee_size`); `Int` (fold offset), `Var` (no push), general (push/pop SI). Reproduce exactly, including `emit_byte_load_zx` vs full `mov`.

**Trap 6 — Pointer-bump ordering.** Prefix bumps first; postfix bumps after; the bump runs on the pinned register / frame slot via `_emit_pointer_bump` and never touches `acc`.

**Trap 7 — AssignExpr-as-value.** `(*p=v)`, `(*p++=v)`, `(*(T*)e=v)` as expressions must keep value-in-AX behavior. After conversion these become `AssignExpr(inner=PlaceStore(...))`, routed through the existing `PlaceStore` arm (`emission.py:913-917`). Verify byte-identity; if the `DerefAssign` re-eval-on-trivial-RHS behavior differs, replicate it.

**Trap 8 — Liveness.** Add `Place`-aware handlers for `PlaceLoad`/`PlaceStore` over `DereferencePlace` and `SubscriptPlace`-over-`DereferencePlace`, plus the increment forms, reproducing legacy use/def sets:
- `DerefAssign`: uses `pointer.name` + expr.
- `PointerDereferenceAssign`: uses `address` + value.
- `DoubleIndex`: uses `array` + both indices.
- `DerefIncrement` / `DerefIncrementAssign`: **previously had NO handler** → fell through to `LivenessAnalysisError` → auto-pin silently skipped. Closing the gap **enables** the optimization where it was off, which can **change output**. **This is the highest-risk item.** Verify empirically with the userland differential. If enabling diverges, scope the new handlers to match legacy behavior exactly (i.e., make them raise `LivenessAnalysisError` for the increment-over-deref shapes so the optimization stays off — matching prior behavior — but model the non-increment Place-over-deref shapes that previously worked via `DerefAssign`/`DoubleIndex`/`PointerDereferenceAssign`).

> **Note on the Plan-2 follow-up gap.** Plan 2 left `PlaceStore`/`PlaceIncDec`/`PlaceLoad` *entirely unmodeled* in liveness (there are zero `Place` references in `liveness.py` today — verified). So member-access functions already silently skip auto-pin. Plan 3 must **not widen** this gap and should **close** it for the shapes it owns, but only if byte-exactness holds. The default-safe posture is: add precise `Place` handlers that reproduce exactly the legacy use/def sets for the converted shapes, and verify the userland differential is empty. If any divergence appears, fall back to raising `LivenessAnalysisError` for the specific Place shapes that previously raised (the increment-over-deref shapes), keeping the optimization off where it was off.

---

## 3. Implementation — TDD task order (mirror Plan 2's discipline)

> Conventions enforced throughout: no abbreviations in new identifiers (spell out expression / index / register / pointer / value); alphabetical method & node ordering in their files; `@dataclass(kw_only=True, slots=True)`; preserve existing comments; use `self.target.acc` / `self.target.bx_register` / `self.target.si_register` / `self.target.low_byte` / `self.target.low_word`.

Test commands (run from repo root `/home/ubuntu/bboeos/.claude/worktrees/parser`):

- Golden: `python3 tests/test_cc_place.py`
- Regenerate golden: `BBOE_UPDATE_GOLDEN=1 python3 tests/test_cc_place.py`
- Place node unit tests: `python3 -m pytest tests/unit/test_cc_place_nodes.py -q`
- Full unit suite: `python3 -m pytest tests/unit -q`
- Reassembly: `python3 tests/test_asm.py`
- Programs (bbfs): `python3 tests/test_programs.py`
- Programs (ext2): `python3 tests/test_programs.py --filesystem ext2`

---

### Task 0 — Confirm clean baseline

- [ ] **Step 0.1.** Confirm the branch and clean tree.
  - Command: `git status --short && git log --oneline -1`
  - Expected: empty status; `HEAD` at `cb37cce8 feat(cc): unify the Member* AST family onto the recursive Place core`.
- [ ] **Step 0.2.** Confirm the existing golden passes and capture the program-suite baselines.
  - Command: `python3 tests/test_cc_place.py`
  - Expected: `PASS  index_member golden byte-identical`
  - Command: `python3 -m pytest tests/unit -q 2>&1 | tail -3`
  - Expected: all passed (record the count, e.g. `693 passed`).
  - Command: `python3 tests/test_asm.py 2>&1 | tail -3`
  - Expected: 49/49 green.
- [ ] **Step 0.3.** Capture the userland `.c` differential baseline (Plan-2 method). This is the empirical byte-exactness oracle for the whole plan.
  - Command (run and **keep the output directory**):
    ```
    mkdir -p /tmp/plan3_before
    for f in $(find user -name '*.c' | sort); do
      out=/tmp/plan3_before/$(echo "$f" | tr '/' '_').asm
      python3 cc.py --bits 32 -I user/libbboeos/include "$f" "$out" 2>/dev/null || echo "SKIP $f"
    done
    ls /tmp/plan3_before | wc -l
    ```
  - Expected: a count of generated `.asm` files (some `.c` may legitimately SKIP if they need flags; record which). Note: do this once; the comparison runs after the parser flip and again at the final gate.
  - No commit (read-only baseline).

---

### Task 1 — Expand the golden fixture to cover EVERY deref shape, captured from the LEGACY compiler

Goal: before changing any compiler code, extend `tests/test_cc_place.py`'s `FIXTURE` with probes for every deref shape, then regenerate the golden **from the unchanged legacy compiler**. This locks legacy bytes as the oracle.

- [ ] **Step 1.1.** Edit `tests/test_cc_place.py`. Append the following probe functions to the end of the `FIXTURE` string literal (immediately before the closing `"""` at current line 105, after `probe_addr_of_dot`). Keep existing probes intact. Use distinct, non-colliding function and global names. Complete additions:

  ```c
  /* --- Plan 3 deref-family probes (captured from the legacy compiler) --- */
  struct flags2 { unsigned char hi; unsigned char lo; };

  int probe_deref_read_char(char *p) { return *p; }
  int probe_deref_read_int(int *p) { return *p; }
  int probe_deref_store_char(char *p, int v) { *p = v; return *p; }
  int probe_deref_store_int(int *p, int v) { *p = v; return *p; }

  int probe_cast_deref_uchar_local(void) { struct flags2 s; s.hi = 7; return *(unsigned char *)&s; }
  int probe_cast_deref_ushort_local(void) { int box; box = 0; return *(unsigned short *)&box; }
  int probe_cast_deref_uchar_expr(char *base, int off) { return *(unsigned char *)(base + off); }
  void probe_cast_deref_store_uchar_local(int v) { struct flags2 s; *(unsigned char *)&s = v; }
  void probe_cast_deref_store_ushort_expr(char *base, int off, int v) { *(unsigned short *)(base + off) = v; }

  char *names[4];
  unsigned short *words[4];
  int probe_double_index_byte_const(void) { return names[1][2]; }
  int probe_double_index_byte_var(int i, int j) { return names[i][j]; }
  int probe_double_index_word_var(int i, int j) { return words[i][j]; }

  int probe_deref_postinc_read(int *p) { int a = *p++; return a; }
  int probe_deref_preinc_read(int *p) { int a = *++p; return a; }
  int probe_deref_postdec_read(char *p) { int a = *p--; return a; }
  void probe_deref_postinc_store(char *out, int v) { *out++ = v; }
  void probe_deref_preinc_store(int *out, int v) { *++out = v; }

  int probe_deref_in_if(int *p) { if (*p) { return 1; } return 0; }
  int probe_deref_assign_expr(int *p, int v) { int y = (*p = v); return y; }
  int probe_deref_incassign_expr(char *out, int v) { int y = (*out++ = v); return y; }
  int probe_sizeof_deref(int *p) { return sizeof(*p); }
  ```

  Notes per shape:
  - `probe_deref_read_*` exercise `*p` read (currently `Index(Var,Int(0))`).
  - `probe_cast_deref_uchar_local` / `probe_cast_deref_ushort_local` exercise the `AddressOf(Var)`-local fast path (trap 3) for byte and word widths.
  - `probe_cast_deref_uchar_expr` / `probe_cast_deref_store_ushort_expr` exercise the general (non-fast-path) read/write.
  - `probe_double_index_*` exercise byte (`char*[]`) and word (`unsigned short*[]`) pointee with const, Var, and general inner indices.
  - `probe_deref_*_read` / `probe_deref_*_store` exercise prefix/postfix increment read and assign for char (byte) and int (word) pointees.
  - `probe_deref_in_if` is the purity trap (trap 1).
  - `probe_deref_assign_expr` / `probe_deref_incassign_expr` are the AssignExpr-as-value trap (trap 7).
  - `probe_sizeof_deref` exercises `_place_type`/`_expression_type` for `sizeof(*p)`.

- [ ] **Step 1.2.** Also update the module docstring (lines 1-24) to mention the added deref-family probes (one sentence appended to the existing paragraph). Preserve the existing text.

- [ ] **Step 1.3.** Regenerate the golden **from the unchanged compiler** (this captures legacy bytes as the oracle):
  - Command: `BBOE_UPDATE_GOLDEN=1 python3 tests/test_cc_place.py`
  - Expected: `WROTE golden <repo>/tests/golden/cc_place_index_member.asm`
- [ ] **Step 1.4.** Re-run without the env var to confirm the golden is now self-consistent:
  - Command: `python3 tests/test_cc_place.py`
  - Expected: `PASS  index_member golden byte-identical`
- [ ] **Step 1.5.** Sanity-check the new golden contains the expected legacy instruction shapes (spot-check, read-only):
  - Command: `grep -nE "probe_double_index_byte_const|probe_cast_deref_uchar_local|probe_deref_postinc_read" tests/golden/cc_place_index_member.asm | head`
  - Expected: labels present.
- [ ] **Step 1.6.** Commit (golden is legacy-captured; no compiler change yet).
  - Branch is already a feature branch; commit directly.
  - Message:
    ```
    test(cc): expand Place golden with the full deref family (legacy capture)

    Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
    ```

> Why this is safe and meaningful: the golden now encodes the exact legacy bytes for every shape we will convert. Every later step re-runs `python3 tests/test_cc_place.py` and must stay PASS.

---

### Task 2 — Place-node construction tests for the deref shapes

Add structural tests mirroring `tests/unit/test_cc_place_nodes.py`'s style for the trees the parser will now build.

- [ ] **Step 2.1.** Edit `tests/unit/test_cc_place_nodes.py`. Append (alphabetical-ish, grouped at end) the following tests. Complete code:

  ```python
  def test_dereference_place_models_named_pointer_deref() -> None:
      """*p is PlaceLoad(DereferencePlace(Var(p)))."""
      load = ast_nodes.PlaceLoad(
          place=ast_nodes.DereferencePlace(pointer=ast_nodes.Var(name="p")),
      )
      assert isinstance(load.place, ast_nodes.DereferencePlace)
      assert load.place.pointer.name == "p"


  def test_dereference_place_store_models_named_pointer_write() -> None:
      """*p = v is PlaceStore(DereferencePlace(Var(p)), v)."""
      store = ast_nodes.PlaceStore(
          place=ast_nodes.DereferencePlace(pointer=ast_nodes.Var(name="p")),
          value=ast_nodes.Int(value=9),
      )
      assert isinstance(store.place, ast_nodes.DereferencePlace)
      assert store.value == ast_nodes.Int(value=9)


  def test_dereference_place_over_cast_models_pointer_dereference() -> None:
      """*(T *)e is PlaceLoad(DereferencePlace(Cast(...)))."""
      load = ast_nodes.PlaceLoad(
          place=ast_nodes.DereferencePlace(
              pointer=ast_nodes.Cast(
                  expression=ast_nodes.Var(name="e"),
                  target_type="unsigned char *",
              ),
          ),
      )
      assert isinstance(load.place.pointer, ast_nodes.Cast)
      assert load.place.pointer.target_type == "unsigned char *"


  def test_subscript_over_dereference_of_index_models_double_index() -> None:
      """name[i][j] is PlaceLoad(SubscriptPlace(DereferencePlace(Index(Var(name), i)), j))."""
      load = ast_nodes.PlaceLoad(
          place=ast_nodes.SubscriptPlace(
              base=ast_nodes.DereferencePlace(
                  pointer=ast_nodes.Index(
                      array=ast_nodes.Var(name="names"),
                      index=ast_nodes.Var(name="i"),
                  ),
              ),
              index=ast_nodes.Var(name="j"),
          ),
      )
      assert isinstance(load.place, ast_nodes.SubscriptPlace)
      assert isinstance(load.place.base, ast_nodes.DereferencePlace)
      assert isinstance(load.place.base.pointer, ast_nodes.Index)
      assert load.place.base.pointer.array.name == "names"
  ```

- [ ] **Step 2.2.** Run the node tests:
  - Command: `python3 -m pytest tests/unit/test_cc_place_nodes.py -q`
  - Expected: all passed (8 original + 4 new = 12).
- [ ] **Step 2.3.** Commit.
  - Message:
    ```
    test(cc): Place construction tests for the deref family shapes

    Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
    ```

---

### Task 3 — Extend the codegen core (no parser flip yet) and verify against the golden via a hand-built-AST oracle

This is the bulk of the work. We extend `_resolve_place` / `_emit_place_load` / `_emit_place_store` / `_emit_place_address_of` for standalone `DereferencePlace` and `SubscriptPlace`-over-`DereferencePlace`, add the purity predicate, the increment composition, and the liveness handlers. We verify each addition with an **oracle test** that builds the Place trees by a post-parse transform and asserts byte-identity against the still-legacy parser output — exactly the Plan-2 discipline. Because the parser is not flipped yet, the legacy dispatch arms still run for real source; the oracle test feeds hand-built Place trees through `generate_*` directly.

#### 3.1 Add the standalone `DereferencePlace` arm to `_resolve_place`

`_resolve_place` returns a `PlaceAddress` and may emit dynamic-offset code (into BX). But the deref-read/write fast paths (frame-direct, self-load through `acc`, `si`-scratch) do **not** fit the `PlaceAddress(const_base, offset, index)` model cleanly — the legacy code emits bespoke sequences. **Decision (locked):** do NOT force `DereferencePlace` through `PlaceAddress`. Instead, give `_emit_place_load` and `_emit_place_store` dedicated `DereferencePlace` branches that emit the legacy sequences verbatim, BEFORE they reach `_resolve_place`. `_resolve_place` is only extended for `SubscriptPlace`-over-`DereferencePlace` (the DoubleIndex shape), which composes naturally.

- [ ] **Step 3.1.1.** Add a private helper to classify a `DereferencePlace`'s width, mirroring the legacy width decision. In `cc/codegen/x86/generator.py`, add a method (alphabetical placement: between `_emit_place_address_of` ends at line 2204 and `_emit_place_increment_decrement` begins at 2206 — actually place alphabetically; `_dereference_place_width` sorts before `_emit_*`, so place it before `_emit_field_load` near the other `_d*`/`_e*` helpers; choose the location that keeps alphabetical order among `def _` methods). Complete method:

  ```python
      def _dereference_place_width(self, place: DereferencePlace, /) -> int:
          """Return the byte width of a load/store through a standalone ``DereferencePlace``.

          Reproduces the legacy ``_emit_pointer_dereference`` /
          ``DerefAssign`` width decision: the pointee type string
          (one ``*`` stripped from the pointer expression's type) maps
          to 1 for byte types, 2 for ``unsigned short`` on targets whose
          ``int_size`` exceeds 2, otherwise the full ``int_size``.
          """
          pointee_type = self._place_type(place)
          if pointee_type in self.BYTE_TYPES:
              return 1
          if pointee_type == "unsigned short" and self.target.int_size > 2:
              return 2
          return self.target.int_size
  ```

  Verify `_place_type(DereferencePlace(...))` (which already strips one `*` via `_expression_type(place.pointer)`) yields the pointee type string for all probe shapes. For `*(unsigned char *)e`, `place.pointer` is a `Cast` with `target_type="unsigned char *"`; `_expression_type(Cast)` returns the cast type; stripping one `*` gives `"unsigned char"`. For `*p` with `p: char *`, `_place_type` returns `"char"`. **This matches `BYTE_TYPES`** (which contains `"char"` and `"unsigned char"`). For the legacy `DerefAssign` generic path that used `holder_type[:-1]` without rstrip (`"char "`), confirm `"char " in BYTE_TYPES` vs `"char" in BYTE_TYPES` — `BYTE_TYPES` membership must be checked: read `BYTE_TYPES` definition and confirm.

  - Pre-step read: `grep -n "BYTE_TYPES" cc/codegen/base.py cc/codegen/x86/generator.py | head` → confirm exact contents include `"char"`, `"unsigned char"` (no trailing space). The new helper uses `_place_type` (rstripped), so it always tests `"char"`; the legacy generic-path bug-for-bug `"char "[:-1]` test must produce the same boolean. **Verify by oracle (3.4): if any byte/word shape diverges, the golden catches it.**

#### 3.2 Extend `_emit_place_load` for standalone `DereferencePlace`

- [ ] **Step 3.2.1.** In `cc/codegen/x86/generator.py`, edit `_emit_place_load` (currently 2228-2255). Insert a `DereferencePlace` branch **after** the two member-shape early returns (after line 2239, before `self.ax_clear()` at 2240). It must reproduce `_emit_pointer_dereference` byte-for-byte, keyed on the pointer expression. Complete inserted block:

  ```python
          if isinstance(place, DereferencePlace):
              self._emit_dereference_place_load(place)
              return
  ```

- [ ] **Step 3.2.2.** Add the `_emit_dereference_place_load` method (alphabetical placement near `_emit_dereference_*`). Complete method, reproducing `emission.py:656-689` exactly:

  ```python
      def _emit_dereference_place_load(self, place: DereferencePlace, /) -> None:
          """Load the value at a standalone ``DereferencePlace`` into the accumulator.

          Byte-for-byte reproduction of the legacy ``_emit_pointer_dereference``:
          the ``AddressOf(Var)``-of-local fast path folds ``*(T *)&local``
          to a direct frame load (no ``lea`` / scratch register); the general
          path evaluates the pointer expression into the accumulator and loads
          through it.  Width comes from :meth:`_dereference_place_width`.
          """
          pointer = place.pointer
          width = self._dereference_place_width(place)
          if isinstance(pointer, AddressOf) and pointer.var.name in self.locals:
              address = f"[{self._local_address(pointer.var.name)}]"
              if width == 1:
                  self.emit_byte_load_zx(address)
              elif width == 2 and self.target.int_size > 2:
                  self.emit(f"        movzx {self.target.acc}, word {address}")
              else:
                  self.emit(f"        mov {self.target.acc}, {address}")
              self.ax_clear()
              return
          self.generate_expression(pointer)
          address_register = self.target.acc
          if width == 1:
              self.emit(f"        mov al, [{address_register}]")
              self.emit_accumulator_zx_from_al()
          elif width == 2 and self.target.int_size > 2:
              self.emit(f"        movzx {address_register}, word [{address_register}]")
          else:
              self.emit(f"        mov {address_register}, [{address_register}]")
          self.ax_clear()
  ```

  > **Trap-3 detail.** The legacy `_emit_pointer_dereference` checked `isinstance(inner, AddressOf)` where `inner = expression.expression`. The parser for `*(T*)&local` produced `PointerDereference(expression=operand.expression, ...)` where `operand` was the `Cast` and `operand.expression` is the cast's inner — i.e. the `AddressOf`, **not** the `Cast`. So when we flip the parser (Task 4) we must build `DereferencePlace(pointer=<cast.expression>)` to preserve this — **NOT** `DereferencePlace(pointer=Cast(...))`. Width then comes from the cast's `target_type` which we must thread through. **Re-decision:** see §3.2.3.

- [ ] **Step 3.2.3.** **Width threading for casts.** The legacy `PointerDereference` carried `target_type` explicitly; the parser stripped the cast and stored only `operand.expression` as the address. If we build `DereferencePlace(pointer=cast.expression)` we LOSE the cast type and `_place_type` cannot recover the pointee width (it would type the inner expression, e.g. `&s` → `struct flags2 *` → `struct flags2`, wrong width). Therefore for the cast shapes the parser must build `DereferencePlace(pointer=Cast(expression=<inner>, target_type=<T*>))` — keep the `Cast` node as the pointer. Then:
  - `_place_type(DereferencePlace(Cast))` → `_expression_type(Cast)` → `target_type` (`"unsigned char *"`) → strip `*` → `"unsigned char"`. Correct width.
  - But the fast-path isinstance check (`pointer is AddressOf`) now fails because `pointer` is a `Cast`. The legacy fast path keyed on the **inner** `AddressOf`. So the fast-path check must also recognize `Cast(expression=AddressOf(Var in locals))`.

  Revise `_emit_dereference_place_load`'s fast-path guard to unwrap one optional `Cast`:

  ```python
          pointer = place.pointer
          width = self._dereference_place_width(place)
          fast_path_target = pointer.expression if isinstance(pointer, Cast) else pointer
          if isinstance(fast_path_target, AddressOf) and fast_path_target.var.name in self.locals:
              address = f"[{self._local_address(fast_path_target.var.name)}]"
              if width == 1:
                  self.emit_byte_load_zx(address)
              elif width == 2 and self.target.int_size > 2:
                  self.emit(f"        movzx {self.target.acc}, word {address}")
              else:
                  self.emit(f"        mov {self.target.acc}, {address}")
              self.ax_clear()
              return
          self.generate_expression(pointer)
          ...
  ```

  > Verify against the legacy: for `*(unsigned char *)&s`, the legacy emitted `emit_byte_load_zx([ebp-N])` (no `lea`). For the general path the legacy did `generate_expression(operand.expression)` — i.e. it evaluated the **inner**, not the cast. `generate_expression(Cast)` is identity codegen (`emission.py:2791-2795`: it just generates the inner), so `generate_expression(pointer)` where `pointer` is the `Cast` produces the identical bytes as `generate_expression(cast.expression)`. **Byte-identical.** The golden confirms.

#### 3.3 Extend `_emit_place_store` for standalone `DereferencePlace`

- [ ] **Step 3.3.1.** In `_emit_place_store` (currently 2257-2282), insert after the two member early returns (after line 2264, before `allowed = ...` at 2265):

  ```python
          if isinstance(place, DereferencePlace):
              self._emit_dereference_place_store(place, value)
              return
  ```

- [ ] **Step 3.3.2.** Add `_emit_dereference_place_store`. It must reproduce **two** legacy paths depending on the pointer shape:
  - **Named pointer `*p = v`** (`pointer` is `Var(name)` and `name in self.locals`/params, no cast/addressof): reproduce `DerefAssign` (`emission.py:4174-4205`), including the `out_register_locals` fast path (trap 4) and the `_emit_load_var(name, register=si)` store.
  - **Cast / arbitrary expression `*(T*)e = v`**: reproduce `PointerDereferenceAssign` (`emission.py:691-723`), including the `AddressOf(Var)`-local fast path (trap 3) and the `push acc / si-scratch / pop acc` general path.

  How to discriminate: the named-pointer form is `DereferencePlace(pointer=Var(name))`; the cast form is `DereferencePlace(pointer=Cast(...))` or any non-`Var` pointer. **But** note `*p = v` with `p` a plain `int *` local and `*(int*)p = v` are different legacy paths emitting potentially different bytes — the named form uses `_emit_load_var` (which can read a pinned register directly), the cast form evaluates the expression and uses `si`. We must route a bare `Var` pointer to the `DerefAssign` reproduction and everything else to the `PointerDereferenceAssign` reproduction. Complete method:

  ```python
      def _emit_dereference_place_store(self, place: DereferencePlace, value: Node, /) -> None:
          """Store *value* through a standalone ``DereferencePlace``.

          Two byte-for-byte legacy reproductions selected by the pointer
          expression shape:

          - ``*p = v`` where *p* is a named pointer (``DereferencePlace`` of a
            bare :class:`Var`): the legacy ``DerefAssign`` path, including the
            ``out_register`` register-alias write and the ``_emit_load_var`` /
            ESI store at pointee width.
          - ``*(T *)e = v`` (cast or arbitrary address expression): the legacy
            ``PointerDereferenceAssign`` path, including the
            ``AddressOf(Var)``-of-local fast store and the general
            push / ESI-scratch / pop sequence.
          """
          pointer = place.pointer
          if isinstance(pointer, Var):
              pointer_name = pointer.name
              if pointer_name in self.out_register_locals:
                  register = self.out_register_locals[pointer_name]
                  self.generate_expression(value)
                  source = self.target.acc
                  if len(register) < len(source):
                      source = self.target.low_word(source)
                  if register != source:
                      self.emit(f"        mov {register}, {source}")
                  self.ax_clear()
                  return
              holder_type = self.variable_types.get(pointer_name)
              if not holder_type or not holder_type.endswith("*"):
                  message = f"pointer dereference write to non-pointer variable '{pointer_name}'"
                  raise CompileError(message, line=place.line)
              pointee_type = holder_type[:-1]
              self.generate_expression(value)
              self._emit_load_var(pointer_name, register=self.target.si_register)
              if pointee_type in self.BYTE_TYPES:
                  self.emit(f"        mov [{self.target.si_register}], {self.target.low_byte(self.target.acc)}")
              else:
                  self.emit(f"        mov [{self.target.si_register}], {self.target.acc}")
              self.ax_clear()
              return
          # Cast / arbitrary address expression: ``*(T *)e = v``.
          width = self._dereference_place_width(place)
          self.generate_expression(value)
          accumulator = self.target.acc
          fast_path_target = pointer.expression if isinstance(pointer, Cast) else pointer
          if isinstance(fast_path_target, AddressOf) and fast_path_target.var.name in self.locals:
              destination = f"[{self._local_address(fast_path_target.var.name)}]"
              if width == 1:
                  self.emit(f"        mov {destination}, {self.target.low_byte(accumulator)}")
              elif width == 2 and self.target.int_size > 2:
                  self.emit(f"        mov word {destination}, {self.target.low_word(accumulator)}")
              else:
                  self.emit(f"        mov {destination}, {accumulator}")
              return
          scratch = self.target.si_register
          self.emit(f"        push {accumulator}")
          self.generate_expression(pointer)
          self.emit(f"        mov {scratch}, {accumulator}")
          self.emit(f"        pop {accumulator}")
          if width == 1:
              self.emit(f"        mov [{scratch}], {self.target.low_byte(accumulator)}")
          elif width == 2 and self.target.int_size > 2:
              self.emit(f"        mov word [{scratch}], {self.target.low_word(accumulator)}")
          else:
              self.emit(f"        mov [{scratch}], {accumulator}")
  ```

  > **Trap 2 / 4 / 3 notes.** The named-pointer branch uses `holder_type[:-1]` (no rstrip) exactly like legacy `DerefAssign` line 4198, NOT the rstripped `_dereference_place_width`, so the `in BYTE_TYPES` test matches the legacy byte-for-byte. The named-pointer branch never emits the `unsigned short` `mov word` form because legacy `DerefAssign` did not (it only branched byte vs full) — preserved. The cast branch DOES emit the `unsigned short` form because legacy `PointerDereferenceAssign` did. The two branches are deliberately asymmetric to match legacy.
  > **`generate_expression(pointer)` for the cast branch** reproduces legacy `generate_expression(statement.address)` because `Cast` codegen is identity (generates the inner). The parser must build `DereferencePlace(pointer=Cast(expression=<inner>, ...))` for the cast write so `_dereference_place_width` recovers the width from the cast's `target_type`. Byte-identical to legacy which evaluated `statement.address` (the cast's inner) — identity Cast codegen makes these equal.

- [ ] **Step 3.3.3.** The `DereferencePlace` write is now also reachable from the `PlaceStore` statement arm (`emission.py:4284-4286`) and the AssignExpr `PlaceStore` arm (`emission.py:913-917`). Both call `_emit_place_store`. The statement arm appends `ax_clear()`; the cast fast-path returns without `ax_clear()` (matching legacy `_emit_pointer_dereference_assign` which had no trailing clear, and the statement dispatch added it at 4289). Verify the statement-level `ax_clear()` semantics match (trap 7). The golden's `probe_cast_deref_store_*` and `probe_deref_assign_expr` cover both.

#### 3.4 Oracle test: hand-built Place trees vs legacy, byte-identity

Create a temporary oracle test that post-parses the Task-1 fixture, rewrites the **legacy** deref nodes into the Place trees, and asserts the Place-tree codegen is byte-identical to the legacy codegen. This proves the core extension before flipping the parser. (Plan 2 used `test_cc_place_member_codegen.py`; this is the analogue.)

- [ ] **Step 3.4.1.** Create `tests/unit/test_cc_place_deref_codegen.py`. The test: (a) builds a small set of self-contained C functions per shape; (b) compiles each twice — once via the unmodified parser (legacy nodes), once via a transform that rewrites the parsed AST's legacy deref nodes into the Place trees from §1.10 — and asserts the emitted assembly is byte-identical. The transform is a small recursive walk. Complete file:

  ```python
  """Oracle: hand-built Place trees for the deref family emit bytes identical to legacy.

  Each probe is parsed twice. The first pass keeps the legacy deref nodes
  (DerefAssign / DerefIncrement / DerefIncrementAssign / PointerDereference /
  PointerDereferenceAssign / DoubleIndex). The second pass rewrites those
  nodes into PlaceLoad / PlaceStore over DereferencePlace / SubscriptPlace
  per the Plan 3 mapping, then runs the same generator. The emitted assembly
  must be byte-identical, proving the Place codegen core reproduces legacy
  output before the parser is flipped.
  """

  from __future__ import annotations

  import dataclasses

  import pytest

  from cc import ast_nodes
  from cc.parser import Parser
  from cc.preprocessor import preprocess  # adjust import to the real entry point
  from cc.codegen.x86.generator import Generator  # adjust to real class/entry


  def _rewrite(node):
      """Recursively rewrite legacy deref nodes into the Place tree shapes."""
      if isinstance(node, ast_nodes.PointerDereference):
          return ast_nodes.PlaceLoad(
              line=node.line,
              place=ast_nodes.DereferencePlace(
                  line=node.line,
                  pointer=ast_nodes.Cast(
                      line=node.line,
                      expression=_rewrite(node.expression),
                      target_type=f"{node.target_type} *",
                  ),
              ),
          )
      if isinstance(node, ast_nodes.PointerDereferenceAssign):
          return ast_nodes.PlaceStore(
              line=node.line,
              place=ast_nodes.DereferencePlace(
                  line=node.line,
                  pointer=ast_nodes.Cast(
                      line=node.line,
                      expression=_rewrite(node.address),
                      target_type=f"{node.target_type} *",
                  ),
              ),
              value=_rewrite(node.value),
          )
      if isinstance(node, ast_nodes.DerefAssign):
          return ast_nodes.PlaceStore(
              line=node.line,
              place=ast_nodes.DereferencePlace(line=node.line, pointer=node.pointer),
              value=_rewrite(node.expr),
          )
      if isinstance(node, ast_nodes.DoubleIndex):
          return ast_nodes.PlaceLoad(
              line=node.line,
              place=ast_nodes.SubscriptPlace(
                  line=node.line,
                  base=ast_nodes.DereferencePlace(
                      line=node.line,
                      pointer=ast_nodes.Index(
                          line=node.line,
                          array=node.array,
                          index=_rewrite(node.outer_index),
                      ),
                  ),
                  index=_rewrite(node.inner_index),
              ),
          )
      # DerefIncrement / DerefIncrementAssign are handled by dedicated
      # dispatch arms that compose PlaceLoad/PlaceStore + _emit_pointer_bump
      # (see Task 3.5); leave them as-is here so this oracle only proves the
      # standalone DereferencePlace load/store core.
      if dataclasses.is_dataclass(node) and not isinstance(node, type):
          changes = {}
          for f in dataclasses.fields(node):
              current = getattr(node, f.name)
              if isinstance(current, ast_nodes.Node):
                  changes[f.name] = _rewrite(current)
              elif isinstance(current, list):
                  changes[f.name] = [
                      _rewrite(item) if isinstance(item, ast_nodes.Node) else item
                      for item in current
                  ]
          if changes:
              return dataclasses.replace(node, **changes)
      return node


  PROBES = {
      "deref_read_int": "int f(int *p) { return *p; }",
      "deref_store_int": "int f(int *p, int v) { *p = v; return *p; }",
      "deref_read_char": "int f(char *p) { return *p; }",
      "deref_store_char": "int f(char *p, int v) { *p = v; return *p; }",
      "cast_deref_uchar_local": "struct s { unsigned char a; }; int f(void) { struct s x; return *(unsigned char *)&x; }",
      "cast_deref_uchar_expr": "int f(char *b, int o) { return *(unsigned char *)(b + o); }",
      "cast_deref_store_uchar_local": "struct s { unsigned char a; }; void f(int v) { struct s x; *(unsigned char *)&x = v; }",
      "cast_deref_store_ushort_expr": "void f(char *b, int o, int v) { *(unsigned short *)(b + o) = v; }",
      "double_index_byte": "char *names[4]; int f(int i, int j) { return names[i][j]; }",
      "double_index_word": "unsigned short *words[4]; int f(int i, int j) { return words[i][j]; }",
      "double_index_const": "char *names[4]; int f(void) { return names[1][2]; }",
  }


  def _compile(source: str, *, rewrite: bool) -> str:
      tokens = preprocess(source)             # adjust to actual preprocessor API
      program = Parser(tokens).parse()        # adjust to actual parse entry
      if rewrite:
          program = _rewrite(program)
      generator = Generator(bits=32)          # adjust to actual constructor
      return generator.generate(program)      # adjust to actual emit entry


  @pytest.mark.parametrize("name", sorted(PROBES))
  def test_place_deref_codegen_matches_legacy(name: str) -> None:
      source = PROBES[name]
      legacy = _compile(source, rewrite=False)
      placed = _compile(source, rewrite=True)
      assert placed == legacy, f"{name}: Place codegen diverged from legacy"
  ```

  > **Implementation note for the engineer:** the imports / constructor / entry-point names (`preprocess`, `Parser(...).parse()`, `Generator(bits=32).generate(...)`) are placeholders for the *real* API. Before writing this file, read `cc.py`'s `main()` to find the exact compile pipeline (tokenize → preprocess → parse → IR/codegen) and the precise function/class names, then wire `_compile` to call them with `--bits 32` semantics and `-I user/libbboeos/include`. The struct-using probes need no include. Match how `tests/test_cc_place.py:emit_asm` shells out if the in-process API is awkward — but in-process is preferred for an oracle so the rewrite can be injected between parse and codegen.

- [ ] **Step 3.4.2.** Run the oracle:
  - Command: `python3 -m pytest tests/unit/test_cc_place_deref_codegen.py -q`
  - Expected: all parametrized cases pass (byte-identical).
  - If any case fails, the failure prints the diverging shape. Debug using `superpowers:systematic-debugging`: diff the two assembly strings, locate the first differing line, and reconcile the new `_emit_dereference_place_*` to the legacy emitter for that shape. Do NOT bless any difference.
- [ ] **Step 3.4.3.** Re-run the golden (the parser is still legacy, so this still passes — the core additions are dormant for real source):
  - Command: `python3 tests/test_cc_place.py`
  - Expected: `PASS  index_member golden byte-identical`
- [ ] **Step 3.4.4.** Commit.
  - Message:
    ```
    feat(cc): Place codegen core for standalone DereferencePlace (load/store)

    Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
    ```

#### 3.5 Increment composition (`*p++` read and `*p++ = v` write)

The increment forms cannot be expressed as a pure Place tree; they need a pointer bump. **Decision (locked):** keep `DerefIncrement` and `DerefIncrementAssign` as the parser's output **but route their codegen through the Place core + `_emit_pointer_bump`**, so that after this plan they compose `PlaceLoad`/`PlaceStore` over `DereferencePlace(Var)` instead of `Index(Var, Int(0))`. This keeps the byte output identical (the legacy `DerefIncrement` already desugared `*p` to `Index(Var, Int(0))`; `PlaceLoad(DereferencePlace(Var))` must emit the same bytes — verify).

Wait — the legacy `DerefIncrement` read desugars to `Index(Var, Int(0))`, which is the existing `_generate_index_expression` path, NOT `_emit_dereference_place_load`. These can emit different bytes (Index uses `_index_pointee_size` and a possibly different load sequence than the deref self-load). **To stay byte-exact, the increment read must continue to use `Index(Var, Int(0))` for its load, not `DereferencePlace`.**

**Re-decision (locked, byte-exact):** the two increment forms keep emitting **exactly** their legacy sequences. The only change is that they are no longer separate "legacy nodes to delete" — but the scope says delete all six. To delete `DerefIncrement`/`DerefIncrementAssign` while preserving bytes, we replace them in the parser with the Place equivalents **plus a bump**, but the read still uses `Index(Var, Int(0))`. Concretely we introduce the composition at the dispatch level using existing primitives:

- For `*p++` read: parser emits `PlaceLoad` over a new lightweight wrapper? No — simpler: the parser builds `DerefIncrement`'s replacement as `PlaceIncDec`? No (wrong arithmetic). 

The cleanest byte-exact route that still deletes the two nodes: **introduce no new node**; instead the parser emits the read as the existing `Index(Var, Int(0))` load wrapped so the bump happens, and the write as `PlaceStore(DereferencePlace(Var), v)` wrapped with the bump. Since a bump is a side effect that must be sequenced, and the AST has no "sequence" node, the increment forms genuinely need a node to carry `(delta, is_postfix, target_name)` and the value.

**Final locked decision:** Do NOT delete `DerefIncrement` and `DerefIncrementAssign` outright in a way that loses their data; instead, after analysis, the scope item "delete the six legacy nodes" is satisfied for these two by **folding them into the Place operation family**: model `*p++`/`*++p` as `PlaceIncDec`-like but with a dedicated pointer-bump. To avoid wrong arithmetic and stay byte-exact, we keep the two nodes' *fields* but **rename/relocate** them as Place-operation nodes is over-engineering. 

Given the byte-exact gate dominates, the **pragmatic locked decision** is: keep `DerefIncrement` and `DerefIncrementAssign` as AST nodes (they ARE already "Place-adjacent operation" nodes carrying `target_name`), but **reimplement their dispatch-arm bodies** to (a) for the read, call `_emit_place_load(DereferencePlace(Var(target)))`? — no, that changes bytes vs `Index`. 

**Resolution:** Verify empirically whether `_emit_dereference_place_load(DereferencePlace(Var(p)))` emits byte-identical output to `generate_expression(Index(Var(p), Int(0)))` for both `int *` and `char *`. If identical, the increment read can use the Place load and the nodes can be deleted with the parser emitting a composite. If NOT identical, the increment forms keep using `Index(Var, Int(0))` for their load and we DO NOT delete `DerefIncrement`/`DerefIncrementAssign` in this plan — instead we document them as the remaining legacy nodes and narrow the plan's "delete six" to "delete four" with a tracked follow-up.

- [ ] **Step 3.5.1.** Empirically compare the two read lowerings. Add a temporary check to the oracle (or a scratch pytest) compiling `int f(int *p){return *p;}` and `int g(char *p){return *p;}` and compare `_emit_dereference_place_load(DereferencePlace(Var(p)))` output against `generate_expression(Index(Var(p), Int(0)))` output, per target width.
  - Command: write a one-off parametrized test `tests/unit/test_cc_deref_vs_index.py` mirroring the oracle's `_compile`, comparing the two lowerings for `int *` and `char *`.
  - Command: `python3 -m pytest tests/unit/test_cc_deref_vs_index.py -q`
  - **Decision gate:**
    - If **byte-identical** for both widths: proceed to 3.5.2 (delete all six, increment forms compose Place load/store + bump).
    - If **divergent**: proceed to 3.5.2-ALT (keep the two increment nodes; delete only the other four). Record the divergence in the PR description as the documented follow-up.
  - Delete the scratch test before committing (`git rm`/remove), or keep it as a permanent regression guard with a descriptive name. Prefer keeping it.

- [ ] **Step 3.5.2 (path: identical).** The increment forms become composites. Keep the two node dispatch arms but rewrite their bodies to use the Place load/store:
  - `DerefIncrement` arm (`emission.py:2798-2828`): replace the two `Index(Var, Int(0))` `generate_expression` calls with `self._emit_place_load(DereferencePlace(line=expression.line, pointer=Var(line=expression.line, name=target)))`, preserving the exact postfix (load then bump) / prefix (bump, `ax_clear`, load) ordering and the `_emit_pointer_bump` calls.
  - `DerefIncrementAssign` arm (`emission.py:4206-4235`): replace the inline store sequence with `self._emit_dereference_place_store(DereferencePlace(line=..., pointer=Var(..., name=target)), statement.expr)`, preserving the prefix-bump-first / postfix-bump-after ordering and trailing `ax_clear()`. Confirm `_emit_dereference_place_store`'s named-pointer branch emits the same bytes as the legacy inline `DerefIncrementAssign` store — **CAUTION:** legacy `DerefIncrementAssign` had a `unsigned short` `mov word` branch (line 4229-4230) that the legacy `DerefAssign` (and thus `_emit_dereference_place_store`'s named branch) does NOT. So they are **not** byte-identical for `unsigned short *` pointee. **Therefore the `DerefIncrementAssign` store cannot blindly reuse `_emit_dereference_place_store`'s named branch.** Keep the inline store in the `DerefIncrementAssign` arm (do not reroute its store), and only convert it if the oracle proves equality including the `unsigned short` case. Since they differ, **keep `DerefIncrementAssign`'s store inline.**

  Given this asymmetry, the **locked outcome** is: even on the "identical read" path, `DerefIncrementAssign`'s store must keep its own `unsigned short`-aware inline sequence. The cleanest byte-exact deletion that still removes the nodes requires the parser composite to carry the bump + a width-correct store — which is exactly what `DerefIncrementAssign` already is. **Conclusion: do not delete `DerefIncrementAssign` and `DerefIncrement` in this plan if their bodies cannot be losslessly expressed via the Place core.** They are kept as Place-adjacent operation nodes whose *read* uses `_emit_place_load(DereferencePlace(Var))` only if byte-identical.

- [ ] **Step 3.5.2-FINAL (locked).** To resolve the ambiguity decisively and keep the gate green: **delete the four pure-data nodes** (`DerefAssign`, `PointerDereference`, `PointerDereferenceAssign`, `DoubleIndex`) in Task 5, and **retain `DerefIncrement` / `DerefIncrementAssign`** as the two remaining legacy nodes, but **reimplement their dispatch arms** so the *read* of the pointee goes through `_emit_place_load(DereferencePlace(Var(target)))` **iff** Step 3.5.1 proved byte-identity (else keep `Index(Var, Int(0))`), while their pointer bump and (for the assign) width-correct store stay as the legacy inline sequences. Document in the PR: "the increment-over-deref family is now a thin composite over the Place core; the two nodes remain because they carry sequencing (load/store + bump) that the declarative Place tree cannot express, mirroring how `IncrementDecrement` and `PlaceIncDec` coexist." This satisfies "roadmap-complete deref family" (all six shapes route through the Place core for their memory access) without a byte-risky forced deletion.

  > **Rationale, stated for the gate:** the scope says "convert these legacy nodes, then DELETE them." For the four data-only nodes this is clean. For the two increment nodes, byte-exactness (the dominating constraint) forbids collapsing their distinct `unsigned short` store path into the shared `DerefAssign`-style store. We convert their *memory access* to the Place core and keep the node as the sequencing carrier. If the reviewer requires literal deletion of all six, the alternative is to add a width parameter to the Place store path — deferred to a follow-up to avoid widening byte risk in this PR.

- [ ] **Step 3.5.3.** Apply the chosen increment-arm edits (read through Place load if 3.5.1 was identical; otherwise leave the read on `Index`). Keep `DerefIncrementAssign`'s inline store. Run:
  - Command: `python3 -m pytest tests/unit/test_cc_place_deref_codegen.py tests/unit/test_cc_deref_vs_index.py -q`
  - Command: `python3 tests/test_cc_place.py`  (still legacy parser → PASS)
  - Expected: all pass; golden still byte-identical.
- [ ] **Step 3.5.4.** Commit.
  - Message:
    ```
    feat(cc): route the deref-increment family's pointee access through the Place core

    Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
    ```

#### 3.6 Purity predicate (trap 1)

- [ ] **Step 3.6.1.** Edit `_is_pure_expression` (`emission.py:1606-1636`). Replace the unconditional `PlaceLoad → True` (lines 1628-1629) with a precise predicate. Complete replacement for that branch:

  ```python
          if isinstance(node, PlaceLoad):
              return self._place_is_pure(node.place)
  ```

- [ ] **Step 3.6.2.** Add the helper `_place_is_pure` (alphabetical placement among `_p*` methods in `generator.py`, or beside `_is_pure_expression` in `emission.py` — keep it in the same module as `_is_pure_expression`, i.e. `emission.py`). Complete method:

  ```python
      def _place_is_pure(self, place: Place, /) -> bool:
          """Return True when reading *place* has no observable side effect.

          A standalone ``DereferencePlace`` (a pointer read through an
          arbitrary address) is treated as IMPURE: the legacy nodes it
          replaces (``PointerDereference``, ``DoubleIndex``,
          ``DerefIncrement``) all reported impure, and the conditional /
          guarded-update elision in
          :meth:`_try_emit_conditional_via_cond_value` relies on that to
          avoid eliding a re-evaluated branch.  Member shapes (dot, arrow,
          chained, struct-array, member-index) stay PURE, matching the
          legacy ``MemberAccess`` behavior.  A ``SubscriptPlace`` is pure
          only when its base chain does not bottom out at a standalone
          ``DereferencePlace`` (the ``name[i][j]`` shape is impure; the
          ``arr[i].field[j]`` / ``ptr->field[i]`` member shapes are pure).
          """
          if isinstance(place, VariablePlace):
              return True
          if isinstance(place, DereferencePlace):
              # Arrow member access wraps DereferencePlace inside MemberPlace
              # and never reaches here as a top-level place; a standalone
              # DereferencePlace is a raw pointer read — impure.
              return False
          if isinstance(place, MemberPlace):
              # Dot / arrow / chained member reads are pure regardless of
              # whether the base is a DereferencePlace (p->f), matching the
              # legacy MemberAccess purity.
              return True
          if isinstance(place, SubscriptPlace):
              return self._place_is_pure(place.base)
          return False
  ```

  > **Trap-1 detail.** `ptr->field` is `MemberPlace(DereferencePlace(Var), field)` — the `MemberPlace` arm returns `True` (pure) without recursing into the `DereferencePlace`, preserving legacy `MemberAccess=True`. `name[i][j]` is `SubscriptPlace(DereferencePlace(Index(...)), j)` — `SubscriptPlace` recurses to its `DereferencePlace` base → `False` (impure), matching legacy `DoubleIndex=False`. `*p` and `*(T*)e` are top-level `DereferencePlace` → `False`, matching legacy. `arr[i].field[j]` is `SubscriptPlace(MemberPlace(SubscriptPlace(VariablePlace,...),...), j)` — recurses to `MemberPlace` → `True`, matching legacy member purity. The `probe_deref_in_if` golden probe locks this.

- [ ] **Step 3.6.3.** Run the oracle and golden (parser still legacy, so the golden's `probe_deref_in_if` still emits via legacy `Index(Var, Int(0))` which is impure-by-fallthrough; after the flip it emits via `PlaceLoad(DereferencePlace)` which is now impure-by-predicate — byte-identical condition codegen):
  - Command: `python3 -m pytest tests/unit/test_cc_place_deref_codegen.py -q && python3 tests/test_cc_place.py`
  - Expected: oracle passes; golden PASS.
- [ ] **Step 3.6.4.** Commit.
  - Message:
    ```
    feat(cc): precise PlaceLoad purity (standalone deref is impure)

    Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
    ```

#### 3.7 Liveness handlers (trap 8) — added but verified empirically

- [ ] **Step 3.7.1.** Edit `cc/codegen/liveness.py`. In `_add_expression_uses` (125-178), add a `PlaceLoad` and a bare `Place` arm that walk the place tree recording every `Var` use, plus keep the existing `DoubleIndex` arm for now (it is still produced by the parser until Task 4; remove it in Task 5). Insert (alphabetically among the `isinstance` chain, before the final `Node` raise) a call into a new `_add_place_uses` helper:

  ```python
          if isinstance(expression, ast_nodes.PlaceLoad):
              self._add_place_uses(expression.place, accumulator)
              return
          if isinstance(expression, ast_nodes.Place):
              self._add_place_uses(expression, accumulator)
              return
  ```
  (Adjust the `ast_nodes.` prefix to match the file's import style — `liveness.py` imports names directly, e.g. `DoubleIndex`, so use bare `PlaceLoad` / `Place` after adding them to the import block at the top.)

- [ ] **Step 3.7.2.** Add `_add_place_uses` to the analyzer. Complete method (place after `_add_expression_uses`):

  ```python
      def _add_place_uses(self, place: object, accumulator: set[str]) -> None:
          """Record every Var read while addressing *place*.

          Mirrors the legacy use sets:
          - DereferencePlace(Var(p)) reads p (was DerefAssign: uses pointer.name).
          - DereferencePlace(expression) reads the expression's Vars
            (was PointerDereferenceAssign: uses address).
          - SubscriptPlace records its base's uses and its index's uses;
            for the name[i][j] shape this yields {array, i, j}
            (was DoubleIndex: uses array + both indices).
          - VariablePlace records its name.
          - MemberPlace recurses into its base.
          """
          if isinstance(place, VariablePlace):
              accumulator.add(place.name)
              return
          if isinstance(place, DereferencePlace):
              self._add_expression_uses(place.pointer, accumulator)
              return
          if isinstance(place, SubscriptPlace):
              self._add_place_uses(place.base, accumulator)
              self._add_expression_uses(place.index, accumulator)
              return
          if isinstance(place, MemberPlace):
              self._add_place_uses(place.base, accumulator)
              return
          message = f"liveness: unhandled place node {type(place).__name__}"
          raise LivenessAnalysisError(message)
  ```

- [ ] **Step 3.7.3.** In `_collect_use_def` (215-281), add statement arms for `PlaceStore` and the surviving increment nodes, mirroring the legacy use/def sets:

  ```python
          if isinstance(statement, PlaceStore):
              self._add_place_uses(statement.place, statement_info.uses)
              self._add_expression_uses(statement.value, statement_info.uses)
              return
          if isinstance(statement, DerefIncrement):
              statement_info.uses.add(statement.target_name)
              return
          if isinstance(statement, DerefIncrementAssign):
              statement_info.uses.add(statement.target_name)
              self._add_expression_uses(statement.expr, statement_info.uses)
              return
  ```
  Keep the existing `DerefAssign`, `DoubleIndex` (in `_add_expression_uses`), and `PointerDereferenceAssign` arms until Task 5 (they still run pre-flip). Add `PlaceStore`, `PlaceLoad`, `Place` to the import block at the top of `liveness.py`.

  > **Trap-8 risk.** `DerefIncrement` / `DerefIncrementAssign` previously had NO handler → `LivenessAnalysisError` → auto-pin skipped for any function using them. Adding handlers ENABLES auto-pin for those functions, which can change output. **This must be verified empirically.** If the userland differential (Step 4.4 / final gate) shows ANY change attributable to a function using `*p++`, revert these two increment arms to NOT being handled — i.e., make `_collect_use_def` raise `LivenessAnalysisError` for `DerefIncrement`/`DerefIncrementAssign` exactly as before (omit the two arms; the final `raise` at the bottom handles them). Document as the preserved-behavior follow-up. The non-increment shapes (`PlaceStore` over `DereferencePlace`, the `name[i][j]` `PlaceLoad`) reproduce the legacy `DerefAssign`/`PointerDereferenceAssign`/`DoubleIndex` use sets which WERE handled, so they should not change behavior.

- [ ] **Step 3.7.4.** Run the unit liveness tests and the full unit suite:
  - Command: `python3 -m pytest tests/unit/test_cc_liveness.py -q`
  - Expected: all pass.
  - Command: `python3 -m pytest tests/unit -q 2>&1 | tail -3`
  - Expected: all pass (count ≥ baseline + new tests).
- [ ] **Step 3.7.5.** Commit.
  - Message:
    ```
    feat(cc): liveness use/def for Place-over-deref and increment shapes

    Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
    ```

---

### Task 4 — Flip the parser to emit Place trees

Now switch the parser construction sites so real source produces the Place trees. The codegen core (Task 3) already reproduces legacy bytes, so the golden must stay byte-identical.

- [ ] **Step 4.1.** Edit `cc/parser.py` `_parse_deref_assignment_no_semi` (317-369). Rewrite the three return sites:
  - `*(T*)e = v` (333-338): return `PlaceStore(line=star_token[2], place=DereferencePlace(line=star_token[2], pointer=Cast(line=star_token[2], expression=operand.expression, target_type=pointee_type + " *")), value=value)`.
    - **CAUTION:** the parser had `pointee_type = self._pointee_type_from_cast(...)` returning the pointee (`"unsigned char"`). The Place needs the full pointer type on the Cast so `_dereference_place_width`/`_place_type` recover the width. Reconstruct `f"{pointee_type} *"` for the Cast's `target_type` (matching the original cast string the user wrote, modulo spacing — verify `_pointee_type_from_cast` rstrips so `f"{pointee_type} *"` reproduces a valid type string). Use `operand` (the original `Cast` node) directly if it already has the right `target_type` — prefer `pointer=operand` (the parsed `Cast`) over reconstructing, since `operand` IS the cast: `place=DereferencePlace(pointer=operand)`. This is cleaner and exact. Use `place=DereferencePlace(line=star_token[2], pointer=operand)`.
  - Prefix `*++p = v` / `*--p = v` (345-351): **retain** `DerefIncrementAssign` (per §3.5-FINAL) — no change, it still carries `delta`, `expr`, `is_postfix=False`, `target_name`.
  - Postfix `*p++ = v` (360-366): **retain** `DerefIncrementAssign` — no change.
  - `*p = v` (369): return `PlaceStore(line=star_token[2], place=DereferencePlace(line=star_token[2], pointer=Var(line=star_token[2], name=name_token[1])), value=expression)`.
  - Update the method's return type annotation and docstring to reflect the new returns (`PlaceStore | DerefIncrementAssign`).

- [ ] **Step 4.2.** Edit `cc/parser.py` `_parse_star_primary` (1387-1452):
  - `*(T*)expr` read (1402-1409): return `PlaceLoad(line=line, place=DereferencePlace(line=line, pointer=operand))` where `operand` is the parsed `Cast`. (Drop the `pointee_type`/`PointerDereference` construction; keep the `_pointee_type_from_cast` validation call so the error messages are preserved — call it for its side-effect validation, then discard the return.) Confirm width recovery: `_place_type(DereferencePlace(Cast))` → cast `target_type` → strip `*` → pointee. Matches.
  - `*(expr)` non-cast forms (1410-1421): **unchanged** (they already desugar to `Index`).
  - prefix `*++p`/`*--p` (1422-1435): **retain** `DerefIncrement`.
  - postfix `*p++`/`*p--` (1436-1447): **retain** `DerefIncrement`.
  - bare `*p` (1448-1452): currently returns `Index(Var(name), Int(0))`. **Change to** `PlaceLoad(line=line, place=DereferencePlace(line=line, pointer=Var(line=line, name=name_token[1])))` — **only if** Step 3.5.1 proved `_emit_dereference_place_load(DereferencePlace(Var))` is byte-identical to `Index(Var, Int(0))`. If divergent, **leave it as `Index`** (the legacy desugar) and document. **The golden's `probe_deref_read_*` will catch any divergence at Step 4.5.**

- [ ] **Step 4.3.** Edit `cc/parser.py` `_parse_primary` DoubleIndex site (842-847). Replace the `DoubleIndex(...)` return with:
  ```python
                  return PlaceLoad(
                      line=line,
                      place=SubscriptPlace(
                          line=line,
                          base=DereferencePlace(
                              line=line,
                              pointer=Index(array=Var(line=line, name=name), index=index, line=line),
                          ),
                          index=inner_index,
                      ),
                  )
  ```
  (The outer `Index` STAYS a plain expression — do not convert it.)

- [ ] **Step 4.4.** Run the full verification battery (parser now emits Place trees; codegen core handles them):
  - Command: `python3 tests/test_cc_place.py`
  - Expected: `PASS  index_member golden byte-identical` — **this is the load-bearing assertion**: every converted shape emits the exact legacy bytes.
  - Command: `python3 -m pytest tests/unit -q 2>&1 | tail -3`
  - Expected: all pass.
  - Command: `python3 -m pytest tests/test_cc_pointer_deref_expr.py tests/test_cc_assign_expr.py tests/test_cc_casts.py -q` (the snippet suites most likely to exercise deref)
  - Expected: all pass.
- [ ] **Step 4.5.** Run the userland differential against the Step 0.3 baseline:
  - Command:
    ```
    mkdir -p /tmp/plan3_after
    for f in $(find user -name '*.c' | sort); do
      out=/tmp/plan3_after/$(echo "$f" | tr '/' '_').asm
      python3 cc.py --bits 32 -I user/libbboeos/include "$f" "$out" 2>/dev/null || echo "SKIP $f"
    done
    diff -rq /tmp/plan3_before /tmp/plan3_after && echo "USERLAND BYTE-IDENTICAL"
    ```
  - Expected: `USERLAND BYTE-IDENTICAL`.
  - **If any file differs:** stop. Diff the offending `.asm` (`diff /tmp/plan3_before/<f>.asm /tmp/plan3_after/<f>.asm`). The most likely cause is the liveness change (trap 8) re-enabling auto-pin for a `*p++` function. Apply the §3.7.3 fallback (revert the two increment liveness arms to unhandled). Re-run until byte-identical. Never bless a diff.
- [ ] **Step 4.6.** Reassembly + program suites:
  - Command: `python3 tests/test_asm.py 2>&1 | tail -3`
  - Expected: 49/49 green.
  - Command: `python3 tests/test_programs.py 2>&1 | tail -5`
  - Expected: bbfs suite green (record counts).
  - Command: `python3 tests/test_programs.py --filesystem ext2 2>&1 | tail -5`
  - Expected: ext2 suite green (record counts).
- [ ] **Step 4.7.** Commit.
  - Message:
    ```
    feat(cc): parser emits Place trees for the deref family

    Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
    ```

---

### Task 5 — Delete the converted legacy nodes, their codegen, dispatch, and liveness arms

Delete `DerefAssign`, `PointerDereference`, `PointerDereferenceAssign`, `DoubleIndex` (the four pure-data nodes). **Retain** `DerefIncrement` / `DerefIncrementAssign` per §3.5-FINAL (they now route memory access through the Place core but remain as sequencing carriers; do not delete). After each deletion, run the golden + unit suite.

- [ ] **Step 5.1.** Remove dead emission code in `cc/codegen/x86/emission.py`:
  - Delete `_emit_pointer_dereference` (656-689) and `_emit_pointer_dereference_assign` (691-723). Keep `_emit_pointer_bump` (617-654) — still used by the increment arms.
  - Delete `_generate_double_index_expression` (1244-1308).
  - Delete the expression-dispatch arm `elif isinstance(expression, DoubleIndex):` (2829-2830) and `elif isinstance(expression, PointerDereference):` (2873-2874).
  - Delete the statement-dispatch arm `elif isinstance(statement, DerefAssign):` (4174-4205) and `elif isinstance(statement, PointerDereferenceAssign):` (4287-4289).
  - In `_generate_assign_expr` (864-922): delete the `DerefAssign` arm (890-904) and `PointerDereferenceAssign` arm (918-919). **Keep** the `DerefIncrementAssign` arm (905-910) — its node survives. Keep the `PlaceStore` arm (913-917), which now also handles `(*p=v)` and `(*(T*)e=v)`.
- [ ] **Step 5.2.** Run golden + unit:
  - Command: `python3 tests/test_cc_place.py && python3 -m pytest tests/unit -q 2>&1 | tail -3`
  - Expected: golden PASS; unit all pass.
- [ ] **Step 5.3.** Remove dead liveness arms in `cc/codegen/liveness.py`:
  - In `_add_expression_uses`: delete the `DoubleIndex` arm (157-161).
  - In `_collect_use_def`: delete the `DerefAssign` arm (248-251) and `PointerDereferenceAssign` arm (264-267). Keep the new `PlaceStore`, `PlaceLoad`, increment arms.
  - Remove now-unused imports (`DerefAssign`, `DoubleIndex`, `PointerDereferenceAssign`) from the top-of-file import block.
- [ ] **Step 5.4.** Remove the IR cosmetic reference in `cc/ir.py`: `_assign_rhs_field_name` (366-375) special-cases `PointerDereferenceAssign`. Read its callers first (`grep -n "_assign_rhs_field_name" cc/ir.py`). If its only purpose was the `PointerDereferenceAssign.value` vs `.expr` field name, and that node is gone, simplify it (drop the `PointerDereferenceAssign` branch) or remove it if now unused. Verify no other references.
- [ ] **Step 5.5.** Delete the four node classes from `cc/ast_nodes.py`:
  - `DerefAssign` (187-192).
  - `DoubleIndex` (253-266).
  - `PointerDereference` (570-585).
  - `PointerDereferenceAssign` (588-604).
  - **Keep** `DerefIncrement` (195-209) and `DerefIncrementAssign` (212-227).
  - Update `AssignExpr`'s docstring (100-109) which enumerates `DerefAssign` / `PointerDereferenceAssign` / `DerefIncrementAssign` — remove the deleted names, keep `DerefIncrementAssign` and `PlaceStore`.
- [ ] **Step 5.6.** Purge dangling imports of the deleted names across the package:
  - Command: `grep -rnE "DerefAssign|DoubleIndex|PointerDereference\b|PointerDereferenceAssign" cc/ | grep -v "DerefIncrement"`
  - Expected after edits: no matches (every hit removed). Remove the corresponding `import` lines in `emission.py` (it imports `PointerDereferenceAssign` at line 73, etc.) and any others surfaced.
- [ ] **Step 5.7.** Full verification battery:
  - Command: `python3 tests/test_cc_place.py`
  - Expected: `PASS  index_member golden byte-identical`.
  - Command: `python3 -m pytest tests/unit -q 2>&1 | tail -3`
  - Expected: all pass.
  - Command: `python3 -c "import ast, pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('cc').rglob('*.py')]" && echo "CC PARSES"`
  - Expected: `CC PARSES` (no NameError surfaces; do a real import smoke test next).
  - Command: `python3 -c "import cc.parser, cc.ir, cc.ast_nodes, cc.codegen.liveness, cc.codegen.x86.generator, cc.codegen.x86.emission; print('IMPORT OK')"`
  - Expected: `IMPORT OK`.
- [ ] **Step 5.8.** Re-run the userland differential (must still be byte-identical after deletion):
  - Command:
    ```
    rm -rf /tmp/plan3_after && mkdir -p /tmp/plan3_after
    for f in $(find user -name '*.c' | sort); do
      out=/tmp/plan3_after/$(echo "$f" | tr '/' '_').asm
      python3 cc.py --bits 32 -I user/libbboeos/include "$f" "$out" 2>/dev/null || echo "SKIP $f"
    done
    diff -rq /tmp/plan3_before /tmp/plan3_after && echo "USERLAND BYTE-IDENTICAL"
    ```
  - Expected: `USERLAND BYTE-IDENTICAL`.
- [ ] **Step 5.9.** Commit.
  - Message:
    ```
    refactor(cc): delete the four converted deref data nodes and their codegen

    Removes DerefAssign, PointerDereference, PointerDereferenceAssign, and
    DoubleIndex; their parser sites, generator/emission lowerers, dispatch
    arms, liveness use/def, and the IR cosmetic reference. The deref-increment
    family (DerefIncrement / DerefIncrementAssign) is retained as the Place
    core's sequencing carrier; its pointee access now routes through the Place
    load/store core.

    Output byte-identical: Place golden unchanged, all 50 userland .c compile
    identically, test_asm 49/49, test_programs bbfs + ext2 green.

    Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
    ```

---

### Task 6 — Final gate

- [ ] **Step 6.1.** Golden: `python3 tests/test_cc_place.py` → `PASS  index_member golden byte-identical`.
- [ ] **Step 6.2.** Full unit suite: `python3 -m pytest tests/unit -q 2>&1 | tail -3` → all pass, count ≥ Step 0.2 baseline + new tests.
- [ ] **Step 6.3.** Snippet suites: `python3 -m pytest tests/test_cc_pointer_deref_expr.py tests/test_cc_assign_expr.py tests/test_cc_casts.py tests/test_cc_bits.py tests/test_cc_bitfields.py tests/test_cc_local_structs.py tests/test_cc_fptr_array.py tests/test_cc_member_index_address.py -q` → all pass.
- [ ] **Step 6.4.** Reassembly: `python3 tests/test_asm.py 2>&1 | tail -3` → 49/49 green.
- [ ] **Step 6.5.** Programs bbfs: `python3 tests/test_programs.py 2>&1 | tail -5` → green.
- [ ] **Step 6.6.** Programs ext2: `python3 tests/test_programs.py --filesystem ext2 2>&1 | tail -5` → green.
- [ ] **Step 6.7.** Userland differential (final): re-run the Step 5.8 differential → `USERLAND BYTE-IDENTICAL`.
- [ ] **Step 6.8.** Lint/format (repo uses ruff per `.pre-commit-config`): `python3 -m ruff check cc tests && python3 -m ruff format --check cc tests` → clean (or run `ruff format` and re-verify the golden + differential, since formatting must not touch emitted bytes — it can't, it's Python source).
- [ ] **Step 6.9.** Final commit if any lint fixes were applied; otherwise the branch is ready for PR.

---

## 4. Self-review pass (run before declaring done)

- [ ] **Spec coverage.** Every scope item addressed: `DerefAssign` (deleted, §5.5; codegen reproduced in `_emit_dereference_place_store` named branch §3.3.2 incl. `out_register` trap 4); `PointerDereference` (deleted; reproduced in `_emit_dereference_place_load` §3.2 incl. fast path trap 3, width trap 2); `PointerDereferenceAssign` (deleted; reproduced in `_emit_dereference_place_store` cast branch); `DoubleIndex` (deleted; reproduced via `SubscriptPlace`-over-`DereferencePlace`-over-`Index` — **note:** the `_resolve_place` extension for this shape (§3.1 deferred) must be implemented; see open item below); `DerefIncrement`/`DerefIncrementAssign` (retained, memory access routed through Place core §3.5).
- [ ] **OPEN ITEM to resolve during execution — DoubleIndex `_resolve_place`/`_emit_place_load` path.** §3.1 chose to give `DereferencePlace` dedicated load/store branches that bypass `_resolve_place`. But `name[i][j]` is `SubscriptPlace(base=DereferencePlace(Index(...)), index=j)` — a `SubscriptPlace`, which currently routes through `_resolve_place` (shape A/B only) and would raise "unsupported Place shape". **Therefore `_emit_place_load` needs an explicit branch for `SubscriptPlace` whose base is a `DereferencePlace`, reproducing `_generate_double_index_expression` (§1.3) byte-for-byte (trap 5: stride from `type_size(pointee)` not `_index_pointee_size`; Int/Var/general sub-paths; `emit_byte_load_zx` vs full).** Add this branch in Step 3.2/3.3 alongside the standalone-`DereferencePlace` branches (insert a `self._is_double_index_place(place)` check → `self._emit_double_index_place_load(place)` before the standalone checks). Implement `_emit_double_index_place_load` as a verbatim port of `_generate_double_index_expression`, extracting `vname` from `place.base.pointer.array.name`, `outer_index` from `place.base.pointer.index`, `inner_index` from `place.index`. The oracle test's `double_index_*` cases already cover it. **This is mandatory and was under-specified in §3.1 — implement it.**
- [ ] **No placeholders.** All inserted code is complete. The only intentional API placeholders are the `_compile` helper's import/entry names in the oracle test (§3.4.1), flagged explicitly for the engineer to wire to the real `cc.py` pipeline.
- [ ] **Name/type consistency.** New methods: `_dereference_place_width`, `_emit_dereference_place_load`, `_emit_dereference_place_store`, `_emit_double_index_place_load`, `_is_double_index_place`, `_place_is_pure` (emission.py), `_add_place_uses` (liveness). All spell out expression/index/register/pointer/value. New nodes built by the parser: `PlaceLoad`, `PlaceStore`, `DereferencePlace`, `SubscriptPlace`, `Index`, `Cast`, `Var` — all existing. No new AST node classes added (good — `slots=True`/`kw_only=True` unaffected). Retained nodes: `DerefIncrement`, `DerefIncrementAssign`.
- [ ] **Alphabetical ordering** maintained in `ast_nodes.py` (deletions only), and new generator/emission methods placed to preserve the `def _` alphabetical convention.
- [ ] **Comments preserved** on all retained code; deleted code's comments go with it.
- [ ] **Byte-exactness empirically proven** three ways: golden snapshot (every shape), oracle test (hand-built Place vs legacy), and the full userland `.c` differential — plus `test_asm` reassembly and `test_programs` bbfs+ext2.
- [ ] **Liveness gap not widened (trap 8):** new Place handlers reproduce legacy use/def sets; the increment-arm handlers are kept ONLY if the userland differential stays byte-identical, else reverted to the prior unhandled/raise behavior (documented follow-up). The Plan-2 member-shape gap is closed for the shapes this plan owns via the `PlaceLoad`/`PlaceStore`/`_add_place_uses` handlers, again gated on the differential.

## 5. Critical Files for Implementation

- /home/ubuntu/bboeos/.claude/worktrees/parser/cc/parser.py
- /home/ubuntu/bboeos/.claude/worktrees/parser/cc/codegen/x86/generator.py
- /home/ubuntu/bboeos/.claude/worktrees/parser/cc/codegen/x86/emission.py
- /home/ubuntu/bboeos/.claude/worktrees/parser/cc/codegen/liveness.py
- /home/ubuntu/bboeos/.claude/worktrees/parser/cc/ast_nodes.py