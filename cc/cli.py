"""Command-line entry point: preprocess → tokenize → parse → codegen."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cc.ccobj import pack_ccobj
from cc.codegen import X86CodeGenerator
from cc.errors import CompileError
from cc.lexer import tokenize
from cc.parser import Parser
from cc.preprocessor import apply_defines, preprocess
from cc.utils import parse_asm_constants

SUBCOMMANDS = ("compile", "pack-ccobj")


def _compile(*, bits: int, input_path: Path, object_mode: bool, output_path: Path | None, target_mode: str) -> int:
    """Translate a C source file to NASM assembly.

    Output is written to ``output_path``, or to stdout when None.
    """
    try:
        source = input_path.read_text(encoding="utf-8")
        # Walk up from the source's directory collecting include dirs from
        # both sides of the tree.  The kernel side holds ``constants.asm`` +
        # kernel-only headers in ``kernel/include/``; the user side holds
        # shared C headers in ``user/libbboeos/include/``.  A user program
        # needs both — its own libbboeos headers plus the kernel's
        # constants.asm — so we don't break on first hit; we ascend to the
        # repo root, picking up every relevant ``include/`` along the way.
        kernel_includes: list[Path] = []
        user_includes: list[Path] = []
        cursor = input_path.parent.resolve()
        while True:
            for candidate in (cursor / "include", cursor / "kernel" / "include"):
                if candidate.is_dir() and candidate not in kernel_includes:
                    kernel_includes.append(candidate)
            for candidate in (cursor / "libbboeos" / "include", cursor / "user" / "libbboeos" / "include"):
                if candidate.is_dir() and candidate not in user_includes:
                    user_includes.append(candidate)
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        # Kernel includes win on collision (they're the legacy inline-impl
        # location); libbboeos prototype headers are the supplement.
        search_paths: tuple[Path, ...] = (*kernel_includes, *user_includes)
        source, defines, function_defines = preprocess(
            source,
            bits=bits,
            include_base=input_path.parent,
            search_paths=search_paths,
        )
        tokens = tokenize(source)
        tokens = apply_defines(defines=defines, function_defines=function_defines, tokens=tokens)
        ast = Parser(tokens, bits=bits).parse_program()
        constants_asm = next(
            (path / "constants.asm" for path in search_paths if (path / "constants.asm").is_file()),
            None,
        )
        constant_values = parse_asm_constants(constants_asm) if constants_asm is not None else {}
        output = X86CodeGenerator(
            bits=bits,
            constant_values=constant_values,
            defines=defines,
            object_mode=object_mode,
            target_mode=target_mode,
        ).generate(ast)
    except CompileError as error:
        location = f"{input_path}:{error.line}" if error.line else str(input_path)
        print(f"{location}: error: {error.message}", file=sys.stderr)
        return 1

    if output_path is not None:
        output_path.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


def main() -> int:
    """Compile a C source file to NASM, or package a NASM .bin + .lst pair.

    ``cc.py compile <args>`` is the default subcommand and is inferred
    when no subcommand verb appears in argv, preserving the legacy
    ``cc.py <input.c> [<output.asm>]`` invocation.

    Returns:
        Exit code (0 for success, 1 for usage or compilation error).

    """
    parser = argparse.ArgumentParser(
        description=("Compile a C source file to NASM, or package a NASM .bin + .lst pair into a .ccobj JSON object file."),
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")

    compile_parser = subparsers.add_parser(
        "compile",
        description="Compile a C source file to NASM (default subcommand).",
        help="compile a C source file to NASM (default if no subcommand given)",
    )
    compile_parser.add_argument("input", help="input .c file")
    compile_parser.add_argument("output", help="output .asm file (default stdout)", nargs="?")
    compile_parser.add_argument(
        "--bits",
        choices=(16, 32),
        default=32,
        help="target CPU mode for emitted assembly (default 32)",
        type=int,
    )
    compile_parser.add_argument(
        "--object",
        action="store_true",
        help=(
            "emit object-mode NASM (section directives, CCREL_* relocation markers,"
            " no flat-binary org or BSS trailer); produced .asm is intended to be"
            " assembled with `nasm -f bin -l file.lst` and packaged via `pack-ccobj`"
        ),
    )
    compile_parser.add_argument(
        "--target",
        choices=("user", "kernel"),
        default="user",
        help=(
            "linkage target: 'user' (default) emits a stand-alone user program;"
            " 'kernel' emits bare assembly suitable for %%include into the kernel blob"
        ),
    )

    pack_parser = subparsers.add_parser(
        "pack-ccobj",
        description="Package a NASM .bin + .lst into a .ccobj JSON.",
        help="package a NASM .bin + .lst into a .ccobj JSON",
    )
    pack_parser.add_argument("bin", help="NASM-produced flat .bin file")
    pack_parser.add_argument("lst", help="NASM-produced .lst listing file")
    pack_parser.add_argument("output", help="output .ccobj path")

    # Sniff: if no subcommand verb is present in argv, default to
    # ``compile``.  Preserves the legacy ``cc.py <input.c> [<output.asm>]``
    # invocation used across make_os.sh and the older test suites.
    arguments_list = sys.argv[1:]
    if arguments_list and not any(arg in SUBCOMMANDS for arg in arguments_list):
        arguments_list = ["compile", *arguments_list]
    arguments = parser.parse_args(arguments_list)

    if arguments.subcommand == "pack-ccobj":
        pack_ccobj(
            bin_path=Path(arguments.bin),
            lst_path=Path(arguments.lst),
            output_path=Path(arguments.output),
        )
        return 0

    return _compile(
        bits=arguments.bits,
        input_path=Path(arguments.input),
        object_mode=arguments.object,
        output_path=Path(arguments.output) if arguments.output is not None else None,
        target_mode=arguments.target,
    )
