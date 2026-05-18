# Security Policy

## Supported Versions

BBoeOS is a hobby / educational operating system. There are no release branches
— only the tip of `main` is supported. If you find an issue against an older
commit, please verify it still reproduces against current `main` before
reporting.

| Version | Supported          |
| ------- | ------------------ |
| `main`  | :white_check_mark: |
| other   | :x:                |

## Reporting a Vulnerability

Please report security issues through GitHub's **private vulnerability
reporting**:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Fill in the advisory form.

This keeps the report private to the maintainer until a fix is ready and a
coordinated disclosure can happen.

Please **do not** open public issues or pull requests for security
vulnerabilities.

### What to expect

BBoeOS is a hobby project maintained in spare time. There is no service-level
commitment for acknowledgement, triage, or fix timelines — reports will be
looked at when the maintainer next has time. If a report sits for a long while
without acknowledgement, feel free to bump it. Reporters who would like to be
named will be credited in the advisory once a fix lands.

## Scope

BBoeOS is not production software and ships with no security guarantees. That
said, the following classes of issues are in scope and welcomed:

- Kernel memory-safety bugs (out-of-bounds, use-after-free, type confusion) in
  `src/arch/x86/` or the C compiler / assembler.
- Ring-3 → ring-0 privilege escalation via syscalls (INT 30h) or the vDSO.
- Filesystem parser bugs (bbfs / ext2) that can be triggered by a crafted drive
  image.
- Network stack bugs (NE2000 driver, ARP / IP / ICMP / UDP) triggerable by
  crafted packets.
- Host-side tooling issues that could compromise a developer's machine (e.g.,
  `add_file.py`, `cc.py`, `make_os.sh`).

Out of scope:

- Denial-of-service via obviously malformed input where no privilege boundary is
  crossed (e.g., the shell crashing on garbage).
- Issues that only reproduce under modifications to the source tree.
