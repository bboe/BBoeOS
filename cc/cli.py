"""Command-line entry point: preprocess → tokenize → parse → codegen."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cc.codegen import X86CodeGenerator
from cc.errors import CompileError
from cc.lexer import tokenize
from cc.parser import Parser
from cc.preprocessor import apply_defines, preprocess


def main() -> int:
    """Compile a C source file to NASM assembly.

    Returns:
        Exit code (0 for success, 1 for usage or compilation error).

    """
    parser = argparse.ArgumentParser(description="Compile a C source file to NASM.")
    parser.add_argument("input", help="input .c file")
    parser.add_argument("output", help="output .asm file (default stdout)", nargs="?")
    parser.add_argument(
        "--bits",
        choices=(16, 32),
        default=16,
        help="target CPU mode for emitted assembly (default 16)",
        type=int,
    )
    arguments = parser.parse_args()

    try:
        source = Path(arguments.input).read_text(encoding="utf-8")
        source, defines = preprocess(source, include_base=Path(arguments.input).parent)
        tokens = tokenize(source)
        tokens = apply_defines(defines=defines, tokens=tokens)
        ast = Parser(tokens).parse_program()
        output = X86CodeGenerator(bits=arguments.bits, defines=defines).generate(ast)
    except CompileError as error:
        location = f"{arguments.input}:{error.line}" if error.line else arguments.input
        print(f"{location}: error: {error.message}", file=sys.stderr)
        return 1

    if arguments.output is not None:
        Path(arguments.output).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0
