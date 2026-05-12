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
        # Walk up from the source's directory looking for a sibling ``include/``
        # directory (the canonical home of constants.asm and shared C headers).
        # Found path is added to the preprocessor search list so any source in
        # the tree can ``#include "program_state.h"`` etc. without a relative
        # path; constants.asm lookup uses the same discovery logic.
        include_dir = None
        cursor = input_path.parent.resolve()
        while True:
            candidate = cursor / "include"
            if candidate.is_dir():
                include_dir = candidate
                break
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        search_paths: tuple[Path, ...] = (include_dir,) if include_dir is not None else ()
        source, defines, function_defines = preprocess(
            source,
            include_base=input_path.parent,
            search_paths=search_paths,
        )
        tokens = tokenize(source)
        tokens = apply_defines(defines=defines, function_defines=function_defines, tokens=tokens)
        ast = Parser(tokens).parse_program()
        constants_asm = include_dir / "constants.asm" if include_dir is not None else None
        constant_values = parse_asm_constants(constants_asm) if constants_asm is not None and constants_asm.is_file() else {}
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
