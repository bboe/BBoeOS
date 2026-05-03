# tools/libc/Makefile — builds libbboeos.a, the freestanding libc shim
# for clang-built bboeos userland programs.

.DEFAULT_GOAL := libbboeos.a
.PHONY: clean

AR      := ar         # GNU ar handles cross-target i386 objects fine; override with `make AR=llvm-ar` if preferred
ASFLAGS := --target=i386-pc-none-elf -m32 -ffreestanding -fno-pic
CC      := clang      # force clang — `?=` would lose to Make's built-in CC=cc default; command-line `make CC=...` still wins
CFLAGS  := --target=i386-pc-none-elf -m32 -ffreestanding -fno-pic \
           -fno-stack-protector -nostdlib -nostdinc -O2 -Wall -Wextra -Werror \
           -Iinclude
C_SRCS  := ctype.c errno.c math.c stdio.c stdlib.c string.c syscall.c
OBJS     = $(C_SRCS:.c=.o) $(S_SRCS:.S=.o)
S_SRCS  := _start.S setjmp.S

clean:
	rm -f $(OBJS) libbboeos.a

libbboeos.a: $(OBJS)
	$(AR) rcs $@ $^

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

%.o: %.S
	$(CC) $(ASFLAGS) -c $< -o $@
