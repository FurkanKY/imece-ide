# Imece Product Plan (authoritative)

> Versionable product contract. Approved plan of
> record. This file is **tracked and published**; it is not git-ignored.
> A local `ROADMAP.md` progress journal exists on contributors' machines but is
> **git-ignored and not published**, so it is deliberately not linked from here;
> where a local note and this file disagree, **this file wins**.
> Last updated: 2026-10-06.

## 1. What Imece is

Imece is **not a classic IDE** and **not a role-based agent theatre**. A
Planner/Coder/Reviewer trio is not the product and is not mandatory.

**An independent coding agent owns a task end to end.** Imece is the layer
around it that makes that claim trustworthy and repeatable:

- owns **work** — task scope, explicit steps, a place to continue or stop;
- owns **isolation** — each attempt works in its own disposable worktree;
- owns **shared knowledge** — project rules fed to the agent as
  clearly-labelled untrusted data. Collaboration **SharedContext** is a
  separate, opt-in mechanism: the default single-agent flow **rejects it
  explicitly** rather than silently enabling it;
- owns **evidence** — deterministic verification plus a canonical audit trail
  behind every claim;
- owns **integration** — nothing lands in the user's tree except through an
  explicit, reviewable, reversible decision;
- owns **human decisions** — accept, continue, reject, ask. The human is a
  decision point, not a supervisor of every agent step.

The agent writes the code. Imece makes the result checkable.

## 2. Entities

The product model is five nouns. Everything else is detail:

| Entity | Meaning | Durability |
|---|---|---|
| **Project** | One opened source tree plus its rules, verification command and shared knowledge. | Durable |
| **Task** | A unit of desired change, stated independently of any agent or role. | Durable, reopenable |
| **Execution** | One isolated attempt at a Task by one agent, in its own worktree, with its own cost/activity record. | Durable; many per Task |
| **Result** | The **outcome** of one Execution: diff, checks run, evidence, and a verification status that may be `pass`, `fail` **or** `not_run`. A Result is not "verified" by definition. | Durable |
| **CombinedCandidate** | Several explicitly selected Results (and/or proposals) merged onto a committed baseline into a new directory outside the checkout, with conflicts and verification surfaced. | Explicit, on demand |

Invariants that follow from this model:

- A Task has **no role field**. Nothing in the product may require a planner,
  a coder or a reviewer to exist before work can start.
- Executions are **independent**. They never share a worktree, and they do not
  require a global "active run" to exist.
- A Result is **never** accepted by implication. Acceptance is a separate,
  recorded, human decision.
- **Integration is explicit and reversible, never automatic.** An Execution
  only ever writes inside its own isolated worktree. Writing into the user's
  **Source** tree happens **only** on an explicit apply decision, and that apply
  takes a **checkpoint first**, so the change stays reversible. A
  **CombinedCandidate** is materialized **isolated**, in a new directory outside
  the checkout and worktree; it is never merged into Source on its own.
  "Controlled integration" therefore does **not** mean "nothing is ever
  written in place" — it means no write is ever automatic, and every write is
  attributable to a human decision.

## 3. Milestones

Each milestone ships a **bounded, user-visible delivery** with its own
acceptance statement. A milestone is closed only when that delivery works —
not when a helper, adapter or test harness exists.

### M1 — One visible vertical: task → single agent → evidence → decision

- Enter a task in Imece, run **one** independent agent on it end to end, see the
  verification/evidence, then **continue** or **accept**.
- No mandatory planner. No mandatory reviewer. No role chooser. No new code
  path was introduced to achieve this — the default path *is* the
  single-agent path.
- Acceptance: a user can go task → evidence → accept, or task → evidence →
  continue, on a real task, without any role concept appearing anywhere in
  the flow.

**Status: IN PROGRESS — implemented, NOT closed.**

