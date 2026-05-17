# BBoeOS design specs

This is an orphan branch (no shared history with `main`) where BBoeOS
design specs live. Specs are written here directly — usually via `git
mktree` + `git commit-tree` plumbing so the active feature worktree
isn't disturbed, or via a dedicated `git worktree` checkout of this
branch.

Each spec is a self-contained brainstorming-output design doc. The
matching implementation plan lives in the feature branch's PR description
(or as a separate spec entry if the plan grows complex).

## Specs

- [2026-05-15 — common utilities](./2026-05-15-common-utilities-design.md)
  — sort + sys_break + supporting cc.py changes. Landed across PRs #379–#382.
- [2026-05-16 — cc.py object files](./2026-05-16-cc-object-files-design.md)
  — ELF emission, `extern` declarations, `ccld` / `ccar`. In progress.
- [2026-05-16 — opendir / readdir](./2026-05-16-opendir-readdir-design.md)
  — POSIX directory iteration via Linux-style `getdents` + `<dirent.h>`.
  Brainstorming complete; implementation pending.
