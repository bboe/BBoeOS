// signal.c — SIGINT dispatch primitives.  Two entry points:
//   signal_dispatch_kill  — reset to a known kernel ESP, tear down the
//                           dying program's PD, jump to shell_reload.
//                           Reused by the SIG_DFL path and by handler-
//                           validation failures in SYS_SYS_SIGRETURN.
//   signal_dispatch_user  — capture interrupted register state into a
//                           sigcontext on the user stack, rewrite the
//                           CPU iret frame to enter the registered
//                           ring-3 handler, and iretd.  Reached from
//                           the SIGINT_TAIL_CHECK macro with pushad
//                           slots still on the kernel stack.
//
// signal_dispatch_kill never returns.  It is reachable only from kernel
// context (IRQ epilogue or syscall epilogue), so it can clobber every
// register and reset ESP without consulting the caller's frame.
//
// signal_dispatch_user does not return to its caller either — it iretds
// directly into the user handler.  It runs on the kernel stack with the
// macro-call frame intact (pushad slots + iret frame), so it can read
// register state straight out of [esp + ...] without any intermediate
// save.
//
// Functions are defined in alphabetical order by visible name, per
// CLAUDE.md.

extern uint32_t current_pd_phys;

asm("_g_current_pd_phys equ current_pd_phys");

// signal_dispatch_user references pending_sigint, in_sigint_handler, and
// sigint_handler directly by their entry.asm label names (no `_g_`
// prefix) — those globals are %included in kernel.asm before this file
// and ps2.c already publishes its own `_g_pending_sigint` alias, so
// re-equ'ing here would collide.  This file's only C-mangled global
// access is current_pd_phys (used by signal_dispatch_kill below).

void address_space_destroy(uint32_t pd_phys);
void put_character(char byte);

void signal_dispatch_kill();
void signal_dispatch_user();

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

// signal_dispatch_user — build a 48-byte sigcontext on the user stack,
// rewrite the CPU iret frame to enter the registered ring-3 handler,
// and iretd.  Reached from SIGINT_TAIL_CHECK when sigint_handler holds
// a user-virt address (not SIG_DFL / SIG_IGN) and we just interrupted
// CPL=3 with a pending SIGINT.
//
// Stack at entry (pushad slots live, cross-priv iret frame above):
//   [esp +  0] saved EDI         [esp + 16] saved EBX
//   [esp +  4] saved ESI         [esp + 20] saved EDX
//   [esp +  8] saved EBP         [esp + 24] saved ECX
//   [esp + 12] saved ESP_pushad  [esp + 28] saved EAX
//   [esp + 32] iret EIP          [esp + 40] iret EFLAGS
//   [esp + 36] iret CS           [esp + 44] iret ESP
//                                [esp + 48] iret SS
//
// Sigcontext layout (48 bytes, written at user_esp - 48):
//   +0   trampoline_addr (FUNCTION_TABLE + VDSO_SIGRETURN_OFFSET = 0x10450)
//   +4   signum (= SIGINT = 2)
//   +8   saved EIP    (interrupted user EIP)
//   +12  saved EFLAGS
//   +16  saved ESP    (original user ESP, before this 48-byte frame)
//   +20  saved EAX    +24 ECX  +28 EDX  +32 EBX
//   +36  saved EBP    +40 ESI  +44 EDI
//
// The user-stack writes go through the active PD (still the user's, since
// CR3 is not touched on IRQ / syscall entry) — a fault while we write
// (e.g. user is near the stack guard) vectors through exc_common, which
// kills the program.  That's the right behaviour.
//
// After building the sigcontext we rewrite [esp + 32] (iret EIP) to the
// handler address and [esp + 44] (iret ESP) to the sigcontext base, set
// in_sigint_handler = 1 and pending_sigint = 0, drop the pushad slots
// (their values now live in the sigcontext) by `add esp, 32`, and iretd.
asm("signal_dispatch_user:\n"
    "        mov edi, [esp + 44]\n"                 // user ESP
    "        sub edi, 48\n"                         // sigcontext base
    "        mov dword [edi + 0], FUNCTION_TABLE + VDSO_SIGRETURN_OFFSET\n"
    "        mov dword [edi + 4], SIGINT\n"
    "        mov eax, [esp + 32]\n"                 // iret EIP
    "        mov [edi + 8], eax\n"
    "        mov eax, [esp + 40]\n"                 // iret EFLAGS
    "        mov [edi + 12], eax\n"
    "        mov eax, [esp + 44]\n"                 // original user ESP
    "        mov [edi + 16], eax\n"
    "        mov eax, [esp + 28]\n"                 // saved EAX
    "        mov [edi + 20], eax\n"
    "        mov eax, [esp + 24]\n"                 // saved ECX
    "        mov [edi + 24], eax\n"
    "        mov eax, [esp + 20]\n"                 // saved EDX
    "        mov [edi + 28], eax\n"
    "        mov eax, [esp + 16]\n"                 // saved EBX
    "        mov [edi + 32], eax\n"
    "        mov eax, [esp + 8]\n"                  // saved EBP
    "        mov [edi + 36], eax\n"
    "        mov eax, [esp + 4]\n"                  // saved ESI
    "        mov [edi + 40], eax\n"
    "        mov eax, [esp + 0]\n"                  // saved EDI
    "        mov [edi + 44], eax\n"
    "        mov eax, [sigint_handler]\n"
    "        mov [esp + 32], eax\n"                 // iret EIP <- handler
    "        mov [esp + 44], edi\n"                 // iret ESP <- sigcontext base
    "        mov byte [in_sigint_handler], 1\n"
    "        mov byte [pending_sigint], 0\n"
    "        add esp, 32\n"                         // drop pushad slots
    "        iretd\n");
