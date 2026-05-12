#include "wait.h"

#define HISTORY_SIZE 16
#define MAX_SEGMENTS 32
#define OP_AND  2
#define OP_END  0
#define OP_OR   3
#define OP_SEMI 1

/* Tokenized command chain: a single line may contain multiple commands
   joined by `;`, `&&`, or `||`.  parse_chain() splits chain_buf in
   place by replacing operator chars with NUL, fills segment_offsets[]
   with byte offsets into chain_buf, and writes the operator type
   *following* each segment into segment_ops[] (the last entry is
   OP_END).  Each segment is then memcpy'd into BUFFER one-at-a-time
   and dispatched, so the EXEC_ARG pointer stays inside the
   kernel-copied 256-byte handoff window.  cc.py global arrays only
   support char/int/uint8_t/struct elements, hence offsets-not-pointers. */
char chain_buf[MAX_INPUT];

/* Redirection state for one dispatch_buffer call.  parse_redirections
   fills these; apply_redirections consumes them.  Each filename is
   null-terminated and lives in redirect_names; the redirect entries
   point in via byte offsets (cc.py arrays don't carry pointer-element
   types end-to-end).  Operator kinds: IN = `<` ; OUT = `>` (truncate);
   APPND = `>>` (append). */
#define MAX_REDIRECTS 3
#define REDIRECT_OP_APPND 0
#define REDIRECT_OP_IN    1
#define REDIRECT_OP_NONE  2
#define REDIRECT_OP_OUT   3

int redirect_count;
char redirect_names[192];  /* MAX_REDIRECTS * MAX_PATH = 3 * 64 */
char redirect_ops[MAX_REDIRECTS];
char redirect_targets[MAX_REDIRECTS];  /* target fd: 0 (stdin) or 1 (stdout) */

/* saved_fds[target] = saved-fd-number from dup(target) before the
   redirect was applied, or -1 if no save was taken.  Indexed by
   target fd (0 or 1).  apply_redirections fills it; restore_redirections
   consumes and clears it. */
int saved_fds[2];

/* Command history ring.  history is a flat array of HISTORY_SIZE slots,
   each MAX_INPUT bytes.  Access slot i as history + (i * MAX_INPUT).
   history_count is the lifetime push count, clipped to HISTORY_SIZE
   for browsing range.  history_view = 0 means the live edit line;
   1..min(history_count, HISTORY_SIZE) walks backward. */
char history[HISTORY_SIZE * MAX_INPUT];
int history_count;
int history_view;

/* Ctrl-K kill buffer.  File-scope so it lands in the program's BSS;
   per-program PDs do not alias the low 1 MB so a fixed-address scratch
   like SECTOR_BUFFER would page-fault. */
char kill_buf[MAX_INPUT];

/* Wait status of the most recently exec()'d child.  Written by
   try_exec() on a successful exec; read by the dispatch loop and
   expand_dollar_question() to expose $? to the user. */
int last_exec_status;

/* Snapshot of the live edit line taken on the first Up keypress.
   Restored when Down walks back past the newest history entry to
   history_view == 0, matching bash's partial-line restore behaviour. */
char saved_line[MAX_INPUT];
int saved_line_length;

int segment_offsets[MAX_SEGMENTS];
char segment_ops[MAX_SEGMENTS];

/* Scratch buffers for the pipeline parser (`cmd1 | cmd2`).  File-scope
   so that (a) cc.py passes their addresses correctly on function calls
   and (b) they live in BSS without consuming additional user stack.
   pipe_left_buf / pipe_right_buf hold the trimmed command tokens;
   pipe_left_path / pipe_right_path hold the bin/-prefixed paths. */
char pipe_left_buf[MAX_INPUT];
char pipe_right_buf[MAX_INPUT];
char pipe_left_path[MAX_PATH];
char pipe_right_path[MAX_PATH];

/* contains_redirect_token — return non-zero if `s` has an unquoted
   `<`, `>`, or `>>`.  Used to reject pipe + redirect on the same
   side (v1 limitation). */
