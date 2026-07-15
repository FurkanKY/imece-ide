# Contributing

Thanks for your interest in Imece IDE. This document explains how to get a
working dev environment, how changes are verified, and the few rules the
codebase holds strictly.

## Dev environment

Follow [SETUP.md](SETUP.md). In short: Python 3.14 venv +
`requirements-dev.txt`, Node ≥ 20 for `web/ui`, and optionally one configured
model provider for end-to-end AI runs. The desktop shell targets Windows;
the engine and frontend build also work on Linux/macOS for development.

## Verify your change

Every change should pass the following before a PR:

```bash
cd web/ui && npm run typecheck && npm run build && cd ../..
python -m pytest -q
```

- **UI changes** additionally need a visual check:
  `node tools/webshot.mjs` renders the mock-bridge UI in real Chromium and
  writes `.uishots/*.png` (Monaco/xterm included). UI work is not "done"
  until the screenshots have been looked at. For the real app use
  `python shell.py --dev`.
- **Bridge/engine changes:** `python -m pytest tests/test_bridge.py -q` runs
  the contract tests without a webview.
- CI runs the same typecheck/build (Ubuntu) and pytest (Windows) on every
  push and PR, plus a gitleaks secret scan.

## Documentation rule

A change that affects users or contributors updates the relevant document in
the same PR — otherwise it isn't done:

| Change | Update |
|--------|--------|
| New model/provider behavior | `README.md` + `docs/SETUP.md` |
| New module, layer, event type, data flow | `docs/ARCHITECTURE.md` |
| New bridge method/event (webhost ↔ web/ui) | `web/ui/src/bridge/protocol.ts` (single source) + `docs/ARCHITECTURE.md` |
| New UI feature or usage pattern | `docs/USAGE.md` |
| Install, dependency or environment note | `docs/SETUP.md` + `requirements*.txt` |
| User-visible release impact | `docs/CHANGELOG.md` |
| Keys, file writes, command execution or data leaving the machine | `PRIVACY.md` and, if needed, `SECURITY.md` |

## Code rules

- **Design tokens:** all visual values come from
  `web/ui/src/styles/tokens.css`; raw hex/px in components is not accepted.
  Add a missing semantic token instead.
- **Motion:** every new animation must respect the OS reduced-motion
  preference and the in-app `animations` setting.
- **Engine compatibility:** `adapters.py`, `providers.py`, `agents.py`,
  `runner.py`, `project_runner.py` and `project.py` may only be extended
  backward-compatibly (new parameters need defaults; smoke-test
  `orchestrator.py` and `app.py` after touching them). `adapters.PROVIDERS`
  and the `LLMResponse` shape are stable contracts; new hosted providers are
  catalog entries in `providers.py`, not new adapter functions.
- **Subprocesses:** always `encoding="utf-8", errors="replace"`, plus
  `PYTHONUTF8=1` where output is decoded (see SETUP.md, "Windows development
  notes").
- **Turkish text:** never use CSS `text-transform` on UI strings (the Turkish
  İ/i problem); uppercase with `toLocaleUpperCase("tr")`.
- **Protected contracts** (change only with a verified need and matching
  tests): `web/ui/src/bridge/protocol.ts`, the `run.event` schema in
  `project_runner.py`, `Project._safe()` path safety, the checkpoint snapshot
  format, and the frameless-window bridge methods in `webhost/window.py`.

## Pull requests

- Keep PRs focused; describe the user-visible effect and the verification you
  ran.
- New dependencies or embedded assets must have an open-source license
  (Apache/MIT/BSD/OFL preferred) and an entry in
  [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md).
- Security issues never go through public issues/PRs — see
  [SECURITY.md](../SECURITY.md).
