#!/usr/bin/env python3
"""Add a file to a BBoeOS drive image."""

from __future__ import annotations

import argparse
import pathlib
import re
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

CONSTANTS_PATH = "src/include/constants.asm"
ENTRIES_PER_SECTOR = 16
ENTRY_SIZE = 32
FILENAME_MAX = 24
FLAG_DIRECTORY = 0x02
FLAG_EXECUTE = 0x01
NAME_FIELD = 25
OFFSET_FLAGS = 25
OFFSET_SECTOR = 26
OFFSET_SIZE = 28  # 4-byte (32-bit) file size
SECTOR_SIZE = 512


def add_file(
    *,
    executable: bool,
    file_path: str,
    image_path: str,
    subdirectory: str | None,
) -> None:
    """Add a file to the BBoeOS drive image.

    Raises
    ------
    SystemExit
        If the filename is too long, the file is empty, the subdirectory is
        not found, or the directory is full.

    """
    filename = pathlib.Path(file_path).name
    if len(filename) > FILENAME_MAX:
        message = f"Error: filename '{filename}' exceeds {FILENAME_MAX} characters"
        raise SystemExit(message)

    file_data = pathlib.Path(file_path).read_bytes()
    if not file_data:
        message = "Error: file is empty"
        raise SystemExit(message)
    file_size = len(file_data)

    directory_sector = read_assign("DIRECTORY_SECTOR")
    directory_sectors = read_assign("DIRECTORY_SECTORS")
    image = load_image(image_path)

    if subdirectory is None:
        parent_offset = (directory_sector - 1) * SECTOR_SIZE
    else:
        subdirectory_entry_offset = find_subdirectory_entry(
            directory_sector=directory_sector,
            directory_sectors=directory_sectors,
            image=image,
            name=subdirectory,
        )
        if subdirectory_entry_offset is None:
            message = f"Error: directory '{subdirectory}' not found"
            raise SystemExit(message)
        parent_start = struct.unpack_from("<H", image, subdirectory_entry_offset + OFFSET_SECTOR)[0]
        parent_offset = (parent_start - 1) * SECTOR_SIZE

    entry_offset = find_free_entry(directory_sectors=directory_sectors, filename=filename, image=image, parent_offset=parent_offset)
    if entry_offset is None:
        location = subdirectory or "root directory"
        message = f"Error: '{location}' is full"
        raise SystemExit(message)

    next_data_sector = compute_next_data_sector(directory_sector=directory_sector, directory_sectors=directory_sectors, image=image)

    flags = FLAG_EXECUTE if executable else 0
    write_entry(entry_offset=entry_offset, flags=flags, image=image, name=filename, size=file_size, start_sector=next_data_sector)
    write_data(data=file_data, image=image, start_sector=next_data_sector)
    save_image(image=image, image_path=image_path)

    relative_path = f"{subdirectory}/{filename}" if subdirectory else filename
    print(f"Added '{relative_path}' ({file_size} bytes) at sector {next_data_sector}")


def compute_next_data_sector(
    *,
    directory_sector: int,
    directory_sectors: int,
    image: bytearray,
) -> int:
    """Return the next free data sector, accounting for files inside subdirectories.

    Returns
    -------
    int
        The first unused data sector.

    """
    next_sector = directory_sector + directory_sectors
    for entry_offset in iter_entries(base_offset=(directory_sector - 1) * SECTOR_SIZE, sector_count=directory_sectors):
        if image[entry_offset] == 0:
            continue
        next_sector = max(next_sector, entry_end_sector(entry_offset=entry_offset, image=image))
        flags = image[entry_offset + OFFSET_FLAGS]
        if flags & FLAG_DIRECTORY:
            subdirectory_start = struct.unpack_from("<H", image, entry_offset + OFFSET_SECTOR)[0]
            subdirectory_offset = (subdirectory_start - 1) * SECTOR_SIZE
            for subdirectory_entry_offset in iter_entries(base_offset=subdirectory_offset, sector_count=directory_sectors):
                if image[subdirectory_entry_offset] == 0:
                    continue
                next_sector = max(next_sector, entry_end_sector(entry_offset=subdirectory_entry_offset, image=image))
    return next_sector


