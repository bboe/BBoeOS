# BBoeOS

A minimal x86 operating system with a single-file bootloader-plus-kernel, shell, filesystem, networking stack, self-hosted assembler, and C compiler.  Boots in 16-bit real mode, flips into flat 32-bit ring-0 protected mode, and runs the shell and user programs from there.

## Dependencies

* nasm: `brew install nasm`
* python3 (for `add_file.py`)

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
  boot/bboeos.asm     Single flat-binary kernel: MBR + post-MBR kernel in one file
  boot/vga_font.asm   Boot-time BIOS ROM font copy into char-gen slot 0x4000
  entry.asm           protected_mode_entry, IRQ 0 / IRQ 6 handlers, shell respawn
  idt.asm             32-bit IDT, exception stubs, INT 30h gate
  syscall.asm         INT 30h dispatch table
  system.asm          reboot (8042), shutdown (APM / QEMU / Bochs)
src/drivers/          ATA, FDC, NE2000, PS/2, RTC, VGA, console, serial
src/fs/               block I/O dispatch, VFS, bbfs, ext2, fd table
src/include/          Shared constants and helper includes
src/lib/              shared_print_*, shared_die / shared_exit / shared_parse_argv
src/net/              ARP, IP, ICMP, UDP
src/syscall/          per-subsystem INT 30h handlers (fs, io, net, rtc, sys)
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
