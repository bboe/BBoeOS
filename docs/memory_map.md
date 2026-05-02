---
title: Memory map
nav_order: 4
---

# Memory map

## Kernel-side static memory map

Kernel-side fixed-physical regions, all reached through the kernel direct map
at virt `DIRECT_MAP_BASE + phys` (= `0xFF800000 + phys` at the current base;
or via the kmap window for frames above the direct-map ceiling). The "in
kernel.bin?" column flags whether the bytes occupy the on-disk image (`yes`)
or live as bare frames reserved by `frame_reserve_range` (`no`). Addresses
from `kernel_stack` onward are derived from
`KERNEL_RESERVED_BASE = page_align(0x20000 + sizeof(kernel.bin))`; example
values shown are for the current build (~29 KB kernel).

Two narrow `frame_reserve_range` calls at boot pin only the regions the kernel
still owns: the vDSO target frame at `0x10000` (one 4 KB page) and
`0x20000..(FRAME_BITMAP_PHYS + frame_bitmap_bytes)` (kernel image +
KERNEL_RESERVED_BASE region; the bitmap end is runtime, sized by `frame_init`
from E820). Everything else in conventional low memory — IVT/BDA at
`0..0x4FF`, `0x600..0x7BFF` gap, MBR landing zone at `0x7C00..0x7DFF`, dead
post-MBR boot code at `0x7E00..0xDFFF`, the unused page-`0xE` region, and the
boot stack at `0x9F000` — stays in the bitmap allocator's free pool. The
build script asserts that `KERNEL_RESERVED_BASE + 0x23000 < 0xA0000`
(worst-case stack + boot PD + first kernel PT + 128 KB bitmap at the
FRAME_PHYSICAL_LIMIT cap) so the kernel-side regions never cross the VGA
aperture under any RAM size.

**Update this table when adding a new fixed-phys region** so newcomers can
find every slot in one place.

