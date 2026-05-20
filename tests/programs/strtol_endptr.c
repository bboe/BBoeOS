/* strtol_endptr — exercise strtol's endptr writeback (cc.py
   pointer-to-pointer support).  Parses "42abc" and verifies that
   *endptr lands on the first non-digit character ('a'). */

#include "../../kernel/include/strtol.h"

int main() {
    char *input = "42abc";
    char *end = NULL;
    int value = strtol(input, &end, 10);
    if (value == 42 && end == input + 2 && end[0] == 'a') {
        printf("ENDPTR_OK value=%d tail=%s\n", value, end);
    } else {
        printf("ENDPTR_BAD value=%d\n", value);
    }
    return 0;
}