def entry_end_sector(*, entry_offset: int, image: bytearray) -> int:
    """Return the first sector past the data for the given directory entry.

    Returns
    -------
    int
        Sector number immediately after the entry's data.

    """
    start = struct.unpack_from("<H", image, entry_offset + OFFSET_SECTOR)[0]
    size = struct.unpack_from("<I", image, entry_offset + OFFSET_SIZE)[0]
    sectors_used = (size + SECTOR_SIZE - 1) // SECTOR_SIZE
    return start + sectors_used


def find_free_entry(
    *,
    directory_sectors: int,
    filename: str,
    image: bytearray,
    parent_offset: int,
) -> int | None:
    """Return the offset of the first free entry in a directory.

    Returns
    -------
    int or None
        Byte offset of the free entry, or None if the directory is full.

    Raises
    ------
    SystemExit
        If `filename` already exists in the directory.

    """
    for entry_offset in iter_entries(base_offset=parent_offset, sector_count=directory_sectors):
        if image[entry_offset] == 0:
            return entry_offset
        name = bytes(image[entry_offset : entry_offset + NAME_FIELD]).rstrip(b"\x00").decode()
        if name == filename:
            message = f"Error: '{filename}' already exists"
            raise SystemExit(message)
    return None


def find_subdirectory_entry(
    *,
    directory_sector: int,
    directory_sectors: int,
    image: bytearray,
    name: str,
) -> int | None:
    """Return the offset of the directory entry for `name` in root, or None.

    Returns
    -------
    int or None
        Byte offset of the matching directory entry, or None.

    """
    for entry_offset in iter_entries(base_offset=(directory_sector - 1) * SECTOR_SIZE, sector_count=directory_sectors):
        if image[entry_offset] == 0:
            continue
        entry_name = bytes(image[entry_offset : entry_offset + NAME_FIELD]).rstrip(b"\x00").decode()
        if entry_name != name:
            continue
        if not (image[entry_offset + OFFSET_FLAGS] & FLAG_DIRECTORY):
            return None
        return entry_offset
    return None


def iter_entries(*, base_offset: int, sector_count: int) -> Iterator[int]:
    """Yield offsets for each directory entry across `sector_count` sectors.

    Yields
    ------
    int
        Byte offset of each entry.

    """
    for i in range(ENTRIES_PER_SECTOR * sector_count):
        yield base_offset + i * ENTRY_SIZE


def load_image(image_path: str, /) -> bytearray:
    """Load drive image into a mutable bytearray.

    Returns
    -------
    bytearray
        The drive image contents.

    """
    return bytearray(pathlib.Path(image_path).read_bytes())


def main() -> None:
    """CLI entry point for adding files to a BBoeOS drive image."""
    parser = argparse.ArgumentParser(description="Add a file to a BBoeOS drive image.")
    parser.add_argument(
        "-d",
        "--subdir",
        dest="subdirectory",
        help="place the file inside this subdirectory under root",
    )
    parser.add_argument(
        "-x",
        "--executable",
        action="store_true",
        help="mark the file as executable (sets FLAG_EXECUTE)",
    )
    parser.add_argument(
        "file",
        help="path to the file to add (or directory name with --mkdir)",
    )
    parser.add_argument(
        "--image",
        default="drive.img",
        help="path to the drive image (default: drive.img)",
    )
    parser.add_argument(
        "--mkdir",
        action="store_true",
        dest="make_directory",
        help="create a subdirectory under root named <file>",
    )
    arguments = parser.parse_args()
    if arguments.make_directory:
        if arguments.subdirectory or arguments.executable:
            parser.error("--mkdir does not accept -d or -x")
        make_directory(dirname=arguments.file, image_path=arguments.image)
    else:
        add_file(
            executable=arguments.executable,
            file_path=arguments.file,
            image_path=arguments.image,
            subdirectory=arguments.subdirectory,
        )


