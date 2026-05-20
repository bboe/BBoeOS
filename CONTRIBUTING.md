# Contributing to BBoeOS

BBoeOS is a personal hobby project. External contributions are accepted only
occasionally and on a case-by-case basis. **Please open an issue to discuss your
idea before opening a pull request** — it saves both of us time if the change
isn't something the project wants.

## Before You Start

- Read the [Code of Conduct](CODE_OF_CONDUCT.md). It applies to all project
  spaces.
- Note the project license: **AGPL-3.0** (see [LICENSE](LICENSE)).
- **Copyright assignment is required.** By submitting a pull request, you agree
  to assign copyright in your contribution to the maintainer under the terms of
  the [Copyright Assignment Agreement](COPYRIGHT_ASSIGNMENT.md) (the Harmony
  CAA-I v1.0 with Section 2.3 Option Five). The CLA Assistant bot will ask you
  to sign electronically on your first PR. The maintainer becomes the copyright
  owner and may relicense the project — including under permissive, proprietary,
  or commercial terms — at their discretion. You retain a broad license back to
  your own contribution under §2.1(d), so you can still reuse it in other
  projects, but you cannot prevent the maintainer from using it however they
  choose.

## Build and Run

See [README.md](README.md) for the host toolchain (`nasm`, QEMU) and the basic
`./make_os.sh` + `qemu-system-i386` workflow. The architecture overview in
[CLAUDE.md](CLAUDE.md) and [docs/architecture.md](docs/architecture.md) is the
right starting point if you want to understand how the boot path, kernel,
paging, and userland fit together.

## Style and Conventions

The conventions live in [CLAUDE.md](CLAUDE.md) — please read the relevant
sections before sending a non-trivial change. Highlights:

- **Sorted order.** New commands, functions, and shell builtins go in
  alphabetical order; `equ` blocks, dispatch chains, and Python functions too.
- **Naming.** Constants and string labels use `UPPER_CASE`. Functions and
  variables use `lower_case`. Local labels use `.dot_prefix`. No abbreviations —
  spell out `expression`, `buffer`, `directory`, etc., in both Python and C.
- **Comments.** Preserve existing comments when editing. Add new ones only when
  the *why* is non-obvious.
- **Markdown.** Wrap prose at 80 columns. Run `tools/wrap_md.py <file.md>` after
  editing.

## Testing

Manual QEMU testing is the primary workflow. The automated suites are:

- `tests/test_asm.py` — self-hosted assembler round-trip against NASM.
- `tests/test_bboefs.py` — bbfs filesystem regressions.
- `tests/test_programs.py` — runtime smoke tests; `--filesystem ext2` exercises
  the ext2 path, `--slow` adds the large-file cases.

For kernel-architecture changes (boot path, paging, DMA), run the full matrix in
`.github/workflows/test.yml` locally before opening a PR.

## Pull Requests

- Branch from `main`.
- Keep changes focused; prefer multiple small PRs over one large one.
- Update [docs/CHANGELOG.md](docs/CHANGELOG.md) under the *Unreleased* section
  when your change is user-visible.
- For substantive rewrites of `user/programs/<name>.c`, update `archive/<name>.asm` in
  the same commit so the archive size comparison stays apples-to-apples.

## Security Issues

Do **not** open public issues for security vulnerabilities. See
[SECURITY.md](SECURITY.md) for the private reporting flow.
