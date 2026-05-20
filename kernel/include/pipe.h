/* pipe.h — kernel-side anonymous pipe object.

   A small fixed pool (MAX_PIPES) of struct pipe lives in BSS;
   sys_pipeline2 allocates one per pipeline, fd_close releases it.
   The 4076-byte ring buffer is sized so the entire struct is
   exactly one 4 KB frame, in case we later move pipes to the
   frame allocator.

   The struct itself is declared privately in pipe.c (same pattern
   as struct fd in fs/fd.c).  Other modules manipulate pipes through
   the function API below; asm callers use PIPE_OFFSET_* constants
   from include/constants.asm.
*/

#ifndef BBOEOS_PIPE_H
#define BBOEOS_PIPE_H

/* Linear-search allocator.  Returns a pool index (0..MAX_PIPES-1) on
   success or -1 on exhaustion.  The returned slot is fully zero-filled
   (all fields including blocked_reader/blocked_writer cleared), then
   in_use is set to 1. */
int pipe_alloc();

/* Resolve a pool index to a struct pipe pointer.  Returns 0 if the
   index is out of range.  The index argument is passed in EDX (cc.py
   auto-pin convention for single-parameter kernel functions that use
   the value as a pure scalar throughout their body). */
struct pipe *pipe_at(int index __attribute__((in_register("edx"))));

/* Returns 1 if both the reader and writer refcounts are zero (no open
   fd holds this end); the caller should then call pipe_release. */
int pipe_both_ends_closed(struct pipe *p);

/* Drain up to `want` bytes from the pipe's ring into `dst`.  Returns
   bytes actually transferred; may be 0 if the buffer is empty.
   Never blocks — the caller is responsible for empty handling.
   Calling convention: p on stack ([ebp+8]), dst in EBX, want in EDI. */
int pipe_buffer_read(struct pipe *p,
                     uint8_t *dst __attribute__((in_register("ebx"))),
                     int want __attribute__((in_register("edi"))));

/* Deposit up to `want` bytes from `src` into the pipe's ring.
   Returns bytes actually transferred; may be 0 if full.  Never blocks.
   Calling convention: p on stack ([ebp+8]), src in EBX, want in EDI. */
int pipe_buffer_write(struct pipe *p,
                      uint8_t *src __attribute__((in_register("ebx"))),
                      int want __attribute__((in_register("edi"))));

/* Decrement the reader or writer open-fd refcount (saturating at 0). */
void pipe_decrement_reader(struct pipe *p);
void pipe_decrement_writer(struct pipe *p);

/* pipe_reader_open / pipe_writer_open — read the per-end open
   refcount.  Returns 0 if the end is fully closed.  Used by
   fd_close_pipe to decide whether to wake the peer. */
int pipe_reader_open(struct pipe *p);

/* Mark the pool slot as free (in_use = 0). */
void pipe_release(struct pipe *p);

/* pipe_release_by_index — release a pipe by its pool index.  Used by
   sys_pipeline2's error-unwind paths to free the pipe pool slot when
   a pipeline build fails before either child has fully owned the
   pipe ends.  Out-of-range indices are silently ignored. */
void pipe_release_by_index(int index);

/* pipe_wake_reader / pipe_wake_writer — flip a blocked peer's state
   back to RUNNING so the scheduler resumes it on the next yield.
   No-op if no peer is parked. */
void pipe_wake_reader(struct pipe *p);
void pipe_wake_writer(struct pipe *p);

int pipe_writer_open(struct pipe *p);

/* Implemented in entry.asm; never returns to the caller (the
   scheduler resumes whichever slot it picks).  These are the cdecl
   wrappers around the asm `kernel_yield` routine — they set up the
   AL / EBX register convention that kernel_yield expects. */
void kernel_yield_read(struct pipe *p);
void kernel_yield_write(struct pipe *p);

#endif
