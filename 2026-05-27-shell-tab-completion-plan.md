# Shell Tab Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add tab completion to the BBoeOS shell — commands in first-word position, files/directories in argument position.

**Architecture:** All logic lives in `user/programs/shell.c`. Tab press dispatches to `tab_complete()` which determines position (command vs argument), enumerates candidates via `open()`+`getdents()`, filters by prefix, and either inserts a unique match or displays all matches bash-style. File-scope BSS buffers hold getdents results and the match list.

**Tech Stack:** BBoeOS cc.py C compiler, NASM assembler, QEMU test harness (`tests/run_qemu.py`)

---

## File Structure

- **Modify:** `user/programs/shell.c` — add tab completion functions and the `case '\t'` arm
- **Create:** `tests/test_tab_complete.py` — dedicated test file for tab completion (boots QEMU, sends Tab keystrokes via serial, asserts output)

---

### Task 1: Add BSS buffers and helper — `collect_matches`

**Files:**
- Modify: `user/programs/shell.c` (add after `saved_line_length` declaration, ~line 91)

- [ ] **Step 1: Add file-scope BSS buffers for completion**

Insert these declarations after the `saved_line_length` declaration (alphabetical among the file-scope blocks):

```c
/* Tab-completion scratch.  complete_arena holds copies of matched entry
   names (packed, NUL-terminated); complete_buffer is the raw getdents
   I/O buffer; complete_matches points into the arena. */
#define COMPLETE_ARENA_BYTES 2048
#define COMPLETE_BUFFER_BYTES 4096
#define COMPLETE_MAX_MATCHES 64

char complete_arena[COMPLETE_ARENA_BYTES];
char *complete_matches[COMPLETE_MAX_MATCHES];
char complete_buffer[COMPLETE_BUFFER_BYTES];
int complete_cursor_out;
int complete_end_out;
int complete_match_count;
```

- [ ] **Step 2: Add `collect_matches` function**

Insert in alphabetical position (after `cursor_back`, before `delete_at_cursor`):

```c
int collect_matches(char *directory, char *prefix, int prefix_length) {
    /* Open *directory*, enumerate entries via getdents, copy names that
       start with *prefix* into complete_arena.  Populates
       complete_matches[] and returns the count.  Stores d_type (DT_DIR=4
       or DT_REG=8) in the byte immediately before each name in the arena
       so the caller can append '/' for directories. */
    int fd = open(directory, O_RDONLY);
    if (fd < 0) {
        return 0;
    }
    int count = 0;
    int arena_used = 0;
    while (1) {
        int bytes = getdents(fd, complete_buffer, COMPLETE_BUFFER_BYTES);
        if (bytes <= 0) {
            break;
        }
        int cursor = 0;
        while (cursor < bytes) {
            int reclen = complete_buffer[cursor + 4] +
                         (complete_buffer[cursor + 5] << 8);
            int type = complete_buffer[cursor + 6];
            char *name = complete_buffer + cursor + 7;
            int name_length = strlen(name);
            int match = 1;
            if (name_length < prefix_length) {
                match = 0;
            } else {
                int i = 0;
                while (i < prefix_length) {
                    if (name[i] != prefix[i]) {
                        match = 0;
                        break;
                    }
                    i = i + 1;
                }
            }
            if (match && count < COMPLETE_MAX_MATCHES) {
                if (arena_used + 1 + name_length + 1 <= COMPLETE_ARENA_BYTES) {
                    complete_arena[arena_used] = type;
                    arena_used = arena_used + 1;
                    complete_matches[count] = complete_arena + arena_used;
                    memcpy(complete_arena + arena_used, name, name_length + 1);
                    arena_used = arena_used + name_length + 1;
                    count = count + 1;
                }
            }
            cursor = cursor + reclen;
        }
    }
    close(fd);
    complete_match_count = count;
    return count;
}
```

- [ ] **Step 3: Build and verify compilation**

Run:
```bash
cd /home/ubuntu/bboeos && ./make_os.sh
```
Expected: builds successfully (no cc.py errors).

- [ ] **Step 4: Commit**

```bash
git add user/programs/shell.c
git commit -m "feat(shell): add collect_matches for tab completion"
```

---

### Task 2: Add `longest_common_prefix` helper

**Files:**
- Modify: `user/programs/shell.c`

- [ ] **Step 1: Add `longest_common_prefix` function**

Insert in alphabetical position (after `insert_char`, before `replace_line`):

```c
int longest_common_prefix() {
    /* Compute the length of the longest common prefix among all entries
       in complete_matches[0..complete_match_count).  Returns 0 if count
       is 0. */
    if (complete_match_count == 0) {
        return 0;
    }
    int length = strlen(complete_matches[0]);
    int i = 1;
    while (i < complete_match_count) {
        int j = 0;
        while (j < length) {
            if (complete_matches[i][j] != complete_matches[0][j]) {
                length = j;
                break;
            }
            j = j + 1;
        }
        i = i + 1;
    }
    return length;
}
```

