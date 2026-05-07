// signal.c — SIGINT dispatch primitives.  Two entry points:
//   signal_dispatch_kill  — reset to a known kernel ESP, tear down the
//                           dying program's PD, jump to shell_reload.
//                           Reused by the SIG_DFL path and by handler-
//                           validation failures in SYS_SYS_SIGRETURN.
//   signal_dispatch_user  — Phase 4 (Task 11) fills this in.  For now
//                           the IRQ-epilogue dispatch macro routes
//                           user-handler-registered cases through
//                           signal_dispatch_kill, so this file does
//                           not need a stub yet — but the symbol will
//                           be added in Task 11.
//
// signal_dispatch_kill never returns.  It is reachable only from kernel
// context (IRQ epilogue or syscall epilogue), so it can clobber every
// register and reset ESP without consulting the caller's frame.
//
// Functions are defined in alphabetical order by visible name, per
// CLAUDE.md.

extern uint32_t current_pd_phys;
asm("_g_current_pd_phys equ current_pd_phys");

void address_space_destroy(uint32_t pd_phys);
void put_character(char byte);

void signal_dispatch_kill();

asm("signal_dispatch_kill:\n"
    "        mov esp, kernel_stack_top\n"
    "        mov al, '^'\n"
    "        call put_character\n"
    "        mov al, 'C'\n"
    "        call put_character\n"
    "        mov al, 0x0A\n"
    "        call put_character\n"
    "        mov eax, [_g_current_pd_phys]\n"
    "        test eax, eax\n"
    "        jz .signal_dispatch_kill_no_pd\n"
    "        push eax\n"
    "        call address_space_destroy\n"
    "        add esp, 4\n"
    "        mov dword [_g_current_pd_phys], 0\n"
    ".signal_dispatch_kill_no_pd:\n"
    "        jmp shell_reload\n");
