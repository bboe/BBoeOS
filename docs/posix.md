---
title: POSIX compatibility
nav_order: 85
---

# POSIX compatibility

BBoeOS is *POSIX-shaped*, not POSIX-compliant.  The shell, fd table, syscall
numbering, signal handling, errno-style error returns, and a handful of
recognisable utilities (`cat`, `ls`, `rm`, `echo`, pipes, `$?`, `&&` / `||`)
will all feel familiar to anyone who has used a Unix.  Underneath, large chunks
of POSIX are missing on purpose: there is no `fork` (only `exec`), only three
signals exist, networking is a custom UDP / ICMP datagram syscall (no BSD socket
API, no TCP), files have no `mtime` / `uid` / `gid` / mode bits, `stat()` is a
stub, and there is no concept of a working directory or environment.

This document is the honest accounting of what is implemented, what is partly
implemented (with notes on what is missing), and what is not implemented at all,
so contributors and prospective porters can answer "can I port my program?" in
one read.

Two userland code paths exist today:

- **cc.py-compiled programs** — everything currently shipped in `bin/`.  These
  reach the OS through a small in-kernel vDSO of `FUNCTION_*` helpers at
  user-virt `0x10000` and through raw `INT 30h` syscall wrappers.  No POSIX libc
  is linked in.
- **clang-compiled programs linked against `tools/libc/`** — today only the
  standalone `hello` test binary built by `tests/test_libbboeos_qemu.py`. This
  is a parallel libc waiting to be wired up, originally cut for a Doom port. Its
  functions are real (102 implemented, 8 stubs) but they cannot be called from a
  shipped program until cc.py learns to emit ELF + link an archive.

For deeper detail on individual subsystems, see:

- [Syscall interface](syscalls.html) — full `INT 30h` reference.
- [Programs](programs.html) — usage and source pointers for every shipped
  utility.
- [Architecture](architecture.html) — signal delivery, cooperative pipes,
  paging.
- [Memory map](memory_map.html) — fixed-physical and per-program virtual layout.

## Status legend

- ✅ — fully implemented; no POSIX gap that matters for typical use.
- ⚠️ — partial; see the Notes column for what is missing or different.
- ❌ — not implemented.

In the syscall / libc table the "In shipped programs?" column answers a
different question: can a cc.py-built program in `bin/` reach this today?

- ✅ — yes, via vDSO `FUNCTION_*` helper or raw `INT 30h` wrapper.
- ⚠️ — only via `tools/libc/`, which is not linked into shipped programs yet.
- ❌ — no, regardless of which path; the kernel does not implement it.

## Userland utilities

The shipped programs that share a name with a standard POSIX utility. Behaviour
notes name only the gap relative to POSIX; see [Programs](programs.html) for
usage.

