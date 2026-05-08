%ifndef IRQ_TAIL_INC
%define IRQ_TAIL_INC

;; SIGINT dispatch tail — invoke from IRQ / syscall handler before iretd
;; (after popad, since this PR's variant doesn't capture register state).
;; Kills the running program if pending_sigint is set and we're returning
;; to user code.  Future PRs add SIG_IGN dispatch and user-handler
;; delivery; for now, SIGINT is unconditional kill.
%macro SIGINT_TAIL_CHECK 0
        cmp word [esp + 4], USER_CODE_SELECTOR
        jne %%no_dispatch
        cmp byte [pending_sigint], 0
        je  %%no_dispatch
        jmp signal_dispatch_kill
%%no_dispatch:
%endmacro

%endif
