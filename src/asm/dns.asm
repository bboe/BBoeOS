        org 0600h

%include "constants.asm"

%assign rr_name_buf BUFFER
%assign cname_buf (BUFFER + 128)

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
        mov bx, [ARGV]
        mov [domain_arg], bx
        mov byte [found_a], 0

        ;; Print "Querying <domain>...\n"
        mov si, MESSAGE_QUERY
        mov cx, MESSAGE_QUERY_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov di, bx
        call FUNCTION_PRINT_STRING
        mov si, MESSAGE_ELLIPSIS
        mov cx, MESSAGE_ELLIPSIS_LENGTH
        call FUNCTION_WRITE_STDOUT

        ;; Send DNS A query and position DI at first answer record
        mov si, [domain_arg]
        call dns_query
        jc .dns_err
        test al, al
        jz .no_answer
        mov [ans_count], al

        ;; Walk answer records, printing each CNAME and all A records
        .answer_loop:
        mov [rr_name_ptr], di  ; Save RR name start for decode_domain

        ;; Skip answer name (compressed pointer or labels)
        cmp byte [di], 0C0h
        jb .skip_ans_labels
        add di, 2
        jmp .at_rr_type
        .skip_ans_labels:
        cmp byte [di], 0
        je .ans_labels_done
        movzx bx, byte [di]
        inc di
        add di, bx
        jmp .skip_ans_labels
        .ans_labels_done:
        inc di

        .at_rr_type:
        ;; TYPE is big-endian; check for A (0x0001) and CNAME (0x0005)
        cmp word [di], 0100h
        je .found_a
        cmp word [di], 0500h
        je .found_cname

        ;; Unknown type: skip TYPE(2)+CLASS(2)+TTL(4) = 8 bytes to RDLENGTH
        add di, 8
        movzx bx, byte [di+1]  ; RDLENGTH low byte (big-endian, high byte assumed 0)
        add di, 2
        add di, bx             ; Skip rdata
        dec byte [ans_count]
        jnz .answer_loop
        cmp byte [found_a], 0
        je .no_answer
        jmp FUNCTION_EXIT

        .found_cname:
        ;; Print "<rr_name> is a CNAME for <target>\n"
        ;; Advance DI to rdata: TYPE(2)+CLASS(2)+TTL(4)+RDLENGTH(2) = 10 bytes
        add di, 8              ; Skip TYPE(2)+CLASS(2)+TTL(4) to reach RDLENGTH
        movzx bx, byte [di+1]  ; RDLENGTH low byte
        add di, 2              ; DI = rdata start (CNAME target in wire format)
        mov ax, di
        add ax, bx
        push ax                ; Push next-RR position
        ;; Decode RR name into rr_name_buf
        push di
        mov si, [rr_name_ptr]
        mov di, rr_name_buf
        call decode_domain
        pop di
        ;; Decode CNAME target into cname_buf
        mov si, di
        mov di, cname_buf
        call decode_domain
        ;; Print "<rr_name> is a CNAME for <target>\n"
        mov di, rr_name_buf
        call FUNCTION_PRINT_STRING
        mov si, MESSAGE_CNAME
        mov cx, MESSAGE_CNAME_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov di, cname_buf
        call FUNCTION_PRINT_STRING
        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        pop di                 ; Restore DI to next RR position
        dec byte [ans_count]
        jnz .answer_loop
        cmp byte [found_a], 0
        je .no_answer
        jmp FUNCTION_EXIT

        .found_a:
        ;; Print "<rr_name> is at <ip>\n"
        ;; Decode RR name into rr_name_buf
        push di
        mov si, [rr_name_ptr]
        mov di, rr_name_buf
        call decode_domain
        pop di
        add di, 10             ; Skip TYPE(2)+CLASS(2)+TTL(4)+RDLENGTH(2) to rdata
        push di                ; Save rdata pointer (IP address)
        mov di, rr_name_buf
        call FUNCTION_PRINT_STRING
        mov si, MESSAGE_IS_AT
        mov cx, MESSAGE_IS_AT_LENGTH
        call FUNCTION_WRITE_STDOUT
        pop si                 ; SI = IP address pointer
        push si
        call FUNCTION_PRINT_IP
        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        mov byte [found_a], 1
        pop di
        add di, 4              ; Advance past 4-byte IP to next RR
        dec byte [ans_count]
        jnz .answer_loop
        jmp FUNCTION_EXIT

        .no_nic:
        mov si, MESSAGE_NO_NIC
        mov cx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .no_arg:
        mov si, MESSAGE_USAGE
        mov cx, MESSAGE_USAGE_LENGTH
        jmp FUNCTION_DIE

        .dns_err:
        mov si, MESSAGE_DNS_ERROR
        mov cx, MESSAGE_DNS_ERROR_LENGTH
        jmp FUNCTION_DIE

        .no_answer:
        mov si, MESSAGE_NO_ANSWER
        mov cx, MESSAGE_NO_ANSWER_LENGTH
        jmp FUNCTION_DIE

decode_domain:
        ;; Decode DNS wire-format name to null-terminated dotted string
        ;; Input: SI = pointer to wire-format name
        ;;        DI = output buffer
        ;;        [dns_base] = start of DNS message (for pointer resolution)
        ;; Output: null-terminated string written at DI
        ;; Clobbers: AX, BX, CX
        xor bh, bh             ; BH = labels-written count (0 = first label)
        .loop:
        mov al, [si]
        test al, al
        jz .done               ; Null label = end of name
        cmp al, 0C0h
        jae .pointer           ; Compression pointer
        ;; Regular label: AL = length
        movzx cx, al
        inc si
        test bh, bh
        jz .write_label        ; No dot before first label
        mov al, '.'
        stosb
        .write_label:
        inc bh
        rep movsb
        jmp .loop
        .pointer:
        ;; Offset = ((al & 0x3F) << 8) | [si+1]
        and al, 3Fh
        mov ah, [si+1]
        xchg al, ah            ; AX = (high_6bits << 8) | low_8bits
        add ax, [dns_base]
        mov si, ax             ; Follow pointer
        jmp .loop
        .done:
        xor al, al
        stosb
        ret

        ;; Data
        ans_count db 0
        dns_base dw 0
        dns_server_ip db 10, 0, 2, 3
        dns_socket_fd dw 0
        domain_arg dw 0
        found_a db 0
        my_mac times 6 db 0
        rr_name_ptr dw 0

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
