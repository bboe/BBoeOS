"""x86 backend for the cc.py code generator.

Entry point :class:`X86CodeGenerator` composes the arch-agnostic
scaffolding (:class:`cc.codegen.base.CodeGeneratorBase`) with the
x86-specific mixins (builtins, peephole, statements, expressions,
IR lowering, …) that live in sibling modules.
"""

from cc.codegen.x86.generator import X86CodeGenerator

__all__ = ["X86CodeGenerator"]
