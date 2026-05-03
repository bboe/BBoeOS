/* tools/libc/builtins.c — hand-rolled subset of clang compiler-rt.
 *
 * 32-bit i386 has no hardware 64-bit divide, so clang lowers
 * `int64_t / int64_t` / `uint64_t / uint64_t` (and the corresponding
 * remainder ops) to calls into compiler-rt's `__divdi3` / `__udivdi3`
 * / `__moddi3` / `__umoddi3`.  Without compiler-rt linked in (we run
 * fully freestanding under -nostdlib), the ld step fails with
 * `undefined reference to __divdi3` the first time a 64-bit divide
 * appears in user code (Doom's m_fixed.c FixedDiv is the trigger).
 *
 * Rather than depend on the host's libclang_rt.builtins-i386.a — which
 * varies across distros and would break the "no third-party code"
 * stance for our libc — we provide a minimal, hand-written
 * implementation here.  Algorithm: textbook binary long division
 * (shift-and-subtract from the high bit downward).  Performance is
 * adequate for Doom's modest 64-bit-divide volume; if it ever shows
 * up in profiles we can add a dword-divisor fast path that uses
 * i386's hardware DIV.  Each function is independent so dead-code
 * elimination drops what isn't called. */

#include <stdint.h>

static uint64_t udiv64_inner(uint64_t numerator, uint64_t denominator, uint64_t *remainder_out) {
    /* Returns quotient and stores the remainder in *remainder_out.
     * Both signed and unsigned wrappers route through this — keeping
     * the bit-walk in one place avoids two near-identical copies. */
    if (denominator == 0) {
        /* Spec leaves /0 undefined; mirror compiler-rt by returning
         * (UINT64_MAX, numerator) so callers see a wildly wrong value
         * instead of crashing.  Real apps shouldn't hit this. */
        *remainder_out = numerator;
        return (uint64_t)-1;
    }
    if (denominator > numerator) {
        *remainder_out = numerator;
        return 0;
    }
    /* Align the divisor to the highest set bit of the numerator so the
     * first subtraction is the largest one possible — saves us walking
     * the leading zeros bit by bit. */
    int shift = 0;
    while ((denominator << 1) <= numerator && (denominator >> 63) == 0) {
        denominator <<= 1;
        shift++;
    }
    uint64_t quotient = 0;
    while (shift >= 0) {
        if (numerator >= denominator) {
            numerator -= denominator;
            quotient |= ((uint64_t)1 << shift);
        }
        denominator >>= 1;
        shift--;
    }
    *remainder_out = numerator;
    return quotient;
}

int64_t __divdi3(int64_t numerator, int64_t denominator) {
    /* Abs both, divide, restore sign of the quotient.  C's signed
     * remainder rule is "quotient truncates toward zero" — that's
     * exactly what (sign-fix outside, unsigned divide inside) gives us. */
    int negate = (numerator < 0) ^ (denominator < 0);
    uint64_t un = numerator < 0 ? (uint64_t)(-numerator) : (uint64_t)numerator;
    uint64_t ud = denominator < 0 ? (uint64_t)(-denominator) : (uint64_t)denominator;
    uint64_t remainder;
    uint64_t quotient = udiv64_inner(un, ud, &remainder);
    return negate ? -(int64_t)quotient : (int64_t)quotient;
}

int64_t __moddi3(int64_t numerator, int64_t denominator) {
    /* Sign of remainder follows the numerator (C99 6.5.5). */
    int negate = numerator < 0;
    uint64_t un = numerator < 0 ? (uint64_t)(-numerator) : (uint64_t)numerator;
    uint64_t ud = denominator < 0 ? (uint64_t)(-denominator) : (uint64_t)denominator;
    uint64_t remainder;
    udiv64_inner(un, ud, &remainder);
    return negate ? -(int64_t)remainder : (int64_t)remainder;
}

uint64_t __udivdi3(uint64_t numerator, uint64_t denominator) {
    uint64_t remainder;
    return udiv64_inner(numerator, denominator, &remainder);
}

uint64_t __umoddi3(uint64_t numerator, uint64_t denominator) {
    uint64_t remainder;
    udiv64_inner(numerator, denominator, &remainder);
    return remainder;
}