def make_directory(*, dirname: str, image_path: str) -> None:
    """Create a subdirectory on the drive image.

    Raises
    ------
    SystemExit
        If the directory name is too long, the root directory is full, or the
        directory would extend past the end of the image.

    """
    if len(dirname) > FILENAME_MAX:
        message = f"Error: directory name '{dirname}' exceeds {FILENAME_MAX} characters"
        raise SystemExit(
            message,
        )

    directory_sector = read_assign("DIRECTORY_SECTOR")
    directory_sectors = read_assign("DIRECTORY_SECTORS")
    image = load_image(image_path)

    parent_offset = (directory_sector - 1) * SECTOR_SIZE
    entry_offset = find_free_entry(directory_sectors=directory_sectors, filename=dirname, image=image, parent_offset=parent_offset)
    if entry_offset is None:
        message = "Error: root directory is full"
        raise SystemExit(message)

    next_data_sector = compute_next_data_sector(directory_sector=directory_sector, directory_sectors=directory_sectors, image=image)
    directory_bytes = directory_sectors * SECTOR_SIZE

    write_entry(
        entry_offset=entry_offset,
        flags=FLAG_DIRECTORY,
        image=image,
        name=dirname,
        size=directory_bytes,
        start_sector=next_data_sector,
    )
    data_offset = (next_data_sector - 1) * SECTOR_SIZE
    if data_offset + directory_bytes > len(image):
        message = f"Error: directory would extend past end of image (need {data_offset + directory_bytes} bytes)"
        raise SystemExit(
            message,
        )
    image[data_offset : data_offset + directory_bytes] = b"\x00" * directory_bytes
    save_image(image=image, image_path=image_path)

    print(f"Created directory '{dirname}' at sector {next_data_sector}")


def read_assign(name: str, /) -> int:
    """Return the integer value of a `%assign NAME VALUE` line in constants.asm.

    Returns
    -------
    int
        The parsed integer value.

    Raises
    ------
    SystemExit
        If the name is not found in constants.asm.

    """
    pattern = re.compile(rf"^\s*%assign\s+{re.escape(name)}\s+(\S+)")
    with pathlib.Path(CONSTANTS_PATH).open(encoding="utf-8") as file:
        for line in file:
            match = pattern.match(line)
            if match:
                return int(match.group(1), 0)
    message = f"Error: {name} not found in {CONSTANTS_PATH}"
    raise SystemExit(message)


def save_image(*, image: bytearray, image_path: str) -> None:
    """Write the drive image bytearray back to disk."""
    pathlib.Path(image_path).write_bytes(image)


def write_data(*, data: bytes, image: bytearray, start_sector: int) -> None:
    """Write raw data to consecutive sectors starting at start_sector.

    Raises
    ------
    SystemExit
        If the data would extend past the end of the image.

    """
    data_offset = (start_sector - 1) * SECTOR_SIZE
    if data_offset + len(data) > len(image):
        message = f"Error: data would extend past end of image (need {data_offset + len(data)} bytes)"
        raise SystemExit(
            message,
        )
    image[data_offset : data_offset + len(data)] = data


def write_entry(
    *,
    entry_offset: int,
    flags: int,
    image: bytearray,
    name: str,
    size: int,
    start_sector: int,
) -> None:
    """Write a directory entry at the given offset."""
    name_bytes = name.encode().ljust(NAME_FIELD, b"\x00")
    image[entry_offset : entry_offset + NAME_FIELD] = name_bytes
    image[entry_offset + OFFSET_FLAGS] = flags
    struct.pack_into("<H", image, entry_offset + OFFSET_SECTOR, start_sector)
    struct.pack_into("<I", image, entry_offset + OFFSET_SIZE, size)


if __name__ == "__main__":
    main()
