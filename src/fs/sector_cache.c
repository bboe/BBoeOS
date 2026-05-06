// fs/sector_cache.c — 8-sector LRU cache between the FS layer and the
// fs/block.asm dispatcher.
//
// Catches the metadata sectors ext2 hits over and over (group
// descriptor, inode table, indirect blocks) on every file open and
// every multi-sector read.  bbfs touches fewer sectors but also
// benefits — its directory block is a hot read.  Net effect on Doom
// level loading: the second and subsequent reads of the same metadata
// sector serve from RAM instead of replaying the FDC / ATA syscall
// path, which on QEMU with a 1.44 MB floppy is the bulk of the wall-
// clock cost during P_SetupLevel.
//
// Cache lives in one 4 KB frame allocated at boot via
// sector_cache_init() — same frame_alloc + DIRECT_MAP_BASE adjust as
// vfs_init_scratch's sector_buffer.  Metadata sits in BSS: 8 entries
// of (sector, last_used, valid) at 12 bytes each = 96 bytes, plus a
// monotonic tick counter.  Both lookup and eviction are single-pass
// linear scans over 8 entries; LRU exploits the BSS-zero invariant
// (invalid entries have last_used = 0, which is older than any tick
// the cache has assigned, so a smallest-last_used scan picks invalid
// slots first without a special case).
//
// The dispatch labels in fs/block.asm are renamed to disk_read_sector
// / disk_write_sector so the public read_sector / write_sector names
// belong to this file.  Every existing FS caller (`call read_sector`
// in bbfs.asm and ext2.asm) routes through the cache without source
// changes.

extern uint8_t *sector_buffer;

#define SECTOR_CACHE_SIZE 8
#define SECTOR_BYTES      512

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

// disk_read_sector / disk_write_sector live in fs/block.asm.  Same
// AX = sector ABI as the public read_sector / write_sector below;
// CF reflects success.
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
int disk_write_sector(int sec __attribute__((in_register("ax"))));

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

// read_sector: cache-aware block read.  AX = sector number, on
// return sector_buffer holds the 512 bytes; CF set on disk error.
// On a hit, copies cache → sector_buffer (no disk syscall).  On a
// miss, calls disk_read_sector and inserts the result into the LRU
// slot (or whatever invalid entry we have).
__attribute__((carry_return))
__attribute__((preserve_register("eax")))
__attribute__((preserve_register("ebx")))
__attribute__((preserve_register("ecx")))
__attribute__((preserve_register("edx")))
__attribute__((preserve_register("esi")))
__attribute__((preserve_register("edi")))
int read_sector(int sec __attribute__((in_register("ax")))) {
    int i;
    int lru;
    int cached_valid;
    uint32_t cached_sector;
    uint32_t cached_last_used;
    uint32_t lru_tick;
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
    if (!disk_read_sector(sec)) {
        return 0;
    }
    lru = 0;
    lru_tick = sector_cache_metadata[0].last_used;
    i = 1;
    while (i < SECTOR_CACHE_SIZE) {
        cached_last_used = sector_cache_metadata[i].last_used;
        if (cached_last_used < lru_tick) {
            lru = i;
            lru_tick = cached_last_used;
        }
        i = i + 1;
    }
    memcpy(sector_cache_data + lru * SECTOR_BYTES,
           sector_buffer,
           SECTOR_BYTES);
    sector_cache_metadata[lru].sector = sec;
    sector_cache_metadata[lru].last_used = sector_cache_tick;
    sector_cache_metadata[lru].valid = 1;
    return 1;
}

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