int contains_redirect_token(char *s) {
    int i = 0;
    int in_single = 0;
    int in_double = 0;
    while (s[i] != '\0') {
        if (s[i] == '\'' && in_double == 0) {
            in_single = 1 - in_single;
        } else if (s[i] == '"' && in_single == 0) {
            in_double = 1 - in_double;
        } else if (in_single == 0 && in_double == 0) {
            if (s[i] == '<' || s[i] == '>') {
                return 1;
            }
        }
        i += 1;
    }
    return 0;
}

/* Forward decl: apply_redirections calls restore_redirections on the
   error-rollback path; restore_redirections sorts after apply in
   source order.  cc.py resolves forward refs silently; clang under
   -std=c99 needs the prototype. */
int restore_redirections();

int apply_redirections() {
    /* For each redirect in order: save the original target via dup,
       open the file, dup2 over the target, close the temp fd.
       Returns 0 on success, -1 on error (with prior saves rolled
       back).  Sets last_exec_status on failure. */
    saved_fds[0] = -1;
    saved_fds[1] = -1;
    int index = 0;
    while (index < redirect_count) {
        int target = redirect_targets[index];
        int op = redirect_ops[index];
        char *path = redirect_names + (index * MAX_PATH);
        /* Save target only once (later same-target redirects reuse the
           same save — bash "last wins" semantics; the earlier file is
           opened+truncated+closed by the later dup2). */
        if (saved_fds[target] < 0) {
            int saved = dup(target);
            if (saved < 0) {
                printf("dup failed\n");
                last_exec_status = 1 << 8;
                return -1;
            }
            saved_fds[target] = saved;
        }
        int flags;
        if (op == REDIRECT_OP_IN) {
            flags = O_RDONLY;
        } else if (op == REDIRECT_OP_OUT) {
            flags = O_WRONLY | O_CREAT | O_TRUNC;
        } else {
            flags = O_WRONLY | O_CREAT;
        }
        int new_fd = open(path, flags);
        if (new_fd < 0) {
            printf("cannot open %s\n", path);
            last_exec_status = 1 << 8;
            restore_redirections();
            return -1;
        }
        if (op == REDIRECT_OP_APPND) {
            seek(new_fd, 0, SEEK_END);
        }
        if (dup2(new_fd, target) < 0) {
            close(new_fd);
            printf("dup2 failed\n");
            last_exec_status = 1 << 8;
            restore_redirections();
            return -1;
        }
        close(new_fd);
        index = index + 1;
    }
    return 0;
}

int cursor_back(int count) {
    if (count > 0) {
        printf("\e[%dD", count);
    }
    return 0;
}

int delete_at_cursor(char *buf, int cursor, int end) {
    /* Shift buf[cursor+1..end) left one slot, redraw, erase the stale
       trailing character, reposition cursor.  Returns the new end. */
    int shift = cursor;
    while (shift < end - 1) {
        buf[shift] = buf[shift + 1];
        shift += 1;
    }
    end -= 1;
    write(STDOUT, buf + cursor, end - cursor);
    putchar(' ');
    cursor_back(end - cursor + 1);
    return end;
}

/* Forward decls: dispatch_buffer calls expand_dollar_question and
   try_exec, both of which sort later in this file.  cc.py resolves
   forward refs silently; clang under -std=c99 needs them up front. */
int expand_dollar_question(char *buffer, int max_len);
int try_exec(char *name);

