        org 0600h

%include "constants.asm"

%define PING_COUNT 4
%define PING_ICMP_LENGTH 16

main:
        cld

        ;; Verify NIC by reading MAC
        mov di, my_mac
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Require exactly one argument
        mov di, ARGV
        call FUNCTION_PARSE_ARGV
        cmp cx, 1
        jne .no_arg

        ;; Try dotted-decimal IP; fall back to DNS
        mov si, [ARGV]
        mov di, target_ip
        call parse_ip
        jnc .have_ip
        mov si, [ARGV]
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

        ;; Open ICMP socket (Linux-style SOCK_DGRAM + IPPROTO_ICMP)
        mov al, SOCK_DGRAM
        mov dl, IPPROTO_ICMP
        mov ah, SYS_NET_OPEN
        int 30h
        jc .sock_err
        mov [socket_fd], ax

        mov byte [count], PING_COUNT
        .loop:
        ;; Build ICMP echo request: 8-byte header + 8-byte payload
        mov di, icmp_packet
        mov al, 8              ; Type: echo request
        stosb
        xor al, al             ; Code
        stosb
        xor ax, ax             ; Checksum placeholder
        stosw
        mov ax, 0100h          ; Identifier = 1 (big-endian)
        stosw
        mov ax, [ping_seq]
        xchg al, ah            ; Sequence in network byte order
        stosw
        inc word [ping_seq]
        xor ax, ax             ; 8 bytes of zero payload
        stosw
        stosw
        stosw
        stosw

        ;; Inline 1's-complement checksum over the 16 ICMP bytes
        mov si, icmp_packet
        mov cx, PING_ICMP_LENGTH / 2
        xor bx, bx
        .cksum:
        lodsw
        add bx, ax
        adc bx, 0
        loop .cksum
        not bx
        mov [icmp_packet + 2], bx

        ;; Record start tick (low word of BIOS tick counter)
        xor ah, ah
        int 1Ah
        mov [start_ticks], dx

        ;; sendto(fd, icmp_packet, 16, target_ip) — ICMP ignores ports
        mov bx, [socket_fd]
        mov si, icmp_packet
        mov cx, PING_ICMP_LENGTH
        mov di, target_ip
        mov ah, SYS_NET_SENDTO
        int 30h
        jc .timeout

        ;; Poll recvfrom until we see an ICMP echo reply
        mov bp, 0FFFFh
        .poll:
        mov bx, [socket_fd]
        mov di, recv_buffer
        mov cx, 128
        xor dx, dx
        mov ah, SYS_NET_RECVFROM
        int 30h
        test ax, ax
        jz .poll_next
        cmp byte [recv_buffer], 0       ; ICMP type 0 = echo reply
        jne .poll_next
        ;; Got reply — RTT = now - start
        xor ah, ah
        int 1Ah
        sub dx, [start_ticks]
        mov ax, dx
        jmp .print_reply
        .poll_next:
        dec bp
        jnz .poll
        jmp .timeout

        .print_reply:
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
        ;; Sleep ~1 second between pings
        mov cx, 1000
        mov ah, SYS_RTC_SLEEP
        int 30h
        dec byte [count]
        jnz .loop

        ;; Close socket and exit
        mov bx, [socket_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
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

        .sock_err:
        mov si, MESSAGE_SOCK_ERR
        mov cx, MESSAGE_SOCK_ERR_LENGTH
        jmp FUNCTION_DIE

resolve_dns:
        ;; Resolve domain to IP via DNS A query
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
        add di, 8
        movzx bx, byte [di+1]
        add di, 2
        add di, bx
        dec cl
        jnz .answer_loop
        jmp .err

        .found_a:
        add di, 10
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
        dns_server_ip db 10, 0, 2, 3
        dns_socket_fd dw 0
        icmp_packet times PING_ICMP_LENGTH db 0
        my_mac times 6 db 0
        ping_seq dw 1
        recv_buffer times 128 db 0
        socket_fd dw 0
        start_ticks dw 0
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
        MESSAGE_SOCK_ERR db `Socket error\n`
        MESSAGE_SOCK_ERR_LENGTH equ $ - MESSAGE_SOCK_ERR
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
