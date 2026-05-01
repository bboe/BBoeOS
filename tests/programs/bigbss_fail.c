/* Boundary tripwire: declares BIGBSS_PAGES + 1 of BSS — exactly one
   page beyond what `bigbss` proves fits — to assert the boundary in
   bigbss_size.h is page-precise.  Pairs with the `bigbss_fail`
   entry in tests/test_programs.py, which boots -m 1024 and expects
   OOM (the recovery message + a follow-up `hello` running in the
   respawned shell).  If BIGBSS_PAGES drifts above the actual
   ceiling, this program would also fit and the test would fail
   (no OOM message). */

#include "bigbss_size.h"

#define INTS_PER_PAGE 1024
#define BIG_SIZE_INTS ((BIGBSS_PAGES + 1) * INTS_PER_PAGE)

int big_array[BIG_SIZE_INTS];

int main() {
    /* Unreachable: program_enter OOMs during phase-2 BSS allocation. */
    printf("bigbss_fail: unreachable — should have OOMed\n");
    return 0;
}
