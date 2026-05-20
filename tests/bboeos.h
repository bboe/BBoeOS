/**
 * BBoeOS builtin declarations and constants for host-side syntax checking.
 *
 * This header is NOT used by cc.py (which has its own builtins and pulls
 * constants from constants.asm).  It exists so that `clang -fsyntax-only
 * -include bboeos.h` can type-check the C sources on a standard compiler.
 */

#ifndef BBOEOS_H
#define BBOEOS_H

#include <ctype.h>
#include <fcntl.h>
#include <getopt.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* sys/stat.h declares fstat/mkdir with different signatures; redirect */
#define fstat(fd) bboeos_fstat(fd)
#define mkdir(name) bboeos_mkdir(name)

/* Suppress our header-only libc stubs in kernel/include/ when compiling
   under the host libc — the real <ctype.h>, <stdlib.h>, and <getopt.h>
   declarations above take precedence and our stubs would conflict. */
#define CTYPE_H
#define GETOPT_H
#define STRTOL_H

/* --- Constants (from kernel/include/constants.asm) --- */

#define ARG_MAX 256
#define DIRECTORY_ENTRY_SIZE 32
#define DIRECTORY_NAME_LENGTH 25
#define DIRECTORY_OFFSET_FLAGS 25
#define ERROR_DIRECTORY_FULL 0x01
#define ERROR_EXISTS 0x02
#define ERROR_FAULT 0x03
#define ERROR_NOT_EMPTY 0x06
#define ERROR_NOT_EXECUTE 0x07
#define ERROR_NOT_FOUND 0x08
#define ERROR_PROTECTED 0x09
#define FLAG_DIRECTORY 0x02
#define FLAG_EXECUTE 0x01
#define IPPROTO_ICMP 1
#define IPPROTO_UDP 17
#define MAX_ARGV_ENTRIES 64
#define MAX_INPUT 256
#define MAX_PATH 64
#define SECTOR_BUFFER ((char *)0xF000)
/* Host-side casts: cc.py accepts the bare integers, but clang's
   stricter type-checker rejects passing an int where the second
   signal() argument expects void (*)(int).  The cast mirrors POSIX's
   <signal.h> definition without changing the runtime value. */
#define SIG_DFL ((void (*)(int))0)
#define SIG_IGN ((void (*)(int))1)
#define SIGALRM 14
#define SIGINT 2
#define SIGPIPE 13
#define SO_RCVTIMEO 1
#define SOCK_DGRAM 1
#define SOCK_RAW 0
#define STDERR STDERR_FILENO
#define STDIN STDIN_FILENO
#define STDOUT STDOUT_FILENO
#define VIDEO_MODE_TEXT_80x25 0x03
#define VIDEO_MODE_VGA_320x200_256 0x13

/* --- BBoeOS-specific function declarations --- */

/* Arm/disarm the per-process interval timer.  delay_ms = 0 cancels;
   interval_ms = 0 means one-shot.  Returns ms remaining on prior alarm. */
unsigned int alarm_ms(unsigned int delay_ms, unsigned int interval_ms);
/* POSIX fstat takes struct stat*; BBoeOS returns just the mode byte */
int bboeos_fstat(int fd);
/* POSIX mkdir takes mode_t; BBoeOS takes only a name */
int bboeos_mkdir(const char *name);
/* 1's-complement checksum for IP / ICMP (BBoeOS-specific) */
int checksum(const char *buffer, int length);
/* BBoeOS syscall: seconds since 1970-01-01 UTC */
unsigned long datetime(void);
/* Print message and exit (no POSIX equivalent) */
void die(const char *message) __attribute__((noreturn));
/* Execute a filesystem program (Linux execv shape: argv is a
   NULL-terminated char** array).  Parent stays suspended; on the
   child's exit the shell receives the wait status (zero-extended 16-bit)
   in AX.  On failure returns a negative ERROR_* code. */
int exec(const char *name, char *const argv[]);
/* Far-memory accessors for the symbol-segment data in real-mode asm.c.
   Compile to ``[es:<offset>]`` memory accesses; will retarget to flat
   ``[<offset>]`` loads/stores when the OS ports to protected mode. */
int far_read16(int offset);
int far_read32(int offset);
int far_read8(int offset);
void far_write16(int offset, int value);
void far_write32(int offset, int value);
void far_write8(int offset, int value);
/* Fill an 8x8 pixel tile at (col, row) with palette index color in VGA mode 13h */
void fill_block(int fd, int col, int row, int color);
/* Linux-style getdents(2): read variable-length directory records from a
   directory fd into buffer.  Each record is uint32 d_ino, uint16 d_reclen,
   uint8 d_type, then a NUL-terminated name padded to 4-byte alignment.
   Returns the number of bytes written, 0 at end-of-directory, or -1 on
   error (e.g. fd is not a directory). */