The engineering vertical **is implemented** and verified by **local fixture
end-to-end** runs. What is **not** met is the acceptance gate: **real-provider**
and **supported-platform** validation. So M1 is honestly described as
"implemented, fixture-verified, awaiting actual-provider/platform acceptance" —
never as "done", "complete", or by any percentage.

What the implemented default flow actually does:

- **Default UI:** a task composer plus a **single provider picker** — not a role
  chain. The panel shows **Çalışma** (work), **Sonuç** (result) and **Etkinlik**
  (activity). There is **no Plan tab and no role chain** in the default flow.
- **Run shape:** `task` + one `providerId` → **isolated worktree** →
  **deterministic verification evidence** → **continue** (same worktree, same
  original task) or **explicit apply / reject / checkpoint**.
- **Legacy three roles** (Planner/Worker/Reviewer) remain as a **compatibility
  backend and historical record**. They are **not the default** and are not
  planned for v1.

### M2 — Task-first main screen, real concurrency — **implemented on development branch; acceptance in progress**

> **M1 is not closed, and its acceptance gate is not met — that has not
> changed.** Real-provider and supported-platform validation was **deferred**,
> because the provider/platform environments needed to run it were not
> available. It stays a **tracked validation debt** and a **release gate before
> M5**, and it is recorded as open, not waived. It **does not block M2**: M2 is
> authorized now and is being worked on. The `m2-run-manager` development
> branch now has the task-first screen and run-indexed frontend, with local
> fixture and browser-mock checks. It is **not merged into main** and is not
> live-provider or native desktop-platform acceptance.

- The main screen is a **task list**, not a role pipeline.
- At least **two independent Executions run concurrently**, each in its own
  worktree.
- The current **global single-active-run assumption is replaced** by a run
  manager; two runs must be live at once and independently completable.
- The backend on the development branch has a **run-ID registry** with a
  ceiling of **two owned runs per project**, independent worktrees and
  cancellation, and explicit **cancel**, **continue**, **apply** and **reject**
  actions addressed by ID. Waiting proposals and retained cleanup resources
  consume capacity too. The development-branch frontend addresses operations
  by captured run ID and preserves background results across task selection.
- On that branch, **Görevler** is the primary project surface. **Araçlar**
  retains the editor, explorer, terminal, Git, LSP and debugger as secondary
  tools. A task uses one provider, not a mandatory role chain.
- Acceptance: two Tasks progress simultaneously with no shared worktree, no
  cross-talk and no lost results.

### M3 — Durable tasks and results, controlled integration — **in progress (2026-10-06)**

- The development branch implements bounded history/retry, durable native
  workspace descriptors, explicit restart continuation, and verified native
  CombinedCandidate apply/rollback. **M3 acceptance is still open**; fixture
  coverage is not supported-platform/native-desktop acceptance.
- Workspace descriptors and quiescent fingerprints are recorded in the existing
  canonical SQLite events/projection; no new database or schema migration is
  introduced. Kernel-held leases prevent concurrent ownership. Startup never
  prunes historical worktrees or automatically adopts/resumes a task.
- Orderly close cancels/drains the worker and retains pending work. On Linux,
  `ProcessRunner` uses a dedicated `PR_SET_CHILD_SUBREAPER` supervisor; only after
  root exit and `waitpid(-1) == ECHILD` does it emit a private nonce-bound receipt.
  Native process-tool and verification completion events carry that receipt;
  sealing requires positive receipts for every started command/check. Windows
  workspace fingerprints use handle-anchored no-reparse traversal. Windows
  `ProcessRunner` assigns suspended commands to a non-breakaway kill-on-close
  Job Object before resuming and emits a receipt after kernel active-process
  count reaches zero. Linux ACP uses a byte-preserving supervisor proxy; Windows
  ACP config travels over a dedicated inherited pipe and agent stdio is passed
  through under the same Job containment. Native SDK/file-only histories need no process
  receipt. These are engineering implementations, not cross-platform acceptance. When positively sealed, explicit continuation claims that
  same workspace and RunID/TaskID, checks project/Git/source/content identity,
  and creates a fresh execution and verification. Historical proposal/checkpoint
  authority is not revived. A free lock without a quiescent seal does not prove
  orphaned subprocesses died: unsealed abrupt-crash workspaces fail closed.
