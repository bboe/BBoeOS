/* Regression test for the program-loader BSS-trailer cross-page peek.

   Before kernel.asm / entry.asm grew the cross-frame stager, the
   loader's 6-byte BSS_MAGIC32 trailer-peek only inspected the last
   loaded binary frame.  When the binary ended 1..5 bytes into the
   last page (binsize mod 4096 in 1..5) the magic dword bridged a
   page boundary and the peek silently fell through to bss_size = 0
   — user_image_end then equalled page_align_up(PROGRAM_BASE +
   binsize) and the .bss_page_loop mapped nothing, so the first BSS
   write faulted with EXC0E.

   This program is padded with an inline-asm ``times`` block tuned so
   cc.py's flat binary lands at binsize mod 4096 = 5 (one of the
   triggering offsets).  The write at ``probe[4096]`` then falls on a
   BSS page that *must* be mapped for the program to run — pre-fix
   the kernel would have skipped that mapping, post-fix it stages the
   trailer across both frames and computes user_image_end correctly.

   If a future cc.py change shifts the binary size out of the trigger
   range the test still passes (no false positive); update the
   ``times N db 0`` constant below to retune.  Use
   ``nasm -f bin -i kernel/include/ trailer_cross_page.asm`` after
   ``cc.py`` to print the binary size and verify ``mod 4096`` lands
   in {1,2,3,4,5}. */

char probe[8192];

int main() {
    /* The asm("times ...") padding below survives cc.py's flat path
       (nasm assembles the times block in-place) but the ccld linker
       drops it as unreferenced, so make_os.sh routes this test
       through the FLAT_PROGRAMS path — see make_os.sh.  The constant
       was tuned so the resulting flat binary lands at binsize mod
       4096 = 5; see the file header for why. */
    asm("times 3983 nop");
    probe[4096] = 'X';
    if (probe[4096] != 'X') {
        printf("trailer_cross_page: FAIL readback\n");
        return 1;
    }
    printf("trailer_cross_page: OK\n");
    return 0;
}
