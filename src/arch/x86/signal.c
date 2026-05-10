// signal.c — Signal dispatch primitives.  Three entry points:
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
//                                   the SIGNAL_TAIL_CHECK macro with
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
// signal_dispatch_kill input contract: EDX = signum (SIGINT, SIGALRM,
// or 0 for "validation failure — print '^?\n'").  Caller must load
// EDX before jumping here.  No other register inputs.
//
// signal_dispatch_user does not return to its caller either — it iretds
// directly into the user handler.  It runs on the kernel stack with the
// macro-call frame intact (pushad slots + iret frame), so it can read
// register state straight out of [esp + ...] without any intermediate
// save.
//
// Functions are defined in alphabetical order by visible name, per
// CLAUDE.md.

#include "program_state.h"

asm("_g_current_program_state equ current_program_state");

// signal_dispatch_user and signal_resume_after_handler access all signal
// state through current_program_state using PROGRAM_STATE_OFFSET_* offsets.
// current_program_state is referenced by its entry.asm label name (no `_g_`
// prefix) in asm strings, and also as a C-mangled extern below (for
// signal_dispatch_kill which reads it via C).

void address_space_destroy(uint32_t pd_phys);
void put_character(char byte);

void signal_dispatch_kill();
void signal_dispatch_user();
void signal_resume_after_handler();

asm("signal_dispatch_kill:\n"
    "        mov esp, kernel_stack_top\n"
    "        mov al, '^'\n"
    "        call put_character\n"
    "        cmp edx, SIGINT\n"
    "        jne .signal_dispatch_kill_not_sigint\n"
    "        mov al, 'C'\n"
    "        jmp .signal_dispatch_kill_emit\n"
    ".signal_dispatch_kill_not_sigint:\n"
    "        cmp edx, SIGALRM\n"
    "        jne .signal_dispatch_kill_unknown\n"
    "        mov al, 'A'\n"
    "        jmp .signal_dispatch_kill_emit\n"
    ".signal_dispatch_kill_unknown:\n"
    "        mov al, '?'\n"
    ".signal_dispatch_kill_emit:\n"
    "        call put_character\n"
    "        mov al, 0x0A\n"
    "        call put_character\n"
    "        mov eax, [_g_current_program_state]\n"
    "        mov eax, [eax + PROGRAM_STATE_OFFSET_PD_PHYS]\n"
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
    "        mov edx, [_g_current_program_state]\n"
    "        mov dword [edx + PROGRAM_STATE_OFFSET_PD_PHYS], 0\n"
    ".signal_dispatch_kill_no_pd:\n"
    "        jmp shell_reload\n");

