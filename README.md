# BBoeOS

## Dependencies

* nasm: `brew install nasm`

## Building and running BBoeOS

* Build the binary

    ./make_os.sh

* Run with QEMU:

    qemu-system-i386 -drive file=floppy.img,format=raw

## Resources

* https://neosmart.net/wiki/mbr-boot-process/
* https://en.wikibooks.org/wiki/X86_Assembly/Bootloaders
* http://www.ousob.com/ng/asm/ng1f806.php
* https://en.wikipedia.org/wiki/BIOS_interrupt_call
* ftp://ftp.embeddedarm.com/old/saved-downloads-manuals/EBIOS-UM.PDF
