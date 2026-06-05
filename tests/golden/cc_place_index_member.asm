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
        add eax, eax
        pop ebx
        add ebx, eax
        pop eax
        mov word [_g_wrecs+4+ebx], ax
        mov eax, [ebp-4]
        imul eax, 12
        mov ebx, eax
        push ebx
        mov eax, [ebp-8]
        add eax, eax
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

probe_member_increment_decrement:
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

probe_deref_read_char:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov esi, [ebp-4]
        movzx eax, byte [esi]
        mov esp, ebp
        pop ebp
        ret

probe_deref_read_int:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov esi, [ebp-4]
        mov eax, [esi]
        mov esp, ebp
        pop ebp
        ret

probe_deref_read_ushort:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov esi, [ebp-4]
        movzx eax, word [esi]
        mov esp, ebp
        pop ebp
        ret

probe_deref_store_char:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        mov esi, [ebp-4]
        mov [esi], al
        movzx eax, byte [esi]
        mov esp, ebp
        pop ebp
        ret

probe_deref_store_int:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        mov esi, [ebp-4]
        mov [esi], eax
        mov esp, ebp
        pop ebp
        ret

probe_deref_store_ushort:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        mov esi, [ebp-4]
        mov [esi], eax
        movzx eax, word [esi]
        mov esp, ebp
        pop ebp
        ret

probe_cast_deref_uchar_local:
        push ebp
        mov ebp, esp
        sub esp, 6
        mov eax, 7
        mov byte [ebp-2], al
        movzx eax, byte [ebp-2]
        mov esp, ebp
        pop ebp
        ret

probe_cast_deref_ushort_local:
        push ebp
        mov ebp, esp
        sub esp, 8
        xor eax, eax
        mov [ebp-4], eax
        movzx eax, word [ebp-4]
        mov esp, ebp
        pop ebp
        ret

probe_cast_deref_uchar_expr:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-4]
        add eax, [ebp-8]
        movzx eax, byte [eax]
        mov esp, ebp
        pop ebp
        ret

probe_cast_deref_ushort_expr:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-4]
        add eax, [ebp-8]
        movzx eax, word [eax]
        mov esp, ebp
        pop ebp
        ret

probe_cast_deref_store_uchar_local:
        push ebp
        mov ebp, esp
        pop ebp
        ret

probe_cast_deref_store_uchar_expr:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov [ebp-12], ecx
        mov eax, [ebp-12]
        push eax
        mov eax, [ebp-4]
        add eax, [ebp-8]
        mov esi, eax
        pop eax
        mov [esi], al
        mov esp, ebp
        pop ebp
        ret

probe_cast_deref_store_ushort_expr:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov [ebp-12], ecx
        mov eax, [ebp-12]
        push eax
        mov eax, [ebp-4]
        add eax, [ebp-8]
        mov esi, eax
        pop eax
        mov word [esi], ax
        mov esp, ebp
        pop ebp
        ret

probe_double_index_byte_const:
        push ebp
        mov ebp, esp
        mov eax, [_g_names+4]
        mov esi, eax
        movzx eax, byte [esi+2]
        pop ebp
        ret

probe_double_index_byte_var:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_names+esi]
        mov esi, eax
        mov eax, [ebp-8]
        add esi, eax
        movzx eax, byte [esi]
        mov esp, ebp
        pop ebp
        ret

probe_double_index_byte_expr:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_names+esi]
        mov esi, eax
        push esi
        mov eax, [ebp-8]
        inc eax
        pop esi
        add esi, eax
        movzx eax, byte [esi]
        mov esp, ebp
        pop ebp
        ret

probe_double_index_int_const:
        push ebp
        mov ebp, esp
        mov eax, [_g_ints+4]
        mov esi, eax
        mov eax, [esi+8]
        pop ebp
        ret

probe_double_index_int_store_const:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        push eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_ints+esi]
        mov esi, eax
        pop eax
        mov [esi], eax
        mov esp, ebp
        pop ebp
        ret

probe_double_index_int_store_var:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov [ebp-12], ecx
        mov eax, [ebp-12]
        push eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_ints+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        pop eax
        mov [esi], eax
        mov esp, ebp
        pop ebp
        ret

probe_double_index_int_var:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_ints+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        mov eax, [esi]
        mov esp, ebp
        pop ebp
        ret

probe_double_index_word_var:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_words+esi]
        mov esi, eax
        mov eax, [ebp-8]
        add eax, eax
        add esi, eax
        mov eax, [esi]
        mov esp, ebp
        pop ebp
        ret

