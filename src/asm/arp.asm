        org 0600h

%include "constants.asm"

main:
        cld

        ;; Require exactly one argument
        mov di, ARGV
        call FUNCTION_PARSE_ARGV
        cmp cx, 1
        jne .usage

        ;; Parse target IP (before MAC read clobbers BUFFER)
        mov si, [ARGV]
        mov di, target_ip
        call parse_ip
        jc .usage

        ;; Read our MAC (verifies NIC is up)
        mov di, my_mac
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Fill arp_frame: src MAC (offset 6), sender MAC (offset 22)
        mov si, my_mac
        mov di, arp_frame + 6
        movsw
        movsw
        movsw
        mov si, my_mac
        mov di, arp_frame + 22
        movsw
        movsw
        movsw

        ;; Fill arp_frame: target IP (offset 38)
        mov si, target_ip
        mov di, arp_frame + 38
        movsw
        movsw

        ;; Open raw Ethernet socket
        mov ah, SYS_NET_OPEN
        int 30h
        jc .no_nic
        mov [net_fd], ax

        ;; Send ARP request
        mov bx, [net_fd]
        mov si, arp_frame
        mov cx, 60
        mov ah, SYS_IO_WRITE
        int 30h

        ;; Poll for ARP reply
        mov word [poll_remaining], 30000
        .poll:
        mov bx, [net_fd]
        mov di, BUFFER + 128
        mov cx, 128
        mov ah, SYS_IO_READ
        int 30h
        jc .poll_next
        test ax, ax
        jz .poll_next

        ;; Check EtherType = 0x0806 (ARP)
        cmp byte [BUFFER + 128 + 12], 08h
        jne .poll_next
        cmp byte [BUFFER + 128 + 13], 06h
        jne .poll_next

        ;; Check opcode = 0x0002 (ARP reply)
        cmp byte [BUFFER + 128 + 20], 00h
        jne .poll_next
        cmp byte [BUFFER + 128 + 21], 02h
        jne .poll_next

        ;; Check sender IP matches our target
        mov ax, [BUFFER + 128 + 28]
        cmp ax, [target_ip]
        jne .poll_next
        mov ax, [BUFFER + 128 + 30]
        cmp ax, [target_ip + 2]
        jne .poll_next

        ;; Close socket
        mov bx, [net_fd]
        mov ah, SYS_IO_CLOSE
        int 30h

        ;; Print "<IP> is at <MAC>\n"
        mov si, target_ip
        call FUNCTION_PRINT_IP
        mov si, MESSAGE_IS_AT
        mov cx, MESSAGE_IS_AT_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov si, BUFFER + 128 + 22  ; Sender MAC from reply
        call FUNCTION_PRINT_MAC
        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        jmp FUNCTION_EXIT

        .poll_next:
        dec word [poll_remaining]
        jnz .poll

        ;; Close socket before reporting timeout
        mov bx, [net_fd]
        mov ah, SYS_IO_CLOSE
        int 30h

        mov si, MESSAGE_TIMEOUT
        mov cx, MESSAGE_TIMEOUT_LENGTH
        jmp FUNCTION_DIE

        .no_nic:
        mov si, MESSAGE_NO_NIC
        mov cx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .usage:
        mov si, MESSAGE_USAGE
        mov cx, MESSAGE_USAGE_LENGTH
        jmp FUNCTION_DIE

        ;; Data
        my_mac times 6 db 0
        net_fd dw 0
        poll_remaining dw 0
        target_ip times 4 db 0

        MESSAGE_IS_AT db ` is at `
        MESSAGE_IS_AT_LENGTH equ $ - MESSAGE_IS_AT
        MESSAGE_NO_NIC db `No NIC found\n`
        MESSAGE_NO_NIC_LENGTH equ $ - MESSAGE_NO_NIC
        MESSAGE_TIMEOUT db `ARP timeout\n`
        MESSAGE_TIMEOUT_LENGTH equ $ - MESSAGE_TIMEOUT
        MESSAGE_USAGE db `usage: arp <ip>\n`
        MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE

%include "arp_frame.asm"
%include "parse_ip.asm"
