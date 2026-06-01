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

;; --- global data ---
        dd 200
        dw 0B032h
_program_end:
_bss_end equ _program_end + 200
;; --- BSS (zero-initialized) ---
_g_points equ _program_end
points equ _g_points
_g_wrecs equ _program_end + 104
wrecs equ _g_wrecs
