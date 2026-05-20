#ifndef BBOEOS_SIGNAL_H
#define BBOEOS_SIGNAL_H

typedef void (*sighandler_t)(int);
typedef volatile int sig_atomic_t;

#define SIG_DFL ((sighandler_t)0)
#define SIG_IGN ((sighandler_t)1)
#define SIG_ERR ((sighandler_t) - 1)

#define SIGALRM 14
#define SIGINT 2

sighandler_t signal(int signum, sighandler_t handler);

#endif