void dispatch_buffer(char *buf, char *exec_path) {
    /* Run a single command sitting in BUFFER.  Splits at the first
       space, expands $? in the argument tail, and routes to a builtin
       or external executable.  Updates last_exec_status so that chain
       operators (&&, ||) and subsequent $? expansions see the result. */
    set_exec_arg(NULL);
    int scan = 0;
    while (buf[scan] != '\0' && buf[scan] != ' ') {
        scan += 1;
    }
    if (buf[scan] == ' ') {
        buf[scan] = '\0';
        set_exec_arg(buf + scan + 1);
        if (expand_dollar_question(buf + scan + 1, MAX_INPUT - (scan + 1)) < 0) {
            printf("$? expansion exceeded MAX_INPUT\n");
            last_exec_status = 1 << 8;
            return;
        }
    }
    if (strcmp(buf, "help") == 0) {
        printf("Commands: help reboot shutdown\n");
        last_exec_status = 0;
    } else if (strcmp(buf, "reboot") == 0) {
        reboot();
    } else if (strcmp(buf, "shutdown") == 0) {
        shutdown();
        printf("APM shutdown failed\n");
        last_exec_status = 1 << 8;
    } else {
        int result = try_exec(buf);
        if (result == 1) {
            last_exec_status = 126 << 8;   /* bash: not executable */
            printf("not executable\n");
        } else if (result == 0) {
            /* Not found in root — retry inside bin/ */
            exec_path[0] = 'b';
            exec_path[1] = 'i';
            exec_path[2] = 'n';
            exec_path[3] = '/';
            int copy_index = 0;
            while (buf[copy_index] != '\0') {
                exec_path[4 + copy_index] = buf[copy_index];
                copy_index += 1;
            }
            exec_path[4 + copy_index] = '\0';
            int bin_result = try_exec(exec_path);
            if (bin_result == 1) {
                last_exec_status = 126 << 8;
                printf("not executable\n");
            } else if (bin_result == 0) {
                last_exec_status = 127 << 8;
                printf("unknown command\n");
            }
        }
    }
}

int expand_dollar_question(char *buffer, int max_len) {
    /* In-place replace every "$?" in buffer with the decimal representation
       of the bash-shaped last status:
         normal exit  -> WEXITSTATUS  (bits 15..8 of last_exec_status)
         signal kill  -> 128 + WTERMSIG (low 7 bits)
       Returns the new length, or -1 if the expansion would exceed max_len.
       Decoding uses the POSIX-shaped macros from include/wait.h. */
    int bash_status;
    if (WIFEXITED(last_exec_status)) {
        bash_status = WEXITSTATUS(last_exec_status);
    } else {
        bash_status = 128 + WTERMSIG(last_exec_status);
    }
    char digits[4];
    int digit_count = 0;
    int n = bash_status;
    if (n == 0) {
        digits[0] = '0';
        digit_count = 1;
    } else {
        while (n > 0) {
            digits[digit_count] = '0' + (n % 10);
            digit_count = digit_count + 1;
            n = n / 10;
        }
    }
    /* Reverse digits in place. */
    int i = 0;
    int j = digit_count - 1;
    while (i < j) {
        char tmp = digits[i];
        digits[i] = digits[j];
        digits[j] = tmp;
        i = i + 1;
        j = j - 1;
    }
    /* Walk buffer, replace each $? with digits[0..digit_count). */
    int read_index = 0;
    int len = strlen(buffer);
    while (read_index < len - 1) {
        if (buffer[read_index] == '$' && buffer[read_index + 1] == '?') {
            int growth = digit_count - 2;
            if (len + growth >= max_len) {
                return -1;
            }
            /* Shift tail right by growth (could be -1, 0, 1, 2). */
            if (growth > 0) {
                int tail_index = len;
                while (tail_index > read_index + 2) {
                    buffer[tail_index + growth] = buffer[tail_index];
                    tail_index = tail_index - 1;
                }
                buffer[read_index + 2 + growth] = buffer[read_index + 2];
            } else if (growth < 0) {
                /* Shift tail left. */
                int tail_index = read_index + 2;
                while (tail_index <= len) {
                    buffer[tail_index + growth] = buffer[tail_index];
                    tail_index = tail_index + 1;
                }
            }
            int digit_index = 0;
            while (digit_index < digit_count) {
                buffer[read_index + digit_index] = digits[digit_index];
                digit_index = digit_index + 1;
            }
            len = len + growth;
            read_index = read_index + digit_count;
        } else {
            read_index = read_index + 1;
        }
    }
    buffer[len] = '\0';
    return len;
}

