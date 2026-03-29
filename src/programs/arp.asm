        org 6000h

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
        mov si, MSG_IP
        mov ah, SYS_IO_PUTS
        int 30h
        pop si                 ; SI = MAC pointer

        call print_mac

        mov al, `\n`
        mov ah, SYS_IO_PUTC
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .no_nic:
        mov si, MSG_NO_NIC
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .timeout:
        mov si, MSG_TIMEOUT
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        ;; Data
        my_mac times 6 db 0
        target_ip db 10, 0, 2, 2

        MSG_IP db `10.0.2.2 is at \0`
        MSG_NO_NIC db `No NIC found\n\0`
        MSG_TIMEOUT db `ARP timeout\n\0`

%include "print_hex.asm"
%include "print_mac.asm"
