# M5 — packaging engineering, acceptance still open

The user authorized starting M5 **without closing** M1 real-provider/platform,
M3 Windows/native-desktop, or M4 real two-machine LAN acceptance. Those tests
remain deferred/open. This document records engineering work, not a packaged
release, a new version, or successful clean-machine acceptance.

## Implemented packaging slices

- Process-tool, verification and ACP supervisors now share an argv builder.
  Source mode invokes the existing Python script; frozen mode re-enters the
  packaged EXE through `--imece-process-supervisor`, before environment/key/log/
  Qt initialization. Supervisor exit codes are preserved; malformed dispatch
  exits 125 rather than opening a second IDE window. Windows Job imports resolve
  inside the frozen Python package, while direct source scripts keep their
  standalone imports. FD/handle, nonce and private receipt contracts stay intact.
- `packaging/check.py` is a standalone, standard-library-only preflight tool.
  It must **not** become a local `packaging` Python package, which would shadow
  the third-party PyPA dependency used by the application/build tools.
- Source preflight checks the built HTML's local assets, license/notices and
  matching desktop/UI version. Bundle preflight checks the Windows `onedir`
  EXE, `_internal` UI/assets, Node runtime, license/notices and ConPTY helper
  executables. It rejects symlink/reparse entries, private dependency/repository
  directories and known user-data/credential filenames (including DPAPI
  `secrets.dat`). This is a structural filter, **not** a full content-secret or
  dependency-license audit.
- Bundle validation writes a deterministic per-file SHA-256
  `package-manifest.json`, excluding itself. It reports version and platform,
  never machine-specific source paths or an acceptance/readiness claim. The ZIP
  has its separate `SHA256SUMS.txt`. Manifests/checksums provide integrity checks,
  not publisher authentication; Authenticode signing remains open.
- `build.ps1` stops on frontend failure instead of packaging a stale build.
  Source preflight runs before PyInstaller; bundle validation runs afterward.
  `-SkipWebBuild` is an explicit developer option, not a freshness guarantee.
- The Windows package smoke tests the **frozen EXE** helper with system `cmd.exe`,
  positive authenticated quiescence and command exit 7; invalid dispatch must
  exit 125. Desktop checks cover native QWebChannel, settings/key status and real
  ConPTY write/read. No Python/npm executable is exposed on the child PATH.
  No providers, agents or LAN listeners are started.
- Smoke uses disposable USERPROFILE/HOME/APPDATA/LOCALAPPDATA/TEMP paths and a
  minimal environment, not inherited provider credentials or the real user store.
  Its temporary loopback CDP endpoint uses a reserved ephemeral port. This is
  test instrumentation only, not an application collaboration listener.
- Linux packaging engineering now includes `build.sh`, conditional Node `bin/node`
  and POSIX PTY payloads, and XDG user-data paths. Source mode's legacy paths and
  Windows LOCALAPPDATA are unchanged; there is no automatic data migration.
  Linux bundle validation allows only relative, internal, regular-file library
  symlinks and records link targets in the manifest. Directory/absolute/escaping/
  broken/cyclic links are refused; manifest output cannot overwrite a runtime
  via a symlink. Windows still refuses all links/reparse entries.
- Linux has no DPAPI integration. Its frozen `.env` fallback is plaintext, saved
  atomically with owner-only 0600 permissions; symlink credential files are
  refused. New data directories created by key saving use 0700. This does not
  provide encryption or defend against a compromised/privileged local user.
- The shared smoke harness supports Linux FD-bound subreaper receipts and POSIX
  PTY write/read, with disposable HOME/XDG paths and an empty development PATH.
  CI supplies a temporary Xvfb display. Local smoke can use an explicit xcb/
  wayland/offscreen platform; Wayland connects to the host socket while retaining
  disposable user-data/runtime directories. Bounded stderr and signal/close
  detection make early failures visible. Offscreen is not native-display proof.
  Both the source-dispatch fixture and the actual frozen package were exercised.
