        org 0600h

%include "constants.asm"

main:
        cld

        ;; Read our MAC (verifies NIC is up)
        mov di, my_mac
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Require dotted-decimal IP argument
        mov bx, [EXEC_ARG]
        test bx, bx
        jz .usage

        mov si, bx
        mov di, target_ip
        call parse_ip
        jc .usage

        ;; Resolve via kernel ARP cache + on-miss request
        mov si, target_ip
        mov ah, SYS_NET_ARP
        int 30h
        jc .timeout

        ;; Print "<IP> is at <MAC>\n"
        push di                 ; Save MAC pointer from ARP
        mov si, target_ip
        call FUNCTION_PRINT_IP
        mov si, MESSAGE_IS_AT
        mov cx, MESSAGE_IS_AT_LENGTH
        call FUNCTION_WRITE_STDOUT
        pop si                  ; SI = MAC pointer
        call FUNCTION_PRINT_MAC

        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        jmp FUNCTION_EXIT

        .no_nic:
        mov si, MESSAGE_NO_NIC
        mov cx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .usage:
        mov si, MESSAGE_USAGE
        mov cx, MESSAGE_USAGE_LENGTH
        jmp FUNCTION_DIE

        .timeout:
        mov si, MESSAGE_TIMEOUT
        mov cx, MESSAGE_TIMEOUT_LENGTH
        jmp FUNCTION_DIE

        ;; Data
        my_mac times 6 db 0
        target_ip times 4 db 0

        MESSAGE_IS_AT db ` is at `
        MESSAGE_IS_AT_LENGTH equ $ - MESSAGE_IS_AT
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC
        MESSAGE_TIMEOUT db `ARP timeout\n`
        MESSAGE_TIMEOUT_LENGTH equ $ - MESSAGE_TIMEOUT
        MESSAGE_USAGE db `usage: arp <ip>\n`
        MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE

%include "parse_ip.asm"
