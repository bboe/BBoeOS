/* Smoke test for large BSS allocation in the per-program PD.
   Declares a 256 KB BSS array, writes a unique value (the index) into
   each int slot, then reads it back.  If program_enter's BSS walk
   skips any page or the frame allocator double-hands a frame, one of
   the verify reads fails — and the index in the FAIL message points
   to the specific slot.  Pairs with the `bigbss` entry in
   tests/test_programs.py. */

#define BIG_SIZE (64 * 1024)
int big_array[BIG_SIZE];

int main() {
    printf("bigbss: writing pattern\n");
    int i = 0;
    while (i < BIG_SIZE) {
        big_array[i] = i;
        i = i + 1;
    }
    printf("bigbss: verifying pattern\n");
    i = 0;
    while (i < BIG_SIZE) {
        if (big_array[i] != i) {
            printf("bigbss: FAIL at %d\n", i);
            return 1;
        }
        i = i + 1;
    }
    printf("bigbss: OK\n");
    return 0;
}
