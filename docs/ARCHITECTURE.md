# Architecture

## Overview

The system is made of three layers:

```
┌──────────────────────────────────────────────────────────────────┐
│  INTERFACES     orchestrator.py (CLI) · app.py (web) · shell.py  │
│                 shell.py = desktop IDE (webhost/ + web/ui/)      │
├──────────────────────────────────────────────────────────────────┤
│  ORCHESTRATION  runner.py (from-scratch) · project_runner.py     │
│                 → generators that yield events                   │
├──────────────────────────────────────────────────────────────────┤
│  AGENTS         agents.py (roles + routing)                      │
│  ADAPTERS       adapters.py + providers.py (catalog/registry)    │
│  TOOLS          project.py (list/read/diff/apply files)          │
└──────────────────────────────────────────────────────────────────┘
```

The core design rule: **each layer depends only on the one below it and knows
nothing about the one above.** Interfaces call the orchestrators; orchestrators
call the agents; agents call the adapters. The same engine therefore runs
unchanged under every interface.

---

## Layer 1 — Adapters (`adapters.py` + `providers.py`)

Every provider is reduced to **one shared signature**:

```python
fn(system_prompt, user_prompt) -> LLMResponse
```

`LLMResponse` carries not just the text but **observability** data:

```python
@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    cost_usd: float
```

There are two adapter families:

- **OpenAI-compatible APIs** — a single generic function,
  `call_openai_compat(...)`, drives every hosted or local provider that
  speaks `POST {base_url}/chat/completions` (DeepSeek, Gemini via Google's
  OpenAI-compatible endpoint, OpenAI, Mistral, Groq, xAI, Qwen, Moonshot,
  OpenRouter, Ollama, user-defined custom endpoints). What differs per
  provider is only configuration: base URL, model, key env var, price table.
- **Agent CLIs** — thin subprocess wrappers following the Claude Code
  pattern (headless prompt in, JSON out): `call_claude`, `call_gemini_cli`,
  `call_codex_cli`, `call_qwen_code`. Claude reads `result`,
  `total_cost_usd` and `usage.*` from the CLI's JSON; the others parse their
  own JSON shapes tolerantly. CLIs use their own login — no API key.

**`providers.py` is the catalog + registry.** `CATALOG` holds the built-in
provider definitions as data; user choices (model per provider, custom
OpenAI-compatible endpoints) live in `providers.json` under the app data
directory. `refresh()` projects the catalog into `adapters.PROVIDERS`
(`{"claude": fn, "deepseek": fn, ...}`), which stays the backward-compatible
lookup used by the agents layer. Adding a hosted provider is therefore a
catalog entry, not a new adapter.

## Layer 2 — Agents (`agents.py`)

An agent = **provider + role instructions**.

```python
ROLE_PROMPTS    = {"planner": "...", "coder": "...", "reviewer": "..."}
DEFAULT_ROUTING = {"planner": "claude", "coder": "deepseek", "reviewer": "gemini"}

class Agent:
    def run(self, user_prompt) -> LLMResponse   # calls its provider

build_agents(routing) -> {"planner": Agent, "coder": Agent, "reviewer": Agent}
```

Changing `routing` is model routing: moving a role to another model. The
instructions (`ROLE_PROMPTS`) stay fixed; only the model that executes them
changes.

## Layer 3 — Orchestration

Both orchestrators are **generators**: they `yield` step-by-step events
(dicts) that interfaces render live. This keeps the flow identical in the CLI,
the web UI and the desktop IDE.

### `runner.py` — generation from scratch

`run_task(task, routing, max_rounds, run_python)` flow:

```
PLAN (Planner) → CODE (Coder) → [ execute → REVIEW → fix ]×N → save
```

`run_python_code(code, execute)` → compile check (`py_compile`) plus optional
execution; returns `(ok, output)`. This is the core of **execution
grounding**: real run results are fed back into the loop.

**Event types:** `stage`, `metric`, `output`, `exec`, `note`, `done`.

### `project_runner.py` — local projects

`run_project_task(project_root, task, routing)` flow:

