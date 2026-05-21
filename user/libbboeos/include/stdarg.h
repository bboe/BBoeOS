#ifndef BBOEOS_STDARG_H
#define BBOEOS_STDARG_H

/* Both clang and cc.py understand the clang-style ``__builtin_va_*``
 * spellings: clang via its native intrinsics, cc.py via the matching
 * builtins in ``cc/codegen/x86/builtins.py``.  ``__builtin_va_list``
 * is a preprocessor macro under cc.py (expands to ``int *``) and a
 * built-in type under clang.  Same header text serves both. */
typedef __builtin_va_list va_list;
#define va_arg(ap, type) __builtin_va_arg(ap, type)
#define va_copy(dest, src) __builtin_va_copy(dest, src)
#define va_end(ap) __builtin_va_end(ap)
#define va_start(ap, last) __builtin_va_start(ap, last)

#endif
