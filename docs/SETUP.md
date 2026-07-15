# Setup

This guide covers running Imece IDE from source, which is currently the only
supported way to use it — prebuilt binaries are not published yet (see
[RELEASE.md](RELEASE.md)).

## 1. Prerequisites

| Component | Version / notes |
|-----------|-----------------|
| Windows | 10/11 — the desktop shell targets Windows (ConPTY terminal, DPAPI key store) |
| Python | 3.14 (what CI and packaging use; PySide6 ≥ 6.11.1 requires a recent Python) |
| Node.js | ≥ 20, for building the frontend |
| Claude Code CLI | optional — `claude --version` must work; a Pro/Max subscription is enough, no API key needed |
| DeepSeek API key | optional — https://platform.deepseek.com → API Keys |
| Gemini API key | optional — https://aistudio.google.com/apikey |

At least one provider (Claude CLI, DeepSeek or Gemini) must be configured for
AI runs; everything else in the IDE works without any keys.

## 2. Python dependencies

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

For a test/contribution environment use `requirements-dev.txt` instead, which
adds `pytest`.

Contents: `requests`, `python-dotenv`, `flask` (web interface),
`PySide6` (desktop shell), `pywinpty` (integrated terminal, ConPTY;
Windows-only), `basedpyright` (Python IntelliSense — ships its own Node
runtime), `debugpy` (debugger).

**Fonts:** the UI uses the Windows `Segoe UI Variable` / `Segoe UI` system
chain, plus bundled JetBrains Mono for code and numeric data
([OFL license](../web/ui/public/fonts/JetBrainsMono-OFL.txt)). Nothing to
install.

## 3. Frontend build

The UI lives in `web/ui/` and is built with Vite (Monaco, xterm and all other
web dependencies come from npm):

```bash
cd web/ui
npm ci            # first time (or npm install)
npm run build     # → web/ui/dist  (python shell.py serves this via app://)
```

Development loop: `npm run dev` (HMR; the same UI runs in a plain browser with
a mock bridge) plus `python shell.py --dev` (the real bridge inside the
embedded window). Visual verification: `node tools/webshot.mjs` →
`.uishots/*.png`.

## 4. API keys (source development)

Copy `.env.example` to `.env` and fill it in:

```ini
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-chat        # any model your account can use

GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash       # any model your account can use

CLAUDE_CLI=claude                   # no key for Claude — just the CLI name
```

> Model names must be real names your provider accepts. Check your provider's
> current documentation for available models and quota limits.

`.env` is git-ignored — never commit or share it. This flow is for source
development only. In the packaged Windows app, enter keys in Settings instead:
Windows DPAPI stores them encrypted for the current user account, and any
legacy plain-text `.env` keys are migrated and cleared on the first Settings
status check.

## 5. Verify

```bash
python -c "from dotenv import load_dotenv; load_dotenv(); \
from adapters import call_deepseek, call_gemini; \
print(call_deepseek('Short answer.', 'one word: test')); \
print(call_gemini('Short answer.', 'one word: test'))"
```

To test Claude separately:

```bash
python -c "from adapters import call_claude; print(call_claude('Short answer.', '2+2?'))"
```

Then run the test suite and the app:

```bash
python -m pytest -q
python shell.py
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