```
Planner sees the project and selects files via 'FILES:'
  → the selected files are read
  → Coder emits complete new contents in '### FILE:' blocks
  → blocks are converted to diffs
  → Reviewer inspects the diff
  → a 'proposal' event (the interface asks the user to approve)
```

Helpers: `_parse_requested_files` (the Planner's FILES list),
`_parse_file_blocks` (the Coder's FILE blocks).

**Event types:** `info`, `stage`, `metric`, `output`, `diff`, `proposal`.

Around a run, the engine provides supporting services:

- `checkpoints.py` — an atomic snapshot under `.imece/checkpoints` before an
  apply; automatic rollback on failure and manual restore via the bridge. Git
  history is never touched.
- `receipts.py` — a per-run change receipt written atomically to
  `.imece/receipts/<id>.json`; `history.json` keeps only a fast index.
- `secret_store.py` — in the packaged Windows app, API keys are encrypted with
  DPAPI under `%LOCALAPPDATA%/ImeceIDE`; source mode keeps the `.env` flow.
- `runconfig.py` — run-command inference for F5 (file by extension; project by
  `.imece/run.json` override > npm/cargo/go/main.py heuristics) plus the
  first-run approval fingerprint.

## Desktop shell (`shell.py` + `webhost/` + `web/ui/`) ★

The desktop interface renders **the entire UI with web technology inside one
`QWebEngineView`** (the Electron model of Cursor/VS Code, but hosted by native
PySide6). The engine stays in Python and is reached over a single **RPC
bridge**.

```
web/ui/  (React 19 + TS + Vite + Tailwind v4)      webhost/  (PySide6 host)
  titlebar · activitybar · explorer · editor          shell.py    entry (--dev)
  (Monaco) · search · scm · aipanel · terminal        scheme.py   app:// scheme (serves dist)
  (xterm) · statusbar · welcome · toasts              window.py   frameless window + webview
    ▲                                                 bridge.py   RPC dispatcher (@handler)
    │ bridge/{protocol,qt,mock}.ts                    state.py    active Project singleton
    │  call(method,params) → Promise                  watcher.py  QFileSystemWatcher → fs.changed
    │  on(channel, cb)     → event stream             api/…       domain handlers
    ▼
  QWebChannel  ◀──── host.call / reply / event ────▶
```

**Bridge contract** (single source of truth:
`web/ui/src/bridge/protocol.ts`): envelope `{id, method, params}` →
`{id, ok, result|error}`; events are `{channel, payload}`. On the Python side
handlers register with `@handler("domain.method")`; long jobs run on QThreads
and resolve via signals. Domains: `window · app · settings · project · fs ·
session · run · terminal · history · search · scm · lsp · exec · debug ·
checkpoint · keys · providers`. `api/scm.py` wraps git CLI subprocesses for
status/diff/stage/unstage/discard/commit (UTF-8, `stdin=DEVNULL`); line diffs
open in the center Monaco diff.

**Language intelligence** (`webhost/jsonrpc.py` + `api/lsp.py` +
`web/ui/src/lib/lsp.ts`): the **basedpyright** language server is attached
through the bridge. `jsonrpc.py` implements Content-Length framing (the wire
format shared by LSP and DAP); `api/lsp.py` manages the server lifecycle
(initialize on project open, restart on project switch, shutdown on exit).
On the web side `lib/lsp.ts` is a **hand-written thin client** (no
monaco-languageclient): document sync from the Monaco model lifecycle
(didOpen/didChange, 200 ms debounce), completion/hover/definition/signature
providers, and `publishDiagnostics` → `setModelMarkers`. URI translation
(`file:///<rel>` ↔ absolute LSP URIs) lives in the same module. TS/JS gets the
same features from Monaco's own worker service (`lib/monaco.ts`).

**Run (F5)** (`runconfig.py` + `api/exec.py`): `api/exec.py` runs the approved
command through the shell, streams output as 16 ms-coalesced `exec.output`
events (terminal pattern) and closes with `exec.exited {code, durationS}`.
Output is captured into the bottom panel's read-only xterm; the raw text is
kept in `state/exec.ts` for "send this error to the team". One concurrent run;
on Windows the process tree is cleaned with `taskkill /T /F`.

**Debugger** (`api/debug.py` + `state/debug.ts` + `components/debug/`): over
debugpy DAP — `python -m debugpy --listen --wait-for-client <file>` on a free
port (the debuggee is our subprocess; stdout streams to the OUTPUT tab), a
retrying socket connection reusing the `jsonrpc.py` framing. Handshake:
initialize→attach→[initialized]→setBreakpoints→configurationDone. On
`stopped`, Python fetches the stack and emits one packet
`debug.stopped {path, line, frames}`; scopes/variables are lazy
(`variablesReference`). The web side owns Monaco glyph-margin breakpoints
(persisted per project) and the amber paused-line highlight; F5 follows the
VS Code convention (continue if paused · debug if `.py` · run otherwise).

**The `app://` custom scheme** (`scheme.py`): Vite ES modules and workers hit
CORS restrictions under `file://`, so a scheme flagged
`SecureScheme|CorsEnabled|FetchApiAllowed` serves `web/ui/dist/` from disk. In
`--dev` mode the Vite dev server (`localhost:5173`, HMR) is loaded instead —
with the real bridge.

**Frameless mechanics:** the HTML titlebar's `pointerdown` calls
`window.startSystemMove()` over the bridge (native snap/move preserved); edge
handles call `startSystemResize(edge)`. Keyboard shortcuts are owned entirely
by the web side (`lib/keymap.ts`).

**Mock bridge** (`bridge/mock/`): the same UI runs in a plain browser against
fake data and a virtual FS (`?scenario=…`) — used for development and for
visual verification via `tools/webshot.mjs` (Playwright; everything renders,
including Monaco and xterm).

**State management** (zustand, one store per domain):
`workspace` (project + lazy tree), `editor` (tabs/dirty/save/center diff),
`run` (run events), `scm`, `terminal`, `search`, `ui` (panel
visibility/sizes), `settings` (prefs → `<html data-*>`), `exec`, `debug`.
Design tokens are defined in `web/ui/src/styles/tokens.css` — the single
source; raw hex/px values are not used in components.

**Editor:** Monaco from npm (`lib/monaco.ts` — worker wiring + the
`imece-dark` theme generated from tokens + diff colors). The center **inline
diff** uses the same `DiffView` component for AI proposals and SCM line diffs.

**Terminal:** `api/terminal.py` opens a pywinpty (ConPTY) PTY; a reader
QThread flushes with 16 ms/256 KB coalescing → `terminal.data` events →
xterm.js.

## Tools layer (`project.py`)

`Project(root)` provides safe operations on a local project:

| Method | Job |
|--------|-----|
| `list_files()` | relative paths of relevant files (noise/binaries filtered) |
| `read_file(rel)` | contents (capped at `MAX_READ_CHARS`) |
| `make_diff(rel, new)` | produces a unified diff |
| `apply(rel, new)` | writes to disk; backs up the old file as `.bak` |
| `_safe(rel)` | **path safety** — blocks any escape from the project root |

---

## Data-flow example (desktop, project mode)

```
User: "convert the date format in utils.py to ISO 8601"  (AI panel composer)
  web → bridge run.start → api/run.py Worker(QThread) → run_project_task(...)
    yield stage/metric/output   → run.event → flow tab + live pipeline
    yield diff                  → Changes list fills
    yield proposal              → center inline diff opens; Apply/Reject enabled
  User clicks Apply → run.applyProposals → checkpoint → Project.apply(...)
    → fs.changed → explorer/tabs/SCM refresh
```

## Why this design?

- **One shared `LLMResponse`** → adding a model = one `call_xxx` plus a
  `PROVIDERS` entry.
- **Generators + events** → the same engine under every interface; the stream
  is live and interface-agnostic.
- **The routing dict** → move roles between models with one line.
- **The Project layer** → file operations in one place, path safety
  guaranteed.
