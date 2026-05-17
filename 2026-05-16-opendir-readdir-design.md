# opendir / readdir / closedir / rewinddir — design

Date: 2026-05-16

## Goal

Add POSIX directory iteration to BBoeOS by restructuring the kernel↔userland
directory interface to match Linux's `getdents`-shaped API, then expose
`<dirent.h>` in `tools/libc`.

This is the first concrete POSIX gap the userland-libc-wiring branch (PR #383
and forward) has been pulling toward: a libc `opendir` is the most-requested
missing piece for portable C code that walks a directory.

## Background

Today every userland program that lists a directory does it by calling
`read(dir_fd, buf, 32)` and decoding the bytes inline. The kernel's
`vfs_read_dir` returns one 32-byte record per call in the bbfs on-disk format:

```
0..24   name (25 bytes; 24 chars + NUL)
25      flags (FLAG_DIRECTORY 02h, FLAG_EXECUTE 01h)
26..27  start_sector (uint16_t)
28..31  size (uint32_t)
```

For bbfs this is the natural format. For ext2 it's a **synthesis**:
`ext2_read_dir` (`src/fs/ext2.asm:642`) reads the ext2 on-disk dirent, looks up
the inode for the type bit, then writes a fake bbfs-shaped record into the user
buffer — truncating the ext2 inode number to 16 bits in the `start_sector`
slot and truncating filenames to 24 chars.

The fake-bbfs synthesis was a pragmatic shim, but it has two real costs: ext2
inodes are silently truncated when they don't fit in 16 bits, and ext2
filenames (up to 255 bytes on disk) are truncated to 24 chars at the
kernel↔user boundary.

Linux solved the same problem decades ago with `getdents` and the `dir_emit`
callback inside the VFS layer. Each filesystem's `iterate_shared` op calls
`dir_emit(name, namelen, ino, type)`; the VFS layer packs the results into
variable-length user-visible records. The on-disk format and the
kernel↔user API are deliberately decoupled.

We're going to do the same thing.

## Architecture

Two PRs:

- **PR 1 — Kernel `getdents` + `ls` migration.** Add the `dir_emit`-style
  callback to the VFS, add `SYS_IO_GETDENTS`, flip `read()` on a directory fd
  to return `EISDIR`, rewrite `ls.c` (the only raw-entry caller) to call
  `SYS_IO_GETDENTS` directly and sort its output.
- **PR 2 — libc `<dirent.h>`.** Add `opendir`/`readdir`/`closedir`/`rewinddir`
  on top of PR 1's syscall. Test via the `hello` libc test binary.

Each PR is self-contained and leaves main green.

## PR 1 — Kernel getdents + ls migration

### Syscall: `SYS_IO_GETDENTS`

Insert alphabetically in the IO group (between `FSTAT` 13h and `IOCTL`) at
**14h**. This shifts `IOCTL`/`OPEN`/`READ`/`SEEK`/`WRITE` up by one number.
Programs reference `SYS_*` symbolically (per the convention in CLAUDE.md), so
the shift is source-compatible — they just rebuild.

```
SYS_IO_GETDENTS 14h    ; BX=fd, DI=buffer, CX=count
                       ; returns AX=bytes written (0 at EOF), CF on error
```

