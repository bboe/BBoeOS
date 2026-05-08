%ifndef IRQ_TAIL_INC
%define IRQ_TAIL_INC

;; SIGINT dispatch tail — invoke from IRQ / syscall handler before iretd
;; (after popad).  Reads the current program's sigint_handler slot:
;;   SIG_DFL → signal_dispatch_kill (never returns)
;;   SIG_IGN → clear pending_sigint, fall through to iretd
;;   user-virt → not yet supported in this PR; treated as SIG_DFL
;;               (PR 3 adds user-handler delivery).
%macro SIGINT_TAIL_CHECK 0
        cmp word [esp + 4], USER_CODE_SELECTOR
        jne %%no_dispatch
        cmp byte [pending_sigint], 0
        je  %%no_dispatch
        mov eax, [sigint_handler]
        cmp eax, SIG_DFL
        je  signal_dispatch_kill
        cmp eax, SIG_IGN
        jne signal_dispatch_kill        ; user-virt handler: treat as SIG_DFL until PR 3
        mov byte [pending_sigint], 0
%%no_dispatch:
%endmacro

%endif
