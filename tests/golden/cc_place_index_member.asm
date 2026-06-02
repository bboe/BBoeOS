        [bits 32]
        org 08048000h

%include "constants.asm"

%define __INT16_TYPE__ signed short
%define __INT32_TYPE__ signed int
%define __INT64_TYPE__ signed long long
%define __INT8_TYPE__ signed char
%define __PTRDIFF_TYPE__ int
%define __SIZE_TYPE__ unsigned int
%define __UINT16_TYPE__ unsigned short
%define __UINT32_TYPE__ unsigned int
%define __UINT64_TYPE__ unsigned long long
%define __UINT8_TYPE__ unsigned char
%define __builtin_va_list int *

probe:
        push ebp
        mov ebp, esp
        sub esp, 16
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov [ebp-12], ecx
        mov eax, [ebp-12]
        push eax
        mov eax, [ebp-4]
        imul eax, 13
        mov ebx, eax
        pop eax
        mov [_g_points+ebx], eax
        mov eax, [ebp-12]
        push eax
        mov eax, [ebp-4]
        imul eax, 13
        mov ebx, eax
        push ebx
        mov eax, [ebp-8]
        pop ebx
        add ebx, eax
        pop eax
        mov byte [_g_points+9+ebx], al
        mov eax, [ebp-4]
        imul eax, 13
        mov ebx, eax
        mov eax, [_g_points+4+ebx]
        mov edx, eax
        mov eax, [ebp-4]
        imul eax, 13
        mov ebx, eax
        push ebx
        mov eax, [ebp-8]
        pop ebx
        add ebx, eax
        movzx eax, byte [_g_points+9+ebx]
        mov ecx, eax
        mov eax, edx
        add eax, ecx
        mov esp, ebp
        pop ebp
        ret

probe_word_member:
        push ebp
        mov ebp, esp
        sub esp, 16
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov [ebp-12], ecx
        mov eax, [ebp-12]
        push eax
        mov eax, [ebp-4]
        imul eax, 12
        mov ebx, eax
        push ebx
        mov eax, [ebp-8]
        shl eax, 1
        pop ebx
        add ebx, eax
        pop eax
        mov word [_g_wrecs+4+ebx], ax
        mov eax, [ebp-4]
        imul eax, 12
        mov ebx, eax
        push ebx
        mov eax, [ebp-8]
        shl eax, 1
        pop ebx
        add ebx, eax
        movzx eax, word [_g_wrecs+4+ebx]
        mov esp, ebp
        pop ebp
        ret

probe_sizeof:
        push ebp
        mov ebp, esp
        mov eax, 4
        pop ebp
        ret

probe_assign_expr:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        push eax
        mov eax, [ebp-4]
        imul eax, 13
        mov ebx, eax
        pop eax
        mov [_g_points+ebx], eax
        mov edx, eax
        mov esp, ebp
        pop ebp
        ret

probe_assign_elem_expr:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov [ebp-12], ecx
        mov eax, [ebp-12]
        push eax
        mov eax, [ebp-4]
        imul eax, 13
        mov ebx, eax
        push ebx
        mov eax, [ebp-8]
        pop ebx
        add ebx, eax
        pop eax
        mov byte [_g_points+9+ebx], al
        mov edx, eax
        mov esp, ebp
        pop ebp
        ret

probe_dot_read:
        push ebp
        mov ebp, esp
        mov ebx, _g_g_outer
        mov eax, [ebx]
        pop ebp
        ret

probe_dot_store:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov ebx, _g_g_outer
        mov eax, [ebp-4]
        mov [ebx], eax
        mov eax, [ebx]
        mov esp, ebp
        pop ebp
        ret

probe_arrow_read:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov ebx, [ebp-4]
        mov eax, [ebx]
        mov esp, ebp
        pop ebp
        ret

probe_arrow_store:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        mov ebx, [ebp-4]
        mov [ebx], eax
        mov eax, [ebx]
        mov esp, ebp
        pop ebp
        ret

probe_chain_read:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov ebx, [ebp-4]
        mov eax, [ebx+5]
        mov ebx, eax
        mov eax, [ebx]
        mov esp, ebp
        pop ebp
        ret

probe_chain_store:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov ebx, [ebp-4]
        mov eax, [ebx+5]
        mov ebx, eax
        mov eax, [ebp-8]
        mov [ebx], eax
        mov ebx, [ebp-4]
        mov eax, [ebx+5]
        mov ebx, eax
        mov eax, [ebx]
        mov esp, ebp
        pop ebp
        ret

