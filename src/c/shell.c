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

int try_exec(char *name) {
    /* Returns 1 if the target exists but is not executable, 0 if it
       was not found at all.  On success SYS_EXEC transfers control and
       never returns. */
    int err = exec(name);
    if (err == ERROR_NOT_EXECUTE) {
        return 1;
    }
    return 0;
}

int main() {
    char *buf = BUFFER;
    /* kill_buf lives inside SECTOR_BUFFER past the 1-byte scratch slot
       that FUNCTION_GET_CHARACTER writes into on every keypress. */
    char *kill_buf = SECTOR_BUFFER + 4;
    /* exec_path assembles "bin/<name>" for the fallback lookup.  ARGV
       (32 bytes) is unused by the shell since main() takes no args,
       and sits outside SECTOR_BUFFER so the directory-sector read
       during find_file doesn't clobber the path mid-lookup. */
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
        }
        if (strcmp(buf, "help") == 0) {
            printf("Commands: help reboot shutdown\n");
        } else if (strcmp(buf, "reboot") == 0) {
            reboot();
        } else if (strcmp(buf, "shutdown") == 0) {
            shutdown();
            printf("APM shutdown failed\n");
        } else if (try_exec(buf)) {
            printf("not executable\n");
        } else {
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
            if (try_exec(exec_path)) {
                printf("not executable\n");
            } else {
                printf("unknown command\n");
            }
        }
    }
}