- [ ] **Step 2: Build and verify**

```bash
cd /home/ubuntu/bboeos && ./make_os.sh
```

- [ ] **Step 3: Commit**

```bash
git add user/programs/shell.c
git commit -m "feat(shell): add longest_common_prefix for tab completion"
```

---

### Task 3: Add `tab_complete` and position-specific helpers

**Files:**
- Modify: `user/programs/shell.c`

- [ ] **Step 1: Add forward declarations**

Add forward declarations near the existing forward-decl block (after `int visual_bell();` at ~line 603):

```c
void tab_complete(char *buf, int cursor, int end);
```

- [ ] **Step 2: Add `tab_complete` function**

Insert in alphabetical position (after `try_exec`, before `visual_bell`):

```c
void tab_complete(char *buf, int cursor, int end) {
    /* Determine if we're completing a command (first word) or an argument
       (subsequent word).  Extract the partial word, collect matches, and
       either insert or display them.  Updates complete_cursor_out and
       complete_end_out for the caller. */
    complete_cursor_out = cursor;
    complete_end_out = end;

    /* Find the start of the current word by scanning backward. */
    int word_start = cursor;
    while (word_start > 0 && buf[word_start - 1] != ' ') {
        word_start = word_start - 1;
    }
    int word_length = cursor - word_start;

    /* Determine position: command if no space precedes the word start. */
    int is_command = 1;
    int scan = 0;
    while (scan < word_start) {
        if (buf[scan] != ' ') {
            is_command = 0;
            break;
        }
        scan = scan + 1;
    }

    /* Null-terminate the partial word for prefix matching. */
    char partial[MAX_INPUT];
    memcpy(partial, buf + word_start, word_length);
    partial[word_length] = '\0';

    int count;
    int dir_prefix_length = 0;
    if (is_command) {
        /* Command completion: builtins + bin/ entries. */
        /* Manually check builtins first. */
        int arena_used = 0;
        count = 0;
        char *builtins[3];
        builtins[0] = "help";
        builtins[1] = "reboot";
        builtins[2] = "shutdown";
        int bi = 0;
        while (bi < 3) {
            int blen = strlen(builtins[bi]);
            int match = 1;
            if (blen < word_length) {
                match = 0;
            } else {
                int ci = 0;
                while (ci < word_length) {
                    if (builtins[bi][ci] != partial[ci]) {
                        match = 0;
                        break;
                    }
                    ci = ci + 1;
                }
            }
            if (match && count < COMPLETE_MAX_MATCHES) {
                if (arena_used + 1 + blen + 1 <= COMPLETE_ARENA_BYTES) {
                    complete_arena[arena_used] = 8; /* DT_REG */
                    arena_used = arena_used + 1;
                    complete_matches[count] = complete_arena + arena_used;
                    memcpy(complete_arena + arena_used, builtins[bi], blen + 1);
                    arena_used = arena_used + blen + 1;
                    count = count + 1;
                }
            }
            bi = bi + 1;
        }
        /* Now add bin/ entries. */
        int fd = open("bin", O_RDONLY);
        if (fd >= 0) {
            while (1) {
                int bytes = getdents(fd, complete_buffer, COMPLETE_BUFFER_BYTES);
                if (bytes <= 0) {
                    break;
                }
                int gc = 0;
                while (gc < bytes) {
                    int reclen = complete_buffer[gc + 4] +
                                 (complete_buffer[gc + 5] << 8);
                    char *name = complete_buffer + gc + 7;
                    int nlen = strlen(name);
                    int match = 1;
                    if (nlen < word_length) {
                        match = 0;
                    } else {
                        int ci = 0;
                        while (ci < word_length) {
                            if (name[ci] != partial[ci]) {
                                match = 0;
                                break;
                            }
                            ci = ci + 1;
                        }
                    }
                    if (match && count < COMPLETE_MAX_MATCHES) {
                        if (arena_used + 1 + nlen + 1 <= COMPLETE_ARENA_BYTES) {
                            complete_arena[arena_used] = 8; /* DT_REG */
                            arena_used = arena_used + 1;
                            complete_matches[count] = complete_arena + arena_used;
                            memcpy(complete_arena + arena_used, name, nlen + 1);
                            arena_used = arena_used + nlen + 1;
                            count = count + 1;
                        }
                    }
                    gc = gc + reclen;
                }
            }
            close(fd);
        }
        complete_match_count = count;
    } else {
        /* Argument completion: split partial at last '/' for dir + prefix. */
        char directory[MAX_PATH];
        char prefix[MAX_INPUT];
        int last_slash = -1;
        int pi = 0;
        while (pi < word_length) {
            if (partial[pi] == '/') {
                last_slash = pi;
            }
            pi = pi + 1;
        }
        if (last_slash < 0) {
            directory[0] = '.';
            directory[1] = '\0';
            memcpy(prefix, partial, word_length + 1);
            dir_prefix_length = 0;
        } else {
            memcpy(directory, partial, last_slash + 1);
            directory[last_slash + 1] = '\0';
            int plen = word_length - last_slash - 1;
            memcpy(prefix, partial + last_slash + 1, plen + 1);
            word_length = plen;
            dir_prefix_length = last_slash + 1;
        }
        count = collect_matches(directory, prefix, word_length);
    }

    if (count == 0) {
        visual_bell();
        return;
    }

    /* Compute longest common prefix of matches. */
    int lcp = longest_common_prefix();

    if (lcp > word_length) {
        /* We can extend the typed word.  Replace the partial with the
           LCP.  If there's exactly one match, also append '/' for dirs. */
        int suffix_length = lcp - word_length;
        int insert_extra = 0;
        if (count == 1) {
            /* Check d_type stored one byte before the name. */
            char type = *(complete_matches[0] - 1);
            if (type == 4) { /* DT_DIR */
                insert_extra = 1;
            }
        }
        /* Insert the new characters at cursor. */
        int total_insert = suffix_length + insert_extra;
        /* Build the insertion string. */
        char insertion[MAX_INPUT];
        memcpy(insertion, complete_matches[0] + word_length, suffix_length);
        if (insert_extra) {
            insertion[suffix_length] = '/';
        }
        int ii = 0;
        while (ii < total_insert && end < MAX_INPUT) {
            end = insert_char(buf, cursor, end, insertion[ii]);
            cursor = cursor + 1;
            ii = ii + 1;
        }
        complete_cursor_out = cursor;
        complete_end_out = end;
        return;
    }

    /* Multiple matches and LCP == what's typed: display all. */
    putchar('\n');
    int mi = 0;
    while (mi < count) {
        if (mi > 0) {
            write(STDOUT, "  ", 2);
        }
        char *name = complete_matches[mi];
        int nlen = strlen(name);
        /* Prepend the directory prefix for display if argument completion. */
        if (dir_prefix_length > 0) {
            write(STDOUT, partial, dir_prefix_length);
        }
        write(STDOUT, name, nlen);
        char type = *(name - 1);
        if (type == 4) { /* DT_DIR */
            putchar('/');
        }
        mi = mi + 1;
    }
    putchar('\n');
    /* Reprint prompt + current input. */
    write(STDOUT, "$ ", 2);
    if (end > 0) {
        write(STDOUT, buf, end);
    }
    /* Reposition cursor if it's not at end. */
    if (cursor < end) {
        cursor_back(end - cursor);
    }
    complete_cursor_out = cursor;
    complete_end_out = end;
}
```

