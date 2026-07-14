# Multi-Agent IDE

> Windows public beta · `v0.3.0-beta.1` · [Türkçe](README.tr.md)

Multi-Agent IDE is a local-first desktop coding workspace where a Planner,
Coder and Reviewer collaborate on a change before you apply it. It combines a
project explorer, Monaco editor, terminal, Git surface, Python LSP/debugging,
reviewable diffs, checkpoints and a persistent change receipt.

## Get started

1. Open the latest GitHub Release and download `MultiAgentIDE-windows.zip`.
2. Verify its SHA-256 against `SHA256SUMS.txt`, extract it, then run
   `MultiAgentIDE.exe`.
3. Open a local project folder. Add a DeepSeek and/or Gemini key in Settings;
   Claude requires a separately installed Claude Code CLI.
4. Describe a task, inspect the suggested diff, then choose Apply or Reject.

This beta is Windows-only and the binary is **unsigned**. Windows SmartScreen may
show a warning. The source is available for review and local builds.

## Trust and privacy

- No telemetry, analytics, or automatic error reporting.
- Model calls happen only when you start an AI run; selected context goes to the
  provider assigned to that role.
- Packaged-app API keys are protected with Windows DPAPI for the current user.
- A project-provided F5 command is shown for approval before its first run, and
  is requested again if the command changes.
- Each AI run has a project-local change receipt with plan, diff, review,
  metrics, apply/checkpoint state and optional Markdown export.

Read the full [privacy statement](PRIVACY.md) and [security policy](SECURITY.md).

## Build from source

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
cd web/ui && npm ci && npm run build && cd ../..
python shell.py
```

For a distributable package, use Windows PowerShell:

```powershell
packaging/build.ps1
node packaging/smoke.mjs
```

## Documentation and contributing

- [Setup](docs/SETUP.md) · [Architecture](docs/ARCHITECTURE.md) · [Usage](docs/USAGE.md)
- [Release checklist](docs/RELEASE.md) · [Changelog](docs/CHANGELOG.md)
- [Contributing](docs/CONTRIBUTING.md) · [Support](SUPPORT.md) · [Code of Conduct](CODE_OF_CONDUCT.md)

Licensed under [Apache-2.0](LICENSE).
