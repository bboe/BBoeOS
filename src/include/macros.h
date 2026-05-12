/* macros.h — standard helper macros for cc.py.

   Requires function-like ``#define`` support (PR #350), ternary
   conditional expression support, and ``#ifndef`` header-guard
   support in cc.py's preprocessor.

   Both macros parenthesise every operand to keep precedence right
   under nested use (``MAX(a + 1, b)`` works the way you'd expect).
*/

#ifndef MACROS_H
#define MACROS_H

#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define MIN(a, b) ((a) < (b) ? (a) : (b))

#endif