probe_cast_base:
        push ebp
        mov ebp, esp
        pop ebp
        ret

probe_inline_index_read:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        push eax
        mov ebx, [ebp-4]
        pop eax
        add ebx, eax
        movzx eax, byte [ebx+4]
        mov esp, ebp
        pop ebp
        ret

probe_inline_index_store:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov [ebp-12], ecx
        mov eax, [ebp-12]
        push eax
        mov eax, [ebp-8]
        push eax
        mov ebx, [ebp-4]
        pop eax
        add ebx, eax
        pop eax
        mov byte [ebx+4], al
        xor eax, eax
        mov esp, ebp
        pop ebp
        ret

probe_inline_index_const:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov ebx, [ebp-4]
        movzx eax, byte [ebx+7]
        mov esp, ebp
        pop ebp
        ret

probe_pointer_index_read:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        push eax
        mov ebx, [ebp-4]
        mov ebx, [ebx+12]
        pop eax
        add ebx, eax
        movzx eax, byte [ebx]
        mov esp, ebp
        pop ebp
        ret

probe_pointer_index_store:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov [ebp-12], ecx
        mov eax, [ebp-12]
        push eax
        mov eax, [ebp-8]
        push eax
        mov ebx, [ebp-4]
        mov ebx, [ebx+12]
        pop eax
        add ebx, eax
        pop eax
        mov byte [ebx], al
        xor eax, eax
        mov esp, ebp
        pop ebp
        ret

probe_word_inline_index:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        shl eax, 1
        push eax
        mov ebx, [ebp-4]
        pop eax
        add ebx, eax
        mov eax, [ebx+16]
        mov esp, ebp
        pop ebp
        ret

probe_member_addr:
        push ebp
        mov ebp, esp
        pop ebp
        ret

probe_member_addr_offset:
        push ebp
        mov ebp, esp
        add eax, 12
        pop ebp
        ret

probe_member_elem_addr:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        push eax
        mov ebx, [ebp-4]
        pop eax
        add ebx, eax
        lea eax, [ebx+4]
        mov esp, ebp
        pop ebp
        ret

probe_bitfield_read:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov ebx, [ebp-4]
        mov al, [ebx+4]
        shr al, 1
        and al, 7
        movzx eax, al
        mov esp, ebp
        pop ebp
        ret

probe_bitfield_store:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        mov ebx, [ebp-4]
        mov cl, al
        and cl, 7
        shl cl, 1
        mov al, [ebx+4]
        and al, 241
        or al, cl
        mov [ebx+4], al
        xor eax, eax
        mov esp, ebp
        pop ebp
        ret

probe_bitfield_one_literal:
        push ebp
        mov ebp, esp
        sub esp, 4
        mov [ebp-4], eax
        mov ebx, [ebp-4]
        or byte [ebx+4], 1
        xor eax, eax
        mov esp, ebp
        pop ebp
        ret

probe_bitfield_constfold:
        push ebp
        mov ebp, esp
        sub esp, 9
        and byte [ebp-5+4], 254
        mov eax, 5
        mov cl, al
        and cl, 7
        shl cl, 1
        mov al, [ebp-5+4]
        and al, 241
        or al, cl
        mov [ebp-5+4], al
        shr al, 1
        and al, 7
        movzx eax, al
        mov esp, ebp
        pop ebp
        ret

probe_member_incdec:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov ebx, [ebp-4]
        mov eax, [ebx]
        inc eax
        mov [ebx], eax
        mov eax, [ebx]
        sub eax, 1
        mov edx, eax
        mov eax, [ebx]
        mov [ebp-8], eax
        mov eax, edx
        add eax, [ebp-8]
        mov esp, ebp
        pop ebp
        ret

probe_member_predec:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov ebx, [ebp-4]
        mov eax, [ebx]
        dec eax
        mov [ebx], eax
        mov eax, [ebx]
        mov esp, ebp
        pop ebp
        ret

probe_addr_of_dot:
        push ebp
        mov ebp, esp
        lea eax, [_g_g_outer]
        pop ebp
        ret

;; --- global data ---
        dd 214
        dw 0B032h
_program_end:
_bss_end equ _program_end + 214
;; --- BSS (zero-initialized) ---
_g_g_flags equ _program_end
g_flags equ _g_g_flags
_g_g_outer equ _program_end + 5
g_outer equ _g_g_outer
_g_points equ _program_end + 14
points equ _g_points
_g_wrecs equ _program_end + 118
wrecs equ _g_wrecs
