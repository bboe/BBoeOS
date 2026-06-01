#ifndef BBOEOS_PORTS_KILO_TERMIOS_H
#define BBOEOS_PORTS_KILO_TERMIOS_H
/* Minimal termios shim for the kilo port.
 *
 * The bboeos fd-0 console is already a raw, un-echoed, single-byte
 * stream (the shell does its own line editing on top of it), so the
 * raw-mode dance kilo performs has nothing to actually toggle.  We
 * model termios as a plain flag bag and make tcgetattr / tcsetattr
 * no-op accessors that succeed, so enableRawMode / disableRawMode run
 * unchanged.  The flag and control-char constants below are only ever
 * masked into / out of those bytes, never interpreted, so any distinct
 * bit values work.
 */

typedef unsigned int tcflag_t;
typedef unsigned char cc_t;

#define NCCS 32

struct termios {
    tcflag_t c_iflag;
    tcflag_t c_oflag;
    tcflag_t c_cflag;
    tcflag_t c_lflag;
    cc_t c_cc[NCCS];
};

/* tcsetattr() optional-actions (ignored — we apply immediately). */
#define TCSANOW 0
#define TCSADRAIN 1
#define TCSAFLUSH 2

/* Input-mode flags (c_iflag). */
#define BRKINT 0x0001
#define ICRNL 0x0002
#define INPCK 0x0004
#define ISTRIP 0x0008
#define IXON 0x0010

/* Output-mode flags (c_oflag). */
#define OPOST 0x0001

/* Control-mode flags (c_cflag). */
#define CS8 0x0030

/* Local-mode flags (c_lflag). */
#define ECHO 0x0001
#define ICANON 0x0002
#define IEXTEN 0x0004
#define ISIG 0x0008

/* Indices into c_cc[]. */
#define VMIN 6
#define VTIME 5

int tcgetattr(int fd, struct termios *termios_p);
int tcsetattr(int fd, int optional_actions, const struct termios *termios_p);

#endif
