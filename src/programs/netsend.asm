        org 6000h

%include "constants.asm"

main:
        cld

        ;; Init NIC
        mov di, my_mac
        mov ah, SYS_NET_INIT
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

        mov si, MSG_SENT
        mov ah, SYS_IO_PUTS
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
        MSG_SENT db `ARP request sent\n\0`

%include "arp_frame.asm"
