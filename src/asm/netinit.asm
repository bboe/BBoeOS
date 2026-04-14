        org 0600h

%include "constants.asm"

main:
        cld

        ;; Probe NE2000 NIC
        mov di, my_mac
        mov ah, SYS_NET_INIT
        int 30h
        jc .no_nic

        ;; Print MAC address
        mov si, MESSAGE_MAC
        mov cx, MESSAGE_MAC_LENGTH
        call write_stdout

        mov si, my_mac
        call print_mac

        mov al, `\n`
        mov ah, SYS_IO_PUT_CHARACTER
        int 30h

        mov si, MESSAGE_INIT
        mov cx, MESSAGE_INIT_LENGTH
        call write_stdout

        mov ah, SYS_EXIT
        int 30h

        .no_nic:
        mov si, MESSAGE_NO_NIC
        mov cx, MESSAGE_NO_NIC_LENGTH
        call write_stdout
        mov ah, SYS_EXIT
        int 30h

        ;; Data
        my_mac times 6 db 0
        MESSAGE_INIT db `NIC initialized\n`
        MESSAGE_INIT_LENGTH equ $ - MESSAGE_INIT
        MESSAGE_MAC db `NIC found: `
        MESSAGE_MAC_LENGTH equ $ - MESSAGE_MAC
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC

%include "print_hex.asm"
%include "print_mac.asm"
%include "write_stdout.asm"
