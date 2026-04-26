        ;; ------------------------------------------------------------
        ;; Network syscalls.
        ;;
        ;; net_recvfrom and net_sendto stage user buffers through
        ;; SECTOR_BUFFER before calling the (still-16-bit-pointer)
        ;; udp / icmp / ip stack.  The stack assumes its inputs live
        ;; in low memory (NET_TRANSMIT_BUFFER, kernel statics, etc.)
        ;; and stores them in 16-bit slots — passing a 32-bit user
        ;; stack pointer directly truncates the high half.  Staging
        ;; sidesteps that until the net protocol files get their own
        ;; pmode port.
        ;; ------------------------------------------------------------

        .net_mac:
        ;; DI = caller's 6-byte buffer.  CF set if no NIC.
        cmp byte [net_present], 0
        je .net_mac_absent
        push esi
        push ecx
        cld
        mov esi, mac_address
        mov ecx, 3                              ; 6 bytes = 3 words
        rep movsw
        pop ecx
        pop esi
        clc
        jmp .iret_cf
        .net_mac_absent:
        stc
        jmp .iret_cf

        .net_open:
        ;; AL = type (SOCK_RAW / SOCK_DGRAM), DL = protocol.
        cmp byte [net_present], 0
        je .net_open_err
        mov [.net_open_type], al
        mov [.net_open_proto], dl
        call fd_alloc                           ; AX = fd, ESI = entry pointer
        jc .net_open_err
        cmp byte [.net_open_type], SOCK_DGRAM
        je .net_open_dgram
        mov byte [esi+FD_OFFSET_TYPE], FD_TYPE_NET
        jmp .net_open_done
        .net_open_dgram:
        cmp byte [.net_open_proto], IPPROTO_ICMP
        jne .net_open_udp
        mov byte [esi+FD_OFFSET_TYPE], FD_TYPE_ICMP
        jmp .net_open_done
        .net_open_udp:
        mov byte [esi+FD_OFFSET_TYPE], FD_TYPE_UDP
        .net_open_done:
        mov byte [esi+FD_OFFSET_FLAGS], 0
        clc
        jmp .iret_cf
        .net_open_err:
        stc
        jmp .iret_cf
        .net_open_proto db 0
        .net_open_type db 0

        .net_recvfrom:
        ;; Receive datagram via fd.
        ;;   UDP (FD_TYPE_UDP):  BX=fd, EDI=recv buf, ECX=max len, DX=local_port
        ;;   ICMP (FD_TYPE_ICMP): same shape; DX ignored
        mov [.rf_buf], edi
        mov [.rf_max], ecx
        mov [.rf_port], dx
        call fd_lookup                          ; ESI = entry pointer
        jc .rf_none
        cmp byte [esi+FD_OFFSET_TYPE], FD_TYPE_UDP
        je .rf_udp
        cmp byte [esi+FD_OFFSET_TYPE], FD_TYPE_ICMP
        je .rf_icmp
        jmp .rf_none
        .rf_udp:
        call udp_receive                        ; DI=payload (low 16), CX=len, CF if none
        jc .rf_none
        ;; Check dest port: UDP dest port is at NET_RECEIVE_BUFFER+36 (big-endian).
        mov ax, [.rf_port]
        xchg al, ah                             ; user port → big-endian
        cmp ax, [NET_RECEIVE_BUFFER+36]
        jne .rf_none
        jmp .rf_copy
        .rf_icmp:
        call icmp_receive                       ; DI=ICMP payload, CX=len, CF if none
        jc .rf_none
        .rf_copy:
        ;; CX = payload length, DI = payload offset (in NET_RECEIVE_BUFFER, fits 16 bits).
        ;; Copy min(CX, rf_max) bytes to user's buffer at [.rf_buf].
        movzx ecx, cx
        cmp ecx, [.rf_max]
        jbe .rf_have_count
        mov ecx, [.rf_max]
        .rf_have_count:
        mov eax, ecx                            ; return value = bytes copied
        movzx esi, di                           ; flat 32-bit source
        mov edi, [.rf_buf]                      ; flat 32-bit destination
        cld
        rep movsb
        clc
        jmp .iret_cf
        .rf_none:
        xor eax, eax                            ; AX = 0 = no bytes
        clc
        jmp .iret_cf
        .rf_buf dd 0
        .rf_max dd 0
        .rf_port dw 0

        .net_sendto:
        ;; Send datagram via fd.
        ;;   UDP (FD_TYPE_UDP):   BX=fd, ESI=payload, ECX=len,
        ;;                         EDI=ip_ptr, DX=src_port,
        ;;                         user EBP (saved at [esp+8]) = dst_port
        ;;   ICMP (FD_TYPE_ICMP): same shape; DX/dst_port ignored
        ;;
        ;; Stage user IP and payload into SECTOR_BUFFER (4-byte IP +
        ;; payload starting at +4) so the net stack sees kernel-
        ;; resident addresses.  Caps payload at SECTOR_BUFFER - 4 = 508
        ;; bytes; larger UDP datagrams land when the staging buffer
        ;; widens.
        mov [.st_fd], bx
        mov [.st_len], cx
        mov [.st_sport], dx
        mov eax, [esp+8]                        ; saved user EBP = dst port
        mov [.st_dport], ax

        ;; Stage 4-byte dest IP at SECTOR_BUFFER+0.
        push esi                                ; save user payload ptr
        mov esi, edi                            ; user EDI = IP source
        mov edi, SECTOR_BUFFER
        cld
        movsd
        pop esi                                 ; restore payload ptr

        ;; Stage payload at SECTOR_BUFFER+4.
        movzx ecx, word [.st_len]
        rep movsb

        mov bx, [.st_fd]
        call fd_lookup                          ; ESI = fd entry pointer
        jc .st_err
        cmp byte [esi+FD_OFFSET_TYPE], FD_TYPE_UDP
        je .st_udp
        cmp byte [esi+FD_OFFSET_TYPE], FD_TYPE_ICMP
        je .st_icmp
        jmp .st_err

        .st_udp:
        mov bx, SECTOR_BUFFER                   ; staged dest IP
        mov di, [.st_sport]
        mov dx, [.st_dport]
        mov si, SECTOR_BUFFER + 4               ; staged payload
        mov cx, [.st_len]
        call udp_send
        jc .st_err
        movzx eax, word [.st_len]               ; bytes sent
        clc
        jmp .iret_cf

        .st_icmp:
        mov bx, SECTOR_BUFFER
        mov al, IPPROTO_ICMP
        mov si, SECTOR_BUFFER + 4
        mov cx, [.st_len]
        call ip_send
        jc .st_err
        movzx eax, word [.st_len]
        clc
        jmp .iret_cf

        .st_err:
        stc
        jmp .iret_cf

        .st_fd    dw 0
        .st_len   dw 0
        .st_sport dw 0
        .st_dport dw 0
