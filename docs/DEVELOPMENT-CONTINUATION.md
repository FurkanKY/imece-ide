# Continue development on another computer

This is a development checkpoint, not an accepted binary release. Use the
`work/m3-m5-continuation` branch. Do not merge to the default branch, tag,
dispatch packaging/publication workflows or publish a release without separate
authorization.

## Fresh checkout

Install Git, Python 3.14 and Node.js 22. See [SETUP.md](SETUP.md) for platform
prerequisites, Qt libraries and provider CLI setup. Recreate dependencies on the
new computer; never copy `.venv`, `node_modules`, compiled frontend or frozen
packages between operating systems.

### Linux

```bash
git clone --branch work/m3-m5-continuation https://github.com/FurkanKY/imece-ide.git
cd imece-ide
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cd web/ui
npm ci
npm run typecheck
npm run build
cd ../..
.venv/bin/python shell.py
```

### Windows PowerShell

```powershell
git clone --branch work/m3-m5-continuation https://github.com/FurkanKY/imece-ide.git
Set-Location imece-ide
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Set-Location web/ui
npm ci
npm run typecheck
npm run build
Set-Location ../..
.\.venv\Scripts\python.exe shell.py
```

Runtime Python requirements have version ranges: a fresh install is not proof
of byte-for-byte reproduction of the original environment. The frontend uses
`npm ci` and its committed lockfile. Build-only dependencies are separate:
`requirements-build.txt`; optional Jev support is `requirements-jev.txt`.

## What transfers, what does not

Commits carry source, tests, build scripts and documentation, not this chat or
local agent state. `.pi/`, `PI_HANDOFF.md`, `.env`, dependencies and build outputs
are deliberately excluded. Configure secrets and CLI logins locally; do not
commit them or paste them into a handoff.

Existing task history, worktree ownership, workspace leases and collaboration
credentials are machine-local authority, not portable development state. Do not
copy their SQLite/worktree files and attempt an automatic restart/adoption on
the new computer. Open the new checkout as a normal project; existing pending
runs remain on their original computer.

## Current engineering checkpoint

- M3: explicit retry/restart, durable ownership, producer quiescence and verified
  native candidate apply/rollback are implemented. Never weaken authority checks
  to make a platform test pass.
- M4: pinned TLS control/proposal planes, explicit participant delivery and
  owner-selected verified integration are implemented; no automatic listeners,
  agent execution, proposal publication or Source integration.
- M5: Linux build, scoped Qt payload, license/provenance inventories, selected
  credential scanning, manifest-bound frozen GUI/helper smoke and engineering
  archive delivery are implemented. Final local offline Python regression:
  **4155 passed, 13 skipped**, in eight disjoint CI shards. A separate extracted
  Linux archive passed audit, Node/debugpy/LSP, Wayland UI/PTY and normal close.
- These results do not establish actual native Windows/frozen EXE execution,
  clean-machine support, real-provider acceptance, real two-machine LAN,
  hardware rendering or final redistribution/legal acceptance.
- Frozen Linux defaults to software rendering after observed hardware startup
  instability; hardware mode is experimental, not a proven repair.

Start with [PRODUCT-PLAN.md](PRODUCT-PLAN.md), [M5-AUDIT.md](M5-AUDIT.md),
[M4-DELIVERY.md](M4-DELIVERY.md) and [MANUAL-ACCEPTANCE.md](MANUAL-ACCEPTANCE.md).
Keep unrun acceptance items open and fix review failures with focused tests.

## Offline verification

For a focused Linux check:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen \
  IMECE_RUN_LIVE_API_TESTS=0 PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_packaged_helper_protocol.py tests/test_shared_candidate_edges.py
```

The full suite can use `--ci-shard-index=0 --ci-shard-count=8`; run all indices
0 through 7, not just one shard, before calling it a full-suite pass. Native
Windows skips on Linux are not native Windows evidence. GitHub Verify runs on
branch pushes; the packaging/release workflow is manual and is not dispatched
by this checkpoint.
