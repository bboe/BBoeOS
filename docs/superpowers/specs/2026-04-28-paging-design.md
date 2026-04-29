# Paging and Virtual Memory — Design

## Summary

Add 32-bit paging to BBoeOS. The kernel relocates to a Linux-shaped high-half virtual range (`0xC0000000+`); the kernel binary is loaded at physical `0x100000` (1 MB) instead of running in place at `0x7C00`. User programs link at virtual `0x08048000` and run in per-program page directories that are created at program load and destroyed at program exit. Ring-3 programs can no longer touch kernel memory.

This is the immediate post-ring-3 milestone. Ring 3 gave us privilege protection; this milestone gives us memory protection.

## Goals

- Memory protection: ring-3 programs cannot read or write any kernel memory; bad pointers `#PF` instead of silently corrupting state.
- Per-program virtual address space: each loaded program has a private page directory built at load and torn down at exit. CR3 changes only at program load, program exit, and the kill path.
- Linux-shaped layout: kernel mapped high (`0xC0000000+`); user programs occupy the low 3 GB; physical kernel relocated to `0x100000`.
- Real virtual memory for large buffers: `edit`'s 1 MB gap buffer becomes ordinary BSS instead of a hardcoded `0x100000` slab. The trailer-magic protocol widens from 16-bit to 32-bit `bss_size`.
- Multi-process foundation: per-address-space plumbing exists; only the scheduler is missing. A future scheduler can switch CR3 between PDs without rearchitecting.

## Non-goals (deferred)

- `SYS_MMAP` / runtime user-side allocation. BSS-via-trailer covers everything currently in `src/c/`.
- Demand-zero BSS. Eager allocation at program load. *Marked as a follow-up: when the page-fault handler grows a "fault inside declared BSS" branch, allocation moves from program load to first touch.*
- Demand-grown stack. Fixed-size 128 KB user stack allocated at program load.
- `kmalloc` / kernel heap. Kernel allocations remain static plus the frame allocator.
- `fork`, multi-process scheduler, copy-on-write, swap, `vmalloc`, highmem.
- 64-bit / PAE.

## Memory layout

### Physical (post-relocation)

| Range | Use | Lifecycle |
|---|---|---|
| `0x00000000..0x000003FF` | Real-mode IVT | Free after PE flip |
| `0x00000400..0x000004FF` | BIOS data area | Free after PE flip |
| `0x00000500..0x000005FF` | E820 table stash | MBR-time real-mode code writes here, PE-side bitmap init reads here |
| `0x00001000..0x00001FFF` | Boot PD (promoted to `kernel_pd_template`) | Bitmap-reserved forever |
| `0x00002000..0x00002FFF` | First kernel PT (covers physical `0..4 MB`; serves identity at PDE[0] and direct-map at PDE[768]) | Bitmap-reserved forever |
| `0x00007C00..0x00007DFF` | MBR | Free after PE flip |
| `0x00007E00..0x0000FFFF` | boot.asm / early-PE bootstrap | Free after dropping identity map |
| `0x00010000..0x000FFFFF` | Kernel image staging area | Free after early-PE copies it to `0x100000` |
| `0x000A0000..0x000FFFFF` | VGA / BIOS reserved | Bitmap-reserved forever |
| `0x00100000..(kernel_end)` | Relocated kernel image | Bitmap-reserved forever |
| `(kernel_end)..(top of RAM)` | Bitmap-allocated frames | User pages, PDs, user PTs, BSS |

The boot.asm code reads the kernel image into `0x10000` via INT 13h. Early-PE code copies it to `0x100000` and zeroes the source area.

### Virtual (every page directory)

| Range | Use | U/S | Source |
|---|---|---|---|
| `0x00000000..0x00000FFF` | NULL guard (unmapped) | — | `*NULL` faults |
| `0x00001000..0x0000FFFF` | Unmapped user low | — | Reserved for future heap / mmap |
| `0x00010000..0x00010FFF` | vDSO code page | R-X user | Shared physical frame across all PDs |
| `0x00011000..0x08047FFF` | Unmapped user low | — | Reserved for future heap / mmap |
| `0x08048000..(prog_end)` | Program text + data | user | Private frames per address space |
| `(prog_end)..(prog_end + bss_size)` | BSS, eager-zeroed | user | Private frames per address space |
| `0x3FFDF000..0x3FFDFFFF` | Stack guard (unmapped) | — | Overflow → clean `#PF` |
| `0x3FFE0000..0x3FFFFFFF` | User stack (128 KB) | user | Private frames per address space |
| `0x40000000` | Stack top (initial ESP) | — | — |
| `0x40000001..0xBFFFFFFF` | Unmapped user high | — | Reserved for future mmap |
| `0xC0000000..0xCFFFFFFF` | Kernel direct map (256 MB) | kernel | Constant offset to physical 0..256 MB |
| `0xC0100000..(kernel_end)` | Kernel image (within direct map) | kernel | Same physical pages as the rest of the direct map; just the kernel binary's portion |
| `0xD0000000..0xFFFFFFFF` | Reserved for future `vmalloc` / `kmap` / `fixmap` | — | No PT pre-allocated |

