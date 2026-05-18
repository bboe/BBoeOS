#include "shell_lex.h"
#include "wait.h"

#define HISTORY_SIZE 16
#define MAX_TOKENS MAX_INPUT

/* The line lexer (shell_lex.h) writes its output into these three
   parallel arrays at the top of every dispatch.  token_kinds carries
   the TOKEN_* stream (terminated by TOKEN_EOF); token_word_offsets
   carries the byte offset into word_buffer for each TOKEN_WORD entry;
   word_buffer is the packed null-terminated word storage that argv
   pointers and redirect filenames index into.  Sized to MAX_INPUT so
   the worst-case all-one-character input still fits. */
int token_kinds[MAX_TOKENS];
int token_word_offsets[MAX_TOKENS];
char word_buffer[MAX_INPUT];

/* Per-command scratch for $? expansion.  Built fresh for each command
   from the WORD tokens that belong to it; argv pointers (and redirect
   filename pointers) hand into this buffer rather than word_buffer so
   $? substitution can grow words without disturbing the lex output.
   Sized 2× MAX_INPUT — each $? expands from 2 bytes to at most 4 (one
   sign byte plus up to three digits), so doubling the input bound is a
   safe ceiling. */
char expanded_buffer[MAX_INPUT * 2];

/* Redirection state for one command.  Filled while walking a command's
   tokens; consumed by apply_redirections.  Each filename is
   null-terminated and lives in redirect_names; the redirect entries
   point in via byte offsets.  Operator kinds: IN = `<` ; OUT = `>`
   (truncate); APPND = `>>` (append).  ``NONE`` is the
   default-uninitialised value — it should never reach
   apply_redirections; if a fourth operator (e.g. heredoc `<<`,
   fd-explicit `2>`) lands it has to be added to the enum so the
   switch in apply_redirections trips the exhaustiveness check. */
#define MAX_REDIRECTS 3

enum RedirectOp {
    REDIRECT_OP_APPND,
    REDIRECT_OP_IN,
    REDIRECT_OP_NONE,
    REDIRECT_OP_OUT,
};

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

/* Live input line buffer — the line editor writes characters here as
   the user types; after Enter the whole line is handed straight to
   lex_line, which fills the token arrays above for the dispatch walk.
   Used to live at the static user-data frame (USER_DATA_BASE+0x500 =
   0x1500, named BUFFER) shared by every program, but the EXEC_ARG
   handoff was the only cross-program use and is gone — so this is now
   plain shell-private .bss. */
char input_buf[MAX_INPUT];

/* Ctrl-K kill buffer.  File-scope so it lands in the program's BSS;
   per-program PDs do not alias the low 1 MB so a fixed-address scratch
   like SECTOR_BUFFER would page-fault. */
char kill_buf[MAX_INPUT];

/* Wait status of the most recently exec()'d child.  Written by
   try_exec() on a successful exec; read by the dispatch loop and
   expand_word() to expose $? to the user. */
int last_exec_status;

/* Snapshot of the live edit line taken on the first Up keypress.
   Restored when Down walks back past the newest history entry to
   history_view == 0, matching bash's partial-line restore behaviour. */
char saved_line[MAX_INPUT];
int saved_line_length;

/* Scratch buffers for the pipeline parser (`cmd1 | cmd2`).  File-scope
   so that (a) cc.py passes their addresses correctly on function calls
   and (b) they live in BSS without consuming additional user stack.
   pipe_left_name / pipe_right_name hold the bare basenames (for
   building the bin/-prefixed path);
   pipe_left_path / pipe_right_path hold the bin/-prefixed paths;
   pipe_left_argv / pipe_right_argv are NULL-terminated char** arrays
   pointing into expanded_buffer (the per-command $?-expanded word
   scratch).  Passed through SYS_SYS_PIPELINE2 to the kernel, which
   validates each array under the shell's PD and then, during each
   child's PD build, reads the strings back through the shell's
   mappings and writes them into the new program's stack frame via a
   kmap alias — building the Linux SysV i386 startup frame in place
   with no kernel-side scratch. */
