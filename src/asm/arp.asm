        org 0600h

%include "constants.asm"

main:
        cld

        ;; Init NIC
        mov di, my_mac
        mov ah, SYS_NET_INIT
        int 30h
        jc .no_nic

        ;; Resolve gateway IP via ARP
        mov si, target_ip
        mov ah, SYS_NET_ARP
        int 30h
        jc .timeout

        ;; Print result: "10.0.2.2 is at XX:XX:XX:XX:XX:XX"
        push di                ; Save MAC pointer
        mov si, MESSAGE_IP
        mov cx, MESSAGE_IP_LENGTH
        call FUNCTION_WRITE_STDOUT
        pop si                 ; SI = MAC pointer

        call FUNCTION_PRINT_MAC

        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        jmp FUNCTION_EXIT

        .no_nic:
        mov si, MESSAGE_NO_NIC
        mov cx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .timeout:
        mov si, MESSAGE_TIMEOUT
        mov cx, MESSAGE_TIMEOUT_LENGTH
        jmp FUNCTION_DIE

        ;; Data
        my_mac times 6 db 0
        target_ip db 10, 0, 2, 2

        MESSAGE_IP db `10.0.2.2 is at `
        MESSAGE_IP_LENGTH equ $ - MESSAGE_IP
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC
        MESSAGE_TIMEOUT db `ARP timeout\n`
        MESSAGE_TIMEOUT_LENGTH equ $ - MESSAGE_TIMEOUT

