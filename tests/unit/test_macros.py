"""Saturation / underflow behaviour of MAX with unsigned operands.

Standard C integer-promotion rules apply: ``uint8_t`` (and any other
unsigned type narrower than ``int``) promotes to ``int`` in arithmetic
contexts, so ``MAX(x - 1, 0)`` where ``x`` is ``uint8_t`` correctly
saturates at 0 — the subtraction happens in signed int and the
comparison uses signed ``jg``.  But ``uint32_t`` does NOT promote
(it's the same width as ``int``); the usual arithmetic conversions
make the comparison unsigned, ``0 - 1 == 0xFFFFFFFF``, and
``MAX(0xFFFFFFFF, 0) == 0xFFFFFFFF`` — the saturation breaks
silently.

These tests pin both behaviours in cc.py's generated asm so a future
codegen change that "fixes" unsigned types to use signed compares (or
breaks ``uint8_t`` promotion) is caught immediately.

The documented workaround for callers that need saturation on a
type ≥ ``int`` width: cast to signed first — ``MAX((int)x - 1, 0)``.

Run with: ``pytest tests/unit/test_macros.py``
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CC = REPO_ROOT / "cc.py"
sys.path.insert(0, str(REPO_ROOT))


def _compile_32bit(source_text: str, /) -> str:
    """Run cc.py --bits 32 on *source_text*; return the generated asm."""
    text = textwrap.dedent(source_text)
    with tempfile.NamedTemporaryFile(
        suffix=".c",
        prefix="_test_macros_",
        dir=str(REPO_ROOT / "user" / "programs"),
        mode="w",
        encoding="utf-8",
        delete=True,
    ) as src_file:
        src_file.write(text)
        src_file.flush()
        result = subprocess.run(
            ["python3", str(CC), "--bits", "32", src_file.name],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_max_via_signed_int_local_saturates() -> None:
    """Copying through an ``int`` local restores saturation on uint32_t.

    cc.py treats C-style integer casts as no-ops — ``(int)g_count``
    does NOT change the operand's signedness at the comparison site.
    The documented workaround for callers who need saturation on a
    type ≥ ``int`` width is to copy through a signed ``int`` local:

        int signed_count = g_count;          // force int type
        int r = MAX(signed_count - 1, 0);    // signed compare → jg

    This test pins that workaround.  The generated asm uses ``jg``
    (signed greater) because the local's declared type is ``int``.
    """
    asm = _compile_32bit("""
        #include "macros.h"

        uint32_t g_count;

        int main(int argc, char *argv[]) {
            g_count = (uint32_t)argc;
            int signed_count = g_count;
            int r = MAX(signed_count - 1, 0);
            return r;
        }
    """)
    assert "jg ." in asm, f"expected signed `jg` branch after `int` local copy; got:\n{asm}"


def test_max_with_uint32_underflows_via_unsigned_compare() -> None:
    """``MAX(uint32_t_var - 1, 0)`` uses ``ja`` (unsigned compare) — BROKEN.

    Standard C integer-promotion rules: ``uint32_t`` is the same width
    as ``int``, so the usual arithmetic conversions promote the ``0``
    literal to ``unsigned int`` (not the other way around).  The
    comparison is then unsigned: ``0u - 1u == 0xFFFFFFFF``, and
    ``0xFFFFFFFF > 0u`` is true, so MAX returns 0xFFFFFFFF instead of
    saturating at 0.

    The generated asm pattern is:
        mov  eax, [global]   ; uint32_t into eax
        dec  eax              ; arithmetic: 0 - 1 = 0xFFFFFFFF
        test eax, eax
        ja   .cond_end_*      ; unsigned above — taken for 0xFFFFFFFF
        xor  eax, eax         ; only reached when eax was 0+1=1...

    This test pins the broken behaviour as documented in
    ``kernel/include/macros.h``: callers who need saturation on a type
    ≥ ``int`` width must cast to signed first — ``MAX((int)x - 1, 0)``.
    """
    asm = _compile_32bit("""
        #include "macros.h"

        uint32_t g_count;

        int main(int argc, char *argv[]) {
            g_count = (uint32_t)argc;
            int r = MAX(g_count - 1, 0);
            return r;
        }
    """)
    # Pin the documented C-standard hazard: cc.py follows the standard
    # and emits an unsigned branch, which is what causes ``MAX(0u - 1, 0)``
    # to return 0xFFFFFFFF instead of 0.  A codegen change to use signed
    # branches for unsigned operands would break C compatibility with
    # clang/gcc; if such a change ever lands it would also break this
    # test and prompt a docs update in macros.h.
    assert "ja ." in asm, f"expected unsigned `ja` branch in MAX(uint32_t - 1, 0); got:\n{asm}"


def test_max_with_uint8_saturates_via_signed_compare() -> None:
    """``MAX(uint8_t_var - 1, 0)`` uses ``jg`` (signed compare).

    Standard C promotes ``uint8_t`` to ``int`` for arithmetic, so
    ``0 - 1`` evaluates as ``-1`` and ``MAX(-1, 0) == 0`` saturates
    correctly.  The generated asm pattern is:
        movzx eax, byte [...]   ; zero-extend uint8_t to int
        dec eax                  ; signed int math: 0 - 1 = -1
        test eax, eax
        jg  .cond_end_*          ; signed greater-than
        xor eax, eax             ; saturate to 0
    """
    asm = _compile_32bit("""
        #include "macros.h"

        struct holder {
            uint8_t count;
        };
        struct holder g_holder;

        int main(int argc, char *argv[]) {
            g_holder.count = (uint8_t)argc;
            int r = MAX(g_holder.count - 1, 0);
            return r;
        }
    """)
    # The body must contain a signed compare branch.  A regression that
    # emits ``ja`` (unsigned) here would silently break the saturation
    # because ``0xFF > 0`` is true unsigned but ``-1 > 0`` is false
    # signed.
    assert "jg ." in asm, f"expected signed `jg` branch in MAX(uint8_t - 1, 0); got:\n{asm}"
    assert "ja ." not in asm or "jge ." in asm.replace("jg .", ""), f"unexpected unsigned `ja` branch in MAX(uint8_t - 1, 0); got:\n{asm}"
