# Release Guide

## Current release

**v0.4.0-beta.1** — source-only release; installation is described in the
[README](../README.md). The Windows `onedir` packaging pipeline
(`packaging/build.ps1` + the **Build Windows beta release** workflow) is
implemented and CI-verified, but publishing prebuilt binaries is postponed
until the beta stabilizes. The rest of this guide applies when binary
releases resume.

## Quick start for end users (packaged app)

1. Run `ImeceIDE.exe` from the extracted package.
2. Choose **Open Folder** on the welcome screen and select a project.
3. In Settings → Model providers, add a provider from the catalog and enter
   its API key ("Test & save" validates it first).
4. To use an agent CLI (Claude Code, Gemini CLI, Codex CLI, Qwen Code),
   install it and make sure it is on `PATH`.
5. Type a task in the team panel; review the proposal as a diff and confirm
   with **Apply**. Every run has a change receipt available from history.

Python and Node are not required to use the packaged app. Keys, preferences
and logs live under `%LOCALAPPDATA%/ImeceIDE`.

## Release checklist

- [ ] Rebuild the package on Windows with `packaging/build.ps1`.
- [ ] Run `node packaging/smoke.mjs`; verify QWebChannel, settings/keys and
      the PTY write→read result are clean.
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
      **Build Windows beta release** GitHub Actions workflow, which builds,
      smokes, zips, checksums and publishes the release.

## Known limits

- This beta is Windows-only; there is no auto-update.
- The package is large (it bundles QtWebEngine, the Python language server
  and terminal helpers).
- Hosted API providers require their own API keys; agent CLIs (Claude Code,
  Gemini CLI, Codex CLI, Qwen Code) require their own installation and
  account. Ollama needs a locally running server.
- Single dark theme. A light theme, split editor, token cost dashboard,
  inline AI editing and autonomy levels are on the v1 scope.
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

Packaging runs only in Windows PowerShell:

```powershell
packaging/build.ps1
node packaging/smoke.mjs
```