- `packaging/dependencies.py` inventories only installed runtime dependency
  closure offline (45 distributions here, including installed optional typesafe-sdk), including extras
  and platform markers. It emits bounded license evidence, no installation paths
  or URLs; unknown evidence is flagged for review. The spec includes runtime
  distribution metadata/license payloads. Current ACP 0.12.1's Apache-2.0 license
  text was inspected and notices updated; final artifact/license/secret review
  is still required. The inventory itself installs nothing or calls no external
  API. After explicit user authorization, PyInstaller and its build dependencies
  were installed in `.venv`; no provider/LAN API was contacted.
- The manual **M5 Windows and Linux package smoke** workflow produces engineering
  Windows ZIP and Ubuntu 24.04 x86_64 tar.gz, checksums, file manifests, dependency
  inventories and smoke reports. Publication waits for **both** platform jobs. `publish=false` is the
  default. Publication additionally requires explicit `acceptance_confirmed=true`
  and an exact application-version tag; neither flag proves the tests passed.
  Maintainers must actually satisfy the release checklist before confirming.
  No workflow, tag, commit, push or release was triggered in this session.

## Running checks

Local source preflight needs an already-built frontend:

```sh
python packaging/check.py version --root .
python packaging/check.py sources --root .
```

Windows, using the existing build environment:

```powershell
packaging/build.ps1
$env:IMECE_PACKAGE_SMOKE_REPORT = 'package-smoke.json'
node packaging/smoke.mjs
```

Linux, with an existing build environment:

```sh
bash packaging/build.sh
IMECE_PACKAGE_SMOKE_REPORT=package-smoke.json xvfb-run -a node packaging/smoke.mjs
python packaging/dependencies.py --requirements requirements.txt --output DEPENDENCIES.json
```

Frozen Linux data defaults to `~/.local/share/ImeceIDE`, or an absolute
`XDG_DATA_HOME/ImeceIDE`; relative XDG values are ignored. Linux archives preserve
executable modes and internal file links. Other distributions/architectures are
not certified by the Ubuntu 24.04 x86_64 job. Git/provider/project toolchains and
system Qt/X11 libraries remain explicit external prerequisites.

Build tools and dependencies must already be installed, as described in
[SETUP.md](SETUP.md). The generated package contains Python/Node runtime payloads,
but **Git must be separately installed and available on PATH for native tasks,
worktrees and SCM**. Provider CLIs are also external prerequisites when selected;
an API provider does not require such a CLI. A repository's own verification
commands may require its language/toolchain. Minimal-PATH smoke is not proof
that a real task needs no external tools.

## Independent Linux evidence for this slice

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS \
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen \
  PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_supervisor_bootstrap.py tests/test_packaging_checks.py \
  tests/test_packaging_spec.py tests/test_package_supervisor_smoke.py \
  tests/test_dependency_inventory.py tests/test_packaged_linux_keys.py \
  tests/test_packaged_graphics.py \
  tests/test_keys.py tests/test_secret_store.py tests/test_runtime_paths.py \
  tests/test_windows_job.py tests/test_process_runtime.py \
  tests/test_acp_client.py tests/test_acp_fake_agent_integration.py