char *pipe_left_argv[MAX_ARGV_ENTRIES + 1];
char pipe_left_name[MAX_INPUT];
char pipe_left_path[MAX_PATH];
char *pipe_right_argv[MAX_ARGV_ENTRIES + 1];
char pipe_right_name[MAX_INPUT];
char pipe_right_path[MAX_PATH];

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
        enum RedirectOp op = redirect_ops[index];
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
        switch (op) {
        case REDIRECT_OP_IN:
            flags = O_RDONLY;
            break;
        case REDIRECT_OP_OUT:
            flags = O_WRONLY | O_CREAT | O_TRUNC;
            break;
        case REDIRECT_OP_APPND:
            /* Open without O_TRUNC; the post-open seek below moves to
               EOF so writes append. */
            flags = O_WRONLY | O_CREAT;
            break;
        case REDIRECT_OP_NONE:
            /* NONE is the unset sentinel — build_command_argv only
               writes IN/OUT/APPND, so NONE here is a producer bug. */
            printf("internal: NONE redirect op reached apply_redirections\n");
            last_exec_status = 1 << 8;
            restore_redirections();
            return -1;
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

/* build_bin_path — produce ``bin/<name>`` in *path_out* (capacity
   MAX_PATH), used by execute_pipeline to feed pipeline2 the
   bin/-prefixed paths the kernel exec layer expects.  Truncates names
   that would not fit. */
void build_bin_path(char *name, char *path_out) {
    path_out[0] = 'b';
    path_out[1] = 'i';
    path_out[2] = 'n';
    path_out[3] = '/';
    int i = 0;
    while (name[i] != '\0' && i < MAX_PATH - 5) {
        path_out[4 + i] = name[i];
        i += 1;
    }
    path_out[4 + i] = '\0';
}

/* Forward decl: build_command_argv calls expand_word, which sorts
   later. */
int expand_word(char *src, char *dst, int max_len);

/* build_command_argv — walk tokens [start, end) (one command's worth)
   and materialise its argv into *argv_out* with words drawn from
   *expanded_out* (a per-command slice of expanded_buffer that this
   function packs).  Returns the new write cursor inside expanded_out
   on success, or -1 on:
     - any TOKEN_REDIRECT_* when ``allow_redirects`` is zero,
     - argv overflow (> MAX_ARGV_ENTRIES),
     - redirect overflow (> MAX_REDIRECTS),
     - missing filename after a redirect operator,
     - $? expansion overflow,
     - command-name (>= MAX_PATH bytes) used by the bin/ fallback.

   When ``allow_redirects`` is nonzero, REDIRECT_* + WORD pairs append
   to the redirect_* globals.  When zero, the v1 pipeline limitation
   rejects them.  Returns the *next byte index inside expanded_out
   that is free* (caller-relative).  Sets *argc_out* to the argv count
   (excluding the NULL terminator). */
int build_command_argv(int start, int end, char **argv_out, int *argc_out,
                       char *expanded_out, int expanded_max, int allow_redirects) {
    int argc = 0;
    int write = 0;
    int token_index = start;
    while (token_index < end) {
        enum TokenKind kind = token_kinds[token_index];
        switch (kind) {
        case TOKEN_WORD:
            if (argc >= MAX_ARGV_ENTRIES) {
                printf("too many arguments\n");
                return -1;
            }
            char *word_src = word_buffer + token_word_offsets[token_index];
            int word_written = expand_word(word_src, expanded_out + write,
                                           expanded_max - write);
            if (word_written < 0) {
                printf("$? expansion exceeded buffer\n");
                return -1;
            }
            argv_out[argc] = expanded_out + write;
            argc += 1;
            write += word_written + 1;   /* skip past the NUL */
            token_index += 1;
            break;
        case TOKEN_REDIRECT_IN:
        case TOKEN_REDIRECT_OUT:
        case TOKEN_REDIRECT_APPEND:
            if (allow_redirects == 0) {
                printf("shell: pipes cannot combine with < > >>\n");
                return -1;
            }
            if (redirect_count >= MAX_REDIRECTS) {
                printf("too many redirections\n");
                return -1;
            }
            if (token_index + 1 >= end || token_kinds[token_index + 1] != TOKEN_WORD) {
                printf("redirection syntax error\n");
                return -1;
            }
            int op;
            int target_fd;
            switch (kind) {
            case TOKEN_REDIRECT_IN:
                op = REDIRECT_OP_IN;
                target_fd = 0;
                break;
            case TOKEN_REDIRECT_OUT:
                op = REDIRECT_OP_OUT;
                target_fd = 1;
                break;
            case TOKEN_REDIRECT_APPEND:
                op = REDIRECT_OP_APPND;
                target_fd = 1;
                break;
            default:
                /* Outer switch already filtered to these three. */
                op = REDIRECT_OP_NONE;
                target_fd = 0;
                break;
            }
            char *name_src = word_buffer + token_word_offsets[token_index + 1];
            char *destination = redirect_names + (redirect_count * MAX_PATH);
            int name_written = expand_word(name_src, destination, MAX_PATH);
            if (name_written < 0) {
                printf("redirect filename too long\n");
                return -1;
            }
            redirect_ops[redirect_count] = op;
            redirect_targets[redirect_count] = target_fd;
            redirect_count += 1;
            token_index += 2;
            break;
        case TOKEN_AND:
        case TOKEN_EOF:
        case TOKEN_OR:
        case TOKEN_PIPE:
        case TOKEN_SEMI:
            /* Segment / pipeline terminators are consumed by the caller;
               reaching one here means the loop bounds were wrong. */
            printf("internal: unexpected token in command\n");
            return -1;
        }
    }
    argv_out[argc] = 0;
    *argc_out = argc;
    return write;
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

/* Forward decl: dispatch_command calls try_exec, which sorts later. */
int try_exec(char *name, char **argv);

/* dispatch_* scratch buffers — file-scope so they stay live for
   try_exec() without consuming user stack on every call.
   dispatch_argv is the NULL-terminated char** array (built from
   already-expanded words in expanded_buffer) handed to exec(); the
   kernel walks it under the shell's PD when staging the new program's
   user-stack argv frame.  dispatch_bin holds the `bin/<name>` fallback
   path. */
char *dispatch_argv[MAX_ARGV_ENTRIES + 1];
char dispatch_bin[MAX_PATH];

void dispatch_command(int argc, char **argv) {
    /* Run a single already-tokenised, $?-expanded command.  Updates
       last_exec_status so that chain operators (&&, ||) and subsequent
       $? expansions see the result.  argv[0] is the program name; the
       caller has already applied any redirects via apply_redirections. */
    if (argc == 0) {
        return;
    }
    char *name = argv[0];
    if (strcmp(name, "help") == 0) {
        printf("Commands: help reboot shutdown\n");
        last_exec_status = 0;
        return;
    } else if (strcmp(name, "reboot") == 0) {
        reboot();   /* noreturn */
    } else if (strcmp(name, "shutdown") == 0) {
        shutdown();
        printf("APM shutdown failed\n");
        last_exec_status = 1 << 8;
        return;
    }
    int result = try_exec(name, argv);
    if (result == 1) {
        last_exec_status = 126 << 8;   /* bash: not executable */
        printf("not executable\n");
        return;
    }
    if (result != 0) {
        return;
    }
    /* Not found in root — retry inside bin/ */
    dispatch_bin[0] = 'b';
    dispatch_bin[1] = 'i';
    dispatch_bin[2] = 'n';
    dispatch_bin[3] = '/';
    int copy_index = 0;
    while (name[copy_index] != '\0' && copy_index < MAX_PATH - 5) {
        dispatch_bin[4 + copy_index] = name[copy_index];
        copy_index += 1;
    }
    dispatch_bin[4 + copy_index] = '\0';
    int bin_result = try_exec(dispatch_bin, argv);
    if (bin_result == 1) {
        last_exec_status = 126 << 8;
        printf("not executable\n");
    } else if (bin_result == 0) {
        last_exec_status = 127 << 8;
        printf("unknown command\n");
    }
}

/* Forward decl: execute_line calls execute_pipeline, which sorts
   later. */
void execute_pipeline(int start, int end);

/* execute_line — top-level driver.  Lex *line* once, then walk the
   token stream by segment (everything between SEMI/AND/OR or EOF),
   honoring && / || short-circuit against last_exec_status.  Each
   non-skipped segment is handed to execute_pipeline. */
void execute_line(char *line) {
    int token_count = lex_line(line, token_kinds, token_word_offsets,
                               word_buffer, MAX_TOKENS, sizeof(word_buffer));
    if (token_count < 0) {
        printf("line too complex\n");
        last_exec_status = 1 << 8;
        return;
    }
    int chain_op = TOKEN_SEMI;   /* First segment runs unconditionally. */
    int token_index = 0;
    while (token_kinds[token_index] != TOKEN_EOF) {
        int segment_start = token_index;
        while (token_kinds[token_index] != TOKEN_EOF
               && token_kinds[token_index] != TOKEN_SEMI
               && token_kinds[token_index] != TOKEN_AND
               && token_kinds[token_index] != TOKEN_OR) {
            token_index += 1;
        }
        int segment_end = token_index;
        int run = 1;
        if (chain_op == TOKEN_AND) {
            run = (last_exec_status == 0);
        } else if (chain_op == TOKEN_OR) {
            run = (last_exec_status != 0);
        }
        if (run && segment_end > segment_start) {
            execute_pipeline(segment_start, segment_end);
        }
        if (token_kinds[token_index] == TOKEN_EOF) {
            break;
        }
        chain_op = token_kinds[token_index];
        token_index += 1;
    }
}

/* execute_pipeline — run tokens [start, end) as a single pipeline.
   Splits on TOKEN_PIPE: one command falls through to dispatch_command
   with redirects; two commands call pipeline2 (redirects on either
   side are rejected per the v1 limitation).  More than two commands
   are rejected.  Updates last_exec_status. */
void execute_pipeline(int start, int end) {
    int first_pipe = -1;
    int pipe_count = 0;
    int token_index = start;
    while (token_index < end) {
        if (token_kinds[token_index] == TOKEN_PIPE) {
            if (first_pipe < 0) {
                first_pipe = token_index;
            }
            pipe_count += 1;
        }
        token_index += 1;
    }
    if (pipe_count > 1) {
        printf("shell: pipelines support only one |\n");
        last_exec_status = 1 << 8;
        return;
    }
    if (pipe_count == 0) {
        /* Single command — redirects allowed. */
        redirect_count = 0;
        int argc;
        int written = build_command_argv(start, end, dispatch_argv, &argc,
                                         expanded_buffer, sizeof(expanded_buffer), 1);
        if (written < 0) {
            last_exec_status = 1 << 8;
            return;
        }
        if (argc == 0) {
            return;
        }
        if (apply_redirections() == 0) {
            dispatch_command(argc, dispatch_argv);
            restore_redirections();
        }
        return;
    }
    /* Two-command pipeline.  Left tokens [start, first_pipe),
       right tokens (first_pipe, end).  Redirects rejected on
       either side. */
    redirect_count = 0;
    int left_argc;
    int left_written = build_command_argv(start, first_pipe,
                                          pipe_left_argv, &left_argc,
                                          expanded_buffer, sizeof(expanded_buffer) / 2, 0);
    if (left_written < 0) {
        last_exec_status = 1 << 8;
        return;
    }
    int right_argc;
    int right_written = build_command_argv(first_pipe + 1, end,
                                           pipe_right_argv, &right_argc,
                                           expanded_buffer + sizeof(expanded_buffer) / 2,
                                           sizeof(expanded_buffer) / 2, 0);
    if (right_written < 0) {
        last_exec_status = 1 << 8;
        return;
    }
    if (left_argc == 0 || right_argc == 0) {
        printf("shell: pipeline side is empty\n");
        last_exec_status = 1 << 8;
        return;
    }
    /* Copy the unprefixed names (argv[0]) into pipe_*_name and build
       the bin/-prefixed paths the kernel needs. */
    int copy_index = 0;
    while (pipe_left_argv[0][copy_index] != '\0' && copy_index < MAX_INPUT - 1) {
        pipe_left_name[copy_index] = pipe_left_argv[0][copy_index];
        copy_index += 1;
    }
    pipe_left_name[copy_index] = '\0';
    copy_index = 0;
    while (pipe_right_argv[0][copy_index] != '\0' && copy_index < MAX_INPUT - 1) {
        pipe_right_name[copy_index] = pipe_right_argv[0][copy_index];
        copy_index += 1;
    }
    pipe_right_name[copy_index] = '\0';
    build_bin_path(pipe_left_name, pipe_left_path);
    build_bin_path(pipe_right_name, pipe_right_path);
    int rc = pipeline2(pipe_left_path, pipe_left_argv,
                       pipe_right_path, pipe_right_argv);
    if (rc < 0) {
        write(STDOUT, "shell: pipeline failed\n", 23);
        last_exec_status = -rc;
    } else {
        last_exec_status = rc;
    }
}

/* expand_word — copy *src* into *dst* (capacity *max_len* including
   the trailing NUL), replacing every literal ``$?`` with the bash-
   shaped last-exit-status digits.  Returns the destination length
   (excluding NUL), or -1 if the expansion would overflow.  Used by
   build_command_argv to materialise per-command argv entries into
   expanded_buffer. */
int expand_word(char *src, char *dst, int max_len) {
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
    int read_index = 0;
    int write_index = 0;
    while (src[read_index] != '\0') {
        if (src[read_index] == '$' && src[read_index + 1] == '?') {
            if (write_index + digit_count >= max_len) {
                return -1;
            }
            int reverse_index = digit_count - 1;
            while (reverse_index >= 0) {
                dst[write_index] = digits[reverse_index];
                write_index += 1;
                reverse_index -= 1;
            }
            read_index += 2;
        } else {
            if (write_index >= max_len - 1) {
                return -1;
            }
            dst[write_index] = src[read_index];
            write_index += 1;
            read_index += 1;
        }
    }
    dst[write_index] = '\0';
    return write_index;
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

int insert_char(char *buf, int cursor, int end, char character) {
    /* Shift buf[cursor..end) right one slot, write character at cursor, redraw
       tail, and reposition cursor.  Returns the new end index. Caller
       guarantees end < MAX_INPUT. */
    int shift = end;
    while (shift > cursor) {
        buf[shift] = buf[shift - 1];
        shift -= 1;
    }
    buf[cursor] = character;
    end += 1;
    write(STDOUT, buf + cursor, end - cursor);
    cursor_back(end - cursor - 1);
    return end;
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

int try_exec(char *name, char **argv) {
    /* Returns:
         0 — file not found; last_exec_status unchanged.
         1 — file exists but is not executable; last_exec_status unchanged.
         2 — exec succeeded; last_exec_status holds the wait status.
         3 — exec failed with OOM; message printed, last_exec_status set. */
    int rc = exec(name, argv);
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
    char *buf = input_buf;
    int vga_fd = open("/dev/vga", O_WRONLY);
    int kill_len = 0;
    while (1) {
        write(STDOUT, "$ ", 2);
        int cursor = 0;
        int end = 0;
        while (1) {
            char character = getchar();
            if (character == '\x01') {
                /* Ctrl-A: beginning of line */
                if (cursor > 0) {
                    cursor_back(cursor);
                    cursor = 0;
                }
            } else if (character == '\x02') {
                /* Ctrl-B: cursor left */
                if (cursor > 0) {
                    cursor_back(1);
                    cursor -= 1;
                }
            } else if (character == '\x03') {
                /* Ctrl-C: cancel line */
                putchar('\n');
                end = 0;
                history_view = 0;
                break;
            } else if (character == '\x04') {
                /* Ctrl-D: shutdown (returns here only on APM failure) */
                shutdown();
            } else if (character == '\x05') {
                /* Ctrl-E: end of line */
                write(STDOUT, buf + cursor, end - cursor);
                cursor = end;
            } else if (character == '\x06') {
                /* Ctrl-F: cursor right */
                if (cursor < end) {
                    putchar(buf[cursor]);
                    cursor += 1;
                }
            } else if (character == '\b' || character == '\x7F') {
                /* Backspace / DEL */
                if (cursor > 0) {
                    cursor_back(1);
                    cursor -= 1;
                    end = delete_at_cursor(buf, cursor, end);
                }
            } else if (character == '\x0B') {
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
            } else if (character == '\x0C') {
                /* Ctrl-L: clear screen and reprompt */
                video_mode(vga_fd, VIDEO_MODE_TEXT_80x25);
                end = 0;
                history_view = 0;
                break;
            } else if (character == '\x0E') {
                /* Ctrl-N: history down (alias of Down arrow). */
                end = history_down(buf, cursor, end);
                cursor = end;
            } else if (character == '\x10') {
                /* Ctrl-P: history up (alias of Up arrow). */
                end = history_up(buf, cursor, end);
                cursor = end;
            } else if (character == '\n') {
                /* Enter — fd_read_console normalises CR → LF on input
                 * (PS/2 Enter scancode and serial-terminal CR both
                 * land here as LF). */
                putchar('\n');
                break;
            } else if (character == '\x19') {
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
            } else if (character == '\x1B') {
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
            } else if (character >= ' ') {
                /* Printable char — insert at cursor */
                if (end >= MAX_INPUT) {
                    visual_bell();
                } else {
                    end = insert_char(buf, cursor, end, character);
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
        /* Drive the whole line through the lex/parse/execute pipeline.
           execute_line walks the token stream once: it segments on
           SEMI/AND/OR (honouring && / || short-circuit), splits each
           segment on PIPE (max one pipe per segment), expands $? per
           word, applies redirects on single-command segments, and
           updates last_exec_status. */
        execute_line(buf);
    }
}
