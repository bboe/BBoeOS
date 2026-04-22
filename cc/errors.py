"""Compiler error type shared across every phase."""

from __future__ import annotations


class CompileError(Exception):
    """Raised for user-visible compilation errors.

    The optional ``line`` attribute lets :func:`main` format the
    diagnostic with a source line number without a Python traceback.
    """

    def __init__(self, message: str, /, *, line: int | None = None) -> None:
        """Store the message and optional line number."""
        self.message = message
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)
