;; irq_tail.inc — Signal dispatch tail macro for IRQ handlers.
;;
;; Include this file before any IRQ handler that needs SIGNAL_TAIL_CHECK.
;; kernel.asm %includes this early (before driver kasm files) so the macro
;; is defined when ps2_irq1_handler and fdc_irq6_handler expand it.
;; entry.asm also %includes it so pmode_irq0/5/6 handlers can use the macro
;; (the definition here replaces the one that was inlined in entry.asm).
;;
;; Stack contract at SIGNAL_TAIL_CHECK invocation (Task 11 reshape):
;;   pushad slots are STILL on the stack (the macro owns popad — see below).
;;   [esp +  0..28]  pushad slots EDI, ESI, EBP, ESP_pushad, EBX, EDX, ECX, EAX
;;   [esp + 32]      iret EIP
;;   [esp + 36]      iret CS    ← checked against USER_CODE_SELECTOR
;;   [esp + 40]      iret EFLAGS
;;   [esp + 44]      iret ESP   (only present on cross-priv iret — i.e. when
;;                                CS at +36 is USER_CODE_SELECTOR)
;;   [esp + 48]      iret SS
;;
;; The macro takes ownership of popad: in the no-dispatch path it pops the
;; pushad slots before iretd; in the dispatch path it routes to a kernel
;; routine that either captures the pushad slots into a sigcontext (user
;; handler — `signal_dispatch_user`) or doesn't need them (SIG_DFL kill —
;; `signal_dispatch_kill`).  Each caller must therefore drop its own popad
;; line and end with `SIGNAL_TAIL_CHECK / iretd`.
;;
;; The symbols pending_sigint, pending_sigalrm, in_signal_handler,
;; sigint_handler, sigalrm_handler, signal_dispatch_kill, and
;; signal_dispatch_user are defined elsewhere (entry.asm / signal.c) and
;; resolved as forward references (NASM -f bin resolves forward label refs
;; across the flat binary).

%ifndef IRQ_TAIL_INC
%define IRQ_TAIL_INC

;; Signal dispatch tail — invoke from IRQ / syscall handler before its
;; iretd.  The macro takes ownership of popad: in the no-dispatch path
;; it pops; in the dispatch path it routes to a kernel routine that
;; captures the pushad slots into a sigcontext (user handler) or
;; doesn't need them (SIG_DFL kill).  Skips dispatch when:
;;   - interrupted CS is not user code (we'd kill kernel context),
;;   - both pending_sigint and pending_sigalrm are clear (nothing to do),
;;   - in_signal_handler is set (already inside a handler — block re-entry
;;     until SYS_SYS_SIGRETURN clears the flag).
;; Checks SIGINT first (lower signum = higher priority), then SIGALRM.
;; On dispatch:
;;   SIG_DFL  → signal_dispatch_kill (never returns; pushad slots discarded)
;;   SIG_IGN  → clear the pending bit for the dispatched signal, fall through
;;              to popad + iretd
;;   user-virt → signal_dispatch_user (never returns to here; captures
;;               pushad slots into sigcontext, rewrites iret frame, iretds
;;               at the handler).
%macro SIGNAL_TAIL_CHECK 0
        cmp word [esp + 36], USER_CODE_SELECTOR
        jne %%no_dispatch
        cmp byte [in_signal_handler], 0
        jne %%no_dispatch
        ;; Check SIGINT first (lower signum = higher priority).
        cmp byte [pending_sigint], 0
        je  %%check_sigalrm
        mov edx, SIGINT
        mov eax, [sigint_handler]
        jmp %%dispatch
%%check_sigalrm:
        cmp byte [pending_sigalrm], 0
        je  %%no_dispatch
        mov edx, SIGALRM
        mov eax, [sigalrm_handler]
%%dispatch:
        cmp eax, SIG_DFL
        je  signal_dispatch_kill           ; never returns; pushad slots discarded
        cmp eax, SIG_IGN
        jne signal_dispatch_user           ; never returns to here; rewrites IRET + iretd
        ;; SIG_IGN: clear the pending bit corresponding to EDX, fall through.
        cmp edx, SIGINT
        jne %%clear_alarm
        mov byte [pending_sigint], 0
        jmp %%no_dispatch
%%clear_alarm:
        mov byte [pending_sigalrm], 0
%%no_dispatch:
        popad
%endmacro

%endif ; IRQ_TAIL_INC
