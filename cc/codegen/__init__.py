"""Code generator package.

The generator that consumes the IR and emits NASM assembly for a given
:class:`cc.target.CodegenTarget`.  Currently only the x86 backend
ships; future ARM / RISC-V backends would sit alongside it (``cc.codegen.arm``
etc.) and share whatever architecture-agnostic scaffolding lives in
:mod:`cc.codegen.base`.
"""

from cc.codegen.x86 import X86CodeGenerator

__all__ = ["X86CodeGenerator"]
