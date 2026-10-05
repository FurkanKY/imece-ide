# Changelog

This file records public, user-visible releases. Development notes and internal
planning records are intentionally not part of the published repository.

## Unreleased

> Everything in this section is **development work on top of
> `v0.4.0-beta.1`**. It is not a released version, not a tag and not a binary.
> Nothing here is announced as available in a download. The tracked
> plan of record is [PRODUCT-PLAN.md](PRODUCT-PLAN.md).

- **Experimental collaboration foundation (metadata-first, loopback only):**
  checkouts of the same repository can share an explicit, offline metadata
  session — a goal/decisions/interfaces record, a task list with **advisory**
  scopes, an explicitly published private proposal per selected path, and a
  combined candidate materialized in a **new directory outside your checkout**
  with real three-way merges. An opt-in loopback control core on literal
  `127.0.0.1` adds authenticated metadata operations, revision subscription
  with bounded replay, a read-only snapshot client and explicit native
  task-status commands. This is a **foundation**, not a team product: it is
  **not connected to the default single-agent flow** (an attached shared
  context is refused explicitly), **no LAN pairing or LAN/WAN deployment or
  TLS exists**, and two-computer, LAN and Windows end-to-end are **unverified**.
  Selected proposal code is shared **verbatim with no automatic redaction**;
  the one-shot credential share uses memory and the system clipboard. See
  [COLLABORATION.md](COLLABORATION.md) and
  [PRIVACY.md](../PRIVACY.md).
