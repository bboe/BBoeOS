        [bits 32]
        org 0600h

%include "constants.asm"

main:
        cld

        ;; Read our MAC into the shell's idle input buffer (no embedded cell).
        mov edi, BUFFER
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Copy our MAC into ARP frame: src MAC at offset 6, sender MAC
        ;; at offset 22.  6 bytes per copy = movsd + movsw.
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
        mov ebx, eax            ; EBX = fd

        ;; Write ARP frame to the socket
        mov esi, arp_frame
        mov ecx, 60
        mov ah, SYS_IO_WRITE
        int 30h
        jc .error

        ;; Close socket
        mov ah, SYS_IO_CLOSE
        int 30h

        mov esi, MESSAGE_SENT
        mov ecx, MESSAGE_SENT_LENGTH
        jmp FUNCTION_DIE

        .no_nic:
        mov esi, MESSAGE_NO_NIC
        mov ecx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .error:
        mov esi, MESSAGE_ERROR
        mov ecx, MESSAGE_ERROR_LENGTH
        jmp FUNCTION_DIE

        ;; Data
        MESSAGE_ERROR db `Send failed\n`
        MESSAGE_ERROR_LENGTH equ $ - MESSAGE_ERROR
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC
        MESSAGE_SENT db `ARP request sent\n`
        MESSAGE_SENT_LENGTH equ $ - MESSAGE_SENT

%include "arp_frame.asm"
