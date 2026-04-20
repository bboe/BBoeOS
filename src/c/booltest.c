/* Smoke test for cc.py's booleanization of comparison BinOps used
   as expression values (``int x = (expr <op> val);`` / as a call
   argument / as a subexpression in arithmetic).  Prior to the fix,
   the codegen emitted ``cmp / mov ax, 0`` and dropped the compare
   result, so every booleanization landed 0 regardless of input.
   Each line prints the observed value followed by the expected
   0 / 1 so a text-diff run catches regressions cheaply. */

int main() {
    int a = 7;
    int b = 7;
    int c = 12;

    int eq_true  = (a == b);          /* 1 */
    int eq_false = (a == c);          /* 0 */
    int ne_true  = (a != c);          /* 1 */
    int ne_false = (a != b);          /* 0 */
    int lt_true  = (a < c);           /* 1 */
    int lt_false = (c < a);           /* 0 */
    int le_true  = (a <= b);          /* 1 */
    int le_false = (c <= a);          /* 0 */
    int gt_true  = (c > a);           /* 1 */
    int gt_false = (a > c);           /* 0 */
    int ge_true  = (a >= b);          /* 1 */
    int ge_false = (a >= c);          /* 0 */

    printf("eq_true  = %u\n", eq_true);
    printf("eq_false = %u\n", eq_false);
    printf("ne_true  = %u\n", ne_true);
    printf("ne_false = %u\n", ne_false);
    printf("lt_true  = %u\n", lt_true);
    printf("lt_false = %u\n", lt_false);
    printf("le_true  = %u\n", le_true);
    printf("le_false = %u\n", le_false);
    printf("gt_true  = %u\n", gt_true);
    printf("gt_false = %u\n", gt_false);
    printf("ge_true  = %u\n", ge_true);
    printf("ge_false = %u\n", ge_false);

    /* Arithmetic on the boolean result: ``a == b`` must actually be
       ``1`` (not 0) for the sum to come out right. */
    int sum = (a == b) + (c > a) + (a != c);  /* 3 */
    printf("sum      = %u\n", sum);

    return 0;
}
