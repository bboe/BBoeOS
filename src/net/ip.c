asm("
ip_checksum:
        ;; Compute ones-complement checksum over a buffer
        ;; Input: ESI = data pointer, ECX = length in bytes (must be even)
        ;; Output: AX = checksum (complemented, ready to store)
        push ebx
        push ecx
        push esi

        xor bx, bx
        shr ecx, 1                      ; Word count
        .cksum_loop:
        lodsw
        add bx, ax
        adc bx, 0                       ; Fold carry
        loop .cksum_loop

        not bx
        mov ax, bx

        pop esi
        pop ecx
        pop ebx
        ret

ip_send:
        ;; Send an IP packet wrapped in an Ethernet frame.
        ;; Input: EBX = pointer to 4-byte dest IP
        ;;        AL = IP protocol number
        ;;        ESI = pointer to payload data
        ;;        ECX = payload length in bytes
        ;; Output: CF set on error (ARP timeout or send failure)
        push eax
        push ebx
        push ecx
        push edx
        push esi
        push edi

        ;; Save inputs
        mov [.is_proto], al
        mov [.is_plen], cx
        mov [.is_payload], esi
        mov [.is_destip], ebx

        ;; 1. Resolve destination MAC via ARP (may use NET_TRANSMIT_BUFFER).
        ;;    If dest is not on local subnet (10.0.2.0/24), use gateway.
        mov esi, ebx
        mov eax, [esi]
        and eax, 0FFFFFFh               ; Mask to first 3 bytes (subnet /24)
        mov edx, [our_ip]
        and edx, 0FFFFFFh
        cmp eax, edx
        je .ip_send_local
        mov esi, gateway_ip             ; Non-local: route via gateway
        .ip_send_local:
        call arp_resolve
        jc .ip_send_done

        ;; 2. Build Ethernet header at NET_TRANSMIT_BUFFER
        mov esi, edi                    ; ESI = resolved dest MAC
        mov edi, NET_TRANSMIT_BUFFER
        cld
        movsw                           ; Dest MAC
        movsw
        movsw
        mov esi, mac_address            ; Src MAC
        movsw
        movsw
        movsw
        mov ax, 0008h                   ; EtherType: IPv4 (0x0800 big-endian)
        stosw

        ;; 3. Build IP header at NET_TRANSMIT_BUFFER + 14 (EDI is already there)
        mov al, 45h                     ; Version 4, IHL 5 (20 bytes)
        stosb
        xor al, al                      ; DSCP/ECN = 0
        stosb
        mov ax, [.is_plen]              ; Total length = 20 + payload
        add ax, 20
        xchg al, ah                     ; Big-endian
        stosw
        mov ax, [ip_id]                 ; Identification
        xchg al, ah
        stosw
        inc word [ip_id]
        mov al, 40h                     ; Flags: Don't Fragment
        stosb
        xor al, al                      ; Fragment offset: 0
        stosb
        mov al, 64                      ; TTL
        stosb
        mov al, [.is_proto]             ; Protocol
        stosb
        xor ax, ax                      ; Header checksum (placeholder)
        stosw
        push esi
        mov esi, our_ip                 ; Source IP
        movsd
        mov esi, [.is_destip]           ; Destination IP
        movsd
        pop esi

        ;; 4. Copy payload to NET_TRANSMIT_BUFFER + 34 (EDI is already there)
        mov esi, [.is_payload]
        movzx ecx, word [.is_plen]
        rep movsb

        ;; 5. Compute and store IP header checksum
        mov esi, NET_TRANSMIT_BUFFER + 14
        mov ecx, 20
        call ip_checksum
        mov [NET_TRANSMIT_BUFFER + 24], ax    ; Offset 14 + 10

        ;; 6. Send the frame
        mov esi, NET_TRANSMIT_BUFFER
        movzx ecx, word [.is_plen]
        add ecx, 34                     ; 14 (Eth) + 20 (IP) + payload
        call ne2k_send

        .ip_send_done:
        pop edi
        pop esi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

        .is_destip  dd 0
        .is_payload dd 0
        .is_plen    dw 0
        .is_proto   db 0

        ;; Variables
        gateway_ip db 10, 0, 2, 2
        ip_id      dw 1
        our_ip     db 10, 0, 2, 15
");
