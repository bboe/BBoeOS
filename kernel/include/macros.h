/* macros.h — standard helper macros for cc.py.

   Requires function-like ``#define`` support (PR #350), ternary
   conditional expression support (PR #351), and ``#ifndef`` header-
   guard support (PR #352) in cc.py's preprocessor.

   Both macros parenthesise every operand to keep precedence right
   under nested use (``MAX(a + 1, b)`` works the way you'd expect).

   --------------------------------------------------------------------
   Unsigned-operand gotcha (standard C; cc.py follows the same rules):
   --------------------------------------------------------------------

   ``MAX(x - 1, 0)`` saturates at 0 only when ``x - 1`` evaluates in
   signed-int arithmetic.  Standard C integer-promotion rules:

     * ``u8``, ``u16`` (narrower than ``int``): promote to
       ``int``.  ``MAX(x - 1, 0)`` works correctly — the comparison
       is signed (cc.py emits ``jg``).

     * ``u32`` (same width as ``int``): does NOT promote.  The
       ``0`` literal is converted up to ``unsigned int``, so the
       comparison is unsigned.  ``0u - 1u == 0xFFFFFFFF``,
       ``0xFFFFFFFF > 0u`` is true, and ``MAX`` returns the underflow.
       cc.py emits ``ja`` here and propagates the wrong result.

   Workaround for ``u32`` (or any unsigned ≥ ``int`` width): copy
   into an ``int`` local first, then pass the local to ``MAX``.  Plain
   C-style integer casts like ``(int)x`` are no-ops in cc.py — they
   don't re-type the comparison.

       int signed_x = unsigned_x;            // force int type
       int r = MAX(signed_x - 1, 0);         // signed compare

   The behaviour is pinned by ``tests/unit/test_macros.py``.
*/

#ifndef MACROS_H
#define MACROS_H

#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define MIN(a, b) ((a) < (b) ? (a) : (b))

#endif
