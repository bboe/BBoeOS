udp_receive:
        ;; Receive a UDP datagram (polls once, handles ARP transparently).
        ;; Output: EDI = full 32-bit pointer to payload inside net_receive_buffer
        ;;         ECX = payload length
        ;;         BX = source port
        ;;         ESI = full 32-bit pointer to 4-byte source IP inside net_receive_buffer
        ;;         CF set if no UDP packet available
        push eax

        call ne2k_receive
        jc .ur_none

        ;; Handle ARP packets transparently
        push esi
        mov esi, edi
        call arp_handle_packet
        pop esi

        ;; Check EtherType = IPv4
        cmp byte [edi+12], 08h
        jne .ur_none
        cmp byte [edi+13], 00h
        jne .ur_none

        ;; Check IP protocol = UDP (17)
        cmp byte [edi+23], 17
        jne .ur_none

        ;; Source IP at Ethernet(14) + IP src offset(12) = offset 26
        lea esi, [edi+26]

        ;; Source port at Ethernet(14) + IP(20) + UDP src(0) = offset 34
        mov bx, [edi+34]
        xchg bl, bh             ; Network to host byte order

        ;; UDP payload length = UDP length field - 8
        mov cx, [edi+38]
        xchg cl, ch
        sub cx, 8
        movzx ecx, cx

        ;; Payload starts at offset 42 (14 Eth + 20 IP + 8 UDP)
        add edi, 42

        pop eax
        clc
        ret

        .ur_none:
        pop eax
        stc
        ret

udp_send:
        ;; Send a UDP datagram via IP.
        ;; Input: EBX = pointer to 4-byte dest IP
        ;;        EDI = source port (low 16), EDX = dest port (low 16)
        ;;        ESI = pointer to payload data, ECX = payload length
        ;; Output: CF set on error
        push eax
        push ebx
        push ecx
        push edx
        push esi
        push edi

        ;; Save inputs
        mov [.ud_destip], ebx
        mov [.ud_plen], cx
        mov [.ud_sport], di
        mov [.ud_dport], dx

        ;; Build UDP header + payload in udp_buffer
        mov edi, udp_buffer
        cld

        mov ax, [.ud_sport]
        xchg al, ah                     ; Big-endian
        stosw
        mov ax, [.ud_dport]
        xchg al, ah                     ; Big-endian
        stosw
        mov ax, [.ud_plen]              ; UDP length = 8 + payload
        add ax, 8
        xchg al, ah
        stosw
        xor ax, ax                      ; Checksum = 0 (optional in IPv4)
        stosw

        ;; Copy payload after UDP header
        movzx ecx, word [.ud_plen]
        rep movsb

        ;; Send via ip_send: protocol 17 (UDP)
        mov ebx, [.ud_destip]
        mov al, 17
        mov esi, udp_buffer
        movzx ecx, word [.ud_plen]
        add ecx, 8                      ; UDP header + payload
        call ip_send

        pop edi
        pop esi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

        .ud_destip dd 0
        .ud_plen   dw 0
        .ud_sport  dw 0
        .ud_dport  dw 0

        ;; Variables
        udp_buffer times 256 db 0
