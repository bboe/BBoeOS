"""Compiler configuration knobs, bundled into one object to pass around.

``CompilerOptions`` carries the small set of stable, user-facing *knobs*
that flow together from the CLI through the translate pipeline into the
code generator — the target CPU width, output mode, and the
house-style-relaxation flags.  Bundling them means a new knob is one
dataclass field plus one argparse line, not a new parameter on every
signature in the chain.

Deliberately excluded: per-invocation *data* such as ``defines``,
``constant_values``, include search paths, and input/output paths.
Those are inputs to a single compile, not configuration, and keeping
them out of this object stops it from drifting into a god-object that
means "everything the compiler touches".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompilerOptions:
    """Immutable bundle of compiler knobs shared across components.

    ``bits`` selects the 16- or 32-bit target.  ``object_mode`` emits
    object-file-friendly NASM (sections + CCREL_* markers).
    ``per_function_sections`` puts each function in its own
    ``.text.<name>`` section.  ``permissive`` relaxes the bboeos
    house-style comparison strictness so unmodified third-party C
    compiles (integer ``0`` as a null-pointer constant, ``if (p)`` /
    ``c != 0`` accepted).  ``target_mode`` is ``"user"`` (stand-alone
    program) or ``"kernel"`` (bare assembly for ``%include``).
    """

    bits: int = 32
    object_mode: bool = False
    per_function_sections: bool = False
    permissive: bool = False
    target_mode: str = "user"
