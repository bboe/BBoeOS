#!/usr/bin/env python3
"""cc.py array-of-function-pointer coverage."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

_PREAMBLE = "#include <stdint.h>\n"
REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
LIBBBOEOS_INCLUDE = REPO_ROOT / "user" / "libbboeos" / "include"


def _compile(name: str, source: str) -> str:
    with tempfile.TemporaryDirectory() as work:
        return compile_snippet(name=name, source=source, work=Path(work))


def compile_snippet(*, name: str, source: str, work: Path) -> str:
    """Compile *source* via cc.py in *work* and return the emitted assembly."""
    source_path = work / f"{name}.c"
    asm_path = work / f"{name}.asm"
    source_path.write_text(_PREAMBLE + source)
    result = subprocess.run(
        ["python3", str(CC), "--bits", "32", "-I", str(LIBBBOEOS_INCLUDE), str(source_path), str(asm_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        message = f"cc.py failed:\n{result.stderr}"
        raise RuntimeError(message)
    return asm_path.read_text()


def test_atexit_pattern() -> None:
    """End-to-end atexit pattern: declare, store, call-through in a loop."""
    source = """
    int call_count;
    void inc(void) { call_count = call_count + 1; }
    void dec(void) { call_count = call_count - 1; }
    static void (*fns[8])(void);
    int count;
    int main(void) {
        fns[0] = inc;
        fns[1] = inc;
        fns[2] = dec;
        count = 3;
        while (count > 0) {
            count = count - 1;
            fns[count]();
        }
        return call_count;
    }
    """
    _compile("atexit_pattern", source)


def test_call_through_index() -> None:
    """Call through a constant-indexed function-pointer array element."""
    source = """
    int result;
    void set_result(void) { result = 42; }
    static void (*handlers[4])(void);
    int main(void) {
        handlers[0] = set_result;
        handlers[0]();
        return result;
    }
    """
    _compile("call_through", source)


def test_call_through_index_with_args() -> None:
    """Call through a function-pointer array element passing arguments."""
    source = """
    int result;
    void add_to_result(int x) { result = result + x; }
    static void (*handlers[4])(int);
    int main(void) {
        handlers[0] = add_to_result;
        handlers[0](7);
        return result;
    }
    """
    _compile("call_args", source)


def test_file_scope_initialized() -> None:
    """File-scope array-of-function-pointer with brace initializer compiles."""
    _compile(
        "fscope_init",
        "void f1(void) {} void f2(void) {}\nstatic void (*handlers[2])(void) = { f1, f2 };\nint main(void) { return 0; }",
    )


def test_file_scope_uninitialized() -> None:
    """File-scope array-of-function-pointer without initializer compiles."""
    _compile(
        "fscope_uninit",
        "static void (*handlers[8])(void);\nint main(void) { return 0; }",
    )


def test_local_scope() -> None:
    """Local array-of-function-pointer declaration and indexed store compiles."""
    _compile(
        "local",
        "void f1(void) {}\nint main(void) { void (*arr[4])(void); arr[0] = f1; return 0; }",
    )


def test_store_indexed() -> None:
    """Store a function pointer into a file-scope indexed array element."""
    _compile(
        "store",
        "void handler(void) {}\nstatic void (*handlers[4])(void);\nint main(void) { handlers[0] = handler; return 0; }",
    )


def test_typedef_array() -> None:
    """Typedef alias for a function-pointer type used as array element type."""
    _compile(
        "typedef_arr",
        "typedef void (*handler_t)(void);\nstatic handler_t handlers[4];\nvoid f(void) {}\nint main(void) { handlers[0] = f; return 0; }",
    )


if __name__ == "__main__":
    test_atexit_pattern()
    test_call_through_index()
    test_call_through_index_with_args()
    test_file_scope_initialized()
    test_file_scope_uninitialized()
    test_local_scope()
    test_store_indexed()
    test_typedef_array()
    print("OK")
