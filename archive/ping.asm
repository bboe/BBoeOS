        [bits 32]
        org 0600h

%include "constants.asm"

%define PING_COUNT 4
%define PING_ICMP_LENGTH 16

main:
        cld

        ;; Verify NIC by reading MAC
        mov edi, my_mac
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Require exactly one argument
        mov edi, ARGV
        call FUNCTION_PARSE_ARGV
        cmp ecx, 1
        jne .no_arg

        ;; Try dotted-decimal IP; fall back to DNS
        mov esi, [ARGV]
        mov edi, target_ip
        call parse_ip
        jnc .have_ip
        mov esi, [ARGV]
        call resolve_dns
        jc .resolve_err

        .have_ip:
        ;; Print "Pinging X.X.X.X...\n"
        mov esi, MESSAGE_PINGING
        mov ecx, MESSAGE_PINGING_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov esi, target_ip
        call FUNCTION_PRINT_IP
        mov esi, MESSAGE_ELLIPSIS
        mov ecx, MESSAGE_ELLIPSIS_LENGTH
        call FUNCTION_WRITE_STDOUT

        ;; Open ICMP socket (Linux-style SOCK_DGRAM + IPPROTO_ICMP)
        mov al, SOCK_DGRAM
        mov dl, IPPROTO_ICMP
        mov ah, SYS_NET_OPEN
        int 30h
        jc .sock_err
        mov [socket_fd], eax

        mov byte [count], PING_COUNT
        .loop:
        ;; Build ICMP echo request: 8-byte header + 8-byte payload
        mov edi, icmp_packet
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
        xor eax, eax           ; 8 bytes of zero payload (2× dword)
        stosd
        stosd

        ;; Inline 1's-complement checksum over the 16 ICMP bytes
        mov esi, icmp_packet
        mov ecx, PING_ICMP_LENGTH / 2
        xor bx, bx
        .cksum:
        lodsw
        add bx, ax
        adc bx, 0
        loop .cksum
        not bx
        mov [icmp_packet + 2], bx

        ;; Record start time (full 32-bit ms via SYS_RTC_MILLIS).
        ;; Pmode has no real-mode IVT, so the 16-bit version's int 1Ah
        ;; is unavailable; SYS_RTC_MILLIS returns DX:AX which we
        ;; recompose into EAX.
        mov ah, SYS_RTC_MILLIS
        int 30h
        shl edx, 16
        and eax, 0xFFFF
        or eax, edx
        mov [start_ms], eax

        ;; sendto(fd, icmp_packet, 16, target_ip) — ICMP ignores ports
        mov ebx, [socket_fd]
        mov esi, icmp_packet
        mov ecx, PING_ICMP_LENGTH
        mov edi, target_ip
        mov ah, SYS_NET_SENDTO
        int 30h
        jc .timeout

        ;; Poll recvfrom until we see an ICMP echo reply
        mov ebp, 0FFFFh
        .poll:
        mov ebx, [socket_fd]
        mov edi, recv_buffer
        mov ecx, 128
        xor dx, dx
        mov ah, SYS_NET_RECVFROM
        int 30h
        test eax, eax
        jz .poll_next
        cmp byte [recv_buffer], 0       ; ICMP type 0 = echo reply
        jne .poll_next
        ;; Got reply — RTT = now_ms - start_ms (full 32-bit)
        mov ah, SYS_RTC_MILLIS
        int 30h
        shl edx, 16
        and eax, 0xFFFF
        or eax, edx
        sub eax, [start_ms]
        jmp .print_reply
        .poll_next:
        dec ebp
        jnz .poll
        jmp .timeout

        .print_reply:
        push eax                ; Save ms delta
        mov esi, MESSAGE_REPLY
        mov ecx, MESSAGE_REPLY_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov esi, target_ip
        call FUNCTION_PRINT_IP
        ;; printf(": time=%d ms\n", duration_ms) — cdecl, args R-to-L
        push dword MESSAGE_TIME_FMT
        call FUNCTION_PRINTF
        add esp, 8              ; pop fmt + duration
        jmp .next

        .timeout:
        mov esi, MESSAGE_TIMEOUT
        mov ecx, MESSAGE_TIMEOUT_LENGTH
        call FUNCTION_WRITE_STDOUT

        .next:
        ;; Sleep ~1 second between pings
        mov ecx, 1000
        mov ah, SYS_RTC_SLEEP
        int 30h
        dec byte [count]
        jnz .loop

        ;; Close socket and exit
        mov ebx, [socket_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        jmp FUNCTION_EXIT

        .no_arg:
        mov esi, MESSAGE_USAGE
        mov ecx, MESSAGE_USAGE_LENGTH
        jmp FUNCTION_DIE

        .no_nic:
        mov esi, MESSAGE_NO_NIC
        mov ecx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .resolve_err:
        mov esi, MESSAGE_RESOLVE_ERROR
        mov ecx, MESSAGE_RESOLVE_ERROR_LENGTH
        jmp FUNCTION_DIE

        .sock_err:
        mov esi, MESSAGE_SOCK_ERR
        mov ecx, MESSAGE_SOCK_ERR_LENGTH
        jmp FUNCTION_DIE

resolve_dns:
        ;; Resolve domain to IP via DNS A query
        ;; Input: ESI = null-terminated domain string
        ;; Output: target_ip filled with first A record, CF set on error
        push ebx
        push ecx
        push edi

        call dns_query
        jc .err
        test al, al
        jz .err
        mov cl, al             ; CL = answer count

        .answer_loop:
        cmp byte [edi], 0C0h
        jb .skip_labels
        add edi, 2
        jmp .check_type
        .skip_labels:
        cmp byte [edi], 0
        je .labels_done
        movzx ebx, byte [edi]
        inc edi
        add edi, ebx
        jmp .skip_labels
        .labels_done:
        inc edi

        .check_type:
        cmp word [edi], 0100h  ; A record = 0x0001 big-endian
        je .found_a
        add edi, 8
        movzx ebx, byte [edi+1]
        add edi, 2
        add edi, ebx
        dec cl
        jnz .answer_loop
        jmp .err

        .found_a:
        add edi, 10
        mov eax, [edi]         ; 4-byte IP — single dword copy
        mov [target_ip], eax
        clc
        jmp .done
        .err:
        stc
        .done:
        pop edi
        pop ecx
        pop ebx
        ret

        ;; Data
        count db 0
        dns_base dd 0
        dns_server_ip db 10, 0, 2, 3
        dns_socket_fd dd 0
        icmp_packet times PING_ICMP_LENGTH db 0
        my_mac times 6 db 0
        ping_seq dw 1
        recv_buffer times 128 db 0
        socket_fd dd 0
        start_ms dd 0
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
        MESSAGE_TIME_FMT db `: time=%d ms\n\0`
        MESSAGE_TIMEOUT db `Request timed out\n`
        MESSAGE_TIMEOUT_LENGTH equ $ - MESSAGE_TIMEOUT
        MESSAGE_USAGE db `Usage: ping <ip|hostname>\n`
        MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE

%include "dns_query.asm"
%include "encode_domain.asm"
%include "parse_ip.asm"
