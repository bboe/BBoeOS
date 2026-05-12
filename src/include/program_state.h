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

struct program_state {
    uint32_t alarm_deadline;       // 0x000
    uint32_t alarm_interval;       // 0x004
    uint32_t current_pipe;         // 0x008  struct pipe* or NULL
    uint8_t fd_table[512];         // 0x00C .. 0x20C (FD_MAX × FD_ENTRY_SIZE)
    uint8_t in_signal_handler;     // 0x20C
    uint8_t pad_after_handler[3];  // 0x20D
    uint32_t kernel_stack_top;     // 0x210  per-slot kernel stack top (a/b/c)
    uint32_t pd_phys;              // 0x214
    uint8_t pending_sigalrm;       // 0x218
    uint8_t pending_sigint;        // 0x219
    uint8_t pad_after_pending[2];  // 0x21A
    uint32_t program_break;        // 0x21C
    uint32_t program_break_min;    // 0x220
    uint32_t saved_esp;            // 0x224  parked kernel ESP while not current
    uint32_t sigalrm_handler;      // 0x228
    uint32_t sigint_handler;       // 0x22C
    uint8_t state;                 // 0x230  STATE_*
    uint8_t pad_after_state[3];    // 0x231
    uint32_t wait_status;          // 0x234  parked exit code while STATE_EXITED
};                                 // total 0x238 = 568 bytes

extern struct program_state *current_program_state;
