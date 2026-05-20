/* Sibling translation unit for multitu_demo.c — see that file for
   the full description of what the multi-TU pipeline exercises.
   This file defines the functions multitu_demo.c declares extern. */
int multitu_helper_add(int a, int b) {
    return a + b;
}

int multitu_helper_blend(int a, int b, int c) {
    return a * 100 + b * 10 + c;
}

int multitu_helper_meaning_of_life() {
    return 39;
}

int multitu_helper_seed() {
    return 3;
}
