asm("
icmp_receive:
        ;; Poll for one ICMP packet destined for us
        ;; Output: DI = pointer to ICMP bytes (within NET_RECEIVE_BUFFER),
        ;;         CX = ICMP byte count, CF clear if packet received
        ;;         CF set if no packet (transparently handles ARP while polling)
        ;; Assumes a 20-byte IP header (no IP options) — matches what our
        ;; stack and every sane peer produces for ICMP.
        push ax

        call ne2k_receive
        jc .ir_none

        ;; Let ARP process the frame transparently.
        push si
        mov si, di
        call arp_handle_packet
        pop si

        ;; Require EtherType=IPv4 (big-endian 0x0800) and proto=ICMP.
        cmp word [di+12], 0008h
        jne .ir_none
        cmp byte [di+23], 1
        jne .ir_none

        ;; CX = IP total length - 20 (IHL). Total length at offset 16 big-endian.
        mov ax, [di+16]
        xchg al, ah
        sub ax, 20
        mov cx, ax

        add di, 34             ; Eth(14) + IP(20) -> ICMP start
        clc
        pop ax
        ret

        .ir_none:
        stc
        pop ax
        ret
");
