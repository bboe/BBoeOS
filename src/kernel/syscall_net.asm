        .net_mac:
        ;; Copy cached MAC to caller's buffer at DI; CF set if NIC absent.
        cmp byte [net_present], 0
        je .net_mac_absent
        push si
        push cx
        cld
        mov si, mac_address
        mov cx, 3              ; 6 bytes = 3 words
        rep movsw
        pop cx
        pop si
        clc
        jmp .iret_cf
        .net_mac_absent:
        stc
        jmp .iret_cf

        .net_open:
        ;; Allocate a socket fd: AL = type (SOCK_RAW=0, SOCK_DGRAM=1),
        ;; DL = protocol (IPPROTO_UDP or IPPROTO_ICMP for SOCK_DGRAM;
        ;; 0 / ignored for SOCK_RAW — raw Ethernet sees every frame).
        ;; CF set if no NIC or table full.
        cmp byte [net_present], 0
        je .net_open_err
        mov [.net_open_type], al
        mov [.net_open_proto], dl
        call fd_alloc          ; AX = fd number, SI = entry pointer
        jc .net_open_err
        cmp byte [.net_open_type], SOCK_DGRAM
        je .net_open_dgram
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_NET
        jmp .net_open_done
        .net_open_dgram:
        cmp byte [.net_open_proto], IPPROTO_ICMP
        jne .net_open_udp
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_ICMP
        jmp .net_open_done
        .net_open_udp:
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_UDP
        .net_open_done:
        mov byte [si+FD_OFFSET_FLAGS], 0
        clc
        jmp .iret_cf
        .net_open_proto db 0
        .net_open_type db 0
        .net_open_err:
        stc
        jmp .iret_cf

        .net_recvfrom:
        ;; Receive datagram via fd.
        ;;   UDP (FD_TYPE_UDP):  BX=fd, DI=recv buf, CX=max len, DX=local_port
        ;;   ICMP (FD_TYPE_ICMP): BX=fd, DI=recv buf, CX=max len, DX ignored
        ;; Returns AX = bytes copied (0 if none), CF clear.
        mov [.rf_buf], di
        mov [.rf_max], cx
        mov [.rf_port], dx
        call fd_lookup         ; SI = entry pointer
        jc .net_recvfrom_none
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_UDP
        je .rf_udp
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_ICMP
        je .rf_icmp
        jmp .net_recvfrom_none
        .rf_udp:
        call udp_receive       ; DI = payload, CX = len, CF if none
        jc .net_recvfrom_none
        ;; Check dest port: UDP dest port is at NET_RECEIVE_BUFFER+36 (big-endian)
        mov ax, [.rf_port]
        xchg al, ah            ; Convert to big-endian for comparison
        cmp ax, [NET_RECEIVE_BUFFER+36]
        jne .net_recvfrom_none
        jmp .rf_common_copy
        .rf_icmp:
        call icmp_receive      ; DI = ICMP payload, CX = len, CF if none
        jc .net_recvfrom_none
        .rf_common_copy:
        ;; Copy min(CX payload, rf_max) bytes from DI to rf_buf
        cmp cx, [.rf_max]
        jbe .rf_copy
        mov cx, [.rf_max]
        .rf_copy:
        mov ax, cx             ; AX = bytes to copy (return value)
        mov si, di             ; SI = source payload pointer
        mov di, [.rf_buf]      ; DI = destination (caller's buffer)
        cld
        rep movsb
        clc
        jmp .iret_cf
        .net_recvfrom_none:
        xor ax, ax
        clc
        jmp .iret_cf
        .rf_buf dw 0
        .rf_max dw 0
        .rf_port dw 0

        .net_sendto:
        ;; Send datagram via fd.
        ;;   UDP (FD_TYPE_UDP):   BX=fd, SI=payload, CX=len,
        ;;                         DI=ip_ptr, DX=src_port, BP=dst_port
        ;;   ICMP (FD_TYPE_ICMP): BX=fd, SI=icmp_bytes, CX=len,
        ;;                         DI=ip_ptr; DX/BP ignored
        ;; Returns AX = bytes sent, CF on error.
        mov [.st_buf], si
        mov [.st_len], cx
        mov [.st_ip], di
        mov [.st_sport], dx
        ;; BP holds our pusha frame pointer; the user's BP (dst_port)
        ;; lives at [bp+4] in the saved area.
        mov ax, [bp+4]
        mov [.st_dport], ax
        call fd_lookup         ; SI = entry pointer
        jc .net_sendto_err
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_UDP
        je .st_udp
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_ICMP
        je .st_icmp
        jmp .net_sendto_err
        .st_udp:
        mov bx, [.st_ip]      ; BX = dest IP pointer
        mov di, [.st_sport]   ; DI = source port
        mov dx, [.st_dport]   ; DX = dest port
        mov si, [.st_buf]     ; SI = payload buffer
        mov cx, [.st_len]     ; CX = payload length
        call udp_send
        jc .net_sendto_err
        mov ax, [.st_len]     ; AX = bytes sent
        jmp .iret_cf
        .st_icmp:
        mov bx, [.st_ip]      ; BX = dest IP pointer
        mov al, 1              ; AL = protocol = ICMP
        mov si, [.st_buf]     ; SI = ICMP bytes (header + data)
        mov cx, [.st_len]     ; CX = length
        call ip_send
        jc .net_sendto_err
        mov ax, [.st_len]
        jmp .iret_cf
        .net_sendto_err:
        stc
        jmp .iret_cf
        .st_buf dw 0
        .st_len dw 0
        .st_ip dw 0
        .st_sport dw 0
        .st_dport dw 0
