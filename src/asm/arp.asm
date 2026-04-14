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
        mov ah, SYS_IO_PUT_STRING
        int 30h
        pop si                 ; SI = MAC pointer

        call print_mac

        mov al, `\n`
        mov ah, SYS_IO_PUT_CHARACTER
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .no_nic:
        mov si, MESSAGE_NO_NIC
        mov ah, SYS_IO_PUT_STRING
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .timeout:
        mov si, MESSAGE_TIMEOUT
        mov ah, SYS_IO_PUT_STRING
        int 30h
        mov ah, SYS_EXIT
        int 30h

        ;; Data
        my_mac times 6 db 0
        target_ip db 10, 0, 2, 2

        MESSAGE_IP db `10.0.2.2 is at \0`
        MESSAGE_NO_NIC db `No NIC found\n\0`
        MESSAGE_TIMEOUT db `ARP timeout\n\0`

%include "print_hex.asm"
%include "print_mac.asm"
