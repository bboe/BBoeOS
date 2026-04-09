#!/usr/bin/env python3
"""Add a file to a BBoeOS drive image."""

from __future__ import annotations

import argparse
import os
import re
import struct

ENTRIES_PER_SECTOR = 16
ENTRY_SIZE = 32
FLAG_DIR = 0x02
FLAG_EXEC = 0x01
FNAME_MAX = 24
NAME_FIELD = 25
OFF_FLAGS = 25
OFF_SECTOR = 26
OFF_SIZE = 28          # 4-byte (32-bit) file size
SECTOR_SIZE = 512

CONSTANTS_PATH = "src/include/constants.asm"


def add_file(
    *,
    executable: bool,
    file_path: str,
    image_path: str,
    subdir: str | None,
) -> None:
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
    image = load_image(image_path)

    if subdir is None:
        parent_offset = (dir_sector - 1) * SECTOR_SIZE
    else:
        sub_entry_off = find_subdir_entry(image, dir_sector, dir_sectors, subdir)
        if sub_entry_off is None:
            raise SystemExit(f"Error: directory '{subdir}' not found")
        parent_start = struct.unpack_from("<H", image, sub_entry_off + OFF_SECTOR)[0]
        parent_offset = (parent_start - 1) * SECTOR_SIZE

    entry_off = find_free_entry(image, parent_offset, dir_sectors, filename)
    if entry_off is None:
        location = subdir if subdir else "root directory"
        raise SystemExit(f"Error: '{location}' is full")

    next_data_sector = compute_next_data_sector(image, dir_sector, dir_sectors)

    flags = FLAG_EXEC if executable else 0
    write_entry(image, entry_off, filename, flags, next_data_sector, file_size)
    write_data(image, next_data_sector, file_data)
    save_image(image_path, image)

    rel = f"{subdir}/{filename}" if subdir else filename
    print(f"Added '{rel}' ({file_size} bytes) at sector {next_data_sector}")


def compute_next_data_sector(image: bytearray, dir_sector: int, dir_sectors: int) -> int:
    """Return the next free data sector, accounting for files inside subdirectories."""
    next_sec = dir_sector + dir_sectors
    for entry_off in iter_entries(image, (dir_sector - 1) * SECTOR_SIZE, dir_sectors):
        if image[entry_off] == 0:
            continue
        next_sec = max(next_sec, entry_end_sector(image, entry_off))
        flags = image[entry_off + OFF_FLAGS]
        if flags & FLAG_DIR:
            sub_start = struct.unpack_from("<H", image, entry_off + OFF_SECTOR)[0]
            sub_offset = (sub_start - 1) * SECTOR_SIZE
            for sub_entry_off in iter_entries(image, sub_offset, dir_sectors):
                if image[sub_entry_off] == 0:
                    continue
                next_sec = max(next_sec, entry_end_sector(image, sub_entry_off))
    return next_sec


def entry_end_sector(image: bytearray, entry_off: int) -> int:
    """Return the first sector past the data for the given directory entry."""
    start = struct.unpack_from("<H", image, entry_off + OFF_SECTOR)[0]
    size = struct.unpack_from("<I", image, entry_off + OFF_SIZE)[0]
    sectors_used = (size + SECTOR_SIZE - 1) // SECTOR_SIZE
    return start + sectors_used


def find_free_entry(
    image: bytearray, parent_offset: int, dir_sectors: int, filename: str
) -> int | None:
    """Return the offset of the first free entry in a directory.

    Raises if `filename` is found while scanning.
    """
    for entry_off in iter_entries(image, parent_offset, dir_sectors):
        if image[entry_off] == 0:
            return entry_off
        name = bytes(image[entry_off:entry_off + NAME_FIELD]).rstrip(b"\x00").decode()
        if name == filename:
            raise SystemExit(f"Error: '{filename}' already exists")
    return None


def find_subdir_entry(
    image: bytearray, dir_sector: int, dir_sectors: int, name: str
) -> int | None:
    """Return the offset of the directory entry for `name` in root, or None."""
    for entry_off in iter_entries(image, (dir_sector - 1) * SECTOR_SIZE, dir_sectors):
        if image[entry_off] == 0:
            continue
        entry_name = bytes(image[entry_off:entry_off + NAME_FIELD]).rstrip(b"\x00").decode()
        if entry_name != name:
            continue
        if not (image[entry_off + OFF_FLAGS] & FLAG_DIR):
            return None
        return entry_off
    return None


