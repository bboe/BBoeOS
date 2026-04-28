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

        ;; Open file for reading
        mov esi, [ARGV]
        mov al, O_RDONLY
        mov ah, SYS_IO_OPEN
        int 30h
        jc .not_found

        mov ebx, eax            ; EBX = fd

.read_loop:
        mov edi, SECTOR_BUFFER
        mov ecx, 512
        mov ah, SYS_IO_READ
        int 30h
        jc .disk_err
        test eax, eax
        jz .done                ; EOF

        ;; Write EAX bytes from SECTOR_BUFFER
        push ebx                ; Save fd
        mov ecx, eax
        mov esi, SECTOR_BUFFER
        call FUNCTION_WRITE_STDOUT
        pop ebx                 ; Restore fd
        jmp .read_loop

.done:
        mov ah, SYS_IO_CLOSE
        int 30h
        jmp FUNCTION_EXIT

.not_found:
        mov esi, FILE_NOT_FOUND
        mov ecx, FILE_NOT_FOUND_LENGTH
        jmp .output

.disk_err:
        mov ah, SYS_IO_CLOSE
        int 30h
        mov esi, DISK_ERROR
        mov ecx, DISK_ERROR_LENGTH
        jmp .output

.usage:
        mov esi, USAGE
        mov ecx, USAGE_LENGTH

.output:
        jmp FUNCTION_DIE

;; Strings
USAGE        db `Usage: cat <filename>\n`
USAGE_LENGTH equ $ - USAGE

DISK_ERROR db `Disk read error\n\0`
DISK_ERROR_LENGTH equ $ - DISK_ERROR - 1
FILE_NOT_FOUND db `File not found\n\0`
FILE_NOT_FOUND_LENGTH equ $ - FILE_NOT_FOUND - 1