// signal_dispatch_user — build a 52-byte sigcontext on the user stack,
// rewrite the CPU iret frame to enter the registered ring-3 handler,
// and iretd.  Reached from SIGNAL_TAIL_CHECK with EAX = handler
// (user-virt, validated to be neither SIG_DFL nor SIG_IGN by the
// macro), EDX = signum (SIGINT or SIGALRM).
//
// EBP holds the stashed handler across the rep movsd / iret-frame reads
// (EAX gets clobbered).
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
// Sigcontext layout (52 bytes, written at user_esp - 52).  The 8-dword
// register block at +8..+36 is laid out in pushad's natural order (EDI
// first, EAX last) so signal_dispatch_user / signal_resume_after_handler
// can move it as a single rep movsd instead of 7 read-write pairs.  The
// pushad ESP slot at +20 is preserved verbatim through the cycle even
// though popad ignores it on restore — keeping the bulk-copy contiguous
// is worth one redundant dword on the user stack.
//   +0   trampoline_addr (FUNCTION_TABLE + VDSO_SIGRETURN_OFFSET = 0x10450)
//   +4   signum (SIGINT=2 or SIGALRM=14, written from EDX)
//   +8   saved EDI    +12 ESI  +16 EBP  +20 ESP_pushad
//   +24  saved EBX    +28 EDX  +32 ECX  +36 EAX
//   +40  saved EIP    (interrupted user EIP)
//   +44  saved EFLAGS
//   +48  saved ESP    (original user ESP, before this 52-byte frame)
//
// The user-stack writes go through the active PD (still the user's, since
// CR3 is not touched on IRQ / syscall entry) — a fault while we write
// (e.g. user is near the stack guard) vectors through exc_common, which
// kills the program.  That's the right behaviour.
//
// After building the sigcontext we rewrite [esp + 32] (iret EIP) to the
// handler address and [esp + 44] (iret ESP) to the sigcontext base, set
// set IN_SIGNAL_HANDLER = 1 and clear the pending bit corresponding to EDX
// (PENDING_SIGINT or PENDING_SIGALRM) via current_program_state, drop the
// pushad slots (their values now live in the sigcontext) by `add esp, 32`,
// and iretd.
asm("signal_dispatch_user:\n"
    "        mov ebp, eax\n"                        // stash handler — EAX gets clobbered below
    "        mov edi, [esp + 44]\n"                 // user ESP
    "        sub edi, 52\n"                         // sigcontext base
    "        mov dword [edi + 0], FUNCTION_TABLE + VDSO_SIGRETURN_OFFSET\n"
    "        mov [edi + 4], edx\n"                  // signum from caller (SIGINT or SIGALRM)
    // Bulk-copy the 8 pushad slots from kernel stack to sigcontext + 8.
    // EBX stashes the sigcontext base across rep movsd (which advances
    // edi past the destination); we restore it for the IRET-frame rewrite.
    "        mov ebx, edi\n"
    "        mov esi, esp\n"
    "        add edi, 8\n"
    "        mov ecx, 8\n"
    "        cld\n"
    "        rep movsd\n"
    // edi is now at sigcontext + 40; fill the iret-frame triplet.
    "        mov eax, [esp + 32]\n"                 // iret EIP -> saved_eip
    "        mov [edi + 0], eax\n"
    "        mov eax, [esp + 40]\n"                 // iret EFLAGS
    "        mov [edi + 4], eax\n"
    "        mov eax, [esp + 44]\n"                 // original user ESP
    "        mov [edi + 8], eax\n"
    "        mov [esp + 32], ebp\n"                 // iret EIP <- handler (from EBP stash)
    "        mov [esp + 44], ebx\n"                 // iret ESP <- sigcontext base
    // Load current_program_state once (ECX was clobbered by rep movsd above).
    "        mov ecx, [current_program_state]\n"
    "        mov byte [ecx + PROGRAM_STATE_OFFSET_IN_SIGNAL_HANDLER], 1\n"
    // Clear the pending bit corresponding to EDX before iretd into the
    // handler.  Without this, the same signal would re-dispatch on the
    // next IRQ tail after SYS_SYS_SIGRETURN clears IN_SIGNAL_HANDLER.
    "        cmp edx, SIGINT\n"
    "        jne .signal_dispatch_user_clear_alarm\n"
    "        mov byte [ecx + PROGRAM_STATE_OFFSET_PENDING_SIGINT], 0\n"
    "        jmp .signal_dispatch_user_iret\n"
    ".signal_dispatch_user_clear_alarm:\n"
    "        mov byte [ecx + PROGRAM_STATE_OFFSET_PENDING_SIGALRM], 0\n"
    ".signal_dispatch_user_iret:\n"
    "        add esp, 32\n"                         // drop pushad slots
    "        iretd\n");

