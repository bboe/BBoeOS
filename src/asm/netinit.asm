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
        call FUNCTION_WRITE_STDOUT

        mov si, my_mac
        call FUNCTION_PRINT_MAC

        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER

        mov si, MESSAGE_INIT
        mov cx, MESSAGE_INIT_LENGTH
        jmp FUNCTION_DIE

        .no_nic:
        mov si, MESSAGE_NO_NIC
        mov cx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        ;; Data
        my_mac times 6 db 0
        MESSAGE_INIT db `NIC initialized\n`
        MESSAGE_INIT_LENGTH equ $ - MESSAGE_INIT
        MESSAGE_MAC db `NIC found: `
        MESSAGE_MAC_LENGTH equ $ - MESSAGE_MAC
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC

