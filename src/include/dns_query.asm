        ;; Legacy scratch slot at phys 0xF000.  Defined here (and in the
        ;; archive/ pure-asm program sources) rather than in
        ;; src/include/constants.asm: per-program PDs no longer alias the
        ;; low 1 MB so the constant has no use in active programs.
        %assign SECTOR_BUFFER 0F000h

dns_query:
        ;; Send DNS A query and return pointer to first answer record
        ;; Input: ESI = null-terminated domain string
        ;; Output: EDI = pointer to first answer record
        ;;         AL = ANCOUNT (may be 0; check before walking answers)
        ;;         CF set on send/receive error
        ;; Caller must define: dns_base (dd), dns_server_ip (4 db),
        ;;                     dns_socket_fd (dd)
        push ebx
        push ecx
        push edx
        push esi
        push ebp

        ;; Build query: fixed header + QNAME + QTYPE + QCLASS
        mov edi, SECTOR_BUFFER
        push esi
        mov esi, .hdr_template
        mov ecx, 12
        rep movsb
        pop esi
        call encode_domain
        jc .err
        mov ax, 0100h          ; QTYPE: A (big-endian 0x0001)
        stosw
        mov ax, 0100h          ; QCLASS: IN (big-endian 0x0001)
        stosw
        mov ecx, edi
        sub ecx, SECTOR_BUFFER ; ECX = total query length

        ;; Open UDP socket
        mov al, SOCK_DGRAM
        mov dl, IPPROTO_UDP
        mov ah, SYS_NET_OPEN
        int 30h
        jc .err
        mov [dns_socket_fd], eax

        ;; Send UDP query to DNS server
        mov ebx, [dns_socket_fd]
        mov esi, SECTOR_BUFFER
        mov edi, dns_server_ip
        mov dx, 1024           ; Source port
        mov ebp, 53            ; Dest port (DNS)
        mov ah, SYS_NET_SENDTO
        int 30h
        jc .err_close

        ;; Poll for response
        mov esi, 0FFFFh
        .poll:
        mov ebx, [dns_socket_fd]
        mov edi, SECTOR_BUFFER ; Receive into separate buffer
        mov ecx, 512
        mov dx, 1024           ; Filter on our source port
        mov ah, SYS_NET_RECVFROM
        int 30h
        test eax, eax
        jnz .got_response
        dec esi
        jnz .poll
        jmp .err_close

        .got_response:
        ;; Close socket before processing response
        push eax
        mov ebx, [dns_socket_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        pop eax

        ;; Response is in SECTOR_BUFFER; save base for compression pointers
        mov dword [dns_base], SECTOR_BUFFER
        mov edi, SECTOR_BUFFER
        mov al, [edi+7]        ; ANCOUNT low byte (big-endian offset 6-7)

        ;; Skip header (12 bytes) + question QNAME + QTYPE(2) + QCLASS(2)
        add edi, 12
        .skip_qname:
        cmp byte [edi], 0
        je .qname_done
        movzx ebx, byte [edi]
        inc edi
        add edi, ebx
        jmp .skip_qname
        .qname_done:
        inc edi
        add edi, 4
        ;; EDI = first answer record; AL = ANCOUNT
        clc
        jmp .done
        .err_close:
        mov ebx, [dns_socket_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        .err:
        stc
        .done:
        pop ebp
        pop esi
        pop edx
        pop ecx
        pop ebx
        ret

        .hdr_template:
        db 00h, 01h           ; Transaction ID
        db 01h, 00h           ; Flags: standard query, recursion desired
        db 00h, 01h           ; QDCOUNT: 1
        db 00h, 00h           ; ANCOUNT: 0
        db 00h, 00h           ; NSCOUNT: 0
        db 00h, 00h           ; ARCOUNT: 0
