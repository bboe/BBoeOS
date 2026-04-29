/* Smoke test for the user-fault kill path on a #PF.
   Writes to virtual 0x00400000 — the start of PDE[1], which is
   unconditionally absent in the per-program PD (program_enter only
   populates the user pages it explicitly walks: image, BSS, stack, and
   the shared low-virt frames at PDE[0]).  Note that virt 0 itself is
   *not* unmapped — the BUFFER/EXEC_ARG/vDSO frames live under PDE[0]'s
   first PT, so virt 0..0xFFF is reachable via that PT's first PTE.
   0x00400000 is the next PDE boundary up and has no PT installed at
   all, so the write raises #PF cleanly.  The kernel sees a user-mode
   fault, tears down the PD, and re-enters shell_reload.  Pairs with
   the `nullderef` entry in tests/test_programs.py.

   The store goes through inline asm because cc.py doesn't accept
   ``*(int *)<addr> = 42``; the asm produces the same encoding the
   cast would. */
int main() {
    printf("nullderef: writing to unmapped user-virt\n");
    asm("mov dword [0x00400000], 42");
    printf("unreachable: kill path failed\n");
    return 0;
}
