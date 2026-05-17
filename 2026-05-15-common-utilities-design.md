# Common Unix utilities for BBoeOS

**Status**: design, awaiting review **Date**: 2026-05-15

## Goal

Add a set of common Unix-style utilities to BBoeOS userland so pipelines and
shell scripting become useful. The shell already supports 2-stage pipelines
(`sys_pipeline2`) and chained commands (`;`, `&&`, `||`), but the only filter
programs that exist today are `cat` and `echo`. This spec covers eleven new
programs and the shared infrastructure they need.

## Scope

In scope:

- `grep`, `sort`, `head`, `tail`, `wc` (the five requested)
- `tee`, `uniq`, `tr`, `true`, `false`, `seq`, `yes` (six extras)
- A shared `read_line` helper for line-oriented filters
- A thin `sbrk`-style heap helper used by `sort`
- `cat` retrofit so it falls back to stdin when no file is given (matching the
  new convention)
- `tests/test_programs.py` entries for each utility

Out of scope (deferred):

- `cut`, `paste`, `xargs` — wait for N-stage pipelines
- `find` — needs recursion beyond the bbfs one-level subdirectory limit
- `expr`, `test`, `[` — wait for shell scripting
- `basename`, `dirname` — path syntax too restricted to be interesting
- Regex in `grep` — literal substring only, by design
- `tail -f` — needs blocking I/O on `/dev/console` and friends

## Cross-cutting design

### stdin convention