def iter_entries(image: bytearray, base_offset: int, sector_count: int):
    """Yield offsets for each directory entry across `sector_count` sectors."""
    for i in range(ENTRIES_PER_SECTOR * sector_count):
        yield base_offset + i * ENTRY_SIZE


def load_image(image_path: str) -> bytearray:
    with open(image_path, "rb") as f:
        return bytearray(f.read())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add a file to a BBoeOS drive image."
    )
    parser.add_argument(
        "-d",
        "--subdir",
        help="place the file inside this subdirectory under root",
    )
    parser.add_argument(
        "-x",
        "--executable",
        action="store_true",
        help="mark the file as executable (sets FLAG_EXEC)",
    )
    parser.add_argument("file", help="path to the file to add (or directory name with --mkdir)")
    parser.add_argument(
        "--image",
        default="drive.img",
        help="path to the drive image (default: drive.img)",
    )
    parser.add_argument(
        "--mkdir",
        action="store_true",
        help="create a subdirectory under root named <file>",
    )
    args = parser.parse_args()
    if args.mkdir:
        if args.subdir or args.executable:
            parser.error("--mkdir does not accept -d or -x")
        mkdir(dirname=args.file, image_path=args.image)
    else:
        add_file(
            executable=args.executable,
            file_path=args.file,
            image_path=args.image,
            subdir=args.subdir,
        )


def mkdir(*, dirname: str, image_path: str) -> None:
    if len(dirname) > FNAME_MAX:
        raise SystemExit(
            f"Error: directory name '{dirname}' exceeds {FNAME_MAX} characters"
        )

    dir_sector = read_assign("DIR_SECTOR")
    dir_sectors = read_assign("DIR_SECTORS")
    image = load_image(image_path)

    parent_offset = (dir_sector - 1) * SECTOR_SIZE
    entry_off = find_free_entry(image, parent_offset, dir_sectors, dirname)
    if entry_off is None:
        raise SystemExit("Error: root directory is full")

    next_data_sector = compute_next_data_sector(image, dir_sector, dir_sectors)
    dir_bytes = dir_sectors * SECTOR_SIZE

    write_entry(image, entry_off, dirname, FLAG_DIR, next_data_sector, dir_bytes)
    data_off = (next_data_sector - 1) * SECTOR_SIZE
    if data_off + dir_bytes > len(image):
        raise SystemExit(
            f"Error: directory would extend past end of image (need {data_off + dir_bytes} bytes)"
        )
    image[data_off:data_off + dir_bytes] = b"\x00" * dir_bytes
    save_image(image_path, image)

    print(f"Created directory '{dirname}' at sector {next_data_sector}")


def read_assign(name: str) -> int:
    """Return the integer value of a `%assign NAME VALUE` line in constants.asm."""
    pattern = re.compile(rf"^\s*%assign\s+{re.escape(name)}\s+(\S+)")
    with open(CONSTANTS_PATH) as f:
        for line in f:
            m = pattern.match(line)
            if m:
                return int(m.group(1), 0)
    raise SystemExit(f"Error: {name} not found in {CONSTANTS_PATH}")


def save_image(image_path: str, image: bytearray) -> None:
    with open(image_path, "wb") as f:
        f.write(image)


def write_data(image: bytearray, start_sector: int, data: bytes) -> None:
    data_off = (start_sector - 1) * SECTOR_SIZE
    if data_off + len(data) > len(image):
        raise SystemExit(
            f"Error: data would extend past end of image (need {data_off + len(data)} bytes)"
        )
    image[data_off:data_off + len(data)] = data


def write_entry(
    image: bytearray,
    entry_off: int,
    name: str,
    flags: int,
    start_sector: int,
    size: int,
) -> None:
    name_bytes = name.encode().ljust(NAME_FIELD, b"\x00")
    image[entry_off:entry_off + NAME_FIELD] = name_bytes
    image[entry_off + OFF_FLAGS] = flags
    struct.pack_into("<H", image, entry_off + OFF_SECTOR, start_sector)
    struct.pack_into("<I", image, entry_off + OFF_SIZE, size)


if __name__ == "__main__":
    main()