/* find_top_level_pipe — scan `segment` for a `|` outside of quoted
   regions that is NOT part of a `||` operator.  Returns the byte
   offset of the first lone `|`, or -1 if none is present.  Returns
   -2 if a second lone `|` is present (rejects double pipelines). */
int find_top_level_pipe(char *segment) {
    int i = 0;
    int in_single = 0;
    int in_double = 0;
    int first = -1;
    while (segment[i] != '\0') {
        if (segment[i] == '\'' && in_double == 0) {
            in_single = 1 - in_single;
        } else if (segment[i] == '"' && in_single == 0) {
            in_double = 1 - in_double;
        } else if (segment[i] == '|' && in_single == 0 && in_double == 0) {
            /* Skip `||` — that is a chain operator already handled. */
            if (segment[i + 1] == '|') {
                i += 2;
                continue;
            }
            if (first < 0) {
                first = i;
            } else {
                return -2;
            }
        }
        i += 1;
    }
    return first;
}

/* Forward declarations: history_down / history_up are defined here in
   alphabetical order but call replace_line and visual_bell, which sort
   after them.  cc.py resolves these forward references silently; clang
   under -std=c99 (test_cc_compatibility's reference build) requires
   explicit declarations. */
int replace_line(char *buf, int cursor, int end, char *new_content, int new_length);
int visual_bell();

int history_down(char *buf, int cursor, int end) {
    /* Walk history one entry forward (toward the live line).  Returns
       the new end; cursor follows.  Silent no-op when already at the
       live line — matches bash.  On reaching the live line, restores
       the partial line that was being edited before the first Up. */
    if (history_view == 0) {
        return end;
    }
    history_view = history_view - 1;
    if (history_view == 0) {
        return replace_line(buf, cursor, end, saved_line, saved_line_length);
    }
    int slot = (history_count - history_view) % HISTORY_SIZE;
    char *entry = history + (slot * MAX_INPUT);
    return replace_line(buf, cursor, end, entry, strlen(entry));
}

int history_up(char *buf, int cursor, int end) {
    /* Walk history one entry back (toward older commands).  Returns
       the new end; cursor follows.  Visual-bell at the oldest entry.
       On the first Up (history_view == 0), snapshot the live edit line
       into saved_line so history_down can restore it. */
    int max_view = history_count;
    if (max_view > HISTORY_SIZE) {
        max_view = HISTORY_SIZE;
    }
    if (history_view >= max_view) {
        visual_bell();
        return end;
    }
    if (history_view == 0) {
        memcpy(saved_line, buf, end);
        saved_line_length = end;
    }
    history_view = history_view + 1;
    int slot = (history_count - history_view) % HISTORY_SIZE;
    char *entry = history + (slot * MAX_INPUT);
    return replace_line(buf, cursor, end, entry, strlen(entry));
}

int insert_char(char *buf, int cursor, int end, char ch) {
    /* Shift buf[cursor..end) right one slot, write ch at cursor, redraw
       tail, and reposition cursor.  Returns the new end index. Caller
       guarantees end < MAX_INPUT. */
    int shift = end;
    while (shift > cursor) {
        buf[shift] = buf[shift - 1];
        shift -= 1;
    }
    buf[cursor] = ch;
    end += 1;
    write(STDOUT, buf + cursor, end - cursor);
    cursor_back(end - cursor - 1);
    return end;
}

