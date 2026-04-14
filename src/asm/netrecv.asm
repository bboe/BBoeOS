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
        mov cx, MESSAGE_SENT_LENGTH
        call write_stdout

        ;; Poll for reply
        mov bx, 0FFFFh
        .poll:
        mov ah, SYS_NET_RECEIVE
        int 30h
        jnc .got_packet
        dec bx
        jnz .poll

        mov si, MESSAGE_TIMEOUT
        mov cx, MESSAGE_TIMEOUT_LENGTH
        call write_stdout
        mov ah, SYS_EXIT
        int 30h

        .got_packet:
        ;; DI = packet buffer, CX = length
        push cx
        mov si, MESSAGE_RECEIVE
        mov cx, MESSAGE_RECEIVE_LENGTH
        call write_stdout
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
        mov cx, MESSAGE_NO_NIC_LENGTH
        jmp .print_exit

        .error:
        mov si, MESSAGE_ERROR
        mov cx, MESSAGE_ERROR_LENGTH

        .print_exit:
        call write_stdout
        mov ah, SYS_EXIT
        int 30h

        ;; Data
        my_mac times 6 db 0

        MESSAGE_ERROR db `Send failed\n`
        MESSAGE_ERROR_LENGTH equ $ - MESSAGE_ERROR
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC
        MESSAGE_RECEIVE db `Received: `
        MESSAGE_RECEIVE_LENGTH equ $ - MESSAGE_RECEIVE
        MESSAGE_SENT db `ARP sent, waiting for reply...\n`
        MESSAGE_SENT_LENGTH equ $ - MESSAGE_SENT
        MESSAGE_TIMEOUT db `No reply (timeout)\n`
        MESSAGE_TIMEOUT_LENGTH equ $ - MESSAGE_TIMEOUT

%include "arp_frame.asm"
%include "print_hex.asm"
%include "write_stdout.asm"
