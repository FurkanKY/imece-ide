# Privacy

Imece IDE has **no telemetry, no analytics and no automatic error reporting**.
It runs no hosted service of its own, so there is no first-party server that
anything is reported to.

## What the app touches

- **Your opened project** — read, and written only inside the isolated Git
  worktree for a run. Nothing reaches your working tree except through an
  explicit apply decision, which takes a checkpoint first.
- **Private per-user app data** — under the OS app-data location (for example
  `%LOCALAPPDATA%/ImeceIDE` on Windows). This holds the run/task metadata store
  (SQLite), preferences and logs.
- **Collaboration metadata stores** — private, newly created bare hub/store
  directories in app data, or paths you explicitly select yourself.
- **Explicitly chosen output paths** — a combined candidate materialized in a
  new directory outside your checkout, and a Markdown receipt export into a
  folder you pick.
- **Your own projects' commands** — a project-provided run/debug command and the
  auto-detected verification commands execute code in that project, on your
  machine, with your permissions.

It does **not** enumerate installed packages, scan your home directory, or read
files you have not opened, explicitly selected, or explicitly pointed it at.

## What leaves the machine

**Only when you start an AI run, and only to the provider you selected for that
run.** The task text and the selected project context are sent to that one
provider — and to no other provider in the catalog. The optional "Test & save"
key check sends a single authentication request to that provider's `/models`
endpoint and nothing else.

### API credentials

Keys are stored **per provider**, locally: a git-ignored `.env` when running from
source, Windows DPAPI encryption under the current user account in packaged
builds.

Storing a key locally does not mean it can never be used anywhere else: the
credential is **transmitted to the provider you selected**, as that provider's
auth protocol requires. If you drive a role/agent through an agent CLI (Claude
Code, Gemini CLI, Codex CLI, Qwen Code), that CLI authenticates with **its own
account and its own protocol** — Imece only detects it on `PATH`.

What Imece does guarantee:

- raw keys are **never** returned to the UI/frontend;
- raw keys are **never** placed in agent prompts or shared context;
- raw keys are **never** written into canonical error or event text — model and
  execution failures surface as fixed, sanitized canonical messages rather than
  raw provider exception text;
- no key is ever sent to an Imece IDE service, because there is no such service.

## Collaboration is an explicit, intentional exception

The experimental collaboration foundation ([docs/COLLABORATION.md](docs/COLLABORATION.md))
is metadata-first and loopback-only, but two things leave your machine when you
use it, and both are deliberate:

- **`shareOnce` is an intentional secret-exception.** Sharing a member
  credential with a collaborator is a one-shot explicit action that puts the
  secret in memory and on the **system clipboard**. Clipboard and memory
  contents are outside Imece's control after that point; clear your clipboard.
- **Selected proposal code is shared verbatim.** Explicit code publication
  captures only the paths you name, and there is **no automatic redaction** — a
  secret embedded in a selected file is shared as-is. Review your selection
  before publishing.

Shared context is labelled **untrusted data**. It becomes part of the prompt of
the **selected** model provider during a run, and only where that run's context
was **explicitly approved** — it is never silently attached to another provider
or another run. The default single-agent flow **refuses** an attached
collaboration shared context explicitly (`collab_unsupported`) rather than
enabling it quietly.

The collaboration listener binds literal `127.0.0.1` only. **LAN pairing, LAN/WAN
deployment and TLS are not implemented.**

## Verification runs project code — it is not a sandbox

Deterministic verification executes the project's **own** test command inside
the isolated worktree. It is a process/filesystem isolation boundary for your
working tree, **not** a security sandbox: the command runs your project's code
with your permissions. Fingerprinting records persistent changes only. See
[SECURITY.md](SECURITY.md).

## Receipts and export

Running a project command and exporting a change receipt both require explicit
user action. A receipt is stored under the opened project's ignored `.imece/`
directory; Markdown export writes only to a folder chosen by the user.

## Optional decision layer ("Karar katmanı")

This is an **optional, off-by-default capability** — it is not a mandatory step,
not a role in the role-free product and not a v1 requirement. It triages a
failed verification check before an automatic fix attempt. **"Kapalı"** (the
default) keeps today's behaviour. **"Kurallar"** runs entirely offline —
nothing about the failure leaves the machine. Only if you explicitly choose
**"Jev (TypeSafe)"** does a decision request go out, and then only to the fixed
endpoint `https://api.typesafe.ai`, containing just these allowlisted state
fields:

- the failing command's arguments, its exit code, whether it timed out;
- a short, cropped error excerpt;
- the run's changed file paths (relative to the opened project);
- the baseline result (status + exit code).

No full file contents, no other project data and no arbitrary context is sent.
The allowlist is pinned separately from the model-provider catalog and from any
agent/role routing.

Known credential values, provider token formats, quoted or bare credential
assignments and PEM private-key blocks are redacted **before** the excerpt is
cropped, and if anything still looks like a secret the request is refused. This
is not an absolute guarantee: the error excerpt can contain real source lines, so
a value embedded in your own code could appear in it. Review your project's test
output and choose "Kurallar" or "Kapalı" if that is not acceptable.

The Jev key (`TYPESAFE_API_KEY`) is stored like any other key (`.env` from
source, Windows DPAPI in packaged builds), is never returned to the UI and is
never sent to Imece IDE. If the Jev call fails (no key, no optional SDK, API
error or timeout), the run falls back to the same offline "Kurallar" behaviour.
Each decision is recorded locally as a `decision.made` event with its answers,
confidence and fallback flag — no raw remote state is written to receipts.

## Honest limits

- **Native HTTP cancellation is bounded.** Cancelling an in-flight native HTTP
  request stops waiting for it; it cannot unsend bytes that have already left
  your machine, and it does not retract data already delivered.
- **Unverified platforms.** Real-provider runs, two-machine/LAN and Windows
  end-to-end acceptance are still an open gate; see
  [docs/PRODUCT-PLAN.md](docs/PRODUCT-PLAN.md).