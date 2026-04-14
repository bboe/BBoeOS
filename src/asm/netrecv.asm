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

        mov si, MESSAGE_SENT
        mov ah, SYS_IO_PUT_STRING
        int 30h

        ;; Poll for reply
        mov bx, 0FFFFh
        .poll:
        mov ah, SYS_NET_RECEIVE
        int 30h
        jnc .got_packet
        dec bx
        jnz .poll

        mov si, MESSAGE_TIMEOUT
        mov ah, SYS_IO_PUT_STRING
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .got_packet:
        ;; DI = packet buffer, CX = length
        push cx
        mov si, MESSAGE_RECEIVE
        mov ah, SYS_IO_PUT_STRING
        int 30h
        pop cx

        ;; Print first 32 bytes as hex
        mov si, di
        cmp cx, 32
        jbe .use_length
        mov cx, 32
        .use_length:
        .hex_loop:
        lodsb
        call print_hex
        mov al, ' '
        mov ah, SYS_IO_PUT_CHARACTER
        int 30h
        loop .hex_loop

        mov al, `\n`
        mov ah, SYS_IO_PUT_CHARACTER
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .no_nic:
        mov si, MESSAGE_NO_NIC
        mov ah, SYS_IO_PUT_STRING
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .error:
        mov si, MESSAGE_ERROR
        mov ah, SYS_IO_PUT_STRING
        int 30h
        mov ah, SYS_EXIT
        int 30h

        ;; Data
        my_mac times 6 db 0

        MESSAGE_ERROR db `Send failed\n\0`
        MESSAGE_NO_NIC db `No NIC found\n\0`
        MESSAGE_RECEIVE db `Received: \0`
        MESSAGE_SENT db `ARP sent, waiting for reply...\n\0`
        MESSAGE_TIMEOUT db `No reply (timeout)\n\0`

%include "arp_frame.asm"
%include "print_hex.asm"
