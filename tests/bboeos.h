/**
 * BBoeOS builtin declarations and constants for host-side syntax checking.
 *
 * This header is NOT used by cc.py (which has its own builtins and pulls
 * constants from constants.asm).  It exists so that `clang -fsyntax-only
 * -include bboeos.h` can type-check the C sources on a standard compiler.
 */

#ifndef BBOEOS_H
#define BBOEOS_H

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* sys/stat.h declares fstat/mkdir with different signatures; redirect */
#define fstat(fd) bboeos_fstat(fd)
#define mkdir(name) bboeos_mkdir(name)

/* --- Constants (from src/include/constants.asm) --- */

#define BUFFER ((char *)0x500)
#define DIRECTORY_ENTRY_SIZE 32
#define DIRECTORY_OFFSET_FLAGS 25
#define ERROR_DIRECTORY_FULL 0x01
#define ERROR_EXISTS 0x02
#define ERROR_NOT_FOUND 0x04
#define ERROR_PROTECTED 0x05
#define FLAG_DIRECTORY 0x02
#define FLAG_EXECUTE 0x01
#define IPPROTO_ICMP 1
#define IPPROTO_UDP 17
#define SECTOR_BUFFER ((char *)0xE000)
#define SOCK_DGRAM 1
#define SOCK_RAW 0
#define STDERR STDERR_FILENO
#define STDIN STDIN_FILENO
#define STDOUT STDOUT_FILENO
#define VIDEO_MODE_CGA_320x200 0x04
#define VIDEO_MODE_CGA_640x200 0x06
#define VIDEO_MODE_EGA_320x200_16 0x0D
#define VIDEO_MODE_EGA_640x200_16 0x0E
#define VIDEO_MODE_EGA_640x350_16 0x10
#define VIDEO_MODE_TEXT_40x25 0x01
#define VIDEO_MODE_TEXT_80x25 0x03
#define VIDEO_MODE_VGA_320x200_256 0x13
#define VIDEO_MODE_VGA_640x480_16 0x12

/* --- BBoeOS-specific function declarations --- */

/* POSIX fstat takes struct stat*; BBoeOS returns just the mode byte */
int bboeos_fstat(int fd);
/* POSIX mkdir takes mode_t; BBoeOS takes only a name */
int bboeos_mkdir(const char *name);
/* BBoeOS syscall: seconds since 1970-01-01 UTC */
unsigned long datetime(void);
/* Print message and exit (no POSIX equivalent) */
void die(const char *message) __attribute__((noreturn));
/* Read NIC MAC address into buffer (no POSIX equivalent) */
int mac(char *buffer);
/* Open a socket: type is SOCK_RAW / SOCK_DGRAM, protocol is IPPROTO_UDP / IPPROTO_ICMP (0 for raw) */
int net_open(int type, int protocol);
/* Receive UDP datagram filtered by port (BBoeOS-specific) */
int recvfrom(int fd, char *buffer, int length, int port);
/* Send UDP datagram (BBoeOS-specific) */
int sendto(int fd, const char *buffer, int length, const char *ip, int src_port, int dst_port);
/* Parse dotted-decimal IP into 4-byte buffer (no POSIX equivalent) */
int parse_ip(const char *string, char *buffer);
/* Print epoch as YYYY-MM-DD HH:MM:SS (no POSIX equivalent) */
void print_datetime(unsigned long epoch);
/* Print 4-byte IP as A.B.C.D (no POSIX equivalent) */
void print_ip(const char *buffer);
/* Print 6-byte MAC as XX:XX:XX:XX:XX:XX (no POSIX equivalent) */
void print_mac(const char *buffer);
/* BBoeOS syscall: seconds since boot */
int uptime(void);
/* Switch video mode via INT 10h (no POSIX equivalent) */
void video_mode(int mode);

/* --- External data --- */

/* ARP frame template (included from arp_frame.asm by cc.py) */
extern char arp_frame[];

#endif
