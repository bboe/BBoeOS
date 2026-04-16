        org 0600h

%include "constants.asm"

main:
        cld

        ;; Init NIC
        mov di, my_mac
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Require exactly one argument
        mov di, ARGV
        call FUNCTION_PARSE_ARGV
        cmp cx, 1
        jne .no_arg

        ;; Try to parse as dotted-decimal IP; fall back to DNS if it fails
        mov si, [ARGV]
        mov di, target_ip
        call parse_ip
        jnc .have_ip
        mov si, [ARGV]         ; Restore SI (parse_ip clobbers it)
        call resolve_dns
        jc .resolve_err

        .have_ip:
        ;; Print "Pinging X.X.X.X...\n"
        mov si, MESSAGE_PINGING
        mov cx, MESSAGE_PINGING_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov si, target_ip
        call FUNCTION_PRINT_IP
        mov si, MESSAGE_ELLIPSIS
        mov cx, MESSAGE_ELLIPSIS_LENGTH
        call FUNCTION_WRITE_STDOUT

        mov byte [count], 4
        .loop:
        mov si, target_ip
        mov ah, SYS_NET_PING
        int 30h
        jc .timeout

        ;; Print "Reply from X.X.X.X: time=N ticks\n"
        push ax
        mov si, MESSAGE_REPLY
        mov cx, MESSAGE_REPLY_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov si, target_ip
        call FUNCTION_PRINT_IP
        mov si, MESSAGE_TIME
        mov cx, MESSAGE_TIME_LENGTH
        call FUNCTION_WRITE_STDOUT
        pop ax
        call FUNCTION_PRINT_DECIMAL
        mov si, MESSAGE_TICKS
        mov cx, MESSAGE_TICKS_LENGTH
        call FUNCTION_WRITE_STDOUT
        jmp .next

        .timeout:
        mov si, MESSAGE_TIMEOUT
        mov cx, MESSAGE_TIMEOUT_LENGTH
        call FUNCTION_WRITE_STDOUT

        .next:
        call delay_1s
        dec byte [count]
        jnz .loop

        jmp FUNCTION_EXIT

        .no_arg:
        mov si, MESSAGE_USAGE
        mov cx, MESSAGE_USAGE_LENGTH
        jmp FUNCTION_DIE

        .no_nic:
        mov si, MESSAGE_NO_NIC
        mov cx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .resolve_err:
        mov si, MESSAGE_RESOLVE_ERROR
        mov cx, MESSAGE_RESOLVE_ERROR_LENGTH
        jmp FUNCTION_DIE

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

        MESSAGE_ELLIPSIS db `...\n`
        MESSAGE_ELLIPSIS_LENGTH equ $ - MESSAGE_ELLIPSIS
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC
        MESSAGE_PINGING db `Pinging `
        MESSAGE_PINGING_LENGTH equ $ - MESSAGE_PINGING
        MESSAGE_REPLY db `Reply from `
        MESSAGE_REPLY_LENGTH equ $ - MESSAGE_REPLY
        MESSAGE_RESOLVE_ERROR db `Could not resolve hostname\n`
        MESSAGE_RESOLVE_ERROR_LENGTH equ $ - MESSAGE_RESOLVE_ERROR
        MESSAGE_TICKS db ` ticks\n`
        MESSAGE_TICKS_LENGTH equ $ - MESSAGE_TICKS
        MESSAGE_TIME db `: time=`
        MESSAGE_TIME_LENGTH equ $ - MESSAGE_TIME
        MESSAGE_TIMEOUT db `Request timed out\n`
        MESSAGE_TIMEOUT_LENGTH equ $ - MESSAGE_TIMEOUT
        MESSAGE_USAGE db `Usage: ping <ip|hostname>\n`
        MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE

%include "dns_query.asm"
%include "encode_domain.asm"
%include "parse_ip.asm"
