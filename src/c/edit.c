/* gap_start / gap_end are file-scope so the cursor-move helpers can
   mutate them directly.  Text logically occupies [0, gap_start) and
   [gap_end, EDIT_BUFFER_SIZE); the gap sits between. */
int gap_start;
int gap_end;

int buffer_character_at(char *buffer, int offset) {
    /* Gap-buffer lookup: map a logical offset to the raw byte.  Returns
       -1 when the offset is past the end of the text. */
    int gap_size = gap_end - gap_start;
    int length = EDIT_BUFFER_SIZE - gap_size;
    if (offset >= length) {
        return -1;
    }
    if (offset >= gap_start) {
        offset += gap_size;
    }
    return buffer[offset];
}

int column_before(char *buffer) {
    /* Count characters between gap_start and the previous newline (or
       start of buffer).  Used to recompute cursor_column after the
       cursor crosses a newline. */
    int column = 0;
    int i = gap_start;
    while (i > 0) {
        i -= 1;
        if (buffer[i] == '\n') {
            return column;
        }
        column += 1;
    }
    return column;
}

/* Move one character from the left side of the gap into the right
   side (cursor steps backward).  Returns the moved character for the
   caller's break / state updates.  Assumes gap_start > 0. */
int gap_move_left() {
    char *buffer = EDIT_BUFFER_BASE;
    buffer[gap_end - 1] = buffer[gap_start - 1];
    gap_start -= 1;
    gap_end -= 1;
    return buffer[gap_end];
}

/* Dual of gap_move_left — move one character from the right side into
   the left (cursor steps forward).  Assumes gap_end < EDIT_BUFFER_SIZE. */
int gap_move_right() {
    char *buffer = EDIT_BUFFER_BASE;
    buffer[gap_start] = buffer[gap_end];
    gap_start += 1;
    gap_end += 1;
    return buffer[gap_start - 1];
}