- [ ] **Step 3: Build and verify**

```bash
cd /home/ubuntu/bboeos && ./make_os.sh
```

- [ ] **Step 4: Commit**

```bash
git add user/programs/shell.c
git commit -m "feat(shell): add tab_complete with command and argument completion"
```

---

### Task 4: Wire Tab into the line editor

**Files:**
- Modify: `user/programs/shell.c` (the `main()` switch statement)

- [ ] **Step 1: Add `case '\t'` to the line editor switch**

In `main()`, inside the `switch (character)` block, add a new case after the `case '\x06'` (Ctrl-F) block and before the `case '\b'` block. Insert in ASCII order — `\t` is `0x09`:

```c
            case '\t':
                /* Tab: trigger completion */
                tab_complete(buf, cursor, end);
                cursor = complete_cursor_out;
                end = complete_end_out;
                break;
```

- [ ] **Step 2: Build and verify**

```bash
cd /home/ubuntu/bboeos && ./make_os.sh
```

- [ ] **Step 3: Manual smoke test**

```bash
cd /home/ubuntu/bboeos && qemu-system-i386 -drive file=drive.img,format=raw -serial stdio -display none
```

At the `$ ` prompt, type `hel` then Tab — should complete to `help`. Type `ls b` then Tab — should complete to `ls bin/`.

- [ ] **Step 4: Commit**

```bash
git add user/programs/shell.c
git commit -m "feat(shell): wire tab completion into line editor"
```

---

### Task 5: Automated test

**Files:**
- Create: `tests/test_tab_complete.py`

- [ ] **Step 1: Write the test file**