probe_deref_postinc_read:
        push ebp
        mov ebp, esp
        sub esp, 4
        mov [ebp-4], eax
        mov eax, [eax]
        add dword [ebp-4], 4
        mov edx, eax
        mov esp, ebp
        pop ebp
        ret

probe_deref_preinc_read:
        push ebp
        mov ebp, esp
        sub esp, 4
        mov [ebp-4], eax
        add dword [ebp-4], 4
        mov eax, [ebp-4]
        mov eax, [eax]
        mov edx, eax
        mov esp, ebp
        pop ebp
        ret

probe_deref_postdec_read:
        push ebp
        mov ebp, esp
        sub esp, 4
        mov [ebp-4], eax
        movzx eax, byte [eax]
        dec dword [ebp-4]
        mov edx, eax
        mov esp, ebp
        pop ebp
        ret

probe_deref_predec_read:
        push ebp
        mov ebp, esp
        sub esp, 4
        mov [ebp-4], eax
        sub dword [ebp-4], 4
        mov eax, [ebp-4]
        mov eax, [eax]
        mov edx, eax
        mov esp, ebp
        pop ebp
        ret

probe_deref_postinc_store:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        mov esi, [ebp-4]
        mov [esi], al
        inc dword [ebp-4]
        mov esp, ebp
        pop ebp
        ret

probe_deref_preinc_store:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov [ebp-8], edx
        add dword [ebp-4], 4
        mov eax, [ebp-8]
        mov esi, [ebp-4]
        mov [esi], eax
        mov esp, ebp
        pop ebp
        ret

probe_deref_in_if:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov esi, [ebp-4]
        mov eax, [esi]
        test eax, eax
        je ._ir_endif44
        mov eax, 1
        mov esp, ebp
        pop ebp
        ret
._ir_endif44:
        xor eax, eax
        mov esp, ebp
        pop ebp
        ret

probe_double_index_in_if:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_ints+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        mov eax, [esi]
        test eax, eax
        je ._ir_endif46
        mov eax, 1
        mov esp, ebp
        pop ebp
        ret
._ir_endif46:
        xor eax, eax
        mov esp, ebp
        pop ebp
        ret

probe_deref_assign_expr:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        mov esi, [ebp-4]
        mov [esi], eax
        mov eax, [ebp-8]
        mov edx, eax
        mov esp, ebp
        pop ebp
        ret

probe_deref_incassign_expr:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        mov esi, [ebp-4]
        mov [esi], al
        inc dword [ebp-4]
        mov edx, eax
        mov esp, ebp
        pop ebp
        ret

probe_cast_deref_assign_expr:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov [ebp-12], ecx
        mov eax, [ebp-12]
        push eax
        mov eax, [ebp-4]
        add eax, [ebp-8]
        mov esi, eax
        pop eax
        mov [esi], al
        mov edx, eax
        mov esp, ebp
        pop ebp
        ret

probe_sizeof_deref:
        push ebp
        mov ebp, esp
        mov eax, 4
        pop ebp
        ret

probe_sizeof_cast_deref_expr:
        push ebp
        mov ebp, esp
        mov eax, 2
        pop ebp
        ret

probe_addr_of_global:
        push ebp
        mov ebp, esp
        mov eax, _g_g_counter
        pop ebp
        ret

probe_addr_of_local:
        push ebp
        mov ebp, esp
        sub esp, 8
        xor eax, eax
        mov [ebp-4], eax
        lea eax, [ebp-4]
        mov esp, ebp
        pop ebp
        ret

probe_sizeof_addr:
        push ebp
        mov ebp, esp
        mov eax, 4
        pop ebp
        ret

probe_postinc_expr:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        inc dword [ebp-4]
        mov eax, [ebp-4]
        sub eax, 1
        mov edx, eax
        add eax, [ebp-4]
        mov esp, ebp
        pop ebp
        ret

probe_preinc_expr:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        inc dword [ebp-4]
        mov eax, [ebp-4]
        mov edx, eax
        add eax, [ebp-4]
        mov esp, ebp
        pop ebp
        ret

probe_postdec_expr:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        dec dword [ebp-4]
        mov eax, [ebp-4]
        add eax, 1
        mov edx, eax
        add eax, [ebp-4]
        mov esp, ebp
        pop ebp
        ret

probe_predec_expr:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        dec dword [ebp-4]
        mov eax, [ebp-4]
        mov edx, eax
        add eax, [ebp-4]
        mov esp, ebp
        pop ebp
        ret

probe_postinc_expr_global:
        push ebp
        mov ebp, esp
        inc dword [_g_g_counter]
        mov eax, [_g_g_counter]
        sub eax, 1
        mov edx, eax
        add eax, [_g_g_counter]
        pop ebp
        ret

probe_preinc_expr_global:
        push ebp
        mov ebp, esp
        inc dword [_g_g_counter]
        mov eax, [_g_g_counter]
        mov edx, eax
        add eax, [_g_g_counter]
        pop ebp
        ret

probe_postinc_stmt:
        push ebp
        mov ebp, esp
        inc dword [_g_g_counter]
        mov eax, [_g_g_counter]
        sub eax, 1
        pop ebp
        ret

probe_predec_stmt:
        push ebp
        mov ebp, esp
        dec dword [_g_g_counter]
        mov eax, [_g_g_counter]
        pop ebp
        ret

probe_postinc_stmt_local:
        push ebp
        mov ebp, esp
        sub esp, 4
        mov [ebp-4], eax
        inc dword [ebp-4]
        mov eax, [ebp-4]
        sub eax, 1
        mov esp, ebp
        pop ebp
        ret

probe_predec_stmt_local:
        push ebp
        mov ebp, esp
        sub esp, 4
        mov [ebp-4], eax
        dec dword [ebp-4]
        mov eax, [ebp-4]
        mov esp, ebp
        pop ebp
        ret

probe_indexed_call_global_const:
        push ebp
        mov ebp, esp
        push eax
        mov eax, [_g_g_fptable+4]
        call eax
        add esp, 4
        pop ebp
        ret

probe_indexed_call_global_var:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        push eax
        lea esi, [_g_g_fptable]
        mov eax, [ebp-4]
        shl eax, 2
        add eax, esi
        mov eax, [eax]
        call eax
        add esp, 4
        mov esp, ebp
        pop ebp
        ret

probe_indexed_call_global_const_exprval:
        push ebp
        mov ebp, esp
        sub esp, 4
        mov [ebp-4], eax
        push edx
        mov eax, [ebp-4]
        push eax
        mov eax, [_g_g_fptable+4]
        call eax
        add esp, 4
        pop edx
        mov edx, eax
        mov esp, ebp
        pop ebp
        ret

probe_indexed_call_global_var_exprval:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov [ebp-8], edx
        push edx
        mov eax, [ebp-8]
        push eax
        lea esi, [_g_g_fptable]
        mov eax, [ebp-4]
        shl eax, 2
        add eax, esi
        mov eax, [eax]
        call eax
        add esp, 4
        pop edx
        mov edx, eax
        mov esp, ebp
        pop ebp
        ret

probe_indexed_call_global_stmt:
        push ebp
        mov ebp, esp
        push eax
        mov eax, [_g_g_fptable]
        call eax
        add esp, 4
        pop ebp
        ret

probe_indexed_call_local_const:
        push ebp
        mov ebp, esp
        sub esp, 28
        mov [ebp-4], eax
        mov eax, [_g_g_fptable_src+8]
        lea esi, [ebp-20]
        mov [esi+8], eax
        mov eax, [ebp-4]
        push eax
        mov eax, [ebp-20+8]
        call eax
        add esp, 4
        mov esp, ebp
        pop ebp
        ret

probe_indexed_call_local_var:
        push ebp
        mov ebp, esp
        sub esp, 32
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_fptable_src+esi]
        push eax
        lea esi, [ebp-24]
        mov eax, [ebp-4]
        shl eax, 2
        add esi, eax
        pop eax
        mov [esi], eax
        mov eax, [ebp-8]
        push eax
        lea esi, [ebp-24]
        mov eax, [ebp-4]
        shl eax, 2
        add eax, esi
        mov eax, [eax]
        call eax
        add esp, 4
        mov esp, ebp
        pop ebp
        ret

probe_indexed_call_local_stmt:
        push ebp
        mov ebp, esp
        sub esp, 24
        mov [ebp-4], eax
        mov eax, [_g_g_fptable_src]
        lea esi, [ebp-20]
        mov [esi], eax
        mov eax, [ebp-4]
        push eax
        mov eax, [ebp-20]
        call eax
        add esp, 4
        mov esp, ebp
        pop ebp
        ret

probe_addr_deref:
        push ebp
        mov ebp, esp
        pop ebp
        ret

probe_named_array_postinc:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov eax, 5
        mov esi, [ebp-4]
        shl esi, 2
        mov [_g_g_arr+esi], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_arr+esi]
        inc eax
        mov esi, [ebp-4]
        shl esi, 2
        mov [_g_g_arr+esi], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_arr+esi]
        sub eax, 1
        mov edx, eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_arr+esi]
        mov [ebp-8], eax
        mov eax, edx
        add eax, [ebp-8]
        mov esp, ebp
        pop ebp
        ret

probe_named_array_predec:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov eax, 5
        mov esi, [ebp-4]
        shl esi, 2
        mov [_g_g_arr+esi], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_arr+esi]
        dec eax
        mov esi, [ebp-4]
        shl esi, 2
        mov [_g_g_arr+esi], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_arr+esi]
        mov esp, ebp
        pop ebp
        ret

probe_named_array_postinc_stmt:
        push ebp
        mov ebp, esp
        sub esp, 4
        mov [ebp-4], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_arr+esi]
        inc eax
        mov esi, [ebp-4]
        shl esi, 2
        mov [_g_g_arr+esi], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_arr+esi]
        sub eax, 1
        mov esp, ebp
        pop ebp
        ret

probe_double_index_postinc:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        mov eax, [esi]
        inc eax
        push eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        pop eax
        mov [esi], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        mov eax, [esi]
        sub eax, 1
        mov esp, ebp
        pop ebp
        ret

probe_double_index_postinc_stmt:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        mov eax, [esi]
        inc eax
        push eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        pop eax
        mov [esi], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        mov eax, [esi]
        sub eax, 1
        mov esp, ebp
        pop ebp
        ret

probe_double_index_preinc:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        mov eax, [esi]
        inc eax
        push eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        pop eax
        mov [esi], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        mov eax, [esi]
        mov esp, ebp
        pop ebp
        ret

probe_named_array_predec_stmt:
        push ebp
        mov ebp, esp
        sub esp, 4
        mov [ebp-4], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_arr+esi]
        dec eax
        mov esi, [ebp-4]
        shl esi, 2
        mov [_g_g_arr+esi], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_arr+esi]
        mov esp, ebp
        pop ebp
        ret

probe_double_index_preinc_stmt:
        push ebp
        mov ebp, esp
        sub esp, 8
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        mov eax, [esi]
        inc eax
        push eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        pop eax
        mov [esi], eax
        mov esi, [ebp-4]
        shl esi, 2
        mov eax, [_g_g_rows+esi]
        mov esi, eax
        mov eax, [ebp-8]
        shl eax, 2
        add esi, eax
        mov eax, [esi]
        mov esp, ebp
        pop ebp
        ret

probe_call_through_ptr:
        push ebp
        mov ebp, esp
        sub esp, 12
        mov [ebp-4], eax
        mov [ebp-8], edx
        mov eax, [ebp-8]
        push eax
        mov eax, [ebp-4]
        call eax
        add esp, 4
        mov esp, ebp
        pop ebp
        ret

probe_chained_bitfield_store:
        push ebp
        mov ebp, esp
        sub esp, 13
        mov [ebp-4], eax
        lea eax, [ebp-9]
        mov ebx, eax
        mov eax, [ebp-4]
        mov cl, al
        and cl, 7
        shl cl, 1
        mov al, [ebx+4]
        and al, 241
        or al, cl
        mov [ebx+4], al
        lea eax, [ebp-9]
        mov ebx, eax
        mov al, [ebx+4]
        shr al, 1
        and al, 7
        movzx eax, al
        mov esp, ebp
        pop ebp
        ret

;; --- global data ---
        dd 346
        dw 0B032h
_program_end:
_bss_end equ _program_end + 346
;; --- BSS (zero-initialized) ---
_g_g_counter equ _program_end
g_counter equ _g_g_counter
_g_g_flags equ _program_end + 4
g_flags equ _g_g_flags
_g_g_outer equ _program_end + 9
g_outer equ _g_g_outer
_g_g_arr equ _program_end + 18
g_arr equ _g_g_arr
_g_g_fptable equ _program_end + 50
g_fptable equ _g_g_fptable
_g_g_fptable_src equ _program_end + 66
g_fptable_src equ _g_g_fptable_src
_g_g_rows equ _program_end + 82
g_rows equ _g_g_rows
_g_ints equ _program_end + 98
ints equ _g_ints
_g_names equ _program_end + 114
names equ _g_names
_g_points equ _program_end + 130
points equ _g_points
_g_words equ _program_end + 234
words equ _g_words
_g_wrecs equ _program_end + 250
wrecs equ _g_wrecs
