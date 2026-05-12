/* macros.h — standard helper macros for cc.py.

   Requires function-like ``#define`` support (PR #350) and ternary
   conditional expression support.  cc.py's preprocessor does not yet
   accept ``#ifndef`` / ``#endif`` header guards (see ``cc/preprocessor.py``),
   so this file deliberately ships without them — include it at most
   once per translation unit.

   Both macros parenthesise every operand to keep precedence right
   under nested use (``MAX(a + 1, b)`` works the way you'd expect).
*/

#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define MIN(a, b) ((a) < (b) ? (a) : (b))
