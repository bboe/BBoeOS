---
title: BBoeOS
---

# BBoeOS

A minimal x86 operating system with a real-mode bootloader (`boot.bin`) and a
paged high-half kernel (`kernel.bin`) concatenated on disk, plus a shell, VFS
(bbfs + ext2), networking stack, self-hosted assembler, and C compiler. Boots
in 16-bit real mode, flips into flat 32-bit protected mode with paging, runs
the kernel at ring 0 and userland programs at ring 3 in per-program page
directories.

## Reference

- [Memory map](memory_map.html) — kernel-side fixed-physical regions and the per-program user-virt layout
- [File structure](file_structure.html) — file-by-file breakdown of `src/` and the host-side build scripts

## Source

The canonical instructions for working in the repository live in
[`CLAUDE.md`](https://github.com/bboe/bboeos/blob/main/CLAUDE.md) at the repo
root. The pages here host the longer reference tables that used to clutter
that file.
