# Decision layer (Jev / TypeSafe)

Optional, experimental **capability** — not a role, not a mandatory step and not
a v1 requirement — that triages a **failed verification check** before the
pipeline spends a fix attempt on it. It answers three typed questions about the
failure (`failure_kind`, `caused_by_change`, `fixable_by_agent`) and maps the
answer to a pipeline action. The product's default flow is **role-free**: this
does not reintroduce a Planner/Coder/Reviewer chain, and a run works with it
switched off.

> **Status (2026-10-02):** the S1b engineering work is complete and validated
> offline; the **live evaluation is PENDING**. Nothing in this document is a
> release go/no-go decision, and no measured live accuracy, latency or cost
> figure is claimed anywhere below. Phase 4/5+ roadmap items are untouched by
> this work.

Related reading: [PRODUCT-PLAN.md](PRODUCT-PLAN.md) (scope of record — this is
explicitly **not v1 scope** and Jev is not mandatory), [PRIVACY.md](../PRIVACY.md)
(what leaves the machine, including the pinned allowlist), [SECURITY.md](../SECURITY.md),
[ARCHITECTURE.md](ARCHITECTURE.md#decision-layer) and
[USAGE.md](USAGE.md#settings). The internal design record for this feature is a
local, git-ignored file and is deliberately not linked or published here.

## Modes (Settings → Karar katmanı (deneysel))

| UI option | Preference | Behaviour |
|---|---|---|
| **Kapalı** | `off` (default) | No triage. A verification failure goes straight to the fix loop — today's behaviour. |
| **Kurallar** | `rules` | Deterministic, fully offline `RuleDecisionBackend`. Nothing leaves the machine. |
| **Jev (TypeSafe)** | `jev` | Explicit opt-in remote backend. On any failure (no key, no SDK, API error, timeout, invalid answer) it falls back to the deterministic rules and the result is marked `fallback_used`. |

Low confidence is **not** a fallback: it maps to the current fix-loop
behaviour, not to the rule backend.

## What leaves the machine (only with `jev`)

Only these allowlisted state fields are ever sent
(`decision_runtime/remote_state.py`):

- `command` — the failing check's argv tokens (bounded, redacted)
- `exit_code`
- `timed_out`
- `error_block` — a short, cropped excerpt of the check's output
- `changed_paths` — workspace-relative paths only
- `baseline_status` (`pass` / `fail` / `timeout` / `error`) and
  `baseline_exit_code`

to the fixed endpoint **`https://api.typesafe.ai`** (HTTPS, pinned;
`TYPESAFE_BASE_URL` is deliberately ignored). No full file contents, no
arbitrary project context, no other state fields — unknown fields are dropped.

Before anything is sent:

- known credential **values** (exact values of credential-named variables
  present in the process environment — `.env` files are never read),
  provider token shapes (OpenAI/Anthropic, GitHub, AWS, Slack, Google, JWT),
  quoted/unquoted credential assignments and credential flags, PEM private-key
  blocks and base64 key bodies are redacted, and
- redaction happens **before** cropping, so a secret that straddles the crop
  boundary cannot survive as a fragment. `VerificationFailureGate` sanitizes
  the full raw stdout/stderr/argv first (`sanitize_process_output`), then
  extracts the error block from the already-redacted text.

If a text still looks secret-shaped after redaction, the state is **rejected**
and nothing is sent.

**Honest limitation:** pattern redaction cannot prove arbitrary text
secret-free. The error excerpt can contain real source lines (stack frames,
assertion values) — code you wrote is not a secret by itself, but a value you
pasted into a test could be. There is no absolute "all possible secrets"
guarantee. Inspect your project's test output/logs; if that is not acceptable
for your project, use **Kurallar** or **Kapalı** — both are fully offline.

The canonical `decision.made` event records only the decision id, question-set
version, backend, model version, latency, prompt tokens, whether the fallback
was used, and the answers (label, probabilities, confidence). No raw remote
state and no request/response bodies go into receipts or logs; the SDK's own
DEBUG wire logging is filtered out.

## Key, SDK and settings

- Key environment variable: **`TYPESAFE_API_KEY`**, entered in Settings →
  **Karar sağlayıcısı** (a section separate from the model-provider catalog).
  It uses the existing secret store: the git-ignored `.env` in source mode,
  Windows DPAPI in packaged builds. Jev is a *decision* provider — planner /
  coder / reviewer routing never reads this key and no provider list shows it.
- **Saving a key performs no network call.** Only the explicit
  "Bağlantıyı sına" (Test connection) button sends one `GET /v1/models`
  request; it carries no project content. It uses the same bounded pattern as
  the backend — the async client with a **~1.5 s wallclock network deadline**
  plus a separately bounded **0.5 s close** (the per-phase httpx timeout alone
  would not bound a trickling response). SDK import and key validation are
  separate local costs and are not part of that budget; the test refuses to run
  inside an existing event loop.
- The decision backend itself reads the key from the process environment at
  decide() time only (never from `.env` files, never at import time).
- The SDK is **optional** and deliberately not a hard dependency — install it
  only when opting in to **Jev (TypeSafe)**:

  ```bash
  # Linux / macOS (source venv)
  .venv/bin/python -m pip install -r requirements-jev.txt

  # Windows PowerShell
  .venv\Scripts\python -m pip install -r requirements-jev.txt
  ```

## Runtime contract (pinned, verified against typesafe-sdk 0.7.2)

- Model pinned to **`jev-1.13.0`**; a bounded `TYPESAFE_DEFAULT_MODEL` may
  override it. Base URL is always the fixed HTTPS endpoint above.
- Answers are read from the SDK's canonical `answers` map: `choice` +
  `probabilities` + `confidence`, `score` + `probabilities` (level keys arrive
  as int or digit-string and are normalized) + `confidence`, and `noul` alone —
  `noul` has **no** separate confidence.
- Every answer is validated locally (exact question set, known labels,
  probabilities in [0,1] summing to ~1 after renormalization, chosen label =
  argmax, score within its levels, non-negative token counts). Anything
  inconsistent → typed error → rule fallback.
- The production path drives the **async** client (`AsyncTypeSafeClient`) from
  sync code: `asyncio.run` + `asyncio.wait_for` around request+read
  (**1.5 s network deadline**) and a separately bounded client close
  (**0.5 s**). Retries are off by default. The deadline bounds the *network
  call only* — SDK import, sanitization and the baseline rerun are separate
  local costs, so do **not** read the 1.5 s as a guaranteed whole-run budget,
  and the deadline is not a strict cancellation guarantee for an injected
  sync client (test seam) or arbitrary library-internal blocking.

## Decision flow and safety guards

1. A check fails → the failing check's raw output is sanitized (remote backend)
   and the deterministic facts are extracted in code (exit code, timeout flag,
   command, error block, changed paths, baseline result).
2. **Baseline rerun** (best effort): the same check re-runs against the
   pre-change snapshot in a throwaway detached worktree. Any failure → "no
   baseline evidence" (never a hard error).
3. The backend decides; on any backend error the deterministic rules decide
   (`fallback_used=True`).
4. Action mapping (only `failure_kind`'s own confidence gates the action):

   | Answer (confident) | Action |
   |---|---|
   | `code_bug` / `test_needs_update` | continue fix loop (classification attached to the fix prompt) |
   | `missing_dependency` / `environment_or_tooling` | stop → `NEEDS_USER` with an explanation |
   | `flaky_or_timeout` | re-run the check once |
   | `unrelated_preexisting` | mark pre-existing, continue to review |
   | low confidence (any) | continue fix loop (today's behaviour) |

5. **`unrelated_preexisting` may skip the fix loop only when the same
   check actually FAILED on the baseline run.** A baseline that passed, was
   never obtained, timed out or errored cannot authorize a skip — the action
   is downgraded to the fix loop while the recorded result still shows the
   model's own answer. "Same check failed" is not proof that the fault is
   identical; and because the rules classify from a single extracted error
   block, a check reporting several different errors is judged on the most
   specific/last one.

## Evaluation (offline harness: `tools/evaluate_decision_triage.py`)

Dataset: **67 labelled fixtures** — **62 synthetic**, **5 `real_capture`**
(Python and Node.js only; **no real Go capture** — Go scenarios exist but are
hand-written synthetic ones). Metrics are computed against each fixture's gold
semantic kind, never against the algorithm's own labels.

Offline rule-baseline run (verified 2026-10-02):

| Metric | Value |
|---|---|
| kind accuracy | 48/67 = 0.716418 |
| `needs_user` precision (stop classes) | 17/20 = 0.85 (below the 0.90 target) |
| `needs_user` coverage | 17/25 = 0.68 |
| calibration ECE / multiclass Brier | 0.127940 / 0.413082 |
| environmental wasted-fix **proxy** | 17/25 avoided (a fixture proxy, **not** measured fix attempts) |

Commands:

```bash
# offline, rules (default) — no SDK, no key, no network
.venv/bin/python tools/evaluate_decision_triage.py

# offline threshold tune/holdout scan — REPORT ONLY, prod defaults (act 0.9 /
# escalate 0.5) are never mutated
.venv/bin/python tools/evaluate_decision_triage.py --act-threshold 0.85

# live — ONLY with explicit approval, key in the environment (no .env read)
.venv/bin/python tools/evaluate_decision_triage.py \
  --backend jev --allow-network --snapshot-output /existing/path/jev.json

# replay a live snapshot offline (threshold scan over real outputs)
.venv/bin/python tools/evaluate_decision_triage.py \
  --backend snapshot --snapshot /existing/path/jev.json --act-threshold 0.9

# small-LLM comparison (the comparator data is IMPORTED, never generated here)
.venv/bin/python tools/evaluate_decision_triage.py \
  --backend snapshot --snapshot /existing/path/jev.json \
  --llm-snapshot /existing/path/llm-snapshot.json
```

Report semantics:

- `triage_screening` — the live numeric criteria (stop precision ≥ 0.90,
  p95 < 1 s, minimum stop support). Applies to **live** runs only; offline runs
  report `not_applicable_offline`.
- `go_no_go` — **always `pending`**. This harness can never emit a release
  "go": it needs a real live run, a real-output (not only synthetic) dataset,
  a live holdout evaluation, an LLM comparator, and **measured** fix-attempt
  reduction from instrumented pipeline runs. The report lists the exact missing
  evidence instead of guessing.

**Live evaluation is pending.** No live run has been executed: it requires a
real `TYPESAFE_API_KEY` in the environment (not set in the development
environment used to write this document), a user-approved CLI invocation, and
then the holdout / LLM comparator / measured-reduction evidence above. The
current dataset is labelled synthetic-heavy and must not be described as
"50 real failures".

## Known limits

- Live accuracy, calibration, p95 latency and cost of the Jev backend are
  **unmeasured**.
- The deterministic rules never emit `test_needs_update`; the dataset includes
  five gold-labelled cases, but the rule backend assigns them a low-confidence
  `code_bug` default instead.
- One error block per failing check (multiple distinct errors → one judgement).
- In-flight native HTTP model calls still cannot be interrupted (pre-existing
  engine limitation, unchanged here).
- The decision layer is off by default and experimental.

## Test status

Reproduce with:

```bash
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS QT_QPA_PLATFORM=offscreen \
  PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider
```

What this does and does not establish (recorded locally on Linux; no pass count
is quoted as progress):

- The default suite makes **no network calls**. The real TypeSafe SDK tests use
  injected mock transports, **not** a live API.
- Skips are expected and explained: Windows-only path semantics and the
  explicitly opt-in live provider test.
- UI `npm run typecheck` and `npm run build` pass; the build reports mixed
  static/dynamic import and chunk-size warnings. Dependency consistency
  (`env -u PYTHONPATH .venv/bin/python -m pip check`) passes.
- CI targets Python 3.14; that interpreter and Windows packaging were **not**
  verified locally. **Live Jev accuracy/latency and release acceptance remain
  pending** independently of these offline results, and no live provider,
  Windows or platform-acceptance claim is made.

Re-run these checks before release; see [CONTRIBUTING.md](CONTRIBUTING.md) for
the test conventions.