int parse_chain(char *line) {
    /* Tokenize `line` in place: replace operator chars with NUL, fill
       segments[]/segment_ops[].  Returns segment count, or -1 if the
       line has more than MAX_SEGMENTS segments.  Leading whitespace
       between operators is trimmed; trailing whitespace within a
       segment is left to the existing first-space split. */
    int count = 0;
    int i = 0;
    int len = strlen(line);
    while (line[i] == ' ') {
        i += 1;
    }
    segment_offsets[count] = i;
    while (i < len) {
        if (line[i] == ';') {
            line[i] = '\0';
            segment_ops[count] = OP_SEMI;
            count += 1;
            if (count >= MAX_SEGMENTS) {
                return -1;
            }
            i += 1;
            while (line[i] == ' ') {
                i += 1;
            }
            segment_offsets[count] = i;
        } else if (line[i] == '&' && line[i + 1] == '&') {
            line[i] = '\0';
            line[i + 1] = '\0';
            segment_ops[count] = OP_AND;
            count += 1;
            if (count >= MAX_SEGMENTS) {
                return -1;
            }
            i += 2;
            while (line[i] == ' ') {
                i += 1;
            }
            segment_offsets[count] = i;
        } else if (line[i] == '|' && line[i + 1] == '|') {
            line[i] = '\0';
            line[i + 1] = '\0';
            segment_ops[count] = OP_OR;
            count += 1;
            if (count >= MAX_SEGMENTS) {
                return -1;
            }
            i += 2;
            while (line[i] == ' ') {
                i += 1;
            }
            segment_offsets[count] = i;
        } else {
            i += 1;
        }
    }
    segment_ops[count] = OP_END;
    return count + 1;
}

int parse_redirections(char *segment) {
    /* Scan `segment` for `>>`, `>`, `<` tokens; for each, capture the
       following whitespace-delimited filename into redirect_names and
       overwrite the operator+filename region with spaces so the
       remaining text dispatches normally.  Returns 0 on success or -1
       on syntax error (missing filename, filename too long, more than
       MAX_REDIRECTS).  Sets redirect_count. */
    redirect_count = 0;
    int i = 0;
    int len = strlen(segment);
    while (i < len) {
        char ch = segment[i];
        int op = REDIRECT_OP_NONE;
        int op_length = 0;
        int target_fd = 0;
        if (ch == '>' && i + 1 < len && segment[i + 1] == '>') {
            op = REDIRECT_OP_APPND;
            op_length = 2;
            target_fd = 1;
        } else if (ch == '>') {
            op = REDIRECT_OP_OUT;
            op_length = 1;
            target_fd = 1;
        } else if (ch == '<') {
            op = REDIRECT_OP_IN;
            op_length = 1;
            target_fd = 0;
        } else {
            i = i + 1;
            continue;
        }
        if (redirect_count >= MAX_REDIRECTS) {
            return -1;
        }
        int token_start = i;
        int scan = i + op_length;
        while (scan < len && segment[scan] == ' ') {
            scan = scan + 1;
        }
        if (scan == len || segment[scan] == '>' || segment[scan] == '<') {
            return -1;
        }
        int name_start = scan;
        while (scan < len && segment[scan] != ' '
               && segment[scan] != '>' && segment[scan] != '<') {
            scan = scan + 1;
        }
        int name_length = scan - name_start;
        if (name_length >= MAX_PATH) {
            return -1;
        }
        char *destination = redirect_names + (redirect_count * MAX_PATH);
        int copy_index = 0;
        while (copy_index < name_length) {
            destination[copy_index] = segment[name_start + copy_index];
            copy_index = copy_index + 1;
        }
        destination[name_length] = '\0';
        redirect_ops[redirect_count] = op;
        redirect_targets[redirect_count] = target_fd;
        redirect_count = redirect_count + 1;
        /* Blank the operator+filename region in the segment. */
        int blank_index = token_start;
        while (blank_index < scan) {
            segment[blank_index] = ' ';
            blank_index = blank_index + 1;
        }
        i = scan;
    }
    return 0;
}

int replace_line(char *buf, int cursor, int end, char *new_content, int new_length) {
    /* Erase the current input area on screen by stepping cursor back
       to col 0 of input, overprinting end with spaces, stepping back,
       then writing new_content.  Returns new_length so the caller can
       update both cursor and end to the returned value.
       Caller guarantees new_length <= MAX_INPUT - 1. */
    cursor_back(cursor);
    int erase_index = 0;
    while (erase_index < end) {
        putchar(' ');
        erase_index = erase_index + 1;
    }
    cursor_back(end);
    memcpy(buf, new_content, new_length);
    if (new_length > 0) {
        write(STDOUT, buf, new_length);
    }
    return new_length;
}

