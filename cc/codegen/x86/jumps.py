"""x86 jump-mnemonic tables.

Kept in a standalone module so ``generator.py`` (which consumes
``JUMP_WHEN_FALSE`` / ``JUMP_WHEN_TRUE`` from ``emit_comparison``)
and ``peephole.py`` (which consumes ``JUMP_INVERT`` from the
double-jump collapse pass) can import the same tables without
pulling each other in, which would cycle.
"""

from __future__ import annotations

CMOV_WHEN_FALSE = {
    "!=": "cmove",
    "<": "cmovge",
    "<=": "cmovg",
    "==": "cmovne",
    ">": "cmovle",
    ">=": "cmovl",
}

CMOV_WHEN_TRUE = {
    "!=": "cmovne",
    "<": "cmovl",
    "<=": "cmovle",
    "==": "cmove",
    ">": "cmovg",
    ">=": "cmovge",
}

JUMP_INVERT = {
    "ja": "jbe",
    "jae": "jb",
    "jb": "jae",
    "jbe": "ja",
    "je": "jne",
    "jg": "jle",
    "jge": "jl",
    "jl": "jge",
    "jle": "jg",
    "jne": "je",
}

JUMP_WHEN_FALSE = {
    "!=": "je",
    "<": "jge",
    "<=": "jg",
    "==": "jne",
    ">": "jle",
    ">=": "jl",
    # Pseudo-operators for ``carry_return`` call conditions.  CF clear
    # means the call reported ``return 1`` (true); CF set means
    # ``return 0`` (false).  ``if (foo())`` dispatches through
    # ``carry`` (jump-false = ``jc``); ``if (foo() == 0)`` through
    # ``not_carry`` (jump-false = ``jnc``).  No real ``cmp`` runs —
    # the ``call`` itself leaves CF holding the result.
    "carry": "jc",
    "not_carry": "jnc",
}

# Unsigned variants — used when either comparison operand has an unsigned
# type (``unsigned char`` / ``unsigned short`` / ``unsigned int`` / ``unsigned long`` or a
# pointer to one of those).  ``jb`` / ``jae`` / ``jbe`` / ``ja`` test
# CF / ZF rather than SF / OF, so byte values 0x80-0xFF compare correctly
# instead of being treated as signed-negative.  ``==`` / ``!=`` and the
# carry pseudo-operators don't depend on signedness so they're identical
# to the signed tables.
JUMP_WHEN_FALSE_UNSIGNED = {
    "!=": "je",
    "<": "jae",
    "<=": "ja",
    "==": "jne",
    ">": "jbe",
    ">=": "jb",
    "carry": "jc",
    "not_carry": "jnc",
}

JUMP_WHEN_TRUE = {
    "!=": "jne",
    "<": "jl",
    "<=": "jle",
    "==": "je",
    ">": "jg",
    ">=": "jge",
    "carry": "jnc",
    "not_carry": "jc",
}

JUMP_WHEN_TRUE_UNSIGNED = {
    "!=": "jne",
    "<": "jb",
    "<=": "jbe",
    "==": "je",
    ">": "ja",
    ">=": "jae",
    "carry": "jnc",
    "not_carry": "jc",
}
