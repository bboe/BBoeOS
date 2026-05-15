/* ctype.h — character classification, subset of libc's <ctype.h>.

   Minimal helpers shared by parsers (strtol) and tokenizers (wc).
   Header-only; each program that includes this file inlines a private
   copy.  When a real libc lands, replace these inclusions with the
   standard <ctype.h> and the function bodies disappear from each
   program's compiled size — the call sites already use libc names.

   isspace matches a subset of libc's ASCII semantics: space, tab,
   newline, carriage return.  Vertical tab and form feed are absent
   because cc.py's lexer doesn't yet recognise the '\v' and '\f'
   escapes; once the lexer learns them, add them here.  Takes a char
   rather than libc's int — cc.py's C subset is restricted enough
   that the wider int signature would obscure rather than help. */

#ifndef CTYPE_H
#define CTYPE_H

int isspace(char character) {
    return character == ' '
        || character == '\t'
        || character == '\n'
        || character == '\r';
}

#endif