- The `m3-task-history` development branch reopens bounded native-agent history
  from the existing SQLite store. Its bounded retry slice explicitly reruns
  only a latest failed/cancelled attempt in the same task/provider chain, using
  the canonical prompt, a new RunID/attempt, and a fresh isolated workspace.
  That workspace snapshots the current Git-visible source state (tracked
  changes and non-ignored untracked files included by the existing workspace
  contract), never the old attempt's worktree. SQLite transaction checks
  serialize concurrent retry admission. Retry does not transfer proposals,
  checkpoints, credentials, mentions, or worktree authority; active, interrupted,
  malformed, changed-provider, and otherwise ineligible histories fail closed.
  Copying an older task still creates a new draft without automatically starting
  a run.
- Same-database restart fixtures, independent-connection retry races, and
  controlled browser history/retry responses are tested separately. Neither
  substitutes for native desktop restart or actual-provider acceptance. The
  focused checks are `node scripts/run-history-acceptance.mjs` and
  `node scripts/run-retry-acceptance.mjs` from `web/ui`.
- One or two explicitly selected owned native results sharing a clean,
  top-level committed baseline can form a CombinedCandidate outside Source.
  Conflicts are surfaced; verification must have unchanged, complete fingerprints
  and PASS before explicit apply. Apply writes exact bytes/modes with a checkpoint;
  explicit rollback checks source/checkpoint provenance and restores bytes/modes.
  Candidate receipts and apply/rollback history survive reopening SQLite.
- The combined fixture/UI check is `node scripts/m3-acceptance.mjs`; details and
  reproducible acceptance gates are in [M3-ACCEPTANCE.md](M3-ACCEPTANCE.md).
- M3 acceptance remains open: actual-provider/supported-platform desktop
  close mid-task → explicit continuation → verified candidate → apply → rollback.
  An isolated Linux QtWebEngine `ShellWindow.closeEvent` fixture verifies orderly
  drain, retained worktree, same-database reopen and same-ID continuation; it is
  not actual-provider/platform release acceptance. The Win32 filesystem walker
  and Job Object supervisor are not yet exercised on a Windows runner in this
  session; Windows ACP Job-backed stdio integration is implemented but likewise
  awaits execution on a real Windows runner.
  Linux subreaper supervision covers process tools, deterministic checks and ACP
  CLI producers. Authenticated cancellation receipts can seal interrupted work
  without turning cancellation into PASS; non-Linux process-backed work remains
  fail-closed.

### M4 — Two people: secure LAN pairing and control — **in progress (engineering slices)**

- The opt-in first engineering slice adds TLS-only, literal-private-IPv4 control
  listeners, out-of-band certificate-pinned clients, bounded one-use member
  invitations, and finite authenticated metadata snapshot/task-status calls.
  Existing loopback HTTP remains loopback-only and unchanged by default. No
  code/proposal bytes are accepted on this control transport.
- Pair credentials are ephemeral and bound to a preselected member identity;
  member role checks stay in Coordinator. Listener shutdown revokes identities
  created by that listener. No listener starts automatically or from import.
- The second engineering slice adds an opt-in, separately bound TLS proposal
  listener/client. Its only code routes explicitly publish one strict bounded
  artifact or fetch one named immutable ID; the existing control channel remains
  metadata-only. Authenticated principal must equal proposal owner; existing
  provenance, CAS, duplicate-ID and atomic private-ref publication checks remain
  authoritative. Revocation fences both listeners. Transport never writes Source;
  candidate assembly/integration remains a separate explicit decision. See
  [M4-PROPOSAL-CHANNEL.md](M4-PROPOSAL-CHANNEL.md).
