;;; ------------------------------------------------------------------------
;;; access.asm — kernel/user pointer boundary checks.
;;;
;;; Same KERNEL_VIRT_BASE boundary that idt.asm's exc_common pivots on for
;;; the user-fault kill path: pointers + lengths must lie wholly below
;;; 0xC0000000 and not 32-bit wrap.  access_ok runs at the syscall edge so
;;; the kernel never even attempts to dereference an out-of-range user
;;; pointer; the kill path stays as the residual catcher for pointers that
;;; pass access_ok but land on an unmapped user page.
;;;
;;; Lives next to address_space.asm and frame.asm because it's a memory-
;;; boundary policy, not a syscall-dispatch concern.  Globals (no leading
;;; dot on the entry points) so future consumers — other exception
;;; handlers, eventual copy_{to,from}_user — can call them directly.
;;; ------------------------------------------------------------------------

access_ok:
        ;; Input:  EBX = user-virt pointer, ECX = byte length.
        ;; Output: CF=0 if (EBX + ECX) <= KERNEL_VIRT_BASE with no 32-bit
        ;;         wrap; CF=1 otherwise.  Zero-length spans are accepted
        ;;         when EBX itself is in user range.
        ;; Preserves all caller registers including EAX.
        push eax
        mov eax, ebx
        add eax, ecx
        jc .bad                                 ; EBX + ECX wrapped past 4 GB
        cmp eax, KERNEL_VIRT_BASE
        ja .bad                                 ; ends inside or past kernel half
        pop eax
        clc
        ret
        .bad:
        pop eax
        stc
        ret

access_ok_string:
        ;; Input:  ESI = user-virt string pointer, ECX = max bytes to scan
        ;;         (must be >= 1; pass MAX_PATH for filename arguments).
        ;; Output: CF=0 if a NUL is found within ECX bytes and every
        ;;         scanned byte address is < KERNEL_VIRT_BASE.
        ;;         CF=1 if no NUL is found in range, or any scanned
        ;;         address would reach into the kernel half.
        ;; Preserves all caller registers including EAX.
        ;;
        ;; Walks one byte at a time so a string ending close to the
        ;; KERNEL_VIRT_BASE boundary is still accepted, instead of being
        ;; rejected by an up-front (ESI + max) range check.  An unmapped
        ;; user page reached during the scan still rides the exc_common
        ;; kill path — that's the intended backstop, not a case
        ;; access_ok_string is trying to catch.
        push eax
        push ecx
        push esi
        test ecx, ecx
        jz .bad
        .loop:
        cmp esi, KERNEL_VIRT_BASE
        jae .bad
        mov al, [esi]
        test al, al
        jz .ok
        inc esi
        loop .loop
        ;; ECX exhausted without seeing a NUL — caller's string is either
        ;; missing the terminator or far longer than MAX_PATH.
        .bad:
        pop esi
        pop ecx
        pop eax
        stc
        ret
        .ok:
        pop esi
        pop ecx
        pop eax
        clc
        ret
