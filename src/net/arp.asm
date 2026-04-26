        %assign ARP_ENTRY_SIZE 12  ; 4 bytes IP + 6 bytes MAC + 2 bytes timestamp
        %assign ARP_TABLE_SIZE 8
        %assign ARP_TIMESTAMP_OFFSET 10
        %assign ARP_TTL_SECONDS 60

arp_handle_packet:
        ;; Process a received Ethernet frame for ARP.
        ;; Input: ESI = pointer to received frame (within NET_RECEIVE_BUFFER)
        ;; Updates ARP table on reply, sends reply to requests for our IP.
        push eax
        push ecx
        push esi
        push edi

        ;; Check EtherType at offset 12 = 0x0806 (ARP)
        cmp byte [esi+12], 08h
        jne .arp_done
        cmp byte [esi+13], 06h
        jne .arp_done

        ;; Check opcode at offset 20-21
        cmp byte [esi+20], 0
        jne .arp_done
        mov al, [esi+21]
        cmp al, 2                       ; ARP reply
        je .arp_reply
        cmp al, 1                       ; ARP request
        je .arp_request_in
        jmp .arp_done

        .arp_reply:
        ;; Add sender to ARP table (sender MAC at +22, sender IP at +28)
        push esi
        lea edi, [esi+22]               ; EDI = sender MAC
        add esi, 28                     ; ESI = sender IP
        call arp_table_add
        pop esi
        jmp .arp_done

        .arp_request_in:
        ;; Check if target IP (offset +38) matches our IP
        mov eax, [esi+38]
        cmp eax, [our_ip]
        jne .arp_done

        ;; Add requester to our ARP table
        push esi
        lea edi, [esi+22]               ; EDI = sender MAC
        add esi, 28                     ; ESI = sender IP
        call arp_table_add
        pop esi

        ;; Build ARP reply at NET_TRANSMIT_BUFFER
        mov edi, NET_TRANSMIT_BUFFER
        cld

        ;; Ethernet dest = requester's MAC (from offset +6 in received frame)
        push esi
        add esi, 6
        movsw
        movsw
        movsw
        pop esi

        ;; Ethernet src = our MAC
        push esi
        mov esi, mac_address
        movsw
        movsw
        movsw
        pop esi

        ;; EtherType: ARP
        mov ax, 0608h
        stosw

        ;; ARP header: hwtype, proto, sizes, opcode=reply
        mov ax, 0100h
        stosw
        mov ax, 0008h
        stosw
        mov ax, 0406h
        stosw
        mov ax, 0200h                   ; Opcode: reply
        stosw

        ;; Sender = us (MAC + IP)
        push esi
        mov esi, mac_address
        movsw
        movsw
        movsw
        mov esi, our_ip
        movsd
        pop esi

        ;; Target = requester (MAC at +22, IP at +28)
        push esi
        add esi, 22
        movsw
        movsw
        movsw
        pop esi
        push esi
        add esi, 28
        movsd
        pop esi

        ;; Pad to 60 bytes
        xor eax, eax
        mov ecx, 9
        rep stosw

        ;; Send reply
        push esi
        mov esi, NET_TRANSMIT_BUFFER
        mov ecx, 60
        call ne2k_send
        pop esi

        .arp_done:
        pop edi
        pop esi
        pop ecx
        pop eax
        ret

arp_resolve:
        ;; Resolve an IP address to a MAC address via ARP.
        ;; Input: ESI = pointer to 4-byte target IP
        ;; Output: EDI = pointer to 6-byte MAC in ARP table, CF set on timeout
        push eax
        push ebx
        push ecx
        push edx

        ;; Check ARP table first
        call arp_table_lookup
        jnc .resolve_done

        ;; Not cached — send ARP request and poll for reply
        call arp_send_request
        jc .resolve_timeout

        mov ebx, 0FFFFh                 ; Timeout counter
        .resolve_poll:
        call ne2k_receive
        jc .resolve_next                ; No packet available

        ;; Got a packet — check if it's ARP
        push esi
        mov esi, edi                    ; ESI = received packet
        call arp_handle_packet
        pop esi

        ;; Check table again
        call arp_table_lookup
        jnc .resolve_done

        .resolve_next:
        dec ebx
        jnz .resolve_poll

        .resolve_timeout:
        stc

        .resolve_done:
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

