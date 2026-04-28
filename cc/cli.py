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
from cc.utils import parse_asm_constants


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
        default=32,
        help="target CPU mode for emitted assembly (default 32)",
        type=int,
    )
    parser.add_argument(
        "--target",
        choices=("user", "kernel"),
        default="user",
        help=(
            "linkage target: 'user' (default) emits a stand-alone user program;"
            " 'kernel' emits bare assembly suitable for %%include into the kernel blob"
        ),
    )
    arguments = parser.parse_args()

    try:
        input_path = Path(arguments.input)
        source = input_path.read_text(encoding="utf-8")
        source, defines = preprocess(source, include_base=input_path.parent)
        tokens = tokenize(source)
        tokens = apply_defines(defines=defines, tokens=tokens)
        ast = Parser(tokens).parse_program()
        # Discover constants.asm alongside the source's include sibling directory
        # (src/c/foo.c → src/include/constants.asm) and parse %assign values so
        # the generator can evaluate local array sizes at compile time.
        constants_asm = input_path.parent.parent / "include" / "constants.asm"
        constant_values = parse_asm_constants(constants_asm) if constants_asm.is_file() else {}
        output = X86CodeGenerator(
            bits=arguments.bits,
            constant_values=constant_values,
            defines=defines,
            target_mode=arguments.target,
        ).generate(ast)
    except CompileError as error:
        location = f"{arguments.input}:{error.line}" if error.line else arguments.input
        print(f"{location}: error: {error.message}", file=sys.stderr)
        return 1

    if arguments.output is not None:
        Path(arguments.output).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0
