/* convention_test — consolidated cc.py calling-convention smoke tests.
 *
 * Two modes, each previously its own program:
 *
 *   carry    — exercises ``__attribute__((carry_return))``: an int
 *              return reported via the carry flag (clc = true, stc =
 *              false), so callers branch via jnc/jc without an
 *              ax-to-flags round trip.  Previously cftest.c.
 *
 *   regparm  — exercises ``__attribute__((regparm(1)))``: arg 0 in AX
 *              before call, callee spills to a local slot.  Covers
 *              literal / local / expression / nested-fastcall call
 *              sites.  Previously fctest.c. */

int remaining;

/* Forward declarations — clang requires them since main() is sorted
   alphabetically and lands ahead of every callee it dispatches to.
   cc.py's whole-file pre-pass resolves these without prototypes. */
__attribute__((regparm(1))) int accumulate(int v);
__attribute__((regparm(1))) int add_one(int v);
__attribute__((regparm(1))) int doubled(int v);
__attribute__((regparm(1))) __attribute__((carry_return)) int
is_positive(int v);
void mode_carry();
void mode_regparm();
int string_equal(char *left, char *right);
__attribute__((carry_return)) int tick();

__attribute__((regparm(1))) int accumulate(int v) {
    return doubled(v) + add_one(v);
}

__attribute__((regparm(1))) int add_one(int v) {
    return v + 1;
}

__attribute__((regparm(1))) int doubled(int v) {
    return v + v;
}

__attribute__((regparm(1))) __attribute__((carry_return)) int
is_positive(int v) {
    if (v > 0) {
        return 1;
    }
    return 0;
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        die("convention_test: pass a mode\n");
    }
    char *mode = argv[1];
    if (string_equal(mode, "carry")) {
        mode_carry();
    } else if (string_equal(mode, "regparm")) {
        mode_regparm();
    } else {
        die("convention_test: unknown mode\n");
    }
    return 0;
}

void mode_carry() {
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
        steps += 1;
    }
    printf("tick() fired %d times, remaining = %d\n", steps, remaining);
}

void mode_regparm() {
    printf("add_one(41)      = %d\n", add_one(41)); /* 42 */
    int x = 10;
    printf("add_one(x + 5)   = %d\n", add_one(x + 5));      /* 16 */
    printf("doubled(x)       = %d\n", doubled(x));          /* 20 */
    printf("nested           = %d\n", add_one(doubled(7))); /* 15 */
    printf("accumulate(9)    = %d\n", accumulate(9));       /* 28 */
}

int string_equal(char *left, char *right) {
    int index = 0;
    while (left[index] != '\0' && right[index] != '\0') {
        if (left[index] != right[index]) {
            return 0;
        }
        index = index + 1;
    }
    return left[index] == right[index];
}

__attribute__((carry_return)) int tick() {
    if (remaining > 0) {
        remaining -= 1;
        return 1;
    }
    return 0;
}
