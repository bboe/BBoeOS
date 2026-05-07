// fs/sector_cache.c — 8-slot sector cache with prefetch-on-miss.
//
// On a miss, read 8 sequential disk sectors in one driver round-trip
// via disk_read_sectors and stamp them into the cache as a sliding
// window.  Subsequent reads of sectors in that window serve from RAM
// without touching the disk — a Doom-style bulk read of N sequential
// sectors collapses from N driver round-trips to ⌈N/8⌉.
//
// Cache lives in one 4 KB frame allocated at boot via
// sector_cache_init() — same frame_alloc + DIRECT_MAP_BASE adjust as
// vfs_init_scratch's sector_buffer.  Metadata sits in BSS: 8 entries
// of (sector, last_used, valid) at 12 bytes each = 96 bytes, plus a
// monotonic tick counter.  Lookup is a single-pass linear scan over
// 8 entries.  Eviction during prefetch is "replace all" — the new
// 8-sector window simply overwrites every slot — which trades LRU
// retention against driver round-trips; for the workloads on this
// OS (sequential WAD reads dominating, with metadata clusters small
// and revisited rarely) the trade favours the prefetch.  A solitary
// metadata sector that needs caching gets pulled in alongside the
// 7 sectors around it; if those neighbours happen to be useful we
// win twice, otherwise we still satisfy the miss in one round-trip.
//
// The dispatch labels in fs/block.asm are renamed to disk_read_sector
// / disk_read_sectors / disk_write_sector so the public read_sector /
// write_sector names belong to this file.  Every existing FS caller
// (`call read_sector` in bbfs.asm and ext2.asm) routes through the
// cache without source changes.

extern uint8_t *sector_buffer;

#define PREFETCH_COUNT    8     // also = SECTOR_CACHE_SIZE: prefetch fills the cache
#define SECTOR_BYTES      512
#define SECTOR_CACHE_SIZE 8

struct sector_cache_entry {
    uint32_t sector;
    uint32_t last_used;
    uint8_t  valid;
    uint8_t  _pad0;
    uint8_t  _pad1;
    uint8_t  _pad2;
};

struct sector_cache_entry sector_cache_metadata[SECTOR_CACHE_SIZE];
asm("sector_cache_metadata equ _g_sector_cache_metadata");

uint8_t *sector_cache_data;
asm("sector_cache_data equ _g_sector_cache_data");

uint32_t sector_cache_tick;
asm("sector_cache_tick equ _g_sector_cache_tick");

// disk_read_sector / disk_read_sectors / disk_write_sector live in
// fs/block.asm and dispatch to fdc / ata.  Same AX = sector ABI as
// the public read_sector / write_sector below; CF reflects success.
// disk_read_sectors additionally takes CX = count and EDI = dest.
__attribute__((carry_return))
__attribute__((preserve_register("eax")))
__attribute__((preserve_register("ebx")))
__attribute__((preserve_register("ecx")))
__attribute__((preserve_register("edx")))
__attribute__((preserve_register("esi")))
__attribute__((preserve_register("edi")))
int disk_read_sector(int sec __attribute__((in_register("ax"))));
__attribute__((carry_return))
__attribute__((preserve_register("eax")))
__attribute__((preserve_register("ebx")))
__attribute__((preserve_register("ecx")))
__attribute__((preserve_register("edx")))
__attribute__((preserve_register("esi")))
__attribute__((preserve_register("edi")))
int disk_read_sectors(int sec __attribute__((in_register("ax"))),
                      int count __attribute__((in_register("cx"))),
                      uint8_t *dest __attribute__((in_register("edi"))));
__attribute__((carry_return))
__attribute__((preserve_register("eax")))
__attribute__((preserve_register("ebx")))
__attribute__((preserve_register("ecx")))
__attribute__((preserve_register("edx")))
__attribute__((preserve_register("esi")))
__attribute__((preserve_register("edi")))
int disk_write_sector(int sec __attribute__((in_register("ax"))));

