/* Maximum-BSS stress test.  Declares BIGBSS_PAGES (the empirically-
   measured ceiling — see bigbss_size.h) of BSS and writes/verifies
   one int per page.  Pairs with two test_programs.py entries:

     * `bigbss`     — boots -m 1024, succeeds.
     * `bigbss_oom` — boots -m 1023 (one MB less RAM), expects OOM
       partway through phase 2 and graceful recovery via the
       respawned shell.  Tripwire if BIGBSS_PAGES is set too low.

   `bigbss_fail` (separate program) declares BIGBSS_PAGES + 1 and
   tripwires against BIGBSS_PAGES being set too high. */

#include "bigbss_size.h"

#define INTS_PER_PAGE 1024
#define BIG_SIZE_INTS (BIGBSS_PAGES * INTS_PER_PAGE)

int big_array[BIG_SIZE_INTS];

int main() {
    printf("bigbss: writing pattern\n");
    int i = 0;
    int idx = 0;
    while (i < BIGBSS_PAGES) {
        big_array[idx] = i;
        idx = idx + INTS_PER_PAGE;
        i = i + 1;
    }
    printf("bigbss: verifying pattern\n");
    i = 0;
    idx = 0;
    while (i < BIGBSS_PAGES) {
        if (big_array[idx] != i) {
            printf("bigbss: FAIL at page %d\n", i);
            return 1;
        }
        idx = idx + INTS_PER_PAGE;
        i = i + 1;
    }
    printf("bigbss: OK\n");
    return 0;
}
