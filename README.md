# Imece IDE

> Public beta · `v0.3.0-beta.1` · source release, Windows-first · [Türkçe](README.tr.md)

**Imece** is a local-first desktop coding workspace where a team of AI agents —
a Planner, a Coder and a Reviewer, each backed by the model you choose —
collaborates on a change and presents it to you as a reviewable diff before a
single file is touched. The name comes from *imece*, the Turkish tradition of
a community pooling its labor for one shared task.

Around that workflow, Imece is a real IDE: project explorer, Monaco editor,
integrated terminal, Git surface, Python language intelligence and debugging,
checkpoints with undo, and a persistent change receipt for every AI run.

![Reviewing an AI-proposed change in Imece IDE](docs/assets/review.png)

## How it works

1. Open a local project folder and describe a task.
2. The Planner scopes the change, the Coder writes it, the Reviewer checks it —
   each role can run on a different provider (Claude, DeepSeek or Gemini), with
   per-step latency, token and cost metrics.
3. You inspect the proposed diff file by file, then **Apply** or **Reject**.
   Applying takes a checkpoint first, so one click undoes it.
4. Every run leaves a change receipt: task, plan, diff, verdict, cost, and
   apply/checkpoint state.

## Get started

Imece IDE is currently distributed as **source only** — no prebuilt binaries
yet. The desktop shell targets **Windows 10/11**; you need Python 3.14 and
Node ≥ 20 (details in [SETUP.md](docs/SETUP.md)).

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
cd web/ui && npm ci && npm run build && cd ../..
python shell.py
```

Add a DeepSeek and/or Gemini API key to a local `.env` file (see
[SETUP.md](docs/SETUP.md)); using Claude requires a separately installed
[Claude Code CLI](https://claude.com/claude-code). Then open a project
folder, describe a task, inspect the suggested diff, and choose Apply or
Reject.

> **Language note:** the application UI is currently Turkish. An English UI is
> planned; all documentation is in English.

## Trust and privacy

- No telemetry, analytics or automatic error reporting.
- Model calls happen only when you start an AI run; the selected context goes
  to the provider assigned to that role and nowhere else.
- API keys stay on your machine: a git-ignored `.env` when running from
  source, Windows DPAPI encryption in packaged builds.
- A project-provided run command (F5) is shown for approval before its first
  run, and re-approved whenever it changes.
- Agents cannot write outside the project folder you opened.

Read the full [privacy statement](PRIVACY.md) and
[security policy](SECURITY.md).

## Packaging

A Windows `onedir` package can be built with PyInstaller and is verified in
CI, but official binary releases are postponed until the beta stabilizes —
unsigned executables are also prone to antivirus false positives. To build
one yourself, use Windows PowerShell:

```powershell
packaging/build.ps1
node packaging/smoke.mjs
```

## Documentation

- [Setup](docs/SETUP.md) · [Usage](docs/USAGE.md) ·
  [Architecture](docs/ARCHITECTURE.md)
- [Release guide](docs/RELEASE.md) · [Changelog](docs/CHANGELOG.md)
- [Contributing](docs/CONTRIBUTING.md) · [Support](SUPPORT.md) ·
  [Code of Conduct](CODE_OF_CONDUCT.md)
- [Third-party notices](THIRD-PARTY-NOTICES.md)

Licensed under [Apache-2.0](LICENSE).
