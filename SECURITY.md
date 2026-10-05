# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or leaked credential.
Use this repository's **Private Vulnerability Reporting** channel under the
repository's GitHub **Security** tab instead. Include reproduction steps,
affected version and impact; do not include real API keys or private source
files.

## Supported line

The supported public line is the current source beta, **`0.4.x`**
(`v0.4.0-beta.1`), plus development work on the unreleased tree toward the next
version. It ships as **source only** — there are no official prebuilt binaries
today. Earlier `0.3.x` and below are no longer the supported line.

## Scope and known platform limits

- **The default flow is role-free.** One task, one selected provider, one
  independent agent working in an isolated Git worktree, deterministic
  verification evidence, then an explicit apply/reject decision. The legacy
  Planner/Worker/Reviewer trio remains only as a compatibility backend.
- **Real-provider and supported-platform acceptance is still an open gate.** The
  default flow is exercised by local fixture runs; no live-provider,
  live-readiness or Windows/Linux quality guarantee is claimed in this
  repository.
- **Windows native fingerprinting is a known limitation.** The "no-follow"
  evidence fingerprint relies on capabilities that are not equally available on
  Windows. Where the required directory-handle capability is missing, the run's
  input inventory is treated as incomplete and a PASS cannot be proven. This is
  a documented product limitation — it is **not** a claim that Windows is
  CI-verified.
- **Windows stream shutdown can wait for the idle read timeout.** Lifecycle
  shutdown is requested immediately, but buffered Winsock readers can drain at
  the fixed five-second idle timeout. Ownership is retained until the worker
  finishes; this is not a sub-second shutdown or live-readiness guarantee.
- **Collaboration is loopback-only and experimental.** The collaboration
  listener binds literal `127.0.0.1` and requires an explicit per-epoch member
  credential. **LAN pairing, LAN/WAN deployment and TLS are not implemented**,
  and no unauthenticated listener is ever exposed. Do not port-forward or
  tunnel the listener. See [docs/COLLABORATION.md](docs/COLLABORATION.md).
- **The optional decision layer** ("Jev / TypeSafe") is **off by default** and
  sends an allowlisted subset to one fixed endpoint only after explicit opt-in.
  It is not a mandatory step and not a role in the product. See
  [PRIVACY.md](PRIVACY.md) and
  [docs/DECISION-LAYER.md](docs/DECISION-LAYER.md).

## Trust and consent model

Imece executes code you and your agent choose to execute. Concretely:

- **Trust the agent's output like any other generated code.** Review the diff
  before applying it. Applying takes a checkpoint first and is reversible.
- **Verification is not a sandbox.** Running a project's own test command runs
  that project's code on your machine, with your permissions. A green
  verification result is evidence about the run — it is not a security
  boundary, and it is not a guarantee that the project is safe to execute.
- **Shared context and shared proposals are deliberate transfers.** Explicitly
  selected code is shared **verbatim** — there is **no automatic redaction** of
  secrets inside the files you select, so review your selection. Treat
  collaborators and their model providers as trusted parties; context
  provenance hashes are integrity checks, **not authenticity**.
- **Credentials stay out of prompts, the frontend and error text**, but the API
  credential you enter is transmitted to the **provider you selected**, over
  that provider's own auth protocol (or used by an agent CLI against its own
  account). Imece IDE runs no hosted service of its own. See
  [PRIVACY.md](PRIVACY.md).
- **No telemetry, analytics or automatic error reporting.** If you find
  otherwise, that is a reportable finding.