- **Single-task default flow (no roles):** the AI panel now defaults to one
  task plus **one** provider — a task goes in, a single independent agent
  works on it end to end in its own isolated worktree, and the result comes
  back with deterministic verification evidence. There is no planner step, no
  reviewer step and no role chain in the default flow; the panel shows **Çalışma
  (work) / Sonuç (result) / Etkinlik (activity)**. The older
  Planner/Worker/Reviewer trio remains available as a **compatibility backend**
  for existing runs and historical receipts, but is no longer the default and
  is not planned for v1. Providers the agent flow cannot drive are now refused
  with a reason instead of falling back silently, and agent-CLI (ACP) providers
  that do not report usage show `—` for cost/tokens instead of a fabricated
  number. See [USAGE.md](USAGE.md#default-flow-task--one-agent--evidence--your-decision).
  **Status: implemented and verified by local fixture end-to-end runs; real
  provider and supported-platform acceptance are still unmet, so no live
  guarantee is made.** The task-first shell and real concurrency (M2) are now
  **authorized and starting** but are **not implemented yet**; restart
  durability (M3), two-machine pairing (M4) and the packaged release gate
  (M5) remain planned — see [PRODUCT-PLAN.md](PRODUCT-PLAN.md).
- **Follow-up requests on the default flow:** while a result is waiting for
  your decision, you can send a follow-up instruction and the **same run**
  continues from the **same worktree** and the **same original task** instead
  of starting over. A follow-up that is refused for a known reason keeps your
  existing ready state and evidence rather than discarding them, and rejecting
  a result or restoring files clears the stale evidence instead of leaving it
  on screen.
- **Experimental decision layer (Jev / TypeSafe) — optional capability:** Settings
  → "Karar katmanı" decides what happens when a verification check fails *before*
  the automatic fix loop spends an attempt on it. This is **not** a role in the
  role-free product, **not** a mandatory step and **not** a v1 requirement.
  **Kapalı** (default) keeps today's behaviour, **Kurallar** classifies the
  failure with deterministic offline rules, and **Jev (TypeSafe)** opts in to a
  fast triage model — with its own key ("Karar sağlayıcısı") and an optional SDK
  (`pip install -r requirements-jev.txt`). Any Jev failure falls back to the
  offline rules, so the feature can never block a run. Only the opt-in Jev
  mode sends data (filtered command, short error excerpt, relative changed
  paths, baseline result) to `https://api.typesafe.ai`, over a separate pinned
  state allowlist and after credential redaction; see [PRIVACY.md](../PRIVACY.md)
  and [DECISION-LAYER.md](DECISION-LAYER.md). Experimental: the live evaluation is
  still pending, so no accuracy or latency claim is made.
- **Follow-up requests on a pending proposal:** while a pipeline run's
  proposal is waiting for a decision, you can type a follow-up instruction
  (e.g. "also handle negative numbers") and the same run continues from the
  same isolated worktree — another Worker attempt, verification/review, and
  a fresh proposal — instead of starting a new task from scratch. See
  [USAGE.md](USAGE.md#follow-up-requests).
- **New AI engine (pipeline):** on an existing Git-repository project, runs
  now go through an isolated, multi-stage pipeline — plan, an initial
  attempt made in a separate isolated copy of the project, automatic
  verification (auto-detected `pytest`/`npm test`/`cargo test`/`go test`, or
  a project-defined `.imece/verify.json`), a semantic review, and a bounded
  automatic fix loop when verification fails or review asks for changes. The
  final result is still always presented as a reviewable diff — Apply/Reject
  is unchanged, and nothing is written to your files until you apply.
  Falls back to the previous ("classic") engine automatically when a
  project isn't a Git repository or a role's provider isn't supported yet by
  the new engine; an "AI engine" preference can also force the classic
  engine. See [ARCHITECTURE.md](ARCHITECTURE.md#ai-engine-pipeline).
- **Apply conflict detection:** applying a proposal now checks whether any
  targeted file changed on disk since the run started, and refuses the
  whole batch (no partial writes) if it did, instead of silently
  overwriting your intervening edit.
- **Account-based access to Claude, ChatGPT and Gemini:** the Planner/Worker/
  Reviewer roles can be backed by an existing Claude Code, Codex or Gemini CLI
  login instead of an API key, driven locally through the Agent Client
  Protocol (requires the CLI installed and logged in, plus Node.js/`npx`).
- **Claude API provider:** Claude models can also be used with an Anthropic
  API key (native Messages API backend, default model `claude-opus-5`).
- **Provider picker:** role dropdowns are grouped into "with account" and
  "with API key", show which providers are ready, and remember your choice;
  first start picks a ready provider automatically.
- **AI engine setting:** Settings → AI engine switches between Auto (new
  engine where possible) and Classic.
- **Linux support:** the desktop shell, integrated terminal (real PTY via
  `ptyprocess`), and run/debug process cleanup now work from source on
  Linux, alongside the existing Windows-first support. See
  [SETUP.md](SETUP.md) for Linux setup notes.

## v0.4.0-beta.1

- **Provider catalog:** the fixed Claude/DeepSeek/Gemini trio is replaced by a
  catalog-driven registry. Settings → Model providers now offers "pick a
  provider → paste a key → test & save" for DeepSeek, Gemini, OpenAI, Mistral,
  Groq, xAI, Qwen, Moonshot, OpenRouter and Ollama, plus custom
  OpenAI-compatible endpoints (self-hosted included) — all served by one
  generic adapter. Gemini now uses Google's OpenAI-compatible endpoint.
- **Agent CLIs:** alongside Claude Code, the Gemini CLI, Codex CLI and Qwen
  Code are detected on `PATH` and can be assigned to any role.
- Per-provider model selection from Settings; key validation before saving.

## v0.3.0-beta.1

- First public beta of Imece IDE, released as source only. The Windows
  packaging pipeline exists and is CI-verified, but prebuilt binaries are
  postponed until the beta stabilizes.
- Local Planner, Coder and Reviewer workflow with reviewable diffs, apply/reject
  controls, checkpoints and project-local change receipts.
- API keys stay local: a git-ignored `.env` for source runs, DPAPI encryption
  in packaged builds. Project F5 commands require explicit approval when first
  seen or changed.
- Local project explorer, Monaco editor, terminal, Git status, Python language
  support and debugging surface.

## Security and privacy

- No telemetry, analytics, or automatic error reporting. Imece runs no hosted
  service of its own.
- Model requests are started by the user and use the single provider selected for
  that run. API credentials are stored locally and transmitted only to the
  provider you selected, over that provider's auth protocol.
- Selected collaboration proposal code is shared verbatim (no automatic
  redaction), and the one-shot credential share uses memory and the system
  clipboard — both explicit, both documented.
- See [PRIVACY.md](../PRIVACY.md) and [SECURITY.md](../SECURITY.md).
