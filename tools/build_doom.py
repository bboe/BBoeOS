#!/usr/bin/env python3
"""Build bin/doom — a flat-binary BBoeOS userland Doom port.

Pipeline:
  1. fetch doomgeneric source (third_party/doomgeneric, GPLv2) if missing
  2. build libbboeos.a (tools/libc/Makefile)
  3. compile each non-platform-backend doomgeneric .c file
  4. compile our bboeos backend (tools/doom/bboeos_doomgeneric.c)
  5. link everything plus tools/libc/_start.o through tools/libc/program.ld
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
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIBC = REPO / "tools" / "libc"
DOOM_DIR = REPO / "tools" / "doom"
THIRD_PARTY = REPO / "third_party" / "doomgeneric" / "doomgeneric"
BUILD = REPO / "build" / "doom"
OUTPUT = BUILD / "doom"  # flat binary; copy to disk image as bin/doom

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

CFLAGS = (
    "--target=i386-pc-none-elf",
    "-m32",
    "-ffreestanding",
    "-fno-pic",
    "-fno-stack-protector",
    "-nostdlib",
    "-nostdinc",
    "-O2",
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
)


def _build_libbboeos() -> None:
    """Run make -C tools/libc to produce libbboeos.a + _start.o."""
    subprocess.check_call(["make", "-C", str(LIBC)])


def _compile_one(*, source: Path) -> Path:
    """Compile one .c → build/doom/<name>.o.  Returns the .o path."""
    obj = BUILD / f"{source.stem}.o"
    subprocess.check_call(
        ["clang", *CFLAGS, "-c", str(source), "-o", str(obj)],
    )
    return obj


def _doomgeneric_sources() -> list[Path]:
    """Return doomgeneric .c files we want to compile (core + audio stubs).

    Excludes platform backends (we provide our own) and audio backends
    (no kernel audio yet).
    """
    excluded = EXCLUDED_PLATFORM_BACKENDS | EXCLUDED_AUDIO_BACKENDS
    return sorted(source for source in THIRD_PARTY.glob("*.c") if source.stem not in excluded)


def _ensure_doomgeneric_fetched() -> None:
    """Run tools/fetch_doom.sh if third_party/doomgeneric is missing."""
    if THIRD_PARTY.is_dir():
        return
    subprocess.check_call([str(REPO / "tools" / "fetch_doom.sh")])


def _link(*, objects: list[Path]) -> None:
    """Ld the objects + libbboeos.a → flat binary at bin/doom."""
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists():
        OUTPUT.unlink()
    subprocess.check_call([
        "ld",
        "-m",
        "elf_i386",
        "-T",
        str(LIBC / "program.ld"),
        "--oformat",
        "binary",
        "-o",
        str(OUTPUT),
        str(LIBC / "_start.o"),
        *[str(obj) for obj in objects],
        str(LIBC / "libbboeos.a"),
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

    _ensure_doomgeneric_fetched()
    _build_libbboeos()

    objects = [_compile_one(source=source) for source in _doomgeneric_sources()]
    objects.append(_compile_one(source=DOOM_DIR / "bboeos_doomgeneric.c"))

    _link(objects=objects)
    size = OUTPUT.stat().st_size
    print(f"built {OUTPUT.relative_to(REPO)} ({size:,} bytes, {len(objects)} objects)")


if __name__ == "__main__":
    main()
