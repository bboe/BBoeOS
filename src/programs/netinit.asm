        org 6000h

%include "constants.asm"

main:
        cld

        ;; Probe NE2000 NIC
        mov di, my_mac
        mov ah, SYS_NET_INIT
        int 30h
        jc .no_nic

        ;; Print MAC address
        mov si, MSG_MAC
        mov ah, SYS_IO_PUTS
        int 30h

        mov si, my_mac
        call print_mac

        mov al, `\n`
        mov ah, SYS_IO_PUTC
        int 30h

        mov si, MSG_INIT
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

        ;; Data
        my_mac times 6 db 0
        MSG_INIT db `NIC initialized\n\0`
        MSG_MAC db `NIC found: \0`
        MSG_NO_NIC db `No NIC found\n\0`

%include "print_hex.asm"
%include "print_mac.asm"
