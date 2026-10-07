# Release Guide

## Current release

**v0.4.0-beta.1** — the current public **source-only** release. There are no
published binaries: installation is described in the
[README](../README.md). The Windows `onedir` packaging pipeline
(`packaging/build.ps1`, `packaging/build.sh` and the **M5 Windows and Linux
package smoke** workflow) exists.
M5 packaging engineering is now authorized with earlier acceptance gates kept
open; the current changes have **not** been exercised as a Windows package here.
Publishing prebuilt binaries is still postponed until real acceptance passes.
See [M5-PACKAGING.md](M5-PACKAGING.md).

Nothing in this guide announces a new release, a tag, or a binary. There is no
v0.5 and no date. The role-free single-task default flow described in the
[README](../README.md) and [PRODUCT-PLAN.md](PRODUCT-PLAN.md) is
**unreleased development work** on top of `v0.4.0-beta.1`, not a shipped
feature. Real-provider and supported-platform acceptance is still an **open
gate** — it must be met before any packaged release (milestone M5).

## Quick start for end users (packaged app)

1. Run `ImeceIDE.exe` from the extracted package.
2. Choose **Open Folder** on the welcome screen and select a project.
3. In Settings → Model providers, add a provider from the catalog and enter
   its API key ("Test & save" validates it first).
4. To use an agent CLI (Claude Code, Gemini CLI, Codex CLI, Qwen Code),
   install it and make sure it is on `PATH`.
5. Type a task in the team panel; review the proposal as a diff and confirm
   with **Apply**. Every run has a change receipt available from history.

For a future accepted package, Python and Node runtimes are bundled. Git must
be separately installed and on PATH for native tasks/worktrees/SCM; project
verification commands may need their own toolchains. Keys, preferences
and logs live under `%LOCALAPPDATA%/ImeceIDE`.

## Release checklist

- [ ] Rebuild the package on Windows with `packaging/build.ps1`.
- [ ] Run `node packaging/smoke.mjs`; verify QWebChannel, settings/keys and
      the frozen supervisor authenticated receipt, isolated user-data paths and
      the PTY write→read result are clean. Retain the smoke report and manifest.
- [ ] Short visible tour with the packaged EXE: open a folder, edit/save a
      file, type into the terminal, check key status in Settings, close and
      reopen.
- [ ] Check titlebar, panels, Monaco, terminal, dialogs and toasts for
      overflow at 125% and 150% Windows scaling.
- [ ] Type file names, searches, commit messages and team tasks with a
      Turkish IME; verify `İ/i/ş/ğ/ü/ö/ç` characters.
- [ ] Produce `SHA256SUMS.txt` and verify the ZIP hash.
- [ ] Secret scan on git history and a dependency/license review are clean.
- [ ] When everything passes, create the git tag and run the manual
      **M5 Windows and Linux package smoke** GitHub Actions workflow. It builds,
      smokes, zips and checksums; publication defaults off and additionally
      requires explicit acceptance confirmation. Build-only artifacts do not
      close any deferred gate.

## Known limits of the published source-only release

The points below describe the public release, not the unreleased M3/M4/M5
engineering on this branch; consult [PRODUCT-PLAN.md](PRODUCT-PLAN.md) for that
status. No new binary release or acceptance is announced here.

- This beta is **Windows-first** and usable from source on Linux; there is no
  auto-update.
- **Real-provider and supported-platform acceptance is an open gate.** The
  role-free default flow is verified by local fixture runs only. Live-provider
  quality, and Windows/Linux end-to-end acceptance, are **not** demonstrated and
  are **not** claimed. Windows-native evidence fingerprinting has a documented
  capability limitation (see [SECURITY.md](../SECURITY.md)); where the required
  no-follow inventory capability is missing, a PASS cannot be proven.
- **One active run at a time.** Concurrency and restart durability are later
  milestones and are **not implemented**. See
  [PRODUCT-PLAN.md](PRODUCT-PLAN.md).
- The package is large (it bundles QtWebEngine, the Python language server
  and terminal helpers).
- Hosted API providers require their own API keys; agent CLIs (Claude Code,
  Gemini CLI, Codex CLI, Qwen Code) require their own installation and
  account. Ollama needs a locally running server. Not every catalog entry can
  drive the single-agent flow; the UI refuses an unsupported provider with a
  reason instead of silently substituting another.
- Collaboration is an **experimental, loopback-only** foundation, not a team
  product: no LAN pairing, no LAN/WAN deployment, no TLS.
- The Git surface covers local status, stage/unstage, discard, diff and
  commit; remote push/pull/branch operations are not included.
- The package is unsigned; release notes must explain the SmartScreen warning
  and checksum verification. Unsigned PyInstaller executables are also prone
  to antivirus false positives (Defender can quarantine the EXE mid-run).
  Authenticode signing is a stable-release gate.

## Developer verification

```bash
cd web/ui
npm ci
npm run typecheck
npm run build
cd ../..
python -m pytest -q
```

Packaging engineering commands (not a claim of accepted binaries):

```powershell
# Windows, PowerShell
packaging/build.ps1
node packaging/smoke.mjs
```

```sh
# Linux; existing build dependencies and Xvfb required
bash packaging/build.sh
xvfb-run -a node packaging/smoke.mjs
```

A local Linux manual-test artifact was built and passed Wayland frozen smoke
using the explicitly authorized PyInstaller environment. It is not a public
release. Windows frozen build and clean-machine/provider/LAN acceptance remain
open. See [MANUAL-ACCEPTANCE.md](MANUAL-ACCEPTANCE.md).