Notes:

- `__pa(virt) = virt - 0xC0000000` and `__va(phys) = phys + 0xC0000000` for any direct-map address.
- 64 kernel page tables cover `0xC0000000..0xCFFFFFFF` (256 MB direct map) and are installed in `kernel_pd_template` before any user PD is created. PDEs `[768..831]` point at them and never change again. Future `kmalloc`-style code lives inside this 256 MB window so kernel PDEs in user PDs never need fan-out updates.
- Kernel pages all carry the G (global) flag so a CR3 reload does not flush them from the TLB.
- The vDSO at `0x00010000..0x00010FFF` is established by an earlier milestone (see `2026-04-28-vdso-design.md`); the program loader maps the shared code page into every user PD as part of `prog_load`.  All per-call scratch state lives on the user stack — there is no vDSO data page.

## Boot path

The kernel splits into two flat-binary nasm outputs to handle the `org` change:

- **`boot.bin`** (`org 0x7C00`): MBR + post-MBR real-mode bootstrap + early-PE bootstrap. Loaded by BIOS at `0x7C00`; runs at low physical; never paged.
- **`kernel.bin`** (`org 0xC0100000`): everything else — IDT, drivers, VFS, syscall dispatcher, post-flip kernel entry. Linked at high virtual; resides at physical `0x100000` after relocation.

`make_os.sh` concatenates them on the floppy image; one disk read sequence still loads everything.

### Sequence

1. **BIOS → `0x7C00`.** MBR runs in real mode. Reads the post-MBR sectors of `boot.bin` into `0x7E00` (current behavior).
2. **Post-MBR real-mode** (in `boot.bin`):
   - INT 13h reads `kernel.bin` sectors → `0x10000`.
   - INT 15h `AX=E820` walks the BIOS memory map, writes entries to `0x500` (terminated by zero entry).
   - PIC remap, A20 enable, GDT load (existing 6-entry GDT: null, kernel code, kernel data, user code, user data, TSS).
   - Far-jump `0x08:early_pe_entry` flips into 32-bit PE, still at low physical.
3. **Early-PE bootstrap** (32-bit, low physical, in `boot.bin`):
   - Copy kernel image: `rep movsd` from `0x10000` to `0x100000`, length = recorded sector count × 512.
   - Build the boot PD at fixed physical `0x1000` and the first kernel PT at fixed physical `0x2000`.
   - Populate the first kernel PT as a constant-offset direct map for physical `0..4 MB`: `PTE[j] → (j * 0x1000)` with U/S=0, R/W=1, G=1. This single PT is used twice in the boot PD: at PDE[0] (identity-maps virtual `0..0x3FFFFF`) and at PDE[768] (direct-maps virtual `0xC0000000..0xC03FFFFF`). Identity and direct-map agree on physical content for this range, so one PT serves both.
   - Boot PD entries: PDE[0] → first kernel PT; PDE[768] → first kernel PT; all other PDEs not-present.
   - `mov cr3, 0x1000`; `or cr0, 0x80010000` (PG | WP).
   - Far-jump `0x08:high_entry`, where `high_entry` is at a kernel-virt address (`0xC01xxxxx`).
