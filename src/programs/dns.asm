        org 6000h

%include "constants.asm"

main:
        cld

        ;; Init NIC
        mov di, my_mac
        mov ah, SYS_NET_INIT
        int 30h
        jc .no_nic

        ;; Require domain argument
        mov bx, [EXEC_ARG]
        test bx, bx
        jz .no_arg
        mov [domain_arg], bx

        ;; Print "Querying <domain>...\n"
        mov si, MSG_QUERY
        mov ah, SYS_IO_PUTS
        int 30h
        mov si, bx
        mov ah, SYS_IO_PUTS
        int 30h
        mov si, MSG_ELLIPSIS
        mov ah, SYS_IO_PUTS
        int 30h

        ;; Build DNS query: fixed header + encoded QNAME + QTYPE + QCLASS
        mov di, dns_query_buf
        mov si, dns_header
        mov cx, 12
        rep movsb              ; Copy 12-byte header
        mov si, [domain_arg]
        call encode_domain     ; Append QNAME
        jc .no_arg
        mov ax, 0100h          ; QTYPE: A (big-endian 0x0001)
        stosw
        mov ax, 0100h          ; QCLASS: IN (big-endian 0x0001)
        stosw
        mov cx, di
        sub cx, dns_query_buf  ; CX = total query length

        ;; Send DNS query
        mov bx, dns_server
        mov di, 1024           ; Source port
        mov dx, 53             ; Dest port (DNS)
        mov si, dns_query_buf
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
        mov al, [di+7]
        mov [ans_count], al    ; Save ANCOUNT (low byte sufficient)

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

        ;; Loop through answer records looking for TYPE A (0x0001)
        .answer_loop:
        ;; Skip answer name (compressed pointer or labels)
        cmp byte [di], 0C0h
        jb .skip_ans_labels
        add di, 2              ; Compressed pointer = 2 bytes
        jmp .check_type
        .skip_ans_labels:
        cmp byte [di], 0
        je .ans_labels_done
        movzx bx, byte [di]
        inc di
        add di, bx
        jmp .skip_ans_labels
        .ans_labels_done:
        inc di

        .check_type:
        ;; TYPE is big-endian; A record = 0x0001 = word 0x0100 little-endian
        cmp word [di], 0100h
        je .found_a
        ;; Not A: skip CLASS(2) + TTL(4) = 6, read RDLENGTH(2), skip rdata
        add di, 6
        movzx bx, byte [di+1]  ; RDLENGTH low byte (high byte is 0 for normal records)
        add di, 2
        add di, bx             ; Skip rdata
        dec byte [ans_count]
        jnz .answer_loop
        jmp .no_answer

        .found_a:
        ;; TYPE(2) + CLASS(2) + TTL(4) + RDLENGTH(2) = 10 bytes before rdata
        add di, 10

        ;; Print "<domain> is at <ip>\n"
        mov si, [domain_arg]
        mov ah, SYS_IO_PUTS
        int 30h
        mov si, MSG_IS_AT
        mov ah, SYS_IO_PUTS
        int 30h
        mov si, di
        call print_ip
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

        .no_arg:
        mov si, MSG_USAGE
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

encode_domain:
        ;; Encode null-terminated domain string into DNS QNAME format
        ;; Input: SI = domain string, DI = output buffer
        ;; Output: DI advanced past encoded name, CF set on error
        ;; Clobbers: AX, BX, CX
        .label_start:
        mov bx, di             ; BX = position of length byte (fill in later)
        inc di                 ; Skip length byte
        xor cx, cx             ; CX = character count for this label
        .char_loop:
        lodsb
        cmp al, '.'
        je .dot
        test al, al
        jz .end
        stosb
        inc cx
        jmp .char_loop
        .dot:
        test cx, cx
        jz .error              ; Empty label (leading or consecutive dots)
        mov [bx], cl           ; Fill in length byte
        jmp .label_start
        .end:
        test cx, cx
        jz .error              ; Empty input or trailing dot
        mov [bx], cl           ; Fill in length byte
        xor al, al
        stosb                  ; Null terminator
        clc
        ret
        .error:
        stc
        ret

        ;; Data
        ans_count db 0
        dns_header:
        db 00h, 01h           ; Transaction ID
        db 01h, 00h           ; Flags: standard query, recursion desired
        db 00h, 01h           ; QDCOUNT: 1
        db 00h, 00h           ; ANCOUNT: 0
        db 00h, 00h           ; NSCOUNT: 0
        db 00h, 00h           ; ARCOUNT: 0
        dns_query_buf times 300 db 0
        dns_server db 10, 0, 2, 3
        domain_arg dw 0
        my_mac times 6 db 0

        MSG_ELLIPSIS db `...\n\0`
        MSG_IS_AT db ` is at \0`
        MSG_NO_ANS db `No answer in DNS response\n\0`
        MSG_NO_NIC db `No NIC found\n\0`
        MSG_QUERY db `Querying \0`
        MSG_SEND_ERR db `Send failed\n\0`
        MSG_TIMEOUT db `DNS timeout\n\0`
        MSG_USAGE db `Usage: dns <domain>\n\0`

%include "print_byte_dec.asm"
%include "print_ip.asm"