arp_send_request:
        ;; Send an ARP request for the given IP.
        ;; Input: ESI = pointer to 4-byte target IP
        ;; Output: CF from ne2k_send
        push eax
        push ecx
        push esi
        push edi

        ;; Build Ethernet + ARP frame at NET_TRANSMIT_BUFFER
        mov edi, NET_TRANSMIT_BUFFER
        cld

        ;; Ethernet dest: broadcast
        mov al, 0FFh
        mov ecx, 6
        rep stosb

        ;; Ethernet src: our MAC
        push esi
        mov esi, mac_address
        movsw
        movsw
        movsw
        pop esi

        ;; EtherType: ARP (0x0806 big-endian)
        mov ax, 0608h                   ; Stored little-endian: 08h, 06h
        stosw

        ;; ARP: hardware type 0x0001, protocol type 0x0800
        mov ax, 0100h                   ; 00h, 01h
        stosw
        mov ax, 0008h                   ; 08h, 00h
        stosw

        ;; Hardware size 6, protocol size 4
        mov ax, 0406h
        stosw

        ;; Opcode: request (0x0001)
        mov ax, 0100h                   ; 00h, 01h
        stosw

        ;; Sender MAC
        push esi
        mov esi, mac_address
        movsw
        movsw
        movsw
        pop esi

        ;; Sender IP (our_ip)
        push esi
        mov esi, our_ip
        movsd
        pop esi

        ;; Target MAC: zeros
        xor eax, eax
        stosw
        stosw
        stosw

        ;; Target IP (from caller's ESI)
        push esi
        movsd
        pop esi

        ;; Pad to 60 bytes (42 so far, 18 more = 9 words)
        xor eax, eax
        mov ecx, 9
        rep stosw

        ;; Send the frame
        mov esi, NET_TRANSMIT_BUFFER
        mov ecx, 60
        call ne2k_send

        pop edi
        pop esi
        pop ecx
        pop eax
        ret

arp_table_add:
        ;; Add or update an ARP table entry.
        ;; Input: ESI = pointer to 4-byte IP, EDI = pointer to 6-byte MAC
        push eax
        push ebx
        push ecx
        push esi
        push edi

        mov ebx, arp_table
        mov ecx, ARP_TABLE_SIZE
        mov eax, [esi]

        ;; Find existing entry or first empty slot
        .add_loop:
        cmp dword [ebx], 0
        je .add_here
        cmp [ebx], eax
        je .add_here
        add ebx, ARP_ENTRY_SIZE
        loop .add_loop

        ;; Table full — round-robin eviction
        push edx
        movzx eax, word [arp_evict]
        mov edx, ARP_ENTRY_SIZE
        mul edx                         ; EDX:EAX = idx * ARP_ENTRY_SIZE
        add eax, arp_table
        mov ebx, eax
        pop edx
        movzx eax, word [arp_evict]
        inc eax
        cmp eax, ARP_TABLE_SIZE
        jb .no_wrap
        xor eax, eax
        .no_wrap:
        mov [arp_evict], ax

        .add_here:
        mov [ebx], eax                  ; Store IP
        ;; Copy 6-byte MAC: source = MAC pointer (EDI), dest = table MAC field
        mov esi, edi
        lea edi, [ebx+4]
        cld
        movsw
        movsw
        movsw
        ;; Stamp with current uptime (16-bit seconds; wraps ~18h)
        push eax
        call uptime_seconds
        mov [ebx+ARP_TIMESTAMP_OFFSET], ax
        pop eax

        pop edi
        pop esi
        pop ecx
        pop ebx
        pop eax
        ret

arp_table_lookup:
        ;; Look up an IP in the ARP table.
        ;; Input: ESI = pointer to 4-byte IP
        ;; Output: EDI = pointer to 6-byte MAC, CF set if not found or TTL expired
        push eax
        push ebx
        push ecx
        push edx

        call uptime_seconds
        mov dx, ax                      ; DX = now (seconds since boot, 16-bit)

        mov ebx, arp_table
        mov ecx, ARP_TABLE_SIZE
        mov eax, [esi]

        .lookup_loop:
        cmp dword [ebx], 0              ; Empty entry?
        je .lookup_miss
        cmp [ebx], eax
        jne .lookup_next
        ;; IP matches — check TTL
        mov ax, dx
        sub ax, [ebx+ARP_TIMESTAMP_OFFSET]
        cmp ax, ARP_TTL_SECONDS
        ja .lookup_miss                 ; expired (or unsigned-wrap from future stamp)
        lea edi, [ebx+4]
        clc
        jmp .lookup_done
        .lookup_next:
        add ebx, ARP_ENTRY_SIZE
        loop .lookup_loop

        .lookup_miss:
        stc

        .lookup_done:
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

        ;; Variables
        arp_evict dw 0
        arp_table times (ARP_TABLE_SIZE * ARP_ENTRY_SIZE) db 0