Every filter takes `[file]` and falls back to fd 0 when omitted. This is what
makes them usable inside `sys_pipeline2` (where the left child's stdout is
spliced to the right child's stdin). `cat` is updated to follow this rule.

### Line buffer helper

A new shared file `src/c/line_helpers.c` exposes:

```c
/* Read one line (up to and including '\n') from fd into buf.
   Returns length including the '\n', 0 on EOF, -1 on read error.
   If the line exceeds max-1 bytes, the tail is dropped and the return
   value reports max-1 (so wc still counts the line, grep still matches
   the prefix). The next call resumes after the dropped tail. */
int read_line(int fd, char *buf, int max);
```

`MAX_LINE` is fixed at 1024. Internally `read_line` keeps a small static
single-fd buffer; the helper is single-fd at a time (which is all our filters
need — they read one input).

The single-file no-linker convention means consumers `#include "line_helpers.c"`
directly. To prevent `make_os.sh` from compiling shared files as standalone
programs, shared files use the `_` prefix that the build script already (or
will) skip. **Plan item**: confirm the build's discovery rule and either match
the existing convention or extend it.

### Heap helper

`sort` is the only utility that buffers all input. It needs a userland
`sbrk`-style helper. Two options:

1. Add a `sys_break` C builtin to `cc/codegen/x86/generator.py` so callers write
   `char *p = sys_break(needed);`. Mirrors how `chmod` etc. are exposed.
2. Implement it as an inline-`asm` wrapper inside a shared `_heap.c`.

**Recommendation**: option 1 (builtin). It's the same pattern used for every
other INT 30h syscall and keeps the user code readable. The plan covers adding
the builtin and a one-line table entry; codegen is mechanical.

The helper API:

```c
/* Move program break to new_break. Pass 0 to query current break.
   Returns new break on success, 0 on failure (no CF — userland convention
   for builtins is "return value 0 means error" where possible). */
void *sys_break(void *new_break);
```

`sort` calls it once at startup to grab a 64 KB heap, fails fast if denied.

### Argument parsing

Flags are parsed by hand (no `getopt`). Each utility supports only the flags
listed below; unknown flags `die()` with usage. Short flags only, no clustering
(`-l -w`, not `-lw`) — matching the codebase's minimalism.

### Exit status

Every program sets `last_exec_status` via its return value from `main` so
`&&`/`||` work. `0` = success, `1` = generic failure, `2` = usage error. `grep`
follows the Unix convention: `0` if any line matched, `1` if none, `2` on error.

### SIGPIPE

`yes` and any program writing to a closed pipe must exit cleanly on SIGPIPE. The
kernel already delivers SIGPIPE; default handler is termination, which is what
we want. No per-program work needed unless a program installs its own SIGINT
handler.

## Per-utility spec

### `grep <pattern> [file]`

Literal substring match (no regex). Prints matching lines verbatim.

Flags:
- `-v` invert: print non-matching lines
- `-n` prepend `line_number:` to each output line
- `-i` case-insensitive

Reads stdin if no file. Exit 0 if any match, 1 if none, 2 on error.

### `sort [file]`

Sort lines in ASCII order. Buffers all input on a 64 KB heap acquired via
`sys_break`. If input exceeds the heap, prints `sort: input too large\n` and
exits 1.

Flags:
- `-r` reverse
- `-n` numeric (parse leading `[+-]?[0-9]+`, lines without a number sort as
  zero; non-numeric tail is ignored)
- `-u` unique (drop adjacent duplicates after sort)

Algorithm: read into heap, build `char *` index array (also on heap), qsort
pointers, write out. qsort is iterative-merge (the C subset has no recursion
restriction but iterative-merge is small and predictable).

### `head [-n N] [file]`

Print first N lines, default N=10. Streams and exits as soon as N is reached
(important for `head` in pipelines so the upstream sees SIGPIPE and stops).

### `tail [-n N] [file]`

Print last N lines, default N=10. Implementation:

- If reading from a regular file with a known size, seek to end, scan back to
  find N newlines, print from there. Bounded memory.
- If reading from stdin, use a ring buffer of N line slots backed by a fixed 64
  KB BSS array (no heap dependency, so this stays in PR 2). Slot width is
  `min(MAX_LINE, 65536 / N)`. If `N > 65536` (slot width would round to zero),
  fail at runtime with `tail: N too large\n`. Lines that exceed the slot width
  are truncated per `read_line`'s policy.

No `-f`.

### `wc [file]`

Count lines, words, bytes. Default prints all three; flags `-l`, `-w`, `-c`
restrict to one. A word is a maximal run of non-whitespace, per the standard
definition.

### `tee <file>`

Copy stdin to stdout and to `file`. Truncates `file`. No `-a` flag for now (the
FS doesn't have a clean append primitive yet — `O_APPEND` is not in the existing
flags table).

### `uniq [file]`

Collapse runs of identical adjacent lines.

Flags:
- `-c` prefix each line with its count
- `-d` only print lines that were duplicated

### `tr <set1> <set2>`

Single-character translate with range support. `a-z`, `A-Z`, `0-9`, etc. expand
to the inclusive ASCII range. A literal `-` is matched if it's the first or last
character of a set. No character classes (`[:alpha:]` etc.), no complement, no
squeeze. After expansion, `set1` and `set2` must be the same length unless `-d`
is given; otherwise fail with `tr: set length mismatch\n`. Expansion is bounded
(256 chars max per set) and parsed into a fixed 256-byte buffer on the stack.

Flags:
- `-d` delete chars in `set1` (ignores `set2`)

### `true` / `false`

`true` exits 0, `false` exits 1. Useful for testing `&&`/`||` and for shell
script scaffolding once it exists.

### `seq [start] end`

Print integers `start..end` inclusive, one per line. `start` defaults to 1.
Negative numbers allowed. No step argument.

### `yes [string]`

Print `string\n` forever (default `"y"`). Exits on SIGPIPE (default handler).

## File layout

```
src/c/
  grep.c
  sort.c
  head.c
  tail.c
  wc.c
  tee.c
  uniq.c
  tr.c
  true.c
  false.c
  seq.c
  yes.c
  _line_helpers.c        # shared, build-discovery-skipped
  cat.c                  # retrofitted for stdin fallback
```

(Naming convention `_*.c` is provisional — confirmed against `make_os.sh` during
planning.)

## Testing

Each utility gets one or more entries in `tests/test_programs.py`. The pattern
is identical to existing entries: boot, run a shell command, regex-match the
output. Representative pipelines:

- `grep`: `echo -e 'aaa\nbbb\naaa' | grep aaa` → 2 lines of `aaa`
- `sort`: `echo -e 'b\na\nc' | sort` → `a\nb\nc`
- `sort -u`: `echo -e 'a\na\nb' | sort -u` → `a\nb`
- `head -n 2`: `seq 1 5 | head -n 2` → `1\n2`
- `tail -n 2`: `seq 1 5 | tail -n 2` → `4\n5`
- `wc -l`: `seq 1 7 | wc -l` → `7`
- `tee`: `echo hi | tee /tmp/x` → `hi` on stdout, and `cat /tmp/x` → `hi`
- `uniq`: `echo -e 'a\na\nb' | uniq` → `a\nb`
- `tr`: `echo HELLO | tr A-Z a-z` → `hello`
- `tr -d`: `echo abc123 | tr -d 0-9` → `abc`
- `true`/`false`: chain with `&&`/`||` and check `$?`
- `seq`: `seq 3` → `1\n2\n3`
- `yes`: `yes hi | head -n 3` → `hi\nhi\nhi`

The `yes | head` test depends on SIGPIPE working in the pipeline. If
`test_programs.py` doesn't already cover the pipeline+SIGPIPE path, the test
entry is the first place this is exercised.

`ext2` filesystem matrix run (`--filesystem ext2`) needs to pass for every
utility that touches the FS (`tee`, `cat`, any test that uses `/tmp/x`).

## Rollout: three PRs

### PR 1 — foundation (streaming, no heap)

- `_line_helpers.c` with `read_line`
- `wc`, `head`, `tail` (file-mode only; stdin-with-ring-buffer comes in PR 2),
  `tee`, `true`, `false`, `seq`, `yes`
- `cat` retrofit for stdin fallback
- test_programs.py entries for all of the above

### PR 2 — text filters

- `grep`, `tr`, `uniq`
- `tail` stdin ring-buffer support
- test_programs.py entries

### PR 3 — sort + heap plumbing

- `sys_break` C builtin in `cc/codegen/x86/generator.py`
- `sort` with all flags
- test_programs.py entries
- docs update: `docs/c_subset.md` builtin table

## Open question for the planning phase

The build's `make_os.sh` autodiscovery rule for which `src/c/*.c` files become
standalone programs vs. shared `#include` files is the one bit I haven't read.
The plan's first step inspects the script and either uses an existing convention
or extends it (e.g., a leading `_` skip rule). If no convention exists, the plan
adds one — a one-line change.

## Non-goals / explicitly deferred

- No regex in grep, ever (this is a stable design choice, not a TODO)
- No tail -f
- No tr ranges or character classes
- No append-mode tee
- No locale-aware sorting
- No multi-file input to any utility (single file or stdin only)