- A third engineering slice exposes explicit owner-side LAN startup, two-listener
  lifecycle, configured-member invitation/revocation and guarded out-of-band invite
  display/copy in the existing native UI. Status exposes listener endpoints and
  certificate pin, never private keys or credentials. Legacy loopback secret
  sharing/local handoff is refused in LAN mode. This is still experimental UI,
  not two-machine acceptance; see [M4-OWNER-LAN.md](M4-OWNER-LAN.md).
- Participant join/manual metadata refresh/disconnect and explicit selected-file
  preview/publication/single-ID receipt fetch are implemented. Ambiguous publication
  retains its immutable ID for checksum reconciliation, without blind retries.
  Owner-selected shared proposals now use durable CombinedCandidate verification,
  confirmed Source apply and checkpoint rollback, including the existing editor
  refresh path. The workflow and boundary review are in [M4-DELIVERY.md](M4-DELIVERY.md).
- Real two-machine native LAN acceptance remains open. Separate-checkout pinned
  loopback TLS, browser fixtures and the operational threat review are engineering
  evidence, not LAN deployment acceptance. At the user’s direction M5 packaging
  engineering has started while these acceptance items remain open.
- Acceptance remains: two machines on a LAN pair with authentication, exchange
  control state, transport a proposal on the separate channel, and integrate it
  explicitly — with no unauthenticated listener ever exposed.

### M5 — Supported-platform acceptance and packaged release — **in progress (packaging engineering)**

- The user explicitly authorized M5 engineering with M1/M3/M4 acceptance still
  deferred/open. Frozen process/ACP dispatch, structural package preflight,
  per-file SHA-256/link manifests, Linux XDG paths and platform-specific
  Node/PTY payloads, offline license inventory and isolated Windows/Linux smoke
  workflow preparation form the first slices. A local Linux manual-test archive
  now passed native Wayland smoke and repo-independent extraction testing; see
  [M5-PACKAGING.md](M5-PACKAGING.md) and [MANUAL-ACCEPTANCE.md](MANUAL-ACCEPTANCE.md).
  Current Windows package execution, clean-machine acceptance and publication
  are not claimed.
- **End-to-end acceptance on the supported platform set** (Windows first,
  Linux as supported today), then **packaged releases**.
- Acceptance: a clean machine installs the package, runs a real task on a real
  provider, and the result is accepted or rolled back — on every supported
  platform.

**Value ladder:** M1–M3 are useful **solo**. M4 makes it **team-complete**.
M5 is the **release** gate. M1 actual-provider/platform acceptance remains a
separate deferred, tracked packaged-release gate; its deferral permits M5
engineering, not acceptance or publication. M3 remains in progress; its Windows and
native-desktop acceptance is explicitly deferred, not completed. M4's engineering
slices do not close its two-machine/proposal acceptance.

## 4. What is explicitly not v1 scope

- Mandatory roles (Planner/Coder/Reviewer, or any role requirement).
- Automatic agent teams or multi-model races.
- Jev as a mandatory step, or any coordination intelligence that gates work.
- Learned/project memory.
- Routing, a new IDE shell, or a new debugger.
- WAN relay, cloud accounts.
- Large new CLI or editor plugin ecosystems.
- A new-project wizard.

The existing editor, terminal, LSP and debugger are **kept as secondary
tools**. They are not the product and are not being deleted — no time is spent
removing them, and none is spent growing them either.

## 5. Reusable assets (do not rebuild)

Already implemented and deliberately reused rather than replaced:

- worktree-based execution isolation;
- deterministic verification;
- the canonical audit trail;
- metadata CAS writes;
- private proposals;
- combined candidates with real three-way merges (materialized **isolated**,
  in a new directory outside the checkout and worktree);
- the role-free single-agent default path, now the product's default.

## 6. Honest status

### M1 status

