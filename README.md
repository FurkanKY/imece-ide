# Imece

[![Verify — main](https://github.com/FurkanKY/imece-ide/actions/workflows/verify.yml/badge.svg?branch=main)](https://github.com/FurkanKY/imece-ide/actions/workflows/verify.yml?query=branch%3Amain)
[![Secret scan — main](https://github.com/FurkanKY/imece-ide/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/FurkanKY/imece-ide/actions/workflows/security.yml?query=branch%3Amain)
[Türkçe](README.tr.md) · [Documentation index](docs/README.md) · [Support](SUPPORT.md) · [Privacy](PRIVACY.md)

Imece is a local-first desktop coding workspace: describe a task, choose one
coding agent, inspect its isolated change and verification evidence, then decide
whether to apply or reject it. No planner/reviewer role chain is required.
Applying takes a checkpoint first. Imece has no telemetry or hosted service.

## What is available

- **Latest tag: `v0.4.0-beta.1`** — the latest source-only release; newer
  development features are not included in that tag.
- **`main`** — unreleased development; the checked-in application remains
  IDE-shaped and single-run, with the role-free single-agent flow. Local fixture
  validation is not live-provider or platform acceptance.
- **M2 work** — task-first UI and concurrent runs are implemented on the
  separate [`m2-run-manager` branch](https://github.com/FurkanKY/imece-ide/tree/m2-run-manager),
  not merged into `main`. Check its [current CI results](https://github.com/FurkanKY/imece-ide/actions/workflows/verify.yml?query=branch%3Am2-run-manager)
  before integrating it. Fixture/mock validation is not live-provider or
  native-platform acceptance.

M1's real-provider and supported-platform acceptance remains deferred validation
debt and a release gate before M5. M3–M5 have not started. See the
[authoritative product plan](docs/PRODUCT-PLAN.md).

## Get started from source

Source is currently the only distribution. The desktop shell targets Windows
10/11 and also runs from source on Linux. Use Python 3.14 and Node.js 22 LTS
(22.12 or newer recommended; Vite 7 requires Node 20.19+ or 22.12+).
Full prerequisites and platform notes are in [Setup](docs/SETUP.md).

**Windows PowerShell** (run from the repository root):

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Push-Location web/ui; npm ci; npm run build; Pop-Location
.\.venv\Scripts\python.exe shell.py
```

**Linux Bash** (run from the repository root):

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
(cd web/ui && npm ci && npm run build)
.venv/bin/python shell.py
```

The application UI is currently Turkish. Isolated agent runs require Git on
`PATH`, a Git project, and a compatible provider configured in Settings; setup and optional
provider details are in [Setup](docs/SETUP.md). Agent CLI providers use their
own installed account flow.

## Trust and privacy

Agent edits are made in a separate Git worktree and can be reviewed before
applying. This is **not a security sandbox**: project verification commands and
shell/project code execute with the user's operating-system privileges. Source
provider keys are stored locally in the git-ignored `.env`; packaged Windows
builds use DPAPI. A selected provider receives the context sent for its run.
Collaboration credentials are memory-only, and explicitly shared proposal code
is sent verbatim without automatic redaction. Review before sharing. See the
[privacy statement](PRIVACY.md) and [security policy](SECURITY.md).

## Documentation and community

- [Documentation index](docs/README.md) · [Setup](docs/SETUP.md) · [Usage](docs/USAGE.md)
- [Contributing](docs/CONTRIBUTING.md) · [Product plan](docs/PRODUCT-PLAN.md)
- [Support](SUPPORT.md) · [Code of Conduct](CODE_OF_CONDUCT.md)
- [Discussions](https://github.com/FurkanKY/imece-ide/discussions) · [Security reporting](https://github.com/FurkanKY/imece-ide/security/advisories/new)

Licensed under [Apache-2.0](LICENSE).
