#!/usr/bin/env python3
"""Add a file to a BBoeOS drive image."""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import struct
import subprocess
import tempfile
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

CONSTANTS_PATH = "src/include/constants.asm"
ENTRIES_PER_SECTOR = 16
ENTRY_SIZE = 32
EXT2_MAGIC = 0xEF53
EXT2_SB_MAGIC_OFFSET = 56  # s_magic field offset within superblock
EXT2_SB_PARTITION_OFFSET = 1024  # superblock offset within ext2 partition
FILENAME_MAX = 24
FLAG_DIRECTORY = 0x02
FLAG_EXECUTE = 0x01
MAX_RESOLVE_DEPTH = 16
NAME_FIELD = 25
OFFSET_FLAGS = 25
OFFSET_SECTOR = 26
OFFSET_SIZE = 28  # 4-byte (32-bit) file size
SECTOR_SIZE = 512
STAGE2_BYTES_OFFSET = 508  # offset of stage2_bytes word within the MBR
_DD = shutil.which("dd") or "dd"
_DEBUGFS = shutil.which("debugfs") or "debugfs"


@contextmanager
def _ext2_partition(*, ext2_start_sector: int, image_path: str) -> Generator[str, None, None]:
    """Extract the ext2 partition to a temp file, yield its path, splice it back.

    Yields
    ------
    str
        Path to the temporary file containing only the ext2 partition.

    """
    with tempfile.NamedTemporaryFile(suffix=".ext2", delete=False) as f:
        tmp = pathlib.Path(f.name)
    try:
        subprocess.run(
            [_DD, f"if={image_path}", f"of={tmp}", "bs=512", f"skip={ext2_start_sector}", "status=none"],
            check=True,
        )
        yield str(tmp)
        subprocess.run(
            [_DD, f"if={tmp}", f"of={image_path}", "bs=512", f"seek={ext2_start_sector}", "conv=notrunc", "status=none"],
            check=True,
        )
    finally:
        tmp.unlink()


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

    ext2_start_sector = compute_directory_sector(image_path=image_path)
    if detect_fs_type(ext2_start_sector=ext2_start_sector, image_path=image_path) == "ext2":
        ext2_add_file(
            executable=executable,
            ext2_start_sector=ext2_start_sector,
            file_path=file_path,
            image_path=image_path,
            subdirectory=subdirectory,
        )
        return

    directory_sector = compute_directory_sector(image_path=image_path)
    directory_sectors = read_assign("DIRECTORY_SECTORS")
    image = load_image(image_path)

    if subdirectory is None:
        parent_offset = (directory_sector) * SECTOR_SIZE
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
        parent_offset = (parent_start) * SECTOR_SIZE

    entry_offset = find_free_entry(directory_sectors=directory_sectors, filename=filename, image=image, parent_offset=parent_offset)
    if entry_offset is None:
        message = f"Error: '{subdirectory or 'root directory'}' is full"
        raise SystemExit(message)

    next_data_sector = compute_next_data_sector(directory_sector=directory_sector, directory_sectors=directory_sectors, image=image)

    flags = FLAG_EXECUTE if executable else 0
    write_entry(entry_offset=entry_offset, flags=flags, image=image, name=filename, size=file_size, start_sector=next_data_sector)
    write_data(data=file_data, image=image, start_sector=next_data_sector)
    save_image(image=image, image_path=image_path)

    relative_path = f"{subdirectory}/{filename}" if subdirectory else filename
    print(f"Added '{relative_path}' ({file_size} bytes) at sector {next_data_sector}")