int restore_redirections() {
    /* Reverse-apply: for each saved fd, dup2 it back onto the target
       and close the saved.  Idempotent — saved_fds[i] == -1 means no
       save was taken for that target. */
    int index = 0;
    while (index < 2) {
        if (saved_fds[index] >= 0) {
            dup2(saved_fds[index], index);
            close(saved_fds[index]);
            saved_fds[index] = -1;
        }
        index = index + 1;
    }
    return 0;
}

int strcmp(const char *a, const char *b) {
    int index = 0;
    while (1) {
        if (a[index] != b[index]) {
            return a[index] - b[index];
        }
        if (a[index] == '\0') {
            return 0;
        }
        index += 1;
    }
}

int try_exec(char *name) {
    /* Returns:
         0 — file not found; last_exec_status unchanged.
         1 — file exists but is not executable; last_exec_status unchanged.
         2 — exec succeeded; last_exec_status holds the wait status.
         3 — exec failed with OOM; message printed, last_exec_status set. */
    int rc = exec(name);
    if (rc >= 0) {
        last_exec_status = rc;
        return 2;
    }
    if (-rc == ERROR_NOT_EXECUTE) {
        return 1;
    }
    if (-rc == ERROR_FAULT) {
        printf("exec: out of memory\n");
        last_exec_status = 1 << 8;
        return 3;
    }
    return 0;
}

int visual_bell() {
    printf("\e[48;5;4m");
    sleep(50);
    printf("\e[48;5;0m");
    return 0;
}

