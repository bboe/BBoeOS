#!/usr/bin/env python3
"""Build bin/doom — a flat-binary BBoeOS userland Doom port.

Pipeline:
  1. fetch doomgeneric source (third_party/doomgeneric, GPLv2) if missing
  2. fetch chocolate-doom OPL stack (third_party/chocolate-doom-opl, GPL-2)
     if missing
  3. build libbboeos.a (tools/libc/Makefile)
  4. compile each non-platform-backend doomgeneric .c file
  5. compile our bboeos backend (tools/doom/bboeos_doomgeneric.c)
  6. link everything plus tools/libc/_start.o through tools/libc/program.ld
     into a flat binary

Each .c file's .o lands in build/doom/ so re-runs are incremental.
``--clean`` wipes build/doom/ first.

Doom backends we exclude (host-specific):
  doomgeneric_{allegro,emscripten,linuxvt,sdl,soso,sosox,win,xlib}
Audio backends we exclude (no kernel audio yet):
  i_{allegromusic,allegrosound,sdlmusic,sdlsound}
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHOCOLATE_OPL = REPO / "third_party" / "chocolate-doom-opl"
LIBC = REPO / "tools" / "libc"
DOOM_DIR = REPO / "tools" / "doom"
THIRD_PARTY = REPO / "third_party" / "doomgeneric" / "doomgeneric"
BUILD = REPO / "build" / "doom"
ELF_OUTPUT = BUILD / "doom.elf"  # ELF with symbols — addr2line uses this
MAP_OUTPUT = BUILD / "doom.map"  # ld map — function-by-function VMA layout
OUTPUT = BUILD / "doom"  # flat binary — copy to disk image as bin/doom

EXCLUDED_PLATFORM_BACKENDS = frozenset({
    "doomgeneric_allegro",
    "doomgeneric_emscripten",
    "doomgeneric_linuxvt",
    "doomgeneric_sdl",
    "doomgeneric_soso",
    "doomgeneric_sosox",
    "doomgeneric_win",
    "doomgeneric_xlib",
})
EXCLUDED_AUDIO_BACKENDS = frozenset({
    "i_allegromusic",
    "i_allegrosound",
    "i_sdlmusic",
    "i_sdlsound",
})
# doomgeneric ships an older, header-incompatible copy of these chocolate-doom
# helpers (memio.{c,h}, mus2mid.{c,h}); we use the chocolate-doom 3.1.0 versions
# fetched into third_party/chocolate-doom-opl/ instead, so the older copies
# must not also be linked or the stable symbols (mem_fread, mus2mid, ...)
# collide.
EXCLUDED_DOOMGENERIC_AUDIO_HELPERS = frozenset({
    "memio",
    "mus2mid",
})
EXCLUDED_WAD_BACKENDS = frozenset({
    # tools/doom/bboeos_wad_file.c provides a `stdc_wad_file` that
    # slurps the WAD into a malloc'd buffer and exposes it through
    # wad_file_t::mapped, so W_CacheLumpNum bypasses fread entirely.
    # Drop doomgeneric's stock W_StdC_* implementations to avoid a
    # duplicate-symbol clash with our backend at link time.
    "w_file_stdc",
})

CFLAGS = (
    "--target=i386-pc-none-elf",
    "-m32",
    "-DFEATURE_SOUND",  # turns on the doomgeneric sound_module / music_module
    # registrations in i_sound.c so DG_sound_module (defined in our
    # tools/doom/i_sound_bboeos.c backend) is picked up at link time.
    # i_sound.c also `#include <SDL_mixer.h>` under FEATURE_SOUND;
    # we satisfy that with an empty shim header in tools/doom/ (added
    # to the include path below).
    "-march=i386",  # bboeos kernel hasn't enabled SSE/SSE2/AVX in CR4 — without
    "-mno-mmx",  # this clang on macOS picks SSE for memcpy/memset and the
    "-mno-sse",  # kernel takes #UD on the first SSE op (EXC06).  Linux clang
    "-mno-sse2",  # happens not to emit SSE for the i386 target by default.
    "-mno-implicit-float",  # Apple clang ignores -mno-sse2 in some struct-init
    # / memcpy paths (vsnprintf was zeroing locals with pxor xmm3, xmm3 even
    # with -march=i386 -mno-sse2).  -mno-implicit-float forbids any implicit FP
    # or SIMD register use; explicit x87 in math.c via inline asm is unaffected.
    "-fno-vectorize",  # Apple clang's loop vectorizer also ignores -march=i386
    "-fno-slp-vectorize",  # and rewrites our zero-pad loops to xorps + movsd
    # SIMD writes — turn off both vectorisers (loop + straight-line) so it can't.
    "-ffreestanding",
    "-fno-pic",
    "-fno-stack-protector",
    "-nostdlib",
    "-nostdinc",
    "-O2",
    "-g",  # DWARF debug info → addr2line on doom.elf maps EIP back to source
    "-w",  # doomgeneric has many warnings under -Wall; suppress
    # CMAP256: pixel_t = uint8_t (palette indices), so DG_ScreenBuffer
    # ends up holding what VGA mode 13h wants directly — DG_DrawFrame is
    # then a 64000-byte memcpy.  Without this, pixel_t would be
    # uint32_t (RGBA) and we'd burn cycles palette-quantising every frame.
    "-DCMAP256",
    # Match VGA mode 13h's native resolution so the framebuffer the
    # engine renders into is exactly the size we blit.  Doom's own
    # internal SCREENWIDTH/SCREENHEIGHT are already 320x200, so
    # I_FinishUpdate's per-line memcpy lands the whole frame into
    # DG_ScreenBuffer with no scaling.
    "-DDOOMGENERIC_RESX=320",
    "-DDOOMGENERIC_RESY=200",
    f"-I{LIBC / 'include'}",
    f"-I{THIRD_PARTY}",
    f"-I{REPO / 'tools' / 'doom' / 'include'}",  # SDL_mixer.h shim
    f"-I{CHOCOLATE_OPL}",  # fetched chocolate-doom OPL stack
)


def _build_libbboeos() -> None:
    """Run make -C tools/libc to produce libbboeos.a + _start.o.

    Forwards a GNU-compatible ``AR`` so the archive's symbol index is
    in the format ld.lld / x86_64-elf-ld expects.  macOS's BSD ``ar``
    produces a ``__.SYMDEF`` index that the cross-linker can read but
    sometimes silently mis-indexes for objects with our --target,
    surfacing as undefined-reference errors at link time.
    """
    archiver = _find_tool(
        candidates=("x86_64-elf-ar", "i686-elf-ar", "llvm-ar", "ar"),
        purpose="GNU-compatible ar",
    )
    env = os.environ | {"AR": archiver}
    subprocess.check_call(["make", "-C", str(LIBC)], env=env)


def _compile_one(*, source: Path, extra_cflags: tuple[str, ...] = ()) -> Path:
    """Compile one .c → build/doom/<name>.o.  Returns the .o path.

    *extra_cflags* are appended after the global CFLAGS so per-source
    additions (e.g. ``-include`` for the chocolate-doom shim) win.
    """
    obj = BUILD / f"{source.stem}.o"
    subprocess.check_call(
        ["clang", *CFLAGS, *extra_cflags, "-c", str(source), "-o", str(obj)],
    )
    return obj


def _doomgeneric_sources() -> list[Path]:
    """Return doomgeneric .c files we want to compile (core + audio stubs).

    Excludes platform backends (we provide our own) and audio backends
    (no kernel audio yet).
    """
    excluded = EXCLUDED_PLATFORM_BACKENDS | EXCLUDED_AUDIO_BACKENDS | EXCLUDED_DOOMGENERIC_AUDIO_HELPERS | EXCLUDED_WAD_BACKENDS
    return sorted(source for source in THIRD_PARTY.glob("*.c") if source.stem not in excluded)


def _ensure_chocolate_opl_fetched() -> None:
    """Run tools/fetch_chocolate_opl.sh — itself idempotent on the pinned commit."""
    subprocess.check_call([str(REPO / "tools" / "fetch_chocolate_opl.sh")])


def _ensure_doomgeneric_fetched() -> None:
    """Run tools/fetch_doom.sh if third_party/doomgeneric is missing."""
    if THIRD_PARTY.is_dir():
        return
    subprocess.check_call([str(REPO / "tools" / "fetch_doom.sh")])


def _find_tool(*, candidates: tuple[str, ...], purpose: str) -> str:
    """Pick the first executable in *candidates* that exists on PATH.

    macOS's /usr/bin/ld is Apple's mach-o linker — it doesn't understand
    GNU options like -T (linker script) or -Map (link map) that we
    need.  The portable fix is to try a sequence of candidates: GNU
    cross-binutils first (``brew install x86_64-elf-binutils`` on
    macOS, present by default on Linux as plain ``ld``/``objcopy``),
    then ``ld.lld``/``llvm-objcopy`` from the LLVM toolchain.
    """
    for tool in candidates:
        path = shutil.which(tool)
        if path is not None:
            return tool
    message = (
        f"build_doom: cannot find a {purpose}; tried {candidates}.  "
        f"On macOS: `brew install x86_64-elf-binutils` (gives x86_64-elf-ld + x86_64-elf-objcopy) "
        f"or `brew install lld llvm` (gives ld.lld + llvm-objcopy).  "
        f"On Linux: `apt install binutils` (default) or `apt install lld llvm`."
    )
    raise SystemExit(message)


def _link(*, objects: list[Path]) -> None:
    """Link to ELF (with symbols + .map), then objcopy to flat binary.

    The flat binary is what bboeos's program loader consumes; the ELF
    sits next to it for addr2line / objdump when something faults.
    Doing the link once + objcopy is faster than two separate ld runs
    and guarantees the two artefacts share addresses byte-for-byte.
    """
    linker = _find_tool(
        candidates=("x86_64-elf-ld", "i686-elf-ld", "ld.lld", "ld"),
        purpose="GNU-compatible linker",
    )
    objcopy = _find_tool(
        candidates=("x86_64-elf-objcopy", "i686-elf-objcopy", "llvm-objcopy", "objcopy"),
        purpose="GNU-compatible objcopy",
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    for stale in (OUTPUT, ELF_OUTPUT, MAP_OUTPUT):
        if stale.exists():
            stale.unlink()
    subprocess.check_call([
        linker,
        "-m",
        "elf_i386",
        "-T",
        str(LIBC / "program.ld"),
        f"-Map={MAP_OUTPUT}",
        "-o",
        str(ELF_OUTPUT),
        str(LIBC / "_start.o"),
        *[str(obj) for obj in objects],
        str(LIBC / "libbboeos.a"),
    ])
    subprocess.check_call([
        objcopy,
        "-O",
        "binary",
        str(ELF_OUTPUT),
        str(OUTPUT),
    ])


def main() -> None:
    """CLI entry point — parse args, fetch / compile / link, print result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clean",
        action="store_true",
        help="wipe build/doom/ before compiling",
    )
    arguments = parser.parse_args()

    if arguments.clean and BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)

    _ensure_chocolate_opl_fetched()
    _ensure_doomgeneric_fetched()
    _build_libbboeos()

    objects = [_compile_one(source=source) for source in _doomgeneric_sources()]
    # Local sources: bboeos_doomgeneric.c is the DG_* backend (display,
    # input, time); audio_mixer.c + i_sound_bboeos.c provide the
    # 8-voice mixer and the sound_module_t adapter that opens
    # /dev/audio (referenced by doomgeneric/i_sound.c when built with
    # -DFEATURE_SOUND, set in CFLAGS above).
    # bboeos_wad_file.c provides a `stdc_wad_file` symbol — the default
    # backend doomgeneric falls through to without `-mmap` — that
    # slurps the entire WAD into a malloc'd buffer at OpenFile time.
    # tools/build_doom.py's EXCLUDED_WAD_BACKENDS drops doomgeneric's
    # stock W_StdC_* so this one wins at link time.
    objects.extend([
        _compile_one(source=DOOM_DIR / "bboeos_doomgeneric.c"),
        _compile_one(source=DOOM_DIR / "audio_mixer.c"),
        _compile_one(source=DOOM_DIR / "bboeos_wad_file.c"),
        _compile_one(source=DOOM_DIR / "i_sound_bboeos.c"),
        _compile_one(source=DOOM_DIR / "opl_bboeos.c"),
    ])
    # Fetched chocolate-doom OPL music stack (third_party/chocolate-doom-opl/,
    # populated by tools/fetch_chocolate_opl.sh above).  i_oplmusic registers
    # `music_opl_module`, the doomgeneric MIDI music backend referenced from
    # i_sound_bboeos.c; midifile parses Standard MIDI File chunks for
    # i_oplmusic; mus2mid converts MUS lumps to MIDI; opl_queue is the timed
    # event queue chocolate uses to schedule OPL writes; memio is chocolate's
    # in-memory file IO shim used by the MUS / MIDI parsers.  ``-include
    # tools/doom/chocolate_compat.h`` defines the ``PACKED_STRUCT`` macro
    # these sources use; doomgeneric's doomtype.h (resolved via
    # -Ithird_party/doomgeneric/...) ships ``PACKEDATTR`` but not
    # ``PACKED_STRUCT``, so the chocolate sources alone need the shim.
    chocolate_cflags = (f"-include{REPO / 'tools' / 'doom' / 'chocolate_compat.h'}",)
    objects.extend([
        _compile_one(source=CHOCOLATE_OPL / "i_oplmusic.c", extra_cflags=chocolate_cflags),
        _compile_one(source=CHOCOLATE_OPL / "memio.c", extra_cflags=chocolate_cflags),
        _compile_one(source=CHOCOLATE_OPL / "midifile.c", extra_cflags=chocolate_cflags),
        _compile_one(source=CHOCOLATE_OPL / "mus2mid.c", extra_cflags=chocolate_cflags),
        _compile_one(source=CHOCOLATE_OPL / "opl_queue.c", extra_cflags=chocolate_cflags),
    ])

    _link(objects=objects)
    size = OUTPUT.stat().st_size
    print(f"built {OUTPUT.relative_to(REPO)} ({size:,} bytes, {len(objects)} objects)")


if __name__ == "__main__":
    main()