| Utility | Status | Notes |
|---------|:------:|-------|
| `cat` | ⚠️ | Single file or stdin; no `-n`, `-v`, `-e`, `-A`; no multi-file concat. |
| `chmod` | ⚠️ | `+x` / `-x` only — no octal modes, no rwx, no group/other bits (the FS has only `FLAG_EXECUTE` + `FLAG_DIRECTORY`). |
| `cp` | ⚠️ | Single file to single destination; preserves the execute bit; no `-r`, `-p`, `-i`, no directory destinations. |
| `date` | ⚠️ | Prints `YYYY-MM-DD HH:MM:SS` from the RTC.  No format string, no `-u`, no `-d`. |
| `echo` | ⚠️ | Space-separated, newline-terminated.  No `-n`, no `-e` escape interpretation. |
| `false` | ✅ | Exits 1. |
| `head` | ⚠️ | Default 10 lines; supports `-n N`.  No `-c`, no multi-file. |
| `ls` | ⚠️ | Appends `/` to dirs and `*` to executables.  No `-l`, `-a`, `-R`, `-1`, `-t`; no sort flags. |
| `mkdir` | ⚠️ | Creates one subdirectory under root only.  No `-p`, no `-m`. |
| `mv` | ⚠️ | Same-directory rename only — cannot move across directories (the FS is one level deep).  No `-f`, `-i`. |
| `ping` | ⚠️ | ICMP echo; resolves names via the same DNS path as `dns`.  No `-c`, `-i`, `-W`, `-s` flags. |
| `rm` | ⚠️ | Single file; refuses files with `FLAG_PROTECTED`.  No `-r`, `-f`, no globbing. |
| `rmdir` | ✅ | Removes an empty subdirectory. |
| `seq` | ⚠️ | GNU-style (POSIX has no `seq`).  `seq [start] end`; no step, no `-f`, no `-s`. |
| `sleep` | ❌ | Not shipped as a program.  `sleep_forever` is a test fixture, not POSIX `sleep`.  Use `sleep_ms()` from `tools/libc` or `SYS_RTC_SLEEP` directly. |
| `tail` | ⚠️ | Default 10 lines; supports `-n N`.  No `-c`, no `-f` (follow), no multi-file. |
| `tee` | ⚠️ | Single output file; no `-a` (append), no multi-file fan-out. |
| `true` | ✅ | Exits 0. |
| `wc` | ⚠️ | Supports `-l`, `-w`, `-c`.  No multi-file summary line, no `-m` (chars). |
| `yes` | ⚠️ | GNU-style (not in POSIX).  Repeats forever; no count limit. |
| `awk` | ❌ | Not shipped. |
| `basename` | ❌ | Not shipped. |
| `cmp` | ❌ | Not shipped. |
| `cut` | ❌ | Not shipped. |
| `df` | ❌ | Not shipped. |
| `diff` | ❌ | Not shipped. |
| `dirname` | ❌ | Not shipped. |
| `du` | ❌ | Not shipped. |
| `env` | ❌ | Not shipped — BBoeOS has no environment variables. |
| `expr` | ❌ | Not shipped. |
| `find` | ❌ | Not shipped. |
| `getopts` | ❌ | Not shipped (would need a shell that exposes argv to scripts; BBoeOS has neither shell scripts nor getopts). |
| `grep` | ❌ | Not shipped. |
| `id` | ❌ | Not shipped — BBoeOS has no users. |
| `kill` | ❌ | Not shipped — there is no PID model; only the shell can spawn children via `SYS_SYS_PIPELINE2`. |
| `ln` | ❌ | Not shipped — the FS has no hard or symbolic links. |
| `more` / `less` | ❌ | Not shipped. |
| `nl` | ❌ | Not shipped. |
| `od` | ❌ | Not shipped. |
| `paste` | ❌ | Not shipped. |
| `printenv` | ❌ | Not shipped — no environment variables. |
| `ps` | ❌ | Not shipped — there is no process table exposed to userland; only the shell + up-to-two pipeline children exist. |
| `pwd` | ❌ | Not shipped — there is no working directory; all paths are root-relative. |
| `sed` | ❌ | Not shipped. |
| `sort` | ❌ | Not shipped. |
| `split` | ❌ | Not shipped. |
| `stty` | ❌ | Not shipped — there is no termios. |
| `tar` | ❌ | Not shipped. |
| `touch` | ❌ | Not shipped — the FS has no mtime to update; cc.py-built programs can `> file` to create. |
| `tr` | ❌ | Not shipped. |
| `tty` | ❌ | Not shipped — there is no /dev/tty abstraction. |
| `umask` | ❌ | Not shipped — there are no mode bits. |
| `uname` | ❌ | Not shipped. |
| `uniq` | ❌ | Not shipped. |
| `who` | ❌ | Not shipped — no users, no `utmp`. |
| `xargs` | ❌ | Not shipped. |

## Shell

The shell (`src/c/shell.c`) supports line editing (history, `Ctrl+R` reverse
search, `Ctrl+K`, `Ctrl+Y`), command chaining with `;`, `&&`, `||` (bash
semantics, equal precedence, left-associative), I/O redirection with `<`, `>`,
`>>`, and a single `|` pipe between exactly two commands.  `$?` expands to the
last command's exit status.

The dispatch chain in `dispatch_buffer()` only recognises three builtins —
everything else execs `bin/<name>`.

| Builtin | Status | Notes |
|---------|:------:|-------|
| `help` | ⚠️ | BBoeOS-specific; prints `Commands: help reboot shutdown`. |
| `reboot` | ⚠️ | BBoeOS-specific; triple-faults via the 8042 keyboard controller. |
| `shutdown` | ⚠️ | BBoeOS-specific; tries APM / QEMU / Bochs hooks. |
| `cd` | ❌ | No per-process working directory. |
| `pwd` | ❌ | No working directory to print. |
| `exit` | ❌ | The shell never exits; `Ctrl+D` is a shortcut for `shutdown`. |
| `export` / `set` / `unset` | ❌ | No environment, no shell variables (only the parser-level `$?`). |
| `alias` / `unalias` | ❌ | No aliases. |
| `read` | ❌ | No shell-script read; the line editor is for the interactive prompt only. |
| `type` / `command` / `which` | ❌ | No name-lookup builtin. |
| `jobs` / `fg` / `bg` / `wait` | ❌ | No job control, no backgrounding (`&`). |
| `trap` | ❌ | No script-level signal handling. |
| `source` / `.` | ❌ | No shell scripts. |
| `times` | ❌ | No per-process time accounting. |
| `ulimit` | ❌ | No resource limits. |

