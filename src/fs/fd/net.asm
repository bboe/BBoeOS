fd_read_net:
        ;; Poll NIC for one frame; copy min(pkt_len, CX) bytes to [DI].
        ;; Returns AX = bytes copied (0 = no packet ready), CF clear.
        push bx
        push cx
        push dx
        push si
        push di
        mov bx, di              ; BX = user destination
        mov dx, cx              ; DX = user buffer size
        call ne2k_receive       ; CF set if no packet; else CX = pkt len
        jc .rnet_empty
        cmp cx, dx
        jbe .rnet_len_ok
        mov cx, dx
        .rnet_len_ok:
        mov si, NET_RECEIVE_BUFFER
        mov di, bx
        mov ax, cx              ; save byte count for return
        cld
        rep movsb
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret
        .rnet_empty:
        xor ax, ax
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret

fd_write_net:
        ;; Send a raw Ethernet frame from the user buffer.
        push bx
        push cx
        push dx
        push si
        mov si, [fd_write_buffer]
        mov ax, cx              ; save for return
        call ne2k_send
        jc .wnet_err
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret
        .wnet_err:
        pop si
        pop dx
        pop cx
        pop bx
        mov ax, -1
        stc
        ret
