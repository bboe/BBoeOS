/* Regression test for the program-loader BSS-trailer cross-page peek.

   Before kernel.asm / entry.asm grew the cross-frame stager, the
   loader's 6-byte BSS_MAGIC32 trailer-peek only inspected the last
   loaded binary frame.  When the binary ended 1..5 bytes into the
   last page (binsize mod 4096 in 1..5) the magic dword bridged a
   page boundary and the peek silently fell through to bss_size = 0
   — user_image_end then equalled page_align_up(PROGRAM_BASE +
   binsize) and the .bss_page_loop mapped nothing, so the first BSS
   write faulted with EXC0E.

   This program is padded with a ``trailer_pad`` blob tuned so the
   linked binary lands at binsize mod 4096 = 3 (one of the triggering
   offsets).  The write at ``probe[4096]`` then falls on a BSS page
   that *must* be mapped for the program to run — pre-fix the kernel
   would have skipped that mapping, post-fix it stages the trailer
   across both frames and computes user_image_end correctly.

   The padding lives in ``.rodata`` as data, NOT in main's code path.
   It is a ``times`` block, and pack-ccobj zero-fills ``times`` reps
   when packing the object (it can't expand NASM's listing rep marker
   back into bytes).  Zero-fill is exactly right for read-only padding
   but would be corrupt instructions if it sat inside ``main`` — which
   is why the original flat-path version emitted ``times nop`` straight
   into the code and this object-pipeline port moves it out to .rodata.
   ccld concatenates every section unconditionally (no dead-section GC),
   so the unreferenced blob survives into the final image.

   If a future cc.py / ccld change shifts the binary size out of the
   trigger range the test still passes (no false positive); update the
   ``times N db 0`` constant below to retune.  Build the object through
   ``make_os.sh`` (or cc.py --object → nasm → pack-ccobj → ccld.py by
   hand) and check ``wc -c`` on the linked binary lands at ``mod 4096``
   in {1,2,3,4,5}. */

char probe[8192];

int main() {
    probe[4096] = 'X';
    if (probe[4096] != 'X') {
        printf("trailer_cross_page: FAIL readback\n");
        return 1;
    }
    printf("trailer_cross_page: OK\n");
    return 0;
}

/* Read-only padding tuned so the linked binary's size mod 4096 = 3 —
   the middle of the {1..5} trigger range, so a small future codegen
   drift in either direction still lands in range.  Emitted after main
   so it never sits at the .text entry point, and in .rodata so
   pack-ccobj's zero-fill of the times rep is harmless data rather than
   executable bytes.  See the file header to retune. */
asm("section .rodata\n"
    "trailer_pad: times 3974 db 0\n"
    "section .text");
