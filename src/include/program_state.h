/* program_state.h — per-program kernel-side state.

   One instance per concurrently-alive program; two BSS slots in
   entry.asm (program_state_a, program_state_b) hold the structs.
   current_program_state points at the running program's slot.

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
    uint8_t fd_table[512];         // 0x008 .. 0x208 (FD_MAX × FD_ENTRY_SIZE)
    uint8_t in_signal_handler;     // 0x208
    uint8_t pad_after_handler[3];  // 0x209
    uint32_t pd_phys;              // 0x20C
    uint8_t pending_sigalrm;       // 0x210
    uint8_t pending_sigint;        // 0x211
    uint8_t pad_after_pending[2];  // 0x212
    uint32_t program_break;        // 0x214
    uint32_t program_break_min;    // 0x218
    uint32_t sigalrm_handler;      // 0x21C
    uint32_t sigint_handler;       // 0x220
};                                 // total 0x224 = 548 bytes

extern struct program_state *current_program_state;
