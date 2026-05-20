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

   Multi-arg cross-TU calls are exercised here: ``multitu_helper_blend``
   takes three int args and ``multitu_helper_add`` takes two.  cc.py's
   Phase B implicit regparm(min(3, n)) default applies in both TUs
   independently — the prototype-side TU and the definition-side TU
   each see the same parameter shape and derive the same convention,
   so EAX/EDX/ECX line up without an explicit annotation. */
extern int multitu_helper_add(int a, int b);
extern int multitu_helper_blend(int a, int b, int c);
extern int multitu_helper_meaning_of_life();
extern int multitu_helper_seed();

int main() {
    int seed = multitu_helper_seed();
    int answer = multitu_helper_meaning_of_life();
    int doubled = multitu_helper_add(seed, seed);
    int blended = multitu_helper_blend(1, 2, 3);
    printf("multitu_demo: %d %d %d\n", seed + answer, doubled, blended);
    return 0;
}
