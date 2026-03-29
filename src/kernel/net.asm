        ;; NE2000 on-board RAM page layout (16KB = 64 pages of 256 bytes)
        %assign ARP_ENTRY_SIZE 10  ; 4 bytes IP + 6 bytes MAC
        %assign ARP_TABLE_SIZE 8
        %assign NE2K_RX_START 46h  ; RX ring start (6 TX pages = 1536 bytes)
        %assign NE2K_RX_STOP 80h   ; RX ring end (one past last page)
        %assign NE2K_TX_PAGE 40h   ; TX buffer start page

ne2k_probe:
        ;; Probe and reset NE2000 NIC, read MAC address into mac_addr
        ;; Output: CF clear on success, CF set on failure (no NIC or timeout)
        push ax
        push cx
        push dx
        push di

        ;; Reset the NIC
        mov dx, NE2K_BASE + 1Fh ; Reset port
        in al, dx
        out dx, al              ; Write back to trigger reset

        ;; Wait for ISR reset bit (bit 7)
        mov cx, 0FFFFh
        mov dx, NE2K_BASE + 07h ; ISR
        .wait_reset:
        in al, dx
        test al, 80h           ; RST bit
        jnz .reset_done
        loop .wait_reset
        stc                    ; Timeout — no NIC found
        jmp .probe_done

        .reset_done:
        ;; Acknowledge all interrupts
        mov al, 0FFh
        out dx, al

        ;; Stop the NIC: page 0, stop, abort DMA
        mov dx, NE2K_BASE      ; CR
        mov al, 21h
        out dx, al

        ;; Verify NIC exists by reading back CR
        in al, dx
        and al, 3Fh            ; Mask off page select bits
        cmp al, 21h
        jne .probe_fail

        ;; NE2000 register reference:
        ;; Datasheet: https://media.digikey.com/pdf/Data%20Sheets/Texas%20Instruments%20PDFs/DP8390D,NS32490D.pdf
        ;; OSDev wiki: https://wiki.osdev.org/Ne2000

        ;; Data configuration: word-wide DMA, normal mode, 4-byte FIFO
        mov dx, NE2K_BASE + 0Eh ; DCR
        mov al, 49h
        out dx, al

        ;; Clear remote byte count
        mov dx, NE2K_BASE + 0Ah ; RBCR0
        xor al, al
        out dx, al
        inc dx                 ; RBCR1
        out dx, al

        ;; Monitor mode — don't accept packets during probe
        mov dx, NE2K_BASE + 0Ch ; RCR
        mov al, 20h
        out dx, al

        ;; Internal loopback
        mov dx, NE2K_BASE + 0Dh ; TCR
        mov al, 02h
        out dx, al

        ;; Read 32 bytes of PROM via remote DMA
        mov dx, NE2K_BASE + 08h ; RSAR0
        xor al, al
        out dx, al             ; Remote start address low = 0
        inc dx                 ; RSAR1
        out dx, al             ; Remote start address high = 0

        mov dx, NE2K_BASE + 0Ah ; RBCR0
        mov al, 20h            ; 32 bytes
        out dx, al
        inc dx                 ; RBCR1
        xor al, al
        out dx, al

        mov dx, NE2K_BASE      ; CR
        mov al, 0Ah            ; Start + Remote Read DMA
        out dx, al

        ;; Read 6 MAC bytes (word mode: low byte of each word is the MAC byte)
        mov di, mac_addr
        mov cx, 6
        mov dx, NE2K_BASE + 10h ; Data port
        cld
        .read_mac:
        in ax, dx
        stosb                  ; Store low byte
        loop .read_mac

        ;; Drain remaining 10 words to complete the 32-byte DMA transfer
        mov cx, 10
        .drain:
        in ax, dx
        loop .drain

        ;; Wait for remote DMA complete (ISR RDC bit)
        mov dx, NE2K_BASE + 07h ; ISR
        .wait_dma:
        in al, dx
        test al, 40h           ; RDC bit
        jz .wait_dma
        mov al, 40h
        out dx, al             ; Acknowledge RDC

        clc                    ; Success
        jmp .probe_done

        .probe_fail:
        stc

        .probe_done:
        pop di
        pop dx
        pop cx
        pop ax
        ret

        ;; Variables
        mac_addr times 6 db 0

