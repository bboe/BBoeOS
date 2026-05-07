/* End-to-end smoke test for SIGINT handler delivery and sigreturn.
   Registers on_sigint as the SIGINT handler, then calls SYS_IO_READ.
   The serial Ctrl+C (0x03) byte is already queued when the read fires:
   fd_read_console reads it, sets pending_sigint, and returns the byte
   (AX = 1, CF clear).  The syscall epilogue's SIGINT_TAIL_CHECK sees
   pending_sigint and calls signal_dispatch_user, which builds a
   sigcontext on the user stack and iretds into on_sigint.  on_sigint
   sets got_sigint = 1 and returns through the vDSO sigreturn
   trampoline.  signal_resume_after_handler restores the interrupted
   register state and iretds back to user code at the instruction
   following the SYS_IO_READ int 30h.  Main checks got_sigint and
   prints CAUGHT to confirm the full delivery and sigreturn round-trip.

   Pairs with the sigint_test entry in tests/test_programs.py. */

int got_sigint;
char read_buf[4];

void on_sigint(int signum) {
    got_sigint = 1;
}

int main() {
    asm("mov ebx, SIGINT\n"
        "mov ecx, on_sigint\n"
        "mov ah, SYS_SYS_SIGNAL\n"
        "int 30h\n");
    asm("mov ebx, 0\n"
        "mov edi, _g_read_buf\n"
        "mov ecx, 1\n"
        "mov ah, SYS_IO_READ\n"
        "int 30h\n");
    if (got_sigint) {
        printf("CAUGHT\n");
    } else {
        printf("NO_SIGNAL\n");
    }
    return 0;
}
