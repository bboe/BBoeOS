        org 6000h

%include "constants.asm"

main:
        cld

        ;; Init NIC
        mov di, my_mac
        mov ah, SYS_NET_INIT
        int 30h
        jc .no_nic

        ;; Require argument
        mov bx, [EXEC_ARG]
        test bx, bx
        jz .no_arg

        ;; Try to parse as dotted-decimal IP; fall back to DNS if it fails
        mov si, bx
        mov di, target_ip
        call parse_ip
        jnc .have_ip
        mov si, bx             ; Restore SI (parse_ip clobbers it)
        call resolve_dns
        jc .resolve_err

        .have_ip:
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

        .resolve_err:
        mov si, MSG_RESOLVE_ERR
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

resolve_dns:
        ;; Resolve domain name to IP via DNS A query
        ;; Input: SI = null-terminated domain string
        ;; Output: target_ip filled with first A record, CF set on error
        push bx
        push cx
        push di

        call dns_query
        jc .err
        test al, al
        jz .err
        mov cl, al             ; CL = answer count

        ;; Walk answer records looking for first A record
        .answer_loop:
        cmp byte [di], 0C0h
        jb .skip_labels
        add di, 2
        jmp .check_type
        .skip_labels:
        cmp byte [di], 0
        je .labels_done
        movzx bx, byte [di]
        inc di
        add di, bx
        jmp .skip_labels
        .labels_done:
        inc di

        .check_type:
        cmp word [di], 0100h   ; A record = 0x0001 big-endian
        je .found_a
        ;; Not A: skip TYPE(2)+CLASS(2)+TTL(4) = 8 bytes to RDLENGTH
        add di, 8
        movzx bx, byte [di+1]  ; RDLENGTH low byte (big-endian, high byte assumed 0)
        add di, 2
        add di, bx
        dec cl
        jnz .answer_loop
        jmp .err

        .found_a:
        add di, 10             ; Skip TYPE(2)+CLASS(2)+TTL(4)+RDLENGTH(2) to rdata
        mov ax, [di]
        mov [target_ip], ax
        mov ax, [di+2]
        mov [target_ip+2], ax
        clc
        jmp .done
        .err:
        stc
        .done:
        pop di
        pop cx
        pop bx
        ret

        ;; Data
        count db 0
        dns_base dw 0
        dns_query_buf times 300 db 0
        dns_server_ip db 10, 0, 2, 3
        my_mac times 6 db 0
        target_ip times 4 db 0

        MSG_ELLIPSIS db `...\n\0`
        MSG_NO_NIC db `No NIC found\n\0`
        MSG_PINGING db `Pinging \0`
        MSG_REPLY db `Reply from \0`
        MSG_RESOLVE_ERR db `Could not resolve hostname\n\0`
        MSG_TICKS db ` ticks\n\0`
        MSG_TIME db `: time=\0`
        MSG_TIMEOUT db `Request timed out\n\0`
        MSG_USAGE db `Usage: ping <ip|hostname>\n\0`

%include "dns_query.asm"
%include "encode_domain.asm"
%include "parse_ip.asm"
%include "print_byte_dec.asm"
%include "print_dec.asm"
%include "print_ip.asm"
