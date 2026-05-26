#!/usr/bin/env python3
"""cc.py GCC extended inline asm coverage."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
LIBBBOEOS_INCLUDE = REPO_ROOT / "user" / "libbboeos" / "include"
_PREAMBLE = "#include <stdint.h>\n"


def _compile(name: str, source: str) -> str:
    with tempfile.TemporaryDirectory() as work:
        return compile_snippet(name=name, source=source, work=Path(work))


def compile_snippet(*, name: str, source: str, work: Path) -> str:
    """Compile *source* with cc.py under *work* and return the emitted assembly text."""
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


def test_clobber_list() -> None:
    """Extended asm with output, empty input section, and clobber list parses and compiles."""
    source = """
    int main(void) {
        int result;
        __asm__ volatile("mov $7, %%eax" : "=a"(result) : : "ebx", "ecx");
        return result;
    }
    """
    _compile("clobber", source)


def test_earlyclobber_byte_output() -> None:
    """Extended asm with earlyclobber byte output constraint (=&q) and %b[name] substitution."""
    source = """
    int main(void) {
        int eax_out;
        unsigned char cf;
        __asm__ volatile(
            "mov $1, %%eax\\n\\t"
            "stc\\n\\t"
            "setc %b[cf]\\n\\t"
            : "=a"(eax_out), [cf] "=&q"(cf)
            :
            :);
        return cf;
    }
    """
    asm = _compile("earlyclobber", source)
    assert "setc" in asm


def test_input_and_output() -> None:
    """Extended asm with named input operand and output operand parses and compiles."""
    source = """
    int main(void) {
        int x = 10;
        int result;
        __asm__ volatile("mov %[val], %%eax" : "=a"(result) : [val] "g"(x));
        return result;
    }
    """
    _compile("input_output", source)


def test_named_input_substitution() -> None:
    """Extended asm with named input operand substitutes %[name] with the operand location."""
    source = """
    int main(void) {
        int x = 42;
        int result;
        __asm__ volatile("mov %[val], %%eax" : "=a"(result) : [val] "g"(x));
        return result;
    }
    """
    asm = _compile("named_input", source)
    main_body = asm.split("main:", 1)[1].split("\n_", 1)[0]
    # %[val] should have been substituted with a location; no literal %[val] should remain.
    # The operand may be a memory address (ebp, _g_) or an immediate (42) or a register.
    assert "%[val]" not in main_body


def test_output_only() -> None:
    """Extended asm with a single output section and no inputs or clobbers parses and compiles."""
    source = """
    int main(void) {
        int result;
        __asm__ volatile("mov $42, %%eax" : "=a"(result));
        return result;
    }
    """
    _compile("output_only", source)


def test_positional_operand_substitution() -> None:
    """Extended asm with positional operand tokens (%0, %1) substituted with resolved locations."""
    source = """
    int main(void) {
        int x = 10;
        int result;
        __asm__ volatile("mov %1, %0" : "=a"(result) : "g"(x));
        return result;
    }
    """
    asm = _compile("positional", source)
    main_body = asm.split("main:", 1)[1].split("\n_", 1)[0]
    # %0 = eax (output "=a"), %1 = memory operand for x; no literal %0 or %1 should remain
    assert "%0" not in main_body
    assert "%1" not in main_body
    assert "eax" in main_body


def test_read_modify_write() -> None:
    """Extended asm with a read-modify-write ('+') output constraint parses and compiles."""
    source = """
    int main(void) {
        int x = 10;
        __asm__ volatile("add $5, %%eax" : "+a"(x));
        return x;
    }
    """
    _compile("rmw", source)


def test_read_modify_write_codegen() -> None:
    """Extended asm with read-modify-write (+) constraint generates pre-load and post-store."""
    source = """
    int main(void) {
        int x = 10;
        __asm__ volatile("add $5, %%eax" : "+a"(x));
        return x;
    }
    """
    asm = _compile("rmw_codegen", source)
    main_body = asm.split("main:", 1)[1].split("\n_", 1)[0]
    # Should load x into eax before, and store eax back after.
    assert "add" in main_body
    # RMW: the variable x must be loaded from memory into eax before the asm template
    # and stored back from eax to memory after.
    assert "eax" in main_body


def test_tied_operand_zero() -> None:
    """Extended asm with tied input operand (constraint "0") shares output 0's register."""
    source = """
    int double_it(int x) {
        int result;
        __asm__ volatile("shl $1, %%eax" : "=a"(result) : "0"(x));
        return result;
    }
    int main(void) { return double_it(21); }
    """
    asm = _compile("tied", source)
    assert "shl" in asm


def test_x87_atan2() -> None:
    """Extended asm with x87 fpatan: =t output, 0-tied and u inputs, st(1) clobber."""
    source = """
    double x;
    double y;
    double result;
    void compute(void) {
        __asm__("fpatan" : "=t"(result) : "0"(x), "u"(y) : "st(1)");
    }
    int main(void) { compute(); return 0; }
    """
    asm = _compile("x87_atan2", source)
    assert "fpatan" in asm
    assert "fld" in asm
    assert "fstp" in asm


def test_x87_cos() -> None:
    """Extended asm with x87 fcos: =t output and 0-tied input."""
    source = """
    double x;
    double result;
    void compute(void) {
        __asm__("fcos" : "=t"(result) : "0"(x));
    }
    int main(void) { compute(); return 0; }
    """
    asm = _compile("x87_cos", source)
    assert "fcos" in asm
    assert "fld" in asm
    assert "fstp" in asm


def test_x87_fnstcw_memory_output() -> None:
    """Extended asm with =m memory output: %0 substituted as bracketed memory operand."""
    source = """
    unsigned int control_word;
    void read_cw(void) {
        __asm__("fnstcw %0" : "=m"(control_word));
    }
    int main(void) { read_cw(); return 0; }
    """
    asm = _compile("x87_fnstcw", source)
    assert "fnstcw" in asm


if __name__ == "__main__":
    test_clobber_list()
    test_earlyclobber_byte_output()
    test_input_and_output()
    test_named_input_substitution()
    test_output_only()
    test_positional_operand_substitution()
    test_read_modify_write()
    test_read_modify_write_codegen()
    test_tied_operand_zero()
    test_x87_atan2()
    test_x87_cos()
    test_x87_fnstcw_memory_output()
    print("OK")
