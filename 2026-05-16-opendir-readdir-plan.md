# opendir / readdir / closedir / rewinddir — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land POSIX directory iteration in BBoeOS — Linux-style `getdents` syscall + ext2 inode preserved end-to-end + libc `<dirent.h>`.

**Architecture:** Two PRs. PR 1 restructures the kernel↔userland directory API (new `SYS_IO_GETDENTS` syscall, `dir_emit` callback inside the VFS, `read()` on a directory fd returns `EISDIR`, `ls.c` migrated and sorted). PR 2 adds `opendir`/`readdir`/`closedir`/`rewinddir` to `tools/libc` on top of the new syscall, tested via the existing `hello` libc test binary.

**Tech Stack:** NASM (kernel asm), cc.py (in-tree C compiler for shipped programs), clang for the libc test binary, Python (pytest-shaped test drivers under `tests/` running QEMU).

**Spec:** see `2026-05-16-opendir-readdir-design.md` on the `design-specs` branch.

---

# PR 1 — kernel `getdents` + `ls.c` migration

## Task 1: Add new constants

**Files:**
- Modify: `src/include/constants.asm` (error code block at lines 11-19; SYS_IO_* block at lines 187-195)

- [ ] **Step 1.1: Add the two new error codes alphabetically**

Insert into the `ERROR_*` block:

```asm
        %assign ERROR_IS_DIRECTORY   0Ah     ; read() called on a directory fd
        %assign ERROR_NOT_DIRECTORY  0Bh     ; getdents called on a non-directory fd
```

Place `ERROR_IS_DIRECTORY` between `ERROR_INVALID` and `ERROR_NOT_EMPTY`; place `ERROR_NOT_DIRECTORY` between `ERROR_NOT_EMPTY` and `ERROR_NOT_EXECUTE`. The numeric values fill the next free slots (`0Ah`, `0Bh`).

- [ ] **Step 1.2: Add `SYS_IO_GETDENTS` and renumber the IO group**

The IO group lives at `src/include/constants.asm:187`. Insert `GETDENTS` alphabetically between `FSTAT` (13h) and `IOCTL`. Renumber `IOCTL`/`OPEN`/`READ`/`SEEK`/`WRITE` up by one:

```asm
        %assign SYS_IO_CLOSE    10h
        %assign SYS_IO_DUP      11h
        %assign SYS_IO_DUP2     12h
        %assign SYS_IO_FSTAT    13h
        %assign SYS_IO_GETDENTS 14h    ; BX=fd, DI=buffer, CX=count; AX=bytes written
        %assign SYS_IO_IOCTL    15h
        %assign SYS_IO_OPEN     16h
        %assign SYS_IO_READ     17h
        %assign SYS_IO_SEEK     18h
        %assign SYS_IO_WRITE    19h
```

- [ ] **Step 1.3: Verify everything still assembles**

```bash
./make_os.sh
```

Expected: clean build (any program that used a numeric INT 30h instead of `SYS_*` would break here — none currently do).

- [ ] **Step 1.4: Commit**

```bash
git add src/include/constants.asm
git commit -m "kernel: add SYS_IO_GETDENTS, ERROR_IS_DIRECTORY, ERROR_NOT_DIRECTORY"
```

---

## Task 2: Wire dispatch entry (stub handler)

**Files:**
- Modify: `src/arch/x86/syscall.asm` (table at lines 102-134; handler bodies follow)

- [ ] **Step 2.1: Add the table entry alphabetically**

Between `SYS_IO_FSTAT` and `SYS_IO_IOCTL` in the dispatch table at `src/arch/x86/syscall.asm:111`:

```asm
        SYS_ENTRY SYS_IO_GETDENTS,   .io_getdents
```

- [ ] **Step 2.2: Add a stub handler that returns -1 / `ERROR_NOT_DIRECTORY`**

Open `src/arch/x86/syscall.asm` and read three existing handler bodies (`.io_read`, `.io_open`, `.io_close`) to see how each:
- captures fd in `BX`,
- calls into the fs layer (`call fd_read`, etc.),
- sets the error code on the failure path,
- routes to the shared `.iret_cf` epilogue with CF set.

