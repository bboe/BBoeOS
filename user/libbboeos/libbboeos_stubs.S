/* user/libbboeos/libbboeos_stubs.S — auto-generated.  DO NOT EDIT.
 *
 * Regenerate with `python3 tools/generate_libbboeos_stubs.py`.
 * Each stub shuffles cdecl stack arguments into regparm
 * registers (EAX/EDX/ECX) then jumps to the shared
 * libbboeos blob via `jmp [FUNCTION_<NAME>_PTR]`.
 * Clang programs link this file BEFORE libbboeos.a so ld
 * resolves each export to the stub and never pulls the
 * full body out of the archive.
 *
 * Source of truth: FUNCTION_<NAME>_PTR offsets in
 * kernel/include/constants.asm + prototypes in
 * user/libbboeos/include/*.h.  Sorted alphabetically.
 */

        .intel_syntax noprefix
        .section .text.libbboeos_stubs, "ax", @progbits

        .globl memchr
        .type  memchr, @function
memchr:
        mov eax, [esp+4]
        mov edx, [esp+8]
        mov ecx, [esp+12]
        jmp [0x00010e34]    /* FUNCTION_MEMCHR_PTR */
        .size memchr, . - memchr

        .globl memcmp
        .type  memcmp, @function
memcmp:
        mov eax, [esp+4]
        mov edx, [esp+8]
        mov ecx, [esp+12]
        jmp [0x00010e38]    /* FUNCTION_MEMCMP_PTR */
        .size memcmp, . - memcmp

        .globl memcpy
        .type  memcpy, @function
memcpy:
        mov eax, [esp+4]
        mov edx, [esp+8]
        mov ecx, [esp+12]
        jmp [0x00010e3c]    /* FUNCTION_MEMCPY_PTR */
        .size memcpy, . - memcpy

        .globl memmove
        .type  memmove, @function
memmove:
        mov eax, [esp+4]
        mov edx, [esp+8]
        mov ecx, [esp+12]
        jmp [0x00010e40]    /* FUNCTION_MEMMOVE_PTR */
        .size memmove, . - memmove

        .globl memset
        .type  memset, @function
memset:
        mov eax, [esp+4]
        mov edx, [esp+8]
        mov ecx, [esp+12]
        jmp [0x00010e44]    /* FUNCTION_MEMSET_PTR */
        .size memset, . - memset

        .globl strcasecmp
        .type  strcasecmp, @function
strcasecmp:
        mov eax, [esp+4]
        mov edx, [esp+8]
        jmp [0x00010e48]    /* FUNCTION_STRCASECMP_PTR */
        .size strcasecmp, . - strcasecmp

        .globl strcat
        .type  strcat, @function
strcat:
        mov eax, [esp+4]
        mov edx, [esp+8]
        jmp [0x00010e4c]    /* FUNCTION_STRCAT_PTR */
        .size strcat, . - strcat

        .globl strchr
        .type  strchr, @function
strchr:
        mov eax, [esp+4]
        mov edx, [esp+8]
        jmp [0x00010e50]    /* FUNCTION_STRCHR_PTR */
        .size strchr, . - strchr

        .globl strcmp
        .type  strcmp, @function
strcmp:
        mov eax, [esp+4]
        mov edx, [esp+8]
        jmp [0x00010e54]    /* FUNCTION_STRCMP_PTR */
        .size strcmp, . - strcmp

        .globl strcpy
        .type  strcpy, @function
strcpy:
        mov eax, [esp+4]
        mov edx, [esp+8]
        jmp [0x00010e58]    /* FUNCTION_STRCPY_PTR */
        .size strcpy, . - strcpy

        .globl strdup
        .type  strdup, @function
strdup:
        mov eax, [esp+4]
        jmp [0x00010e5c]    /* FUNCTION_STRDUP_PTR */
        .size strdup, . - strdup

        .globl strerror
        .type  strerror, @function
strerror:
        mov eax, [esp+4]
        jmp [0x00010e60]    /* FUNCTION_STRERROR_PTR */
        .size strerror, . - strerror

        .globl strlen
        .type  strlen, @function
strlen:
        mov eax, [esp+4]
        jmp [0x00010e64]    /* FUNCTION_STRLEN_PTR */
        .size strlen, . - strlen

        .globl strncasecmp
        .type  strncasecmp, @function
strncasecmp:
        mov eax, [esp+4]
        mov edx, [esp+8]
        mov ecx, [esp+12]
        jmp [0x00010e68]    /* FUNCTION_STRNCASECMP_PTR */
        .size strncasecmp, . - strncasecmp

        .globl strncat
        .type  strncat, @function
strncat:
        mov eax, [esp+4]
        mov edx, [esp+8]
        mov ecx, [esp+12]
        jmp [0x00010e6c]    /* FUNCTION_STRNCAT_PTR */
        .size strncat, . - strncat

        .globl strncmp
        .type  strncmp, @function
strncmp:
        mov eax, [esp+4]
        mov edx, [esp+8]
        mov ecx, [esp+12]
        jmp [0x00010e70]    /* FUNCTION_STRNCMP_PTR */
        .size strncmp, . - strncmp

        .globl strncpy
        .type  strncpy, @function
strncpy:
        mov eax, [esp+4]
        mov edx, [esp+8]
        mov ecx, [esp+12]
        jmp [0x00010e74]    /* FUNCTION_STRNCPY_PTR */
        .size strncpy, . - strncpy

        .globl strrchr
        .type  strrchr, @function
strrchr:
        mov eax, [esp+4]
        mov edx, [esp+8]
        jmp [0x00010e78]    /* FUNCTION_STRRCHR_PTR */
        .size strrchr, . - strrchr

        .globl strstr
        .type  strstr, @function
strstr:
        mov eax, [esp+4]
        mov edx, [esp+8]
        jmp [0x00010e7c]    /* FUNCTION_STRSTR_PTR */
        .size strstr, . - strstr
