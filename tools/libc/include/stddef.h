#ifndef BBOEOS_LIBC_STDDEF_H
#define BBOEOS_LIBC_STDDEF_H
#define NULL ((void*)0)
/* Use the compiler's built-in width macros so our types match the
 * compiler's view of size_t / ptrdiff_t on every host (32-bit on bboeos,
 * 64-bit on the Linux host used for the libc unit tests). */
typedef __PTRDIFF_TYPE__ ptrdiff_t;
typedef __SIZE_TYPE__    size_t;
#define offsetof(t, m) ((size_t)&(((t*)0)->m))
#endif
