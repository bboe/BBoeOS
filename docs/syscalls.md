---
title: Syscall interface (INT 30h)
nav_order: 80
---

# Syscall interface (`INT 30h`)

Programs loaded from the filesystem can use `INT 30h` for OS services.
Syscall numbers are defined symbolically as `SYS_*` constants in
[`src/include/constants.asm`](https://github.com/bboe/BBoeOS/blob/main/src/include/constants.asm) — programs reference the names, not the numbers.

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
| 30h   | rtc_datetime | Get wall-clock time, EAX = unsigned seconds since 1970-01-01 UTC |
| 31h   | rtc_millis   | Get milliseconds since boot, EAX = ms (wraps at ~49.7 days)      |
| 32h   | rtc_sleep    | Busy-wait for ECX milliseconds                                   |
| 33h   | rtc_uptime   | Get uptime in seconds, EAX = elapsed seconds (wraps at ~136 yr)  |
| 40h   | video_map    | Map mode-13h framebuffer into program PD; EAX = user-virt; CF on OOM |
| F0h   | sys_break      | Set/query program break, EBX = new break (0 to query); EAX = resulting break |
| F1h   | sys_exec     | Execute program, SI = filename, CF on error            |
| F2h   | sys_exit     | Reload and return to shell                             |
| F3h   | sys_reboot   | Reboot                                                |
| F4h   | sys_shutdown | Shutdown                                              |
