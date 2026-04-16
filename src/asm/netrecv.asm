        org 0600h

%include "constants.asm"

        %assign RECV_BUFFER_SIZE 1536

main:
        cld

        ;; Read our MAC
        mov di, my_mac
        mov ah, SYS_NET_MAC
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

        ;; Open raw Ethernet socket
        mov ah, SYS_NET_OPEN
        int 30h
        jc .no_nic
        mov [net_fd], ax

        ;; Send ARP request
        mov bx, [net_fd]
        mov si, arp_frame
        mov cx, 60
        mov ah, SYS_IO_WRITE
        int 30h
        jc .error

        mov si, MESSAGE_SENT
        mov cx, MESSAGE_SENT_LENGTH
        call FUNCTION_WRITE_STDOUT

        ;; Poll for reply
        mov word [poll_remaining], 0FFFFh
        .poll:
        mov bx, [net_fd]
        mov di, recv_buffer
        mov cx, RECV_BUFFER_SIZE
        mov ah, SYS_IO_READ
        int 30h
        jc .error
        test ax, ax
        jnz .got_packet
        dec word [poll_remaining]
        jnz .poll

        mov si, MESSAGE_TIMEOUT
        mov cx, MESSAGE_TIMEOUT_LENGTH
        jmp FUNCTION_DIE

        .got_packet:
        ;; AX = bytes read, [recv_buffer] = packet
        mov [packet_length], ax
        mov bx, [net_fd]
        mov ah, SYS_IO_CLOSE
        int 30h

        mov si, MESSAGE_RECEIVE
        mov cx, MESSAGE_RECEIVE_LENGTH
        call FUNCTION_WRITE_STDOUT

        ;; Print first 32 bytes as hex
        mov si, recv_buffer
        mov cx, [packet_length]
        cmp cx, 32
        jbe .use_length
        mov cx, 32
        .use_length:
        .hex_loop:
        lodsb
        call FUNCTION_PRINT_HEX
        mov al, ' '
        call FUNCTION_PRINT_CHARACTER
        loop .hex_loop

        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        jmp FUNCTION_EXIT

        .no_nic:
        mov si, MESSAGE_NO_NIC
        mov cx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .error:
        mov si, MESSAGE_ERROR
        mov cx, MESSAGE_ERROR_LENGTH
        jmp FUNCTION_DIE

        ;; Data
        my_mac times 6 db 0
        net_fd dw 0
        packet_length dw 0
        poll_remaining dw 0
        recv_buffer times RECV_BUFFER_SIZE db 0

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
