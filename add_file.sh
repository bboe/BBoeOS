#!/bin/sh
#
# Usage: ./add_file.sh <floppy.img> <file_to_add>
#
# Adds a file to the BBoeOS floppy image filesystem.

set -e

if [ $# -ne 2 ]; then
    echo "Usage: $0 <floppy.img> <file_to_add>" >&2
    exit 1
fi

IMG="$1"
FILE="$2"
FILENAME=$(basename "$FILE")

DIR_SECTOR_OFFSET=$((5 * 512))  # Sector 6 at byte offset 2560
ENTRY_SIZE=16
MAX_ENTRIES=32
FNAME_MAX=11

if [ ${#FILENAME} -gt $FNAME_MAX ]; then
    echo "Error: filename '${FILENAME}' exceeds ${FNAME_MAX} characters" >&2
    exit 1
fi

FILE_SIZE=$(wc -c < "$FILE" | tr -d ' ')
if [ "$FILE_SIZE" -gt 512 ]; then
    echo "Error: file exceeds 512 bytes (single-sector limit)" >&2
    exit 1
fi
if [ "$FILE_SIZE" -eq 0 ]; then
    echo "Error: file is empty" >&2
    exit 1
fi

# Find next free directory entry and next free data sector
NEXT_ENTRY=-1
NEXT_DATA_SECTOR=7  # First data sector

for i in $(seq 0 $((MAX_ENTRIES - 1))); do
    OFFSET=$((DIR_SECTOR_OFFSET + i * ENTRY_SIZE))
    FIRST_BYTE=$(dd if="$IMG" bs=1 skip="$OFFSET" count=1 2>/dev/null | od -An -tu1 | tr -d ' ')
    if [ -z "$FIRST_BYTE" ] || [ "$FIRST_BYTE" = "0" ]; then
        NEXT_ENTRY=$i
        break
    fi
    # Track next free data sector from this entry
    SECTOR_OFFSET=$((OFFSET + 12))
    START_SEC=$(dd if="$IMG" bs=1 skip="$SECTOR_OFFSET" count=2 2>/dev/null | od -An -tu2 | tr -d ' ')
    SIZE_BYTES=$(dd if="$IMG" bs=1 skip=$((OFFSET + 14)) count=2 2>/dev/null | od -An -tu2 | tr -d ' ')
    SECTORS_USED=$(( (SIZE_BYTES + 511) / 512 ))
    END_SECTOR=$((START_SEC + SECTORS_USED))
    if [ "$END_SECTOR" -gt "$NEXT_DATA_SECTOR" ]; then
        NEXT_DATA_SECTOR=$END_SECTOR
    fi
done

if [ "$NEXT_ENTRY" -eq -1 ]; then
    echo "Error: directory full" >&2
    exit 1
fi

ENTRY_OFFSET=$((DIR_SECTOR_OFFSET + NEXT_ENTRY * ENTRY_SIZE))

# Write filename (null-padded to 12 bytes)
printf '%s' "$FILENAME" | dd of="$IMG" bs=1 seek="$ENTRY_OFFSET" conv=notrunc 2>/dev/null
# Null-pad remaining bytes
REMAINING=$((12 - ${#FILENAME}))
dd if=/dev/zero of="$IMG" bs=1 seek=$((ENTRY_OFFSET + ${#FILENAME})) count="$REMAINING" conv=notrunc 2>/dev/null

# Write start sector (2 bytes, little-endian)
printf "\\$(printf '%03o' $((NEXT_DATA_SECTOR & 0xFF)))\\$(printf '%03o' $(((NEXT_DATA_SECTOR >> 8) & 0xFF)))" | \
    dd of="$IMG" bs=1 seek=$((ENTRY_OFFSET + 12)) conv=notrunc 2>/dev/null

# Write file size (2 bytes, little-endian)
printf "\\$(printf '%03o' $((FILE_SIZE & 0xFF)))\\$(printf '%03o' $(((FILE_SIZE >> 8) & 0xFF)))" | \
    dd of="$IMG" bs=1 seek=$((ENTRY_OFFSET + 14)) conv=notrunc 2>/dev/null

# Write file data
DATA_OFFSET=$(( (NEXT_DATA_SECTOR - 1) * 512 ))
dd if="$FILE" of="$IMG" bs=1 seek="$DATA_OFFSET" conv=notrunc 2>/dev/null

echo "Added '${FILENAME}' (${FILE_SIZE} bytes) at sector ${NEXT_DATA_SECTOR}, entry ${NEXT_ENTRY}"
