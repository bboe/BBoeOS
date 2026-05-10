---
title: Syscall interface (INT 30h)
nav_order: 80
---

# Syscall interface (`INT 30h`)

Programs loaded from the filesystem can use `INT 30h` for OS services. Syscall
numbers are defined symbolically as `SYS_*` constants in
[`src/include/constants.asm`](https://github.com/bboe/BBoeOS/blob/main/src/include/constants.asm)
— programs reference the names, not the numbers.

| AH    | Name         | Description                                          |
|-------|--------------|------------------------------------------------------|
| 00h   | fs_chmod     | Set file flags, SI = filename, AL = flags, CF on err  |
| 01h   | fs_mkdir     | Create subdirectory, SI = name, AX = start sector, CF on err |
| 02h   | fs_rename    | Rename or move file, SI = old name, DI = new name, CF on err |
| 03h   | fs_rmdir     | Remove an empty directory, SI = name, CF on err        |
| 04h   | fs_unlink    | Delete a file, SI = filename, CF on err               |
| 10h   | io_close     | Close fd, BX = fd, CF on error                        |
| 11h   | io_fstat     | Get file status, BX = fd; AL = mode, CX:DX = size     |
| 12h   | io_ioctl     | Device control, BX = fd, AL = cmd, args in other regs per (fd_type, cmd); CF on err |
| 13h   | io_open      | Open file, SI = filename, AL = flags, DL = mode; AX = fd, CF on err |
| 14h   | io_read      | Read from fd, BX = fd, DI = buf, CX = count; AX = bytes, CF on err |
| 15h   | io_seek      | Seek file fd, BX = fd, ECX = offset, AL = whence (SEEK_SET=0, SEEK_CUR=1, SEEK_END=2); EAX = new position (clamped to [0, size]), CF on err |
| 16h   | io_write     | Write to fd, BX = fd, SI = buf, CX = count; AX = bytes, CF on err |
| 20h   | net_mac      | Read cached MAC, DI = 6-byte buffer, CF if no NIC      |
| 21h   | net_open     | Open socket, AL = type (SOCK_RAW=0, SOCK_DGRAM=1), DL = protocol (IPPROTO_UDP=17, IPPROTO_ICMP=1; 0 for raw); AX = fd, CF if no NIC or table full |
| 22h   | net_recvfrom | Recv datagram via fd (UDP or ICMP): BX=fd, DI=buf, CX=len, DX=port (UDP) or ignored (ICMP); AX=bytes (0=none), CF err |
| 23h   | net_sendto   | Send datagram via fd: BX=fd, SI=buf, CX=len, DI=IP; UDP also uses DX=src port, BP=dst port (ignored for ICMP); AX=bytes, CF err |
| 30h   | rtc_alarm    | Arm/disarm interval timer. EBX = ms_until_first_fire (0 = cancel), ECX = ms_interval (0 = one-shot). EAX = ms remaining on prior alarm (0 if none). CF clear, no error path. Fires SIGALRM via SIGNAL_TAIL_CHECK. |
| 31h   | rtc_datetime | Get wall-clock time, EAX = unsigned seconds since 1970-01-01 UTC |
| 32h   | rtc_millis   | Get milliseconds since boot, EAX = ms (wraps at ~49.7 days)      |
| 33h   | rtc_sleep    | Busy-wait for ECX milliseconds; returns CF=1 + AL=ERROR_INTERRUPTED if a signal (SIGINT or SIGALRM) is pending |
| 34h   | rtc_uptime   | Get uptime in seconds, EAX = elapsed seconds (wraps at ~136 yr)  |
| 40h   | video_map    | Map mode-13h framebuffer into program PD; EAX = user-virt (0xB8000000) on success, EAX = 0 + CF on PT-allocation failure |
| F0h   | sys_break      | Set/query program break, EBX = new break (0 to query); EAX = resulting break |
| F1h   | sys_exec     | Execute program, SI = filename, CF on error            |
| F2h   | sys_exit     | Reload and return to shell                             |
| F3h   | sys_reboot   | Reboot                                                |
| F4h   | sys_shutdown | Shutdown                                              |
| F5h   | sys_signal   | Register signal handler. EBX = signum (SIGINT or SIGALRM), ECX = handler (SIG_DFL=0, SIG_IGN=1, or user-virt addr ≥ PROGRAM_BASE); EAX = previous handler. CF set + AL=ERROR_INVALID on bad signum/addr |
| F6h   | sys_sigreturn| Restore sigcontext from user stack at [user_esp + 4]; never returns through the regular path — resumes the saved EIP/EFLAGS/ESP/registers. Used only via the vDSO trampoline at the end of a signal handler |

## `/dev/midi` ioctls (FD_TYPE_MIDI = 6)

| AL  | Name              | Behavior                                                    |
|-----|-------------------|-------------------------------------------------------------|
| 00h | MIDI_IOCTL_DRAIN  | block via `sti`/`hlt` until the kernel ring drains (head == tail), AX = 0, CF clear |
| 01h | MIDI_IOCTL_FLUSH  | drop queued events, KEY_OFF all 18 voices, AX = 0, CF clear |
| 02h | MIDI_IOCTL_QUERY  | AX = `g_opl3_present` (0 or 1), CF clear                    |

Wire format on `/dev/midi` is 6-byte commands: `(delay_lo, delay_hi, bank, reg,
value, reserved)`.

## Error codes

When a syscall sets CF on return, AL holds one of these codes (symbolic names in
`src/include/constants.asm`):

| AL  | Name                  | Meaning                                                      |
|-----|-----------------------|--------------------------------------------------------------|
| 01h | ERROR_DIRECTORY_FULL  | No free directory entries (copy/create)                      |
| 02h | ERROR_EXISTS          | Destination name already exists (rename/copy)                |
| 03h | ERROR_FAULT           | Bad user pointer: out of user range, wraps, or filename has no NUL within MAX_PATH |
| 04h | ERROR_INTERRUPTED     | Cooperative-interrupt return (SIGINT or SIGALRM pending during blocking syscall) — maps to `EINTR` in libc |
| 05h | ERROR_INVALID         | Invalid argument (bad signum, out-of-range handler address, etc.) |
| 06h | ERROR_NOT_EMPTY       | Directory is not empty (rmdir)                               |
| 07h | ERROR_NOT_EXECUTE     | File exists but is not executable (exec)                     |
| 08h | ERROR_NOT_FOUND       | File not found                                               |
| 09h | ERROR_PROTECTED       | File is protected (rename/chmod)                             |
