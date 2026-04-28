        [bits 32]
        org 0600h

%include "constants.asm"

main:
        cld

        ;; Require exactly one argument
        mov edi, ARGV
        call FUNCTION_PARSE_ARGV
        cmp ecx, 1
        jne .usage

        ;; Parse target IP (before MAC read clobbers BUFFER)
        mov esi, [ARGV]
        mov edi, target_ip
        call parse_ip
        jc .usage

        ;; Read our MAC (verifies NIC is up)
        mov edi, my_mac
        mov ah, SYS_NET_MAC
        int 30h
        jc .no_nic

        ;; Fill arp_frame: src MAC (offset 6), sender MAC (offset 22).
        ;; 6 bytes is one dword + one word, so movsd then movsw.
        mov esi, my_mac
        mov edi, arp_frame + 6
        movsd
        movsw
        mov esi, my_mac
        mov edi, arp_frame + 22
        movsd
        movsw

        ;; Fill arp_frame: target IP (offset 38) — single 4-byte copy.
        mov esi, target_ip
        mov edi, arp_frame + 38
        movsd

        ;; Open raw Ethernet socket
        mov al, SOCK_RAW
        mov ah, SYS_NET_OPEN
        int 30h
        jc .no_nic
        mov [net_fd], eax

        ;; Send ARP request
        mov ebx, [net_fd]
        mov esi, arp_frame
        mov ecx, 60
        mov ah, SYS_IO_WRITE
        int 30h

        ;; Poll for ARP reply
        mov dword [poll_remaining], 30000
        .poll:
        mov ebx, [net_fd]
        mov edi, BUFFER + 128
        mov ecx, 128
        mov ah, SYS_IO_READ
        int 30h
        jc .poll_next
        test eax, eax
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

        ;; Check sender IP matches our target — single 4-byte compare.
        mov eax, [BUFFER + 128 + 28]
        cmp eax, [target_ip]
        jne .poll_next

        ;; Close socket
        mov ebx, [net_fd]
        mov ah, SYS_IO_CLOSE
        int 30h

        ;; Print "<IP> is at <MAC>\n"
        mov esi, target_ip
        call FUNCTION_PRINT_IP
        mov esi, MESSAGE_IS_AT
        mov ecx, MESSAGE_IS_AT_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov esi, BUFFER + 128 + 22  ; Sender MAC from reply
        call FUNCTION_PRINT_MAC
        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        jmp FUNCTION_EXIT

        .poll_next:
        dec dword [poll_remaining]
        jnz .poll

        ;; Close socket before reporting timeout
        mov ebx, [net_fd]
        mov ah, SYS_IO_CLOSE
        int 30h

        mov esi, MESSAGE_TIMEOUT
        mov ecx, MESSAGE_TIMEOUT_LENGTH
        jmp FUNCTION_DIE

        .no_nic:
        mov esi, MESSAGE_NO_NIC
        mov ecx, MESSAGE_NO_NIC_LENGTH
        jmp FUNCTION_DIE

        .usage:
        mov esi, MESSAGE_USAGE
        mov ecx, MESSAGE_USAGE_LENGTH
        jmp FUNCTION_DIE

        ;; Data
        my_mac times 6 db 0
        net_fd dd 0
        poll_remaining dd 0
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
