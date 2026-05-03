#ifndef BBOEOS_LIBC_SETJMP_H
#define BBOEOS_LIBC_SETJMP_H

/* 6 dwords: ebx, esi, edi, ebp, esp, eip. */
typedef int jmp_buf[6];

void longjmp(jmp_buf env, int val) __attribute__((noreturn));
int  setjmp(jmp_buf env);

#endif
