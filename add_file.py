#!/usr/bin/env python3
"""Add a file to a BBoeOS drive image."""

from __future__ import annotations

import argparse
import os
import re
import struct

ENTRY_SIZE = 32
FLAG_EXEC = 0x01
FNAME_MAX = 26
MAX_ENTRIES = 32
NAME_FIELD = 27
OFF_FLAGS = 27
OFF_SECTOR = 28
OFF_SIZE = 30
SECTOR_SIZE = 512

CONSTANTS_PATH = "src/include/constants.asm"


def add_file(*, executable: bool, file_path: str, image_path: str) -> None:
    filename = os.path.basename(file_path)
    if len(filename) > FNAME_MAX:
        raise SystemExit(
            f"Error: filename '{filename}' exceeds {FNAME_MAX} characters"
        )

    with open(file_path, "rb") as f:
        file_data = f.read()
    if not file_data:
        raise SystemExit("Error: file is empty")
    file_size = len(file_data)

    dir_sector = read_assign("DIR_SECTOR")
    dir_sectors = read_assign("DIR_SECTORS")
    dir_offset = (dir_sector - 1) * SECTOR_SIZE

    with open(image_path, "rb") as f:
        image = bytearray(f.read())

    next_entry = -1
    next_data_sector = dir_sector + dir_sectors
    for i in range(MAX_ENTRIES):
        entry_off = dir_offset + i * ENTRY_SIZE
        if image[entry_off] == 0:
            next_entry = i
            break
        name = bytes(image[entry_off:entry_off + NAME_FIELD]).rstrip(b"\x00").decode()
        if name == filename:
            raise SystemExit(f"Error: file '{filename}' already exists")
        start_sec = struct.unpack_from("<H", image, entry_off + OFF_SECTOR)[0]
        size_bytes = struct.unpack_from("<H", image, entry_off + OFF_SIZE)[0]
        sectors_used = (size_bytes + SECTOR_SIZE - 1) // SECTOR_SIZE
        end_sector = start_sec + sectors_used
        if end_sector > next_data_sector:
            next_data_sector = end_sector

    if next_entry < 0:
        raise SystemExit("Error: directory full")

    entry_off = dir_offset + next_entry * ENTRY_SIZE
    name_bytes = filename.encode().ljust(NAME_FIELD, b"\x00")
    image[entry_off:entry_off + NAME_FIELD] = name_bytes
    image[entry_off + OFF_FLAGS] = FLAG_EXEC if executable else 0
    struct.pack_into("<H", image, entry_off + OFF_SECTOR, next_data_sector)
    struct.pack_into("<H", image, entry_off + OFF_SIZE, file_size)

    data_off = (next_data_sector - 1) * SECTOR_SIZE
    if data_off + file_size > len(image):
        raise SystemExit(
            f"Error: file would extend past end of image (need {data_off + file_size} bytes)"
        )
    image[data_off:data_off + file_size] = file_data

    with open(image_path, "wb") as f:
        f.write(image)

    print(
        f"Added '{filename}' ({file_size} bytes) at sector {next_data_sector}, "
        f"entry {next_entry}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add a file to a BBoeOS drive image."
    )
    parser.add_argument(
        "-x",
        "--executable",
        action="store_true",
        help="mark the file as executable (sets FLAG_EXEC)",
    )
    parser.add_argument("file", help="path to the file to add")
    parser.add_argument(
        "--image",
        default="drive.img",
        help="path to the drive image (default: drive.img)",
    )
    args = parser.parse_args()
    add_file(executable=args.executable, file_path=args.file, image_path=args.image)


def read_assign(name: str) -> int:
    """Return the integer value of a `%assign NAME VALUE` line in constants.asm."""
    pattern = re.compile(rf"^\s*%assign\s+{re.escape(name)}\s+(\S+)")
    with open(CONSTANTS_PATH) as f:
        for line in f:
            m = pattern.match(line)
            if m:
                return int(m.group(1), 0)
    raise SystemExit(f"Error: {name} not found in {CONSTANTS_PATH}")


if __name__ == "__main__":
    main()
