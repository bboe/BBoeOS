/* Smoke test for the stack guard page (one page below the 128 KB user
   stack at 0x3FFE0000).  Recurses with a 1 KB local frame until the
   guard page faults; the kernel sees a user-mode #PF with cr2 inside
   the guard region, tears down the PD, and re-enters shell_reload.
   Pairs with the `stackbomb` entry in tests/test_programs.py. */

void recurse(int depth) {
    char frame[1024];
    int i = 0;
    while (i < 1024) {
        frame[i] = depth;
        i = i + 1;
    }
    recurse(depth + 1);
}

int main() {
    printf("stackbomb: starting recursion\n");
    recurse(0);
    printf("unreachable: stack guard missed\n");
    return 0;
}
