        [bits 32]
        org 0600h

%include "constants.asm"

%assign rr_name_buf BUFFER
%assign cname_buf (BUFFER + 128)

main:
        cld

        ;; Init NIC
        mov edi, my_mac
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Require exactly one argument
        mov edi, ARGV
        call FUNCTION_PARSE_ARGV
        cmp ecx, 1
        jne .no_arg
        mov ebx, [ARGV]
        mov [domain_arg], ebx
        mov byte [found_a], 0

        ;; Print "Querying <domain>...\n"
        mov esi, MESSAGE_QUERY
        mov ecx, MESSAGE_QUERY_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov edi, [domain_arg]  ; FUNCTION_WRITE_STDOUT clobbered ebx
        call FUNCTION_PRINT_STRING
        mov esi, MESSAGE_ELLIPSIS
        mov ecx, MESSAGE_ELLIPSIS_LENGTH
        call FUNCTION_WRITE_STDOUT

        ;; Send DNS A query and position EDI at first answer record
        mov esi, [domain_arg]
        call dns_query
        jc .dns_err
        test al, al
        jz .no_answer
        mov [ans_count], al

        ;; Walk answer records, printing each CNAME and all A records
        .answer_loop:
        mov [rr_name_ptr], edi ; Save RR name start for decode_domain

        ;; Skip answer name (compressed pointer or labels)
        cmp byte [edi], 0C0h
        jb .skip_ans_labels
        add edi, 2
        jmp .at_rr_type
        .skip_ans_labels:
        cmp byte [edi], 0
        je .ans_labels_done
        movzx ebx, byte [edi]
        inc edi
        add edi, ebx
        jmp .skip_ans_labels
        .ans_labels_done:
        inc edi

        .at_rr_type:
        ;; TYPE is big-endian; check for A (0x0001) and CNAME (0x0005)
        cmp word [edi], 0100h
        je .found_a
        cmp word [edi], 0500h
        je .found_cname

        ;; Unknown type: skip TYPE(2)+CLASS(2)+TTL(4) = 8 bytes to RDLENGTH
        add edi, 8
        movzx ebx, byte [edi+1]; RDLENGTH low byte (big-endian, high byte assumed 0)
        add edi, 2
        add edi, ebx           ; Skip rdata
        dec byte [ans_count]
        jnz .answer_loop
        cmp byte [found_a], 0
        je .no_answer
        jmp FUNCTION_EXIT

        .found_cname:
        ;; Print "<rr_name> is a CNAME for <target>\n"
        ;; Advance EDI to rdata: TYPE(2)+CLASS(2)+TTL(4)+RDLENGTH(2) = 10 bytes
        add edi, 8             ; Skip TYPE(2)+CLASS(2)+TTL(4) to reach RDLENGTH
        movzx ebx, byte [edi+1]; RDLENGTH low byte
        add edi, 2             ; EDI = rdata start (CNAME target in wire format)
        mov eax, edi
        add eax, ebx
        push eax               ; Push next-RR position
        ;; Decode RR name into rr_name_buf
        push edi
        mov esi, [rr_name_ptr]
        mov edi, rr_name_buf
        call decode_domain
        pop edi
        ;; Decode CNAME target into cname_buf
        mov esi, edi
        mov edi, cname_buf
        call decode_domain
        ;; Print "<rr_name> is a CNAME for <target>\n"
        mov edi, rr_name_buf
        call FUNCTION_PRINT_STRING
        mov esi, MESSAGE_CNAME
        mov ecx, MESSAGE_CNAME_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov edi, cname_buf
        call FUNCTION_PRINT_STRING
        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        pop edi                ; Restore EDI to next RR position
        dec byte [ans_count]
        jnz .answer_loop
        cmp byte [found_a], 0
        je .no_answer
        jmp FUNCTION_EXIT

        .found_a:
        ;; Print "<rr_name> is at <ip>\n"
        ;; Decode RR name into rr_name_buf
        push edi
        mov esi, [rr_name_ptr]
        mov edi, rr_name_buf
        call decode_domain
        pop edi
        add edi, 10            ; Skip TYPE(2)+CLASS(2)+TTL(4)+RDLENGTH(2) to rdata
        push edi               ; Save rdata pointer (IP address)
        mov edi, rr_name_buf
        call FUNCTION_PRINT_STRING
        mov esi, MESSAGE_IS_AT
        mov ecx, MESSAGE_IS_AT_LENGTH
        call FUNCTION_WRITE_STDOUT
        pop esi                ; ESI = IP address pointer
        push esi
        call FUNCTION_PRINT_IP
        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        mov byte [found_a], 1
        pop edi
        add edi, 4             ; Advance past 4-byte IP to next RR
        dec byte [ans_count]
        jnz .answer_loop
        jmp FUNCTION_EXIT

        .no_nic:
        mov esi, MESSAGE_NO_NIC
        mov ecx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .no_arg:
        mov esi, MESSAGE_USAGE
        mov ecx, MESSAGE_USAGE_LENGTH
        jmp FUNCTION_DIE

        .dns_err:
        mov esi, MESSAGE_DNS_ERROR
        mov ecx, MESSAGE_DNS_ERROR_LENGTH
        jmp FUNCTION_DIE

        .no_answer:
        mov esi, MESSAGE_NO_ANSWER
        mov ecx, MESSAGE_NO_ANSWER_LENGTH
        jmp FUNCTION_DIE