| Phys range | Kernel-virt | Size | Symbol / purpose | In kernel.bin? |
|---|---|---|---|---|
| `0x00010000..0x00010FFF` | n/a | 4 KB | vDSO (shared user-virt frame; per-program PDs alias it user-side) | no |
| `0x00020000..0x00020001` | `0xFF820000..0xFF820001` | 2 B | `jmp short high_entry` trampoline (offset 0 of kernel.bin) | yes |
| `0x00020002` | `0xFF820002` | 1 B | `boot_disk` (BIOS drive number, written by boot.asm post-load) | yes |
| `0x00020003..0x00020004` | `0xFF820003..0xFF820004` | 2 B | `directory_sector` (LBA of first directory sector) | yes |
| `0x00020008..` | `0xFF820008..` | ~29 KB | `kernel.bin` `high_entry` and resident kernel code | yes |
| `KERNEL_RESERVED_BASE` (~`0x28000..0x28FFF`) | `0xFF828000..` | 4 KB | `kernel_stack` (`KERNEL_RESERVED_BASE = page_align(0x20000 + kernel_size)`; poison-filled with `0xDEADBEEF` at boot for high-water tracking) | no |
| ~`0x29000..0x29FFF` | `0xFF829000..` | 4 KB | boot PD (`BOOT_PD_PHYS`); freed back to the bitmap pool by `high_entry` after `kernel_idle_pd` takes over the CR3-target role. The slot is then just a regular conventional frame — the bitmap allocator can hand it out for user pages. | no |
| ~`0x2A000..0x2AFFF` | `0xFF82A000..` | 4 KB | first kernel PT (`FIRST_KERNEL_PT_PHYS`) | no |
| ~`0x2B000..` | `0xFF82B000..` | runtime, ≤ 128 KB | `frame_bitmap` (size set by `frame_init` from the highest type=1 E820 base, clamped to FRAME_PHYSICAL_LIMIT ≈ 4 GB — `-m 1` pays ~20 bytes, `-m 1024` pays 32 KB, `-m 4096` pays 128 KB; `frame_init` fills the storage before any allocator call, so the bytes don't ride on disk inside `kernel.bin`) | no |
| `FRAME_BITMAP_PHYS + frame_bitmap_bytes` | `0xFF800000 +` same | -- | end of the kernel reserve sweep — runtime ceiling, equals `0x2B000 + 20 B` on `-m 1`, `0x2B000 + 32 KB` on `-m 1024`, and `0x2B000 + 128 KB` on `-m 4096`; everything past this in conventional RAM is owned by the bitmap allocator (subject to E820's reserved regions, including the VGA aperture at `0xA0000..0xFFFFF`) | -- |
| dynamic | dynamic | 4 KB | FS scratch frame — allocated by `vfs_init` on every boot (FS is always used); sliced into two named pointers (`sector_buffer` at offset 0, `ext2_sd_buffer` at offset 512 when ext2 is detected). 1.5 KB used inside the 4 KB frame on ext2; 512 B used on bbfs. bbfs systems leave `ext2_sd_buffer = 0` (no caller reaches the ext2_search_blk paths that read it). | no |
| dynamic | dynamic | 4 KB | NIC scratch frame — allocated by `network_initialize` only when an NE2000 NIC is detected; sliced into four named pointers (`net_receive_buffer` at offset 0, `net_transmit_buffer` at 1536, `arp_table` at 3072, `udp_buffer` at 3168), 3.4 KB used inside the 4 KB frame. Sessions without a NIC leave the four pointers at 0 and never spend the frame. The ARP-table slice is zero-filled at init (lookup/add keys on `[entry] == 0` for empty slots); the other slices are fully overwritten on each use | no |
| dynamic | dynamic | 4 KB | `kernel_idle_pd` — a kernel-only PD allocated by `high_entry` post-PT-alloc. Kernel-half (PDEs FIRST_KERNEL_PDE..1023) copy-imaged from the boot PD; user-half (PDEs 0..FIRST_KERNEL_PDE-1) zero. Used as the canonical kernel-half PDE source for `address_space_create`, as CR3 between programs, and as the CR3-swap target during `address_space_destroy`. `kernel_idle_pd_phys` (entry.asm BSS) holds its phys. Replaces the boot PD's permanent-frame role; the boot PD's frame is freed back to the bitmap pool once the idle PD takes over | no |
| dynamic | dynamic | 4 KB | kmap window PT (`kmap_pt_phys`) — allocated by `kmap_init` after the idle PD takes over. Installed at `kernel_idle_pd[1023]` so every per-program PD inherits the window through `address_space_create`'s kernel-half copy-image. Holds the PTEs for the `KMAP_SLOT_COUNT = 4` slots at virt `0xFFC00000..0xFFC03FFF`; `kmap_map`/`kmap_unmap` write and clear them on demand to alias frames above `FRAME_DIRECT_MAP_LIMIT` | no |

## User-side virtual layout

Per per-program PD; same shape for every program PD that
`address_space_create` builds.

| User-virt range | Size | Purpose |
|---|---|---|
| `0x00000000..0x00000FFF` | 4 KB | NULL guard — not mapped (PTE[0] absent so `*(int *)0` raises #PF) |
| `0x00001000..0x00001FFF` | 4 KB | shell↔program handoff frame at `USER_DATA_BASE` (ARGV at +0x4DE, EXEC_ARG at +0x4FC, BUFFER at +0x500) |
| `0x00010000..0x00010FFF` | 4 KB | vDSO (`FUNCTION_PRINT_STRING`, `FUNCTION_DIE`, …) |
| `0x08048000..` | program-sized | program text + BSS (Linux ELF-shaped load address) |
| `0xFF7E0000..0xFF7EFFFF` | 64 KB | unmapped (stack guard region) |
| `0xFF7F0000..0xFF7FFFFF` | 64 KB | user stack (16 pages, top at `USER_STACK_TOP`) |
| `0xFF800000` | -- | `USER_STACK_TOP` (one past end of stack; equals user/kernel boundary = `KERNEL_VIRT_BASE`) |
| `0xFF800000..0xFFBFFFFF` | 4 MB | kernel direct map (PDE 1022 = FIRST_KERNEL_PDE, copy-imaged from `kernel_idle_pd`) |
| `0xFFC00000..0xFFFFFFFF` | 4 MB | kmap window (PDE 1023, copy-imaged from `kernel_idle_pd`); only the first `KMAP_SLOT_COUNT = 4` PTEs are ever used at runtime |
