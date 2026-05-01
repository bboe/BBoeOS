/* Smoke test for cc.py's ``__attribute__((regparm(1)))`` calling
   convention.  Callers load arg 0 into AX before ``call fn``; the
   prologue spills AX into a local stack slot so the body can read
   the parameter through the normal local path.  Exercises:
     - fastcall called with a constant literal
     - fastcall called with a local variable
     - fastcall called with a computed expression
     - a fastcall helper that itself calls another fastcall helper
       (arg 0 eval must come last so earlier pushes can't trash AX) */

__attribute__((regparm(1)))
int add_one(int v) {
    return v + 1;
}

__attribute__((regparm(1)))
int doubled(int v) {
    return v + v;
}

__attribute__((regparm(1)))
int accumulate(int v) {
    return doubled(v) + add_one(v);
}

int main() {
    printf("add_one(41)      = %d\n", add_one(41));             /* 42 */
    int x = 10;
    printf("add_one(x + 5)   = %d\n", add_one(x + 5));          /* 16 */
    printf("doubled(x)       = %d\n", doubled(x));              /* 20 */
    printf("nested           = %d\n", add_one(doubled(7)));     /* 15 */
    printf("accumulate(9)    = %d\n", accumulate(9));           /* 28 */
    return 0;
}
