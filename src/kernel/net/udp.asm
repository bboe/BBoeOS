udp_receive:
        ;; Receive a UDP datagram (polls once, handles ARP transparently)
        ;; Output: DI = payload pointer (within NET_RECEIVE_BUFFER)
        ;;         CX = payload length
        ;;         BX = source port
        ;;         SI = pointer to 4-byte source IP (within NET_RECEIVE_BUFFER)
        ;;         CF set if no UDP packet available
        push ax

        call ne2k_receive
        jc .ur_none

        ;; Handle ARP packets transparently
        push si
        mov si, di
        call arp_handle_packet
        pop si

        ;; Check EtherType = IPv4
        cmp byte [di+12], 08h
        jne .ur_none
        cmp byte [di+13], 00h
        jne .ur_none

        ;; Check IP protocol = UDP (17)
        cmp byte [di+23], 17
        jne .ur_none

        ;; Extract fields from the received frame
        ;; Source IP at Ethernet(14) + IP src offset(12) = offset 26
        lea si, [di+26]

        ;; Source port at Ethernet(14) + IP(20) + UDP src(0) = offset 34
        mov bx, [di+34]
        xchg bl, bh            ; Network to host byte order

        ;; UDP payload length = UDP length field - 8
        mov cx, [di+38]
        xchg cl, ch
        sub cx, 8

        ;; Payload starts at offset 42 (14 Eth + 20 IP + 8 UDP)
        add di, 42

        pop ax
        clc
        ret

        .ur_none:
        pop ax
        stc
        ret

udp_send:
        ;; Send a UDP datagram via IP
        ;; Input: BX = pointer to 4-byte dest IP
        ;;        DI = source port, DX = dest port
        ;;        SI = pointer to payload data, CX = payload length
        ;; Output: CF set on error
        mov [.ud_srcport], di
        push ax
        push bx
        push cx
        push dx
        push si
        push di

        ;; Save inputs
        mov [.ud_destip], bx
        mov [.ud_plen], cx

        ;; Build UDP header + payload in udp_buffer
        mov di, udp_buffer
        cld

        mov ax, [.ud_srcport]
        xchg al, ah            ; Big-endian
        stosw
        xchg dl, dh            ; Dest port big-endian
        mov ax, dx
        stosw
        mov ax, [.ud_plen]     ; UDP length = 8 + payload
        add ax, 8
        xchg al, ah
        stosw
        xor ax, ax             ; Checksum = 0 (optional in IPv4)
        stosw

        ;; Copy payload after UDP header
        mov cx, [.ud_plen]
        rep movsb

        ;; Send via ip_send: protocol 17 (UDP)
        mov bx, [.ud_destip]
        mov al, 17
        mov si, udp_buffer
        mov cx, [.ud_plen]
        add cx, 8              ; UDP header + payload
        call ip_send

        pop di
        pop si
        pop dx
        pop cx
        pop bx
        pop ax
        ret

        .ud_destip dw 0
        .ud_plen dw 0
        .ud_srcport dw 0

        ;; Variables
        udp_buffer times 256 db 0