int main() {
    /* Ignore SIGINT — the shell prefers to keep its line editor alive when
       the user types Ctrl+C at the prompt.  Cooked 0x03 still arrives in
       the byte stream so the line editor can choose to display ^C and
       reset its input buffer; without SIG_IGN, the kernel-side default is
       to kill the program (which here would mean reloading the shell). */
    asm("mov ebx, SIGINT\n"
        "mov ecx, SIG_IGN\n"
        "mov ah, SYS_SYS_SIGNAL\n"
        "int 30h\n");
    /* Marker print exactly once per shell-load.  Tests assert this line
       appears once across N commands, verifying shell-survives-child. */
    write(STDOUT, "[shell:start]\n", 14);
    char *buf = BUFFER;
    /* exec_path assembles "bin/<name>" for the fallback lookup.  ARGV
       (32 bytes) is unused by the shell since main() takes no args. */
    char *exec_path = ARGV;
    int vga_fd = open("/dev/vga", O_WRONLY);
    int kill_len = 0;
    while (1) {
        write(STDOUT, "$ ", 2);
        int cursor = 0;
        int end = 0;
        while (1) {
            char ch = getchar();
            if (ch == '\x01') {
                /* Ctrl-A: beginning of line */
                if (cursor > 0) {
                    cursor_back(cursor);
                    cursor = 0;
                }
            } else if (ch == '\x02') {
                /* Ctrl-B: cursor left */
                if (cursor > 0) {
                    cursor_back(1);
                    cursor -= 1;
                }
            } else if (ch == '\x03') {
                /* Ctrl-C: cancel line */
                putchar('\n');
                end = 0;
                history_view = 0;
                break;
            } else if (ch == '\x04') {
                /* Ctrl-D: shutdown (returns here only on APM failure) */
                shutdown();
            } else if (ch == '\x05') {
                /* Ctrl-E: end of line */
                write(STDOUT, buf + cursor, end - cursor);
                cursor = end;
            } else if (ch == '\x06') {
                /* Ctrl-F: cursor right */
                if (cursor < end) {
                    putchar(buf[cursor]);
                    cursor += 1;
                }
            } else if (ch == '\b' || ch == '\x7F') {
                /* Backspace / DEL */
                if (cursor > 0) {
                    cursor_back(1);
                    cursor -= 1;
                    end = delete_at_cursor(buf, cursor, end);
                }
            } else if (ch == '\x0B') {
                /* Ctrl-K: kill to end of line */
                if (cursor < end) {
                    int span = end - cursor;
                    if (span > MAX_INPUT) {
                        span = MAX_INPUT;
                    }
                    kill_len = span;
                    int copy_index = 0;
                    while (copy_index < span) {
                        kill_buf[copy_index] = buf[cursor + copy_index];
                        copy_index += 1;
                    }
                    int erase_index = 0;
                    while (erase_index < span) {
                        putchar(' ');
                        erase_index += 1;
                    }
                    cursor_back(span);
                    end = cursor;
                }
            } else if (ch == '\x0C') {
                /* Ctrl-L: clear screen and reprompt */
                video_mode(vga_fd, VIDEO_MODE_TEXT_80x25);
                end = 0;
                history_view = 0;
                break;
            } else if (ch == '\x0E') {
                /* Ctrl-N: history down (alias of Down arrow). */
                end = history_down(buf, cursor, end);
                cursor = end;
            } else if (ch == '\x10') {
                /* Ctrl-P: history up (alias of Up arrow). */
                end = history_up(buf, cursor, end);
                cursor = end;
            } else if (ch == '\n') {
                /* Enter — fd_read_console normalises CR → LF on input
                 * (PS/2 Enter scancode and serial-terminal CR both
                 * land here as LF). */
                putchar('\n');
                break;
            } else if (ch == '\x19') {
                /* Ctrl-Y: yank from kill buffer */
                int yank_index = 0;
                while (yank_index < kill_len) {
                    if (end >= MAX_INPUT) {
                        visual_bell();
                        break;
                    }
                    end = insert_char(buf, cursor, end, kill_buf[yank_index]);
                    cursor += 1;
                    yank_index += 1;
                }
            } else if (ch == '\x1B') {
                /* CSI escape — consume "[" + parameter bytes + final.
                   Recognised: [A (Up) and [B (Down) for history recall.
                   Other CSI codes (including xterm modified arrows
                   like [1;2A from Shift+Up on serial) are silently
                   discarded so the line editor stays untouched. */
                char escape_next = getchar();
                if (escape_next == '[') {
                    char final_byte = getchar();
                    while (final_byte >= '0' && final_byte <= '?') {
                        final_byte = getchar();
                    }
                    if (final_byte == 'A') {
                        end = history_up(buf, cursor, end);
                        cursor = end;
                    } else if (final_byte == 'B') {
                        end = history_down(buf, cursor, end);
                        cursor = end;
                    }
                }
            } else if (ch >= ' ') {
                /* Printable char — insert at cursor */
                if (end >= MAX_INPUT) {
                    visual_bell();
                } else {
                    end = insert_char(buf, cursor, end, ch);
                    cursor += 1;
                }
            }
        }
        buf[end] = 0;
        /* Push to history before dispatch.  Skip empty lines and
           consecutive duplicates (bash default).  Reset history_view
           for the next prompt. */
        if (end > 0) {
            int previous_slot = (history_count - 1) % HISTORY_SIZE;
            int is_duplicate = 0;
            if (history_count > 0 && strcmp(buf, history + (previous_slot * MAX_INPUT)) == 0) {
                is_duplicate = 1;
            }
            if (is_duplicate == 0) {
                int slot = history_count % HISTORY_SIZE;
                char *entry = history + (slot * MAX_INPUT);
                memcpy(entry, buf, end);
                entry[end] = '\0';
                history_count = history_count + 1;
            }
        }
        history_view = 0;
        if (end == 0) {
            continue;
        }
        /* Tokenize the line into chained segments (`;`, `&&`, `||`),
           then dispatch each in BUFFER one-at-a-time.  chain_buf holds
           the parsed copy so per-segment $? expansion in BUFFER does
           not corrupt segments not yet processed. */
        memcpy(chain_buf, buf, end + 1);
        int n_segments = parse_chain(chain_buf);
        if (n_segments < 0) {
            printf("too many commands in chain\n");
            continue;
        }
        int seg_index = 0;
        while (seg_index < n_segments) {
            int run = 1;
            if (seg_index > 0) {
                int prev_op = segment_ops[seg_index - 1];
                if (prev_op == OP_AND) {
                    run = (last_exec_status == 0);
                } else if (prev_op == OP_OR) {
                    run = (last_exec_status != 0);
                }
            }
            char *segment = chain_buf + segment_offsets[seg_index];
            if (run && segment[0] != '\0') {
                int seg_len = strlen(segment);
                memcpy(buf, segment, seg_len + 1);
                /* Pipeline check: detect `cmd1 | cmd2` before touching
                   redirection globals.  parse_chain already consumed `||`
                   as OP_OR, so any remaining lone `|` is a pipe operator. */
                int pipe_at = find_top_level_pipe(buf);
                if (pipe_at == -2) {
                    write(STDOUT, "shell: pipelines support only one |\n", 36);
                    last_exec_status = 1 << 8;
                } else if (pipe_at >= 0) {
                    /* Copy left side into pipe_left_buf and trim trailing
                       spaces. */
                    int pi = 0;
                    while (pi < pipe_at) {
                        pipe_left_buf[pi] = buf[pi];
                        pi += 1;
                    }
                    while (pi > 0 && pipe_left_buf[pi - 1] == ' ') {
                        pi -= 1;
                    }
                    pipe_left_buf[pi] = 0;
                    /* Copy right side into pipe_right_buf, skip leading
                       spaces. */
                    pi = pipe_at + 1;
                    while (buf[pi] == ' ') {
                        pi += 1;
                    }
                    int rj = 0;
                    while (buf[pi] != '\0') {
                        pipe_right_buf[rj] = buf[pi];
                        pi += 1;
                        rj += 1;
                    }
                    pipe_right_buf[rj] = 0;
                    /* Reject redirect on either side (v1 limitation). */
                    if (contains_redirect_token(pipe_left_buf) != 0 ||
                        contains_redirect_token(pipe_right_buf) != 0) {
                        write(STDOUT, "shell: pipes cannot combine with < > >>\n", 40);
                        last_exec_status = 1 << 8;
                    } else {
                        /* Build bin/-prefixed paths into pipe_left_path and
                           pipe_right_path.  Mirror the existing exec_path
                           pattern in dispatch_buffer. */
                        pipe_left_path[0] = 'b';
                        pipe_left_path[1] = 'i';
                        pipe_left_path[2] = 'n';
                        pipe_left_path[3] = '/';
                        int ci = 0;
                        while (pipe_left_buf[ci] != '\0' && ci < MAX_PATH - 5) {
                            pipe_left_path[4 + ci] = pipe_left_buf[ci];
                            ci += 1;
                        }
                        pipe_left_path[4 + ci] = 0;
                        pipe_right_path[0] = 'b';
                        pipe_right_path[1] = 'i';
                        pipe_right_path[2] = 'n';
                        pipe_right_path[3] = '/';
                        ci = 0;
                        while (pipe_right_buf[ci] != '\0' && ci < MAX_PATH - 5) {
                            pipe_right_path[4 + ci] = pipe_right_buf[ci];
                            ci += 1;
                        }
                        pipe_right_path[4 + ci] = 0;
                        int rc = pipeline2(pipe_left_path, pipe_right_path);
                        if (rc < 0) {
                            write(STDOUT, "shell: pipeline failed\n", 23);
                            last_exec_status = -rc;
                        } else {
                            last_exec_status = rc;
                        }
                    }
                } else {
                /* Strip redirections out of buf and into the redirect_*
                   globals BEFORE dispatch_buffer's first-space split looks
                   at the cmd name.  Parse errors short-circuit dispatch. */
                if (parse_redirections(buf) < 0) {
                    printf("redirection syntax error\n");
                    last_exec_status = 1 << 8;
                } else if (apply_redirections() == 0) {
                    dispatch_buffer(buf, exec_path);
                    restore_redirections();
                }
                /* On apply_redirections failure last_exec_status is set; nothing
                   to restore (apply rolls back its own saves on the failure path). */
                }
            }
            seg_index += 1;
        }
    }
}
