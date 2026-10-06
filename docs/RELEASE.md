# Release Guide

## Current release

**v0.4.0-beta.1** — the latest public **source-only** release. There
are no published binaries: installation is described in the
[README](../README.md). The Windows `onedir` packaging pipeline
(`packaging/build.ps1` + the manual **Build Windows beta release** workflow)
exists, but that workflow has not published a binary release.

Nothing in this guide announces a new release, a tag, or a binary. There is no
v0.5 and no date. The role-free single-task default flow described in the
[README](../README.md) and [PRODUCT-PLAN.md](PRODUCT-PLAN.md) is
**unreleased development work** on top of `v0.4.0-beta.1`, not a shipped
feature. `main` contains unreleased M1 development work; M2 concurrency work is
still on an unmerged branch and is not part of this release. Real-provider and
supported-platform acceptance remain unproven; no M5 quality claim is made.

## Local package acceptance (no official binaries published)

1. Run `ImeceIDE.exe` from the extracted package.
2. Choose **Open Folder** on the welcome screen and select a project.
3. In Settings → Model providers, add a provider from the catalog and enter
   its API key ("Test & save" validates it first).
4. To use an agent CLI (Claude Code, Gemini CLI, Codex CLI, Qwen Code),
   install it and make sure it is on `PATH`.
5. Type a task in the work panel; review the proposal as a diff and confirm
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
- [ ] Build only an existing, reviewed version tag with the manual
      **Build Windows beta release** workflow. The default `publish: false`
      builds, smokes, checksums and uploads a workflow artifact; it does not
      create a tag or release.
- [ ] After acceptance and release approval, run the workflow again for the
      same tag with `publish: true`. That run rebuilds from the tag and publishes
      its own artifact only after confirming the tag still resolves to its
      checked-out commit; this does not attest that it is byte-identical to an
      artifact accepted in an earlier run.

## Manual workflow use

In Actions, select **Build Windows beta release**, choose an existing strict
`vMAJOR.MINOR.PATCH` tag (optionally with a prerelease such as
`v0.4.0-beta.1`), and run with `publish` left false for artifact-only testing.
The workflow checks out the explicit `refs/tags/` reference rather than the
selected workflow branch or a similarly named branch.
Only after reviewing the artifact and approving a release should the same tag
be run with `publish` enabled. That run builds again and publishes its
same-run artifact; the privileged publishing job does not rebuild or execute
source code. The publish job checks that the existing tag resolves to the
build commit immediately before publishing and uses `--verify-tag` to prevent
creating a missing tag. GitHub tag protection/immutability is not configured
by this workflow, so this check is not an immutable-tag guarantee. The workflow
does not create tags, bump versions, or publish automatically.

## Known limits

- This beta is **Windows-first** and usable from source on Linux; there is no
  auto-update.
- **Real-provider and supported-platform acceptance is an open gate.** The
  role-free default flow is verified by local fixture runs only. Live-provider
  quality, and Windows/Linux end-to-end acceptance, are **not** demonstrated and
  are **not** claimed. Windows-native evidence fingerprinting has a documented
  capability limitation (see [SECURITY.md](../SECURITY.md)); where the required
  no-follow inventory capability is missing, a PASS cannot be proven.
- **One active run at a time on the published tag and `main`.** The unmerged
  M2 development branch implements concurrency; restart durability is still
  unimplemented. Neither is delivered by the current release. See
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
(cd web/ui && npm run typecheck && npm run build)
.venv/bin/python -m pytest -q
```

Use the configured development environment from [Contributing](CONTRIBUTING.md).
On Windows, use `.\.venv\Scripts\python.exe` and run frontend commands from
`web/ui`, returning to the repository root before running Python.

Packaging runs only in Windows PowerShell:

```powershell
packaging/build.ps1
node packaging/smoke.mjs
```
