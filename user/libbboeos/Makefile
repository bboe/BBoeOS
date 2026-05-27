# user/libbboeos/Makefile — builds libbboeos.a (full freestanding shim,
# used to populate the shared libbboeos blob) and libbboeos_stubs.o
# (per-export cdecl-to-regparm shim; linked into clang programs BEFORE
# libbboeos.a so the full bodies stay in the blob instead of being
# duplicated into every binary).
#
# All C sources are pre-compiled by cc.py in make_os.sh before this
# Makefile runs — the .o files are already up to date.  This Makefile
# only assembles the .asm files (via nasm -f elf32) and archives
# everything into libbboeos.a.

.DEFAULT_GOAL := all
.PHONY: all clean stubs_asm syscalls_h

all: libbboeos.a libbboeos_stubs.o

# libbboeos_stubs.asm is auto-generated from kernel/include/constants.asm
# by tools/generate_libbboeos_stubs.py and gitignored.  Idempotent —
# regenerate on every build so a renumbered FUNCTION_*_PTR propagates.
stubs_asm:
	@python3 ../../tools/generate_libbboeos_stubs.py

# include/syscalls.h is generated from kernel/include/constants.asm by
# tools/generate_syscalls_h.py and gitignored.  Run it unconditionally
# before any compile so a renumbered constant in the asm side propagates
# here.  The generator writes only on content change so this isn't a
# forced rebuild — make sees the .h timestamp move only when the
# contents actually changed.
syscalls_h:
	@python3 ../../tools/generate_syscalls_h.py

AR      ?= ar         # `?=` so build_doom.py / a CLI can override with x86_64-elf-ar / llvm-ar — Apple's BSD ar produces an archive index x86_64-elf-ld can't read
ASM_SRCS := _start.asm setjmp.asm
C_SRCS   := builtins.c ctype.c dirent.c errno.c math.c signal.c stdio.c stdlib.c string.c syscall.c
OBJS      = $(ASM_SRCS:.asm=.o) $(C_SRCS:.c=.o)

clean:
	rm -f $(OBJS) libbboeos.a libbboeos_stubs.o libbboeos_stubs.asm include/syscalls.h

libbboeos.a: $(OBJS)
	$(AR) rcs $@ $^

libbboeos_stubs.o: libbboeos_stubs.asm
	nasm -f elf32 -o $@ $<

libbboeos_stubs.asm: stubs_asm

%.o: %.asm
	nasm -f elf32 -o $@ $<
