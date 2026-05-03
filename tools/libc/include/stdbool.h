#ifndef BBOEOS_LIBC_STDBOOL_H
#define BBOEOS_LIBC_STDBOOL_H
/* C99 stdbool.h shim — clang has __bool_true_false_are_defined but in
 * -nostdinc mode we still need to define _Bool / true / false. */
#define bool  _Bool
#define true  1
#define false 0
#define __bool_true_false_are_defined 1
#endif
