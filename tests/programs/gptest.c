/* Smoke test for the user-fault kill path in idt.asm's exc_common.
   Executes `cli` at CPL=3 with IOPL=0, which raises #GP.  The kernel
   tears down this program's PD and re-enters shell_reload — the next
   shell command should run as if nothing happened.  Pairs with the
   `gptest` entry in tests/test_programs.py, which asserts the EXC0D
   diagnostic and a successful follow-up command. */
int main() {
    printf("gptest: cli from ring 3\n");
    asm("cli");
    printf("unreachable: kill path failed\n");
    return 0;
}