def compute_directory_sector(*, image_path: str) -> int:
    """Return the sector where the filesystem directory starts on disk.

    NASM embeds stage2's byte count in the MBR at ``STAGE2_BYTES_OFFSET``
    (little-endian word).  Stage1 reads the same word at boot to size the
    disk-read; here we mirror its arithmetic: sectors = ceil(bytes / 512),
    directory starts at sectors + 1 (right after stage2 on disk).

    Returns
    -------
    int
        The 1-based LBA where directory entries (bbfs) or the ext2
        partition (ext2) begin.

    """
    with pathlib.Path(image_path).open("rb") as file:
        file.seek(STAGE2_BYTES_OFFSET)
        stage2_bytes = struct.unpack("<H", file.read(2))[0]
    return (stage2_bytes + SECTOR_SIZE - 1) // SECTOR_SIZE + 1


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
    for entry_offset in iter_entries(base_offset=(directory_sector) * SECTOR_SIZE, sector_count=directory_sectors):
        if image[entry_offset] == 0:
            continue
        next_sector = max(next_sector, entry_end_sector(entry_offset=entry_offset, image=image))
        flags = image[entry_offset + OFFSET_FLAGS]
        if flags & FLAG_DIRECTORY:
            subdirectory_start = struct.unpack_from("<H", image, entry_offset + OFFSET_SECTOR)[0]
            subdirectory_offset = (subdirectory_start) * SECTOR_SIZE
            for subdirectory_entry_offset in iter_entries(base_offset=subdirectory_offset, sector_count=directory_sectors):
                if image[subdirectory_entry_offset] == 0:
                    continue
                next_sector = max(next_sector, entry_end_sector(entry_offset=subdirectory_entry_offset, image=image))
    return next_sector


def detect_fs_type(*, ext2_start_sector: int, image_path: str) -> str:
    """Return "ext2" if the image has a valid ext2 superblock magic, else "bbfs".

    Returns
    -------
    str
        ``"ext2"`` or ``"bbfs"``.

    """
    offset = ext2_start_sector * SECTOR_SIZE + EXT2_SB_PARTITION_OFFSET + EXT2_SB_MAGIC_OFFSET
    magic_size = struct.calcsize("<H")
    try:
        with pathlib.Path(image_path).open("rb") as f:
            f.seek(offset)
            data = f.read(magic_size)
        (magic,) = struct.unpack("<H", data)
    except (OSError, struct.error):
        return "bbfs"
    else:
        return "ext2" if magic == EXT2_MAGIC else "bbfs"


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


