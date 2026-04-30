/* Smoke test for the user-fault kill path on a NULL dereference.
   Writes to virtual address 0, which is unmapped in every per-program
   PD: PTE[0] (covering 0..0xFFF) stays not-present so any access to
   the first page raises #PF.  Programs that need the shell↔program
   handoff frame (ARGV / EXEC_ARG / BUFFER) reach it through user-virt
   USER_DATA_BASE = 0x1000 (PTE[1]) instead.  The CPU raises #PF, the
   kernel sees a user-mode fault, tears down the PD, and re-enters
   shell_reload.  Pairs with the `nullderef` entry in tests/test_programs.py.

   The store goes through inline asm because cc.py doesn't accept
   ``*(int *)0 = 42``; the asm produces the same encoding the cast
   would. */
int main() {
    printf("nullderef: writing to NULL\n");
    asm("mov dword [0], 42");
    printf("unreachable: kill path failed\n");
    return 0;
}
