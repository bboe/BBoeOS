        [bits 32]
        org 0600h

%include "constants.asm"

        %assign RECEIVE_BUFFER_SIZE 128

main:
        cld

        ;; Read our MAC into the shell's idle input buffer (no embedded cell).
        mov edi, BUFFER
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Build ARP request (copy our MAC into frame, 6 bytes = movsd+movsw)
        mov esi, BUFFER
        mov edi, arp_frame + 6
        movsd
        movsw
        mov esi, BUFFER
        mov edi, arp_frame + 22
        movsd
        movsw

        ;; Open raw Ethernet socket
        mov al, SOCK_RAW
        mov ah, SYS_NET_OPEN
        int 30h
        jc .no_nic
        mov [net_fd], eax

        ;; Send ARP request
        mov ebx, [net_fd]
        mov esi, arp_frame
        mov ecx, 60
        mov ah, SYS_IO_WRITE
        int 30h
        jc .error

        mov esi, MESSAGE_SENT
        mov ecx, MESSAGE_SENT_LENGTH
        call FUNCTION_WRITE_STDOUT

        ;; Poll for reply, reading into BUFFER + 128 (leaves room below for MAC)
        mov dword [poll_remaining], 30000
        .poll:
        mov ebx, [net_fd]
        mov edi, BUFFER + 128
        mov ecx, RECEIVE_BUFFER_SIZE
        mov ah, SYS_IO_READ
        int 30h
        jc .error
        test eax, eax
        jnz .got_packet
        dec dword [poll_remaining]
        jnz .poll

        mov esi, MESSAGE_TIMEOUT
        mov ecx, MESSAGE_TIMEOUT_LENGTH
        jmp FUNCTION_DIE

        .got_packet:
        ;; EAX = bytes read, [BUFFER + 128] = packet
        mov [packet_length], eax
        mov ebx, [net_fd]
        mov ah, SYS_IO_CLOSE
        int 30h

        mov esi, MESSAGE_RECEIVE
        mov ecx, MESSAGE_RECEIVE_LENGTH
        call FUNCTION_WRITE_STDOUT

        ;; Print first 32 bytes as hex
        mov esi, BUFFER + 128
        mov ecx, [packet_length]
        cmp ecx, 32
        jbe .use_length
        mov ecx, 32
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
        mov esi, MESSAGE_NO_NIC
        mov ecx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .error:
        mov esi, MESSAGE_ERROR
        mov ecx, MESSAGE_ERROR_LENGTH
        jmp FUNCTION_DIE

        ;; Data
        net_fd dd 0
        packet_length dd 0
        poll_remaining dd 0

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
