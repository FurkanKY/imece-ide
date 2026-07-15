# Changelog

This file records public, user-visible releases. Development notes and internal
planning records are intentionally not part of the published repository.

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
