        org 0600h

%include "constants.asm"

main:
        cld

        ;; Read our MAC into the shell's idle input buffer (no embedded cell).
        mov di, BUFFER
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Copy our MAC into ARP frame (src MAC at offset 6, sender MAC at offset 22)
        mov si, BUFFER
        mov di, arp_frame + 6
        mov cx, 3
        rep movsw
        mov si, BUFFER
        mov di, arp_frame + 22
        mov cx, 3
        rep movsw

        ;; Open raw Ethernet socket
        mov ah, SYS_NET_OPEN
        int 30h
        jc .no_nic
        mov bx, ax              ; BX = fd

        ;; Write ARP frame to the socket
        mov si, arp_frame
        mov cx, 60
        mov ah, SYS_IO_WRITE
        int 30h
        jc .error

        ;; Close socket
        mov ah, SYS_IO_CLOSE
        int 30h

        mov si, MESSAGE_SENT
        mov cx, MESSAGE_SENT_LENGTH
        jmp FUNCTION_DIE

        .no_nic:
        mov si, MESSAGE_NO_NIC
        mov cx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .error:
        mov si, MESSAGE_ERROR
        mov cx, MESSAGE_ERROR_LENGTH
        jmp FUNCTION_DIE

        ;; Data
        MESSAGE_ERROR db `Send failed\n`
        MESSAGE_ERROR_LENGTH equ $ - MESSAGE_ERROR
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC
        MESSAGE_SENT db `ARP request sent\n`
        MESSAGE_SENT_LENGTH equ $ - MESSAGE_SENT

%include "arp_frame.asm"
