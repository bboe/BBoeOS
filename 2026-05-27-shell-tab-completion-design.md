# Shell Tab Completion

## Summary

Add tab completion to the BBoeOS shell. In command position (first word),
Tab completes against builtin names and executables in `bin/`. In argument
position, Tab completes against files and directories in the relevant
directory (cwd or path prefix). Directories get a trailing `/` appended to
the completion.

## Behavior

### Trigger

The Tab character (`0x09`) in the line editor triggers completion.

### Context detection

The word under the cursor is extracted by scanning backward from cursor to
the nearest space (or start of line). If no space precedes the word start,
it is in command position; otherwise it is in argument position.

### Command completion (first word)

Sources:
- Builtins: `help`, `reboot`, `shutdown`
- Entries in `bin/`: opened via `open("bin/", O_RDONLY)` + `getdents` loop

All `bin/` entries are executables by convention. The `bin/` prefix is
stripped so completions show the bare name (matching how users type
commands).

### Argument completion (subsequent words)

The partial word is split at the last `/` to derive:
- `directory` — everything up to and including the last `/` (or `.` if
  none)
- `prefix` — everything after the last `/`

`open(directory)` + `getdents` enumerates candidates. Both files and
directories are offered; directories get a `/` suffix appended.

### Matching and display

- **Prefix match**: candidates whose name starts with the typed prefix.
- **Single match**: the completion is inserted in-place (the prefix is
  replaced with the full name). If the match is a directory, `/` is
  appended. No trailing space is added (the user may want to continue
  typing a path).
- **Multiple matches**: insert the longest common prefix of all matches.
  If the longest common prefix equals what is already typed (i.e. Tab
  cannot narrow further), print all matches on the next line separated by
  two spaces, then reprint the prompt and current input buffer.
- **No matches**: visual bell (existing `visual_bell()` flash).

### Edge cases

- Empty first word + Tab: list all commands (builtins + bin/ entries).
- Tab at end of line with trailing space: argument completion with empty
  prefix (list all files in cwd).
- Completion in the middle of a word: not supported — only completes at
  the cursor when cursor == end of the current word token.

## Implementation

All changes are in `user/programs/shell.c`. No kernel modifications.

### New functions (alphabetical)

- `complete_argument(buf, cursor, end)` — extract partial, open directory,
  enumerate via getdents, filter by prefix, insert or display.
- `complete_command(buf, cursor, end)` — extract partial, enumerate
  builtins + bin/ entries, filter by prefix, insert or display.
- `tab_complete(buf, cursor, end)` — dispatch to complete_command or
  complete_argument based on position.

### New data (file-scope BSS)

- `char complete_buffer[4096]` — getdents buffer for completion.
- `char *complete_matches[64]` — pointers into an arena for matched names.
- `char complete_arena[2048]` — storage for matched name strings.

### Line editor integration

In the `main()` switch, add a `case '\t':` arm that calls `tab_complete`
and updates `cursor`/`end` with the returned values.

### Return convention

`tab_complete` (and its helpers) returns the new `end` value via an
out-parameter or a packed cursor|end return (cc.py supports struct returns
or pointer out-params). The simplest approach: two file-scope globals
`complete_cursor` and `complete_end` that the Tab case reads after the
call.

## Testing

- `tests/test_programs.py`: add a test that types a partial command + Tab
  and verifies the completed command appears in the output, then runs it.
- Manual: verify single match insertion, multiple match listing, directory
  `/` suffix, and visual bell on no match.
