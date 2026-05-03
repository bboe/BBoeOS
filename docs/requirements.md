---
title: Requirements
nav_order: 30
---

# Requirements

External tools the build, the OS image, and the test suites depend on.

## Build (`./make_os.sh`)

Always required:

- **`nasm`** — assembles `boot.bin`, `kernel.bin`, every cc.py-emitted `.asm`, and the user programs.
- **POSIX shell utilities** — `cat`, `dd`, `dirname`, `find`, `mkdir`, `printf`, `rm`, `sed`, `sort`, `tr`, `wc`. All are part of the macOS and Linux base systems.
- **`python3`** — runs `cc.py` (the C-subset compiler) and `add_file.py` (the host-side image writer). Standard library only — no `pip install` step needed.

Required only when building with `--ext2`:

- **`debugfs`** — `add_file.py` shells out to it to write files, set inode flags, and create directories inside the ext2 image.
- **`mke2fs`** — `make_os.sh` invokes it directly to format the ext2 partition inside the disk image.

Both ship together in the **`e2fsprogs`** package.

## Running the OS

- **`qemu-system-i386`** — the primary target. `qemu-system-x86_64 -machine pc` also works.

## Tests

All test runners are bare-Python QEMU drivers; they need every build dependency above plus `qemu-system-i386`.

Tests under `tests/unit/` use **`pytest`** (`pip install pytest`).

The libc tests need **`clang`**:

- `tests/unit/test_libbboeos.py` runs clang in its host-native default mode to compile each unit test's tiny C program.  Same compiler as the smoke test below, so installing one covers both.
- `tests/test_libbboeos_qemu.py` (the libc on-OS smoke test) drives the freestanding cross-compile (`--target=i386-pc-none-elf -m32 -ffreestanding -nostdinc -nostdlib`) plus a few build-system tools:

  - **`make`** — invoked as `make -C tools/libc`; reuses the Makefile's CFLAGS so the test doesn't duplicate them.
  - **`ld`** (GNU BFD ld from `binutils`) — links the test binary against `libbboeos.a` and the `tools/libc/program.ld` linker script.
  - **`ar`** (GNU `ar`, also from `binutils`) — packs the libc objects into `libbboeos.a`.

## Install commands

Ubuntu / Debian:

```sh
sudo apt-get install -y e2fsprogs nasm qemu-system-x86
# Plus, for the libc on-OS smoke test:
sudo apt-get install -y binutils clang make
```

macOS (Homebrew):

```sh
brew install e2fsprogs nasm qemu
# Plus, for the libc on-OS smoke test (clang + make + ld ship with Xcode CLT):
xcode-select --install
```

### macOS gotcha: `e2fsprogs` is keg-only

Homebrew installs `e2fsprogs` keg-only on macOS, so `mke2fs` and `debugfs` are not on `$PATH` by default. Either symlink them or add the keg's `sbin/` to your shell's `PATH`:

```sh
export PATH="$(brew --prefix e2fsprogs)/sbin:$PATH"
```

This only matters for `--ext2` builds — the default `bbfs` build never touches either tool.