- **The role-free vertical exists.** It is implemented and verified by local
  fixture end-to-end runs. This document does **not** say otherwise, and it
  does not say M1 is finished either.
- **M1 is IN PROGRESS, not closed.** The unmet gate is **actual-provider** and
  **supported-platform** acceptance. That validation was **deferred** — the
  provider/platform environments required to run it were unavailable — so it is
  carried as **tracked validation debt**, not as a waived requirement. Real
  provider runs and two-computer / LAN / Windows end-to-end remain unverified,
  so no live-quality or live-readiness claim is made anywhere in these docs.
  It remains a **release gate before M5** and does **not** block M2.
- There is **no mandatory Planner and no mandatory Reviewer** in the default
  flow, and **no new code path** was introduced for M1 — the default path is
  the single-agent path.

### What the default flow is today

- **Default UI:** task + **one** provider, with **Çalışma / Sonuç / Etkinlik**
  (work / result / activity) and **no Plan tab, no role chain**.
- **Run shape:** task + `providerId` → isolated worktree → deterministic
  verification evidence → continue (same worktree, same original task) →
  explicit apply / reject / checkpoint.
- **Legacy three-role** Planner/Worker/Reviewer remains as a **compatibility
  backend and historical record only**. It is not the default and not planned
  for v1.

### What is still not done

- The task-first shell and multi-run frontend are **development-branch
  changes**, not a published main-branch delivery yet. Local mock acceptance
  exercises the visible UI, but the complete two-run frontend/Qt-host flow on
  supported desktop platforms and actual providers remains **unverified**.
- **M3 is not closed.** Its bounded engineering flows are implemented and
  fixture-tested. Native-desktop acceptance, real Windows execution of the
  newly implemented no-reparse walker and process/ACP Job supervisors, and
  unsealed abrupt-crash producer handling remain explicit gates/limits; see
  [M3-ACCEPTANCE.md](M3-ACCEPTANCE.md).
- **M4 engineering includes explicit participant delivery and verified owner
  integration; real two-machine acceptance remains open. M5 packaging engineering
  has started with a local Linux manual-test package and Wayland smoke.** M1's
  deferred acceptance remains a packaged-release gate, not a ban on M5 engineering.

### M2 engineering checkpoint — development branch only

- Headless Qt/Git/SQLite fixtures exercise two live runs in distinct
  worktrees, targeted cancellation while the other run continues, independent
  follow-up/results, explicit apply/checkpoint and reject, capacity limits,
  and source-hash conflict refusal for overlapping proposals.
- Failed admissions and delayed activity shutdown retain or release resources
  explicitly. A later failed start must not hide an earlier run from control.
- The registry is **process-local** with bounded terminal history. This does
  not implement M3's task/result reopening after restart.
- The frontend keeps per-run proposals, evidence, follow-up drafts, activity
  and checkpoints. Captured-ID operations and root/revision fences prevent
  late replies from changing another task or conferring wrong-project authority.
  Activity is bounded to 500 items per run; clean terminal history to 32,
  without evicting running, review-ready, busy or uncertain records.
- Three reproducible browser checks cover controlled admission/response races,
  independent mock runs and source-conflict refusal, and the task-first UI.
  The UI check creates two running tasks, refuses a third, cancels A while B
  continues, follows up B, opens its diff, applies/restores its checkpoint,
  rejects C, and reaches evidence and apply controls at 320px. **These are
  mock/fixture checks, not live-provider, native worktree or Windows desktop
  acceptance.** The backend fixtures separately exercise real Git worktrees.