## System calls and C library

Each row names a POSIX function, the BBoeOS syscall (or vDSO helper) that backs
it where one exists, and whether a cc.py-built program in `bin/` can call it
today.

### Process control

There is no `fork`.  `SYS_SYS_EXEC` replaces the current image in place (Linux
SysV i386 startup frame: `argc / argv / NULL / empty envp`). `SYS_SYS_PIPELINE2`
is shell-only and spawns exactly two cooperatively- scheduled children with
`cmd1.stdout | cmd2.stdin` — see [Architecture › Cooperative
pipes](architecture.html#cooperative-pipes-cmd1--cmd2).

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `_exit` | `SYS_SYS_EXIT` (F2h) | ✅ | ✅ | Reloads shell; child status returned only via `SYS_SYS_PIPELINE2`. |
| `exit` | libc → `_exit` | ⚠️ | ⚠️ | Only via `tools/libc`; runs up to 8 `atexit` callbacks then `_exit`. |
| `execve` | `SYS_SYS_EXEC` (F1h) | ⚠️ | ✅ | No `envp` (always empty); no path search (shell adds the `bin/` retry); recursive exec from a child rejected with `EINVAL`. |
| `execv` / `execvp` / `execle` / … | — | ❌ | ❌ | Wrappers not provided; spell the argv array yourself and call `SYS_SYS_EXEC`. |
| `fork` / `vfork` / `clone` | — | ❌ | ❌ | Not implemented; no plan to add.  Only the shell's `SYS_SYS_PIPELINE2` creates additional processes. |
| `wait` / `waitpid` / `waitid` | — | ❌ | ❌ | `SYS_SYS_PIPELINE2` returns child 2's exit status to the shell, but there is no general wait API. |
| `system` | libc (stub) | ⚠️ | ⚠️ | `tools/libc` stub — always returns `-1`. |
| `atexit` | libc | ⚠️ | ⚠️ | `tools/libc` only; 8 slots. |
| `getpid` / `getppid` | — | ❌ | ❌ | No PID model. |
| `getuid` / `geteuid` / `getgid` / `getegid` | — | ❌ | ❌ | No users. |
| `setuid` / `setgid` / `setgroups` | — | ❌ | ❌ | No users. |
| `setpgrp` / `setsid` / `getpgrp` | — | ❌ | ❌ | No process groups or sessions. |

### Signals

Three signals exist: `SIGINT` (2), `SIGPIPE` (13), `SIGALRM` (14).  Handlers
register via `SYS_SYS_SIGNAL`; the vDSO trampoline at handler end calls
`SYS_SYS_SIGRETURN` to restore context.  Delivery, dispatch modes, and
cooperative interruption of blocking syscalls are documented in [Architecture ›
Signal delivery](architecture.html#signal-delivery).

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `signal` | `SYS_SYS_SIGNAL` (F6h) | ⚠️ | ✅ | Only three signums accepted (SIGINT, SIGPIPE, SIGALRM); bad signum → `EINVAL`. |
| `alarm` | `SYS_RTC_ALARM` (30h) | ✅ | ✅ | Second-granularity wrapper around the ms-granularity syscall. |
| `setitimer` | `SYS_RTC_ALARM` (30h) | ⚠️ | ✅ | First-fire + interval supported (POSIX `ITIMER_REAL` shape), but fires only `SIGALRM`. |
| `sigreturn` (internal) | `SYS_SYS_SIGRETURN` (F7h) | ✅ | ✅ | Invoked by the vDSO trampoline, not by user code. |
| `sigaction` | — | ❌ | ❌ | No `sa_mask`, no `SA_RESTART`, no `siginfo_t`. |
| `sigprocmask` / `sigpending` / `sigsuspend` | — | ❌ | ❌ | No mask / pending / suspend semantics. |
| `kill` / `killpg` / `raise` | — | ❌ | ❌ | Only the kernel posts signals (SIGINT from Ctrl+C, SIGPIPE from broken pipe writes, SIGALRM from the timer). |
| Real-time signals | — | ❌ | ❌ | No `SIGRTMIN`..`SIGRTMAX`, no queueing. |
| Job-control signals (SIGSTOP / SIGCONT / SIGTSTP / SIGTTIN / SIGTTOU) | — | ❌ | ❌ | No job control. |
| All other POSIX signals (SIGHUP, SIGQUIT, SIGTERM, SIGCHLD, SIGUSR1/2, SIGBUS, SIGSEGV, SIGILL, SIGFPE, …) | — | ❌ | ❌ | Faults (#PF, #UD, #DE) `EXC0D`-trap and exit the program; they are not delivered as signals. |

### File I/O

`open` flags are `O_RDONLY` (00h), `O_WRONLY` (01h), `O_CREAT` (10h), `O_TRUNC`
(20h); `O_RDWR` is the bitwise OR of `O_RDONLY | O_WRONLY`.  `fstat` returns
`(mode, size)` only — no `mtime`, `nlink`, `uid`, `gid`, or inode number.

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `open` | `SYS_IO_OPEN` (15h) | ⚠️ | ✅ | No `O_APPEND`, `O_EXCL`, `O_NONBLOCK`, `O_DIRECTORY`, `O_CLOEXEC`; `mode` arg ignored. |
| `close` | `SYS_IO_CLOSE` (10h) | ✅ | ✅ | Flushes the dirty bit so `fd_close` updates the on-disk size. |
| `read` | `SYS_IO_READ` (16h) | ✅ | ✅ | Works on files, directories (raw entries), pipes, console, network, devices. |
| `write` | `SYS_IO_WRITE` (18h) | ✅ | ✅ | Same fd-type coverage as `read`. |
| `lseek` | `SYS_IO_SEEK` (17h) | ⚠️ | ✅ | `SEEK_SET` / `SEEK_CUR` / `SEEK_END` on files; clamped to `[0, size]`.  Pipes / devices are unseekable (no `ESPIPE` — returns success with no movement). |
| `dup` | `SYS_IO_DUP` (11h) | ✅ | ✅ | Singleton devices (e.g. `/dev/vga`) refuse with an error. |
| `dup2` | `SYS_IO_DUP2` (12h) | ✅ | ✅ | Linux semantics: `dup2(N, N)` is a no-op; otherwise the target fd is closed first. |
| `ioctl` | `SYS_IO_IOCTL` (14h) | ⚠️ | ✅ | Per-device command dispatch (console, VGA, audio, MIDI); no `F_SETFL`-style file-flag ioctls. |
| `fstat` | `SYS_IO_FSTAT` (13h) | ⚠️ | ✅ | Returns only `(FLAG_*, size)`; no `struct stat` in full POSIX shape. |
| `stat` / `lstat` | libc (stub) | ⚠️ | ⚠️ | `tools/libc` stub — always returns `-1`. |
| `fcntl` | — | ❌ | ❌ | No `F_GETFL`, `F_SETFL`, `F_GETFD`, `F_SETFD`, `F_DUPFD`, locks. |
| `pipe` / `pipe2` | — | ❌ | ❌ | The kernel implements `FD_TYPE_PIPE_R` / `FD_TYPE_PIPE_W` but exposes them only through `SYS_SYS_PIPELINE2` (shell-only). |
| `mkfifo` / `mknod` | — | ❌ | ❌ | No named pipes, no device nodes. |
| `link` / `symlink` / `readlink` / `unlinkat` | — | ❌ | ❌ | No links of either kind. |
| `truncate` / `ftruncate` | — | ❌ | ❌ | |
| `sync` / `fsync` / `fdatasync` | — | ❌ | ❌ | Writes hit the disk on `close` via the dirty bit. |
| `access` / `faccessat` | — | ❌ | ❌ | No permission model to query. |
| `umask` | — | ❌ | ❌ | No mode bits. |
| `select` / `pselect` / `poll` / `ppoll` / `epoll_*` | — | ❌ | ❌ | No multiplexed wait; ioctls return immediately when no data is available. |

### Filesystem

`bbfs` (the built-in floppy FS) and `ext2` (read-write, with some limitations)
are both single-level under root.

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `chmod` | `SYS_FS_CHMOD` (00h) | ⚠️ | ✅ | Sets `FLAG_EXECUTE` / `FLAG_DIRECTORY` only.  No rwx, no setuid/setgid/sticky. |
| `mkdir` | `SYS_FS_MKDIR` (01h) | ⚠️ | ✅ | One level under root only; no `mode` arg; `tools/libc` wrapper is a stub returning `-1`. |
| `rmdir` | `SYS_FS_RMDIR` (03h) | ✅ | ✅ | Returns `ERROR_NOT_EMPTY` (mapped to `EACCES` in libc, not `ENOTEMPTY`) if the directory is non-empty. |
| `rename` | `SYS_FS_RENAME` (02h) | ⚠️ | ✅ | Same-directory rename only — cannot move across directories.  `tools/libc` `rename()` is a stub. |
| `unlink` / `remove` | `SYS_FS_UNLINK` (04h) | ⚠️ | ✅ | Files only; cannot unlink a directory; `tools/libc` `remove()` is a stub. |
| `opendir` / `readdir` / `closedir` / `rewinddir` | — | ❌ | ❌ | Directories are read as raw files; programs decode the on-disk 32-byte entry layout themselves. |
| `chdir` / `fchdir` / `getcwd` | — | ❌ | ❌ | No working directory. |
| `chown` / `fchown` / `lchown` | — | ❌ | ❌ | No ownership. |
| `chroot` / `mount` / `umount` | — | ❌ | ❌ | |
| `statvfs` / `fstatvfs` | — | ❌ | ❌ | |

### Memory

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `brk` | `SYS_SYS_BREAK` (F0h) | ✅ | ✅ | No error path. |
| `sbrk` | `SYS_SYS_BREAK` (F0h) | ✅ | ✅ | libc wrapper or do the math inline. |
| `malloc` / `free` / `calloc` / `realloc` | libc | ⚠️ | ⚠️ | `tools/libc` only — real `sbrk`-backed free-list allocator with coalescing.  Shipped cc.py programs roll their own or stay statically sized. |
| `mmap` | `SYS_VIDEO_MAP` (40h) | ⚠️ | ✅ | Maps the mode-13h VGA framebuffer (320×200×8bpp) at user-virt `0xB8000000`.  No file or anonymous mmap. |
| `munmap` / `mprotect` / `mremap` / `mlock` / `madvise` | — | ❌ | ❌ | |
| `shm_open` / `shmget` / `shmat` / `shmdt` | — | ❌ | ❌ | No shared memory. |

### Time

`SYS_RTC_DATETIME` returns a bare unsigned epoch second; there is no `struct
timespec`.  `SYS_RTC_SLEEP` busy-waits and is interruptible by SIGINT or SIGALRM
(returns `EINTR`).

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `time` | `SYS_RTC_DATETIME` (31h) | ⚠️ | ✅ | Bare `unsigned int` — no `time_t *` argument convention. |
| `gettimeofday` | `SYS_RTC_MILLIS` (32h) | ⚠️ | ⚠️ | `tools/libc` wrapper returns monotonic ms-since-boot, *not* wall-clock; `tz` arg ignored. |
| `clock_gettime` / `clock_settime` / `clock_getres` | — | ❌ | ❌ | No `clockid_t`, no `CLOCK_MONOTONIC` proper. |
| `nanosleep` / `clock_nanosleep` | — | ❌ | ❌ | Use `sleep_ms()` (libc) or `SYS_RTC_SLEEP` directly. |
| `strftime` / `gmtime` / `localtime` / `mktime` / `asctime` / `ctime` | — | ❌ | ❌ | The vDSO `FUNCTION_PRINT_DATETIME` prints the canonical `YYYY-MM-DD HH:MM:SS` form. |
| `setitimer` / `getitimer` | `SYS_RTC_ALARM` (30h) | ⚠️ | ✅ | First-fire + interval (ms) supported; only the `ITIMER_REAL` flavour exists. |
| `times` | — | ❌ | ❌ | No per-process CPU accounting. |

### Networking

The networking syscalls are a custom datagram API rather than the BSD socket
model: `SYS_NET_OPEN` returns an fd specialised for `IPPROTO_UDP` or
`IPPROTO_ICMP`; `SYS_NET_SENDTO` / `SYS_NET_RECVFROM` carry destination IP +
port in fixed registers.  There is no TCP and no `socket(AF_INET, …)`
indirection.

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `socket` | `SYS_NET_OPEN` (21h) | ⚠️ | ✅ | Only `SOCK_RAW` (proto 0 = ICMP) and `SOCK_DGRAM` (proto = `IPPROTO_UDP` or `IPPROTO_ICMP`); no address-family argument. |
| `sendto` | `SYS_NET_SENDTO` (23h) | ⚠️ | ✅ | Custom register convention; UDP carries both ports, ICMP ignores them. |
| `recvfrom` | `SYS_NET_RECVFROM` (22h) | ⚠️ | ✅ | Non-blocking peek — returns 0 when no datagram is available; kernel-side block-on-wait is a tracked followup. |
| `close` | `SYS_IO_CLOSE` (10h) | ✅ | ✅ | Network fds close through the normal `io_close` path. |
| `bind` / `listen` / `accept` / `connect` | — | ❌ | ❌ | UDP sockets bind implicitly on first `sendto`. |
| `send` / `recv` / `sendmsg` / `recvmsg` | — | ❌ | ❌ | |
| `getsockopt` / `setsockopt` / `shutdown` | — | ❌ | ❌ | |
| `getsockname` / `getpeername` | — | ❌ | ❌ | |
| TCP (`SOCK_STREAM`, listen/accept, SYN handshake) | — | ❌ | ❌ | UDP / ICMP only. |
| `getaddrinfo` / `gethostbyname` / `getnameinfo` | — | ❌ | ❌ | Use the standalone `dns` program. |
| `if_nametoindex` / SIOCGIFADDR / netlink | — | ❌ | ❌ | One NIC, fixed name. |

### Terminal / TTY

Input handling is fixed in `drivers/console.c` (PS/2 + COM1 feeding fd 0, with
the line editor in the shell).  Programs cannot enter raw mode or control echo /
line discipline from userland.

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `isatty` / `ttyname` | — | ❌ | ❌ | |
| `tcgetattr` / `tcsetattr` / `cfgetispeed` / `cfsetispeed` / `tcdrain` / `tcflush` / `tcsendbreak` | — | ❌ | ❌ | No termios at all. |
| Pseudo-tty (`openpty`, `forkpty`, `/dev/pts/*`) | — | ❌ | ❌ | |

### Standard I/O (stdio)

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `printf` / `fprintf` / `vprintf` / `vfprintf` | libc + vDSO `FUNCTION_PRINTF` | ⚠️ | ✅ | The vDSO `FUNCTION_PRINTF` handles the common `%s %d %x %c %u` set; `tools/libc` `vsnprintf` is a fuller (314-line) format parser including width / precision / padding. |
| `sprintf` / `snprintf` / `vsprintf` / `vsnprintf` | libc | ⚠️ | ⚠️ | `tools/libc` only. |
| `fopen` / `fclose` / `fread` / `fwrite` / `fseek` / `ftell` / `fgetc` / `fputc` / `fputs` / `puts` / `getchar` / `putchar` | libc | ⚠️ | ⚠️ | `tools/libc` only.  cc.py-built programs use vDSO `FUNCTION_GET_CHARACTER` / `FUNCTION_PRINT_CHARACTER` / `FUNCTION_PRINT_STRING` / `FUNCTION_WRITE_STDOUT` directly. |
| `fgets` / `getline` / `getdelim` | — | ❌ | ❌ | Not in `tools/libc`. |
| `scanf` / `fscanf` / `vscanf` / `vfscanf` | — | ❌ | ❌ | |
| `sscanf` | libc (stub) | ⚠️ | ⚠️ | `tools/libc` stub — always returns 0. |
| `perror` / `clearerr` | — | ❌ | ❌ | `strerror` exists in libc. |
| `setvbuf` / `setbuf` | — | ❌ | ❌ | No buffered-IO modes. |
| `tmpfile` / `tmpnam` / `mkstemp` | — | ❌ | ❌ | |
| `feof` / `ferror` / `fflush` (no-op) | libc | ⚠️ | ⚠️ | `tools/libc` only; `fflush` is a no-op (no buffering to flush). |
| `remove` / `rename` (libc-layer) | libc (stubs) | ⚠️ | ⚠️ | `tools/libc` stubs — always return `-1`. |
| `rewind` | libc (no-op) | ⚠️ | ⚠️ | `tools/libc` no-op (does not seek). |

### String, ctype, stdlib

All of the rows below are implemented in `tools/libc/` (string.c, ctype.c,
stdlib.c) and exercised by `tests/test_libbboeos_qemu.py`'s `hello` binary — but
they are not reachable from cc.py-built shipped programs.

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `memcpy` / `memmove` / `memset` / `memcmp` / `memchr` | libc | ⚠️ | ⚠️ | `tools/libc` only. |
| `strcpy` / `strncpy` / `strcat` / `strncat` / `strcmp` / `strncmp` / `strcasecmp` / `strncasecmp` / `strchr` / `strrchr` / `strstr` / `strlen` / `strdup` / `strerror` | libc | ⚠️ | ⚠️ | `tools/libc` only. |
| Full `ctype.h` (`isalnum` / `isalpha` / `iscntrl` / `isdigit` / `isspace` / `islower` / `isupper` / `isprint` / `ispunct` / `isxdigit` / `tolower` / `toupper`) | libc | ⚠️ | ⚠️ | `tools/libc` only. |
| `qsort` / `bsearch` | libc | ⚠️ | ⚠️ | `tools/libc` only (Sedgewick quicksort, recursive binary search). |
| `strtol` / `strtoul` | libc | ⚠️ | ⚠️ | `tools/libc` only. |
| `atoi` / `atol` | libc | ⚠️ | ⚠️ | `tools/libc` only. |
| `atof` | libc (stub) | ⚠️ | ⚠️ | `tools/libc` stub — always returns `0.0`. |
| `abs` / `labs` | libc | ⚠️ | ⚠️ | `abs` only; no `labs` / `llabs`. |
| `rand` / `srand` | libc | ⚠️ | ⚠️ | LCG PRNG. |
| `getenv` | libc (stub) | ⚠️ | ⚠️ | `tools/libc` stub — always returns `NULL`.  No environment. |
| `setenv` / `unsetenv` / `putenv` / `clearenv` | — | ❌ | ❌ | No environment. |
| `system` | libc (stub) | ⚠️ | ⚠️ | `tools/libc` stub — always returns `-1`. |
| `locale` (`setlocale`, `LC_*`, collation, wide chars) | — | ❌ | ❌ | ASCII only. |

### Math

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `sin` / `cos` / `tan` / `atan2` / `sqrt` / `exp` / `log` / `log2` / `log10` / `pow` / `floor` / `ceil` / `fabs` (+ `f` variants) | libc | ⚠️ | ⚠️ | `tools/libc/math.c` — all implemented via x87 inline asm.  Float variants wrap the double form. |
| `asin` / `acos` / `atan` / `sinh` / `cosh` / `tanh` / `expm1` / `log1p` / `cbrt` / `hypot` / `fmod` / `modf` / `frexp` / `ldexp` / `round` / `trunc` | — | ❌ | ❌ | Not in `tools/libc/math.c`. |

### Process IPC

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| Anonymous pipes (kernel side) | `FD_TYPE_PIPE_R` / `FD_TYPE_PIPE_W` | ⚠️ | ⚠️ | Implemented; exposed only via `SYS_SYS_PIPELINE2` (shell creates the pipe pair on behalf of `cmd1 | cmd2`). |
| `pipe` / `pipe2` (user-callable) | — | ❌ | ❌ | |
| Named pipes (`mkfifo`) | — | ❌ | ❌ | |
| SysV message queues (`msgget` / `msgsnd` / `msgrcv`) | — | ❌ | ❌ | |
| SysV semaphores (`semget` / `semop` / `semctl`) | — | ❌ | ❌ | |
| SysV / POSIX shared memory | — | ❌ | ❌ | |
| POSIX message queues (`mq_*`) | — | ❌ | ❌ | |

### Threading

Single-threaded only.  Within a "program" there is no concurrency primitive
beyond signal handlers (which run on the same stack via the vDSO trampoline).

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `pthread_create` / `pthread_join` / `pthread_detach` / `pthread_*` (all of it) | — | ❌ | ❌ | |
| Mutexes / condition variables / barriers / rwlocks | — | ❌ | ❌ | |
| Atomics / `stdatomic.h` | — | ❌ | ❌ | |
| `clone` / `futex` (Linux-ism) | — | ❌ | ❌ | |

### Setjmp, errno, misc

`tools/libc` maps a subset of `ERROR_*` to errno: `ENOSPC`, `EEXIST`, `EFAULT`,
`EINTR`, `EINVAL`, `EACCES` (catch-all for `ERROR_NOT_EMPTY` /
`ERROR_NOT_EXECUTE` / `ERROR_PROTECTED`), `ENOENT`, with `EIO` as the default
fallback.  POSIX-distinct codes like `EBADF`, `EISDIR`, `ENOTDIR`, `ESPIPE`,
`ENOTEMPTY`, `EPERM` are not synthesised separately.

| POSIX function | Backing | Status | In shipped programs? | Notes |
|----------------|---------|:------:|:------:|-------|
| `setjmp` / `longjmp` | libc (asm) | ⚠️ | ⚠️ | `tools/libc/setjmp.S` — 6-slot `jmp_buf` (esp/ebp/eip + 3 callee-saved). |
| `sigsetjmp` / `siglongjmp` | — | ❌ | ❌ | No signal mask to save. |
| `assert` | libc | ⚠️ | ⚠️ | `tools/libc/assert.h` — `fprintf(stderr, …) + abort()`. |
| `abort` | libc | ⚠️ | ⚠️ | `tools/libc` only — exits 134. |
| `errno` | libc | ⚠️ | ⚠️ | `tools/libc` only — see code list above. |
| `getopt` / `getopt_long` | — | ❌ | ❌ | Individual programs roll their own argv parsing. |
| `regex` (`regcomp` / `regexec`) | — | ❌ | ❌ | |
| `dlopen` / `dlsym` / `dlclose` | — | ❌ | ❌ | All code is statically compiled in. |

## Filesystem metadata

| Field | bbfs | ext2 | Notes |
|-------|:----:|:----:|-------|
| File name (≤26 bytes) | ✅ | ✅ | Both filesystems store names in 27-byte slots. |
| File size (32-bit) | ✅ | ✅ | |
| File type (regular vs directory) | ✅ | ✅ | Tracked via `FLAG_DIRECTORY`. |
| Execute bit | ✅ | ✅ | The only "permission" bit — `FLAG_EXECUTE`. |
| Inode number | ❌ | ✅ | The ext2 driver tracks inodes; bbfs has no inode concept. |
| `mtime` / `atime` / `ctime` | ❌ | ❌ | |
| Owner uid / gid | ❌ | ❌ | |
| Mode bits (rwx ×3) | ❌ | ❌ | |
| Link count (`nlink`) | ❌ | ❌ | |
| Symbolic links | ❌ | ❌ | |
| Hard links | ❌ | ❌ | |
| Special files (block / char / FIFO / socket) | ❌ | ❌ | |
| Sparse files | ❌ | ❌ | |
| Extended attributes / ACLs | ❌ | ❌ | |
| Max path | ❌ | ❌ | Hard cap `MAX_PATH = 64`; only one `/` allowed (single-level subdirs under root). |

## BBoeOS-specific extras

These have no POSIX counterpart but are part of the OS surface area. Documented
here so a porter knows they exist (and is not surprised by unfamiliar names in
the source).

- **Programs**: `asm` (self-hosted assembler), `edit` (modal text editor),
  `draw` (40×25 VGA canvas), `arp` / `dns` (network diagnostics),
  `pipe_producer` / `pipe_consumer` / `pipe_drain` / `pipe_spam` (pipeline test
  fixtures), `recursive_exec_test`, `fd_helpers`, `exit_status`,
  `sleep_forever`, `uptime`.
- **Syscalls**: `SYS_VIDEO_MAP`, `SYS_NET_MAC`, `SYS_RTC_MILLIS`,
  `SYS_RTC_UPTIME`, `SYS_SYS_REBOOT`, `SYS_SYS_SHUTDOWN`, `SYS_SYS_PIPELINE2`.
- **vDSO helpers**: `FUNCTION_PRINT_IP`, `FUNCTION_PRINT_MAC`,
  `FUNCTION_PRINT_DATETIME`, `FUNCTION_PRINT_BYTE_DECIMAL`,
  `FUNCTION_PRINT_DECIMAL`, `FUNCTION_PRINT_HEX`.
- **Device fds**: `/dev/vga` (mode-13h framebuffer + palette ioctls),
  `/dev/audio` (SB16 PCM stream), `/dev/midi` (OPL3 register-write stream).
- **`tools/libc` extensions** (non-POSIX): `alarm_ms()`, `sleep_ms()`,
  `uptime_ms()`, `video_map()`.

## Updating this document

When adding a new `SYS_*` syscall, a shell builtin, or a POSIX-named program,
update the relevant table in `docs/posix.md` in the same commit (this mirrors
the existing `docs/CHANGELOG.md` + `docs/syscalls.md` discipline).  Run `python3
tools/wrap_md.py docs/posix.md` after editing.
