#!/usr/bin/env python3
"""cc.py object-file archive packer.

Reads `.ccobj` members and writes a JSON manifest enumerating each
member's globally-defined symbols.  Members are stored by basename
only; the linker resolves them as siblings of the manifest.
"""

from __future__ import annotations

import argparse
import json
import operator
import sys
from pathlib import Path

CCAR_VERSION = 1
CCOBJ_VERSION = 1


def _extract_provides(*, ccobj_path: Path) -> list[str]:
    """Return the sorted list of globally-bound symbol names in a `.ccobj`."""
    try:
        with ccobj_path.open(encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as error:
        sys.exit(f"ccar: {ccobj_path}: invalid JSON ({error})")
    if payload.get("version") != CCOBJ_VERSION:
        sys.exit(f"ccar: {ccobj_path}: unsupported .ccobj version {payload.get('version')!r} (expected {CCOBJ_VERSION})")
    provides = [name for name, info in payload.get("symbols", {}).items() if info.get("binding") == "global"]
    return sorted(provides)


def main() -> int:
    """Parse arguments and write the `.ccar` manifest."""
    parser = argparse.ArgumentParser(
        description="Pack .ccobj files into a .ccar archive manifest.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Output path for the .ccar manifest.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="One or more .ccobj files (must live in the same directory as --output).",
    )
    arguments = parser.parse_args()

    output_directory = arguments.output.parent.resolve()
    members: list[dict] = []
    for input_path in arguments.inputs:
        if input_path.parent.resolve() != output_directory:
            sys.exit(f"ccar: {input_path}: input must live in the same directory as --output ({output_directory})")
        members.append({
            "file": input_path.name,
            "provides": _extract_provides(ccobj_path=input_path),
        })

    manifest = {
        "members": sorted(members, key=operator.itemgetter("file")),
        "version": CCAR_VERSION,
    }
    with arguments.output.open("w", encoding="utf-8") as file:
        json.dump(manifest, file, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
