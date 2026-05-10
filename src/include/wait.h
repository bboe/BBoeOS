/* wait.h — POSIX-shaped wait-status accessors.

   Wait-status word (16 bits, returned by exec()):
     bits 0..6  : signum (0 if exited, 0x7F = "killed by CPU exception")
     bit 7      : reserved (always 0; POSIX uses for "core dumped")
     bits 8..15 : exit code (only meaningful when bits 0..6 are 0)
*/

#ifndef WAIT_H
#define WAIT_H

#define WIFEXITED(status)   (((status) & 0x7F) == 0)
#define WIFSIGNALED(status) (((status) & 0x7F) != 0 && ((status) & 0x7F) != 0x7F)
#define WIFCRASHED(status)  (((status) & 0x7F) == 0x7F)
#define WEXITSTATUS(status) (((status) >> 8) & 0xFF)
#define WTERMSIG(status)    ((status) & 0x7F)

#endif
