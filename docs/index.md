---
title: Home
nav_order: 10
---

# BBoeOS

A minimal x86 operating system with a real-mode bootloader (`boot.bin`) and a
paged high-half kernel (`kernel.bin`) concatenated on disk, plus a shell, VFS
(bbfs + ext2), networking stack, self-hosted assembler, and C compiler. Boots
in 16-bit real mode, flips into flat 32-bit protected mode with paging, runs
the kernel at ring 0 and userland programs at ring 3 in per-program page
directories.

## Start here

- [Getting started](getting_started.html) — build, boot, run a built-in, add a file, write a tiny C program
- [Requirements](requirements.html) — external tools the build, the OS image, and the test suites depend on
- [Programs](programs.html) — catalog of the executables that ship in `bin/`

## Writing programs

- [C subset reference](c_subset.html) — what `cc.py` accepts, the vDSO, and the compiler's builtin functions

## Reference

- [Architecture](architecture.html) — boot path, post-flip bring-up, ring-3 userland, paging and per-program address spaces, build-time derivation
- [Memory map](memory_map.html) — kernel-side fixed-physical regions and the per-program user-virt layout
- [Syscall interface](syscalls.html) — the `INT 30h` syscall table with argument-register conventions
- [File structure](file_structure.html) — file-by-file breakdown of `src/` and the host-side build scripts
- [Changelog](CHANGELOG.html) — detailed history of changes by version and date

## Source

The canonical instructions for working in the repository live in
[`CLAUDE.md`](https://github.com/bboe/BBoeOS/blob/main/CLAUDE.md) at the repo
root. The pages here host the longer reference tables that used to clutter
that file.
