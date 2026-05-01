/* Smoke test for cc.py's #include directive.  Pulls a #define and a
   helper function out of a sibling header and exercises both. */

#include "inctest.h"

int main() {
    printf("magic  = %u\n", INCTEST_MAGIC);      /* 3054 */
    printf("square = %u\n", inctest_square(12)); /* 144 */
    return 0;
}