node --check packaging/smoke.mjs
node --check packaging/supervisor-smoke.mjs
```

Earlier scope: **238 passed, 5 platform skips**. Additional inventory/content/license/delivery regression evidence and the latest repeatable delivery are tracked in [M5-AUDIT.md](M5-AUDIT.md). Coverage includes fixture bundle
validation, known-secret filtering, correct PyInstaller `_internal` resource
layout, package import compatibility, actual Linux helper dispatch before UI,
authenticated process drain, Node FD smoke against real source dispatch,
platform-specific spec wiring, Linux private key-file permissions, offline
inventory and existing process/ACP contracts. Source preflight passed against
the current built UI. Standalone Node checks establish syntax only.

## Actual local Linux package and manual handoff

After explicit installation/build authorization, `bash packaging/build.sh` ran
without skipping the frontend build. PyInstaller **6.22.3**, Python **3.12.3**,
Ubuntu 24.04 x86_64/glibc 2.39 produced `dist/ImeceIDE`. Structural preflight and
manifest generation passed. Native **Wayland** frozen smoke passed: supervisor
nonce/ECHILD proof and exit 7, invalid-dispatch exit 125, app:// assets,
QWebChannel, settings/key status, isolated user paths, real PTY write/read,
no browser console errors. The child had no development Python/npm on PATH.
The full matching Node runtime/third-party license is vendored and shipped
under `_internal/licenses`; builds refuse an unreviewed version without its
matching license payload (the wheel wrapper's license alone is insufficient).
Bundled Node **24.19.0**, frozen debugpy **1.8.22** version dispatch and a frozen
LSP initialize → shutdown/exit handshake also passed with an empty PATH.

Evidence resides in ignored `dist/`: `package-smoke.json`, `helper-smoke.json`,
`DEPENDENCIES.json`, per-file `ImeceIDE/package-manifest.json`, and the local
manual-test archive plus `SHA256SUMS.txt`. The archive was extracted outside
this repository: native Wayland/supervisor/PTY smoke passed again with a private
empty-PATH child; the earlier archive's **9091 file hashes and 134 internal links** matched its manifest. The refreshed archive adds licensing/provenance payloads; its current manifest/receipt is authoritative.
This proves local extraction/runtime independence, not a second clean machine.
`BUILD-INFO.json` records the dirty,
uncommitted source state: HEAD alone is **not** the artifact identity. No tag,
commit/push, workflow dispatch or public release was made. See
[MANUAL-ACCEPTANCE.md](MANUAL-ACCEPTANCE.md) for manual testing and result capture.

GPU-backed Wayland startup intermittently crashed/hung. Bounded GDB probes did
not establish a definitive upstream root cause; passing retries alone were not
accepted as a fix. Frozen Linux now defaults to Chromium `--disable-gpu`, with an
explicit hardware opt-in and unchanged source/Windows defaults. Six consecutive
actual frozen Wayland checks completed with UI/PTY success and **normal
window.confirmClose → closeEvent → exit 0**, followed by another final pass.
This is a tested compatibility mitigation, not certification of the hardware
rendering path. Chromium sandbox and process supervision remain enabled.

The system has no `libxcb-cursor0`; the initial X11 attempt failed. The harness
now properly selects the available native Wayland socket, which passed. X11
requires its documented external Qt/XCB dependencies; it is **not** accepted by
this Wayland result. Some Qt/QML resource warnings remain nonfatal; the actual UI
and terminal checks passed. Neither local smoke nor helper handshakes establish
clean-machine/other-distribution support, provider execution or full M3/M4
acceptance. No current Windows EXE was built or native Windows smoke run here.

## Open release gates

- [x] Current local Linux build, structural manifest and Wayland frozen desktop/
  supervisor smoke; this is engineering evidence, not full supported-platform
  or clean-machine acceptance.

- [ ] Run the current Windows build/preflight and isolated frozen smoke; retain
  artifact hashes, report and exact runner/commit/version information.
- [ ] Clean Windows machine: extract without Python/npm, install explicit Git/
  selected-provider prerequisites, run a real task, verify/apply/rollback.
- [ ] Native Windows close/restart/worktree, no-reparse paths, Job and ACP tests;
  display scaling/IME/editor/terminal usability checks.
- [ ] Real provider and supported-platform end-to-end acceptance (M1 debt).
- [ ] Real two-machine authenticated LAN and explicit proposal integration (M4).
- [ ] Linux supported-platform acceptance and an explicitly tested packaging
  strategy before claiming packaged support there.
- [x] Repeatable artifact integrity/content and source/history signature checks; staged Python/frontend/font/native/bootloader license texts. See [M5-AUDIT.md](M5-AUDIT.md).
- [ ] Final legal redistribution/source review (including hook-collected Qt/Chromium add-ons and Windows native payloads); stable-release signing.
- [ ] Confirm acceptance, authorize a release/version/tag separately; do not infer
  publication authority from engineering checks or deferred gates.
