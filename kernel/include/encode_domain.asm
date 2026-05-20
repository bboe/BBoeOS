encode_domain:
        ;; Encode null-terminated domain string into DNS QNAME wire format
        ;; Input: ESI = domain string, EDI = output buffer
        ;; Output: EDI advanced past encoded name, CF set on error
        ;; Clobbers: EAX, EBX, ECX
        .label_start:
        mov ebx, edi           ; EBX = position of length byte (fill in later)
        inc edi                ; Skip length byte
        xor ecx, ecx           ; ECX = character count for this label
        .char_loop:
        lodsb
        cmp al, '.'
        je .dot
        test al, al
        jz .end
        stosb
        inc ecx
        jmp .char_loop
        .dot:
        test ecx, ecx
        jz .error              ; Empty label (leading or consecutive dots)
        mov [ebx], cl          ; Fill in length byte
        jmp .label_start
        .end:
        test ecx, ecx
        jz .error              ; Empty input or trailing dot
        mov [ebx], cl          ; Fill in length byte
        xor al, al
        stosb                  ; Null terminator
        clc
        ret
        .error:
        stc
        ret
