/* getopt.h — minimal libc-compatible short-option parser.

   Subset of POSIX getopt(3):
   - Recognises single-character short options listed in *optstring*.
   - A trailing ':' on an option char in *optstring* marks it as
     value-taking; the next argv entry is consumed and exposed as
     `optarg`.
   - Returns the option char on a match, '?' on an unknown option,
     -1 when argv is exhausted or the next argv isn't a recognised
     option.

   Not supported (deliberately, to keep the header tiny):
   - Combined flags (`-lw` for `-l -w`).
   - Value-attached form (`-nN` for `-n N`).
   - The `--` end-of-options sentinel and GNU `--long` options.
   - argv permutation; positional args may not follow flag args.

   `optind` and `optarg` are file-scope globals — each program that
   includes this header gets its own private copies, same convention as
   `strtol.h` and `ctype.h`.  When a real libc lands, replace the
   inclusion with the standard `<getopt.h>` and the bodies disappear
   from each program's compiled size. */

#ifndef GETOPT_H
#define GETOPT_H

int optind = 1;
char *optarg = NULL;

int getopt(int argc, char **argv, char *optstring) {
    if (optind >= argc) {
        return -1;
    }
    char *current = argv[optind];
    if (current[0] != '-' || current[1] == '\0') {
        return -1;
    }
    char option = current[1];
    int spec_index = 0;
    while (optstring[spec_index] != '\0') {
        if (optstring[spec_index] == option) {
            optind += 1;
            if (optstring[spec_index + 1] == ':') {
                if (optind >= argc) {
                    return '?';
                }
                optarg = argv[optind];
                optind += 1;
            }
            return option;
        }
        spec_index += 1;
    }
    return '?';
}

#endif
