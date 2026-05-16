#include "getopt.h"

/* Stateful set2 walker — yields one expanded char per call, returns -1
   at end.  Walked in parallel with set1's inline expansion to populate
   the translation table without materialising set1[] / set2[]. */
int set2_in_range;
char *set2_input;
int set2_pos;
int set2_range_current;
int set2_range_end;

int set2_next() {
    if (set2_in_range) {
        int character = set2_range_current;
        if (character == set2_range_end) {
            set2_in_range = 0;
        } else {
            set2_range_current = character + 1;
        }
        return character;
    }
    int i = set2_pos;
    if (set2_input[i] == '\0') {
        return -1;
    }
    if (set2_input[i + 1] == '-' && set2_input[i + 2] != '\0' && set2_input[i] <= set2_input[i + 2]) {
        int start = set2_input[i] & 0xFF;
        int end = set2_input[i + 2] & 0xFF;
        set2_pos = i + 3;
        if (start < end) {
            set2_in_range = 1;
            set2_range_current = start + 1;
            set2_range_end = end;
        }
        return start;
    }
    set2_pos = i + 1;
    return set2_input[i] & 0xFF;
}

int main(int argc, char *argv[]) {
    int delete_mode = 0;
    int option = getopt(argc, argv, "d");
    while (option != -1) {
        if (option == 'd') {
            delete_mode = 1;
        } else {
            die("tr: bad flag\n");
        }
        option = getopt(argc, argv, "d");
    }
    int positional_count = argc - optind;
    if (delete_mode) {
        if (positional_count != 1) {
            die("Usage: tr -d <set1>\n");
        }
    } else if (positional_count != 2) {
        die("Usage: tr <set1> <set2>\n");
    }
    char translation[256];
    char deleted[256];
    int i = 0;
    while (i < 256) {
        translation[i] = i;
        deleted[i] = 0;
        i += 1;
    }
    if (!delete_mode) {
        set2_input = argv[optind + 1];
        set2_pos = 0;
        set2_in_range = 0;
    }
    char *set1_input = argv[optind];
    i = 0;
    while (set1_input[i] != '\0') {
        int start = set1_input[i] & 0xFF;
        int end;
        if (set1_input[i + 1] == '-' && set1_input[i + 2] != '\0' && set1_input[i] <= set1_input[i + 2]) {
            end = set1_input[i + 2] & 0xFF;
            i += 3;
        } else {
            end = start;
            i += 1;
        }
        int character = start;
        while (character <= end) {
            if (delete_mode) {
                deleted[character] = 1;
            } else {
                int target = set2_next();
                if (target < 0) {
                    die("tr: set length mismatch\n");
                }
                translation[character] = target;
            }
            character += 1;
        }
    }
    if (!delete_mode && set2_next() >= 0) {
        die("tr: set length mismatch\n");
    }
    char buffer[1024];
    char output_buffer[1024];
    while (1) {
        int bytes_read = read(STDIN, buffer, 1024);
        if (bytes_read <= 0) {
            break;
        }
        int output_length = 0;
        i = 0;
        while (i < bytes_read) {
            int character = buffer[i] & 0xFF;
            if (deleted[character] == '\0') {
                output_buffer[output_length] = translation[character];
                output_length += 1;
            }
            i += 1;
        }
        if (output_length > 0) {
            write(STDOUT, output_buffer, output_length);
        }
    }
    return 0;
}
