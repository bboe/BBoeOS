        org 0600h

%include "constants.asm"

main:
        cld

        ;; Init NIC
        mov di, my_mac
        mov ah, SYS_NET_INIT
        int 30h
        jc .no_nic

        ;; Build ARP request (copy our MAC into frame)
        mov si, my_mac
        mov di, arp_frame + 6
        mov cx, 3
        rep movsw
        mov si, my_mac
        mov di, arp_frame + 22
        mov cx, 3
        rep movsw

        ;; Send ARP request
        mov si, arp_frame
        mov cx, 60
        mov ah, SYS_NET_SEND
        int 30h
        jc .error

        mov si, MSG_SENT
        mov ah, SYS_IO_PUTS
        int 30h

        ;; Poll for reply
        mov bx, 0FFFFh
        .poll:
        mov ah, SYS_NET_RECV
        int 30h
        jnc .got_packet
        dec bx
        jnz .poll

        mov si, MSG_TIMEOUT
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .got_packet:
        ;; DI = packet buffer, CX = length
        push cx
        mov si, MSG_RECV
        mov ah, SYS_IO_PUTS
        int 30h
        pop cx

        ;; Print first 32 bytes as hex
        mov si, di
        cmp cx, 32
        jbe .use_len
        mov cx, 32
        .use_len:
        .hex_loop:
        lodsb
        call print_hex
        mov al, ' '
        mov ah, SYS_IO_PUTC
        int 30h
        loop .hex_loop

        mov al, `\n`
        mov ah, SYS_IO_PUTC
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .no_nic:
        mov si, MSG_NO_NIC
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .error:
        mov si, MSG_ERROR
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        ;; Data
        my_mac times 6 db 0

        MSG_ERROR db `Send failed\n\0`
        MSG_NO_NIC db `No NIC found\n\0`
        MSG_RECV db `Received: \0`
        MSG_SENT db `ARP sent, waiting for reply...\n\0`
        MSG_TIMEOUT db `No reply (timeout)\n\0`

%include "arp_frame.asm"
%include "print_hex.asm"
