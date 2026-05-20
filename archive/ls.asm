        [bits 32]
        org 0600h

%include "constants.asm"

%define DT_DIR        4
%define BUFFER_BYTES  4096
%define ARENA_BYTES   2048
%define MAX_ENTRIES   64

;; Stack frame layout (relative to EBP after `mov ebp, esp`):
;;   [ebp + 0]                   names[MAX_ENTRIES] (4 B per slot)
;;   [ebp + NAMES_BYTES]         is_dir[MAX_ENTRIES] (1 B per slot)
;;   [ebp + IS_DIR_OFF + ...]    arena[ARENA_BYTES]
;;   [ebp + BUF_OFF]             getdents_buf[BUFFER_BYTES]
;;   [ebp + FRAME_VARS]          count (dword), arena_used (dword), fd (dword), key_dir (byte)
%define NAMES_BYTES   (MAX_ENTRIES * 4)
%define IS_DIR_OFF    NAMES_BYTES
%define ARENA_OFF     (IS_DIR_OFF + MAX_ENTRIES)
%define BUF_OFF       (ARENA_OFF + ARENA_BYTES)
%define VARS_OFF      (BUF_OFF + BUFFER_BYTES)
%define V_COUNT       (VARS_OFF + 0)
%define V_ARENA_USED  (VARS_OFF + 4)
%define V_FD          (VARS_OFF + 8)
%define V_KEY_DIR     (VARS_OFF + 12)
%define FRAME_TOTAL   (VARS_OFF + 16)

main:
        cld

        ;; Linux-style argv: argc on stack, argv ptrs follow.  Save
        ;; argv[1] (or DOT) into ESI before allocating the frame so we
        ;; don't have to track the post-sub offset.
        pop ecx                                 ; ECX = argc
        cmp ecx, 2
        ja .not_found
        mov esi, DOT
        cmp ecx, 1
        je .frame
        mov esi, [esp + 4]                      ; argv[1]
.frame:
        sub esp, FRAME_TOTAL
        mov ebp, esp                            ; EBP = frame base
        mov dword [ebp + V_COUNT], 0
        mov dword [ebp + V_ARENA_USED], 0

        ;; open(path, O_RDONLY)
        mov al, O_RDONLY
        mov ah, SYS_IO_OPEN
        int 30h
        jc .not_found
        mov [ebp + V_FD], eax

.read_loop:
        mov ebx, [ebp + V_FD]
        lea edi, [ebp + BUF_OFF]
        mov ecx, BUFFER_BYTES
        mov ah, SYS_IO_GETDENTS
        int 30h
        test eax, eax
        jle .read_done                          ; 0 = EOF, negative = error

        ;; Walk records: each is [d_ino:4][d_reclen:2][d_type:1][name:...]
        lea edx, [ebp + BUF_OFF]                ; EDX = cursor
        lea ebx, [edx + eax]                    ; EBX = end-of-records
.record_loop:
        cmp edx, ebx
        jae .read_loop

        ;; names[count] = &arena[arena_used]
        mov eax, [ebp + V_COUNT]
        mov ecx, [ebp + V_ARENA_USED]
        lea edi, [ebp + ARENA_OFF + ecx]
        mov [ebp + eax*4], edi
        ;; is_dir[count] = (d_type == DT_DIR)
        movzx ecx, byte [edx + 6]
        cmp cl, DT_DIR
        sete cl
        mov [ebp + IS_DIR_OFF + eax], cl

        ;; Copy NUL-terminated name from [edx+7] into arena.  EDI
        ;; already points at the arena slot.
        push ebx
        push edx
        lea esi, [edx + 7]
.copy_byte:
        lodsb
        stosb
        test al, al
        jnz .copy_byte
        ;; arena_used = edi - &arena[0]
        lea ecx, [edi - ARENA_OFF]
        sub ecx, ebp
        mov [ebp + V_ARENA_USED], ecx
        pop edx
        pop ebx

        inc dword [ebp + V_COUNT]
        movzx ecx, word [edx + 4]               ; reclen
        add edx, ecx
        jmp .record_loop

.read_done:
        ;; close(fd)
        mov ebx, [ebp + V_FD]
        mov ah, SYS_IO_CLOSE
        int 30h

.sort:
        ;; Insertion sort.  i in EBX, j in EDX.
        mov eax, [ebp + V_COUNT]
        cmp eax, 2
        jb .print
        mov ebx, 1
.sort_outer:
        cmp ebx, [ebp + V_COUNT]
        jae .print
        mov esi, [ebp + ebx*4]                  ; key_name = names[i]
        mov al, [ebp + IS_DIR_OFF + ebx]
        mov [ebp + V_KEY_DIR], al
        mov edx, ebx
        dec edx
.sort_inner:
        test edx, edx
        js .insert
        push ebx
        push edx
        push esi
        mov edi, esi                            ; b = key
        mov esi, [ebp + edx*4]                  ; a = names[j]
        ;; Match ls.c's libbboeos call: push args R-to-L (cdecl),
        ;; indirect through FUNCTION_STRCMP_PTR, pop args.
        push edi
        push esi
        call [FUNCTION_STRCMP_PTR]
        add esp, 8
        pop esi
        pop edx
        pop ebx
        test eax, eax
        jle .insert
        ;; names[j+1] = names[j]; is_dir[j+1] = is_dir[j]
        mov edi, [ebp + edx*4]
        mov [ebp + edx*4 + 4], edi
        mov al, [ebp + IS_DIR_OFF + edx]
        mov [ebp + IS_DIR_OFF + edx + 1], al
        dec edx
        jmp .sort_inner
.insert:
        mov [ebp + edx*4 + 4], esi
        mov al, [ebp + V_KEY_DIR]
        mov [ebp + IS_DIR_OFF + edx + 1], al
        inc ebx
        jmp .sort_outer

.print:
        xor ebx, ebx
.print_loop:
        cmp ebx, [ebp + V_COUNT]
        jae .exit
        mov esi, [ebp + ebx*4]
        mov edi, esi
        xor al, al
        mov ecx, -1
        repne scasb
        not ecx
        dec ecx                                 ; ECX = strlen
        call FUNCTION_WRITE_STDOUT
        cmp byte [ebp + IS_DIR_OFF + ebx], 0
        je .newline
        mov al, '/'
        call FUNCTION_PRINT_CHARACTER
.newline:
        mov al, 10
        call FUNCTION_PRINT_CHARACTER
        inc ebx
        jmp .print_loop

.exit:
        ;; Skip stack tear-down: FUNCTION_EXIT never returns and the
        ;; kernel discards the PD on exit.
        jmp FUNCTION_EXIT

.not_found:
        mov esi, MESSAGE_NOT_FOUND
        mov ecx, MESSAGE_NOT_FOUND_LENGTH
        jmp FUNCTION_DIE

;; Strings
DOT                      db '.',0
MESSAGE_NOT_FOUND        db `Not found\n`
MESSAGE_NOT_FOUND_LENGTH equ $ - MESSAGE_NOT_FOUND