decode_domain:
        ;; Decode DNS wire-format name to null-terminated dotted string
        ;; Input: ESI = pointer to wire-format name
        ;;        EDI = output buffer
        ;;        [dns_base] = start of DNS message (for pointer resolution)
        ;; Output: null-terminated string written at EDI
        ;; Clobbers: EAX, EBX, ECX
        xor bh, bh             ; BH = labels-written count (0 = first label)
        .loop:
        mov al, [esi]
        test al, al
        jz .done               ; Null label = end of name
        cmp al, 0C0h
        jae .pointer           ; Compression pointer
        ;; Regular label: AL = length
        movzx ecx, al
        inc esi
        test bh, bh
        jz .write_label        ; No dot before first label
        mov al, '.'
        stosb
        .write_label:
        inc bh
        rep movsb
        jmp .loop
        .pointer:
        ;; Offset = ((al & 0x3F) << 8) | [esi+1]
        and al, 3Fh
        mov ah, [esi+1]
        xchg al, ah            ; AX = (high_6bits << 8) | low_8bits
        movzx eax, ax
        add eax, [dns_base]
        mov esi, eax           ; Follow pointer
        jmp .loop
        .done:
        xor al, al
        stosb
        ret

        ;; Data
        ans_count db 0
        dns_base dd 0
        dns_server_ip db 10, 0, 2, 3
        dns_socket_fd dd 0
        domain_arg dd 0
        found_a db 0
        my_mac times 6 db 0
        rr_name_ptr dd 0

        MESSAGE_CNAME db ` is a CNAME for `
        MESSAGE_CNAME_LENGTH equ $ - MESSAGE_CNAME
        MESSAGE_DNS_ERROR db `DNS query failed\n`
        MESSAGE_DNS_ERROR_LENGTH equ $ - MESSAGE_DNS_ERROR
        MESSAGE_ELLIPSIS db `...\n`
        MESSAGE_ELLIPSIS_LENGTH equ $ - MESSAGE_ELLIPSIS
        MESSAGE_IS_AT db ` is at `
        MESSAGE_IS_AT_LENGTH equ $ - MESSAGE_IS_AT
        MESSAGE_NO_ANSWER db `No answer in DNS response\n`
        MESSAGE_NO_ANSWER_LENGTH equ $ - MESSAGE_NO_ANSWER
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC
        MESSAGE_QUERY db `Querying `
        MESSAGE_QUERY_LENGTH equ $ - MESSAGE_QUERY
        MESSAGE_USAGE db `Usage: dns <domain>\n`
        MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE

%include "dns_query.asm"
%include "encode_domain.asm"
