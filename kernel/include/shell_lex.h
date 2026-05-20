/* shell_lex.h — single-pass, quote-aware lexer for the bboeos shell.

   The shell line is broken into a stream of tokens once at the very
   top of dispatch.  Quote handling is the lexer's responsibility and
   *only* the lexer's responsibility — operator bytes (`;`, `&&`, `||`,
   `|`, `<`, `>`, `>>`) inside `'…'` or `"…"` are folded into the
   surrounding WORD instead of emitting an operator token, so every
   downstream consumer can scan tokens without re-tracking quotes.

   The lexer fills three caller-supplied arrays:

     token_kinds[i]         — TOKEN_* constant (TOKEN_EOF terminates).
     token_word_offsets[i]  — for TOKEN_WORD, byte offset into
                              word_buffer where this token's
                              null-terminated word starts.  Undefined
                              for non-WORD tokens; callers should not
                              read it for those.
     word_buffer[off..]     — flat sequence of null-terminated words;
                              one entry per WORD token.

   Returns the token count NOT including the terminating TOKEN_EOF, or
   -1 if the input would overflow either max_tokens (room for count +
   one TOKEN_EOF slot) or max_word_bytes (cumulative WORD bytes +
   their NULs).

   Inputs and outputs are pure C arrays — no syscalls, no allocation —
   so this header compiles cleanly under clang and cc.py both. */

#ifndef SHELL_LEX_H
#define SHELL_LEX_H

/* Discriminant for the lexer's output stream.  Declared as an enum so
   cc.py's switch-on-enum exhaustiveness check fires at every dispatch
   site when a new token kind lands. */
enum TokenKind {
    TOKEN_AND,
    TOKEN_EOF,
    TOKEN_OR,
    TOKEN_PIPE,
    TOKEN_REDIRECT_APPEND,
    TOKEN_REDIRECT_IN,
    TOKEN_REDIRECT_OUT,
    TOKEN_SEMI,
    TOKEN_WORD,
};

int lex_line(char *input, int *kinds_out, int *offsets_out, char *words_out,
             int max_tokens, int max_word_bytes) {
    int scan = 0;
    int token_count = 0;
    int word_write = 0;
    while (input[scan] != '\0') {
        while (input[scan] == ' ' || input[scan] == '\t') {
            scan += 1;
        }
        if (input[scan] == '\0') {
            break;
        }
        /* Reserve at least one slot for the trailing TOKEN_EOF. */
        if (token_count >= max_tokens - 1) {
            return -1;
        }
        char character = input[scan];
        /* The ``input[scan + 1]`` lookahead below is safe even when
           ``input[scan]`` is the last non-NUL byte: the C string contract
           guarantees a NUL terminator one past the last character, and
           NUL compares unequal to every operator we test, so the
           candidate two-byte operator simply falls through to its
           single-byte sibling (or to the WORD branch). */
        if (character == ';') {
            kinds_out[token_count] = TOKEN_SEMI;
            token_count += 1;
            scan += 1;
            continue;
        }
        if (character == '&' && input[scan + 1] == '&') {
            kinds_out[token_count] = TOKEN_AND;
            token_count += 1;
            scan += 2;
            continue;
        }
        if (character == '|' && input[scan + 1] == '|') {
            kinds_out[token_count] = TOKEN_OR;
            token_count += 1;
            scan += 2;
            continue;
        }
        if (character == '|') {
            kinds_out[token_count] = TOKEN_PIPE;
            token_count += 1;
            scan += 1;
            continue;
        }
        if (character == '>' && input[scan + 1] == '>') {
            kinds_out[token_count] = TOKEN_REDIRECT_APPEND;
            token_count += 1;
            scan += 2;
            continue;
        }
        if (character == '>') {
            kinds_out[token_count] = TOKEN_REDIRECT_OUT;
            token_count += 1;
            scan += 1;
            continue;
        }
        if (character == '<') {
            kinds_out[token_count] = TOKEN_REDIRECT_IN;
            token_count += 1;
            scan += 1;
            continue;
        }
        /* WORD: copy bytes into words_out, consuming `'` / `"` as
           quote toggles instead of byte content (so the kernel never
           sees the surrounding quote characters in argv). */
        kinds_out[token_count] = TOKEN_WORD;
        offsets_out[token_count] = word_write;
        token_count += 1;
        int in_single = 0;
        int in_double = 0;
        while (input[scan] != '\0') {
            char character = input[scan];
            if (character == '\'' && in_double == 0) {
                in_single = 1 - in_single;
                scan += 1;
                continue;
            }
            if (character == '"' && in_single == 0) {
                in_double = 1 - in_double;
                scan += 1;
                continue;
            }
            if (in_single == 0 && in_double == 0) {
                if (character == ' ' || character == '\t' || character == ';' ||
                    character == '|' || character == '&' || character == '<' ||
                    character == '>') {
                    break;
                }
            }
            if (word_write >= max_word_bytes - 1) {
                return -1;
            }
            words_out[word_write] = character;
            word_write += 1;
            scan += 1;
        }
        if (word_write >= max_word_bytes) {
            return -1;
        }
        words_out[word_write] = '\0';
        word_write += 1;
    }
    kinds_out[token_count] = TOKEN_EOF;
    return token_count;
}

#endif