// signal_resume_after_handler — service SYS_SYS_SIGRETURN.  Restore the
// interrupted register state (saved by signal_dispatch_user into a
// sigcontext on the user stack) into the syscall-entry kernel frame, so
// the syscall epilogue's popad + iretd resumes user code at the point
// the signal preempted it.
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
// fields (signum at +0, pushad block at +4..+32, saved_eip at +36,
// saved_eflags at +40, saved_esp at +44) are reachable as [edi + N].
//
// We:
//   1. Validate saved_eip is in user-virt (PROGRAM_BASE..KERNEL_VIRT_BASE).
//      Fail → signal_dispatch_kill (corrupt sigcontext == bad program).
//   2. Validate saved_esp is in user-virt (PROGRAM_BASE..=KERNEL_VIRT_BASE,
//      since USER_STACK_TOP == KERNEL_VIRT_BASE is the legal high bound).
//   3. Rewrite iret EIP / EFLAGS / ESP from saved_eip / saved_eflags /
//      saved_esp.
//   4. Bulk-copy the 8-dword pushad block from sigcontext to the kernel-
//      stack pushad slots so the syscall epilogue's popad sees the user's
//      pre-signal regs.  rep movsd hits the layout exactly because the
//      sigcontext block is laid out in pushad's natural order.
//   5. Clear IN_SIGNAL_HANDLER in current_program_state so signals can
//      deliver again.
//   6. If PENDING_SIGINT OR PENDING_SIGALRM was set in current_program_state
//      during the handler, redeliver — SIGINT first (priority by signum) —
//      either kill (SIG_DFL), drop it (SIG_IGN), or jump straight to
//      signal_dispatch_user, which reads the just-rewritten [esp + 44]
//      and builds a fresh sigcontext on the now-restored user stack.
//   7. popad + iretd to user code at saved_eip.
//
// The function never returns to its caller.  .sys_sigreturn jumps here
// rather than calling — we own the popad and iretd.
asm("signal_resume_after_handler:\n"
    "        mov edi, [esp + 44]\n"                 // user ESP = sigcontext_base + 4
    "        mov eax, [edi + 36]\n"                 // saved_eip
    "        cmp eax, PROGRAM_BASE\n"
    "        jb  .signal_resume_corrupt\n"
    "        cmp eax, KERNEL_VIRT_BASE\n"
    "        jae .signal_resume_corrupt\n"
    "        mov eax, [edi + 44]\n"                 // saved_esp
    "        cmp eax, PROGRAM_BASE\n"
    "        jb  .signal_resume_corrupt\n"
    "        cmp eax, KERNEL_VIRT_BASE\n"
    "        ja  .signal_resume_corrupt\n"
    "        mov eax, [edi + 36]\n"                 // saved_eip
    "        mov [esp + 32], eax\n"                 // iret EIP
    "        mov eax, [edi + 40]\n"                 // saved_eflags
    // Sanitize before reloading into the iret frame: the user controls
    // every bit of saved_eflags via the on-stack sigcontext, so without
    // masking a handler could return with IOPL=3 (ring-3 in/out) or
    // VM=1 (Virtual-8086 entry) — privilege escalation.  Keep only
    // CF/PF/AF/ZF/SF/TF/DF/OF; force IF=1 so user code stays
    // interruptible.
    "        and eax, USER_EFLAGS_MASK\n"
    "        or  eax, EFLAGS_IF_BIT\n"
    "        mov [esp + 40], eax\n"                 // iret EFLAGS
    "        mov eax, [edi + 44]\n"                 // saved_esp
    "        mov [esp + 44], eax\n"                 // iret ESP
    // Bulk-copy 8 pushad slots from sigcontext+4 (saved_edi) to kernel
    // stack pushad area at esp+0..28.  Order matches pushad: EDI, ESI,
    // EBP, ESP_pushad, EBX, EDX, ECX, EAX top-to-bottom in both layouts.
    // edi is clobbered by rep movsd (advanced past the source) but we're
    // done reading the sigcontext after this — the redelivery branch
    // re-reads [esp + 44] (saved_esp) which we just wrote.
    "        lea esi, [edi + 4]\n"
    "        mov edi, esp\n"
    "        mov ecx, 8\n"
    "        cld\n"
    "        rep movsd\n"
    // Load current_program_state once (ECX was clobbered by rep movsd above).
    "        mov ecx, [current_program_state]\n"
    "        mov byte [ecx + PROGRAM_STATE_OFFSET_IN_SIGNAL_HANDLER], 0\n"
    // Redelivery: a signal fired while we were in the handler.  Walk
    // SIGINT first (priority by signum), then SIGALRM.
    "        cmp byte [ecx + PROGRAM_STATE_OFFSET_PENDING_SIGINT], 0\n"
    "        je  .signal_resume_check_alarm\n"
    "        mov eax, [ecx + PROGRAM_STATE_OFFSET_SIGINT_HANDLER]\n"
    "        mov edx, SIGINT\n"
    "        jmp .signal_resume_dispatch\n"
    ".signal_resume_check_alarm:\n"
    "        cmp byte [ecx + PROGRAM_STATE_OFFSET_PENDING_SIGALRM], 0\n"
    "        je  .signal_resume_no_pending\n"
    "        mov eax, [ecx + PROGRAM_STATE_OFFSET_SIGALRM_HANDLER]\n"
    "        mov edx, SIGALRM\n"
    ".signal_resume_dispatch:\n"
    "        cmp eax, SIG_DFL\n"
    "        je  signal_dispatch_kill\n"
    "        cmp eax, SIG_IGN\n"
    "        jne signal_dispatch_user\n"
    "        cmp edx, SIGINT\n"
    "        jne .signal_resume_clear_alarm\n"
    "        mov byte [ecx + PROGRAM_STATE_OFFSET_PENDING_SIGINT], 0\n"
    "        jmp .signal_resume_no_pending\n"
    ".signal_resume_clear_alarm:\n"
    "        mov byte [ecx + PROGRAM_STATE_OFFSET_PENDING_SIGALRM], 0\n"
    ".signal_resume_no_pending:\n"
    "        popad\n"
    "        iretd\n"
    // EDX = 0 picks the "^?\n" banner — distinguishes a corrupt-
    // sigcontext kill from a real signal kill.
    ".signal_resume_corrupt:\n"
    "        xor edx, edx\n"
    "        jmp signal_dispatch_kill\n");
