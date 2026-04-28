        [bits 32]
        org 0600h

%include "constants.asm"

main:
        cld

        ;; Require exactly two arguments
        mov edi, ARGV
        call FUNCTION_PARSE_ARGV
        cmp ecx, 2
        jne .usage

        mov eax, [ARGV+4]
        mov [dest_name], eax

        ;; Open source file for reading
        mov esi, [ARGV]
        mov al, O_RDONLY
        mov ah, SYS_IO_OPEN
        int 30h
        jc .not_found
        mov [src_fd], eax

        ;; Get source file's permission flags via fstat
        mov ebx, eax
        mov ah, SYS_IO_FSTAT
        int 30h
        mov [src_mode], al

        ;; Open dest file for writing (create new, with source permissions)
        mov esi, [dest_name]
        mov al, O_WRONLY + O_CREAT
        mov dl, [src_mode]
        mov ah, SYS_IO_OPEN
        int 30h
        jc .dest_err
        mov [dest_fd], eax

        ;; Copy loop: read from src into buffer, write buffer to dest
.copy_loop:
        mov ebx, [src_fd]
        mov edi, copy_buf
        mov ecx, 512
        mov ah, SYS_IO_READ
        int 30h
        test eax, eax
        jz .copy_done           ; EOF
        cmp eax, -1
        je .disk_err

        ;; Write the bytes we just read
        mov ecx, eax
        mov ebx, [dest_fd]
        mov esi, copy_buf
        mov ah, SYS_IO_WRITE
        int 30h
        cmp eax, -1
        je .disk_err
        jmp .copy_loop

.copy_done:
        mov ebx, [dest_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        mov ebx, [src_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        jmp FUNCTION_EXIT

.dest_err:
        ;; Close src before reporting error
        mov ebx, [src_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        mov esi, MESSAGE_EXISTS
        mov ecx, MESSAGE_EXISTS_LENGTH
        jmp .die

.disk_err:
        mov ebx, [dest_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        mov ebx, [src_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        mov esi, MESSAGE_DISK_ERROR
        mov ecx, MESSAGE_DISK_ERROR_LENGTH
        jmp .die

.not_found:
        mov esi, MESSAGE_NOT_FOUND
        mov ecx, MESSAGE_NOT_FOUND_LENGTH
        jmp .die

.usage:
        mov esi, MESSAGE_USAGE
        mov ecx, MESSAGE_USAGE_LENGTH

.die:
        jmp FUNCTION_DIE

;; Variables
dest_fd   dd 0
dest_name dd 0
src_fd    dd 0
src_mode db 0

;; Strings
MESSAGE_DISK_ERROR  db `Disk error\n`
MESSAGE_DISK_ERROR_LENGTH equ $ - MESSAGE_DISK_ERROR
MESSAGE_EXISTS    db `File already exists\n`
MESSAGE_EXISTS_LENGTH equ $ - MESSAGE_EXISTS
MESSAGE_NOT_FOUND db `File not found\n`
MESSAGE_NOT_FOUND_LENGTH equ $ - MESSAGE_NOT_FOUND
MESSAGE_USAGE     db `Usage: cp <srcname> <destname>\n`
MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE


;; Copy buffer (512 bytes, right after code+data)
copy_buf:
