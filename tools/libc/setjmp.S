        .intel_syntax noprefix
        .text

        .globl longjmp
longjmp:
        mov  eax, [esp + 4]     /* jmp_buf * */
        mov  edx, [esp + 8]     /* val */
        test edx, edx
        jnz  1f
        mov  edx, 1             /* longjmp(0) returns 1, per C standard */
1:
        mov  ebx, [eax + 0]
        mov  esi, [eax + 4]
        mov  edi, [eax + 8]
        mov  ebp, [eax + 12]
        mov  esp, [eax + 16]
        mov  ecx, [eax + 20]    /* saved EIP (the return address from inside setjmp) */
        mov  eax, edx           /* what setjmp() will see as its return value */
        jmp  ecx

        .globl setjmp
setjmp:
        mov  eax, [esp + 4]     /* jmp_buf * */
        mov  [eax + 0],  ebx
        mov  [eax + 4],  esi
        mov  [eax + 8],  edi
        mov  [eax + 12], ebp
        lea  ecx, [esp + 4]     /* ESP value before this call's CALL pushed */
        mov  [eax + 16], ecx
        mov  ecx, [esp]         /* return address */
        mov  [eax + 20], ecx
        xor  eax, eax           /* setjmp returns 0 from the direct path */
        ret
