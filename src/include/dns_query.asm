dns_query:
        ;; Send DNS A query and return pointer to first answer record
        ;; Input: SI = null-terminated domain string
        ;; Output: DI = pointer to first answer record
        ;;         AL = ANCOUNT (may be 0; check before walking answers)
        ;;         CF set on send/receive error
        ;; Caller must define: dns_base (dw), dns_query_buf (300 db), dns_server_ip (4 db)
        push bx
        push cx
        push dx
        push si

        ;; Build query: fixed header + QNAME + QTYPE + QCLASS
        mov di, dns_query_buf
        push si
        mov si, .hdr_template
        mov cx, 12
        rep movsb
        pop si
        call encode_domain
        jc .err
        mov ax, 0100h          ; QTYPE: A (big-endian 0x0001)
        stosw
        mov ax, 0100h          ; QCLASS: IN (big-endian 0x0001)
        stosw
        mov cx, di
        sub cx, dns_query_buf  ; CX = total query length

        ;; Send UDP query to DNS server
        mov bx, dns_server_ip
        mov di, 1024           ; Source port
        mov dx, 53             ; Dest port (DNS)
        mov si, dns_query_buf
        mov ah, SYS_NET_UDP_SEND
        int 30h
        jc .err

        ;; Poll for response
        mov bx, 0FFFFh
        .poll:
        mov ah, SYS_NET_UDP_RECV
        int 30h
        jnc .got_response
        dec bx
        jnz .poll
        jmp .err

        .got_response:
        ;; DI = DNS response payload; save base for compression pointer resolution
        mov [dns_base], di
        mov al, [di+7]         ; ANCOUNT low byte (big-endian offset 6-7)

        ;; Skip header (12 bytes) + question QNAME + QTYPE(2) + QCLASS(2)
        add di, 12
        .skip_qname:
        cmp byte [di], 0
        je .qname_done
        movzx bx, byte [di]
        inc di
        add di, bx
        jmp .skip_qname
        .qname_done:
        inc di
        add di, 4
        ;; DI = first answer record; AL = ANCOUNT
        clc
        jmp .done
        .err:
        stc
        .done:
        pop si
        pop dx
        pop cx
        pop bx
        ret

        .hdr_template:
        db 00h, 01h           ; Transaction ID
        db 01h, 00h           ; Flags: standard query, recursion desired
        db 00h, 01h           ; QDCOUNT: 1
        db 00h, 00h           ; ANCOUNT: 0
        db 00h, 00h           ; NSCOUNT: 0
        db 00h, 00h           ; ARCOUNT: 0