int main(int argc, char *argv[]) {
    if (argc != 1) {
        die("Usage: edit <filename>\n");
    }
    char *buffer = EDIT_BUFFER_BASE;
    char *kill_buf = EDIT_KILL_BUFFER;
    char *filename = argv[0];
    gap_start = 0;
    gap_end = EDIT_BUFFER_SIZE;
    int cursor_line = 0;
    int cursor_column = 0;
    int view_line = 0;
    int view_column = 0;
    int kill_length = 0;
    int dirty = 0;
    int confirm_quit = 0;
    char *status_message = NULL;
    char sector[512];

    int vga_fd = open("/dev/vga", O_WRONLY);
    int fd = open(filename, O_RDONLY);
    if (fd >= 0) {
        if (fstat(fd) & FLAG_DIRECTORY) {
            close(fd);
            die("Is a directory\n");
        }
        int bytes = read(fd, buffer, EDIT_BUFFER_SIZE);
        if (bytes < 0) {
            close(fd);
            die("Load error\n");
        }
        /* Detect overflow by attempting one more byte past the buffer. */
        int extra = read(fd, kill_buf, 1);
        close(fd);
        if (extra > 0) {
            die("File too large for edit buffer\n");
        }
        /* Relocate content from [0, bytes) to [BUFFER_SIZE - bytes,
           BUFFER_SIZE) so gap_start=0 puts the cursor at file start.
           Copy from the top down so overlapping ranges are safe. */
        int src = bytes - 1;
        int dest = EDIT_BUFFER_SIZE - 1;
        while (src >= 0) {
            buffer[dest] = buffer[src];
            dest -= 1;
            src -= 1;
        }
        gap_end = EDIT_BUFFER_SIZE - bytes;
    }

    while (1) {
        /* ---- Render ---- */
        video_mode(vga_fd, VIDEO_MODE_TEXT_80x25);

        /* Walk to the start of view_line by counting newlines. */
        int offset = 0;
        int lines_to_skip = view_line;
        while (lines_to_skip > 0) {
            int character = buffer_character_at(buffer, offset);
            if (character < 0) {
                break;
            }
            offset += 1;
            if (character == 10) {
                lines_to_skip -= 1;
            }
        }

        /* Horizontal scroll: keep cursor_column in view. */
        if (cursor_column < view_column) {
            view_column = cursor_column;
        } else if (cursor_column >= view_column + 80) {
            view_column = cursor_column - 79;
        }

        /* Print up to 24 rows. */
        int rows_remaining = 24;
        int at_eof = 0;
        while (rows_remaining > 0 && at_eof == 0) {
            int skip = view_column;
            int visible = 80;
            while (1) {
                int character = buffer_character_at(buffer, offset);
                if (character < 0) {
                    at_eof = 1;
                    break;
                }
                offset += 1;
                if (character == 10) {
                    if (visible != 0) {
                        putchar('\n');
                    }
                    rows_remaining -= 1;
                    break;
                }
                if (skip > 0) {
                    skip -= 1;
                } else if (visible > 0) {
                    putchar(character);
                    visible -= 1;
                }
            }
            if (at_eof != 0 && visible != 80) {
                /* Partial row at EOF: consume a display row; emit \n
                   unless a full row already wrapped the cursor. */
                rows_remaining -= 1;
                if (visible != 0) {
                    putchar('\n');
                }
            }
        }
        /* Pad remaining rows with \n to land on row 25. */
        while (rows_remaining > 0) {
            putchar('\n');
            rows_remaining -= 1;
        }

        /* ---- Status bar (row 25) ---- */
        if (confirm_quit) {
            write(STDOUT, "Unsaved changes. Ctrl+Q again to quit.", 38);
        } else if (status_message != NULL) {
            printf("%s", status_message);
            status_message = NULL;
        } else {
            printf("%s", filename);
            if (dirty) {
                write(STDOUT, " [modified]", 11);
            }
            printf("  line %d  col %d", cursor_line + 1, cursor_column + 1);
        }

        /* Move cursor to cursor_line/cursor_column (1-based ANSI). */
        printf("\e[%d;%dH",
               cursor_line - view_line + 1,
               cursor_column - view_column + 1);

        /* ---- Get input ---- */
        char character = getchar();

        if (confirm_quit) {
            if (character == '\x11') {
                video_mode(vga_fd, VIDEO_MODE_TEXT_80x25);
                return 0;
            }
            confirm_quit = 0;
            continue;
        }

        /* Serial arrow keys arrive as ESC [ A/B/C/D.  Translate them
           into the matching Ctrl-char so the handlers below cover
           both paths from a single body. */
        if (character == '\x1B') {
            char prefix = getchar();
            character = 0;
            if (prefix == '[') {
                char code = getchar();
                if (code == 'A') {
                    character = '\x10';
                } else if (code == 'B') {
                    character = '\x0E';
                } else if (code == 'C') {
                    character = '\x06';
                } else if (code == 'D') {
                    character = '\x02';
                }
            }
        }

        if (character == '\x01') {
            /* Ctrl+A: move to beginning of line.  Shift chars from the
               pre-gap side over the gap until we hit a newline. */
            while (gap_start > 0 && buffer[gap_start - 1] != '\n') {
                gap_move_left();
            }
            cursor_column = 0;
        } else if (character == '\x02') {
            /* Ctrl+B: cursor left one character. */
            if (gap_start > 0) {
                int c = gap_move_left();
                if (c == '\n') {
                    if (cursor_line > 0) {
                        cursor_line -= 1;
                        cursor_column = column_before(buffer);
                        if (cursor_line < view_line) {
                            view_line = cursor_line;
                        }
                    }
                } else if (cursor_column > 0) {
                    cursor_column -= 1;
                }
            }
        } else if (character == '\x05') {
            /* Ctrl+E: move to end of line. */
            while (gap_end < EDIT_BUFFER_SIZE && buffer[gap_end] != '\n') {
                gap_move_right();
                cursor_column += 1;
            }
        } else if (character == '\x06') {
            /* Ctrl+F: cursor right one character. */
            if (gap_end < EDIT_BUFFER_SIZE) {
                int c = gap_move_right();
                if (c == '\n') {
                    cursor_line += 1;
                    cursor_column = 0;
                    if (cursor_line >= view_line + 24) {
                        view_line = cursor_line - 23;
                    }
                } else {
                    cursor_column += 1;
                }
            }
        } else if (character == '\b' || character == '\x7F') {
            /* Backspace / DEL: delete the char before the cursor. */
            if (gap_start > 0) {
                char c = buffer[gap_start - 1];
                gap_start -= 1;
                dirty = 1;
                if (c == '\n') {
                    if (cursor_line > 0) {
                        cursor_line -= 1;
                        cursor_column = column_before(buffer);
                        if (cursor_line < view_line) {
                            view_line = cursor_line;
                        }
                    }
                } else if (cursor_column > 0) {
                    cursor_column -= 1;
                }
            }
        } else if (character == '\x0B') {
            /* Ctrl+K: kill from cursor through end of line.  Overflow
               past the kill buffer silently drops the tail. */
            int kill_index = 0;
            while (gap_end < EDIT_BUFFER_SIZE) {
                char c = buffer[gap_end];
                gap_end += 1;
                dirty = 1;
                if (kill_index < EDIT_KILL_BUFFER_SIZE) {
                    kill_buf[kill_index] = c;
                    kill_index += 1;
                }
                if (c == '\n') {
                    break;
                }
            }
            kill_length = kill_index;
        } else if (character == '\r' || character == '\n') {
            /* Enter: insert newline at cursor. */
            if (gap_start < gap_end) {
                buffer[gap_start] = '\n';
                gap_start += 1;
                dirty = 1;
                cursor_line += 1;
                cursor_column = 0;
                if (cursor_line >= view_line + 24) {
                    view_line = cursor_line - 23;
                }
            }
        } else if (character == '\x0E') {
            /* Ctrl+N: move down one line, staying as close to the
               original column as the target line allows. */
            int target_col = cursor_column;
            int found_nl = 0;
            while (gap_end < EDIT_BUFFER_SIZE) {
                if (gap_move_right() == '\n') {
                    found_nl = 1;
                    break;
                }
            }
            if (found_nl) {
                cursor_line += 1;
                cursor_column = 0;
                if (cursor_line >= view_line + 24) {
                    view_line = cursor_line - 23;
                }
                while (target_col > 0 && gap_end < EDIT_BUFFER_SIZE && buffer[gap_end] != '\n') {
                    gap_move_right();
                    cursor_column += 1;
                    target_col -= 1;
                }
            }
        } else if (character == '\x10') {
            /* Ctrl+P: move up one line. */
            if (cursor_line > 0) {
                int target_col = cursor_column;
                int found_nl = 0;
                /* Step back across the newline ending the prior line. */
                while (gap_start > 0) {
                    if (gap_move_left() == '\n') {
                        found_nl = 1;
                        break;
                    }
                }
                if (found_nl) {
                    /* Walk back to the start of the previous line. */
                    while (gap_start > 0 && buffer[gap_start - 1] != '\n') {
                        gap_move_left();
                    }
                    cursor_line -= 1;
                    cursor_column = 0;
                    if (cursor_line < view_line) {
                        view_line = cursor_line;
                    }
                    while (target_col > 0 && gap_end < EDIT_BUFFER_SIZE && buffer[gap_end] != '\n') {
                        gap_move_right();
                        cursor_column += 1;
                        target_col -= 1;
                    }
                }
            }
        } else if (character == '\x11') {
            /* Ctrl+Q: quit; require a second Ctrl+Q when dirty. */
            if (!dirty) {
                video_mode(vga_fd, VIDEO_MODE_TEXT_80x25);
                return 0;
            }
            confirm_quit = 1;
        } else if (character == '\x13') {
            /* Ctrl+S: save via open(O_WRONLY|O_CREAT|O_TRUNC) + write
               in 512-byte chunks built from the gap buffer. */
            int save_fd = open(filename, O_WRONLY + O_CREAT + O_TRUNC, 0);
            if (save_fd < 0) {
                status_message = "Cannot create file (directory full?)";
            } else {
                int total_length = EDIT_BUFFER_SIZE - (gap_end - gap_start);
                int logical_offset = 0;
                int write_err = 0;
                while (logical_offset < total_length) {
                    int chunk_size = total_length - logical_offset;
                    if (chunk_size > 512) {
                        chunk_size = 512;
                    }
                    int i = 0;
                    while (i < chunk_size) {
                        sector[i] = buffer_character_at(buffer, logical_offset + i);
                        i += 1;
                    }
                    if (write(save_fd, sector, chunk_size) < 0) {
                        write_err = 1;
                        break;
                    }
                    logical_offset += chunk_size;
                }
                close(save_fd);
                if (write_err) {
                    status_message = "Write error";
                } else {
                    dirty = 0;
                    status_message = "Saved.";
                }
            }
        } else if (character == '\x19') {
            /* Ctrl+Y: yank the kill buffer at the cursor. */
            int i = 0;
            while (i < kill_length) {
                if (gap_start < gap_end) {
                    char c = kill_buf[i];
                    buffer[gap_start] = c;
                    gap_start += 1;
                    dirty = 1;
                    if (c == '\n') {
                        cursor_line += 1;
                        cursor_column = 0;
                        if (cursor_line >= view_line + 24) {
                            view_line = cursor_line - 23;
                        }
                    } else {
                        cursor_column += 1;
                    }
                }
                i += 1;
            }
        } else if (character >= ' ' && character <= '~') {
            /* Printable ASCII: insert at cursor. */
            if (gap_start < gap_end) {
                buffer[gap_start] = character;
                gap_start += 1;
                dirty = 1;
                cursor_column += 1;
            }
        }
    }
}
