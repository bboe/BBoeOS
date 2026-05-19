/* End-to-end multi-translation-unit demo for the cc.py + ccld
   pipeline.  This source declares functions that live in a sibling
   translation unit (multitu_demo_helper.c).  The build compiles
   each .c file independently with cc.py --object → pack-ccobj,
   then ccld resolves the cross-object rel32 calls and produces
   the loadable flat binary.

   Both objects must be present at link time or the linker
   rejects with an unresolved-extern error, which is the whole
   point of the test.  test_programs.py runs the linked program
   under QEMU and matches its output.

   Cross-TU calls are intentionally zero-arg here.  cc.py's
   single-TU analyzer commits a callee to either regparm or cdecl
   based on intra-TU call shapes, while a cross-TU caller has no
   visibility into that decision and falls back to cdecl.  Multi-
   arg cross-TU calls therefore mis-pair the convention until
   cc.py grows a `static` keyword (or an equivalent ABI marker)
   so the analyzer knows when a callee is exported for cross-TU
   use and must commit to the stable convention.  Zero-arg
   crossings are unaffected because both conventions agree on
   "no args, return in EAX". */
extern int multitu_helper_meaning_of_life();
extern int multitu_helper_seed();

int main() {
    int seed = multitu_helper_seed();
    int answer = multitu_helper_meaning_of_life();
    printf("multitu_demo: %d\n", seed + answer);
    return 0;
}
