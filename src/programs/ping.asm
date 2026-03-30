        org 6000h

%include "constants.asm"

main:
        cld

        ;; Init NIC
        mov di, my_mac
        mov ah, SYS_NET_INIT
        int 30h
        jc .no_nic

        ;; Require IP argument
        mov bx, [EXEC_ARG]
        test bx, bx
        jz .no_arg

        ;; Parse IP address from argument into target_ip
        mov si, bx
        mov di, target_ip
        call parse_ip
        jc .no_arg

        ;; Print "Pinging X.X.X.X...\n"
        mov si, MSG_PINGING
        mov ah, SYS_IO_PUTS
        int 30h
        mov si, target_ip
        call print_ip
        mov si, MSG_ELLIPSIS
        mov ah, SYS_IO_PUTS
        int 30h

        mov byte [count], 4
        .loop:
        mov si, target_ip
        mov ah, SYS_NET_PING
        int 30h
        jc .timeout

        ;; Print "Reply from X.X.X.X: time=N ticks\n"
        push ax
        mov si, MSG_REPLY
        mov ah, SYS_IO_PUTS
        int 30h
        mov si, target_ip
        call print_ip
        mov si, MSG_TIME
        mov ah, SYS_IO_PUTS
        int 30h
        pop ax
        call print_dec
        mov si, MSG_TICKS
        mov ah, SYS_IO_PUTS
        int 30h
        jmp .next

        .timeout:
        mov si, MSG_TIMEOUT
        mov ah, SYS_IO_PUTS
        int 30h

        .next:
        call delay_1s
        dec byte [count]
        jnz .loop

        mov ah, SYS_EXIT
        int 30h

        .no_arg:
        mov si, MSG_USAGE
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

delay_1s:
        ;; Wait approximately 1 second using BIOS timer ticks
        push ax
        push cx
        push dx
        xor ah, ah
        int 1Ah                ; DX = current tick count
        add dx, 18             ; ~1 second (18.2 ticks/sec)
        mov cx, dx
        .wait:
        xor ah, ah
        int 1Ah
        cmp dx, cx
        jb .wait
        pop dx
        pop cx
        pop ax
        ret

        ;; Data
        count db 0
        my_mac times 6 db 0
        target_ip times 4 db 0

        MSG_ELLIPSIS db `...\n\0`
        MSG_NO_NIC db `No NIC found\n\0`
        MSG_PINGING db `Pinging \0`
        MSG_REPLY db `Reply from \0`
        MSG_TICKS db ` ticks\n\0`
        MSG_TIME db `: time=\0`
        MSG_TIMEOUT db `Request timed out\n\0`
        MSG_USAGE db `Usage: ping <ip>\n\0`

%include "parse_ip.asm"
%include "print_byte_dec.asm"
%include "print_dec.asm"
%include "print_ip.asm"