4. **High-half kernel** (32-bit, paging on, in `kernel.bin`):
   - Reload segment registers; set `esp` to the new `KERNEL_STACK_TOP` (kernel-virt address corresponding to physical `0x9FFF0`, i.e. `0xC009FFF0`).
   - Drop identity mapping: zero PDE[0] in the boot PD; full CR3 reload to flush stale TLB entries for the first 4 MB.
   - Promote boot PD: from this point it is `kernel_pd_template`, the source of truth for the top-256 PDEs that get copied into every new user PD.
   - Initialize bitmap frame allocator from the E820 table at virtual `0xC0000500`. Mark reserved: BIOS regions (VGA at `0xA0000..0xFFFFF`, etc.), kernel image (`0x100000..kernel_end`), boot PD at `0x1000`, first kernel PT at `0x2000`.
   - Allocate the remaining 63 kernel PTs from the bitmap and install them in `kernel_pd_template` as PDEs `[769..831]`, each populated as a direct map for its 4 MB physical region. These 63 PTs land within the first 4 MB of physical RAM (the bitmap's first-fit allocations skip the kernel image at `0x100000+`, then return frames in the still-mapped first-4 MB region), so they are reachable for population through the existing direct map without needing a temporary kmap slot. Invariant after this step: kernel-half PDEs `[768..831]` are immutable for the lifetime of the system.
   - Patch TSS, install IDT, install IRQ handlers, `ltr`, `sti`. (Existing `protected_mode_entry` work, now running at kernel-virt addresses.)
   - Driver / VFS / NIC inits.
   - First `shell_reload`.

## Data structures and core helpers

### Frame allocator (`memory_management/frame.asm`)

```nasm
frame_bitmap:       times (256*1024/32) db 0   ; 8 KB, one bit per 4 KB frame, 256 MB ceiling
frame_total:        dd 0                       ; total frames detected
frame_free:         dd 0                       ; running free count
frame_search_hint:  dd 0                       ; first-fit starting position
```

Public interface (CPL=0 only):

- `frame_alloc()` → `EAX = phys`, CF on OOM. First-fit scan from `frame_search_hint`. Caller zeroes if needed.
- `frame_free(eax = phys)` → clears the bit; updates `frame_search_hint` if `eax < hint`.
- `frame_init(esi = e820_ptr)` → marks "type 1" regions free, then marks reserved regions used at boot.

The 256 MB ceiling is enforced statically by the bitmap size. RAM beyond 256 MB is ignored. Easy to widen later by changing one constant.

### Address-space helpers (`memory_management/address_space.asm`)

Public interface (CPL=0 only):

- `address_space_create()` → `EAX = pd_phys`, CF on OOM. Allocates one frame, zeroes it, copies top-256 PDEs from `kernel_pd_template`. Does not switch CR3.
- `address_space_destroy(eax = pd_phys)` → walks user-half PDEs (0..767). For each present PDE: walks the PT, frees each present user-page frame, frees the PT frame. Finally frees the PD frame. Caller must not have `pd_phys` loaded in CR3.
- `address_space_map_page(eax = pd_phys, ebx = user_virt, ecx = phys, edx = flags)` → installs / replaces a PTE. Allocates a PT frame on demand if the relevant PDE is not-present. CF on OOM. Does not invalidate TLB; caller invalidates if `pd_phys == current CR3`.
- `address_space_unmap_page(eax = pd_phys, ebx = user_virt)` → clears the PTE. Does not free the underlying frame. Issues `invlpg` if `pd_phys == current CR3`.

### Program loader (`prog/load.asm`)

`prog_load(esi = path)`:

1. `vfs_find` → fail (CF) if not found.
2. `vfs_load` into a kernel-BSS scratch buffer (`program_scratch`, sized for the largest expected program image — start at 256 KB, grow if needed). The buffer lives in the kernel binary, so its kernel-virt address is fixed at link time and its physical pages are reserved as part of the kernel image. Read the trailer to extract `bss_size` (now 32-bit, see "Trailer change" below).
3. `address_space_create` → new PD.
4. For each user-virt page in `[0x08048000..(0x08048000 + binary_size + bss_size)]`:
   - `frame_alloc` → user frame.
   - For pages within the binary: kernel-side memcpy from scratch to the new frame's direct-map address.
   - For pages within BSS: zero the frame.
   - `address_space_map_page(new_pd, virt, frame_phys, P|RW|U)`.
5. For each user-virt page in `[0x3FFE0000..0x40000000]`:
   - `frame_alloc`, zero, `address_space_map_page` with `P|RW|U`.
6. Stack guard at `0x3FFDF000`: leave PDE/PTE not-present.
7. `mov cr3, new_pd_phys`.
8. `iretd` to ring 3 with `EIP=0x08048000`, `ESP=0x40000000`, `EFLAGS=0x202`, selectors `USER_CODE_SELECTOR` / `USER_DATA_SELECTOR`.

### `sys_exit` lifecycle

1. `old_pd = current CR3`.
2. `mov cr3, kernel_pd_template_phys` (now running with no user mappings).
3. `address_space_destroy(old_pd)`.
4. Restore `[shell_esp]`; fall into `shell_reload`.

`shell_reload` calls `prog_load` to build a fresh shell address space and entry into ring 3.

### Trailer change (BSS-via-trailer widening)

Old protocol (4 bytes):

```
dw bss_size            ; 16-bit, max 64 KB
dw 0xB055              ; magic
```

New protocol (6 bytes):

```
dd bss_size            ; 32-bit, max 4 GB
dw 0xB032              ; new magic ("BSS-32") — distinguishes from the old 16-bit format
```

Loader logic:

- Read the trailing 2 bytes. If they equal `0xB032`, BSS size is the preceding `dd`.
- If they equal the old `0xB055`, BSS size is the preceding `dw` (back-compat for any not-yet-rebuilt binary; the back-compat branch is deleted in the same milestone once every program in `src/c/` has been rebuilt with the new trailer).
- Otherwise treat as no BSS.

`edit.c` drops the hardcoded `EDIT_BUFFER_BASE` (`0x100000`) and `EDIT_KILL_BUFFER` (`0x200000`); the gap buffer and kill buffer become ordinary file-scope arrays that land in the program's BSS region.

### Kernel access to arbitrary user frames during program load

A frame freshly returned by `frame_alloc` may have a physical address anywhere in the 0..256 MB ceiling. The direct map at `0xC0000000..0xCFFFFFFF` covers the whole range, so kernel code can always reach the new frame at virtual `phys + 0xC0000000` without installing any per-call temporary mapping.

### Migration of low-memory kernel data structures

The kernel currently keeps several data structures at fixed low-physical addresses (a real-mode legacy from when the kernel ran in place at `0x7C00`):

- Kernel stack at physical `0x90000..0x9FFFF` (`KERNEL_STACK_TOP = 0x9FFF0`)
- Disk buffer (`SECTOR_BUFFER`) at `0xE000`
- Input buffer (`BUFFER`) at `0x500`
- NIC transmit / receive buffers at `0xE200` / `0xE800`
- `EXEC_ARG` pointer at a fixed low address

In the paged world these stay reachable via the direct map (their virtual addresses become `0xC0000000 + phys`), but they need explicit bitmap reservations and they sit in physical ranges that would otherwise be free user-page territory. The cleanest fix is to migrate every kernel data structure currently at a fixed low-memory address into the kernel binary's BSS, where they're automatically reserved as part of the kernel image at `0x100000+`. Concretely:

- Define `kernel_stack: times 16384 db 0; kernel_stack_top:` in `entry.asm`. `KERNEL_STACK_TOP` is updated to the new kernel-virt label.
- Move `SECTOR_BUFFER`, `BUFFER`, `NET_TRANSMIT_BUFFER`, `NET_RECEIVE_BUFFER`, `EXEC_ARG` storage into the appropriate driver / kernel `.asm` files as BSS labels.
- The constants in `src/include/constants.asm` go from physical-address numerics to label references resolved by the linker at kernel-virt addresses.

After migration, the bitmap reservation list is just: BIOS regions, kernel image, boot PD, first kernel PT — no more individual low-memory carve-outs.

## Exception and fault handling

The current `idt.asm` halts on every CPU exception. Paging makes `#PF` and `#GP` recoverable in many cases. New handlers in `arch/x86/exc.asm`; `exc_common` retained for unhandled cases.

### `#PF` handler dispatch (vector 14)

Error code bits (CPU-pushed): bit 0 = present-vs-not-present, bit 1 = read-vs-write, bit 2 = supervisor-vs-user, bit 3 = reserved-bit, bit 4 = instruction-fetch. CR2 = faulting linear address.

Tree:

1. Saved CS == kernel selector (`0x08`) → CPL=0 fault:
   - CR2 ≥ `0xC0000000` → kernel bug. Print `#PF KSV eip=... cr2=... err=...` on COM1, halt.
   - CR2 < `0xC0000000` → kernel was dereferencing a user pointer during a syscall. Print a brief diagnostic, restore `[shell_esp]`, fall into kill-program path.
2. Saved CS == user selector (`0x1B`) → CPL=3 fault:
   - For now: always kill the program. Print `#PF eip=... cr2=... err=...` on COM1, kill-program path.
   - When lazy-BSS lands later, this branch grows a "CR2 inside the program's declared BSS region?" check that allocates / maps / retries before falling through to the kill path.

Kill-program path:

1. `dead_pd = current CR3`.
2. `mov cr3, kernel_pd_template_phys`.
3. `address_space_destroy(dead_pd)`.
4. Restore `[shell_esp]`; `jmp shell_reload`.

The kill path shares its body with `sys_exit`'s tear-down.

### `#GP` handler (vector 13)

User in ring 3 hits a privileged instruction (`cli`, `in`, `out`, CR writes) → kernel triage same as `#PF` user-mode case: print `#GP eip=... err=...`, kill program. Existing `EXCnn` diagnostic is reused.

### Other exceptions

`#DE`, `#UD`, `#NM`, etc.: kernel-mode → halt with `EXCnn`; user-mode → kill program. `#DF` always halts.

### Kernel→user pointer validation

Syscall handlers that take user pointer + length validate before dereferencing:

- Pointer + length wholly below `0xC0000000`.
- No address-space wrap.

If validation fails, syscall returns CF (errno-equivalent). If validation passes but the pointer references an unmapped user page, the kernel takes a `#PF` at CPL=0 with CR2 < `0xC0000000` — caught by the dispatch tree above and routed to the kill path.

## Testing

### Regression coverage that must survive

- `tests/test_asm.py` (self-hosting assembler) — assembling each `static/*.asm` produces byte-identical output to NASM.
- `tests/test_bboefs.py` (filesystem regressions) — fs_copy, fs_mkdir, large-file handling. Both BBoeFS and ext2 image variants.
- Manual QEMU sweep of every program in `src/c/`: `cat`, `cp`, `edit`, `ls`, `mkdir`, `mv`, `rm`, `rmdir`, `netinit`, `netsend`, `netrecv`, `dns`, `ping`, `arp`, `date`, `uptime`, `chmod`. Checklist before merge.

### New tests

Three new C smoke tests in `src/c/`:

- `nullderef.c` — writes to `*(volatile int *)0`. Expected: `#PF eip=... cr2=00000000 err=...` on COM1, shell prompt returns. Verifies user-mode `#PF` recovery, NULL guard, and shell tear-down + reload through the kill path. Folds in a second sub-test that calls `io_read` with `buf = 0xC0000000` to exercise `access_ok`.
- `bigbss.c` — declares a 256 KB file-scope array, writes a known pattern, reads it back, prints OK or a diff. Verifies 32-bit `bss_size` trailer, eager BSS allocation, BSS zeroed at load, and user-half PT allocation across multiple PDEs.
- `stackbomb.c` — infinite recursion with a stack-allocated buffer. Expected: `#PF` with CR2 in the guard region, shell respawns. Verifies the stack guard.

A new `tests/test_paging.py` boots QEMU with `-serial fifo`, runs each test from the shell, matches expected serial output. Same harness shape as `test_asm.py`.

### Bootstrap manual checklist

- Boot under `qemu -m 16M`, `-m 32M`, `-m 128M`, `-m 256M`. E820 probe reports each correctly. Bitmap free-frame count matches expected RAM minus reservations.
- Boot with `-machine acpi=off` (existing shutdown failure-path test).
- Both `bboefs` and `ext2` images.

### Explicitly out of scope

Multi-process, fork, COW, demand-grown stack, lazy BSS, highmem (>256 MB RAM), `mmap`.

## Follow-up work

Tracked here so the design stays focused on this milestone:

- **Lazy / demand-zero BSS.** Mark BSS PTEs not-present at load; allocate + zero on first `#PF` per page. Plug into the `#PF` user-mode branch.
- **`SYS_MMAP` and runtime user allocation.** Required before user-side `malloc` / `free`.
- **`kmalloc` / kernel heap.** Lives in the reserved kernel-virt range above `0xCFFFFFFF`. Will need fan-out machinery the first time we add a kernel PDE that the boot template doesn't already cover.
- **Demand-grown user stack.** `VM_GROWSDOWN`-style: PTE not-present below current SP, `#PF` allocates and grows.
- **Multi-process / scheduler.** Per-address-space plumbing already in place; needs a runqueue, context switch, and CR3 swap on tick.
- **Highmem / >256 MB RAM.** Requires either a wider direct map (eats kernel-virt) or `kmap`-style temporary slots. Out of scope while QEMU defaults to 128 MB.
