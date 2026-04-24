        %assign ARP_ENTRY_SIZE 12  ; 4 bytes IP + 6 bytes MAC + 2 bytes timestamp
        %assign ARP_TABLE_SIZE 8
        %assign ARP_TIMESTAMP_OFFSET 10
        %assign ARP_TTL_SECONDS 60

arp_handle_packet:
        ;; Process a received Ethernet frame for ARP
        ;; Input: SI = pointer to received frame (NET_RECEIVE_BUFFER)
        ;; Updates ARP table on reply, sends reply to requests for our IP
        push ax
        push cx
        push si
        push di

        ;; Check EtherType at offset 12 = 0x0806 (ARP)
        cmp byte [si+12], 08h
        jne .arp_done
        cmp byte [si+13], 06h
        jne .arp_done

        ;; Check opcode at offset 20-21
        cmp byte [si+20], 0
        jne .arp_done
        mov al, [si+21]
        cmp al, 2              ; ARP reply
        je .arp_reply
        cmp al, 1              ; ARP request
        je .arp_request_in
        jmp .arp_done

        .arp_reply:
        ;; Add sender to ARP table (sender MAC at +22, sender IP at +28)
        push si
        lea di, [si+22]       ; DI = sender MAC
        add si, 28            ; SI = sender IP
        call arp_table_add
        pop si
        jmp .arp_done

        .arp_request_in:
        ;; Check if target IP (offset +38) matches our IP
        mov eax, [si+38]
        cmp eax, [our_ip]
        jne .arp_done

        ;; Add requester to our ARP table
        push si
        lea di, [si+22]       ; DI = sender MAC
        add si, 28            ; SI = sender IP
        call arp_table_add
        pop si

        ;; Build ARP reply at NET_TRANSMIT_BUFFER
        mov di, NET_TRANSMIT_BUFFER
        cld

        ;; Ethernet dest = requester's MAC (from offset +6 in received frame)
        push si
        add si, 6
        movsw
        movsw
        movsw
        pop si

        ;; Ethernet src = our MAC
        push si
        mov si, mac_address
        movsw
        movsw
        movsw
        pop si

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
        mov ax, 0200h          ; Opcode: reply
        stosw

        ;; Sender = us (MAC + IP)
        push si
        mov si, mac_address
        movsw
        movsw
        movsw
        mov si, our_ip
        movsd
        pop si

        ;; Target = requester (MAC at +22, IP at +28)
        push si
        add si, 22
        movsw
        movsw
        movsw
        pop si
        push si
        add si, 28
        movsd
        pop si

        ;; Pad to 60 bytes
        xor ax, ax
        mov cx, 9
        rep stosw

        ;; Send reply
        push si
        mov si, NET_TRANSMIT_BUFFER
        mov cx, 60
        call ne2k_send
        pop si

        .arp_done:
        pop di
        pop si
        pop cx
        pop ax
        ret

arp_resolve:
        ;; Resolve an IP address to a MAC address via ARP
        ;; Input: SI = pointer to 4-byte target IP
        ;; Output: DI = pointer to 6-byte MAC in ARP table, CF set on timeout
        push ax
        push bx
        push cx
        push dx

        ;; Check ARP table first
        call arp_table_lookup
        jnc .resolve_done

        ;; Not cached — send ARP request and poll for reply
        call arp_send_request
        jc .resolve_timeout

        mov bx, 0FFFFh         ; Timeout counter
        .resolve_poll:
        call ne2k_receive
        jc .resolve_next       ; No packet available

        ;; Got a packet — check if it's ARP
        push si
        mov si, di             ; SI = received packet
        call arp_handle_packet
        pop si

        ;; Check table again
        call arp_table_lookup
        jnc .resolve_done

        .resolve_next:
        dec bx
        jnz .resolve_poll

        .resolve_timeout:
        stc

        .resolve_done:
        pop dx
        pop cx
        pop bx
        pop ax
        ret