- Run the frontend checks from `web/ui` after typecheck/build:

  ```sh
  node scripts/run-state-acceptance.mjs
  node scripts/mock-runs-acceptance.mjs
  node scripts/task-workspace-acceptance.mjs
  ```

  They start owned, strict-port, literal-loopback Vite servers, block external
  browser requests, and use `CHROME_BIN` (default `/usr/bin/google-chrome`).
  The UI check saves screenshots under `M2_SCREENSHOT_DIR` (default the
  system temporary directory's `opencode` folder). The Ubuntu UI CI job runs
  the same checks. Browser CI does not substitute for native desktop acceptance.
- Unknown admission acceptance stays fail-closed rather than allowing a blind
  retry. This is process-local safety, not M3 durability or a recovery guarantee.
- Reproduce the backend scope on this branch with `python -m pytest -q
  tests/test_run_registry.py tests/test_agent_application_e2e.py
  tests/test_agent_bridge_regressions.py` in the configured development
  environment. These are local fixture checks, not live-provider or Windows
  desktop acceptance.

### Collaboration honesty

- **Collaboration is an implemented experimental foundation, not a team
  product.** The metadata-first session, explicit private proposals, combined
  candidates and the loopback control core exist and are locally exercised
  ([COLLABORATION.md](COLLABORATION.md)). They are **not integrated with the
  default single-agent flow** and are **not** M4's two-machine product. An
  explicitly constructed TLS LAN control/proposal library slice exists, but the
  default listener remains loopback-only; there is no automatic LAN activation,
  production certificate/pin UI or WAN support. Two-computer/LAN and Windows
  end-to-end remain **unverified**.
- The default role-free flow **rejects** collaboration **SharedContext
  explicitly** (`collab_unsupported`) rather than silently enabling it. A
  silent default would be the failure mode this plan forbids.

### Provider honesty

- The **provider catalog is capability-limited**: not every catalog entry can
  drive the single-agent path, and the UI states the reason per provider.
  A provider the engine cannot run is refused with a reason, never silently
  substituted.
- **ACP (agent-CLI) providers report unknown cost and token usage**, shown as
  `—`. No cost or token figure is invented for a provider that cannot report
  one.

## 7. Delivery and acceptance rules

These rules are binding on how milestones close.

- **Tests are not the progress metric.** A green suite does not mean a
  milestone is done, and no test count is quoted as product progress.
- **No real-provider claims.** Fixture-based end-to-end runs may advance the
  engineering, but actual provider and platform validation is a **separate,
  unmet gate**. Fixture E2E passing never substitutes for a real run.
- **No day counts and no percentages.** No ETA, no completion percentage, no
  "X% done".
- **Bounded delivery closes a milestone.** A milestone closes on its stated
  acceptance, end to end and user-visible. Helper-only or
  infrastructure-only work is legitimate engineering but is **not** milestone
  closure.
- Historical test counts elsewhere in these docs (for example the previously
  recorded targeted scopes) are **historical targeted test results** for those
  scopes. They are not evidence that any part of this plan is complete.

### Validation evidence (not a product-progress metric)

No pass count is quoted as progress. What matters is *which kind* of evidence
exists, and its limits. Reproduce it with:

```bash
python -m pytest -q tests/test_agent_execution.py tests/test_agent_application_e2e.py \
  tests/test_agent_bridge_regressions.py tests/test_agent_evidence_regressions.py \
  tests/test_agent_security_regressions.py tests/test_agent_factory.py
```

What that scope actually exercises:

- **Real end-to-end harness, scripted model.** The application-level run uses
   the real **headless Qt bridge** (not a rendered desktop shell), real **Git**, real **SQLite** metadata, a real
  **worktree**, the real `NativeWorkerAdapter`, and a **SCRIPTED model
  backend** — with a **real verification subprocess** actually executed. The
  model output is scripted; the surrounding machinery is not mocked.
- **Covered behaviours** (new/extended tests in
  `tests/test_agent_execution.py`, `tests/test_agent_application_e2e.py`,
  `tests/test_agent_bridge_regressions.py`,
   `tests/test_agent_evidence_regressions.py`, `tests/test_agent_security_regressions.py`,
   `tests/test_agent_factory.py`, and adjacent legacy/native bridge regressions):
  follow-up continuation; apply with checkpoint; stale-WIP rejection; reject;
  cancel; the root/authority checks; and lifecycle streaming with **paginated**
  history beyond the first page of events.
- **No-follow evidence fingerprint, robust and bounded.** One
  **pre-verification** inventory is pinned, and the **same originals** are
  compared **before and after** verification. Standard generated cache
  directories are excluded **only when they are new**; a changed or
  pre-existing one is not waved through. Any input mutation, any mode change,
  or an **incomplete** inventory **invalidates** a PASS. A displayed PASS also
  requires a **canonically matching receipt** plus matching plan checks, and a
   **final-diff capture mismatch invalidates the displayed PASS**.
- **Credential and resource safety:** model/execution failure diagnostics are
  fixed-message canonical facts rather than raw provider exceptions; a scripted
  secret-bearing exception is absent from persisted events. Directory inventory
  releases its owned descriptors. On platforms without the required nofollow
  directory-handle capability, inventory is incomplete and cannot prove PASS;
  Windows/platform acceptance has not been demonstrated by Linux tests.
- **Actual provider attribution:** an agent run stores only its selected
  `agent_provider`, not unused legacy default planner/reviewer assignments.
- **Optional activity streaming degrades safely.** If the optional
  `ActivityStreamer` fails, a successfully admitted run still returns its
  `runId` and stays **cancellable** — it never becomes a falsely reported
  *failed start*.
- **Default frontend authority.** A **known follow-up rejection** preserves the
  ready state and its evidence; a **rejected result or a restore clears** the
  stale evidence; a **pending** follow-up root that goes A → B → A
  **invalidates authority**.

### Browser validation — reported honestly

- A browser smoke run against a **mock bridge** (a local, uncommitted script)
   exercised the decision path end to end: **0 application failures**, including
   explicit apply, checkpoint confirmation and rollback, in addition to
   continue/reject. The script is a local developer tool, not part of this
   repository, so it is not reproducible from a clone.
- It runs against a **browser MOCK bridge**: **no real provider** and **no real
  Git** were exercised there. It validates UI wiring only.
- **Known vendor warning:** 4 Monaco *"Canceled"* diff-disposal page errors were
  still present. They are **separated explicitly** from application failures
  and remain an **open known vendor warning**. This is **not** a
   "zero browser errors" claim — it is "zero *application* failures, with 4
  known Monaco warnings tracked separately".
- UI **typecheck and build pass**, with pre-existing warnings that are not
  claimed as clean.

## 8. Delivery order

1. **M2 — active now, and authorized.** Task-first main screen plus a run
   manager: a **run-ID registry** with a bounded concurrency ceiling,
   **independent per-run leases**, and explicit **cancel** / **apply** addressed
   to one run by ID, replacing the current global single-active-run assumption.
   Implementation is **starting**; nothing here claims it is built.
2. **M1's deferred acceptance gate is not dropped.** Actual-provider and
   supported-platform validation is **tracked validation debt**, deferred
   because those environments were unavailable. It is a **release gate before
   M5** and does not block M2. M1 stays **IN PROGRESS, not closed** — the
   deferral is a scheduling fact, not an acceptance.
3. The collaboration feature freeze applied during M2. The user has explicitly
   authorized M4 work while M3's Windows/native acceptance is deferred. Keep new
   collaboration scope within M4's explicit opt-in, authenticated and bounded
   slices; do not silently enable SharedContext or conflate control with code
   proposal transport.
4. M3's bounded engineering flows are implemented on the development branch,
   with Windows/native acceptance **explicitly deferred and not accepted/closed**.
   Per user direction, do not mark M3 complete; continue bounded M4 engineering
   in parallel without weakening M3's fail-closed platform checks. M4's real
   two-machine and separate-proposal-channel acceptance, then M3/M1 platform
   gates and M5 release acceptance, remain outstanding.
