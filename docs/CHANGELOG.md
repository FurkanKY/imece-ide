# Changelog

This file records public, user-visible releases. Development notes and internal
planning records are intentionally not part of the published repository.

## Unreleased

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

- No telemetry, analytics, or automatic error reporting.
- Model requests are started by the user and use the provider assigned to each
  role. See [PRIVACY.md](../PRIVACY.md) and [SECURITY.md](../SECURITY.md).
