# BBoeOS

A minimal x86 bootloader and OS written in NASM assembly, running in 16-bit real mode.

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

    ./test_asm.py            # full suite
    ./test_asm.py edit       # one program; artifacts kept in a temp dir

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
add_file.py           Host-side script to add files to drive image
make_os.sh            Build script
```

## Known limitations / TODO

* **`edit` cannot open `asm.asm`.** The gap buffer is 20 KB at `0x2000` with
  the 2.5 KB kill buffer at `0x7000`, sandwiched between the edit binary
  (loaded at `PROGRAM_BASE` = `0x0600`) and the resident kernel (stage 1 MBR
  at `0x7C00`, stage 2 above it through `~0xE000`). The hard ceiling for a
  contiguous gap buffer in segment 0 is ~27 KB (the gap from just past the
  edit binary up to `0x7C00`); a separate ~4.5 KB of slack exists above the
  NIC buffers at `0xEE00`–`0xFFFF` and could host the kill buffer, but
  `static/asm.asm` is ~96 KB so neither rearrangement helps. The real fix is
  to relocate the gap buffer into its own segment(s): one segment at e.g.
  `1000h:0000` gets 64 KB; splitting across two segments gets 128 KB and
  clears `asm.asm` with headroom. Requires widening `gap_start`/`gap_end` to
  17-bit (or dword) and routing every `BUF_BASE` access through a
  segment-aware helper.

## Resources

* https://neosmart.net/wiki/mbr-boot-process/
* https://en.wikibooks.org/wiki/X86_Assembly/Bootloaders
* http://www.ousob.com/ng/asm/ng1f806.php
* https://en.wikipedia.org/wiki/BIOS_interrupt_call
* ftp://ftp.embeddedarm.com/old/saved-downloads-manuals/EBIOS-UM.PDF