Errors:
- Caller buffer too small for the next record → returns -1 / `ERROR_INVAL`
  (matches Linux's `EINVAL`). Iteration state is unchanged — a subsequent
  call with a larger buffer succeeds at the same position.
- fd is not a directory → -1 / `ERROR_NOT_DIRECTORY` (mapped to `ENOTDIR` in
  libc).
- Disk error → -1 / generic disk error.

### Wire record layout

Variable-length records packed in the user buffer:

```
offset 0:  uint32_t d_ino       // ext2 inode for ext2; start sector for bbfs
offset 4:  uint16_t d_reclen    // bytes of THIS record incl. padding
offset 6:  uint8_t  d_type      // DT_REG (8), DT_DIR (4), DT_UNKNOWN (0)
offset 7:  char     d_name[]    // null-terminated, padded so next d_ino aligns 4

// No d_off field — Linux includes one for seekdir/telldir resume; we don't
// support either, so the field would be dead weight.
```

`d_reclen = round_up(7 + namelen + 1, 4)`. Minimum 8 bytes (empty name + NUL).
Maximum 264 bytes (255-char ext2 name + NUL, padded to align). Callers walk by
adding `d_reclen` to the cursor; EOF when `getdents` returns 0.

**Why not the 16-bit-truncated `d_ino` we have today?** The existing 32-byte
entry already carries an ext2-inode-stuffed-into-`start_sector` field. We
could keep that and call it `d_ino` in libc — zero kernel work. Rejected
because ext2 images with >65535 inodes silently collide; the truncation is a
latent footgun every time someone mounts a non-toy image. Going Linux-style
removes it for free as a side effect of fixing the ext2 fake-bbfs synthesis.

### VFS contract change

Add a `dir_emit(ctx, name, namelen, ino, type)` callback in asm. The
`ctx` carries the user buffer kernel-virt pointer, total bytes remaining, and
current write offset. Both `bbfs_read_dir` and `ext2_read_dir` get rewritten
to call `dir_emit` instead of writing raw 32-byte entries.

`dir_emit` is responsible for:
- Computing `d_reclen` from `namelen`.
- Returning a "buffer full" indication to the FS driver so it stops walking
  for this syscall (and the next call resumes from the same position).
- Doing the user-buffer write (the FS driver doesn't touch user memory
  directly).

The result: ext2's fake-bbfs synthesis (the entire post-inode-read block in
`ext2_read_dir`) goes away. ext2 emits its native name length and full 32-bit
inode; bbfs emits up to 24 chars and a synthetic 32-bit inode (zero-extend the
start sector).

### `read()` on a directory

New error `ERROR_IS_DIRECTORY` in `src/include/constants.asm` (next free slot
in the error code group). `fd_read` dispatched to a directory-typed fd now
returns -1 with this error. The `fd_ops` table entry for `FD_TYPE_DIRECTORY`'s
read slot goes from `fd_read_dir` to `fd_read_isdir` (or equivalent).

libc maps `ERROR_IS_DIRECTORY` → `EISDIR` in `tools/libc/syscall.c`.

**Why not keep `read()` returning the legacy 32-byte format alongside
`getdents`?** It would let existing callers keep working without migration.
Rejected because there's exactly one caller (`ls.c`) and the whole point of
the refactor was to delete the bbfs-shaped synthesis path — keeping it
alongside the new code defeats the motivation. With only one program to
migrate, the cost of doing it right is one ≈30-line `ls.c` rewrite.

### ls.c rewrite

```c
int main(int argc, char *argv[]) {
    char *name = argc > 1 ? argv[1] : ".";
    int fd = open(name, O_RDONLY);
    if (fd < 0) {
        die("Not found\n");
    }

    char buf[4096];                 // 1 page; enough for any 48-entry dir
    int collected = 0;
    char *names[48];                // ptrs into a separate scratch arena
    unsigned char types[48];
    char arena[4096];
    int arena_used = 0;

    while (1) {
        int bytes = sys_io_getdents(fd, buf, sizeof buf);
        if (bytes == 0) {
            break;
        }
        int cursor = 0;
        while (cursor < bytes) {
            unsigned short reclen = *(unsigned short *)(buf + cursor + 4);
            unsigned char  type   = buf[cursor + 6];
            char          *src    = buf + cursor + 7;
            int            len    = strlen(src) + 1;
            memcpy(arena + arena_used, src, len);
            names[collected] = arena + arena_used;
            types[collected] = type;
            collected += 1;
            arena_used += len;
            cursor += reclen;
        }
    }

    qsort_pairs(names, types, collected);   // small inline qsort by name

    for (int i = 0; i < collected; i += 1) {
        write(STDOUT, names[i], strlen(names[i]));
        if (types[i] == DT_DIR) {
            putchar('/');
        }
        putchar('\n');
    }
    close(fd);
    return 0;
}
```