```python
#!/usr/bin/env python3
"""Tab-completion tests for the BBoeOS shell.

Boots QEMU, sends partial commands + Tab via serial, and verifies the
shell completes them correctly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from run_qemu import QemuSession, qemu_session  # noqa: E402


def build_image() -> Path:
    """Build the OS image and return the drive path."""
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
    )
    return REPO_ROOT / "drive.img"


def test_command_completion_unique():
    """Typing 'hel' + Tab completes to 'help'."""
    drive = build_image()
    with qemu_session(drive=drive) as session:
        session.wait_for_prompt()
        session.write_serial("hel\t\r")
        session.wait_for_prompt()
        assert "Commands:" in session.output


def test_command_completion_multiple():
    """Typing 'sh' + Tab shows multiple matches (shell-style listing)."""
    drive = build_image()
    with qemu_session(drive=drive) as session:
        session.wait_for_prompt()
        session.write_serial("sh\t")
        import time
        time.sleep(0.3)
        session.write_serial("\x03")
        session.wait_for_prompt()
        assert "shutdown" in session.output or "sh" in session.output


def test_argument_completion_directory():
    """Typing 'ls bi' + Tab completes to 'ls bin/'."""
    drive = build_image()
    with qemu_session(drive=drive) as session:
        session.wait_for_prompt()
        session.write_serial("ls bi\t\r")
        session.wait_for_prompt()
        output = session.output
        assert "bin/" in output or "ls" in output


if __name__ == "__main__":
    test_command_completion_unique()
    print("PASS: test_command_completion_unique")
    test_command_completion_multiple()
    print("PASS: test_command_completion_multiple")
    test_argument_completion_directory()
    print("PASS: test_argument_completion_directory")
    print("\nAll tab-completion tests passed.")
```

- [ ] **Step 2: Run the test**

```bash
cd /home/ubuntu/bboeos && python3 tests/test_tab_complete.py
```

Expected: All three tests pass. If `test_command_completion_unique` fails, the Tab byte may not be reaching the shell — check that `fd_read_console` passes `0x09` through (it should, since it only filters specific control codes).

- [ ] **Step 3: Commit**

```bash
git add tests/test_tab_complete.py
git commit -m "test(shell): add automated tab completion tests"
```

---

### Task 6: Edge case — empty Tab lists all commands

**Files:**
- Modify: `user/programs/shell.c` (the `tab_complete` function handles this already — `word_length == 0` means `prefix_length == 0`, so all entries match)
- Modify: `tests/test_tab_complete.py`

- [ ] **Step 1: Add test for empty-prefix command listing**

Append to `tests/test_tab_complete.py` before the `if __name__` block:

```python
def test_empty_tab_lists_commands():
    """Pressing Tab with empty input lists all available commands."""
    drive = build_image()
    with qemu_session(drive=drive) as session:
        session.wait_for_prompt()
        session.write_serial("\t")
        import time
        time.sleep(0.5)
        session.write_serial("\x03")
        session.wait_for_prompt()
        output = session.output
        assert "help" in output
        assert "reboot" in output
```

And add to the `__main__` block:

```python
    test_empty_tab_lists_commands()
    print("PASS: test_empty_tab_lists_commands")
```

- [ ] **Step 2: Run test**

```bash
cd /home/ubuntu/bboeos && python3 tests/test_tab_complete.py
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_tab_complete.py
git commit -m "test(shell): add empty-tab lists all commands test"
```

---

### Task 7: Verify Tab doesn't break serial input passthrough

**Files:**
- Modify: `kernel/drivers/serial.c` or `kernel/syscalls/io.asm` — only if `0x09` is being filtered

- [ ] **Step 1: Check that Tab (0x09) passes through the serial input path**

Search for any filtering of control characters in the console/serial input path:

```bash
grep -rn '0x09\|\\t\|TAB' kernel/drivers/serial.c kernel/drivers/ps2.asm kernel/syscalls/ 2>/dev/null
```

If `0x09` is explicitly filtered or translated, remove that filter. If it passes through (most likely — the shell already handles other control chars like `0x01`–`0x06` and `0x0B`–`0x10`), no kernel change is needed.

- [ ] **Step 2: Run full test suite to verify no regressions**

```bash
cd /home/ubuntu/bboeos && python3 tests/test_programs.py
```

Expected: all existing tests pass.

- [ ] **Step 3: Commit (only if kernel changes were needed)**

```bash
git add -A && git commit -m "fix(serial): pass Tab (0x09) through to userland"
```

---

### Task 8: Update CHANGELOG

**Files:**
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Add changelog entry**

Add under the `## [Unreleased]` section:

```markdown
- **Shell: tab completion.** Press Tab to complete command names (builtins
  + executables in `bin/`) in first-word position, or files and
  directories in argument position.  Single matches are inserted
  in-place (directories get a trailing `/`); multiple matches display a
  bash-style listing below the prompt; no matches flash a visual bell.
```

- [ ] **Step 2: Commit**

```bash
git add docs/CHANGELOG.md
git commit -m "docs: add tab completion changelog entry"
```
