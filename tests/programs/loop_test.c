/* loop_test — consolidated loop smoke tests.
 *
 * Two modes that used to be separate programs: basic (printf in a
 * counted while loop) and array (walk a string array via sizeof). */

/* Forward declarations — clang requires them since main() is sorted
   alphabetically and lands ahead of every callee it dispatches to.
   cc.py's whole-file pre-pass resolves these without prototypes. */
void mode_array();
void mode_basic();
int string_equal(char *left, char *right);

int main(int argc, char *argv[]) {
    if (argc < 2) {
        die("loop_test: pass a mode\n");
    }
    char *mode = argv[1];
    if (string_equal(mode, "basic")) {
        mode_basic();
    } else if (string_equal(mode, "array")) {
        mode_array();
    } else {
        die("loop_test: unknown mode\n");
    }
    return 0;
}

void mode_array() {
    char *messages[] = {"a", "b", "c"};
    int i = 0;
    while (i < sizeof(messages) / sizeof(char *)) {
        int length = strlen(messages[i]);
        write(STDOUT, messages[i], length);
        i += 1;
    }
    putchar('\n');
}

void mode_basic() {
    int i = 0;
    while (i < 5) {
        printf("a");
        i += 1;
    }
    putchar('\n');
}

int string_equal(char *left, char *right) {
    int index = 0;
    while (left[index] != '\0' && right[index] != '\0') {
        if (left[index] != right[index]) {
            return 0;
        }
        index = index + 1;
    }
    return left[index] == right[index];
}
