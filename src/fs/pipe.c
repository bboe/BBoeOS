// fs/pipe.c — anonymous pipe pool + ring buffer.
#include "macros.h"
//
// One static pool of MAX_PIPES struct pipes lives in BSS.  Allocation
// is linear search keyed on the in_use flag.  Each pipe's ring buffer
// is a classic head/tail/count layout: head advances on read, tail
// on write, count distinguishes empty (== 0) from full
// (== PIPE_BUFFER_BYTES).
//
// asm callers reach the same struct via PIPE_OFFSET_* in
// include/constants.asm — fields are kept strictly in offset-order
// matching those constants.

struct pipe {
    uint32_t blocked_reader;             // 0x000  struct program_state*
    uint32_t blocked_writer;             // 0x004
    uint8_t buffer[4076];                // 0x008  size must match PIPE_BUFFER_BYTES in constants.asm
    uint16_t count;                      // 0xFF4
    uint16_t head;                       // 0xFF6
    uint8_t in_use;                      // 0xFF8
    uint8_t reader_fd_open;              // 0xFF9
    uint16_t tail;                       // 0xFFA
    uint8_t writer_fd_open;              // 0xFFC
    uint8_t pad_after_writer_fd_open[3]; // 0xFFD  align struct to PIPE_SIZE (one frame)
};                                       // total 0x1000

struct pipe pipe_pool[4];  // size must match MAX_PIPES in constants.asm

int pipe_alloc() {
    int i;
    int j;
    struct pipe *slot;
    uint8_t *bytes;
    i = 0;
    slot = pipe_pool;
    while (i < MAX_PIPES) {
        if (slot->in_use == 0) {
            bytes = (uint8_t *)slot;
            j = 0;
            // cc.py has no memset; zero the slot byte-by-byte. PIPE_SIZE (not
            // PIPE_BUFFER_BYTES) — clear all fields, not just the ring buffer.
            while (j < PIPE_SIZE) {
                bytes[j] = 0;
                j = j + 1;
            }
            slot->in_use = 1;
            return i;
        }
        slot = slot + 1;
        i = i + 1;
    }
    return -1;
}

struct pipe *pipe_at(int index) {
    if (index < 0) {
        return 0;
    }
    if (index >= MAX_PIPES) {
        return 0;
    }
    return &pipe_pool[index];
}

// Returns 1 if both refcounts are zero (caller should free the slot).
int pipe_both_ends_closed(struct pipe *p) {
    return p->reader_fd_open == 0 && p->writer_fd_open == 0;
}

int pipe_buffer_read(struct pipe *p, uint8_t *dst, int want) {
    int bytes_read;
    uint8_t *buf;
    buf = p->buffer;
    bytes_read = 0;
    while (bytes_read < want) {
        if (p->count == 0) {
            break;
        }
        dst[bytes_read] = buf[p->head];
        p->head = p->head + 1;
        if (p->head >= PIPE_BUFFER_BYTES) {
            p->head = 0;
        }
        p->count = p->count - 1;
        bytes_read = bytes_read + 1;
    }
    return bytes_read;
}

int pipe_buffer_write(struct pipe *p, uint8_t *src, int want) {
    int bytes_written;
    uint8_t *buf;
    buf = p->buffer;
    bytes_written = 0;
    while (bytes_written < want) {
        if (p->count >= PIPE_BUFFER_BYTES) {
            break;
        }
        buf[p->tail] = src[bytes_written];
        p->tail = p->tail + 1;
        if (p->tail >= PIPE_BUFFER_BYTES) {
            p->tail = 0;
        }
        p->count = p->count + 1;
        bytes_written = bytes_written + 1;
    }
    return bytes_written;
}

// Saturate at 0 so a double-close on the same end doesn't underflow.
void pipe_decrement_reader(struct pipe *p) {
    p->reader_fd_open = MAX(p->reader_fd_open - 1, 0);
}

// Saturate at 0 so a double-close on the same end doesn't underflow.
void pipe_decrement_writer(struct pipe *p) {
    p->writer_fd_open = MAX(p->writer_fd_open - 1, 0);
}

// Clearing in_use is sufficient — pipe_alloc zero-fills the slot
// on the next allocation, so no memset here.
void pipe_release(struct pipe *p) {
    p->in_use = 0;
}
