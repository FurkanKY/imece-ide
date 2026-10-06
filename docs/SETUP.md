# Setup

This guide covers running Imece IDE from source, which is currently the only
supported way to use it — prebuilt binaries are not published yet (see
[RELEASE.md](RELEASE.md)).

## 1. Prerequisites

| Component | Version / notes |
|-----------|-----------------|
| Windows | 10/11 — the desktop shell targets Windows first (ConPTY terminal, DPAPI key store); packaging (prebuilt binaries) is Windows-only |
| Linux | also supported from source (real PTY terminal via `ptyprocess`, `.env`-based key storage — no DPAPI); no packaged build yet |
| Python | 3.14 (what CI and packaging use; PySide6 ≥ 6.11.1 requires a recent Python) |
| Node.js | Node 22 LTS, 22.12+ recommended, for building the frontend (Vite 7 supports Node 20.19+ or 22.12+) |
| Git | installed on `PATH`; isolated agent runs require a Git project |
| Provider / agent CLI | optional for setup and offline verification; configure one only to make an AI run |

No provider credentials are needed to install, build or run the offline tests.
An AI run requires a compatible configured provider or installed agent CLI.

## 2. Python dependencies

**Windows PowerShell**, from the repository root:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Linux Bash**, from the repository root:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

For a test/contribution environment use `requirements-dev.txt` instead, which
adds `pytest`.

The experimental Jev (TypeSafe) **decision layer** is optional and deliberately
not part of `requirements.txt`. Install it only if you want to opt in to
Settings → Karar katmanı → **Jev (TypeSafe)**:

