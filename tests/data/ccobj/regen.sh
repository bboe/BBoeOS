#!/bin/sh
# Regenerate fixture .lst / .bin from .asm.  Run after editing a fixture.
set -eu
cd "$(dirname "$0")"
REPO_ROOT=$(git rev-parse --show-toplevel)
for asm in *.asm; do
    base=${asm%.asm}
    nasm -f bin -i "$REPO_ROOT/src/include/" \
         -l "$base.lst" -o "$base.bin" "$asm"
done
