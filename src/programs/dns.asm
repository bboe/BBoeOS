        org 6000h

%include "constants.asm"

main:
        cld

        ;; Init NIC
        mov di, my_mac
        mov ah, SYS_NET_INIT
        int 30h
        jc .no_nic

        ;; Send DNS query for example.com
        mov si, MSG_QUERY
        mov ah, SYS_IO_PUTS
        int 30h

        mov bx, dns_server
        mov di, 1024           ; Source port
        mov dx, 53             ; Dest port (DNS)
        mov si, dns_query
        mov cx, dns_query_len
        mov ah, SYS_NET_UDP_SEND
        int 30h
        jc .send_err

        ;; Poll for DNS response
        mov bx, 0FFFFh
        .poll:
        mov ah, SYS_NET_UDP_RECV
        int 30h
        jnc .got_response
        dec bx
        jnz .poll

        mov si, MSG_TIMEOUT
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .got_response:
        ;; DI = DNS response payload, CX = length
        ;; Check ANCOUNT at offset 6-7 (big-endian)
        cmp byte [di+7], 0
        je .no_answer

        ;; Skip DNS header (12 bytes)
        add di, 12

        ;; Skip QNAME (length-prefixed labels, null-terminated)
        .skip_qname:
        cmp byte [di], 0
        je .qname_done
        movzx bx, byte [di]   ; Label length
        inc di
        add di, bx             ; Skip label bytes
        jmp .skip_qname
        .qname_done:
        inc di                 ; Skip null terminator
        add di, 4              ; Skip QTYPE + QCLASS

        ;; Now at answer section
        ;; Skip name field (may be compressed pointer 0xC0xx or labels)
        cmp byte [di], 0C0h
        jb .skip_answer_name
        add di, 2              ; Compressed pointer = 2 bytes
        jmp .at_answer_rr
        .skip_answer_name:
        .skip_aname:
        cmp byte [di], 0
        je .aname_done
        movzx bx, byte [di]
        inc di
        add di, bx
        jmp .skip_aname
        .aname_done:
        inc di

        .at_answer_rr:
        ;; type(2) + class(2) + TTL(4) + rdlength(2) = 10 bytes before rdata
        add di, 10

        ;; Print result
        mov si, MSG_RESULT
        mov ah, SYS_IO_PUTS
        int 30h

        ;; Print IP address from rdata (4 bytes at DI)
        mov si, di
        mov cx, 4
        .print_ip:
        lodsb
        call print_byte_dec
        dec cx
        jz .ip_done
        push cx
        mov al, '.'
        mov ah, SYS_IO_PUTC
        int 30h
        pop cx
        jmp .print_ip
        .ip_done:

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

        .send_err:
        mov si, MSG_SEND_ERR
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .no_answer:
        mov si, MSG_NO_ANS
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        ;; Data
        dns_server db 10, 0, 2, 3
        my_mac times 6 db 0

        ;; DNS query for example.com A record
        dns_query:
        db 00h, 01h           ; Transaction ID
        db 01h, 00h           ; Flags: standard query, recursion desired
        db 00h, 01h           ; QDCOUNT: 1
        db 00h, 00h           ; ANCOUNT: 0
        db 00h, 00h           ; NSCOUNT: 0
        db 00h, 00h           ; ARCOUNT: 0
        db 7, 'example'       ; QNAME: example.com
        db 3, 'com'
        db 0                  ; Root label terminator
        db 00h, 01h           ; QTYPE: A
        db 00h, 01h           ; QCLASS: IN
        dns_query_end:
        dns_query_len equ (dns_query_end - dns_query)

        MSG_NO_ANS db `No answer in DNS response\n\0`
        MSG_NO_NIC db `No NIC found\n\0`
        MSG_QUERY db `Querying example.com...\n\0`
        MSG_RESULT db `example.com is at \0`
        MSG_SEND_ERR db `Send failed\n\0`
        MSG_TIMEOUT db `DNS timeout\n\0`

%include "print_byte_dec.asm"