int getdents(int fd, char *buffer, int count);
/* Read NIC MAC address into buffer (no POSIX equivalent) */
int mac(char *buffer);
/* Open a socket: type is SOCK_RAW / SOCK_DGRAM, protocol is IPPROTO_UDP / IPPROTO_ICMP (0 for raw) */
int net_open(int type, int protocol);
/* Parse dotted-decimal IP into 4-byte buffer (no POSIX equivalent) */
int parse_ip(const char *string, char *buffer);
/* Atomically spawn two children connected by a pipe: left_path's stdout
   feeds right_path's stdin.  Each side's argv is a NULL-terminated
   char** array (or NULL for no args).  The kernel validates each array
   under the shell's PD up front and stays on the shell's PD across
   both child builds; for each child, stage_user_argv re-walks the
   array under that PD and copies the strings directly into the child's
   stack page via a kmap alias, building the Linux SysV i386 startup
   frame in place with no intermediate kernel buffer.  Returns
   right_path's wait status on success or a negative ERROR_* code on
   error.  Caller must be the shell (slot_a). */
int pipeline2(const char *left_path, char *const left_argv[],
              const char *right_path, char *const right_argv[]);
/* Print epoch as YYYY-MM-DD HH:MM:SS (no POSIX equivalent) */
void print_datetime(unsigned long epoch);
/* Print 4-byte IP as A.B.C.D (no POSIX equivalent) */
void print_ip(const char *buffer);
/* Print 6-byte MAC as XX:XX:XX:XX:XX:XX (no POSIX equivalent) */
void print_mac(const char *buffer);
/* Warm-reboot the machine via the keyboard controller (no return) */
void reboot(void) __attribute__((noreturn));
/* Receive UDP datagram filtered by port (BBoeOS-specific) */
int recvfrom(int fd, char *buffer, int length, int port);
/* Reposition file fd's read cursor; whence is SEEK_SET / SEEK_CUR / SEEK_END.
   Returns the new absolute position (clamped to [0, file_size]) or -1 on
   error.  POSIX's lseek takes off_t; BBoeOS uses int and returns int. */
int seek(int fd, int offset, int whence);
/* Send UDP datagram (BBoeOS-specific) */
int sendto(int fd, const char *buffer, int length, const char *ip, int src_port,
           int dst_port);
/* Program VGA DAC register `index` to 6-bit RGB (r, g, b each 0..63) */
void set_palette_color(int fd, int index, int r, int g, int b);
/* Set a per-socket option.  Currently supports SO_RCVTIMEO (option_name=1,
   value=ms; 0 disables blocking).  Returns 0 on success, -1 on bad fd /
   wrong fd type / unknown option / negative value. */
int setsockopt(int fd, int option_name, int value);
/* Register handler for SIGINT, SIGPIPE, or SIGALRM. */
typedef void (*bboeos_sighandler_t)(int);
bboeos_sighandler_t signal(int signum, bboeos_sighandler_t handler);
/* Power off via APM. Returns only when APM is unavailable. */
void shutdown(void);
/* Busy-wait for N milliseconds. unistd.h's sleep collides (takes seconds);
   rely on cc.py's builtin for compilation, don't redeclare here. */
/* Linux-style brk(2): set/query the program break.  EBX = new break (0 =
   query); returns resulting break.  Used by user/programs/sort.c to acquire its
   line-buffer heap. */
void *sys_break(void *new_break);
/* BBoeOS syscall: seconds since boot */
int uptime(void);
/* Milliseconds since boot (low 16 bits; assign to unsigned long for the full DX:AX) */
int uptime_ms(void);
/* Switch video mode (no POSIX equivalent) */
void video_mode(int fd, int mode);

/* --- External data --- */

/* ARP frame template (included from arp_frame.asm by cc.py) */
extern char arp_frame[];

/* Assembler keyword strings (defined in user/programs/asm.c's trailing asm block).
   Exposed as NAMED_CONSTANTS in cc.py so match_word_c(STR_X) can pass
   the keyword address through AX without per-keyword wrappers. */
extern char STR_ALIGN[];
extern char STR_ASSIGN[];
extern char STR_BITS[];
extern char STR_BYTE[];
extern char STR_DB[];
extern char STR_DD[];
extern char STR_DEFINE[];
extern char STR_DW[];
extern char STR_DWORD[];
extern char STR_ENDMACRO[];
extern char STR_EQU[];
extern char STR_INCLUDE[];
extern char STR_MACRO[];
extern char STR_ORG[];
extern char STR_SHORT[];
extern char STR_TIMES[];
extern char STR_WORD[];

/* Register table (defined in user/programs/asm.c's trailing asm block): 4-byte
   packed entries of name[2] + reg + size, zero-terminated.  Exposed as
   a NAMED_CONSTANT so parse_register can walk it from pure C. */
extern char register_table[];

/* End-of-program sentinel label (emitted by cc.py at the binary tail).
   _bss_end follows immediately after any BSS variables; when there are none
   it equals _program_end.  Scratch buffers in asm.c's main() sit past
   _bss_end — LINE_BUFFER / OUTPUT_BUFFER / SOURCE_BUFFER expand to
   _bss_end + N. */
extern char _bss_end[];
extern char _program_end[];

#endif
