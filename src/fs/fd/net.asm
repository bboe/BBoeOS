fd_read_net:
        ;; Poll NIC for one frame; copy min(pkt_len, CX) bytes to [EDI].
        ;; Returns AX = bytes copied (0 = no packet ready), CF clear.
        push ebx
        push ecx
        push edx
        push esi
        push edi
        mov ebx, edi            ; EBX = user destination
        mov edx, ecx            ; EDX = user buffer size
        call ne2k_receive       ; CF set if no packet; else CX = pkt len
        jc .rnet_empty
        cmp cx, dx              ; clamp pkt len to user buffer size (both ≤ 64 KB)
        jbe .rnet_len_ok
        mov cx, dx
        .rnet_len_ok:
        mov esi, NET_RECEIVE_BUFFER
        mov edi, ebx
        movzx eax, cx           ; return value = bytes copied
        movzx ecx, cx           ; zero-extend for rep movsb
        cld
        rep movsb
        pop edi
        pop esi
        pop edx
        pop ecx
        pop ebx
        clc
        ret
        .rnet_empty:
        xor eax, eax
        pop edi
        pop esi
        pop edx
        pop ecx
        pop ebx
        clc
        ret

fd_write_net:
        ;; Send a raw Ethernet frame from the user buffer.
        push ebx
        push ecx
        push edx
        push esi
        mov esi, [fd_write_buffer]
        movzx eax, cx           ; save count for return
        call ne2k_send
        jc .wnet_err
        pop esi
        pop edx
        pop ecx
        pop ebx
        clc
        ret
        .wnet_err:
        pop esi
        pop edx
        pop ecx
        pop ebx
        mov eax, -1
        stc
        ret
