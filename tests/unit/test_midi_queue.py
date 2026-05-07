"""Pytest unit tests for the /dev/midi event queue + drain logic.

Compiles a stripped copy of `src/fs/fd/midi.c` and `src/drivers/opl3.c`
against the host system clang as a shared library and exercises the
pure-C ring + drain logic through ctypes accessors defined in
`midi_queue_harness.c`.

The kernel sources are cc.py-targeted: they use carry_return /
register-pinning attributes, NASM-syntax `asm("foo equ _g_foo")`
aliases, and the `kernel_outb` / `kernel_inb` cc.py intrinsics.  We
work around the contamination by:

    1. Erasing every `asm(...);` statement from the source text before
       handing it to clang.  The aliases (`asm("foo equ _g_foo");`) are
       NASM-only and would die in GAS; the inline-asm definition of
       `fd_ioctl_midi` is intentionally skipped -- that function is
       exercised by the integration test (play_midi smoke).
    2. `#define __attribute__(x)` inside the harness so cc.py-specific
       attribute payloads (`carry_return`, `out_register("ax")`,
       `in_register("ecx")`) compile away silently.

What is and isn't tested here
-----------------------------

Tested directly via ctypes:
    midi_reset_state, midi_ring_full, midi_ring_push, midi_drain_due,
    fd_close_midi, fd_write_midi (carry_return attr erased; the
    returned int is ignored since the test exercises the side-effects
    on bytes_written / the ring).

Deferred to integration testing:
    fd_ioctl_midi -- implemented entirely in NASM-syntax inline asm.
    opl_probe / status-register handshake -- host clang has no real
    OPL chip, and the function is short enough that the QEMU-side
    smoke test (which sees the chip respond) is a better fit.

Run with: ``pytest tests/unit/test_midi_queue.py -v``
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS = Path(__file__).resolve().parent / "midi_queue_harness.c"
MIDI_SOURCE = REPO_ROOT / "src" / "fs" / "fd" / "midi.c"
OPL3_SOURCE = REPO_ROOT / "src" / "drivers" / "opl3.c"

# OPL bank port pairs; matches src/drivers/opl3.c opl_write().
BANK0_STATUS = 0x388
BANK0_DATA = 0x389
BANK1_STATUS = 0x38A
BANK1_DATA = 0x38B
DELAY_PORT = 0x80  # io_delay_short reads this 4 times after each outb.


def _build_shared_library() -> tuple[ctypes.CDLL, Path]:
    """Compile harness + stripped kernel sources into a host shared library."""
    temp_directory = Path(tempfile.mkdtemp())
    midi_stripped = temp_directory / "midi_stripped.c"
    opl3_stripped = temp_directory / "opl3_stripped.c"
    library_path = temp_directory / "midi_queue.so"

    # Wrap each stripped source so the type names (uint8_t etc), the
    # `__attribute__(x)` no-op, and the cc.py-builtin signatures
    # (kernel_inb / kernel_outb) come from a host-clang prelude.  In
    # the real kernel build kernel_inb/outb are intrinsics that emit
    # `in`/`out` directly; here they're ordinary C functions defined
    # in the harness so the tests can capture port traffic.
    prelude = "#include <stdint.h>\n#define __attribute__(x)\nint kernel_inb(int port);\nvoid kernel_outb(int port, int value);\n"
    midi_stripped.write_text(prelude + _strip_kernel_source(source=MIDI_SOURCE.read_text()))
    opl3_stripped.write_text(prelude + _strip_kernel_source(source=OPL3_SOURCE.read_text()))

    subprocess.check_call([
        "clang",
        "-O0",
        "-Wall",
        "-Werror",
        # The kernel sources lean on K&R-era `int foo()` as
        # zero-argument prototypes; clang -Wall would warn about
        # `-Wstrict-prototypes` on -Wpedantic, but at -Wall -Werror it
        # passes.  Suppress unused-parameter to keep the harness lean.
        "-Wno-unused-parameter",
        "-shared",
        "-fPIC",
        str(HARNESS),
        str(midi_stripped),
        str(opl3_stripped),
        "-o",
        str(library_path),
    ])
    library = ctypes.CDLL(str(library_path))

    # Harness accessors.
    library.harness_reset_records.argtypes = []
    library.harness_reset_records.restype = None
    library.harness_record_count_get.argtypes = []
    library.harness_record_count_get.restype = ctypes.c_int
    library.harness_record_is_inb.argtypes = [ctypes.c_int]
    library.harness_record_is_inb.restype = ctypes.c_int
    library.harness_record_port.argtypes = [ctypes.c_int]
    library.harness_record_port.restype = ctypes.c_int
    library.harness_record_value.argtypes = [ctypes.c_int]
    library.harness_record_value.restype = ctypes.c_int
    library.harness_midi_head.argtypes = []
    library.harness_midi_head.restype = ctypes.c_uint8
    library.harness_midi_tail.argtypes = []
    library.harness_midi_tail.restype = ctypes.c_uint8
    library.harness_midi_virtual_clock.argtypes = []
    library.harness_midi_virtual_clock.restype = ctypes.c_uint32

    # Kernel surface.
    library.midi_reset_state.argtypes = []
    library.midi_reset_state.restype = None
    library.midi_ring_full.argtypes = []
    library.midi_ring_full.restype = ctypes.c_int
    library.midi_ring_push.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.midi_ring_push.restype = None
    library.midi_drain_due.argtypes = []
    library.midi_drain_due.restype = None
    library.fd_close_midi.argtypes = []
    library.fd_close_midi.restype = None
    library.fd_write_midi.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_int,
    ]
    library.fd_write_midi.restype = ctypes.c_int
    library.opl_silence_all.argtypes = []
    library.opl_silence_all.restype = None
    library.opl_write.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    library.opl_write.restype = None

    return library, temp_directory


def _filter_outbs(*, exclude_delay_port: bool = True) -> list[tuple[int, int]]:
    """Return the list of (port, value) tuples recorded by kernel_outb.

    By default skips the io_delay_short reads (kernel_inb 0x80 x 4
    after each outb) and any other inb traffic.  The remaining list is
    just OPL register/data writes, which is what every test cares
    about.
    """
    out: list[tuple[int, int]] = []
    for index in range(_LIBRARY.harness_record_count_get()):
        if _LIBRARY.harness_record_is_inb(index):
            continue
        port = _LIBRARY.harness_record_port(index)
        value = _LIBRARY.harness_record_value(index)
        if exclude_delay_port and port == DELAY_PORT:
            continue
        out.append((port, value))
    return out


def _reset_state() -> None:
    """Drop queued events, zero the recorder, zero the virtual clock."""
    _set_ticks(ticks=0)
    _LIBRARY.midi_reset_state()
    _LIBRARY.harness_reset_records()


def _set_buffer(*, payload: bytes) -> ctypes.Array:
    """Stash `payload` in a Python-owned ctypes buffer + point fd_write_buffer at it.

    Returns the array so the caller can keep it alive for the duration
    of the test (ctypes does not pin storage referenced through
    in_dll'd globals).
    """
    array = (ctypes.c_uint8 * len(payload))(*payload)
    _FD_WRITE_BUFFER.value = ctypes.addressof(array)
    return array


def _set_ticks(*, ticks: int) -> None:
    _SYSTEM_TICKS.value = ticks


def _strip_kernel_source(*, source: str) -> str:
    r"""Remove cc.py-specific bits that host clang can't compile.

    Strips every `asm("...");` statement (single- and multi-line):
        - `asm("midi_ring equ _g_midi_ring");`  - NASM equ alias
        - `asm("fd_ioctl_midi:\n" "..." ...);`  - inline NASM function

    The cc.py attribute payloads (`carry_return`, `out_register(...)`,
    `in_register(...)`) are handled by `#define __attribute__(x)` in
    the harness, not by this textual pass.
    """
    # Regex matches `asm(` ... `);` non-greedily across lines.
    pattern = re.compile(r"\basm\s*\([^;]*?\)\s*;", re.DOTALL)
    return pattern.sub("", source)


_LIBRARY, _TEMP_DIRECTORY = _build_shared_library()

_SYSTEM_TICKS = ctypes.c_uint32.in_dll(_LIBRARY, "system_ticks")
_FD_WRITE_BUFFER = ctypes.c_void_p.in_dll(_LIBRARY, "fd_write_buffer")


# -- Tests -----------------------------------------------------------


def test_bad_bank_dropped_clock_advances() -> None:
    """bank=2 commands consume bytes + advance the virtual clock but never enqueue."""
    _reset_state()
    payload = bytes([
        # Command 1: bank 2 (invalid), delay 10.  Should be dropped.
        0x0A,
        0x00,
        0x02,
        0x40,
        0x7F,
        0x00,
        # Command 2: bank 0 (valid), delay 5.  Total clock = 15.
        0x05,
        0x00,
        0x00,
        0x41,
        0x33,
        0x00,
    ])
    array = _set_buffer(payload=payload)
    bytes_written = ctypes.c_int(0)
    _LIBRARY.fd_write_midi(ctypes.byref(bytes_written), len(payload))
    assert bytes_written.value == 12  # both commands consumed.
    assert _LIBRARY.harness_midi_tail() == 1  # only the valid one enqueued.
    assert _LIBRARY.harness_midi_virtual_clock() == 15

    # Drain at tick 14: nothing yet (the valid event is due at 15).
    _set_ticks(ticks=14)
    _LIBRARY.midi_drain_due()
    assert _filter_outbs() == []

    # Drain at tick 15: the valid event fires.
    _set_ticks(ticks=15)
    _LIBRARY.midi_drain_due()
    assert _filter_outbs() == [(BANK0_STATUS, 0x41), (BANK0_DATA, 0x33)]
    del array


def test_close_emits_key_off_for_all_voices() -> None:
    """fd_close_midi -> opl_silence_all -> 18 KEY_OFF outb pairs (9 voices x 2 banks)."""
    _reset_state()
    # Pre-load some queued events to show fd_close_midi drops them too.
    payload = bytes([0x0A, 0x00, 0x00, 0x40, 0x7F, 0x00] * 5)
    array = _set_buffer(payload=payload)
    bytes_written = ctypes.c_int(0)
    _LIBRARY.fd_write_midi(ctypes.byref(bytes_written), len(payload))
    assert _LIBRARY.harness_midi_tail() == 5

    _LIBRARY.harness_reset_records()
    _LIBRARY.fd_close_midi()
    assert _LIBRARY.harness_midi_head() == 0
    assert _LIBRARY.harness_midi_tail() == 0

    outbs = _filter_outbs()
    expected: list[tuple[int, int]] = []
    for voice in range(9):
        # opl_silence_all: opl_write(0, 0xB0+voice, 0); opl_write(1, 0xB0+voice, 0).
        # Each opl_write is two outbs (status, then data).
        expected.extend([
            (BANK0_STATUS, 0xB0 + voice),
            (BANK0_DATA, 0),
            (BANK1_STATUS, 0xB0 + voice),
            (BANK1_DATA, 0),
        ])
    assert outbs == expected
    assert len(outbs) == 36  # 9 voices x 2 banks x 2 outbs (status+data).
    del array


def test_empty_queue_drain_is_noop() -> None:
    """No queued events -> midi_drain_due emits zero outbs."""
    _reset_state()
    _set_ticks(ticks=1000)
    _LIBRARY.midi_drain_due()
    assert _filter_outbs() == []
    assert _LIBRARY.harness_midi_head() == 0
    assert _LIBRARY.harness_midi_tail() == 0


def test_full_ring_short_writes() -> None:
    """256 commands -> first 255 enqueue, fd_write_midi reports 1530 bytes consumed.

    The ring has MIDI_RING_SIZE=256 slots but only 255 effective
    capacity (head==tail means empty).  The 256th command is left in
    the userland buffer for the next write.
    """
    _reset_state()
    # 256 commands, each delay=1, bank 0, distinct (reg, value).
    payload = b"".join(bytes([0x01, 0x00, 0x00, index & 0xFF, (index ^ 0x55) & 0xFF, 0x00]) for index in range(256))
    assert len(payload) == 256 * 6
    array = _set_buffer(payload=payload)
    bytes_written = ctypes.c_int(0)
    _LIBRARY.fd_write_midi(ctypes.byref(bytes_written), len(payload))
    # 255 consumed x 6 bytes = 1530.
    assert bytes_written.value == 255 * 6
    assert _LIBRARY.harness_midi_tail() == 255
    assert _LIBRARY.harness_midi_head() == 0
    assert _LIBRARY.midi_ring_full() == 1
    del array


def test_single_immediate_event_emits_outb_pair() -> None:
    """delay=0 on bank 0 -> drain emits (0x388,reg) + (0x389,value)."""
    _reset_state()
    payload = bytes([
        0x00,
        0x00,  # delay = 0
        0x00,  # bank 0
        0x40,
        0x7F,  # reg 0x40, value 0x7F
        0x00,  # reserved
    ])
    array = _set_buffer(payload=payload)
    bytes_written = ctypes.c_int(0)
    _LIBRARY.fd_write_midi(ctypes.byref(bytes_written), len(payload))
    assert bytes_written.value == 6
    assert _LIBRARY.harness_midi_tail() == 1
    assert _LIBRARY.harness_midi_head() == 0
    # Drain at any tick >= 0 fires it.
    _set_ticks(ticks=0)
    _LIBRARY.midi_drain_due()
    assert _filter_outbs() == [(BANK0_STATUS, 0x40), (BANK0_DATA, 0x7F)]
    assert _LIBRARY.harness_midi_head() == 1
    del array


def test_ten_events_in_order() -> None:
    """Ten events at delays 10,10,...,10 (cumulative 10..100) fire in order."""
    _reset_state()
    # Each command advances the virtual clock by 10 ticks.
    # bank 0, reg 0x10+index, value 0xA0+index.
    payload = b"".join(bytes([0x0A, 0x00, 0x00, 0x10 + index, 0xA0 + index, 0x00]) for index in range(10))
    array = _set_buffer(payload=payload)
    bytes_written = ctypes.c_int(0)
    _LIBRARY.fd_write_midi(ctypes.byref(bytes_written), len(payload))
    assert bytes_written.value == 60
    assert _LIBRARY.harness_midi_tail() == 10

    # tick 9: nothing due yet.
    _set_ticks(ticks=9)
    _LIBRARY.midi_drain_due()
    assert _filter_outbs() == []

    # tick 10: first event (reg 0x10).
    _set_ticks(ticks=10)
    _LIBRARY.midi_drain_due()
    assert _filter_outbs() == [(BANK0_STATUS, 0x10), (BANK0_DATA, 0xA0)]
    assert _LIBRARY.harness_midi_head() == 1

    # Walk forward 10 ticks at a time, draining each event in turn.
    for index in range(1, 10):
        _LIBRARY.harness_reset_records()
        _set_ticks(ticks=10 + index * 10)
        _LIBRARY.midi_drain_due()
        assert _filter_outbs() == [
            (BANK0_STATUS, 0x10 + index),
            (BANK0_DATA, 0xA0 + index),
        ]
        assert _LIBRARY.harness_midi_head() == index + 1
    del array
