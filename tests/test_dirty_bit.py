#!/usr/bin/env python3
"""Open-WRONLY-no-write must not clobber an existing file's size.

Pre-fix: ``fd_close`` for a writable FILE fd unconditionally calls
``vfs_update_size``, which writes ``position`` (0 for an unwritten fd)
as the new size -- destroying the file.  After A3+A4 the close only
flushes when ``entry->dirty`` is set.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402

from add_file import (  # noqa: E402
    NAME_FIELD,
    OFFSET_SIZE,
    SECTOR_SIZE,
    add_file,
    compute_directory_sector,
    iter_entries,
    load_image,
)

_DIRECTORY_SECTORS = 3
_PROBE_NAME = "dirty_probe"
_PROBE_BODY = b"hello-dirty\n"


def _read_size_from_image(*, image_path: Path, name: str) -> int:
    image = load_image(str(image_path))
    directory_sector = compute_directory_sector(image_path=str(image_path))
    base_offset = directory_sector * SECTOR_SIZE
    name_bytes = name.encode()
    for entry_offset in iter_entries(base_offset=base_offset, sector_count=_DIRECTORY_SECTORS):
        if image[entry_offset] == 0:
            continue
        entry_name = bytes(image[entry_offset : entry_offset + NAME_FIELD]).rstrip(b"\x00")
        if entry_name != name_bytes:
            continue
        return struct.unpack_from("<I", image, entry_offset + OFFSET_SIZE)[0]
    message = f"file {name!r} not found in image"
    raise AssertionError(message)


def test_open_wronly_no_write_preserves_size() -> None:
    """Assert that open-WRONLY-then-close without any write preserves the existing file size."""
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    image = REPO_ROOT / "drive.img"
    with tempfile.TemporaryDirectory() as temporary_directory:
        probe_path = Path(temporary_directory) / _PROBE_NAME
        probe_path.write_bytes(_PROBE_BODY)
        add_file(
            executable=False,
            file_path=str(probe_path),
            image_path=str(image),
            subdirectory=None,
        )

    pre_size = _read_size_from_image(image_path=image, name=_PROBE_NAME)
    assert pre_size == len(_PROBE_BODY), f"setup: pre-size {pre_size} != {len(_PROBE_BODY)}"

    with qemu_session(monitor=False, snapshot=False) as session:
        session.send_command(f"noop_writer {_PROBE_NAME}")

    post_size = _read_size_from_image(image_path=image, name=_PROBE_NAME)
    assert post_size == pre_size, f"open-without-write must preserve size: pre={pre_size}, post={post_size}"
    print("PASS: test_open_wronly_no_write_preserves_size")


def main() -> int:
    """Build the OS image and run the dirty-bit regression test."""
    test_open_wronly_no_write_preserves_size()
    print("1 passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
