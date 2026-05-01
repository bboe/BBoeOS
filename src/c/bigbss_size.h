/* Empirically-measured page-precise ceiling on BSS — the largest
   array `bigbss` can declare and still complete program_enter's
   phase-2 BSS allocation under the 1 GB direct-map cap.

   Anchored at -m 1025, the smallest QEMU memory size where the OS
   can take full advantage of its 1 GB direct map.  At exactly
   -m 1024 QEMU/SeaBIOS plants ~128 KB of ACPI tables (type-3 and
   type-4 in E820) near the top of the 1 GB block, eating ~32
   frames out of the bitmap.  At -m ≥ 1025 those reservations shift
   above the 1 GB clamp and the bottom 1 GB is fully type-1, giving
   us the full 262,144 frames minus kernel reservations.  Above
   -m 1025 there's no further benefit — the kernel direct map is
   clamped to 1 GB regardless.

   Kept in a shared header so `bigbss` (uses this exact count) and
   `bigbss_fail` (uses this + 1 page, asserts OOM) stay coupled.
   Three tests pin the boundary:

     * bigbss      — BSS_PAGES = BIGBSS_PAGES.  Must fit at -m 1025
                     (the max-beneficial RAM size).
     * bigbss_oom  — BSS_PAGES = BIGBSS_PAGES.  Must OOM at -m 1024
                     (one MB less; ACPI reservation moves into the
                     bitmap and ~32 frames disappear).  Tripwire if
                     BIGBSS_PAGES is set too low.
     * bigbss_fail — BSS_PAGES = BIGBSS_PAGES + 1.  Must OOM at
                     -m 1025.  Tripwire if BIGBSS_PAGES is set too
                     high.

   When the kernel adds or removes pages — direct-map PT count
   shifts, kernel image grows, new boot-time allocations land — re-
   probe at -m 1025 (or any -m ≥ 1025; the answer is identical) and
   update this constant.  The two tripwire tests will go red (one
   or both) when it drifts. */

#define BIGBSS_PAGES 261493
