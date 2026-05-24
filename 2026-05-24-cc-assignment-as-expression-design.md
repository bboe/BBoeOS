# cc.py: assignment as expression (parens required)

## Motivation

`cc.py` today treats assignment as a statement only.  Expressions like
`while ((*dst++ = *src++))` (the classic `strcpy` idiom in
`user/libc/string.c:210`) therefore can't compile, and any C source
that uses `=` inside a condition, function argument, or `return` has
to be rewritten before cc.py will accept it.  Several remaining
unported files in `user/libc/` are blocked on this one feature.

The goal is to teach cc.py to accept assignments as expressions —
producing the post-assignment value — while keeping the parser
unambiguous and discouraging the `if (x = y)` typo that motivated
GCC's `-Wparentheses` in the first place.

## Surface rule

An assignment is an expression **iff** it appears in source wrapped in
its own dedicated pair of parentheses:

```
( <lvalue> <assign-op> <rhs> )
```

where `<assign-op>` is the top-level operator inside the wrapping
parens (the RHS may of course be any expression, including another
parenthesized assignment).  The wrapping
parentheses must belong to the assignment itself — parentheses
inherited from an enclosing construct (a `while ( ... )`, a function
call `f( ... )`, etc.) do not count.

Accepted:

```c
while ((p = next))            // dedicated inner parens
*dst++ = (*src++ = ch);       // RHS is a parenthesized assignment
f((x = y), (z += 1));         // each arg has its own wrapping
return (x = y);
a = (b = c);                  // explicit right-to-left chain
if ((p = lookup(name)))
```

Rejected:

```c
if (x = y)                    // only one paren pair, not dedicated
f(x = y)                      // call parens don't count
a = b = c;                    // no parens around inner assignment
```

Top-level assignment statements (`x = y;`) are unchanged — they
remain statements and do not require parentheses.

## Operators covered

All eleven assignment operators:

```
=  +=  -=  *=  /=  %=  &=  |=  ^=  <<=  >>=
```

Compound forms desugar exactly as the statement form does today —
`lhs op= rhs` becomes `lhs = lhs op rhs`.  The only new work is
keeping the stored value live in the result register instead of
discarding it.

## Lvalue forms covered

Whatever the existing statement-assignment paths accept:

- Bare variable: `x`
- Pointer dereference: `*p`, `*(T *)p`
- Pre/post increment on the deref base: `*p++`, `*++p`
- Array index: `a[i]`
- Struct member: `s.f`, `p->f`
- Combinations: `a[i].f`, `s.a[i]`, `p->a[i].f`, etc.

Each existing `*Assign` AST node (`Assign`, `DerefAssign`,
`PointerDereferenceAssign`, `IndexAssign`, `MemberAssign`,
`IndexMemberAssign`, `MemberIndexAssign`, `IndexMemberIndexAssign`,
`DerefIncrementAssign`, …) gets an expression-context lowering
counterpart that leaves the assigned value in EAX.

## Result value and type

- **Value:** the post-assignment value of the lvalue (for `=`, that
  is the RHS after any implicit conversion; for compound forms, the
  value just stored).
- **Type:** the lvalue's declared type.
- **Not an lvalue:** `((x = y)) = z` is a compile error.  C-style
  right-to-left chains must be written with explicit parens:
  `a = (b = c)`.

## Parser implementation

A new hook in the primary-expression path.  When the parser sees `(`
at the start of a primary expression, it does a bounded try-parse:

1. Speculatively parse an lvalue.
2. If the next token is one of the eleven assignment operators,
   commit: parse the RHS, require a closing `)`, and emit an
   `AssignExpr` (or `CompoundAssignExpr`) AST node carrying the
   underlying `*Assign` shape.
3. Otherwise rewind and fall through to the existing parenthesized-
   expression path.

The "dedicated parens" rule falls out for free: any context that
wants assignment-as-expression must literally write `(...)` around
the assignment, because that is the only production that recognizes
the form.  No new disambiguation logic is needed in `if`/`while`/
function-call/etc. — they each parse a normal expression, and that
expression happens to be allowed to be an `AssignExpr`.

Bare `x = y;` continues to be parsed by the existing statement path
(which never goes through the primary-expression hook), so the
statement form is untouched.

## Codegen

One helper per lvalue shape that:

1. Computes the lvalue's address / register as the existing
   statement-form `*Assign` lowering does.
2. Evaluates the RHS into a register.
3. Performs the store.
4. Leaves the stored value in EAX as the expression result.

For compound assignments, the existing desugaring (`x op= y` →
`x = x op y`) is reused; the only change is the trailing "value in
EAX" exit.

## Test plan

New tests under `tests/test_cc_*` covering:

- Accept: `(*dst++ = *src++)` (string.c idiom), each of the eleven
  operators inside parens, `a = (b = c)` chaining, use inside
  `while`, `if`, `for` (init / cond / step), ternary branches,
  function-call arguments, `return` expressions.
- Lvalue variety: `(x = y)`, `(*p = y)`, `(a[i] = y)`, `(s.f = y)`,
  `(p->f = y)`, `(*p++ = y)`, `(a[i].f = y)`.
- Reject: `if (x = y)`, `f(x = y)`, `a = b = c`, `((x = y)) = z`.
- Re-enable the blocked `strcpy`-style loop in `user/libc/string.c`
  and confirm the existing program / asm / ext2 suites still pass.

## Out of scope

- Assignment as lvalue (`(x = y) = z`).
- Comma operator.
- Statement expressions (`({ ... })`).
- Removing the parenthesization requirement.  The requirement is
  permanent — it is the feature, not a temporary restriction.
