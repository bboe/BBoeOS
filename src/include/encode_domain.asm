encode_domain:
        ;; Encode null-terminated domain string into DNS QNAME wire format
        ;; Input: SI = domain string, DI = output buffer
        ;; Output: DI advanced past encoded name, CF set on error
        ;; Clobbers: AX, BX, CX
        .label_start:
        mov bx, di             ; BX = position of length byte (fill in later)
        inc di                 ; Skip length byte
        xor cx, cx             ; CX = character count for this label
        .char_loop:
        lodsb
        cmp al, '.'
        je .dot
        test al, al
        jz .end
        stosb
        inc cx
        jmp .char_loop
        .dot:
        test cx, cx
        jz .error              ; Empty label (leading or consecutive dots)
        mov [bx], cl           ; Fill in length byte
        jmp .label_start
        .end:
        test cx, cx
        jz .error              ; Empty input or trailing dot
        mov [bx], cl           ; Fill in length byte
        xor al, al
        stosb                  ; Null terminator
        clc
        ret
        .error:
        stc
        ret