def ext2_add_file(
    *,
    executable: bool,
    ext2_start_sector: int,
    file_path: str,
    image_path: str,
    subdirectory: str | None,
) -> None:
    """Add a file to an ext2 partition via debugfs.

    Raises
    ------
    SystemExit
        If debugfs reports an error writing the file.

    """
    filename = pathlib.Path(file_path).name
    dest = f"/{subdirectory}/{filename}" if subdirectory else f"/{filename}"
    with _ext2_partition(ext2_start_sector=ext2_start_sector, image_path=image_path) as tmp:
        result = subprocess.run(
            [_DEBUGFS, "-w", "-R", f"write {file_path} {dest}", tmp],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            message = f"Error: debugfs write failed:\n{result.stderr.decode()}"
            raise SystemExit(message)
        if executable:
            subprocess.run(
                [_DEBUGFS, "-w", "-R", f"set_inode_field {dest} mode 0100755", tmp],
                check=True,
                capture_output=True,
            )
    file_size = pathlib.Path(file_path).stat().st_size
    relative_path = f"{subdirectory}/{filename}" if subdirectory else filename
    print(f"Added '{relative_path}' ({file_size} bytes) [ext2]")


def ext2_make_directory(*, dirname: str, ext2_start_sector: int, image_path: str) -> None:
    """Create a directory in an ext2 partition via debugfs.

    Raises
    ------
    SystemExit
        If debugfs reports an error creating the directory.

    """
    with _ext2_partition(ext2_start_sector=ext2_start_sector, image_path=image_path) as tmp:
        result = subprocess.run(
            [_DEBUGFS, "-w", "-R", f"mkdir /{dirname}", tmp],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            message = f"Error: debugfs mkdir failed:\n{result.stderr.decode()}"
            raise SystemExit(message)
    print(f"Created directory '{dirname}' [ext2]")


def find_entry(
    *,
    directory_sectors: int,
    directory_start_sector: int,
    image: bytes | bytearray,
    name: str,
) -> tuple[int, int, int] | None:
    """Return (flags, start_sector, size) for `name` in a directory, or None.

    Returns
    -------
    tuple[int, int, int] | None
        ``(flags, start_sector, size)`` if found, else ``None``.

    """
    base = (directory_start_sector) * SECTOR_SIZE
    target = name.encode()
    for entry_offset in iter_entries(base_offset=base, sector_count=directory_sectors):
        if image[entry_offset] == 0:
            continue
        entry_name = bytes(image[entry_offset : entry_offset + NAME_FIELD]).rstrip(b"\x00")
        if entry_name != target:
            continue
        flags = image[entry_offset + OFFSET_FLAGS]
        sector = struct.unpack_from("<H", image, entry_offset + OFFSET_SECTOR)[0]
        size = struct.unpack_from("<I", image, entry_offset + OFFSET_SIZE)[0]
        return (flags, sector, size)
    return None


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
    for entry_offset in iter_entries(base_offset=(directory_sector) * SECTOR_SIZE, sector_count=directory_sectors):
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

    ext2_start_sector = compute_directory_sector(image_path=image_path)
    if detect_fs_type(ext2_start_sector=ext2_start_sector, image_path=image_path) == "ext2":
        ext2_make_directory(dirname=dirname, ext2_start_sector=ext2_start_sector, image_path=image_path)
        return

    directory_sector = compute_directory_sector(image_path=image_path)
    directory_sectors = read_assign("DIRECTORY_SECTORS")
    image = load_image(image_path)

    parent_offset = (directory_sector) * SECTOR_SIZE
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
    data_offset = (next_data_sector) * SECTOR_SIZE
    if data_offset + directory_bytes > len(image):
        message = f"Error: directory would extend past end of image (need {data_offset + directory_bytes} bytes)"
        raise SystemExit(
            message,
        )
    image[data_offset : data_offset + directory_bytes] = b"\x00" * directory_bytes
    save_image(image=image, image_path=image_path)

    print(f"Created directory '{dirname}' at sector {next_data_sector}")


def compute_directory_sector(*, image_path: str) -> int:
    """Return the sector where the filesystem directory starts on disk.

    NASM embeds stage2's byte count in the MBR at ``STAGE2_BYTES_OFFSET``
    (little-endian word).  Stage1 reads the same word at boot to size the
    disk-read; here we mirror its arithmetic: sectors = ceil(bytes / 512),
    directory starts at sectors + 1 (right after stage2 on disk).

    Returns
    -------
    int
        The 1-based LBA where directory entries (bbfs) or the ext2
        partition (ext2) begin.

    """
    with pathlib.Path(image_path).open("rb") as file:
        file.seek(STAGE2_BYTES_OFFSET)
        stage2_bytes = struct.unpack("<H", file.read(2))[0]
    return (stage2_bytes + SECTOR_SIZE - 1) // SECTOR_SIZE + 1


def read_assign(name: str, /) -> int:
    """Return the integer value of a `%assign NAME VALUE` line in constants.asm.

    Symbolic references (where VALUE is itself a constant name) are resolved
    recursively up to ``MAX_RESOLVE_DEPTH`` levels.

    Returns
    -------
    int
        The parsed integer value.

    """
    pattern = re.compile(r"^\s*%assign\s+(\w+)\s+(\S+)")
    assigns: dict[str, str] = {}
    with pathlib.Path(CONSTANTS_PATH).open(encoding="utf-8") as file:
        for line in file:
            m = pattern.match(line)
            if m:
                assigns[m.group(1)] = m.group(2)

    def resolve(key: str, depth: int = 0) -> int:
        if depth > MAX_RESOLVE_DEPTH:
            message = f"Error: circular reference resolving {key} in {CONSTANTS_PATH}"
            raise SystemExit(message)
        val = assigns.get(key)
        if val is None:
            message = f"Error: {key} not found in {CONSTANTS_PATH}"
            raise SystemExit(message)
        try:
            return int(val, 0)
        except ValueError:
            return resolve(val, depth + 1)

    return resolve(name)


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
    data_offset = (start_sector) * SECTOR_SIZE
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