```bash
# Linux (source venv)
.venv/bin/python -m pip install -r requirements-jev.txt
```

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe -m pip install -r requirements-jev.txt
```

Its key (`TYPESAFE_API_KEY`) is entered in Settings → **Karar sağlayıcısı**
and stored like every other key (`.env` in source mode, DPAPI in packaged
builds); it is separate from the model-provider catalog and from role routing.
See [DECISION-LAYER.md](DECISION-LAYER.md).

Contents: `requests`, `python-dotenv`, `flask` (web interface),
`PySide6` (desktop shell), `pywinpty` (integrated terminal, ConPTY;
Windows-only), `ptyprocess` (integrated terminal, real PTY; Linux/macOS-only),
`basedpyright` (Python IntelliSense — ships its own Node
runtime), `debugpy` (debugger).

**Fonts:** the UI uses the Windows `Segoe UI Variable` / `Segoe UI` system
chain, plus bundled JetBrains Mono for code and numeric data
([OFL license](../web/ui/public/fonts/JetBrainsMono-OFL.txt)). Nothing to
install.

## 3. Frontend build

The UI lives in `web/ui/` and is built with Vite (Monaco, xterm and all other
web dependencies come from npm):

```powershell
Push-Location web/ui
npm ci            # first time (or npm install)
npm run build     # → web/ui/dist  (python shell.py serves this via app://)
Pop-Location
```

Linux Bash equivalent (also from repository root):

```bash
(cd web/ui && npm ci && npm run build)
```

Development loop: `npm run dev` (HMR; the same UI runs in a plain browser with
a mock bridge) plus `.\.venv\Scripts\python.exe shell.py --dev` on Windows or
`.venv/bin/python shell.py --dev` on Linux, from the repository root. Visual verification: `node tools/webshot.mjs` →
`.uishots/*.png`.

## 4. Providers and API keys

The recommended flow is in-app: **Settings → Model providers → Add provider →
paste your key → "Test & save"**. The catalog ships with DeepSeek, Gemini,
OpenAI, Mistral, Groq, xAI, Qwen, Moonshot, OpenRouter and Ollama, plus a
"Custom (OpenAI-compatible)" entry for any other endpoint that speaks
`POST {base_url}/chat/completions` (self-hosted included). Agent CLIs
(Claude Code, Gemini CLI, Codex CLI, Qwen Code) need no key — install them,
log in with their own account flow, and the Settings list shows whether each
one was found on `PATH`.

Keys are stored per provider: in source mode they are written to the local
git-ignored `.env`; in the packaged Windows app they go to a DPAPI-encrypted
store bound to your Windows account (legacy plain-text `.env` keys are
migrated and cleared on the first Settings status check).

Editing `.env` by hand also works — each catalog provider reads a fixed pair
of variables:

```ini
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-chat        # any model your account can use

GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash       # any model your account can use

CLAUDE_CLI=claude                   # no key for Claude — just the CLI name
```

The same pattern applies to the rest of the catalog (`OPENAI_API_KEY`,
`MISTRAL_API_KEY`, `GROQ_API_KEY`, `XAI_API_KEY`, `DASHSCOPE_API_KEY`,
`MOONSHOT_API_KEY`, `OPENROUTER_API_KEY`; Ollama needs no key). A model
picked in Settings overrides the `*_MODEL` variable.

> Model names must be real names your provider accepts. Check your provider's
> current documentation for available models and quota limits.

`.env` is git-ignored — never commit or share it.

The Jev (TypeSafe) decision-layer key is **not** part of this catalog: it is a
separate "decision provider" (`TYPESAFE_API_KEY`), it never appears in the
Planner/Coder/Reviewer provider lists, and the optional SDK it needs is
installed separately (see §2 and [DECISION-LAYER.md](DECISION-LAYER.md)).

## 5. Offline verification

Run the default suite first; it uses fakes/in-process transports and does not
call provider services:

```bash
.venv/bin/python -m pytest -q
```

On Windows PowerShell use:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Launch the desktop app from the repository root after building the frontend:

```powershell
.\.venv\Scripts\python.exe shell.py
```

```bash
.venv/bin/python shell.py
```

### Optional provider smoke checks

These examples make real network requests, disclose the prompt to the selected
provider and may consume quota. They are **not** required for setup or normal
verification. First configure credentials locally as described above; never
paste keys into commands or issues.

```bash
.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv(); from adapters import call_deepseek; print(call_deepseek('Short answer.', 'one word: test'))"
```

```powershell
.\.venv\Scripts\python.exe -c "from dotenv import load_dotenv; load_dotenv(); from adapters import call_deepseek; print(call_deepseek('Short answer.', 'one word: test'))"
```

---

## Windows development notes

Three environment pitfalls worth knowing before they cost you time.

### A) `python` not found in git-bash

In git-bash, `python` can resolve to the **Microsoft Store redirect stub**
(`AppData/Local/Microsoft/WindowsApps/python`) instead of a real interpreter
and fail with "Python was not found".

- In **PowerShell**, `python` usually resolves correctly — prefer it.
- Permanent fix: Windows Settings → *App execution aliases* → disable the
  `python.exe` / `python3.exe` toggles, and put your real Python on `PATH`.

### B) Legacy code pages (e.g. Turkish cp1254)

When running code in a subprocess (execution grounding, console), the child
process may write non-ASCII characters in the legacy code page; if UTF-8
decoding crashes, output is silently lost. The codebase handles this pattern
consistently (`runner.py: run_python_code`):

```python
env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
subprocess.run(..., encoding="utf-8", errors="replace", env=env)
```

Keep the same `encoding="utf-8", errors="replace"` discipline in any new
subprocess call.

### C) Flask debug reloader

`app.py` runs with `use_reloader=False`; otherwise each run writes
`output/result.py`, which triggers the reloader and kills the in-flight
request.

## Linux (source mode)

Running from source works the same way as on Windows, with POSIX paths:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
(cd web/ui && npm ci && npm run build)
.venv/bin/python shell.py
```

The integrated terminal uses `ptyprocess` (a real PTY) instead of ConPTY, and
spawns `$SHELL` if it is set and executable, falling back to `/bin/bash` then
`/bin/sh`. Key storage falls back to the plain `.env` file (no DPAPI
equivalent is used outside a packaged Windows build); packaging itself
(prebuilt binaries) remains Windows-only for now.

### Pre-set `PYTHONPATH` from another toolchain breaks pytest

If your shell has a global `PYTHONPATH` set by an unrelated toolchain (for
example ROS's `setup.bash`), it can shadow this project's own packages and
break `pytest`'s plugin loading — a ROS plugin importing `launch`/`yaml` from
the system interpreter fails collection with errors that don't look related to
`PYTHONPATH` at all. Clear it for the run instead of trying to reconcile it:

```bash
env -u PYTHONPATH QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q
```

(`QT_QPA_PLATFORM=offscreen` is for headless machines/containers that touch
`PySide6.QtWidgets`; the `PATH` prefix just makes `pytest` resolve to the venv.)

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` also makes collection work, but it disables
**every** pytest plugin — including any Qt/PySide-side plugins and fixtures the
suite may rely on. Prefer `env -u PYTHONPATH`.
