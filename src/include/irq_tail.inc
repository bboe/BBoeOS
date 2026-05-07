;; irq_tail.inc — SIGINT dispatch tail macro for IRQ handlers.
;;
;; Include this file before any IRQ handler that needs SIGINT_TAIL_CHECK.
;; kernel.asm %includes this early (before driver kasm files) so the macro
;; is defined when ps2_irq1_handler and fdc_irq6_handler expand it.
;; entry.asm also %includes it so pmode_irq0/5/6 handlers can use the macro
;; (the definition here replaces the one that was inlined in entry.asm).
;;
;; Stack contract at SIGINT_TAIL_CHECK invocation:
;;   All general-purpose registers restored (popad or equivalent).
;;   [esp + 0] = iret EIP
;;   [esp + 4] = iret CS   ← checked against USER_CODE_SELECTOR
;;   [esp + 8] = iret EFLAGS
;;
;; The symbols pending_sigint, in_sigint_handler, sigint_handler, and
;; signal_dispatch_kill are defined in entry.asm and resolved as forward
;; references (NASM -f bin resolves forward label refs across the flat binary).

%ifndef IRQ_TAIL_INC
%define IRQ_TAIL_INC

;; SIGINT dispatch tail — invoke from IRQ / syscall handler before iretd.
;; Stack at invocation: popad has already executed, so [esp] is the iret
;; EIP and [esp + 4] is the iret CS.  Skips dispatch when:
;;   - interrupted CS is not user code (we'd kill kernel context),
;;   - pending_sigint is clear (nothing to do),
;;   - in_sigint_handler is set (already inside a handler — block re-entry
;;     until SYS_SYS_SIGRETURN clears the flag).
;; On dispatch:
;;   SIG_DFL  → signal_dispatch_kill (never returns)
;;   SIG_IGN  → clear pending_sigint, fall through to iretd
;;   user-virt→ Task 11 will route to signal_dispatch_user; for now
;;              treat as SIG_DFL (kill).
%macro SIGINT_TAIL_CHECK 0
        cmp word [esp + 4], USER_CODE_SELECTOR
        jne %%no_dispatch
        cmp byte [pending_sigint], 0
        je  %%no_dispatch
        cmp byte [in_sigint_handler], 0
        jne %%no_dispatch
        mov eax, [sigint_handler]
        cmp eax, SIG_DFL
        je  signal_dispatch_kill
        cmp eax, SIG_IGN
        jne signal_dispatch_kill        ; user-handler dispatch arrives in Task 11; kill for now
        mov byte [pending_sigint], 0
%%no_dispatch:
%endmacro

%endif ; IRQ_TAIL_INC