Add `.io_getdents` alphabetically with the same shape, but as a **temporary stub** that always sets the error code to `ERROR_NOT_DIRECTORY` and jumps to whichever shared error-return label the other handlers use. The stub exists only so the syscall is reachable; it will be replaced by the real implementation in Task 4 (Step 4.4).

Concretely, the stub needs to: (a) write `-1` into the saved-AX slot in the syscall frame, (b) store `ERROR_NOT_DIRECTORY` in whatever errno-equivalent global the file uses, (c) jump to the same epilogue label `.io_read` uses on its failure path. The exact macro and label names are visible at line ~312 of the file — mirror them.

- [ ] **Step 2.3: Build and boot**

```bash
./make_os.sh
qemu-system-i386 -drive file=drive.img,format=raw -serial stdio
```

Type any command at the prompt; the shell should still boot normally. Exit QEMU.

- [ ] **Step 2.4: Commit**

```bash
git add src/arch/x86/syscall.asm
git commit -m "kernel: wire SYS_IO_GETDENTS dispatch entry (stub returning ENOTDIR)"
```

---

## Task 3: Add `dir_emit` callback helper

**Files:**
- Create: `src/fs/dir_emit.asm` (new file)
- Modify: `src/fs/vfs.asm` (extern thunks)
- Modify: `make_os.sh` or whatever builds `src/fs/*.asm` (verify `dir_emit.asm` is picked up — likely auto via the build script's `src/fs/*.asm` glob; confirm)

- [ ] **Step 3.1: Write `dir_emit` in asm**

`dir_emit(ctx, name, namelen, ino, type)` is the FS↔VFS callback. Inputs (register convention to match existing asm style):
- `ESI` = ctx pointer
- `EDI` = name pointer (kernel-virt)
- `ECX` = namelen (excluding NUL)
- `EDX` = ino (32-bit)
- `AL`  = type (DT_REG=8, DT_DIR=4, DT_UNKNOWN=0)

`ctx` is a struct in kernel memory:
```
0:  uint32_t user_buffer_kvirt
4:  uint32_t bytes_remaining
8:  uint32_t bytes_written
```

Behavior:
1. Compute `reclen = round_up(7 + namelen + 1, 4)`.
2. If `reclen > bytes_remaining`: set CF (caller-buffer-full signal) and return; ctx is not modified, so the FS driver knows to back off and not advance its iteration cursor.
3. Else: write `ino` (4 bytes), `reclen` (2 bytes), `type` (1 byte), `name` (namelen+1 bytes including NUL), zero-pad to `reclen`, into `user_buffer_kvirt + bytes_written`.
4. `bytes_remaining -= reclen`, `bytes_written += reclen`, clear CF, return.

Worth noting: the user buffer is in kernel-virt (mapped via the existing per-program PD; the syscall path is responsible for validating and translating before invoking `dir_emit`).

Show the full helper (~40 lines). Save to `src/fs/dir_emit.asm`. Export the label.

- [ ] **Step 3.2: Verify it assembles**

```bash
./make_os.sh
```

Expected: clean.

- [ ] **Step 3.3: Commit**

```bash
git add src/fs/dir_emit.asm src/fs/vfs.asm
git commit -m "fs/vfs: add dir_emit callback for kernel-side dirent packing"
```

---

## Task 4: Rewrite `bbfs_read_dir` to use `dir_emit`

**Files:**
- Modify: `src/fs/bbfs.asm` (function at line 387, `bbfs_read_dir`)

The current `bbfs_read_dir` reads one 32-byte raw bbfs entry at a time. We need to rewrite it so that on each call from the syscall layer, it loops over as many entries as fit in the user buffer (calling `dir_emit` once per entry) and updates the fd's position only when `dir_emit` accepted the record.

- [ ] **Step 4.1: Write a failing test**

Add to `tests/test_bboefs.py`:

```python
def test_getdents_basic(tmp_path):
    """SYS_IO_GETDENTS on the root directory returns each live entry once."""
    image = setup_image(tmp_path)
    # Boot, run a tiny test program that calls getdents on "/" and prints
    # each (d_ino, d_type, d_name) tuple it sees.  Compare against the
    # known root-directory contents.
    output = run_commands(image, ["getdents_probe /"], timeout=COMMAND_TIMEOUT)
    expected_names = sorted(KNOWN_ROOT_FILES)
    actual_names = sorted(parse_getdents_probe_output(output))
    assert actual_names == expected_names
```

`getdents_probe` is a tiny new test binary committed under `src/c/` (cc.py-built) that calls `SYS_IO_GETDENTS` via raw INT 30h and prints `d_ino:d_type:d_name\n` for each record it parses. Skeleton:

```c
// src/c/getdents_probe.c
int main(int argc, char *argv[]) {
    char *path = argc > 1 ? argv[1] : "/";
    int fd = open(path, O_RDONLY);
    if (fd < 0) die("open failed\n");
    char buf[4096];
    while (1) {
        int bytes = sys_io_getdents(fd, buf, sizeof buf);
        if (bytes <= 0) break;
        int cursor = 0;
        while (cursor < bytes) {
            unsigned int   ino  = *(unsigned int *)(buf + cursor + 0);
            unsigned short rec  = *(unsigned short *)(buf + cursor + 4);
            unsigned char  type = buf[cursor + 6];
            char          *name = buf + cursor + 7;
            printf("%u:%u:%s\n", ino, (unsigned)type, name);
            cursor += rec;
        }
    }
    close(fd);
    return 0;
}
```

`sys_io_getdents` is a one-liner INT 30h wrapper (model after the existing `read()` wrapper in any current shipped program — `cat.c` is a good example).

- [ ] **Step 4.2: Run the test — expect failure**

```bash
tests/test_bboefs.py test_getdents_basic
```

Expected: fails (the stub returns `ENOTDIR`). This proves the test wires through the syscall correctly.

- [ ] **Step 4.3: Rewrite `bbfs_read_dir`**

The new shape (pseudocode, translate to asm):

```
bbfs_read_dir(esi=fd_entry, ctx in caller-known register e.g. EBP):
    .loop:
        if fd_position >= DIRECTORY_SECTORS * 512: return ok (EOF)
        read sector at fd_position
        if entry at that offset is empty (name[0] == 0):
            fd_position += 32
            jmp .loop
        compute ino = entry.start_sector
        compute type = (entry.flags & FLAG_DIRECTORY) ? DT_DIR : DT_REG
        compute namelen = strlen(entry.name)
        call dir_emit(ctx, &entry.name, namelen, ino, type)
        if CF set (buffer full):
            ; do NOT advance fd_position; return ok (caller will see bytes
            ; written so far and resume later)
            return ok
        fd_position += 32
        jmp .loop
```

The full asm is ~60 lines; structurally similar to the current implementation at `src/fs/bbfs.asm:387` but the inner "rep movsb into user buffer" block is replaced by a `call dir_emit`.

- [ ] **Step 4.4: Wire `bbfs_read_dir` to `.io_getdents`**

In `src/arch/x86/syscall.asm`, `.io_getdents` handler:
1. Look up fd entry; reject if type != FD_TYPE_DIRECTORY with `ERROR_NOT_DIRECTORY`.
2. Validate user buffer range (existing helpers in the syscall path do this).
3. Set up `ctx` on the kernel stack (3 dwords: user_buffer_kvirt, count, 0).
4. Call the FS-specific `vfs_read_dir(ctx, fd_entry)` via vfs vtable.
5. Return `ctx.bytes_written` in AX.

- [ ] **Step 4.5: Run the test — expect pass**

```bash
tests/test_bboefs.py test_getdents_basic
```

Expected: passes. Root directory entries enumerated by name.

- [ ] **Step 4.6: Commit**

```bash
git add src/fs/bbfs.asm src/arch/x86/syscall.asm tests/test_bboefs.py src/c/getdents_probe.c
git commit -m "fs/bbfs: rewrite bbfs_read_dir around dir_emit; wire SYS_IO_GETDENTS"
```

---

## Task 5: Rewrite `ext2_read_dir` to use `dir_emit`

**Files:**
- Modify: `src/fs/ext2.asm` (function at line 642, `ext2_read_dir`)

The current function (lines 642-738) is the bbfs-synthesis blob the design doc calls out. Most of the per-entry logic stays — the `inode==0` skip, the `rec_len` advance, the inode lookup for type bits — but the "write 32 bytes into user buffer" block at the end disappears, replaced by a `call dir_emit` with the *real* 32-bit inode and the full filename (no DIRECTORY_NAME_LENGTH-1 truncation).

- [ ] **Step 5.1: Write a failing test**

Add to `tests/test_bboefs.py`:

```python
def test_getdents_ext2_long_names(tmp_path):
    """ext2 filenames > 24 chars survive intact through getdents."""
    image = setup_ext2_image(tmp_path)
    long_name = "this_filename_is_longer_than_twenty_four_chars.txt"  # 49 chars
    add_file_to_ext2(image, long_name, b"hello\n")
    output = run_commands(image, ["getdents_probe /"], timeout=COMMAND_TIMEOUT)
    names = parse_getdents_probe_output(output)
    assert long_name in names
```

(The ext2 image-builder helper `add_file_to_ext2` may need adding to `tests/test_bboefs.py` — it's a thin wrapper around mounting and copying. If a similar fixture exists, use it.)

- [ ] **Step 5.2: Run, expect fail**

```bash
tests/test_bboefs.py test_getdents_ext2_long_names
```

Expected: fails — currently the long name is truncated to 24 chars.

- [ ] **Step 5.3: Rewrite `ext2_read_dir`**

Restructure the function so that after determining (inode, type, name_pointer, namelen), it calls `dir_emit(ctx, name_ptr, namelen, inode, type)` instead of writing the fake bbfs record. Remove the entire "Copy name from static buffer to output", "Write flags, inode, size into output" blocks (lines ~709-722). Remove the namelen clamp at line 683-686. Restructure for the loop-until-buffer-full pattern matching `bbfs_read_dir` (Task 4).

- [ ] **Step 5.4: Run the test, expect pass**

```bash
tests/test_bboefs.py test_getdents_ext2_long_names
```

Expected: passes.

- [ ] **Step 5.5: Run all FS tests on the ext2 matrix**

```bash
tests/test_bboefs.py --filesystem ext2
```

Expected: all pass (regression check for ext2 directory walks).

- [ ] **Step 5.6: Commit**

```bash
git add src/fs/ext2.asm tests/test_bboefs.py
git commit -m "fs/ext2: rewrite ext2_read_dir around dir_emit; preserve long names and full inode"
```

---

## Task 6: `read()` on a directory returns `EISDIR`

**Files:**
- Modify: `src/fs/fd.c` (fd_ops table at line 149)
- Create or modify: `src/fs/fd/fs.c` (new handler `fd_read_isdir`)

- [ ] **Step 6.1: Write a failing test**

Add to `tests/test_bboefs.py`:

```python
def test_read_on_directory_fd_returns_eisdir(tmp_path):
    """read() on a directory fd returns ERROR_IS_DIRECTORY."""
    image = setup_image(tmp_path)
    output = run_commands(image, ["read_dir_probe /"], timeout=COMMAND_TIMEOUT)
    assert "errno=10" in output   # ERROR_IS_DIRECTORY = 0Ah
```

`read_dir_probe` is a tiny new test program: `open("/", O_RDONLY)`, `read(fd, buf, 32)`, print the AX return + the error code.

- [ ] **Step 6.2: Run, expect fail (currently returns the 32-byte entry)**

- [ ] **Step 6.3: Implement `fd_read_isdir`**

In `src/fs/fd/fs.c`:

```c
__attribute__((carry_return))
int fd_read_isdir(int *result __attribute__((out_register("ax"))),
                  struct fd *entry __attribute__((in_register("esi"))),
                  uint8_t *destination __attribute__((in_register("edi"))),
                  int count __attribute__((in_register("ecx")))) {
    *result = -1;
    /* set the global errno-equivalent to ERROR_IS_DIRECTORY here per
     * the existing convention in fd_read */
    return 0;  /* CF set */
}
```

- [ ] **Step 6.4: Wire it into `fd_ops`**

In `src/fs/fd.c:149`, change the `FD_TYPE_DIRECTORY` row's `.read` slot from `fd_read_dir` to `fd_read_isdir`. (`fd_read_dir` itself is no longer reachable from `read()` and can stay as a private helper — or be deleted entirely since `bbfs_read_dir` / `ext2_read_dir` are called directly from the `getdents` path.)

- [ ] **Step 6.5: Run test, expect pass**

- [ ] **Step 6.6: Commit**

```bash
git add src/fs/fd.c src/fs/fd/fs.c tests/test_bboefs.py
git commit -m "fs/fd: read() on a directory fd now returns ERROR_IS_DIRECTORY"
```

---

## Task 7: Rewrite `ls.c`

**Files:**
- Modify: `src/c/ls.c` (full rewrite)

- [ ] **Step 7.1: Update the `ls` test in `tests/test_programs.py`**

Find the existing `ls` test case. Update the expected-output regex to (a) be sorted alphabetically, (b) drop the `*` execute suffix. Specifically:
- Entries appear in `strcmp` order.
- Directories still get `/`.
- Files (executable or not) get nothing after the name.

- [ ] **Step 7.2: Run, expect fail**

```bash
tests/test_programs.py ls
```

Expected: current `ls` is in slot order and has `*` suffixes.

- [ ] **Step 7.3: Rewrite `ls.c`**

Replace `src/c/ls.c` with (full source):

```c
#include "getopt.h"   // if argv parsing grows; otherwise omit

#define DT_DIR 4
#define DT_REG 8

int sys_io_getdents(int fd, void *buf, int count) {
    int result;
    __asm__ volatile (
        "mov ah, SYS_IO_GETDENTS\n"
        "int 0x30\n"
        : "=a"(result)
        : "b"(fd), "D"(buf), "c"(count)
        : "memory"
    );
    return result;
}

int main(int argc, char *argv[]) {
    char *name = argc > 1 ? argv[1] : ".";
    int fd = open(name, O_RDONLY);
    if (fd < 0) die("Not found\n");

    char buffer[4096];
    char arena[4096];
    int  arena_used = 0;
    char *names[48];
    unsigned char types[48];
    int  count = 0;

    while (1) {
        int bytes = sys_io_getdents(fd, buffer, sizeof buffer);
        if (bytes <= 0) break;
        int cursor = 0;
        while (cursor < bytes) {
            unsigned short reclen = *(unsigned short *)(buffer + cursor + 4);
            unsigned char  type   = buffer[cursor + 6];
            char          *src    = buffer + cursor + 7;
            int            len    = strlen(src) + 1;
            memcpy(arena + arena_used, src, len);
            names[count] = arena + arena_used;
            types[count] = type;
            count += 1;
            arena_used += len;
            cursor += reclen;
        }
    }
    close(fd);

    /* Insertion sort by name — small N (max 48), tiny code. */
    for (int i = 1; i < count; i += 1) {
        char *key_name = names[i];
        unsigned char key_type = types[i];
        int j = i - 1;
        while (j >= 0 && strcmp(names[j], key_name) > 0) {
            names[j + 1] = names[j];
            types[j + 1] = types[j];
            j -= 1;
        }
        names[j + 1] = key_name;
        types[j + 1] = key_type;
    }

    for (int i = 0; i < count; i += 1) {
        write(STDOUT, names[i], strlen(names[i]));
        if (types[i] == DT_DIR) putchar('/');
        putchar('\n');
    }
    return 0;
}
```

Notes:
- Uses insertion sort because cc.py-built programs don't have qsort.
- `arena` keeps names alive after we close fd (the `buffer` is reused per call).
- Max 48 entries matches `DIRECTORY_SECTORS * 512 / DIRECTORY_ENTRY_SIZE` for the root.

- [ ] **Step 7.4: Run test, expect pass**

```bash
tests/test_programs.py ls
```

- [ ] **Step 7.5: Commit**

```bash
git add src/c/ls.c tests/test_programs.py
git commit -m "ls: migrate to SYS_IO_GETDENTS; POSIX-conformant sorted output without * suffix"
```

---

## Task 8: Doc updates

**Files:**
- Modify: `docs/syscalls.md`
- Modify: `docs/posix.md`
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 8.1: Update `docs/syscalls.md`**

Add `SYS_IO_GETDENTS` row alphabetically in the IO group; note the renumber. The doc is structured as a table per group — match the existing format.

- [ ] **Step 8.2: Update `docs/posix.md`**

- Flip the existing `read` row note to mention EISDIR on directories.
- Add a `getdents` row in the file I/O section as ✅ / ✅ (kernel implements it, shipped programs reach it via raw INT 30h).
- The `opendir`/`readdir`/`closedir`/`rewinddir` rows in Filesystem section stay ❌ for PR 1 — they go ⚠️ in PR 2.
- The "Directories are read as raw files" note on the `opendir` row changes to "No libc wrapper yet — use `SYS_IO_GETDENTS` directly."
- ls row note updates: drop the `*` suffix mention.

- [ ] **Step 8.3: Update `docs/CHANGELOG.md`**

Under Unreleased, add entries:
- New `SYS_IO_GETDENTS` syscall returning Linux-style variable-length records; renumbers IO group.
- `read()` on a directory fd now returns EISDIR.
- ext2 long filenames (>24 chars) survive through directory iteration.
- ext2 inode numbers are now exposed in full (32-bit, not truncated to 16).
- `ls` migrated to getdents; output sorted; `*` execute suffix removed.

- [ ] **Step 8.4: Run wrap_md**

```bash
python3 tools/wrap_md.py docs/syscalls.md docs/posix.md docs/CHANGELOG.md
```

- [ ] **Step 8.5: Commit**

```bash
git add docs/
git commit -m "docs: getdents + ls migration (syscalls, posix, changelog)"
```

---

## Task 9: Full CI matrix locally

Per the standing rule for kernel-architecture changes, every suite in `.github/workflows/test.yml` runs locally before declaring PR 1 done.

- [ ] **Step 9.1: Run every suite**

```bash
tests/test_asm.py
tests/test_bboefs.py
tests/test_bboefs.py --filesystem ext2
tests/test_bboefs.py --filesystem ext2 --slow
tests/test_programs.py
tests/test_programs.py --filesystem ext2
tests/test_pipeline.py
tests/test_libbboeos_qemu.py
```

Expected: all pass.

- [ ] **Step 9.2: Open PR 1**

```bash
git push -u origin bboe/opendir-readdir-pr1
gh pr create --title "feat: Linux-style getdents + ls migration" --body "..."
```

---

# PR 2 — libc `<dirent.h>`

Branch off the freshly merged `main` (which now has PR 1's syscall and behavior changes available).

## Task 10: Add new errno values to libc

**Files:**
- Modify: `tools/libc/include/errno.h`
- Modify: `tools/libc/syscall.c` (error mapping table around line 13-30)

- [ ] **Step 10.1: Add `EISDIR` and `ENOTDIR` to errno.h**

Find the existing block (currently defines `ENOSPC`, `EEXIST`, `EFAULT`, `EINTR`, `EINVAL`, `EACCES`, `ENOENT`, `EIO`). Add the two new entries alphabetically:

```c
#define EISDIR  21
#define ENOTDIR 20
```

(Pick numeric values to match Linux's traditional assignments; check the existing entries follow Linux's numbers.)

- [ ] **Step 10.2: Map `ERROR_IS_DIRECTORY` and `ERROR_NOT_DIRECTORY`**

In `tools/libc/syscall.c`, find the `switch` that maps `ERROR_*` to errno (around line 13-30). Add:

```c
case ERROR_IS_DIRECTORY:   return EISDIR;
case ERROR_NOT_DIRECTORY:  return ENOTDIR;
```

Insert each alphabetically into the switch body to match the file's existing pattern.

- [ ] **Step 10.3: Build the libc test binary**

```bash
make -C tools/libc
```

Expected: clean.

- [ ] **Step 10.4: Commit**

```bash
git add tools/libc/include/errno.h tools/libc/syscall.c
git commit -m "libc: add EISDIR / ENOTDIR errno mappings"
```

---

## Task 11: Add `<dirent.h>` header

**Files:**
- Create: `tools/libc/include/dirent.h`

- [ ] **Step 11.1: Write the header**

```c
#ifndef BBOEOS_LIBC_DIRENT_H
#define BBOEOS_LIBC_DIRENT_H

#include <sys/types.h>

struct dirent {
    ino_t         d_ino;
    unsigned char d_type;
    char          d_name[256];
};

#define DT_UNKNOWN  0
#define DT_DIR      4
#define DT_REG      8

typedef struct DIR DIR;

DIR           *opendir(const char *path);
struct dirent *readdir(DIR *d);
int            closedir(DIR *d);
void           rewinddir(DIR *d);

#endif
```

- [ ] **Step 11.2: Commit**

```bash
git add tools/libc/include/dirent.h
git commit -m "libc: add <dirent.h> public header"
```

---

## Task 12: Implement `dirent.c`

**Files:**
- Create: `tools/libc/dirent.c`
- Modify: `tools/libc/Makefile` (add `dirent.o` to the build)

- [ ] **Step 12.1: Extend the `hello` test binary with a failing dirent block**

Find `tests/test_libbboeos_qemu.py` and the `hello` source it builds. Append a dirent exercise before the existing "tests passed" print:

```c
#include <dirent.h>
{
    DIR *d = opendir("/");
    if (d == NULL) { puts("FAIL: opendir / returned NULL"); _exit(1); }
    int n = 0;
    int saw_bin = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        printf("entry: ino=%u type=%u name=%s\n",
               (unsigned)e->d_ino, (unsigned)e->d_type, e->d_name);
        if (strcmp(e->d_name, "bin") == 0) saw_bin = 1;
        n += 1;
    }
    if (!saw_bin) { puts("FAIL: did not see 'bin' subdirectory"); _exit(1); }
    rewinddir(d);
    int n2 = 0;
    while ((e = readdir(d)) != NULL) n2 += 1;
    if (n != n2) { puts("FAIL: rewinddir produced different count"); _exit(1); }
    if (closedir(d) != 0) { puts("FAIL: closedir nonzero"); _exit(1); }
    if (opendir("nonexistent_path_xyz") != NULL) {
        puts("FAIL: opendir on missing path should be NULL");
        _exit(1);
    }
    puts("dirent: ok");
}
```

Update the test harness's expected-output regex to look for `dirent: ok`.

- [ ] **Step 12.2: Run, expect fail (linker error — dirent functions undefined)**

```bash
tests/test_libbboeos_qemu.py
```

- [ ] **Step 12.3: Write `tools/libc/dirent.c`**

```c
#include <dirent.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include "bboeos_libc.h"   /* for sys_io_getdents wrapper / syscall stubs */

struct DIR {
    int            fd;
    int            buffer_bytes;
    int            buffer_cursor;
    struct dirent  entry;
    unsigned char  buffer[4096];
};

DIR *opendir(const char *path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return NULL;
    DIR *d = malloc(sizeof *d);
    if (d == NULL) {
        close(fd);
        errno = ENOMEM;
        return NULL;
    }
    d->fd = fd;
    d->buffer_bytes = 0;
    d->buffer_cursor = 0;
    return d;
}

struct dirent *readdir(DIR *d) {
    if (d->buffer_cursor >= d->buffer_bytes) {
        int bytes = sys_io_getdents(d->fd, d->buffer, sizeof d->buffer);
        if (bytes <= 0) return NULL;
        d->buffer_bytes = bytes;
        d->buffer_cursor = 0;
    }
    unsigned char *rec = d->buffer + d->buffer_cursor;
    d->entry.d_ino  = *(unsigned int *)(rec + 0);
    d->entry.d_type = rec[6];
    /* Names from the wire are always NUL-terminated and <= 255 chars, so
     * strcpy into d_name[256] is safe.  If the kernel ever lifts the
     * cap, cap here too. */
    strcpy(d->entry.d_name, (char *)(rec + 7));
    d->buffer_cursor += *(unsigned short *)(rec + 4);
    return &d->entry;
}

int closedir(DIR *d) {
    if (d == NULL) { errno = EINVAL; return -1; }
    int rc = close(d->fd);
    free(d);
    return rc;
}

void rewinddir(DIR *d) {
    if (d == NULL) return;
    lseek(d->fd, 0, SEEK_SET);
    d->buffer_bytes = 0;
    d->buffer_cursor = 0;
}
```

(`sys_io_getdents` is the raw syscall wrapper. If `tools/libc/syscall.c` doesn't have one yet, add it — model after the existing `read()` wrapper in that file.)

- [ ] **Step 12.4: Add `dirent.o` to the libc Makefile**

In `tools/libc/Makefile`, find the object list and add `dirent.o` alphabetically.

- [ ] **Step 12.5: Run test, expect pass**

```bash
tests/test_libbboeos_qemu.py
```

Expected: `dirent: ok` appears in output; test passes.

- [ ] **Step 12.6: Commit**

```bash
git add tools/libc/dirent.c tools/libc/Makefile tests/test_libbboeos_qemu.py
git commit -m "libc: implement opendir/readdir/closedir/rewinddir on top of SYS_IO_GETDENTS"
```

---

## Task 13: Doc updates

**Files:**
- Modify: `docs/posix.md`
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 13.1: Update `docs/posix.md`**

Flip these rows from ❌ to ⚠️ (libc only, not reachable from cc.py-built programs):
- `opendir`
- `readdir`
- `closedir`
- `rewinddir`

Each row's Notes column:
- `opendir`: "tools/libc only; backs onto `SYS_IO_GETDENTS`."
- `readdir`: "tools/libc only; returns `(d_ino, d_type, d_name)`. `d_ino` is the ext2 inode for ext2, start sector for bbfs. `d_name[256]` matches glibc/musl/BSD."
- `closedir`: "tools/libc only."
- `rewinddir`: "tools/libc only; lseek to 0 + reset internal buffer."

Add note in section header about DT_* values populated (DT_REG, DT_DIR; never DT_UNKNOWN — every entry has a known type).

- [ ] **Step 13.2: Update `docs/CHANGELOG.md`**

Under Unreleased, add: `tools/libc` gains `<dirent.h>` (`opendir`/`readdir`/`closedir`/`rewinddir`) backed by `SYS_IO_GETDENTS`.

- [ ] **Step 13.3: Run wrap_md**

```bash
python3 tools/wrap_md.py docs/posix.md docs/CHANGELOG.md
```

- [ ] **Step 13.4: Commit**

```bash
git add docs/
git commit -m "docs: libc <dirent.h> rows + changelog"
```

---

## Task 14: Open PR 2

- [ ] **Step 14.1: Run libc test suite**

```bash
tests/test_libbboeos_qemu.py
```

Expected: pass.

- [ ] **Step 14.2: Run a smoke pass of the rest of CI**

```bash
tests/test_asm.py
tests/test_programs.py
```

Expected: pass. (libc work shouldn't touch shipped-program behavior, but smoke is cheap.)

- [ ] **Step 14.3: Open the PR**

```bash
git push -u origin bboe/opendir-readdir-pr2
gh pr create --title "feat(libc): <dirent.h> opendir/readdir/closedir/rewinddir" --body "..."
```

---

## Notes for the implementer

- **Asm register conventions.** The existing bbfs/ext2 directory functions use `ESI = fd_entry`, `EDI = user buffer`. The new `dir_emit` callback adds a `ctx` pointer — choose a free register (likely `EBP` since it's callee-saved in the BBoeOS asm conventions; verify against `src/arch/x86/syscall.asm`'s frame setup).
- **The "buffer-full" backoff.** When `dir_emit` rejects a record (CF set), the FS driver MUST NOT advance `fd_position` for the rejected entry. The next `getdents` call resumes by re-reading the same entry. Test this explicitly with a small caller buffer that exactly fits N-1 records.
- **Errno-from-asm.** The existing pattern stores the error code in a global the syscall epilogue reads. Match it; don't invent a new error-passing scheme.
- **ext2 image fixture.** The current `tests/test_bboefs.py` builds the ext2 image at test-fixture setup. Adding a long-name file may require extending the image-builder helper. Look for an existing helper that copies files into the ext2 image and extend it; don't roll a new one.
- **Renumbering safety.** Before pushing PR 1, `grep -rn "0x16\|0x17\|0x18" src/` to confirm no hand-written numeric INT 30h calls clash with the new IO numbers. (Convention says they all use `SYS_IO_*`, but verify.)
