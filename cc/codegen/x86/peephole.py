"""x86 peephole optimization passes.

Runs over the emitted assembly buffer after ``generate()`` has walked
the AST / IR.  Each ``peephole_*`` pass scans for a specific
instruction-sequence pattern and rewrites it into a shorter / cheaper
equivalent; :meth:`Peepholer.run` orchestrates them.  All passes are
lexical — no AST or IR state — so they're safe to run in any order as
long as earlier passes' outputs don't silently invalidate later
passes' assumptions.

The patterns are x86-specific (``mov ax, imm / add ax, … / mov reg,
ax`` etc.), so this module lives in the x86 package rather than
:mod:`cc.codegen.base`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cc.codegen.x86.jumps import JUMP_INVERT

if TYPE_CHECKING:
    from cc.target import CodegenTarget


class Peepholer:
    """x86 peephole passes, run as a standalone post-processing stage.

    Owns a reference to the emitted-line buffer and the
    :class:`cc.target.CodegenTarget` describing the accumulator and
    register pool.  Instantiate, call :meth:`run`, and use the
    returned list as the new emit buffer.
    """

    def __init__(self, *, lines: list[str], target: CodegenTarget) -> None:
        """Capture the emit buffer and target descriptor.

        ``lines`` is consumed in place by the passes that mutate it and
        replaced by the ones that rebuild it; call :meth:`run` to
        obtain the final buffer regardless of which passes ran.
        """
        self.lines = lines
        self.target = target

    def _dedup_register_reloads(self, register: str, /) -> None:
        """Skip ``mov {register}, <source>`` when ``<source>`` already reached this register.

        The tracked source goes stale on anything that changes either
        the register itself (direct clobber) or the source register
        when ``<source>`` is register-sourced — e.g. ``mov si, ax / inc
        ax / mov si, ax`` is NOT a redundant reload because ``inc ax``
        makes the second ``mov si, ax`` store a different value.
        Memory / immediate sources stay stable until the destination
        register is clobbered.
        """
        value: str | None = None
        result: list[str] = []
        # Instructions that clobber the destination register directly.
        clobber_prefixes = (
            f"add {register}",
            "call ",
            "int ",
            "lodsb",
            "lodsw",
            "movsb",
            "movsw",
            f"pop {register}",
            "rep ",
            f"sub {register}",
            "xchg",
            f"xor {register}",
        )
        # Register-modifying mnemonics we care about as SOURCE clobbers.
        # ``mov <reg>, X`` is handled below alongside the other writers.
        source_clobber_operations = (
            "add ",
            "and ",
            "dec ",
            "div ",
            "idiv ",
            "imul ",
            "inc ",
            "mov ",
            "mul ",
            "neg ",
            "not ",
            "or ",
            "rcl ",
            "rcr ",
            "rol ",
            "ror ",
            "sal ",
            "sar ",
            "shl ",
            "shr ",
            "sub ",
            "xor ",
        )
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith(f"mov {register}, "):
                source = stripped[len(f"mov {register}, ") :]
                if source == value:
                    continue  # redundant — skip
                value = source
            elif stripped.endswith(":") or stripped.startswith(clobber_prefixes):
                value = None
            elif value is not None and "[" not in value:
                # Source is a register or immediate.  Check whether this
                # instruction writes to the source register, invalidating
                # the stored value.  e.g. ``mov si, ax / inc ax`` — the
                # tracked ``ax`` in SI no longer matches the current AX.
                for operation in source_clobber_operations:
                    if not stripped.startswith(operation):
                        continue
                    target = stripped[len(operation) :].split(",", 1)[0].strip()
                    if target == value or (len(target) == 2 and target[1] in "lh" and target[0] == value[0]):
                        value = None
                    break
            result.append(line)
        self.lines = result

    @staticmethod
    def _extract_local_label(line: str, /) -> str | None:
        """Return the _l_ label from a store or declaration, or None.

        Stops at the first non-identifier byte so a byte-offset store
        like ``mov [_l_sum+1], al`` still resolves to ``_l_sum`` — the
        same way peephole_dead_stores resolves reads.
        """
        # Store: mov [_l_NAME], ... or mov word [_l_NAME], ...
        if line.startswith("mov") and "[_l_" in line and "], " in line:
            start = line.index("[_l_") + 1
            end = start
            while end < len(line) and (line[end].isalnum() or line[end] == "_"):
                end += 1
            return line[start:end]
        # Declaration: _l_NAME: dw 0
        if line.startswith("_l_") and line.endswith(": dw 0"):
            return line[: line.index(":")]
        return None

    def _reads_acc(self, line: str, /) -> bool:
        """Return True if *line* reads AX / AL / AH (any width).

        Conservative: any appearance of the accumulator name in a
        non-destination position counts as a read.  ``mov ax, X``
        overwrites AX without reading it (destination); everything
        else (``cmp ax, X``, ``add ax, X``, ``mov X, ax``, etc.)
        reads.  ``mov al, X`` and ``mov ah, X`` only overwrite the
        named half — the OTHER half is preserved, which counts as
        an AX read for our purposes (the post-transform AX value
        differs from the pre-transform one).
        """
        acc = self.target.acc
        # Destination-only writes that fully overwrite AX.
        full_writes = (
            f"mov {acc}, ",
            f"xor {acc}, {acc}",
            f"pop {acc}",
            f"movzx {acc}, ",
        )
        if any(line.startswith(prefix) for prefix in full_writes):
            return False
        # Anywhere else: any mention of ax / al / ah is a read.
        return any(re.search(rf"\b{token}\b", line) for token in (acc, "al", "ah"))

    def peephole_compare_through_register(self) -> None:
        """Fold ``mov ax, <reg> / cmp ax, <X>`` into ``cmp <reg>, <X>``.

        When the cmp's left operand is already in a 16-bit register,
        the rebinding through AX is just to satisfy the existing
        ``emit_comparison`` template that always lands the left
        operand in AX.  ``cmp r16, r16`` and ``cmp r16, [mem]`` are
        the same length as the AX-flavored forms, so deleting the
        2-byte ``mov ax, <reg>`` is pure win.

        Only applied when the instruction after the cmp is a
        conditional jump — that's the only context where AX's value
        is provably dead after the cmp (the cmp itself doesn't write
        AX, but subsequent fall-through code might consume the
        rebinding).
        """
        registers = self.target.non_acc_registers
        jump_prefixes = (
            "ja ",
            "jae ",
            "jb ",
            "jbe ",
            "jc ",
            "je ",
            "jg ",
            "jge ",
            "jl ",
            "jle ",
            "jnc ",
            "jne ",
            "jno ",
            "jnp ",
            "jns ",
            "jnz ",
            "jo ",
            "jp ",
            "js ",
            "jz ",
        )
        mov_acc_prefix = f"mov {self.target.acc}, "
        cmp_acc_prefix = f"cmp {self.target.acc}, "
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            source = a[len(mov_acc_prefix) :]
            if source not in registers:
                i += 1
                continue
            if not b.startswith(cmp_acc_prefix):
                i += 1
                continue
            if not any(c.startswith(prefix) for prefix in jump_prefixes):
                i += 1
                continue
            rhs = b[len(cmp_acc_prefix) :]
            self.lines[i] = f"        cmp {source}, {rhs}"
            del self.lines[i + 1]

    def peephole_constant_to_register(self) -> None:
        """Fold ``mov ax, imm / mov <reg>, ax`` into a direct load.

        Replaces the two-instruction load with ``mov <reg>, imm`` or,
        when the constant is zero, ``xor <reg>, <reg>`` (one byte
        shorter).
        """
        registers = self.target.non_acc_registers
        mov_acc_prefix = f"mov {self.target.acc}, "
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            immediate = a[len(mov_acc_prefix) :]
            if immediate.startswith("[") or immediate in registers:
                i += 1
                continue
            if not b.startswith("mov "):
                i += 1
                continue
            parts = b[len("mov ") :].split(", ")
            if len(parts) != 2 or parts[1] != self.target.acc or parts[0] not in registers:
                i += 1
                continue
            register = parts[0]
            if immediate == "0":
                self.lines[i] = f"        xor {register}, {register}"
            else:
                self.lines[i] = f"        mov {register}, {immediate}"
            del self.lines[i + 1]
            continue

    def peephole_dead_ah(self) -> None:
        """Drop ``xor ah, ah`` when no intervening instruction reads AH.

        The zero-extension after ``mov al, [mem]`` is dead whenever the
        *first non-AX-preserving* instruction after it either
        overwrites AH (``mov ah, X``) or consumes only AL (``cmp al,
        imm``, ``test al, al``, ``mov [addr], al``, ``or al, al``).
        Byte-scalar global loads unconditionally emit the ``xor`` so
        the load is safe under later word-sized arithmetic; this
        peephole reclaims the two bytes on the common
        compare-and-branch path.

        Scans forward across AX-preserving instructions (register-to-
        register moves not touching AX, pushes/pops of non-AX regs,
        ``cmp`` / ``test`` on non-AX operands, ``clc`` / ``stc`` /
        ``cld``) so that patterns like ``xor ah, ah ; pop si ;
        test ax, ax`` fold the whole trio.  Stops at any control flow
        (``jmp`` / ``call`` / Jcc / ``ret`` / label), since the
        consumer might be reached along a different path where AH
        isn't zero.

        ``xor ah, ah`` itself sets flags (ZF=1, CF=0), but any consumer
        we elide against either sets its own flags (``cmp``, ``test``,
        ``or``) or doesn't use flags (``mov``), so dropping the xor
        never changes observable control-flow.
        """
        al_only_prefixes = (
            "mov [",  # mov [addr], al
            "cmp al,",
            "test al,",
            "or al,",
            "and al,",
            "xor al,",
            "add al,",
            "sub al,",
            "mov ah, ",
        )
        # AX-preserving skip list — instructions that don't touch AX
        # (including AH) and don't transfer control.  Any instruction
        # not recognized here aborts the scan conservatively.
        ax_preserving_pushpop = {
            f"{operation} {register}" for operation in ("push", "pop") for register in ("bx", "cx", "dx", "si", "di", "bp")
        }
        ax_preserving_prefixes = ("cmp ", "test ")  # cmp/test on non-AX also fine since they don't write AX
        ax_preserving_exact = {"clc", "stc", "cld"}

        def is_ax_preserving(stmt: str) -> bool:
            if stmt in ax_preserving_pushpop or stmt in ax_preserving_exact:
                return True
            # ``mov <non-AX reg>, ...`` preserves AX.
            match = re.match(r"mov\s+(bx|cx|dx|si|di|bp|bh|bl|ch|cl|dh|dl|sp|ss|es|ds|cs|fs|gs),", stmt)
            if match:
                return True
            # ``(add|sub|and|or|xor|inc|dec|shl|shr|neg|not) <non-AX reg>``.
            match = re.match(r"(add|sub|and|or|xor|inc|dec|shl|shr|neg|not)\s+(bx|cx|dx|si|di|bp|b[hl]|c[hl]|d[hl])", stmt)
            if match:
                return True
            # ``mov [mem], <non-AX>`` — a store that doesn't read AX.
            match = re.match(r"mov\s+\[[^\]]+\],\s*(bx|cx|dx|si|di|bp|\d+|0x[0-9a-fA-F]+)", stmt)
            if match:
                return True
            # ``(inc|dec|add|sub|and|or|xor) word|byte [mem]`` — memory
            # arithmetic not involving AX.
            match = re.match(r"(add|sub|and|or|xor|inc|dec)\s+(word|byte)\s+\[", stmt)
            if match:
                return True
            if any(stmt.startswith(prefix) for prefix in ax_preserving_prefixes):
                # ``cmp al, X`` / ``test al, X`` would itself be the
                # AL-only consumer we're looking for, not a skip.  Also
                # ``cmp ax, X`` / ``test ax, X`` read AH, so the scan
                # aborts conservatively in both cases.
                return not stmt.startswith(("cmp al,", "test al,", "cmp ax", "test ax"))
            return False

        i = 0
        while i < len(self.lines) - 1:
            if self.lines[i].strip() != "xor ah, ah":
                i += 1
                continue
            # Scan forward past AX-preserving instructions to the first
            # real consumer.
            j = i + 1
            while j < len(self.lines) and is_ax_preserving(self.lines[j].strip()):
                j += 1
            if j >= len(self.lines):
                i += 1
                continue
            b = self.lines[j].strip()
            # Word operation on AX that only inspects AL because AH is known
            # zero — rewrite to the byte form so the xor becomes dead.
            # ``test ax, ax`` → ``test al, al`` and ``cmp ax, K`` →
            # ``cmp al, K`` when K fits in a byte.  Byte form is 1 byte
            # shorter; the dropped xor reclaims another 2 bytes per
            # site.
            if b == "test ax, ax":
                self.lines[j] = self.lines[j].replace("test ax, ax", "test al, al")
                b = "test al, al"
            elif b.startswith("cmp ax, "):
                operand = b[len("cmp ax, ") :]
                try:
                    value = int(operand, 0)
                except ValueError:
                    value = None
                if value is not None and 0 <= value <= 255:
                    self.lines[j] = self.lines[j].replace("cmp ax, ", "cmp al, ", 1)
                    b = f"cmp al, {operand}"
            if b.startswith(al_only_prefixes):
                # For ``mov [addr], al`` verify the source operand is
                # actually ``al`` (not ``ax``) — the prefix match would
                # otherwise catch word stores.
                if b.startswith("mov [") and not b.endswith(", al"):
                    i += 1
                    continue
                del self.lines[i]
                continue
            i += 1

    def peephole_dead_code(self) -> None:
        """Remove unreachable instructions after unconditional jumps."""
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a.startswith("jmp ") and not b.endswith(":") and ":" not in b:
                del self.lines[i + 1]
                continue
            i += 1

    def peephole_dead_stores(self) -> None:
        """Remove stores to local variables that are never loaded."""
        # Collect all _l_ labels referenced anywhere except as a store
        # destination.  Stores are "mov ... [_l_X], <source>"; reads include
        # "mov <dst>, [_l_X]", "cmp word [_l_X], ...", etc.
        loaded: set[str] = set()
        for line in self.lines:
            stripped = line.strip()
            if self._extract_local_label(stripped) is not None:
                continue
            cursor = 0
            while True:
                start = stripped.find("[_l_", cursor)
                if start < 0:
                    break
                # Extract the bare label — stop at the first non-identifier
                # byte. `[_l_sum+1]` must count as a reference to `_l_sum`,
                # not `_l_sum+1`.
                label_end = start + 1
                while label_end < len(stripped) and (stripped[label_end].isalnum() or stripped[label_end] == "_"):
                    label_end += 1
                loaded.add(stripped[start + 1 : label_end])
                cursor = stripped.index("]", label_end) + 1
        # Remove stores and declarations for labels never loaded.
        result: list[str] = []
        for line in self.lines:
            stripped = line.strip()
            label = self._extract_local_label(stripped)
            if label is not None and label not in loaded:
                continue
            result.append(line)
        self.lines = result

    def peephole_dead_temp_slots(self) -> None:
        """Drop stores to bp-relative temp slots that are never read.

        Compiler-generated IR temps (``_ir_*``) get a stack slot at
        function entry and are typically written once via ``mov [bp-N],
        reg`` then consumed directly from the register without ever
        re-reading the slot.  :meth:`peephole_store_reload` deletes the
        reload when one is emitted, but the write itself remains; this
        pass deletes writes whose slot is never read at all within the
        function.

        Only negative-offset slots qualify (``[bp-N]``).  Positive
        offsets (``[bp+N]``) reference caller-pushed parameters, which
        a callee mustn't reorder.

        Read detection covers compound forms (``[bp-N+1]``, ``[bp-N+si]``,
        ``[bp-N-2]``) as well as the bare ``[bp-N]`` — partial-byte
        reads of a word slot land at ``[bp-N+1]`` and would be missed
        by a bare-form regex, so any ``[bp-N...]`` operand counts as
        a read of slot N.
        """
        base_register = self.target.base_register
        slot_pattern = re.compile(rf"\[{base_register}-(\d+)(?:[+\-][^\]]+)?\]")
        store_pattern = re.compile(rf"^mov \[{base_register}-(\d+)\],")
        # Collect every slot that's READ anywhere.  For ``mov [bp-N], <src>``
        # the destination ``[bp-N]`` is a write — skip past the closing
        # ``]`` before scanning the rest for reads.  Other instructions
        # treat any ``[bp-N...]`` reference as a read.
        read_slots: set[int] = set()
        for line in self.lines:
            stripped = line.strip()
            scan_start = 0
            if store_pattern.match(stripped):
                scan_start = stripped.index("]") + 1
            read_slots.update(int(match.group(1)) for match in slot_pattern.finditer(stripped, scan_start))
        # Drop writes whose slot is never read.
        result: list[str] = []
        for line in self.lines:
            stripped = line.strip()
            store_match = store_pattern.match(stripped)
            if store_match is not None and int(store_match.group(1)) not in read_slots:
                continue
            result.append(line)
        self.lines = result

    def peephole_dead_test_after_sbb(self) -> None:
        """Drop ``test ax, ax`` immediately after ``sbb ax, ax``.

        The sbb produces 0 (CF clear) or -1 (CF set) and already
        sets ZF correctly, so the compiler's follow-up test is dead.
        """
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a == f"sbb {self.target.acc}, {self.target.acc}" and b == f"test {self.target.acc}, {self.target.acc}":
                del self.lines[i + 1]
                continue
            i += 1

    def peephole_double_jump(self) -> None:
        """Collapse conditional-jump-over-unconditional-jump sequences.

        Replaces ``jCC .L1 / jmp .L2 / .L1:`` with ``jCC_inv .L2``.
        The ``.L1:`` label is kept when other jumps still target it;
        deleting it would leave those references dangling.
        """
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            # Match: jCC .label1 / jmp .label2 / .label1:
            parts = a.split()
            if len(parts) == 2 and parts[0] in JUMP_INVERT and b.startswith("jmp ") and c == f"{parts[1]}:":
                target = b.split()[1]
                label = parts[1]
                self.lines[i] = f"        {JUMP_INVERT[parts[0]]} {target}"
                label_referenced_elsewhere = any(
                    j != i and j != i + 1 and j != i + 2 and (tokens := self.lines[j].split()) and len(tokens) >= 2 and tokens[-1] == label
                    for j in range(len(self.lines))
                )
                if label_referenced_elsewhere:
                    del self.lines[i + 1]
                else:
                    del self.lines[i + 1 : i + 3]
                continue
            i += 1

    def peephole_dx_to_memory(self) -> None:
        """Fold ``mov ax, dx / mov [X], ax`` into ``mov [X], dx``.

        The pair arises after a ``%`` expression whose remainder the
        ``%`` handler stages into AX just so the standard store path
        can flush it to the local — but the intermediate AX hop is
        dead if the next instruction writes that memory anyway.
        """
        acc = self.target.acc
        dx = self.target.dx_register
        acc_from_dx = f"mov {acc}, {dx}"
        comma_acc_suffix = f", {acc}"  # match ``", ax"`` / ``", eax"`` tails and drop the space
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a == acc_from_dx and b.startswith("mov [") and b.endswith(comma_acc_suffix):
                self.lines[i + 1] = f"{self.lines[i + 1][: -len(comma_acc_suffix)]},{dx}"
                del self.lines[i]
                continue
            i += 1

    def peephole_fold_zero_save(self) -> None:
        """Fuse ``xor reg, reg / push reg`` into ``push 0``.

        When ``cursor_column = 0`` is immediately followed by code that
        clobbers the pinned CX as scratch (and therefore needs to
        push/pop it), the compiler emits ``xor cx, cx / push cx``
        followed later by ``pop cx`` to restore zero.  The two-byte
        ``xor cx, cx`` plus one-byte ``push cx`` (3 bytes) collapses
        to a single two-byte ``push 0`` (``6A 00``) — the body and the
        eventual ``pop cx`` are unchanged, since the popped value is
        still zero.

        The xor's flag-side-effects are dead in every emission path
        cc.py produces here: ``push cx`` doesn't read flags and the
        intervening body overwrites them before the next conditional
        jump.
        """
        registers = {"ax", "bx", "cx", "dx", "si", "di", "bp"}
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a.startswith("xor ") and " " in a[4:]:
                parts = a[4:].split(", ")
                if len(parts) == 2 and parts[0] == parts[1] and parts[0] in registers and b == f"push {parts[0]}":
                    self.lines[i] = "        push 0"
                    del self.lines[i + 1]
                    continue
            i += 1

    def peephole_index_through_memory(self) -> None:
        """Use ``add si, [mem]`` instead of staging through AX.

        Recognizes::

            push ax
            mov si, [BASE]
            mov ax, [INDEX]
            add si, ax
            pop ax

        and rewrites it as::

            mov si, [BASE]
            add si, [INDEX]

        Safe because the eight-byte form ``add si, [mem]`` is a single
        8086 instruction and AX is never disturbed.  Saves the
        push/pop AX pair (2 bytes) and the redundant ``mov ax, [mem]``
        (3 bytes) for a net 3-byte gain (the new ``add si, [mem]`` is
        2 bytes longer than the old ``add si, ax``).
        """
        acc = self.target.acc
        si = self.target.si_register
        push_acc = f"push {acc}"
        pop_acc = f"pop {acc}"
        mov_si_mem_prefix = f"mov {si}, ["
        mov_acc_mem_prefix = f"mov {acc}, ["
        add_si_acc = f"add {si}, {acc}"
        i = 0
        while i < len(self.lines) - 4:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            d = self.lines[i + 3].strip()
            e = self.lines[i + 4].strip()
            if a != push_acc or e != pop_acc:
                i += 1
                continue
            if not (b.startswith(mov_si_mem_prefix) and b.endswith("]")):
                i += 1
                continue
            if not (c.startswith(mov_acc_mem_prefix) and c.endswith("]")):
                i += 1
                continue
            if d != add_si_acc:
                i += 1
                continue
            mem_operand = c[len(f"mov {acc}, ") :]
            self.lines[i] = self.lines[i + 1]
            self.lines[i + 1] = f"        add {si}, {mem_operand}"
            del self.lines[i + 2 : i + 5]
            continue

    def peephole_jump_next(self) -> None:
        """Remove unconditional jumps to the immediately following label."""
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a.startswith("jmp ") and b == f"{a.split()[1]}:":
                del self.lines[i]
                continue
            i += 1

    def peephole_label_forwarding(self) -> None:
        """Retarget jumps through a label that immediately trampolines.

        When an unreachable-by-fall-through label ``.L1:`` is followed
        by ``jmp .L2``, rewrite every ``jCC .L1`` in the rest of the
        function to ``jCC .L2`` and drop the label/jmp pair.  "No
        fall-through" is proven by requiring the previous line to be
        an unconditional ``jmp`` — that's the shape ``break`` at the
        end of a ``while (1)`` body produces right before the
        implicit program exit.
        """
        jumps = {
            "ja",
            "jae",
            "jb",
            "jbe",
            "jc",
            "je",
            "jg",
            "jge",
            "jl",
            "jle",
            "jmp",
            "jnc",
            "jne",
            "jno",
            "jnp",
            "jns",
            "jnz",
            "jo",
            "jp",
            "js",
            "jz",
        }
        i = 1
        while i < len(self.lines) - 1:
            previous_line = self.lines[i - 1].strip()
            label_line = self.lines[i].strip()
            next_line = self.lines[i + 1].strip()
            if not previous_line.startswith("jmp "):
                i += 1
                continue
            if not (label_line.endswith(":") and " " not in label_line):
                i += 1
                continue
            if not next_line.startswith("jmp "):
                i += 1
                continue
            old_label = label_line[:-1]
            new_target = next_line[len("jmp ") :]
            if old_label == new_target:
                i += 1
                continue
            for j in range(len(self.lines)):
                if j == i or j == i + 1:
                    continue
                stripped = self.lines[j].strip()
                parts = stripped.split(None, 1)
                if len(parts) == 2 and parts[0] in jumps and parts[1] == old_label:
                    self.lines[j] = self.lines[j].replace(old_label, new_target)
            del self.lines[i : i + 2]
            i = max(1, i - 1)

    def peephole_memory_arithmetic(self) -> None:
        """Fuse load/modify/store sequences into direct arithmetic.

        Handles these patterns where ``D`` is either a memory operand
        ``[L]`` or a 16-bit general-purpose register:
        - ``mov ax, D / mov cx, 1 / add ax, cx / mov D, ax`` →
          ``inc D`` (or ``inc word [L]`` for memory)
        - ``mov ax, D / mov cx, 1 / sub ax, cx / mov D, ax`` →
          ``dec D`` (or ``dec word [L]`` for memory)
        - ``mov ax, D / mov cx, imm / (add|sub) ax, cx /
          mov D, ax`` → ``(add|sub) D, imm``
        """
        registers = self.target.non_acc_registers
        mov_acc_prefix = f"mov {self.target.acc}, "
        mov_cx_prefix = f"mov {self.target.count_register}, "
        add_acc_cx = f"add {self.target.acc}, {self.target.count_register}"
        sub_acc_cx = f"sub {self.target.acc}, {self.target.count_register}"
        i = 0
        while i < len(self.lines) - 3:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            d = self.lines[i + 3].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            source = a[len(mov_acc_prefix) :]
            is_memory = source.startswith("[") and source.endswith("]")
            is_register = source in registers
            if not (is_memory or is_register):
                i += 1
                continue
            if not (b.startswith(mov_cx_prefix) and not b.endswith("]")):
                i += 1
                continue
            if c not in {add_acc_cx, sub_acc_cx}:
                i += 1
                continue
            if d != f"mov {source}, {self.target.acc}":
                i += 1
                continue
            immediate = b[len(mov_cx_prefix) :]
            operator = "add" if c == add_acc_cx else "sub"
            width = f"{self.target.word_size} " if is_memory else ""
            if immediate == "1":
                instruction = "inc" if operator == "add" else "dec"
                self.lines[i] = f"        {instruction} {width}{source}"
            else:
                self.lines[i] = f"        {operator} {width}{source}, {immediate}"
            del self.lines[i + 1 : i + 4]
            continue
        # Second pass: 3-instruction pattern without CX intermediate.
        # Handles four shapes of ``D = D <operation> Y`` where D is memory or
        # a 16-bit register:
        #   mov ax, D / (add|sub|and) ax, imm  / mov D, ax → operation D, imm
        #   mov ax, D / inc ax  / mov D, ax                → inc D
        #   mov ax, D / dec ax  / mov D, ax                → dec D
        #   mov ax, D / (add|sub|and) ax, <reg> / mov D, ax → operation D, <reg>
        mnemonic_operations = {"add", "sub", "and", "or", "xor"}
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            source = a[len(mov_acc_prefix) :]
            is_memory = source.startswith("[") and source.endswith("]")
            is_register = source in registers
            if not (is_memory or is_register):
                i += 1
                continue
            operator = None
            operand = None
            if b == f"inc {self.target.acc}":
                operator = "inc"
                operand = ""
            elif b == f"dec {self.target.acc}":
                operator = "dec"
                operand = ""
            else:
                for operation in mnemonic_operations:
                    prefix = f"{operation} {self.target.acc}, "
                    if b.startswith(prefix):
                        operator = operation
                        operand = b[len(prefix) :]
                        break
            if operator is None:
                i += 1
                continue
            # Reject memory operands — would need swapping to ``mov ax, [X] /
            # operation D, ax`` and handled by the next pass instead.
            if operand.startswith("["):
                i += 1
                continue
            if c != f"mov {source}, {self.target.acc}":
                i += 1
                continue
            width = f"{self.target.word_size} " if is_memory else ""
            if operator in ("inc", "dec"):
                self.lines[i] = f"        {operator} {width}{source}"
            elif operand == "1" and operator in ("add", "sub"):
                instruction = "inc" if operator == "add" else "dec"
                self.lines[i] = f"        {instruction} {width}{source}"
            else:
                self.lines[i] = f"        {operator} {width}{source}, {operand}"
            del self.lines[i + 1 : i + 3]
            continue
        # Third pass: ``D = D <operation> [X]`` with both sides in memory.
        # ``mov ax, D / operation ax, [X] / mov D, ax`` collapses to
        # ``mov ax, [X] / operation D, ax`` (10 bytes → 7 for word operations).  Only
        # safe when D is memory (the target of ``operation D, ax`` must be
        # addressable as r/m16) and D ≠ X (overlapping would read the
        # stale value after the operation writes D).
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            source = a[len(mov_acc_prefix) :]
            if not (source.startswith("[") and source.endswith("]")):
                i += 1
                continue
            operator = None
            rhs = None
            for operation in ("add", "sub", "and", "or", "xor"):
                prefix = f"{operation} {self.target.acc}, "
                if b.startswith(prefix):
                    operator = operation
                    rhs = b[len(prefix) :]
                    break
            if operator is None:
                i += 1
                continue
            if not (rhs.startswith("[") and rhs.endswith("]")):
                i += 1
                continue
            if rhs == source:
                i += 1
                continue
            if c != f"mov {source}, {self.target.acc}":
                i += 1
                continue
            self.lines[i] = f"        mov {self.target.acc}, {rhs}"
            self.lines[i + 1] = f"        {operator} {source}, {self.target.acc}"
            del self.lines[i + 2]
            continue

    def peephole_memory_arithmetic_byte(self) -> None:
        """Fuse byte-global load / modify / store into memory-direct byte ops.

        Byte-scalar globals load via ``mov al, [_g_X] / xor ah, ah`` and
        store via ``mov [_g_X], al``; a compound-assign emits:

            mov al, [_g_X]
            xor ah, ah
            inc ax           (or: add|sub|and|or|xor ax, imm16,
                              or: mov cx, imm16 / add|sub ax, cx)
            mov [_g_X], al

        The low byte of the AX-width operation is identical to the
        corresponding AL-width operation on the same low byte (addition /
        subtraction / bitwise all ignore the high byte when the result
        is truncated to AL on store), so the whole sequence collapses
        to a single memory-direct byte instruction:

            inc byte [_g_X]              4 bytes (FE 06 xxxx)
            dec byte [_g_X]              4 bytes
            add|sub byte [_g_X], imm8    5 bytes (80 /N xxxx imm8)
            and|or|xor byte [_g_X], imm8 5 bytes

        Byte-width ops require the immediate to fit in 8 bits —
        bitwise masks wider than a byte would lose their high-byte
        effect when narrowed.  For ``add`` / ``sub`` any 16-bit
        immediate truncates cleanly to imm8 for the low-byte result
        (carry into AH is discarded on store), so those fuse
        regardless of the original ``mov cx, <imm>`` width.

        Saves 4-5 bytes per compound-assign site on a byte-scalar
        global — the reason cc.py can keep ``include_depth`` /
        ``iteration_count`` / similar arithmetic-heavy byte globals
        as ``uint8_t`` without regressing binary size.
        """

        def fits_imm8(literal: str, /) -> bool:
            try:
                value = int(literal, 0)
            except ValueError:
                return False
            return -128 <= value <= 255

        # 4-line pattern without CX intermediate:
        #   mov al, [mem] / xor ah, ah / <operation> ax, <imm|reg> / mov [mem], al
        single_immediate_operations = {"add", "sub", "and", "or", "xor"}
        i = 0
        while i < len(self.lines) - 3:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            d = self.lines[i + 3].strip()
            if not a.startswith("mov al, ["):
                i += 1
                continue
            source = a[len("mov al, ") :]
            if not (source.startswith("[") and source.endswith("]")):
                i += 1
                continue
            if b != "xor ah, ah":
                i += 1
                continue
            if d != f"mov {source}, al":
                i += 1
                continue
            if c == "inc ax":
                self.lines[i] = f"        inc byte {source}"
                del self.lines[i + 1 : i + 4]
                continue
            if c == "dec ax":
                self.lines[i] = f"        dec byte {source}"
                del self.lines[i + 1 : i + 4]
                continue
            operation_name: str | None = None
            operand: str | None = None
            for operation in single_immediate_operations:
                prefix = f"{operation} ax, "
                if c.startswith(prefix):
                    operation_name = operation
                    operand = c[len(prefix) :]
                    break
            if operation_name is None:
                i += 1
                continue
            if operand.startswith("["):
                i += 1
                continue
            # Bitwise masks narrowed to byte can silently drop
            # high-byte effect; only fuse when the literal fits in 8
            # bits.  add/sub truncate cleanly so any imm is OK.
            if operation_name in ("and", "or", "xor") and not fits_imm8(operand):
                i += 1
                continue
            if operation_name == "add" and operand == "1":
                self.lines[i] = f"        inc byte {source}"
            elif operation_name == "sub" and operand == "1":
                self.lines[i] = f"        dec byte {source}"
            else:
                # NASM accepts the wider literal for add/sub byte; it
                # assembles the low 8 bits since the destination is
                # byte-sized.
                self.lines[i] = f"        {operation_name} byte {source}, {operand}"
            del self.lines[i + 1 : i + 4]
            continue

        # 5-line pattern with CX intermediate (matches the codegen shape
        # before peephole_memory_arithmetic fuses the CX-mov):
        #   mov al, [mem] / xor ah, ah / mov cx, <imm> / (add|sub) ax, cx
        #   / mov [mem], al
        i = 0
        while i < len(self.lines) - 4:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            d = self.lines[i + 3].strip()
            e = self.lines[i + 4].strip()
            if not a.startswith("mov al, ["):
                i += 1
                continue
            source = a[len("mov al, ") :]
            if not (source.startswith("[") and source.endswith("]")):
                i += 1
                continue
            if b != "xor ah, ah":
                i += 1
                continue
            if not (c.startswith("mov cx, ") and not c.endswith("]")):
                i += 1
                continue
            if d not in {"add ax, cx", "sub ax, cx"}:
                i += 1
                continue
            if e != f"mov {source}, al":
                i += 1
                continue
            immediate = c[len("mov cx, ") :]
            operator = "add" if d == "add ax, cx" else "sub"
            if immediate == "1":
                instruction = "inc" if operator == "add" else "dec"
                self.lines[i] = f"        {instruction} byte {source}"
            else:
                self.lines[i] = f"        {operator} byte {source}, {immediate}"
            del self.lines[i + 1 : i + 5]
            continue

    def peephole_redundant_bx(self) -> None:
        """Remove redundant ``mov bx, X`` / ``mov si, X`` reloads.

        Tracks the value in each scratch register across instructions
        that don't clobber it (comparisons, conditional jumps).  Resets
        on labels, calls, interrupts, and any instruction that writes
        to the register.  BX and SI are both subscript scratch targets
        so either can linger with a useful value across sites.
        """
        self._dedup_register_reloads(self.target.bx_register)
        self._dedup_register_reloads(self.target.si_register)

    def peephole_redundant_byte_mask(self) -> None:
        """Drop ``and ax, 255`` when AX is provably zero-extended from a byte.

        The C expression ``byte_local & 0xFF`` (or any wider mask whose
        low byte saturates the byte operand) codegens as ``mov al,
        [X] / xor ah, ah / and ax, 255``.  The zero-extend has already
        cleared AH, so the mask is a no-op on the value.  Dropping it
        saves 4 bytes per site — there are 106+ sites in asm.c from
        the ``emit_byte(x & 0xFF)`` idiom alone.

        The ``and`` does set flags, though: ZF = (AL == 0), unlike the
        preceding ``xor`` which always leaves ZF=1 (AH=0).  So the
        drop is only safe when the following instruction doesn't
        consume flags — walk forward to confirm.  Conservative
        allowlist: ``mov`` / ``call`` / ``push`` / ``pop`` / ``shl`` /
        ``shr`` / ``ret`` don't read flags; conditional jumps
        (``j*`` except ``jmp``) and ``adc`` / ``sbb`` / ``rcl`` /
        ``rcr`` do.  Anything else: bail.
        """
        flag_safe_prefixes = (
            "mov ",
            "call ",
            "push ",
            "pop ",
            "shl ",
            "shr ",
            "ret",
            "int ",
            "lea ",
        )
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a == "xor ah, ah" and b == "and ax, 255":
                # Look past the mask at what actually consumes the value.
                follower = self.lines[i + 2].strip() if i + 2 < len(self.lines) else ""
                if follower.startswith(flag_safe_prefixes):
                    del self.lines[i + 1]
                    continue
            i += 1

    def peephole_redundant_register_swap(self) -> None:
        """Drop ``mov B, A`` immediately after ``mov A, B`` — A still holds B's value.

        Common after :meth:`peephole_register_arithmetic`'s sibling
        cases and at out_register function epilogues, where
        ``*argc = count`` (with ``count`` pinned to CX and ``argc``
        having ``out_register("cx")``) emits the redundant pair
        ``mov ax, cx ; mov cx, ax``.  The second ``mov cx, ax`` is a
        no-op — CX already holds count.  The first ``mov ax, cx`` is
        also dead at the function epilogue, but liveness analysis is
        beyond a peephole; the trailing dead store stays.
        """
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a.startswith("mov ") and b.startswith("mov "):
                a_parts = a[len("mov ") :].split(", ")
                b_parts = b[len("mov ") :].split(", ")
                if len(a_parts) == 2 and len(b_parts) == 2 and a_parts[0] == b_parts[1] and a_parts[1] == b_parts[0]:
                    del self.lines[i + 1]
                    continue
            i += 1

    def peephole_register_arithmetic(self) -> None:
        """Compute directly into a pinned-local target register.

        Turns ``mov ax, X / <operation> ax, Y / mov <reg>, ax`` into
        ``mov <reg>, X / <operation> <reg>, Y`` when <reg> isn't already
        read by Y (e.g., ``sub reg, reg`` would zero it).  The
        ``mov <reg>, X`` step collapses to nothing when X is the same
        register as <reg> — handled by :meth:`peephole_self_move`.

        Saves the trailing ``mov <reg>, ax`` (2 bytes) whenever the
        arithmetic result is being piped straight into a register
        (typically a pinned local).  After the transform AX retains
        whatever it held before the sequence — the original sequence
        ended with AX holding the result, so the rewrite changes AX's
        post-sequence value.  Skip the transform when the next
        instruction reads AX so cc.py's post-emit code paths that
        consume the just-stored value via ``cmp ax, ...`` etc. still
        see the correct value.

        Also handles the unary forms ``inc ax`` / ``dec ax`` between
        the two ``mov``s — same shape, no immediate operand.
        """
        registers = self.target.non_acc_registers
        binary_operations = tuple(f"{operation} {self.target.acc}," for operation in ("add", "sub", "and", "or", "xor"))
        unary_operations = (f"inc {self.target.acc}", f"dec {self.target.acc}")
        mov_acc_prefix = f"mov {self.target.acc}, "
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith(mov_acc_prefix):
                i += 1
                continue
            is_binary = any(b.startswith(operation) for operation in binary_operations)
            is_unary = b in unary_operations
            if not (is_binary or is_unary):
                i += 1
                continue
            if not c.startswith("mov "):
                i += 1
                continue
            parts = c[len("mov ") :].split(", ")
            if len(parts) != 2 or parts[1] != self.target.acc or parts[0] not in registers:
                i += 1
                continue
            target = parts[0]
            # Skip when the operand of the arithmetic references the
            # target register — rewriting would make it self-referential.
            if is_binary:
                operand = b.split(", ", 1)[1]
                if target in operand.split():
                    i += 1
                    continue
            # Skip when the instruction after the sequence reads AX —
            # cc.py occasionally pipes the result both into a pinned
            # register and through AX (e.g., ``mov dx, ax ; cmp ax, bx``).
            # Dropping the trailing ``mov reg, ax`` would leave AX
            # holding its pre-sequence value, breaking that read.
            if i + 3 < len(self.lines) and self._reads_acc(self.lines[i + 3].strip()):
                i += 1
                continue
            source = a[len(mov_acc_prefix) :]
            if is_unary:
                op_name = "inc" if b.startswith("inc ") else "dec"
                self.lines[i] = f"        mov {target}, {source}"
                self.lines[i + 1] = f"        {op_name} {target}"
                del self.lines[i + 2]
            else:
                new_op = b.replace(f"{self.target.acc},", f"{target},", 1)
                self.lines[i] = f"        mov {target}, {source}"
                self.lines[i + 1] = f"        {new_op}"
                del self.lines[i + 2]
            continue

    def peephole_self_move(self) -> None:
        """Drop ``mov X, X`` no-ops.

        These typically arise from :meth:`peephole_register_arithmetic`
        rewriting ``mov ax, R / op ax, K / mov R, ax`` into
        ``mov R, R / op R, K`` when source and target collide on the
        same pinned register.
        """
        result: list[str] = []
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith("mov "):
                parts = stripped[len("mov ") :].split(", ")
                if len(parts) == 2 and parts[0] == parts[1]:
                    continue
            result.append(line)
        self.lines = result

    def peephole_store_reload(self) -> None:
        """Remove redundant store-then-reload sequences.

        Looks for ``mov [ADDR], ax`` followed (possibly across
        AX-preserving instructions like ``cmp``, ``test``, conditional
        jumps, or pushes/pops of non-AX registers) by ``mov ax, [ADDR]``
        — the reload is dead.  Stops scanning when it hits an
        instruction that could change AX, ``[ADDR]``, or control flow
        in a way that lets a different value reach the reload.
        """
        skip_prefixes = (
            "cmp ",
            "test ",
            "ja ",
            "jae ",
            "jb ",
            "jbe ",
            "jc ",
            "je ",
            "jg ",
            "jge ",
            "jl ",
            "jle ",
            "jnc ",
            "jne ",
            "jno ",
            "jnp ",
            "jns ",
            "jnz ",
            "jo ",
            "jp ",
            "js ",
            "jz ",
        )
        non_ax_pushpop = {f"{operation} {register}" for operation in ("push", "pop") for register in ("bx", "cx", "dx", "si", "di", "bp")}
        i = 0
        while i < len(self.lines) - 1:
            line = self.lines[i].strip()
            if not (line.startswith("mov [") and line.endswith((f"], {self.target.acc}", "], al"))):
                i += 1
                continue
            address = line[4 : line.index("]") + 1]
            reload_word = f"mov {self.target.acc}, {address}"
            reload_byte = f"mov al, {address}"
            j = i + 1
            removed = False
            while j < len(self.lines):
                candidate = self.lines[j].strip()
                if candidate in (reload_word, reload_byte):
                    del self.lines[j]
                    removed = True
                    break
                # AX-preserving instructions: cmp/test/Jcc and pushes/pops
                # of registers other than AX.
                if any(candidate.startswith(prefix) for prefix in skip_prefixes) or candidate in non_ax_pushpop:
                    j += 1
                    continue
                break
            if removed:
                continue
            i += 1

    def peephole_unused_cld(self) -> None:
        """Remove or deduplicate ``cld`` instructions.

        When no string instruction is emitted, all ``cld`` instructions
        are removed.  Otherwise, redundant ``cld`` instructions are
        removed when the direction flag is already clear (no intervening
        label, call, or interrupt that could change DF).
        """
        string_operations = ("lodsb", "lodsw", "stosb", "stosw", "movsb", "movsw", "scasb", "scasw", "cmpsb", "cmpsw", "rep ")
        has_string_operations = any(any(line.strip().startswith(operation) for operation in string_operations) for line in self.lines)
        if not has_string_operations:
            self.lines = [line for line in self.lines if line.strip() != "cld"]
            return
        # Deduplicate: track whether DF is known-clear.
        df_clear = False
        result: list[str] = []
        for line in self.lines:
            stripped = line.strip()
            if stripped == "cld":
                if df_clear:
                    continue  # redundant
                df_clear = True
            elif stripped.endswith(":") or stripped.startswith(("call ", "int ")):
                df_clear = False
            result.append(line)
        self.lines = result

    def run(self) -> list[str]:
        """Run peephole optimization passes over generated assembly.

        Ordering note: :meth:`peephole_memory_arithmetic` and
        :meth:`peephole_dx_to_memory` both run before
        :meth:`peephole_store_reload` so that load/modify/store triples
        get folded into a direct ``inc D`` (etc.) first, and so that a
        ``%`` expression's ``mov ax, dx / mov [X], ax`` collapses to
        ``mov [X], dx`` before ``store_reload`` gets to consider the
        pair.  Reversing either order lets ``store_reload`` delete a
        reload that ``emit_store_local`` added as a safety net — the
        subsequent fuse would then leave AX holding the pre-store value
        (the quotient, in the ``%`` case) while the downstream code
        reads AX expecting the just-stored value.
        """
        self.peephole_dead_code()
        self.peephole_double_jump()
        self.peephole_jump_next()
        self.peephole_label_forwarding()
        self.peephole_memory_arithmetic()
        self.peephole_memory_arithmetic_byte()
        self.peephole_dx_to_memory()
        self.peephole_store_reload()
        self.peephole_dead_temp_slots()
        self.peephole_constant_to_register()
        self.peephole_register_arithmetic()
        self.peephole_self_move()
        self.peephole_redundant_register_swap()
        self.peephole_index_through_memory()
        self.peephole_fold_zero_save()
        self.peephole_compare_through_register()
        self.peephole_dead_ah()
        self.peephole_redundant_byte_mask()
        self.peephole_unused_cld()
        self.peephole_dead_stores()
        self.peephole_dead_test_after_sbb()
        self.peephole_redundant_bx()
        return self.lines
