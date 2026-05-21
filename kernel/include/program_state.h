/* program_state.h — per-program kernel-side state.

   One instance per concurrently-alive program; three BSS slots in
   entry.asm (program_state_a, program_state_b, program_state_c) hold
   the structs.  current_program_state points at the running program's
   slot.  Slot_a is the shell; slot_b/slot_c are the cooperatively-
   scheduled pipeline children.

   Field layout mirrors the PROGRAM_STATE_OFFSET_* constants in
   include/constants.asm; explicit pad fields keep the byte offsets
   stable without relying on cc.py auto-padding.

   The fd_table field is a raw 512-byte array because struct fd is
   defined locally in fs/fd.c and isn't visible here.  fs/fd.c's
   fd_table_base() casts the bytes to ``struct fd *``.
*/

#include "types.h"

struct program_state {
    u32 alarm_deadline;      // 0x000
    u32 alarm_interval;      // 0x004
    u32 current_pipe;        // 0x008  struct pipe* or NULL
    u8 fd_table[512];        // 0x00C .. 0x20C (FD_MAX × FD_ENTRY_SIZE)
    u8 in_signal_handler;    // 0x20C
    u8 pad_after_handler[3]; // 0x20D
    u32 kernel_stack_top;    // 0x210  per-slot kernel stack top (a/b/c)
    u32 pd_phys;             // 0x214
    u8 pending_sigalrm;      // 0x218
    u8 pending_sigint;       // 0x219
    u8 pending_sigpipe;      // 0x21A
    u8 pad_after_pending[1]; // 0x21B
    u32 program_break;       // 0x21C
    u32 program_break_min;   // 0x220
    u32 saved_esp;           // 0x224  parked kernel ESP while not current
    u32 sigalrm_handler;     // 0x228
    u32 sigint_handler;      // 0x22C
    u32 sigpipe_handler;     // 0x230
    u8 state;                // 0x234  STATE_*
    u8 pad_after_state[3];   // 0x235
    u32 wait_status;         // 0x238  parked exit code while STATE_EXITED
}; // total 0x23C = 572 bytes

extern struct program_state *current_program_state;
