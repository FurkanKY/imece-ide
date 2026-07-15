# Changelog

This file records public, user-visible releases. Development notes and internal
planning records are intentionally not part of the published repository.

## v0.3.0-beta.1

- First public beta of Imece IDE, released as source only. The Windows
  packaging pipeline exists and is CI-verified, but prebuilt binaries are
  postponed until the beta stabilizes.
- Local Planner, Coder and Reviewer workflow with reviewable diffs, apply/reject
  controls, checkpoints and project-local change receipts.
- API keys stay local: a git-ignored `.env` for source runs, DPAPI encryption
  in packaged builds. Project F5 commands require explicit approval when first
  seen or changed.
- Local project explorer, Monaco editor, terminal, Git status, Python language
  support and debugging surface.

## Security and privacy

- No telemetry, analytics, or automatic error reporting.
- Model requests are started by the user and use the provider assigned to each
  role. See [PRIVACY.md](../PRIVACY.md) and [SECURITY.md](../SECURITY.md).