ne2k_init:
        ;; Fully initialize the NE2000 for sending and receiving packets
        ;; Must be called after successful ne2k_probe
        push ax
        push cx
        push dx
        push si

        ;; Page 0, stop, DMA abort
        mov dx, NE2K_BASE
        mov al, 21h
        out dx, al

        ;; Set up RX ring buffer pages
        mov dx, NE2K_BASE + 01h ; PSTART
        mov al, NE2K_RX_START
        out dx, al
        mov dx, NE2K_BASE + 02h ; PSTOP
        mov al, NE2K_RX_STOP
        out dx, al
        mov dx, NE2K_BASE + 03h ; BOUNDARY
        mov al, NE2K_RX_START
        out dx, al

        ;; Set TX page start
        mov dx, NE2K_BASE + 04h ; TPSR
        mov al, NE2K_TX_PAGE
        out dx, al

        ;; Switch to page 1 to set CURR and physical address
        mov dx, NE2K_BASE       ; CR
        mov al, 61h             ; Page 1, stop, DMA abort
        out dx, al

        ;; Set CURR (next page NIC will write to)
        mov dx, NE2K_BASE + 07h ; CURR (page 1)
        mov al, NE2K_RX_START + 1
        out dx, al

        ;; Program physical address registers PAR0-PAR5 (page 1, regs 01h-06h)
        cld
        mov si, mac_addr
        mov dx, NE2K_BASE + 01h
        mov cx, 6
        .set_mac:
        lodsb
        out dx, al
        inc dx
        loop .set_mac

        ;; Set multicast filter to accept all (MAR0-MAR7, page 1, regs 08h-0Fh)
        mov dx, NE2K_BASE + 08h
        mov cx, 8
        mov al, 0FFh
        .set_mar:
        out dx, al
        inc dx
        loop .set_mar

        ;; Switch back to page 0
        mov dx, NE2K_BASE       ; CR
        mov al, 21h             ; Page 0, stop, DMA abort
        out dx, al

        ;; Accept broadcast and unicast packets
        mov dx, NE2K_BASE + 0Ch ; RCR
        mov al, 04h             ; AB (Accept Broadcast)
        out dx, al

        ;; Normal transmit mode (no loopback)
        mov dx, NE2K_BASE + 0Dh ; TCR
        xor al, al
        out dx, al

        ;; Clear all pending interrupts
        mov dx, NE2K_BASE + 07h ; ISR
        mov al, 0FFh
        out dx, al

        ;; Disable interrupt generation (polled mode)
        mov dx, NE2K_BASE + 0Fh ; IMR
        xor al, al
        out dx, al

        ;; Start the NIC
        mov dx, NE2K_BASE       ; CR
        mov al, 22h             ; Page 0, start, DMA abort
        out dx, al

        pop si
        pop dx
        pop cx
        pop ax
        ret

