// signal.c — SIGINT dispatch primitives.  Three entry points:
//   signal_dispatch_kill          — reset to a known kernel ESP, tear
//                                   down the dying program's PD, jump
//                                   to shell_reload.  Reused by the
//                                   SIG_DFL path and by handler-
//                                   validation failures in
//                                   SYS_SYS_SIGRETURN.
//   signal_dispatch_user          — capture interrupted register state
//                                   into a sigcontext on the user
//                                   stack, rewrite the CPU iret frame
//                                   to enter the registered ring-3
//                                   handler, and iretd.  Reached from
//                                   the SIGINT_TAIL_CHECK macro with
//                                   pushad slots still on the kernel
//                                   stack.
//   signal_resume_after_handler   — restore the interrupted register
//                                   state from a sigcontext on the
//                                   user stack, then iretd back to the
//                                   pre-signal user code.  Reached
//                                   from .sys_sigreturn (SYS_SYS_-
//                                   SIGRETURN) via the vDSO trampoline
//                                   that the user handler returns to.
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
void signal_resume_after_handler();

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

// signal_resume_after_handler — service SYS_SYS_SIGRETURN.  Restore the
// interrupted register state (saved by signal_dispatch_user into a
// sigcontext on the user stack) into the syscall-entry kernel frame, so
// the syscall epilogue's popad + iretd resumes user code at the point
// the SIGINT preempted it.
//
// Reach path: signal_dispatch_user iretds into the user handler with
// ESP pointing at sigcontext base.  The handler runs as a plain C-style
// function and ends with `ret`, which pops the first dword (the
// trampoline address at sigcontext+0) into EIP and lands the CPU at the
// vDSO sigreturn trampoline (FUNCTION_TABLE + VDSO_SIGRETURN_OFFSET).
// The trampoline executes `mov eax, 0xF6 / int 0x30` and we re-enter
// the kernel through the syscall dispatcher.  Cross-priv iret + pushad
// produced the standard syscall frame:
//   [esp +  0..28]  pushad slots EDI..EAX (junk — the trampoline only
//                   touched EAX)
//   [esp + 32]      iret EIP   (back into the trampoline; unused)
//   [esp + 36]      iret CS
//   [esp + 40]      iret EFLAGS
//   [esp + 44]      iret ESP   (== sigcontext base + 4, because the
//                                handler's `ret` popped the trampoline
//                                address)
//   [esp + 48]      iret SS
//
// Let edi = [esp + 44] = sigcontext_base + 4.  The remaining sigcontext
// fields (signum at +4, saved_eip at +8, ..., saved_edi at +44) are
// reachable as [edi + 0], [edi + 4], ..., [edi + 40].
//
// We:
//   1. Validate saved_eip is in user-virt (PROGRAM_BASE..KERNEL_VIRT_BASE).
//      Fail → signal_dispatch_kill (corrupt sigcontext == bad program).
//   2. Validate saved_esp is in user-virt (PROGRAM_BASE..=KERNEL_VIRT_BASE,
//      since USER_STACK_TOP == KERNEL_VIRT_BASE is the legal high bound).
//   3. Rewrite iret EIP / EFLAGS / ESP from saved_eip / saved_eflags /
//      saved_esp.
//   4. Rewrite the kernel-stack pushad slots from saved_eax .. saved_edi
//      so the syscall epilogue's popad sees the user's pre-signal regs.
//   5. Clear in_sigint_handler so SIGINT can deliver again.
//   6. If pending_sigint was set during the handler, redeliver it now —
//      either kill (SIG_DFL), drop it (SIG_IGN), or jump straight to
//      signal_dispatch_user, which reads the just-rewritten [esp + 44]
//      and builds a fresh sigcontext on the now-restored user stack.
//   7. popad + iretd to user code at saved_eip.
//
// The function never returns to its caller.  .sys_sigreturn jumps here
// rather than calling — we own the popad and iretd.
asm("signal_resume_after_handler:\n"
    "        mov edi, [esp + 44]\n"                 // user ESP = sigcontext_base + 4
    "        mov eax, [edi + 4]\n"                  // saved_eip
    "        cmp eax, PROGRAM_BASE\n"
    "        jb  signal_dispatch_kill\n"
    "        cmp eax, KERNEL_VIRT_BASE\n"
    "        jae signal_dispatch_kill\n"
    "        mov eax, [edi + 12]\n"                 // saved_esp
    "        cmp eax, PROGRAM_BASE\n"
    "        jb  signal_dispatch_kill\n"
    "        cmp eax, KERNEL_VIRT_BASE\n"
    "        ja  signal_dispatch_kill\n"
    "        mov eax, [edi + 4]\n"                  // saved_eip
    "        mov [esp + 32], eax\n"                 // iret EIP
    "        mov eax, [edi + 8]\n"                  // saved_eflags
    // Sanitize before reloading into the iret frame: the user controls
    // every bit of saved_eflags via the on-stack sigcontext, so without
    // masking a handler could return with IOPL=3 (ring-3 in/out) or
    // VM=1 (Virtual-8086 entry) — privilege escalation.  Keep only
    // CF/PF/AF/ZF/SF/TF/DF/OF; force IF=1 so user code stays
    // interruptible.
    "        and eax, USER_EFLAGS_MASK\n"
    "        or  eax, EFLAGS_IF_BIT\n"
    "        mov [esp + 40], eax\n"                 // iret EFLAGS
    "        mov eax, [edi + 12]\n"                 // saved_esp
    "        mov [esp + 44], eax\n"                 // iret ESP
    "        mov eax, [edi + 16]\n"                 // saved_eax  -> pushad EAX slot
    "        mov [esp + 28], eax\n"
    "        mov eax, [edi + 20]\n"                 // saved_ecx
    "        mov [esp + 24], eax\n"
    "        mov eax, [edi + 24]\n"                 // saved_edx
    "        mov [esp + 20], eax\n"
    "        mov eax, [edi + 28]\n"                 // saved_ebx
    "        mov [esp + 16], eax\n"
    "        mov eax, [edi + 32]\n"                 // saved_ebp
    "        mov [esp + 8], eax\n"
    "        mov eax, [edi + 36]\n"                 // saved_esi
    "        mov [esp + 4], eax\n"
    "        mov eax, [edi + 40]\n"                 // saved_edi
    "        mov [esp + 0], eax\n"
    "        mov byte [in_sigint_handler], 0\n"
    "        cmp byte [pending_sigint], 0\n"
    "        je  .signal_resume_no_pending\n"
    "        mov eax, [sigint_handler]\n"
    "        cmp eax, SIG_DFL\n"
    "        je  signal_dispatch_kill\n"
    "        cmp eax, SIG_IGN\n"
    "        jne signal_dispatch_user\n"
    "        mov byte [pending_sigint], 0\n"
    ".signal_resume_no_pending:\n"
    "        popad\n"
    "        iretd\n");