**The `*` execute suffix is dropped.** Today's `ls.c` prints `*` after
executable files based on `FLAG_EXECUTE` in the raw 32-byte entry. The
Linux `d_type` field deliberately doesn't expose execute-ness (that's a
permission bit, not a type), and POSIX `ls` without `-F`/`-p` flags
doesn't add suffixes. GNU `ls` only computes the execute indicator when
`-F` or `--color` is active, in which case it `stat()`s each entry. We
don't have a working `stat()` (the libc one is a stub returning -1) and
adding `-F` to our `ls` is out of scope for this PR. Net effect: BBoeOS
`ls` becomes more POSIX-conformant — just `/` after directories,
nothing else. The execute indicator can come back when `stat()` lands
and `-F` is implemented; that's a clean separate change.

**Why not extend the wire record with a flags byte?** Considered and
rejected. It would have let us keep the `*` without `stat()`, but
defeats the "type vs permission" separation Linux deliberately put into
`d_type`, and would carry FS-specific bits across the kernel↔user
boundary that no portable code knows what to do with. Better to do it
the way Linux does it (stat for permission queries) when we're ready.

### Tests

- `tests/test_bboefs.py`:
  - `getdents` on a non-empty root directory (bbfs and ext2): names returned,
    `d_type` correct, `d_ino` non-zero.
  - `getdents` on an empty directory: returns 0 first call.
  - `getdents` after the FS has deleted entries: slot reuse doesn't leak
    stale records (kernel skips zero/deleted entries — already the case for
    bbfs via `bbfs_read_dir`'s skip loop and for ext2 via the existing
    inode==0 skip).
  - `read()` on a directory fd returns the EISDIR error.
  - Long ext2 filenames (>24 chars) survive intact through `getdents`.
- `tests/test_programs.py`: `ls` output is sorted alphabetically.
- Full CI matrix locally (kernel-architecture change per the standing rule).

### Documentation

- `docs/syscalls.md`: new `SYS_IO_GETDENTS` row, renumbering note.
- `docs/posix.md`: drop the "Directories are read as raw files" note from the
  opendir row (still ❌ in PR 1, but the *reason* is "no libc wrapper"
  instead); update the file-I/O `read` row to note EISDIR on directories.
- `docs/CHANGELOG.md`: entry for the syscall + ls migration.

## PR 2 — libc `<dirent.h>`

### New files

- `tools/libc/include/dirent.h` — public header.
- `tools/libc/dirent.c` — implementation.

### Public header

```c
#ifndef BBOEOS_LIBC_DIRENT_H
#define BBOEOS_LIBC_DIRENT_H

#include <sys/types.h>

struct dirent {
    ino_t         d_ino;
    unsigned char d_type;
    char          d_name[256];   // matches glibc / musl / BSD convention
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

`d_name[256]` matches the universal libc convention (glibc, musl, FreeBSD).
sizeof(struct dirent) gives the worst case; actual names are shorter and
null-terminated. POSIX deliberately leaves the size unspecified and every
mainstream libc picks fixed 256.

### Internal DIR struct

Private to `dirent.c`:

```c
struct DIR {
    int            fd;
    int            buffer_bytes;    // bytes valid in buffer
    int            buffer_cursor;   // next record offset
    struct dirent  entry;           // returned-pointer slot
    unsigned char  buffer[4096];    // one getdents call worth
};
```

4 KB buffer is sized for ≈ 15 worst-case (255-char-name) records or many
shorter ones. Fits in one malloc.

### Implementation sketch

```c
DIR *opendir(const char *path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        return NULL;
    }
    DIR *d = malloc(sizeof *d);
    if (!d) {
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
        if (bytes <= 0) {
            return NULL;
        }
        d->buffer_bytes = bytes;
        d->buffer_cursor = 0;
    }
    unsigned char *rec = d->buffer + d->buffer_cursor;
    d->entry.d_ino   = *(uint32_t *)(rec + 0);
    d->entry.d_type  = rec[6];
    strcpy(d->entry.d_name, (char *)(rec + 7));
    d->buffer_cursor += *(uint16_t *)(rec + 4);
    return &d->entry;
}

