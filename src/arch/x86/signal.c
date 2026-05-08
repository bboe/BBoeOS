// signal.c — SIGINT default-kill primitive.  Single entry point:
//   signal_dispatch_kill — reset to a known kernel ESP, tear down the
//                          dying program's PD, and jump to shell_reload.
//                          Reached from the SIGINT_TAIL_CHECK macro at
//                          IRQ / syscall iret epilogues when
//                          pending_sigint is set and the interrupted
//                          frame is CPL=3.  Always-kill in this PR;
//                          a follow-up adds SIG_IGN and user-handler
//                          delivery.
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
    // Switch CR3 to kernel_idle_pd before address_space_destroy frees
    // the dying PD's frame; mirrors .sys_exit (syscall.asm).  Without
    // this, CR3 is left dangling at a freed-then-reallocated frame and
    // the next kernel-virt access through it page-faults (EXC0E).
    "        push eax\n"
    "        mov eax, [kernel_idle_pd_phys]\n"
    "        mov cr3, eax\n"
    "        pop eax\n"
    "        push eax\n"
    "        call address_space_destroy\n"
    "        add esp, 4\n"
    "        mov dword [_g_current_pd_phys], 0\n"
    ".signal_dispatch_kill_no_pd:\n"
    "        jmp shell_reload\n");
