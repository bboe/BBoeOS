dns_query:
        ;; Send DNS A query and return pointer to first answer record
        ;; Input: SI = null-terminated domain string
        ;; Output: DI = pointer to first answer record
        ;;         AL = ANCOUNT (may be 0; check before walking answers)
        ;;         CF set on send/receive error
        ;; Caller must define: dns_base (dw), dns_server_ip (4 db),
        ;;                     dns_socket_fd (dw)
        push bx
        push cx
        push dx
        push si
        push bp

        ;; Build query: fixed header + QNAME + QTYPE + QCLASS
        mov di, SECTOR_BUFFER
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
        sub cx, SECTOR_BUFFER  ; CX = total query length

        ;; Open UDP socket
        mov al, SOCK_DGRAM
        mov dl, IPPROTO_UDP
        mov ah, SYS_NET_OPEN
        int 30h
        jc .err
        mov [dns_socket_fd], ax

        ;; Send UDP query to DNS server
        mov bx, [dns_socket_fd]
        mov si, SECTOR_BUFFER
        mov di, dns_server_ip
        mov dx, 1024           ; Source port
        mov bp, 53             ; Dest port (DNS)
        mov ah, SYS_NET_SENDTO
        int 30h
        jc .err_close

        ;; Poll for response
        mov si, 0FFFFh
        .poll:
        mov bx, [dns_socket_fd]
        mov di, SECTOR_BUFFER  ; Receive into separate buffer
        mov cx, 512
        mov dx, 1024           ; Filter on our source port
        mov ah, SYS_NET_RECVFROM
        int 30h
        test ax, ax
        jnz .got_response
        dec si
        jnz .poll
        jmp .err_close

        .got_response:
        ;; Close socket before processing response
        push ax
        mov bx, [dns_socket_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        pop ax

        ;; Response is in SECTOR_BUFFER; save base for compression pointers
        mov word [dns_base], SECTOR_BUFFER
        mov di, SECTOR_BUFFER
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
        .err_close:
        mov bx, [dns_socket_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        .err:
        stc
        .done:
        pop bp
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
