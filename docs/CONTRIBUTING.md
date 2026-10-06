# Contributing

Thanks for your interest in Imece IDE. This document explains how to get a
working dev environment, how changes are verified, and the few rules the
codebase holds strictly.

## What this project is right now

Imece is a **local-first, source-only** desktop coding workspace. The default
flow is **role-free**: one task, one selected provider, one independent coding
agent working in an isolated Git worktree, deterministic verification evidence
behind the result, and an explicit apply/reject decision by you. The legacy
Planner/Worker/Reviewer trio remains only as a compatibility backend.

That vertical is **implemented and verified by local fixture end-to-end runs**,
and it is **not closed**: real-provider and supported-platform acceptance is an
outstanding gate, tracked as validation debt in
[PRODUCT-PLAN.md](PRODUCT-PLAN.md). Two rules follow, and they are binding on
how anything is written here:

- **No percentages, no ETAs, no "X% done", no day counts.** Tests are not the
  progress metric — a green suite is not milestone closure.
- **"Offline/local" is not "accepted".** Fixture, mock-bridge and offline
  regression results are real engineering evidence, but they never substitute
  for a real-provider run or for supported-platform acceptance. Describe which
  kind of evidence you have, and keep the gap visible.

These documents are the **public application and contributor docs**. Internal
planning, product-strategy, design-work and agent-session/journey records stay
local and unpublished — keep that narration (session roles, review choreography,
per-run case counts) out of the published set.

## Dev environment

Follow [SETUP.md](SETUP.md). In short: Python 3.14 venv with
`requirements-dev.txt`, Node 22 LTS (22.12+ recommended; Vite 7 supports
20.19+/22.12+) for `web/ui`, and optionally a configured provider for an
explicit live run. The desktop shell targets Windows and runs from source on
Linux; macOS desktop support is not claimed.

## Verify your change

Every change should pass the following before a PR:

```bash
(cd web/ui && npm run typecheck && npm run build)
.venv/bin/python -m pytest -q
```

On Windows PowerShell use `.\.venv\Scripts\python.exe -m pytest -q`; run the
frontend commands from `web/ui` (`npm run typecheck`, then `npm run build`).

- **UI changes** additionally need a visual check:
  `node tools/webshot.mjs` renders the mock-bridge UI in real Chromium and
  writes `.uishots/*.png` (Monaco/xterm included). UI work is not "done"
  until the screenshots have been looked at. For the real app use
  `.venv/bin/python shell.py --dev` on Linux, or
   `.\.venv\Scripts\python.exe shell.py --dev` on Windows.
- **Bridge/engine changes:** `.venv/bin/python -m pytest tests/test_bridge.py -q` runs
  the contract tests without a webview.
- Documentation links can be checked with `.venv/bin/python tools/check_repo_hygiene.py`
  (or the Windows venv interpreter). The checker uses Git's tracked-file index:
  stage intended new documentation files first. It validates local file and
  directory targets, not remote URLs or Markdown anchors.
- The verification workflow builds/typechecks the frontend and runs Python tests on Ubuntu,
  runs the Windows collected-test suite in eight shards, and scans full Git
  history for secrets, with a separate local documentation-link check.
  **A green CI is not platform acceptance** — in particular the
  Windows job is not a demonstration that the evidence fingerprint works
  natively on Windows; see the known limitation in
  [SECURITY.md](../SECURITY.md).
  Linux runs the complete suite in one process. Windows partitions the collected
  tests into eight deterministic collected-test shards; every collected test belongs to exactly
  one shard. Local runs without shard options still run the complete suite. To
  reproduce one shard:

  ```bash
  .venv/bin/python -m pytest --ci-shard-index=0 --ci-shard-count=8
  ```
- **The default suite makes no external provider calls.** Some integration
  fixtures use owned loopback sockets and local child processes. Model and backend tests use
  fakes and injected transports; the decision layer's optional real-SDK tests
  run the actual `typesafe-sdk` client against an in-process
  `httpx2.MockTransport` (still no network) and skip themselves when the SDK
  is not installed. Tests that really call a provider API are opt-in only and
  stay skipped unless you explicitly ask for them, because they spend your
  quota:

  ```bash
  IMECE_RUN_LIVE_API_TESTS=1 .venv/bin/python -m pytest -q
  ```

  Leave that variable unset for normal work and in CI.
- **"Offline" does not mean "identical on every machine."** Some areas are
  environment-dependent by nature: ACP/process tests spawn local processes and
  can be sensitive to what is installed locally, and individual cases are
  skipped or specialized for `git` on `PATH`, symlink support and
  Windows/POSIX path semantics. Expect a local run to differ from CI in
  coverage, not necessarily in outcome — report platform-specific failures with
  the OS and tool versions.
- **On Linux**, a `PYTHONPATH` exported by an unrelated toolchain (e.g. ROS)
  can break pytest's plugin loading; run the suite as
  `env -u PYTHONPATH QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`
  (see [SETUP.md](SETUP.md#pre-set-pythonpath-from-another-toolchain-breaks-pytest)).

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
| Milestone scope, status or acceptance change | `docs/PRODUCT-PLAN.md` (authoritative) |
| Collaboration/session/proposal behaviour | `docs/COLLABORATION.md` |
| Decision-layer modes, allowlist or fallback | `docs/DECISION-LAYER.md` + `PRIVACY.md` |
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
