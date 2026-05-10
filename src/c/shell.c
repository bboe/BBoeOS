/* Ctrl-K kill buffer.  File-scope so it lands in the program's BSS;
   pre-Phase-4 the shell stashed it inside SECTOR_BUFFER (phys 0xF000)
   reachable through the shim's identity user mapping, but per-program
   PDs no longer alias the low 1 MB so a stale fixed-address access
   would page-fault. */
char kill_buf[MAX_INPUT];

/* Wait status of the most recently exec()'d child.  Written by
   try_exec() on a successful exec; read by the dispatch loop and
   expand_dollar_question() to expose $? to the user. */
int last_exec_status;

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

int cursor_back(int count) {
    if (count > 0) {
        printf("\e[%dD", count);
    }
    return 0;
}

int visual_bell() {
    printf("\e[48;5;4m");
    sleep(50);
    printf("\e[48;5;0m");
    return 0;
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

int expand_dollar_question(char *buffer, int max_len) {
    /* In-place replace every "$?" in buffer with the decimal representation
       of the bash-shaped last status:
         normal exit  -> WEXITSTATUS  (bits 15..8 of last_exec_status)
         signal kill  -> 128 + WTERMSIG (low 7 bits)
       Returns the new length, or -1 if the expansion would exceed max_len. */
    int bash_status;
    int signum = last_exec_status & 0x7F;
    if (signum == 0) {
        bash_status = (last_exec_status >> 8) & 0xFF;
    } else {
        bash_status = 128 + signum;
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

int try_exec(char *name) {
    /* Returns:
         0 — file not found (or other error); last_exec_status unchanged.
         1 — file exists but is not executable; last_exec_status unchanged.
         2 — exec succeeded; last_exec_status holds the wait status. */
    int rc = exec(name);
    if (rc >= 0) {
        last_exec_status = rc;
        return 2;
    }
    if (-rc == ERROR_NOT_EXECUTE) {
        return 1;
    }
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
                break;
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
        if (end == 0) {
            continue;
        }
        /* Split command name and argument at the first space */
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
                continue;
            }
        }
        if (strcmp(buf, "help") == 0) {
            printf("Commands: help reboot shutdown\n");
        } else if (strcmp(buf, "reboot") == 0) {
            reboot();
        } else if (strcmp(buf, "shutdown") == 0) {
            shutdown();
            printf("APM shutdown failed\n");
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
                    last_exec_status = 126 << 8;   /* bash: not executable */
                    printf("not executable\n");
                } else if (bin_result == 0) {
                    last_exec_status = 127 << 8;   /* bash: command not found */
                    printf("unknown command\n");
                }
            }
        }
    }
}