int closedir(DIR *d) {
    int rc = close(d->fd);
    free(d);
    return rc;
}

void rewinddir(DIR *d) {
    lseek(d->fd, 0, SEEK_SET);
    d->buffer_bytes = 0;
    d->buffer_cursor = 0;
}
```

### errno additions

- `EISDIR` in `tools/libc/include/errno.h`.
- `ENOTDIR` in `tools/libc/include/errno.h` (if not already present).
- `ERROR_IS_DIRECTORY` → `EISDIR` and `ERROR_NOT_DIRECTORY` → `ENOTDIR` in
  `tools/libc/syscall.c`'s mapping table (replaces the `EIO` fallback for
  these specific codes).

### Tests

Extend the `hello` binary in `tests/test_libbboeos_qemu.py`:

```c
// dirent block in hello:
DIR *d = opendir("/");
assert(d != NULL);
int count = 0;
struct dirent *e;
while ((e = readdir(d)) != NULL) {
    printf("entry: %s type=%d ino=%u\n", e->d_name, e->d_type, e->d_ino);
    count += 1;
}
assert(count > 0);
rewinddir(d);
int count2 = 0;
while ((e = readdir(d)) != NULL) {
    count2 += 1;
}
assert(count == count2);
assert(closedir(d) == 0);
assert(opendir("nonexistent_path") == NULL);
```

Test harness greps the output for the printed entries and for "test:
dirent: ok".

### Documentation

- `docs/posix.md`: flip `opendir` / `readdir` / `closedir` / `rewinddir` rows
  from ❌ to ⚠️ (libc only, not reachable from cc.py-built programs yet).
  Note d_ino source per FS, d_type value set, d_name buffer size.
- `docs/CHANGELOG.md`: entry for the libc add.

## Out of scope

- `scandir`, `alphasort`, `seekdir`, `telldir` — convenient but no shipped
  code calls them; trivial to add later when an actual port asks.
- `getdents` from cc.py-built programs as a libc-level abstraction. They
  call `SYS_IO_GETDENTS` directly via INT 30h (this is what `ls.c` does in
  PR 1).
- Generalizing the `*` execute-suffix display in `ls` — requires `stat()` to
  land first.
- Multi-level FS hierarchy. Subdirectories still cap at one level under root.
- Long bbfs filenames. bbfs on-disk format is unchanged; only ext2 names can
  exceed 24 chars through this new path.

## Risks

- **VFS callback rework in asm.** `bbfs_read_dir` and `ext2_read_dir` are
  both in asm, with careful register conventions. Restructuring them around
  `dir_emit` is the bulk of PR 1's risk surface. Mitigation: the full FS
  test matrix locally before declaring done.
- **Wire format compatibility.** Once `SYS_IO_GETDENTS` ships, the record
  layout is a kernel↔user contract. We can extend (add fields after
  `d_name`) but can't reorder. Choose the offsets carefully on first
  landing.
- **Renumbering the IO group.** Five syscalls (IOCTL/OPEN/READ/SEEK/WRITE) shift up by one. Any
  hand-written INT 30h call sites using the numeric constant (instead of
  the `SYS_*` symbol) break. Convention is symbolic; grep before merging.

## Updating this document

If the design changes during implementation, update this file in place on
the `design-specs` branch (typically via `git mktree` + `git commit-tree`
plumbing so the feature worktree stays clean). The feature branch shares
no history with `design-specs`, so spec edits never need to be rebased
out of a feature PR.
