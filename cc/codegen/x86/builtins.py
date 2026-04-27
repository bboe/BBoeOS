"""x86 builtin dispatchers.

One handler per C builtin name (``printf``, ``memcpy``, ``write``,
``far_write16``, …).  ``X86CodeGenerator.generate_call`` looks up
``getattr(self, f"builtin_{name}", None)`` to find them, so the names
and signatures are part of the public contract.

Each handler validates its argument count via
:meth:`cc.codegen.base.CodeGeneratorBase._check_argument_count`, routes
the operands into the registers the BBoeOS ABI or the builtin's
instruction shape requires, and emits the x86 sequence.  Clobbers are
declared up front in ``X86CodeGenerator.BUILTIN_CLOBBERS`` so
``generate_call`` knows which pinned registers to save around the
call site.
"""

from __future__ import annotations

from cc.ast_nodes import Int, Node, String, Var
from cc.errors import CompileError
from cc.utils import decode_first_character, decode_string_escapes, string_byte_length


class BuiltinsMixin:
    """x86 builtin dispatchers, mixed into :class:`X86CodeGenerator`.

    Relies on the mixing class to provide ``self.target``, ``self.emit``,
    ``self.ax_clear``, ``self.emit_register_from_argument``,
    ``self.emit_si_from_argument``, ``self._emit_syscall``,
    ``self.emit_error_syscall_tail``, ``self.new_string_label``,
    ``self.generate_expression``, ``self.NAMED_CONSTANTS``, and the
    memory/locals state initialized by ``CodeGeneratorBase.__init__``.
    """

    def builtin_asm(self, arguments: list[Node], /) -> None:
        r"""Emit an inline-asm string literal verbatim.

        Takes one string literal; C escape sequences (``\n``, ``\t``,
        ``\\``, ``\x??``) are decoded, and the result is split on
        newlines and emitted as individual lines so multi-instruction
        blocks can be written as ``asm("mov ax, 0\nmov es, ax");``.
        Pinned register values are conservatively assumed clobbered
        (see ``BUILTIN_CLOBBERS``); AX tracking is invalidated.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="asm")
        argument = arguments[0]
        if not isinstance(argument, String):
            message = "asm() argument must be a string literal"
            raise CompileError(message, line=argument.line)
        for line in decode_string_escapes(argument.content).splitlines():
            self.emit(line)
        self.ax_clear()

    def builtin_checksum(self, arguments: list[Node], /) -> None:
        """Generate code for the checksum(buf, len) builtin.

        Computes the 1's-complement 16-bit checksum used by IP and ICMP.
        ``len`` must be even; caller is responsible for zero-padding
        odd-length buffers.  Returns the folded, complemented checksum
        in AX, ready to store in the header field.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="checksum")
        buffer_argument, length_argument = arguments
        self.emit_si_from_argument(buffer_argument)
        self.emit_register_from_argument(argument=length_argument, register=self.target.count_register)
        label_index = self.new_label()
        self.emit("        cld")
        self.emit("        xor bx, bx")
        self.emit("        shr cx, 1")
        self.emit(f".ck_loop_{label_index}:")
        self.emit("        lodsw")
        self.emit("        add bx, ax")
        self.emit("        adc bx, 0")
        self.emit(f"        loop .ck_loop_{label_index}")
        self.emit("        not bx")
        self.emit("        mov ax, bx")
        self.ax_clear()

    def builtin_chmod(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the chmod() builtin.

        Returns 0 on success or an ERR_* code on failure.  When
        *fuse_exit* is True, emits ``jnc FUNCTION_EXIT`` instead of
        converting the carry flag to a 0-or-error integer.  When
        *fuse_die* is set, emits a direct ``jc FUNCTION_DIE`` with the
        given message preloaded in SI/CX.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="chmod")
        self.emit_si_from_argument(arguments[0])
        self.generate_expression(arguments[1])
        self._emit_syscall("FS_CHMOD")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=True)

    def builtin_close(self, arguments: list[Node], /) -> None:
        """Generate code for the close() builtin.

        Closes a file descriptor.  ``close(fd)`` emits
        ``mov bx, <fd> / mov ah, SYS_IO_CLOSE / int 30h``.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="close")
        self.emit_register_from_argument(argument=arguments[0], register=self.target.bx_register)
        self._emit_syscall("IO_CLOSE")

    def builtin_datetime(self, arguments: list[Node], /) -> None:
        """Generate code for the datetime() builtin.

        Returns unsigned seconds since 1970-01-01 UTC in DX:AX. Valid
        through the year 2106 (32-bit epoch overflow).
        """
        self._check_argument_count(arguments=arguments, expected=0, name="datetime")
        self._emit_syscall("RTC_DATETIME")

    def builtin_die(self, arguments: list[Node], /) -> None:
        """Generate code for the die() builtin.

        Pre-loads SI and CX (string + length) and jumps to a shared
        ``.die`` label that calls ``write_stdout`` then exits.
        """
        if self.target_mode == "kernel":
            message = "die() not available in --target kernel"
            raise CompileError(message)
        self._check_argument_count(arguments=arguments, expected=1, name="die")
        argument = arguments[0]
        if not isinstance(argument, String):
            message = "die() requires a string literal"
            raise CompileError(message, line=argument.line)
        label = self.new_string_label(argument.content)
        length = string_byte_length(argument.content)
        self.emit(f"        mov {self.target.si_register}, {label}")
        self.emit(f"        mov {self.target.count_register}, {length}")
        self.emit("        jmp FUNCTION_DIE")

    def builtin_exec(self, arguments: list[Node], /) -> None:
        """Generate code for the exec(name) builtin.

        Emits ``mov si, <name> / mov ah, SYS_EXEC / int 30h``.  On
        success, control is transferred to the loaded program and never
        returns here.  On failure (CF set), AL contains an ``ERROR_*``
        code; ``xor ah, ah`` zero-extends it for comparison against
        ``ERROR_NOT_EXECUTE`` etc.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="exec")
        self.emit_si_from_argument(arguments[0])
        self._emit_syscall("EXEC")
        self.emit_accumulator_zx_from_al()
        self.ax_clear()

    def builtin_exit(self, arguments: list[Node], /) -> None:
        """Generate code for the exit() builtin."""
        if self.target_mode == "kernel":
            message = "exit() not available in --target kernel"
            raise CompileError(message)
        self._check_argument_count(arguments=arguments, expected=0, name="exit")
        self.emit("        jmp FUNCTION_EXIT")

    def builtin_far_read16(self, arguments: list[Node], /) -> None:
        """Generate code for the ``far_read16(offset)`` builtin.

        Reads a 16-bit word from ``offset`` into the accumulator.
        Real mode: ``mov bx, <offset> / mov ax, [es:bx]`` — the ES
        prefix routes the access through the symbol-table segment.
        Pmode flat: ``mov ebx, <offset> / movzx eax, word [ebx]``;
        the ``movzx`` keeps the high half of EAX clean so the symbol
        table's per-entry 16-bit value sits in a value-comparable
        full-width int.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="far_read16")
        self.emit_register_from_argument(argument=arguments[0], register=self.target.bx_register)
        if self.target.int_size == 2:
            self.emit(f"        mov {self.target.acc}, {self.target.far_ref(self.target.bx_register)}")
        else:
            self.emit(f"        movzx {self.target.acc}, word {self.target.far_ref(self.target.bx_register)}")
        self.ax_clear()

    def builtin_far_read32(self, arguments: list[Node], /) -> None:
        """Generate code for the ``far_read32(offset)`` builtin.

        Reads a 32-bit dword from ``offset`` into the accumulator.
        Real mode: ``mov bx, <offset> / mov ax, [es:bx]`` (legacy
        16-bit fallback — only the low 16 bits land, sufficient for
        the asm.c self-host's symbol table when run under --bits 16).
        Pmode flat: ``mov ebx, <offset> / mov eax, [ebx]`` — full
        dword for symbol values that exceed 16 bits (``mov edi,
        JUMP_TABLE`` where JUMP_TABLE = SYMBOL_BASE + 0xF000 needs
        the upper bits intact).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="far_read32")
        self.emit_register_from_argument(argument=arguments[0], register=self.target.bx_register)
        self.emit(f"        mov {self.target.acc}, {self.target.far_ref(self.target.bx_register)}")
        self.ax_clear()

    def builtin_far_write32(self, arguments: list[Node], /) -> None:
        """Generate code for the ``far_write32(offset, value)`` builtin.

        Stores a 32-bit dword to ``offset``.  Pmode-only contract —
        the 16-bit codegen paths intentionally emit a word-width
        store so the on-disk image keeps the 36-byte symbol entry
        layout from the legacy 16-bit asm.  Asm.c gates use of this
        builtin to the pmode build.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="far_write32")
        offset_argument, value_argument = arguments
        if isinstance(value_argument, Int):
            self.emit_register_from_argument(argument=offset_argument, register=self.target.bx_register)
            if self.target.int_size == 2:
                self.emit(f"        mov word {self.target.far_ref(self.target.bx_register)}, {value_argument.value & 0xFFFF}")
            else:
                self.emit(f"        mov dword {self.target.far_ref(self.target.bx_register)}, {value_argument.value & 0xFFFFFFFF}")
        else:
            self.emit_register_from_argument(argument=value_argument, register=self.target.acc)
            self.emit(f"        push {self.target.acc}")
            self.emit_register_from_argument(argument=offset_argument, register=self.target.bx_register)
            self.emit(f"        pop {self.target.acc}")
            self.emit(f"        mov {self.target.far_ref(self.target.bx_register)}, {self.target.acc}")
        self.ax_clear()

    def builtin_far_read8(self, arguments: list[Node], /) -> None:
        """Generate code for the ``far_read8(offset)`` builtin.

        Reads a byte at ``ES:offset`` zero-extended into AX.  Emits
        ``mov bx, <offset> / mov al, [es:bx] / xor ah, ah`` in real
        mode; protected-mode retargeting would drop the ES prefix
        and leave the byte load unchanged.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="far_read8")
        self.emit_register_from_argument(argument=arguments[0], register=self.target.bx_register)
        self.emit(f"        mov al, {self.target.far_ref(self.target.bx_register)}")
        self.emit_accumulator_zx_from_al()
        self.ax_clear()

    def builtin_far_write16(self, arguments: list[Node], /) -> None:
        """Generate code for the ``far_write16(offset, value)`` builtin.

        Stores a 16-bit word to ``offset``.  Constant values compile
        to ``mov word [<offset>], <value>``; non-constant values
        route the low 16 bits of the accumulator (``ax``) through a
        push/pop guard around the offset eval.  In pmode the asm.c
        symbol table reserves only 2 bytes for the value field, so
        the store width must be word-sized regardless of int_size.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="far_write16")
        offset_argument, value_argument = arguments
        accumulator_word = self.target.low_word(self.target.acc)
        if isinstance(value_argument, Int):
            self.emit_register_from_argument(argument=offset_argument, register=self.target.bx_register)
            self.emit(f"        mov word {self.target.far_ref(self.target.bx_register)}, {value_argument.value & 0xFFFF}")
        else:
            self.emit_register_from_argument(argument=value_argument, register=self.target.acc)
            self.emit(f"        push {self.target.acc}")
            self.emit_register_from_argument(argument=offset_argument, register=self.target.bx_register)
            self.emit(f"        pop {self.target.acc}")
            self.emit(f"        mov {self.target.far_ref(self.target.bx_register)}, {accumulator_word}")
        self.ax_clear()

    def builtin_far_write8(self, arguments: list[Node], /) -> None:
        """Generate code for the ``far_write8(offset, value)`` builtin.

        Stores a byte to ``ES:offset``.  Shape mirrors
        :meth:`builtin_far_write16`: constant values compile to a
        single ``mov byte [es:bx], <value>`` store; non-constant
        values route through AX with a push/pop guard around the
        offset evaluation.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="far_write8")
        offset_argument, value_argument = arguments
        if isinstance(value_argument, Int):
            self.emit_register_from_argument(argument=offset_argument, register=self.target.bx_register)
            self.emit(f"        mov byte {self.target.far_ref(self.target.bx_register)}, {value_argument.value & 0xFF}")
        else:
            self.emit_register_from_argument(argument=value_argument, register=self.target.acc)
            self.emit(f"        push {self.target.acc}")
            self.emit_register_from_argument(argument=offset_argument, register=self.target.bx_register)
            self.emit(f"        pop {self.target.acc}")
            self.emit(f"        mov {self.target.far_ref(self.target.bx_register)}, al")
        self.ax_clear()

    def builtin_fill_block(self, arguments: list[Node], /) -> None:
        """Generate code for fill_block(fd, col, row, color).

        Thin wrapper over SYS_IO_IOCTL / VGA_IOCTL_FILL_BLOCK: programs
        first open("/dev/vga") to obtain the fd, then call fill_block to
        fill an 8x8 tile at (col, row) with the given palette index in
        mode 13h.  Emits BX=fd, CL=col, CH=row, DL=color, AL=cmd.
        """
        self._check_argument_count(arguments=arguments, expected=4, name="fill_block")
        fd_arg, col_arg, row_arg, color_arg = arguments
        self.emit_register_from_argument(argument=col_arg, register=self.target.acc)
        self.emit("        mov cl, al")
        self.emit_register_from_argument(argument=row_arg, register=self.target.acc)
        self.emit("        mov ch, al")
        self.emit_register_from_argument(argument=color_arg, register=self.target.acc)
        self.emit("        mov dl, al")
        self.emit_register_from_argument(argument=fd_arg, register=self.target.bx_register)
        self.emit("        mov al, VGA_IOCTL_FILL_BLOCK")
        self._emit_syscall("IO_IOCTL")
        self.ax_clear()

    def builtin_set_palette_color(self, arguments: list[Node], /) -> None:
        """Generate code for set_palette_color(fd, index, r, g, b).

        Thin wrapper over SYS_IO_IOCTL / VGA_IOCTL_SET_PALETTE.  Programs
        the VGA DAC entry ``index`` to 6-bit (r, g, b) via the kernel's
        vga_set_palette_color driver.  Emits BX=fd, CL=index, CH=r,
        DL=g, DH=b, AL=cmd.
        """
        self._check_argument_count(arguments=arguments, expected=5, name="set_palette_color")
        fd_arg, index_arg, r_arg, g_arg, b_arg = arguments
        self.emit_register_from_argument(argument=index_arg, register=self.target.acc)
        self.emit("        mov cl, al")
        self.emit_register_from_argument(argument=r_arg, register=self.target.acc)
        self.emit("        mov ch, al")
        self.emit_register_from_argument(argument=g_arg, register=self.target.acc)
        self.emit("        mov dl, al")
        self.emit_register_from_argument(argument=b_arg, register=self.target.acc)
        self.emit("        mov dh, al")
        self.emit_register_from_argument(argument=fd_arg, register=self.target.bx_register)
        self.emit("        mov al, VGA_IOCTL_SET_PALETTE")
        self._emit_syscall("IO_IOCTL")
        self.ax_clear()

    def builtin_fstat(self, arguments: list[Node], /) -> None:
        """Generate code for the fstat() builtin.

        ``fstat(fd)`` emits ``mov bx, <fd> / mov ah, SYS_IO_FSTAT /
        int 30h``.  Returns the file mode (flags byte) in AX.
        The syscall also returns CX:DX = file size, but those are
        discarded here.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="fstat")
        self.emit_register_from_argument(argument=arguments[0], register=self.target.bx_register)
        self._emit_syscall("IO_FSTAT")
        self.emit_accumulator_zx_from_al()
        self.ax_clear()

    def builtin_getchar(self, arguments: list[Node], /) -> None:
        """Generate code for the getchar() builtin.

        Reads a single byte from stdin (blocking) via
        FUNCTION_GET_CHARACTER.  Returns the byte zero-extended in AX.
        """
        self._check_argument_count(arguments=arguments, expected=0, name="getchar")
        self.emit("        call FUNCTION_GET_CHARACTER")
        self.emit_accumulator_zx_from_al()
        self.ax_clear()

    def builtin_inb(self, arguments: list[Node], /) -> None:
        """Generate code for inb(port). Reads one byte from an I/O port.

        Emits ``mov dx, <port>; in al, dx; xor ah, ah``.  Result is
        zero-extended in AX, so callers can write
        ``int status = inb(0x3FD);`` without further masking.
        Kernel-only — userspace ring 3 cannot execute IN/OUT under the
        planned CPL/IOPL configuration, so calls in ``--target user``
        are rejected at compile time.
        """
        if self.target_mode != "kernel":
            message = "inb() is kernel-only; not available in --target user"
            raise CompileError(message)
        self._check_argument_count(arguments=arguments, expected=1, name="inb")
        self.emit_register_from_argument(argument=arguments[0], register=self.target.dx_register)
        self.emit("        in al, dx")
        self.emit("        xor ah, ah")
        self.ax_clear()

    def builtin_inw(self, arguments: list[Node], /) -> None:
        """Generate code for inw(port). Reads one 16-bit word from an I/O port.

        Emits ``mov dx, <port>; in ax, dx``.  Kernel-only (see :meth:`builtin_inb`).
        """
        if self.target_mode != "kernel":
            message = "inw() is kernel-only; not available in --target user"
            raise CompileError(message)
        self._check_argument_count(arguments=arguments, expected=1, name="inw")
        self.emit_register_from_argument(argument=arguments[0], register=self.target.dx_register)
        self.emit("        in ax, dx")
        self.ax_clear()

    def builtin_mac(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the mac(buffer) builtin.

        Reads the cached NIC MAC address (6 bytes) into ``buffer``.
        Returns 0 on success, 1 if no NIC is present.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="mac")
        self.emit_register_from_argument(argument=arguments[0], register=self.target.di_register)
        self._emit_syscall("NET_MAC")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=False)

    def builtin_memcpy(self, arguments: list[Node], /) -> None:
        """Generate code for the memcpy(destination, source, n) builtin.

        Emits ``mov di, <destination> / mov si, <source> / mov cx, <n>
        / cld / rep movsb``.  Byte-wise copy; caller's DI, SI, CX are
        clobbered.
        """
        self._check_argument_count(arguments=arguments, expected=3, name="memcpy")
        destination_argument, source_argument, count_argument = arguments
        self.emit_register_from_argument(argument=destination_argument, register=self.target.di_register)
        self.emit_register_from_argument(argument=source_argument, register=self.target.si_register)
        self.emit_register_from_argument(argument=count_argument, register=self.target.count_register)
        self.emit("        cld")
        self.emit("        rep movsb")
        self.ax_clear()

    def builtin_mkdir(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the mkdir() builtin.

        Returns 0 on success or an ERR_* code on failure.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="mkdir")
        self.emit_si_from_argument(arguments[0])
        self._emit_syscall("FS_MKDIR")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=True)

    def builtin_net_open(self, arguments: list[Node], /) -> None:
        """Generate code for the net_open(type, protocol) builtin.

        ``net_open(type, protocol)`` emits ``mov al, <type> /
        mov dl, <protocol> / mov ah, SYS_NET_OPEN / int 30h`` where type
        is SOCK_RAW (0) or SOCK_DGRAM (1) and protocol is IPPROTO_UDP (17)
        or IPPROTO_ICMP (1) for datagram sockets (ignored for raw
        Ethernet sockets — pass 0).  Returns fd in AX on success, or -1
        if no NIC is present.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="net_open")
        type_argument, protocol_argument = arguments
        if isinstance(type_argument, Int) or (isinstance(type_argument, Var) and type_argument.name in self.NAMED_CONSTANTS):
            self.emit(f"        mov al, {type_argument.value if isinstance(type_argument, Int) else type_argument.name}")
        else:
            self.generate_expression(type_argument)
        self.emit_register_from_argument(argument=protocol_argument, register="dl")
        self._emit_syscall("NET_OPEN")
        label_index = self.new_label()
        self.emit(f"        jnc .ok_{label_index}")
        self.emit(f"        mov {self.target.acc}, -1")
        self.emit(f".ok_{label_index}:")
        self.ax_clear()

    def builtin_open(self, arguments: list[Node], /) -> None:
        """Generate code for the open() builtin.

        ``open(name, flags)`` or ``open(name, flags, mode)`` emits
        ``mov si, <name> / mov al, <flags> / [mov dl, <mode>] /
        mov ah, SYS_IO_OPEN / int 30h``.  The optional *mode*
        parameter sets the file permission flags (e.g. ``FLAG_EXECUTE``)
        when ``O_CREAT`` creates a new file.  Returns the fd number
        in AX, or -1 on error (CF set).
        """
        if len(arguments) < 2 or len(arguments) > 3:
            message = "open() expects 2 or 3 arguments"
            raise CompileError(message, line=arguments[0].line if arguments else None)
        name_argument = arguments[0]
        flags_argument = arguments[1]
        self.emit_si_from_argument(name_argument)
        if (flags_expr := self._constant_expression(flags_argument)) is not None:
            for name in self._collect_constant_references(flags_argument):
                self.emit_constant_reference(name)
            self.emit(f"        mov al, {flags_expr}")
        else:
            self.generate_expression(flags_argument)
        if len(arguments) == 3:
            self.emit_register_from_argument(argument=arguments[2], register="dl")
        self._emit_syscall("IO_OPEN")
        self.ax_clear()

    def builtin_outb(self, arguments: list[Node], /) -> None:
        """Generate code for outb(port, value). Writes one byte to an I/O port.

        Constant ``value`` compiles to ``mov dx, <port>; mov al, <value>;
        out dx, al``.  Non-constant ``value`` evaluates first into AX,
        is pushed across the port-evaluation, then popped — the same
        save-around-eval shape :meth:`builtin_far_write8` uses.
        Kernel-only (see :meth:`builtin_inb` for the rationale).
        """
        if self.target_mode != "kernel":
            message = "outb() is kernel-only; not available in --target user"
            raise CompileError(message)
        self._check_argument_count(arguments=arguments, expected=2, name="outb")
        port_arg, value_arg = arguments
        if isinstance(value_arg, Int):
            self.emit_register_from_argument(argument=port_arg, register=self.target.dx_register)
            self.emit(f"        mov al, {value_arg.value & 0xFF}")
        else:
            self.emit_register_from_argument(argument=value_arg, register=self.target.acc)
            self.emit(f"        push {self.target.acc}")
            self.emit_register_from_argument(argument=port_arg, register=self.target.dx_register)
            self.emit(f"        pop {self.target.acc}")
        self.emit("        out dx, al")
        self.ax_clear()

    def builtin_outw(self, arguments: list[Node], /) -> None:
        """Generate code for outw(port, value). Writes one 16-bit word to an I/O port.

        Constant ``value`` compiles to ``mov dx, <port>; mov ax, <value>;
        out dx, ax``.  Non-constant ``value`` uses the same push/pop guard
        as :meth:`builtin_outb`.  Kernel-only.
        """
        if self.target_mode != "kernel":
            message = "outw() is kernel-only; not available in --target user"
            raise CompileError(message)
        self._check_argument_count(arguments=arguments, expected=2, name="outw")
        port_arg, value_arg = arguments
        if isinstance(value_arg, Int):
            self.emit_register_from_argument(argument=port_arg, register=self.target.dx_register)
            self.emit(f"        mov {self.target.acc}, {value_arg.value & 0xFFFF}")
        else:
            self.emit_register_from_argument(argument=value_arg, register=self.target.acc)
            self.emit(f"        push {self.target.acc}")
            self.emit_register_from_argument(argument=port_arg, register=self.target.dx_register)
            self.emit(f"        pop {self.target.acc}")
        self.emit("        out dx, ax")
        self.ax_clear()

    def builtin_parse_ip(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the parse_ip(string, buffer) builtin.

        Parses a dotted-decimal IP string into a 4-byte buffer.
        Returns 0 on success, 1 on parse error.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="parse_ip")
        self.emit_si_from_argument(arguments[0])
        self.emit_register_from_argument(argument=arguments[1], register=self.target.di_register)
        self.emit("        call parse_ip")
        self.required_includes.add("parse_ip.asm")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=False)

    def builtin_print_datetime(self, arguments: list[Node], /) -> None:
        """Generate code for the print_datetime(unsigned long) builtin.

        Prints the epoch value as ``YYYY-MM-DD HH:MM:SS`` (no newline).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="print_datetime")
        self.generate_long_expression(arguments[0])
        self.emit("        call FUNCTION_PRINT_DATETIME")

    def builtin_print_ip(self, arguments: list[Node], /) -> None:
        """Generate code for the print_ip(buffer) builtin.

        Prints a 4-byte IP address as ``A.B.C.D`` (no newline).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="print_ip")
        self.emit_si_from_argument(arguments[0])
        self.emit("        call FUNCTION_PRINT_IP")

    def builtin_print_mac(self, arguments: list[Node], /) -> None:
        """Generate code for the print_mac(buffer) builtin.

        Prints a 6-byte MAC address as ``XX:XX:XX:XX:XX:XX`` (no newline).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="print_mac")
        self.emit_si_from_argument(arguments[0])
        self.emit("        call FUNCTION_PRINT_MAC")

    def builtin_printf(self, arguments: list[Node], /) -> None:
        """Generate code for the printf() builtin.

        First argument must be a string literal.  Remaining arguments
        are pushed right-to-left onto the stack, followed by the format
        string pointer.  Uses cdecl calling convention (caller cleans).

        Optimization: when the format string contains no ``%`` at all
        (no format specifiers, no ``%%`` escapes), emits a direct
        ``call FUNCTION_PRINT_STRING`` instead of the full printf
        machinery.
        """
        if not arguments or not isinstance(arguments[0], String):
            message = "printf() requires a string literal as the first argument"
            raise CompileError(message, line=arguments[0].line if arguments else None)
        fmt = arguments[0].content
        # Fast path: no '%' at all → emit print_string directly.
        if "%" not in fmt and len(arguments) == 1:
            label = self.new_string_label(fmt)
            self.emit(f"        mov {self.target.di_register}, {label}")
            self.emit("        call FUNCTION_PRINT_STRING")
            return
        # Count format specifiers (excluding %%) to validate argument count.
        expected_args = 0
        i = 0
        while i < len(fmt):
            if fmt[i] == "%" and i + 1 < len(fmt):
                if fmt[i + 1] != "%":
                    expected_args += 1
                i += 2
            else:
                i += 1
        if len(arguments) - 1 != expected_args:
            message = f"printf() format expects {expected_args} argument{'s' if expected_args != 1 else ''}, got {len(arguments) - 1}"
            raise CompileError(message, line=arguments[0].line)
        # Push arguments right-to-left.
        for arg in reversed(arguments[1:]):
            self.generate_expression(arg)
            self.emit(f"        push {self.target.acc}")
        # Push format string pointer.
        label = self.new_string_label(fmt)
        self.emit(f"        push {label}")
        self.emit("        call FUNCTION_PRINTF")
        stack_size = len(arguments) * self.target.int_size
        self.emit(f"        add {self.target.stack_register}, {stack_size}")

    def builtin_putchar(self, arguments: list[Node], /) -> None:
        """Generate code for the putchar() builtin."""
        self._check_argument_count(arguments=arguments, expected=1, name="putchar")
        argument = arguments[0]
        if isinstance(argument, String):
            byte_val = decode_first_character(argument.content)
            self.emit(f"        mov al, {byte_val}")
        elif isinstance(argument, Int):
            self.emit(f"        mov al, {argument.value}")
        else:
            self.generate_expression(argument)
        self.emit("        call FUNCTION_PRINT_CHARACTER")

    def builtin_read(self, arguments: list[Node], /) -> None:
        """Generate code for the read() builtin.

        ``read(fd, buffer, count)`` emits ``mov bx, <fd> /
        mov di, <buffer> / mov cx, <count> / mov ah, SYS_IO_READ /
        int 30h``.  Returns bytes read in AX (0 = EOF, -1 = error).
        """
        self._check_argument_count(arguments=arguments, expected=3, name="read")
        fd_argument, buffer_argument, count_argument = arguments
        self.emit_register_from_argument(argument=fd_argument, register=self.target.bx_register)
        self.emit_register_from_argument(argument=buffer_argument, register=self.target.di_register)
        self.emit_register_from_argument(argument=count_argument, register=self.target.count_register)
        self._emit_syscall("IO_READ")
        self.ax_clear()

    def builtin_reboot(self, arguments: list[Node], /) -> None:
        """Generate code for the reboot() builtin.

        Emits ``mov ah, SYS_REBOOT / int 30h``.  Does not return on
        success; the kernel triggers a warm reboot via the keyboard
        controller.
        """
        self._check_argument_count(arguments=arguments, expected=0, name="reboot")
        self._emit_syscall("REBOOT")

    def builtin_recvfrom(self, arguments: list[Node], /) -> None:
        """Generate code for the recvfrom() builtin.

        ``recvfrom(fd, buf, len, port)`` emits ``mov bx, <fd> /
        mov di, <buf> / mov cx, <len> / mov dx, <port> /
        mov ah, SYS_NET_RECVFROM / int 30h``.
        Returns bytes received in AX (0 if no matching packet).
        """
        self._check_argument_count(arguments=arguments, expected=4, name="recvfrom")
        fd_argument, buffer_argument, len_argument, port_argument = arguments
        self.emit_register_from_argument(argument=fd_argument, register=self.target.bx_register)
        self.emit_register_from_argument(argument=buffer_argument, register=self.target.di_register)
        self.emit_register_from_argument(argument=len_argument, register=self.target.count_register)
        self.emit_register_from_argument(argument=port_argument, register=self.target.dx_register)
        self._emit_syscall("NET_RECVFROM")
        self.ax_clear()

    def builtin_rename(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the rename() builtin.

        ``rename(oldname, newname)`` emits ``mov si, <oldname> /
        mov di, <newname> / mov ah, SYS_FS_RENAME / int 30h``.
        Returns 0 on success or an ERROR_* code on failure.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="rename")
        self.emit_si_from_argument(arguments[0])
        self.emit_register_from_argument(argument=arguments[1], register=self.target.di_register)
        self._emit_syscall("FS_RENAME")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=True)

    def builtin_rmdir(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the rmdir() builtin.

        ``rmdir(path)`` emits ``mov si, <path> / mov ah, SYS_FS_RMDIR /
        int 30h``.  Returns 0 on success or an ERROR_* code on failure.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="rmdir")
        self.emit_si_from_argument(arguments[0])
        self._emit_syscall("FS_RMDIR")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=True)

    def builtin_sendto(self, arguments: list[Node], /) -> None:
        """Generate code for the sendto() builtin.

        ``sendto(fd, buf, len, ip_ptr, src_port, dst_port)`` emits
        register setup and ``mov ah, SYS_NET_SENDTO / int 30h``.
        The 6th argument (dst_port) goes in BP (saved/restored).
        Returns bytes sent in AX, or -1 on error.
        """
        self._check_argument_count(arguments=arguments, expected=6, name="sendto")
        fd_argument, buf_argument, len_argument, ip_argument, sport_argument, dport_argument = arguments
        self.emit_register_from_argument(argument=fd_argument, register=self.target.bx_register)
        self.emit_si_from_argument(buf_argument)
        self.emit_register_from_argument(argument=len_argument, register=self.target.count_register)
        self.emit_register_from_argument(argument=ip_argument, register=self.target.di_register)
        self.emit_register_from_argument(argument=sport_argument, register=self.target.dx_register)
        self.emit(f"        push {self.target.base_register}")
        if isinstance(dport_argument, Int):
            self.emit(f"        mov {self.target.base_register}, {dport_argument.value}")
        elif isinstance(dport_argument, Var) and dport_argument.name in self.NAMED_CONSTANTS:
            self.emit(f"        mov {self.target.base_register}, {dport_argument.name}")
        elif isinstance(dport_argument, Var) and dport_argument.name in self.pinned_register:
            self.emit(f"        mov {self.target.base_register}, {self.pinned_register[dport_argument.name]}")
        elif (
            isinstance(dport_argument, Var)
            and self._is_memory_scalar(dport_argument.name)
            and not self._is_byte_scalar(dport_argument.name)
        ):
            self.emit(f"        mov {self.target.base_register}, [{self._local_address(dport_argument.name)}]")
        else:
            self.generate_expression(dport_argument)
            self.emit(f"        mov {self.target.base_register}, {self.target.acc}")
        self._emit_syscall("NET_SENDTO")
        self.emit(f"        pop {self.target.base_register}")
        # Normalize the CF error signal into AX = -1 so callers can
        # check the return value with ``< 0``.
        label_index = self.new_label()
        self.emit(f"        jnc .ok_{label_index}")
        self.emit(f"        mov {self.target.acc}, -1")
        self.emit(f".ok_{label_index}:")
        self.ax_clear()

    def builtin_set_exec_arg(self, arguments: list[Node], /) -> None:
        """Generate code for the set_exec_arg(arg) builtin.

        Writes the pointer *arg* to ``[EXEC_ARG]`` so that
        ``FUNCTION_PARSE_ARGV`` in the next exec()'d program can find
        it.  Pass NULL (0) to clear.  Used by the shell to forward
        command arguments into child programs.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="set_exec_arg")
        self.generate_expression(arguments[0])
        self.emit(f"        mov [EXEC_ARG], {self.target.acc}")

    def builtin_shutdown(self, arguments: list[Node], /) -> None:
        """Generate code for the shutdown() builtin.

        Emits ``mov ah, SYS_SHUTDOWN / int 30h``.  Does not return on
        success.  On APM failure the syscall returns, letting the caller
        print a diagnostic and continue.
        """
        self._check_argument_count(arguments=arguments, expected=0, name="shutdown")
        self._emit_syscall("SHUTDOWN")

    def builtin_sleep(self, arguments: list[Node], /) -> None:
        """Generate code for the sleep(milliseconds) builtin.

        ``sleep(ms)`` emits ``mov cx, <ms> / mov ah, SYS_RTC_SLEEP /
        int 30h``.  Busy-waits for the requested duration.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="sleep")
        self.emit_register_from_argument(argument=arguments[0], register=self.target.count_register)
        self._emit_syscall("RTC_SLEEP")

    def builtin_strlen(self, arguments: list[Node], /) -> None:
        """Generate code for the strlen() builtin.

        ``strlen(ptr)`` scans for a null terminator and returns the
        string length in AX.  Uses ``repne scasb`` (clobbers CX, DI).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="strlen")
        self.emit_register_from_argument(argument=arguments[0], register=self.target.di_register)
        self.emit("        xor al, al")
        self.emit(f"        mov {self.target.count_register}, 0FFFFh")
        self.emit("        cld")
        self.emit("        repne scasb")
        self.emit(f"        mov {self.target.acc}, 0FFFEh")
        self.emit(f"        sub {self.target.acc}, {self.target.count_register}")
        self.ax_clear()

    def builtin_unlink(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the unlink() builtin.

        ``unlink(path)`` emits ``mov si, <path> / mov ah, SYS_FS_UNLINK /
        int 30h``.  Returns 0 on success or an ERROR_* code on failure.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="unlink")
        self.emit_si_from_argument(arguments[0])
        self._emit_syscall("FS_UNLINK")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=True)

    def builtin_uptime(self, arguments: list[Node], /) -> None:
        """Generate code for the uptime() builtin."""
        self._check_argument_count(arguments=arguments, expected=0, name="uptime")
        self._emit_syscall("RTC_UPTIME")

    def builtin_uptime_ms(self, arguments: list[Node], /) -> None:
        """Generate code for the uptime_ms() builtin.

        Returns milliseconds since boot in DX:AX (via SYS_RTC_MILLIS).
        Callers assigning to ``unsigned long`` get the full 32-bit value
        (wraps at ~49.7 days); callers using ``int`` truncate to the
        low 16 bits (wraps at ~65.5 s, fine for short elapsed intervals
        such as ping round trips).
        """
        self._check_argument_count(arguments=arguments, expected=0, name="uptime_ms")
        self._emit_syscall("RTC_MILLIS")

    def builtin_video_mode(self, arguments: list[Node], /) -> None:
        """Generate code for the video_mode(fd, mode) builtin.

        Thin wrapper over SYS_IO_IOCTL / VGA_IOCTL_MODE.  Switches the
        VGA mode and clears the framebuffer + serial terminal.  Emits
        BX=fd, DL=mode, AL=cmd.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="video_mode")
        fd_arg, mode_arg = arguments
        self.emit_register_from_argument(argument=mode_arg, register=self.target.acc)
        self.emit("        mov dl, al")
        self.emit_register_from_argument(argument=fd_arg, register=self.target.bx_register)
        self.emit("        mov al, VGA_IOCTL_MODE")
        self._emit_syscall("IO_IOCTL")
        self.ax_clear()

    def builtin_write(self, arguments: list[Node], /) -> None:
        """Generate code for the write() builtin.

        ``write(fd, buffer, count)`` emits ``mov bx, <fd> /
        mov si, <buffer> / mov cx, <count> / mov ah, SYS_IO_WRITE /
        int 30h``.  Returns bytes written in AX (-1 on error).
        """
        self._check_argument_count(arguments=arguments, expected=3, name="write")
        fd_argument, buffer_argument, count_argument = arguments
        self.emit_register_from_argument(argument=buffer_argument, register=self.target.si_register)
        self.emit_register_from_argument(argument=count_argument, register=self.target.count_register)
        self.emit_register_from_argument(argument=fd_argument, register=self.target.bx_register)
        self._emit_syscall("IO_WRITE")
        self.ax_clear()
