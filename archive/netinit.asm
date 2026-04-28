        [bits 32]
        org 0600h

%include "constants.asm"

main:
        cld

        ;; Read cached MAC (NIC was probed at boot)
        mov edi, my_mac
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Print MAC address
        mov esi, MESSAGE_MAC
        mov ecx, MESSAGE_MAC_LENGTH
        call FUNCTION_WRITE_STDOUT

        mov esi, my_mac
        call FUNCTION_PRINT_MAC

        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        jmp FUNCTION_EXIT

        .no_nic:
        mov esi, MESSAGE_NO_NIC
        mov ecx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        ;; Data
        my_mac times 6 db 0
        MESSAGE_MAC db `NIC found: `
        MESSAGE_MAC_LENGTH equ $ - MESSAGE_MAC
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC
