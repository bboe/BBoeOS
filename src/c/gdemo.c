/* Demonstrates file-scope (global) variable support.  Exercises a
   scalar counter mutated from a helper, a fixed-size char buffer
   filled by main, and a zero-initialized int array treated as a ring
   of scratch slots.  Also verifies that sizeof(global_array) folds to
   the expected byte count at compile time. */

int counter;
int history[8];
char label[8];

void bump() {
    counter = counter + 1;
}

int main() {
    counter = 10;
    int i = 0;
    while (i < 5) {
        bump();
        history[i] = counter;
        i = i + 1;
    }
    label[0] = 'g';
    label[1] = 'l';
    label[2] = 'o';
    label[3] = 'b';
    label[4] = 0;
    int j = 0;
    while (j < 5) {
        printf("%s[%d] = %d\n", label, j, history[j]);
        j = j + 1;
    }
    printf("sizeof(history) = %d\n", sizeof(history));
    printf("sizeof(label) = %d\n", sizeof(label));
}