arp_send_request:
        ;; Send an ARP request for the given IP
        ;; Input: SI = pointer to 4-byte target IP
        ;; Output: CF from ne2k_send
        push ax
        push cx
        push si
        push di

        ;; Build Ethernet + ARP frame at NET_TRANSMIT_BUFFER
        mov di, NET_TRANSMIT_BUFFER
        cld

        ;; Ethernet dest: broadcast
        mov al, 0FFh
        mov cx, 6
        rep stosb

        ;; Ethernet src: our MAC
        push si
        mov si, mac_address
        movsw
        movsw
        movsw
        pop si

        ;; EtherType: ARP (0x0806 big-endian)
        mov ax, 0608h          ; Stored little-endian: 08h, 06h
        stosw

        ;; ARP: hardware type 0x0001, protocol type 0x0800
        mov ax, 0100h          ; 00h, 01h
        stosw
        mov ax, 0008h          ; 08h, 00h
        stosw

        ;; Hardware size 6, protocol size 4
        mov ax, 0406h
        stosw

        ;; Opcode: request (0x0001)
        mov ax, 0100h          ; 00h, 01h
        stosw

        ;; Sender MAC
        push si
        mov si, mac_address
        movsw
        movsw
        movsw
        pop si

        ;; Sender IP (our_ip)
        push si
        mov si, our_ip
        movsd
        pop si

        ;; Target MAC: zeros
        xor ax, ax
        stosw
        stosw
        stosw

        ;; Target IP (from caller's SI)
        push si
        movsd
        pop si

        ;; Pad to 60 bytes (42 so far, 18 more = 9 words)
        xor ax, ax
        mov cx, 9
        rep stosw

        ;; Send the frame
        mov si, NET_TRANSMIT_BUFFER
        mov cx, 60
        call ne2k_send

        pop di
        pop si
        pop cx
        pop ax
        ret

arp_table_add:
        ;; Add or update an ARP table entry
        ;; Input: SI = pointer to 4-byte IP, DI = pointer to 6-byte MAC
        push ax
        push bx
        push cx
        push si
        push di

        mov bx, arp_table
        mov cx, ARP_TABLE_SIZE
        mov eax, [si]

        ;; Find existing entry or first empty slot
        .add_loop:
        cmp dword [bx], 0
        je .add_here
        cmp [bx], eax
        je .add_here
        add bx, ARP_ENTRY_SIZE
        loop .add_loop

        ;; Table full — round-robin eviction
        push dx
        mov ax, [arp_evict]
        mov dx, ARP_ENTRY_SIZE
        mul dx                 ; DX:AX = idx * ARP_ENTRY_SIZE
        add ax, arp_table
        mov bx, ax
        pop dx
        mov ax, [arp_evict]
        inc ax
        cmp ax, ARP_TABLE_SIZE
        jb .no_wrap
        xor ax, ax
        .no_wrap:
        mov [arp_evict], ax

        .add_here:
        mov [bx], eax          ; Store IP
        ;; Copy 6-byte MAC
        mov si, di             ; Source = MAC pointer
        lea di, [bx+4]        ; Dest = table MAC field
        cld
        movsw
        movsw
        movsw
        ;; Stamp with current uptime (16-bit seconds; wraps ~18h)
        push eax
        call uptime_seconds
        mov [bx+ARP_TIMESTAMP_OFFSET], ax
        pop eax

        pop di
        pop si
        pop cx
        pop bx
        pop ax
        ret

arp_table_lookup:
        ;; Look up an IP in the ARP table
        ;; Input: SI = pointer to 4-byte IP
        ;; Output: DI = pointer to 6-byte MAC, CF set if not found or TTL expired
        push ax
        push bx
        push cx
        push dx

        call uptime_seconds
        mov dx, ax              ; DX = now (seconds since boot, 16-bit)

        mov bx, arp_table
        mov cx, ARP_TABLE_SIZE
        mov eax, [si]

        .lookup_loop:
        cmp dword [bx], 0     ; Empty entry?
        je .lookup_miss
        cmp [bx], eax
        jne .lookup_next
        ;; IP matches — check TTL
        mov ax, dx
        sub ax, [bx+ARP_TIMESTAMP_OFFSET]
        cmp ax, ARP_TTL_SECONDS
        ja .lookup_miss         ; expired (or unsigned-wrap from future stamp)
        lea di, [bx+4]
        clc
        jmp .lookup_done
        .lookup_next:
        add bx, ARP_ENTRY_SIZE
        loop .lookup_loop

        .lookup_miss:
        stc

        .lookup_done:
        pop dx
        pop cx
        pop bx
        pop ax
        ret

        ;; Variables
        arp_evict dw 0
        arp_table times (ARP_TABLE_SIZE * ARP_ENTRY_SIZE) db 0
