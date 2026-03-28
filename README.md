# BBoeOS

A minimal x86 bootloader and OS written in NASM assembly, running in 16-bit real mode.

## Dependencies

* nasm: `brew install nasm`

## Building and running BBoeOS

* Build the binary

    ./make_os.sh

* Run with QEMU:

    qemu-system-i386 -drive file=floppy.img,format=raw

* Run with serial console:

    qemu-system-i386 -drive file=floppy.img,format=raw -serial stdio

* Add a file to the filesystem:

    ./add_file.sh floppy.img <file>

## File Structure

```
src/include/          Shared includes
  constants.asm       Shared constants (memory addresses, filesystem params)
src/kernel/           Kernel assembly source
  bboeos.asm          Stage 1 boot code, shell loader, shared functions
  io.asm              Filesystem I/O (find_file, read_sector), visual_bell
  readline.asm        Line editor with cursor movement, kill/yank
  syscall.asm         INT 30h syscall handler
  system.asm          Graphics mode, reboot, shutdown
src/programs/         User-space programs
  shell.asm           Shell: CLI loop, command dispatch, built-in commands
add_file.sh           Host-side script to add files to floppy image
make_os.sh            Build script
```

## Resources

* https://neosmart.net/wiki/mbr-boot-process/
* https://en.wikibooks.org/wiki/X86_Assembly/Bootloaders
* http://www.ousob.com/ng/asm/ng1f806.php
* https://en.wikipedia.org/wiki/BIOS_interrupt_call
* ftp://ftp.embeddedarm.com/old/saved-downloads-manuals/EBIOS-UM.PDF