// read_sector: cache-aware block read.  AX = sector number, on
// return sector_buffer holds the 512 bytes; CF set on disk error.
// On a hit, copies cache → sector_buffer (no disk syscall).  On a
// miss, fires one disk_read_sectors call for PREFETCH_COUNT sectors
// straight into the cache data frame and stamps every slot's
// metadata to the new (sec..sec+PREFETCH_COUNT-1) window.  The
// requested sector ends up in slot 0; the next 7 sequential reads
// hit the cache.
__attribute__((carry_return))
__attribute__((preserve_register("eax")))
__attribute__((preserve_register("ebx")))
__attribute__((preserve_register("ecx")))
__attribute__((preserve_register("edx")))
__attribute__((preserve_register("esi")))
__attribute__((preserve_register("edi")))
int read_sector(int sec __attribute__((in_register("ax")))) {
    int i;
    int cached_valid;
    uint32_t cached_sector;
    sector_cache_tick = sector_cache_tick + 1;
    i = 0;
    while (i < SECTOR_CACHE_SIZE) {
        cached_valid = sector_cache_metadata[i].valid;
        cached_sector = sector_cache_metadata[i].sector;
        if (cached_valid != 0 && cached_sector == sec) {
            memcpy(sector_buffer,
                   sector_cache_data + i * SECTOR_BYTES,
                   SECTOR_BYTES);
            sector_cache_metadata[i].last_used = sector_cache_tick;
            return 1;
        }
        i = i + 1;
    }
    if (!disk_read_sectors(sec, PREFETCH_COUNT, sector_cache_data)) {
        return 0;
    }
    i = 0;
    while (i < SECTOR_CACHE_SIZE) {
        sector_cache_metadata[i].sector = sec + i;
        sector_cache_metadata[i].last_used = sector_cache_tick;
        sector_cache_metadata[i].valid = 1;
        i = i + 1;
    }
    memcpy(sector_buffer, sector_cache_data, SECTOR_BYTES);
    return 1;
}

// sector_cache_init: allocate the 4 KB cache data frame and store
// its kernel-virt at sector_cache_data.  Hard-stop on OOM; the bitmap
// allocator must succeed at boot, same as vfs_init_scratch.
void sector_cache_init();
asm("sector_cache_init:\n"
    "        push eax\n"
    "        call frame_alloc\n"
    "        jc .sci_oom\n"
    "        add eax, DIRECT_MAP_BASE\n"
    "        mov [_g_sector_cache_data], eax\n"
    "        pop eax\n"
    "        ret\n"
    ".sci_oom:\n"
    "        hlt\n"
    "        jmp .sci_oom\n");

// write_sector: write through to disk and refresh the cache entry
// in place if we have one for this sector.  We deliberately don't
// insert on write — only sectors a previous read brought into the
// cache get refreshed; writing to a non-cached sector doesn't
// pollute the LRU.
__attribute__((carry_return))
__attribute__((preserve_register("eax")))
__attribute__((preserve_register("ebx")))
__attribute__((preserve_register("ecx")))
__attribute__((preserve_register("edx")))
__attribute__((preserve_register("esi")))
__attribute__((preserve_register("edi")))
int write_sector(int sec __attribute__((in_register("ax")))) {
    int i;
    int cached_valid;
    uint32_t cached_sector;
    if (!disk_write_sector(sec)) {
        return 0;
    }
    sector_cache_tick = sector_cache_tick + 1;
    i = 0;
    while (i < SECTOR_CACHE_SIZE) {
        cached_valid = sector_cache_metadata[i].valid;
        cached_sector = sector_cache_metadata[i].sector;
        if (cached_valid != 0 && cached_sector == sec) {
            memcpy(sector_cache_data + i * SECTOR_BYTES,
                   sector_buffer,
                   SECTOR_BYTES);
            sector_cache_metadata[i].last_used = sector_cache_tick;
            return 1;
        }
        i = i + 1;
    }
    return 1;
}
