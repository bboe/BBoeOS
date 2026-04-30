# BBoeOS

A minimal x86 operating system: a real-mode bootloader hands off to a
paged 32-bit protected-mode kernel that runs userland programs at
ring 3.  Includes a shell, VFS with bbfs and ext2 backends, NE2000
networking (ARP / IP / ICMP / UDP), a self-hosted assembler, and a
custom C subset compiler that translates `src/c/*.c` to NASM-compatible
assembly on the host.

The kernel ships as two flat binaries (`boot.bin` + `kernel.bin`)
concatenated on disk.  `boot.bin` is the MBR + post-MBR real-mode
bootstrap + 32-bit paging bring-up; `kernel.bin` is the high-half
kernel (`org 0xC0020000`) that owns drivers, filesystems, the network
stack, and the INT 30h syscall surface.  Each user program runs in its
own page directory built by `address_space_create`; the kernel half
(PDEs 768..1023) is copy-imaged from a single `kernel_idle_pd`, and
the user half holds the program's text, BSS, stack, and a shared
vDSO page.

## Dependencies

* nasm: `brew install nasm`
* python3 (for `add_file.py`, `cc.py`, and the `tests/` harness)

## Minimum runtime requirements

* **1 MB RAM** boots the shell and runs every program in `bin/`,
  including the self-hosted assembler (`asm`), the BSS-stress test
  (`bigbss`, 256 KB BSS), and the 448 KB-BSS editor (`edit`).  The
  kernel-side fixed-physical region — `kernel.bin` (~29 KB) + 4 KB
  kernel stack + first kernel PT + 8 KB frame bitmap — sits in
  conventional RAM below the VGA aperture at `0xA0000`.  Filesystem
  and NIC scratch frames are allocated dynamically from the bitmap
  allocator only when their subsystems initialize, so a no-NIC boot
  never spends those frames.  Default for the `tests/` harness is
  `qemu-system-i386 -m 1`.
* `qemu-system-i386` defaults to 128 MB, well above the 1 MB floor.
  Pass `-m 1` to exercise the minimum-RAM contract.

## Building and running BBoeOS

* Build the binary

    ./make_os.sh

* Run with QEMU:

    qemu-system-i386 -drive file=drive.img,format=raw

* Run with serial console:

    qemu-system-i386 -drive file=drive.img,format=raw -serial stdio

* Add a file to the filesystem:

    ./add_file.py <file>

* Run the self-hosting assembler test suite (diffs each program in `static/`
  against NASM output after reassembling it inside the OS):

    tests/test_asm.py            # full suite
    tests/test_asm.py edit       # one program; artifacts kept in a temp dir

## File Structure

```
src/arch/x86/         Architecture-specific code
  boot/boot.asm       Pre-paging boot binary: MBR + post-MBR + early-PE bootstrap
  boot/vga_font.asm   Boot-time BIOS ROM font copy into char-gen slot 0x4000
  kernel.asm          Post-paging high-half kernel (org 0xC0020000)
  entry.asm           protected_mode_entry, IRQ 0 / IRQ 6 handlers, shell respawn
  idt.asm             32-bit IDT, exception stubs, INT 30h gate
  syscall.asm         INT 30h dispatch table
  system.asm          reboot (8042), shutdown (APM / QEMU / Bochs)
src/drivers/          ATA, FDC, NE2000, PS/2, RTC, VGA, console, serial
src/fs/               block I/O dispatch, VFS, bbfs, ext2, fd table
src/include/          Shared constants and helper includes
src/lib/              shared_print_*, shared_die / shared_exit / shared_parse_argv
src/memory_management/  Bitmap frame allocator (frame.asm)
src/net/              ARP, IP, ICMP, UDP
src/syscall/          Per-subsystem INT 30h handlers (fs, io, net, rtc, sys)
src/c/                User-space programs (C sources, compiled by cc.py)
add_file.py           Host-side script to add files to drive image
cc.py                 Host-side C subset compiler
make_os.sh            Build script
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a detailed history of changes by version and date.

## Resources

* https://neosmart.net/wiki/mbr-boot-process/
* https://en.wikibooks.org/wiki/X86_Assembly/Bootloaders
* http://www.ousob.com/ng/asm/ng1f806.php
* https://en.wikipedia.org/wiki/BIOS_interrupt_call
* ftp://ftp.embeddedarm.com/old/saved-downloads-manuals/EBIOS-UM.PDF