ne2k_send:
        ;; Send a raw Ethernet frame via the NE2000
        ;; Input: SI = pointer to frame data, CX = frame length in bytes
        ;; Output: CF clear on success, CF set on error
        push ax
        push cx
        push dx
        push si

        ;; Ensure minimum Ethernet frame size (60 bytes, NIC adds 4-byte FCS)
        cmp cx, 60
        jae .len_ok
        mov cx, 60
        .len_ok:

        push cx                ; Save frame length for TX byte count

        ;; Round up to even byte count for word-mode DMA
        inc cx
        and cx, 0FFFEh

        ;; Set remote DMA start address to TX buffer (page * 256)
        mov dx, NE2K_BASE + 08h ; RSAR0
        xor al, al
        out dx, al
        inc dx                  ; RSAR1
        mov al, NE2K_TX_PAGE
        out dx, al

        ;; Set remote byte count
        mov dx, NE2K_BASE + 0Ah ; RBCR0
        mov al, cl
        out dx, al
        inc dx                  ; RBCR1
        mov al, ch
        out dx, al

        ;; Start remote write DMA
        mov dx, NE2K_BASE       ; CR
        mov al, 12h             ; Page 0, start, remote write
        out dx, al

        ;; Write frame data to NIC via data port
        shr cx, 1              ; Word count
        mov dx, NE2K_BASE + 10h ; Data port
        cld
        rep outsw

        ;; Wait for remote DMA complete
        mov dx, NE2K_BASE + 07h ; ISR
        .wait_dma:
        in al, dx
        test al, 40h           ; RDC bit
        jz .wait_dma
        mov al, 40h
        out dx, al             ; Acknowledge RDC

        pop cx                 ; Restore frame length

        ;; Set TX page start register
        mov dx, NE2K_BASE + 04h ; TPSR
        mov al, NE2K_TX_PAGE
        out dx, al

        ;; Set TX byte count
        mov dx, NE2K_BASE + 05h ; TBCR0
        mov al, cl
        out dx, al
        inc dx                  ; TBCR1
        mov al, ch
        out dx, al

        ;; Issue transmit command
        mov dx, NE2K_BASE       ; CR
        mov al, 26h             ; Page 0, start, transmit
        out dx, al

        ;; Wait for transmit complete (PTX or TXE bit in ISR)
        mov dx, NE2K_BASE + 07h ; ISR
        mov cx, 0FFFFh
        .wait_tx:
        in al, dx
        test al, 0Ah           ; PTX (02h) or TXE (08h)
        jnz .tx_done
        loop .wait_tx
        stc                    ; Timeout
        jmp .send_done

        .tx_done:
        test al, 08h           ; Transmit error?
        jnz .tx_error
        mov al, 0Ah
        out dx, al             ; Acknowledge PTX + TXE
        clc
        jmp .send_done

        .tx_error:
        mov al, 0Ah
        out dx, al             ; Acknowledge
        stc

        .send_done:
        pop si
        pop dx
        pop cx
        pop ax
        ret

ne2k_recv:
        ;; Receive a packet from the NE2000 RX ring buffer (polled)
        ;; Output: DI = NET_RX_BUF (packet data), CX = packet length
        ;;         CF clear if packet received, CF set if no packet available
        push ax
        push bx
        push dx
        push si

        ;; Read CURR from page 1
        mov dx, NE2K_BASE       ; CR
        mov al, 62h             ; Page 1, start, DMA abort
        out dx, al
        mov dx, NE2K_BASE + 07h ; CURR (page 1)
        in al, dx
        mov bl, al              ; BL = CURR

        ;; Switch back to page 0
        mov dx, NE2K_BASE       ; CR
        mov al, 22h             ; Page 0, start, DMA abort
        out dx, al

        ;; Next read page = BOUNDARY + 1 (wrap at PSTOP)
        mov dx, NE2K_BASE + 03h ; BOUNDARY
        in al, dx
        inc al
        cmp al, NE2K_RX_STOP
        jb .no_wrap_read
        mov al, NE2K_RX_START
        .no_wrap_read:

        ;; If next read page == CURR, ring is empty
        cmp al, bl
        je .no_packet

        mov bh, al              ; BH = page to read from

        ;; Read 4-byte ring buffer header via remote DMA
        mov dx, NE2K_BASE + 08h ; RSAR0
        xor al, al
        out dx, al              ; Address low = 0 (page-aligned)
        inc dx                  ; RSAR1
        mov al, bh
        out dx, al              ; Address high = read page

        mov dx, NE2K_BASE + 0Ah ; RBCR0
        mov al, 4
        out dx, al
        inc dx                  ; RBCR1
        xor al, al
        out dx, al

        mov dx, NE2K_BASE       ; CR
        mov al, 0Ah             ; Start, remote read DMA
        out dx, al

        ;; Read header (word mode): word 1 = [next_page:status], word 2 = length
        mov dx, NE2K_BASE + 10h ; Data port
        in ax, dx               ; AL = status, AH = next page
        mov bl, ah              ; BL = next page pointer
        in ax, dx               ; AX = total length (including 4-byte header)
        sub ax, 4
        mov cx, ax              ; CX = Ethernet frame length

        ;; Wait for header DMA complete
        mov dx, NE2K_BASE + 07h ; ISR
        .wait_hdr_dma:
        in al, dx
        test al, 40h            ; RDC bit
        jz .wait_hdr_dma
        mov al, 40h
        out dx, al              ; Acknowledge

        ;; Read packet data at (read_page * 256 + 4)
        push cx                 ; Save packet length

        ;; Round up to even for word-mode DMA
        mov ax, cx
        inc ax
        and ax, 0FFFEh
        mov cx, ax

        mov dx, NE2K_BASE + 08h ; RSAR0
        mov al, 4               ; Past the 4-byte header
        out dx, al
        inc dx                  ; RSAR1
        mov al, bh              ; Read page
        out dx, al

        mov dx, NE2K_BASE + 0Ah ; RBCR0
        mov al, cl
        out dx, al
        inc dx                  ; RBCR1
        mov al, ch
        out dx, al

        mov dx, NE2K_BASE       ; CR
        mov al, 0Ah             ; Start, remote read DMA
        out dx, al

        ;; Read packet data into NET_RX_BUF
        shr cx, 1              ; Word count
        mov di, NET_RX_BUF
        mov dx, NE2K_BASE + 10h ; Data port
        cld
        rep insw

        ;; Wait for packet DMA complete
        mov dx, NE2K_BASE + 07h ; ISR
        .wait_pkt_dma:
        in al, dx
        test al, 40h            ; RDC bit
        jz .wait_pkt_dma
        mov al, 40h
        out dx, al              ; Acknowledge

        ;; Update BOUNDARY = next_page - 1 (wrap at PSTART)
        mov al, bl              ; Next page from header
        dec al
        cmp al, NE2K_RX_START
        jae .no_wrap_bndy
        mov al, NE2K_RX_STOP - 1
        .no_wrap_bndy:
        mov dx, NE2K_BASE + 03h ; BOUNDARY
        out dx, al

        pop cx                 ; Restore packet length
        mov di, NET_RX_BUF
        clc
        jmp .recv_done

        .no_packet:
        stc

        .recv_done:
        pop si
        pop dx
        pop bx
        pop ax
        ret

