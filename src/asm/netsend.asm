        org 0600h

%include "constants.asm"

main:
        cld

        ;; Init NIC
        mov di, my_mac
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Copy our MAC into ARP frame (src MAC at offset 6, sender MAC at offset 22)
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
        my_mac times 6 db 0

        MESSAGE_ERROR db `Send failed\n`
        MESSAGE_ERROR_LENGTH equ $ - MESSAGE_ERROR
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC
        MESSAGE_SENT db `ARP request sent\n`
        MESSAGE_SENT_LENGTH equ $ - MESSAGE_SENT

%include "arp_frame.asm"
