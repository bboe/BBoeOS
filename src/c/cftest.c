/* Smoke test for cc.py's ``__attribute__((carry_return))`` calling
   convention.  A ``carry_return`` function reports its int return via
   the carry flag rather than AX: ``return 1`` emits ``clc`` before the
   epilogue (true / success), ``return 0`` emits ``stc`` (false /
   failure).  Callers use it in an ``if`` / ``while`` condition where
   cc.py dispatches through ``jnc`` (true) / ``jc`` (false) — no AX
   round-trip, no ``test ax, ax`` before the branch.

   Exercises:
     - a regparm(1) + carry_return helper (arg in AX, result in CF)
     - a no-arg carry_return helper
     - the call as an ``if`` condition (true-path taken)
     - the call as an ``if`` condition (false-path taken)
     - the call as a ``while`` condition (loops until false) */

__attribute__((regparm(1)))
__attribute__((carry_return))
int is_positive(int v) {
    if (v > 0) {
        return 1;
    }
    return 0;
}

int remaining;

__attribute__((carry_return))
int tick() {
    if (remaining > 0) {
        remaining = remaining - 1;
        return 1;
    }
    return 0;
}

int main() {
    if (is_positive(5)) {
        printf("is_positive(5): true\n");
    } else {
        printf("is_positive(5): false\n");
    }

    if (is_positive(0)) {
        printf("is_positive(0): true\n");
    } else {
        printf("is_positive(0): false\n");
    }

    remaining = 3;
    int steps = 0;
    while (tick()) {
        steps = steps + 1;
    }
    printf("tick() fired %d times, remaining = %d\n", steps, remaining);
    return 0;
}
