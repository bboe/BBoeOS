/* Empirically-measured page-precise ceiling on BSS — the largest
   array `bigbss` can declare and still complete program_enter's
   phase-2 BSS allocation at -m 2048.

   Anchored at -m 2048 to exercise the kmap window
   (memory_management/kmap.asm) end-to-end.  At this size the user
   pool spans ~2 GB of physical RAM but the kernel direct map only
   covers 4 MB (PDE 1022 = FIRST_KERNEL_PDE); every frame above
   that handed out by the bitmap allocator is reached through a
   kmap_map slot in PDE 1023's window.  With BIGBSS_PAGES = 523,595,
   nearly all of program_enter's phase-2 zero-fills go through the
   slow path, proving the kmap helpers correctly alias high-physical
   frames.

   Below -m 2048 the user pool shrinks and BIGBSS_PAGES no longer
   fits; above -m 2048 BIGBSS_PAGES rises further as more high
   frames become addressable through kmap.  This constant is *not*
   the absolute kernel ceiling — it's the ceiling for this specific
   -m, deliberately chosen because it straddles the direct-map
   boundary and gives both halves of kmap solid coverage.

   Kept in a shared header so `bigbss` (uses this exact count) and
   `bigbss_fail` (uses this + 1 page, asserts OOM) stay coupled.
   Three tests pin the boundary:

     * bigbss      — BSS_PAGES = BIGBSS_PAGES.  Must fit at -m 2048.
                     Nearly every BSS frame (>1024 frames sit
                     within the 4 MB direct map; the rest go
                     through kmap) exercises the slow path during
                     program_enter's phase 2 zero fill — direct
                     end-to-end kmap coverage.
     * bigbss_oom  — BSS_PAGES = BIGBSS_PAGES.  Must OOM at -m 2047
                     (one MB less RAM = ~256 fewer frames; the
                     bitmap can no longer fit BIGBSS_PAGES + per-PD
                     overhead).  Tripwire if BIGBSS_PAGES is set
                     too low — a downward drift > ~256 frames would
                     start fitting at -m 2047 and the test would
                     pass without OOMing.
     * bigbss_fail — BSS_PAGES = BIGBSS_PAGES + 1.  Must OOM at
                     -m 2048.  Tripwire if BIGBSS_PAGES is set too
                     high — page-precise.

   When the kernel adds or removes pages — direct-map PT count
   shifts, kernel image grows, new boot-time allocations land — re-
   probe at -m 2048 and update this constant.  The two tripwire
   tests will go red (one or both) when it drifts. */

#define BIGBSS_PAGES 523595