arp_handle_packet:
        ;; Process a received Ethernet frame for ARP
        ;; Input: SI = pointer to received frame (NET_RX_BUF)
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

        ;; Build ARP reply at NET_TX_BUF
        mov di, NET_TX_BUF
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
        mov si, mac_addr
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
        mov si, mac_addr
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
        mov si, NET_TX_BUF
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
        call ne2k_recv
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

        ;; Build Ethernet + ARP frame at NET_TX_BUF
        mov di, NET_TX_BUF
        cld

        ;; Ethernet dest: broadcast
        mov al, 0FFh
        mov cx, 6
        rep stosb

        ;; Ethernet src: our MAC
        push si
        mov si, mac_addr
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
        mov si, mac_addr
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
        mov si, NET_TX_BUF
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
        mov ax, [arp_evict]
        mov bx, ax
        shl ax, 1              ; ax = idx * 2
        shl bx, 3              ; bx = idx * 8
        add ax, bx             ; ax = idx * 10 = idx * ARP_ENTRY_SIZE
        add ax, arp_table
        mov bx, ax
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

        pop di
        pop si
        pop cx
        pop bx
        pop ax
        ret

arp_table_lookup:
        ;; Look up an IP in the ARP table
        ;; Input: SI = pointer to 4-byte IP
        ;; Output: DI = pointer to 6-byte MAC, CF set if not found
        push ax
        push bx
        push cx

        mov bx, arp_table
        mov cx, ARP_TABLE_SIZE
        mov eax, [si]

        .lookup_loop:
        cmp dword [bx], 0     ; Empty entry?
        je .lookup_miss
        cmp [bx], eax
        je .lookup_hit
        add bx, ARP_ENTRY_SIZE
        loop .lookup_loop

        .lookup_miss:
        stc
        jmp .lookup_done

        .lookup_hit:
        lea di, [bx+4]        ; DI = pointer to MAC (past IP)
        clc

        .lookup_done:
        pop cx
        pop bx
        pop ax
        ret

        ;; Network state
        arp_evict dw 0
        arp_table times (ARP_TABLE_SIZE * ARP_ENTRY_SIZE) db 0
        our_ip db 10, 0, 2, 15
