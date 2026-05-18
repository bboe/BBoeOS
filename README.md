# BBoeOS design specs

This is an orphan branch (no shared history with `main`) where BBoeOS
design specs live. Specs are written here directly — usually via `git
mktree` + `git commit-tree` plumbing so the active feature worktree
isn't disturbed, or via a dedicated `git worktree` checkout of this
branch.

Each spec is a self-contained brainstorming-output design doc. When the
implementation plan grows complex enough to need its own document, it
lands here as `<date>-<topic>-plan.md` alongside the spec.

## Specs and plans

- [2026-05-15 — common utilities](./2026-05-15-common-utilities-design.md)
  — sort + sys_break + supporting cc.py changes. Landed across PRs #379–#382.
- [2026-05-16 — cc.py object files](./2026-05-16-cc-object-files-design.md)
  — ELF emission, `extern` declarations, `ccld` / `ccar`. In progress.
- [2026-05-16 — opendir / readdir](./2026-05-16-opendir-readdir-design.md)
  — POSIX directory iteration via Linux-style `getdents` + `<dirent.h>`.
  Plan: [2026-05-16-opendir-readdir-plan.md](./2026-05-16-opendir-readdir-plan.md).
  Status: design + plan complete; implementation pending.
- [2026-05-18 — blocking recvfrom](./2026-05-18-blocking-recvfrom-design.md)
  — add `timeout_ms` to `SYS_NET_RECVFROM`; kernel-side `hlt`-loop
  wait instead of userspace `sleep(1)` polling. Design approved;
  plan + implementation pending.
