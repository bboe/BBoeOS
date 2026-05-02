---
title: Programs
nav_order: 40
---

# Programs

Every executable that ships with BBoeOS lives as a single C file under `src/c/`, gets compiled by `cc.py`, assembled by NASM, and added to the disk image inside `bin/`. The shell first looks in the root directory, then retries with a `bin/` prefix — so `cat foo` finds `bin/cat`.

`shell.c` is the only program loaded directly by the kernel; everything else is launched via `SYS_SYS_EXEC` (which the shell invokes for any unknown command).

## Shell built-ins

These three commands are dispatched inside `shell.c` itself, not by execing a separate program:

| Command | Effect |
|---------|--------|
| `help` | Prints `Commands: help reboot shutdown`. |
| `reboot` | Triple-faults via the 8042 keyboard controller. |
| `shutdown` | Tries APM / QEMU / Bochs shutdown hooks. Falls through to `APM shutdown failed` if none worked. `Ctrl-D` at the prompt is a shortcut for the same call. |

## Filesystem

| Program | Usage | Source |
|---------|-------|--------|
| `cat` | `cat <filename>` — print a file to stdout. | `src/c/cat.c` |
| `chmod` | `chmod [+x\|-x] <file>` — set or clear the `FLAG_EXECUTE` bit. | `src/c/chmod.c` |
| `cp` | `cp <src> <dst>` — copy a file (preserves mode bits). | `src/c/cp.c` |
| `ls` | `ls [path]` — list directory entries; appends `/` to dirs and `*` to executables. | `src/c/ls.c` |
| `mkdir` | `mkdir <name>` — create a subdirectory under root. | `src/c/mkdir.c` |
| `mv` | `mv <old> <new>` — rename a file. Names are capped at 26 chars. | `src/c/mv.c` |
| `rm` | `rm <file>` — unlink. Refuses files with `FLAG_PROTECTED`. | `src/c/rm.c` |
| `rmdir` | `rmdir <dir>` — remove an empty subdirectory. | `src/c/rmdir.c` |

## Editors and tools

| Program | Usage | Notes |
|---------|-------|-------|
| `asm` | `asm <source> <output>` | Self-hosted x86 assembler. Resolves `%include` paths relative to the source's directory. |
| `draw` | `draw` | 40x25 colour canvas with arrow-key cursor; great for sanity-checking the VGA driver and palette. |
| `echo` | `echo [args...]` | Space-separated, newline-terminated. |
| `edit` | `edit <filename>` | Modal text editor with a 448 KB edit buffer + 2.5 KB kill buffer. |

## Time

| Program | Usage | Output |
|---------|-------|--------|
| `date` | `date` | Wall-clock time as `YYYY-MM-DD HH:MM:SS`, sourced from the RTC. |
| `uptime` | `uptime` | `HH:MM:SS` since boot, sourced from the PIT-driven tick counter. |

## Networking

All four NIC programs require QEMU to be launched with `-netdev user,id=net0 -device ne2k_isa,netdev=net0,irq=3,iobase=0x300`.

| Program | Usage | Notes |
|---------|-------|-------|
| `arp` | `arp <ip>` | Sends an ARP request and prints the resolved MAC. |
| `dns` | `dns <domain>` | UDP DNS query against the resolver QEMU's user-net stack hands out (10.0.2.3 by default). |
| `ping` | `ping <ip\|hostname>` | ICMP echo. Resolves names via the same DNS path. |

## Adding a new program

1. Drop a C file in `src/c/` (see [C subset reference](c_subset.html) for what `cc.py` accepts).
2. `./make_os.sh` — the build script auto-discovers every `*.c` under `src/c/`.
3. Boot, type the program name at the shell. Unknown names fall through to `bin/<name>`.

The shell's three built-ins (`help`, `reboot`, `shutdown`) are dispatched inside `shell.c`'s `else if (strcmp(buf, "name") == 0)` chain — adding a built-in means a new branch and an updated `help` string. Adding a regular external command means just a new `src/c/*.c` file.
