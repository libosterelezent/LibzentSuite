# Security Policy

## Supported surfaces

LibzentSuite is a self-hosted Discord orchestration suite. Security
reports are welcome for:

- The core framework (`core/`), adapters (`adapters/`), and interfaces
  (`interfaces/`) — especially account isolation, credential handling,
  authorization (the API token gate and the bot invoker gate), and the
  plugin sandbox boundaries.
- The FastAPI surface (`interfaces/api/`) — authentication, input
  validation, and anything reachable before authentication.
- CI and repository configuration.

Out of scope: Discord's own platform behavior, the third-party
`better.py-self` fork's internals (report upstream), and issues
requiring access to the operator's own credentials or machine.

## Reporting a vulnerability

**Do not open a public issue for security reports.** Use GitHub's
private vulnerability reporting on this repository (Report a
vulnerability → Security tab), which notifies the owner directly and
keeps the report private until a fix lands.

Include: affected surface, reproduction steps, impact, and — if
relevant to account isolation or credential handling — which invariant
you believe breaks (the boundaries are documented in
`docs/architecture/architecture.rst`).

## Handling expectations

- Acknowledgment within a few days; this is a solo-maintained project.
- Fixes ship on the normal branch cadence; credited disclosure after
  the fix unless you prefer otherwise.

## Hardening posture

- Credentials are encrypted at rest (Fernet); keys never live in the
  repository.
- CI pins every action (`.github/workflows/actions.lock`) and runs
  secret scanning, dependency audit, and static security analysis
  weekly and on pull requests (`.github/workflows/security.yml`).
- Workflow tokens run read-only by default.
