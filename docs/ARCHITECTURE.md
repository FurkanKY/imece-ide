# Architecture

## Overview

For an existing local project, a run goes through one of two engines,
selected per run by `engine_factory.py`:

- **The pipeline engine** (this document's main subject) — an isolated,
  multi-stage run with deterministic verification, semantic review and a
  bounded fix loop. Used when the project is a Git repository and every
  role's provider is one the pipeline engine supports.
- **The legacy engine** (`project_runner.py`, described under "Legacy
  engine" below) — a simpler Plan → Code → Review → propose flow with no
  isolation and no automated verification. Used whenever the pipeline
  engine's requirements aren't met, or the user explicitly selects it.

Both engines are reached the same way from every interface:

```
┌──────────────────────────────────────────────────────────────────┐
│  INTERFACES     orchestrator.py (CLI) · app.py (web) · shell.py  │
│                 shell.py = desktop IDE (webhost/ + web/ui/)      │
├──────────────────────────────────────────────────────────────────┤
│  ENGINE         engine_factory.py picks pipeline vs. legacy       │
│                 pipeline_runtime/  (this document)                │
│                 project_runner.py (legacy, see below)             │
├──────────────────────────────────────────────────────────────────┤
│  AGENTS         agent_runtime/ (pipeline) · agents.py (legacy)    │
│  ADAPTERS       adapters.py + providers.py (catalog/registry)    │
│  TOOLS          project.py (list/read/diff/apply files)          │
└──────────────────────────────────────────────────────────────────┘
```

The core design rule carries over to the pipeline engine too: each package
depends only on the ones below it. `pipeline_runtime` depends on the small
ports exposed by `fix_runtime`, `planner_runtime`, `executor_runtime` and
`change_runtime`; nothing below `pipeline_runtime` imports it back.

---

## AI engine (pipeline)

### Flow

```
 run.start
    │
    ▼
 [1] plan ─────────────────────────────── planner_runtime (native or ACP)
    │
    ▼
 [2] detect a deterministic verification plan for the workspace
    │      (.imece/verify.json, or pytest/npm/cargo/go heuristics — see
    │       "Verification detection" below; may be "none detected")
    ▼
 [3] initial worker attempt ────────────── executor_runtime (native or ACP)
    │        (runs inside an isolated Git worktree, never the user's
    │         real files — see "Isolation" below)
    ▼
 [4] capture the change set (workspace diff)
    │
    ├─ no changes at all ──────────────────────► run.completed (no_changes)
    │
    ├─ no verification plan detected
    │        └─ reviewer runs in ADVISORY mode (no pass/fail gate)
    │                                         ──► WAITING_USER (review_advisory)
    │
    └─ verification plan detected
             │
             ▼
       [5] run verification ──────────────── verification_runtime (native)
             │
             ├─ FAIL / TIMEOUT / ERROR ──► fix loop (VERIFICATION_FAIL trigger)
             │
             └─ PASS
                   │
                   ▼
             [6] semantic review ──────────── review_runtime (native or ACP)
                   │
                   ├─ NEEDS_FIX ──────────► fix loop (REVIEW_NEEDS_FIX trigger)
                   │
                   └─ APPROVED ───────────► RunCompletionGate.complete_reviewed()
                                             ──► WAITING_USER (proposal.ready)

 fix loop (bounded, DEFAULT_MAX_FIX_ATTEMPTS = 2 attempts)
    │  worker attempt → verification → (review if verification passes) → repeat
    ▼
 COMPLETED (reviewer APPROVED)  or  EXHAUSTED / FAILED ──► run.failed

 WAITING_USER (proposal.ready)
    │
    ├─ Apply / Reject ──────────────────────────► terminal, worktree disposed
    │
    └─ run.followUp {feedback} ───────────────── PipelineRunner.continue_with_feedback
             │        run.resumed (WAITING_USER → RUNNING, same worktree,
             │        NO new Planner attempt)
             ▼
       fix loop with a USER_FEEDBACK trigger (worker attempt → verification
       (if a plan is still detected) → review → repeat, same bounded budget)
             │
             └─► RunCompletionGate.complete_reviewed() (same await_user gate)
                   ──► WAITING_USER again (proposal.ready) — repeatable
```

### Follow-up on a proposal (F2)

A Reviewer-APPROVED, `WAITING_USER` pipeline run does not have to end in
Apply or Reject: `PipelineRunner.continue_with_feedback(run_id, workspace,
feedback, ...)` lets the user type a follow-up instruction ("also handle
negative numbers", "rename x to y") and resumes the **same** Run from the
**same** worktree — no new Planner attempt, no fresh isolation setup.

- A new `FixTriggerKind.USER_FEEDBACK` (`fix_runtime.models.FixTrigger`)
  carries the user's `feedback` (bounded, non-empty, NUL-free) and the
  `diff_sha256` of the change set it refers to — `FixLoopRunner` validates
  that `diff_sha256` against the worktree's current capture before acting on
  it, exactly like it already does for a stale `REVIEW_NEEDS_FIX` review. It
  may also carry the last known verification/review reports purely as
  context (never as a gate).
- `fix_runtime.prompt.render_fix_worker_input` gives a `USER_FEEDBACK`
  trigger its own "FOLLOW-UP INSTRUCTION FROM THE USER" section, trusted
  like the original task (never truncated) and kept separate from the
  diagnostic "FIX FEEDBACK" section used by the other two trigger kinds.
- `continue_with_feedback` records `run.resumed` (via
  `CanonicalPipelineRecorder.resumed()` — `run_runtime.projector` only
  accepts `RUN_RESUMED` from `WAITING_USER`), re-detects the verification
  plan, and then either runs the bounded `FixLoopRunner` (verification
  plan present) or a single advisory worker-then-review attempt (no plan —
  mirroring `run()`'s own "no verification plan" path), settling through
  the *same* `await_user` `RunCompletionGate` used for the initial run. The
  Reviewer's task context is augmented with the follow-up text (bounded)
  so semantic review is aware of it too. Cancellation and the
  no-verification-plan path work exactly as they do for `run()`.
- The worktree is **no longer disposed right after building proposals** —
  `webhost/api/run.py` now keeps it alive for as long as the canonical Run
  stays `WAITING_USER`, and disposes it only on Apply, Reject, cancel,
  failure, a new `run.start`, or app shutdown. `run.followUp {feedback}` is
  the bridge handler: valid only for a pipeline run currently `WAITING_USER`
  with a live worktree (a clear Turkish `BridgeError` otherwise, e.g. for
  the classic engine), and it clears the in-memory proposals, restarts the
  live activity stream from the run's current event sequence, and rebuilds
  proposals (fresh `baseHash`) once the continuation settles again.

Even a run that reaches Reviewer-APPROVED with a passing verification is
**not** applied automatically: `PipelineRunner` always constructs its
`RunCompletionGate` with `settlement="await_user"`, so a successful pipeline
run lands the canonical Run in `WAITING_USER` with a `proposal.ready` event,
never in `RUN_COMPLETED`. The user's Apply/Reject decision (see "Data-flow
example" below) is what actually settles the run. This holds whether
APPROVED is reached on the first attempt or after the fix loop.

If neither an explicit `.imece/verify.json` nor any heuristic matches
anything in the workspace, there is no way to automatically "pass" or "fail"
the run — synthesizing a fake PASS was considered and rejected as dishonest.
Instead the reviewer still runs, in an advisory capacity (its verdict is
informational, not a gate), and the run is left `WAITING_USER` with reason
`review_advisory` so a human decides.

### Layering and dependency direction

```
 pipeline_runtime/            (composes everything below into one flow)
        │
        ├── planner_runtime/  fix_runtime/  review_runtime/  verification_runtime/
        │        (small, independent runtimes; each exposes a narrow port)
        │
        ├── executor_runtime/ (binds fix_runtime's WorkerAttemptRunner /
        │        ReviewAttemptRunner / VerificationAttemptRunner ports to a
        │        concrete backend: native or ACP)
        │
        ├── agent_runtime/    (AgentSession + ModelBackend — native API calls)
        ├── acp_runtime/      (Agent Client Protocol client — account CLIs)
        │
        ├── change_runtime/   (captures a workspace's diff as a WorkspaceChangeSet)
        ├── workspace/         (GitWorktreeWorkspace — isolation, see below)
        │
        └── run_runtime/      (canonical Run/event history — SQLite, durable,
                 the single source of truth every other package writes
                 evidence into and reads evidence back from)
```

`engine_factory.py` sits above all of this: it decides *which* engine and
*which* concrete adapter per role, but never *when* something runs or how a
Run's lifecycle unfolds — that discipline stays inside `pipeline_runtime`
and `run_runtime`.

### Canonical event model

Every fact about a run — plan produced, execution started/completed/failed,
verification started/completed, review started/completed, a fix loop
attempt, the run's own terminal state — is appended as an immutable
`RunEvent` to a per-run, ordered, SQLite-backed event log (`run_runtime/`).
Nothing downstream trusts in-memory state alone; recorders
(`CanonicalPipelineRecorder`, the reviewer's and fix loop's own sinks) only
ever *record* events, and `RunCompletionGate` only ever *reads* that same
event history back before it allows a run to settle.

`RunCompletionGate` (`run_runtime/completion.py`) is the sole place allowed
to write a run's terminal state. Before doing so it re-derives the decision
from canonical evidence already in the log — e.g. `complete_reviewed()`
requires finding the *latest* verification's terminal event with
`status == "pass"`, followed by a review's terminal event with
`verdict == "APPROVED"` for a matching `diff_sha256`, with no newer execution
activity in between (which would make that evidence stale). This is what
lets a run settle honestly even after retries, cancellations or a fix loop
detour — the gate does not trust that the caller is telling it the truth, it
re-checks.

`RunCompletionGate(settlement=...)` has two modes:

- `"complete"` — a successful decision writes `run.completed` (used by
  callers that don't need a human approval step, e.g. tests).
- `"await_user"` (the pipeline engine's default) — the exact same evidence
  checks run, but a successful decision writes the run into `WAITING_USER`
  with a `proposal.ready` event carrying the same provenance, instead of
  `run.completed`. A human's Apply/Reject decision is recorded afterward via
  `run_runtime.legacy.LegacyRunCoordinator.record_proposal_applied` /
  `record_proposal_rejected`, both of which require the run to currently be
  `WAITING_USER`.

Failures (`fail_execution`, `fail_verification`, `fail_fix_loop`) are never
affected by `settlement` — a failure is always `run.failed`.

### Isolation

The pipeline engine never lets an agent touch the user's real working
directory directly. `workspace.worktree.GitWorktreeWorkspace.create()`:

1. Resolves the source folder's Git repository and current `HEAD`.
2. Creates a separate, detached, linked Git worktree (`git worktree add
   --detach`) pinned to that `HEAD` — the user's own branch/HEAD/index is
   never touched.
3. Overlays the user's *current* Git-visible working state (staged +
   unstaged changes, tracked deletions, and non-ignored untracked files)
   onto that worktree, so the agent starts from what the user actually sees
   on disk, not just their last commit. Ignored files (`.env`,
   `node_modules`, `.venv`, build output, …) are never copied. Symlinks are
   recreated as symlinks without following their target, so nothing outside
   the repo can leak in through one.
4. Freezes that overlaid state as a local, unpublished "synthetic snapshot"
   commit (via `git add -A` + `write-tree` + `commit-tree` plumbing — never
   `git commit`, so no user commit hooks run and nothing is pushed).

Everything the worker/fix loop changes is then diffed against that synthetic
snapshot, not against the user's last real commit — so the resulting
proposal is exactly "what the agent changed," not "everything that differs
from the last commit" (which would also include the user's own uncommitted
work). The Planner and Reviewer only ever read this same isolated workspace
copy — never the user's real files — so a run in progress can't be disturbed
by (and can't disturb) whatever the user keeps doing in their editor.

Known limits: repositories with unresolved merge conflicts or containing a
Git submodule/gitlink are rejected upfront (`UnsupportedRepositoryStateError`)
rather than silently mishandled.

When a run ends (applied, rejected, failed, or the app restarts after a
crash) the worktree is removed via `git worktree remove --force` plus
`git worktree prune`; `engine_factory.prune_startup_workspaces()` sweeps any
worktree left behind by a previous crash on the next startup.

### Stale-apply guard

Because a pipeline run can take a while, the user may keep editing the same
project in the IDE while it runs. Before writing anything to disk,
`webhost/api/run.py`'s apply handler re-hashes each proposed file's *current*
on-disk content and compares it against the hash recorded when the proposal
was built. If any targeted file changed in the meantime, Apply refuses for
the whole batch: nothing is written, no checkpoint is taken, and the
proposal stays pending — the user re-runs the task to get a fresh proposal,
or uses Reject. The canonical Run itself stays `WAITING_USER`; no
`proposal.applied` event is ever recorded for a conflicting apply.

### Executors: native backends vs. ACP

Each pipeline role (planner, worker, reviewer) is executed by one of two
kinds of adapter, chosen per-provider by `engine_factory.py`:

- **Native** (`agent_runtime/` + `executor_runtime.Native*Adapter`) — calls
  the provider's API directly through a `ModelBackend`: OpenAI's Responses
  API, the Chat Completions shape for any OpenAI-compatible provider, or
  Anthropic's Messages API. Used for any API-key-backed ("openai"-kind)
  provider in the catalog.
- **ACP** (`acp_runtime/` + `executor_runtime.Acp*Adapter`) — drives an
  account-based agent CLI as a local subprocess over the [Agent Client
  Protocol](https://agentclientprotocol.com), launched through `npx` so the
  pinned adapter package is fetched on first use rather than requiring a
  global install. It runs exactly one prompt per attempt, observes streaming
  updates, always denies permission requests (the pipeline engine's own
  worktree isolation is the safety boundary, not the CLI's own prompts), and
  deterministically tears the subprocess tree down afterward. `engine_factory`
  wires this path for Claude Code
  (`@agentclientprotocol/claude-agent-acp`), Codex CLI
  (`@agentclientprotocol/codex-acp`) and Gemini CLI (`@google/gemini-cli
  --acp`); launch presets live in `executor_runtime.acp_presets`. Qwen Code
  has no ACP route yet, so a role assigned to it falls back to the legacy
  engine.

Verification is always native (`NativeVerificationAttemptAdapter`) — it runs
the detected/explicit process argv directly and is never routed through a
provider at all.

### Engine selection and legacy fallback

`engine_factory.select_engine(project_root, routing, ai_engine_pref=...)`
decides per run:

- `ai_engine_pref == "legacy"` — the user explicitly asked for the classic
  engine; used with no further checks.
- `ai_engine_pref == "auto"` (the default) — the pipeline engine is used
  only if **both** hold:
  1. `project_root` is inside a Git repository (`git rev-parse
     --show-toplevel` succeeds).
  2. Every one of the three roles (planner, worker, reviewer) is assigned a
     provider the pipeline engine supports — an API-key-backed provider, or
     one of the wired ACP CLIs above.

  If either check fails, the run falls back to the legacy engine and a
  user-visible reason is surfaced in the run's event stream (e.g. "project
  isn't a Git repository" or "<provider> isn't supported by the new engine
  yet — using the classic engine"). Nothing about this fallback is silent.

Setting up a pipeline run can itself fail (e.g. the workspace can't be
created); that also falls back to the legacy engine for that run, with its
own reason surfaced the same way.

### Verification detection

`pipeline_runtime.verification_detect.detect_verification_plan(root)` picks,
in order:

1. **`.imece/verify.json`** — an explicit, user-authored JSON list of
   checks, each shaped as:

   ```json
   [
     { "id": "unit", "title": "Unit tests", "argv": ["python3", "-m", "pytest", "-q"], "timeout_ms": 300000 }
   ]
   ```

   `argv` is always an explicit argument vector, never a shell command
   string — there is no shell-injection surface here. `timeout_ms` defaults
   to `300000` (5 minutes) if omitted.
2. **Heuristics**, evaluated independently — more than one may match at once
   (e.g. a repo with both a Python backend and a JS frontend becomes one
   plan with two checks):
   - A pytest config (`pytest.ini`, `[tool:pytest]` in `setup.cfg`,
     `[tool.pytest.ini_options]` in `pyproject.toml`), a `tests/` directory,
     or any `test_*.py` file at the project root → `python3 -m pytest -q`.
   - A `package.json` with a real (non-default) `scripts.test` →
     `npm test`.
   - A `Cargo.toml` → `cargo test`.
   - A `go.mod` → `go test ./...`.

If nothing matches, `detect_verification_plan` returns `None` — see the
"no verification plan detected" branch of the flow above.

### Provider selection

Each role (Planner, Coder, Reviewer) is assigned a provider in the AI panel.
The picker groups providers into account-based ones (agent CLIs driven over
ACP) and API-key ones (native backends: Chat Completions for OpenAI-compatible
endpoints, Anthropic Messages for the Claude API). `providers.status_of()`
reports, per provider, whether a key is present or the CLI and `npx` are
available, and whether the new engine supports it. On first start
`providers.recommended_routing()` assigns the first ready provider to all
three roles (accounts first: Claude Code, Codex, Gemini CLI; then API
providers); the user's choice is persisted in the UI preferences. The
"AI engine" setting (Auto / Classic) lives in the Settings dialog.

---

## Legacy engine (fallback)

This is the original, simpler engine. It has no isolation (it edits diffs
against the project directly, gated only by the Apply step) and no automated
verification or fix loop — a human review step (the Reviewer role) is the
only check before a proposal is shown. It remains in use whenever the
pipeline engine's requirements above aren't met, and can be explicitly
selected via the "AI engine" preference.

### Layer 1 — Adapters (`adapters.py` + `providers.py`)

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

### Layer 2 — Agents (`agents.py`)

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

### Layer 3 — Orchestration

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
kept in `state/exec.ts` for "send this error to the team". One concurrent
run; the child process tree is cleaned up on exit or stop — `taskkill /T /F`
on Windows, a `psutil`-based SIGTERM-then-SIGKILL sweep of the process tree
on Linux/macOS (the shell is started in its own process group via
`start_new_session=True` so the sweep reaches every descendant).

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

**Terminal:** `api/terminal.py` opens a real PTY — pywinpty (ConPTY) on
Windows, `ptyprocess` on Linux/macOS; a reader QThread flushes with
16 ms/256 KB coalescing → `terminal.data` events → xterm.js. Arrow keys,
colors and interactive programs (`python` REPL, `less`, …) work on both.

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

### Pipeline engine

```
User: "convert the date format in utils.py to ISO 8601"  (AI panel composer)
  web → bridge run.start → engine_factory.select_engine(...) → "pipeline"
    → GitWorktreeWorkspace.create(...)         isolated worktree, HEAD + user's
                                                uncommitted state overlaid
    → PipelineRunner.run(...)
        plan            → run.event (stage="planning")
        working         → initial worker attempt in the worktree
        verifying       → python3 -m pytest -q (detected)              PASS
        reviewing       → semantic review                              APPROVED
        RunCompletionGate.complete_reviewed(...) → WAITING_USER, proposal.ready
      → run.event {type: "proposal", proposals: [...]}
        → Changes tab + center inline diff open; Apply/Reject enabled
  User clicks Apply → run.applyProposals
    → re-hash each proposed file against its recorded base hash (stale-apply guard)
    → checkpoint → write files from the worktree → proposal.applied (canonical)
    → GitWorktreeWorkspace.dispose() → fs.changed → explorer/tabs/SCM refresh
```

If verification fails or the reviewer returns `NEEDS_FIX`, the same run
hands off to the bounded fix loop instead of reaching `WAITING_USER`
directly; the fix loop retries worker → verify → review up to
`DEFAULT_MAX_FIX_ATTEMPTS` (2) times before the run is either completed the
same way or ends in `run.failed` (`fix_loop_exhausted` / `fix_loop_failed`).

### Legacy engine

```
User: "convert the date format in utils.py to ISO 8601"  (AI panel composer)
  web → bridge run.start → engine_factory.select_engine(...) → "legacy"
    → api/run.py Worker(QThread) → run_project_task(...)
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
