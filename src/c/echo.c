#include "getopt.h"

int interpret_escapes(char *string) {
    int read_index = 0;
    int write_index = 0;
    while (string[read_index] != '\0') {
        if (string[read_index] != '\\') {
            string[write_index] = string[read_index];
            read_index += 1;
            write_index += 1;
            continue;
        }
        char next = string[read_index + 1];
        if (next == '\0') {
            string[write_index] = '\\';
            write_index += 1;
            read_index += 1;
            continue;
        }
        char emitted;
        int recognized = 1;
        if (next == 'n') {
            emitted = '\n';
        } else if (next == 't') {
            emitted = '\t';
        } else if (next == 'r') {
            emitted = '\r';
        } else if (next == 'b') {
            emitted = '\b';
        } else if (next == 'e') {
            emitted = '\e';
        } else if (next == '0') {
            emitted = '\0';
        } else if (next == '\\') {
            emitted = '\\';
        } else {
            recognized = 0;
        }
        if (recognized) {
            string[write_index] = emitted;
            write_index += 1;
            read_index += 2;
        } else {
            string[write_index] = '\\';
            string[write_index + 1] = next;
            write_index += 2;
            read_index += 2;
        }
    }
    return write_index;
}

int main(int argc, char *argv[]) {
    int interpret = 0;
    int suppress_newline = 0;
    int option = getopt(argc, argv, "en");
    while (option != -1) {
        if (option == 'e') {
            interpret = 1;
        } else if (option == 'n') {
            suppress_newline = 1;
        } else {
            die("echo: bad flag\n");
        }
        option = getopt(argc, argv, "en");
    }
    int first = optind;
    int i = first;
    while (i < argc) {
        if (i > first) {
            putchar(' ');
        }
        int length = interpret ? interpret_escapes(argv[i]) : strlen(argv[i]);
        write(STDOUT, argv[i], length);
        i += 1;
    }
    if (!suppress_newline) {
        putchar('\n');
    }
    return 0;
}
