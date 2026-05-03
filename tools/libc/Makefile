# tools/libc/Makefile — builds libbboeos.a, the freestanding libc shim
# for clang-built bboeos userland programs.

.DEFAULT_GOAL := libbboeos.a
.PHONY: clean

AR      ?= ar         # `?=` so build_doom.py / a CLI can override with x86_64-elf-ar / llvm-ar — Apple's BSD ar produces an archive index x86_64-elf-ld can't read
# -march=i386 -mno-{mmx,sse,sse2} -mno-implicit-float: pin ISA to i386
# so clang doesn't emit SSE for memcpy/memset; the bboeos kernel hasn't
# toggled CR4.OSFXSR so any SSE instruction faults #UD at runtime.  Linux
# clang defaults to no SSE for our --target; Apple clang ignores -mno-sse2
# in some struct-init paths and emits `pxor xmm, xmm` to zero locals
# anyway, so we add -mno-implicit-float to forbid implicit FP/SIMD use
# entirely.  Explicit x87 in math.c via inline asm is unaffected.
ASFLAGS := --target=i386-pc-none-elf -m32 -march=i386 -ffreestanding -fno-pic
CC      := clang      # force clang — `?=` would lose to Make's built-in CC=cc default; command-line `make CC=...` still wins
CFLAGS  := --target=i386-pc-none-elf -m32 -march=i386 -mno-mmx -mno-sse -mno-sse2 \
           -mno-implicit-float -fno-vectorize -fno-slp-vectorize \
           -ffreestanding -fno-pic -fno-stack-protector \
           -nostdlib -nostdinc -O2 -Wall -Wextra -Werror -Iinclude
C_SRCS  := builtins.c ctype.c errno.c math.c stdio.c stdlib.c string.c syscall.c
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
