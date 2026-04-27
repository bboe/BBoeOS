# BBoeOS

A minimal x86 operating system with a two-stage bootloader, shell, filesystem, networking stack, self-hosted assembler, and C compiler — all running in 16-bit real mode on a floppy disk.

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
src/include/          Shared includes
  constants.asm       Shared constants (memory addresses, filesystem params)
src/kernel/           Kernel assembly source
  ansi.asm            ANSI escape sequence parser, serial output
  bboeos.asm          Stage 1 boot code, shell loader, shared functions
  fd.asm              File descriptor table management (open, read, write, close)
  io.asm              Filesystem I/O (find_file, read_sector), visual_bell
  net.asm             NE2000 NIC driver: ARP, IP, ICMP, UDP
  syscall.asm         INT 30h syscall handler
  system.asm          Graphics mode, reboot, shutdown
src/asm/              User-space programs (assembly sources)
src/c/                User-space programs (C sources, compiled by cc.py)
add_file.py           Host-side script to add files to drive image
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
