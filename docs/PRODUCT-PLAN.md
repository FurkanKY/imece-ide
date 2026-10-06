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

### M2 — Task-first main screen, real concurrency — **implemented on separate branch; not merged**

> **M1 is not closed, and its acceptance gate is not met — that has not
> changed.** Real-provider and supported-platform validation was **deferred**,
> because the provider/platform environments needed to run it were not
> available. It stays a **tracked validation debt** and a **release gate before
> M5**, and it is recorded as open, not waived. It **does not block M2**.
> M2 is implemented on the public
> [`m2-run-manager` branch](https://github.com/FurkanKY/imece-ide/tree/m2-run-manager),
> not merged into `main`. The branch has fixture/mock validation; consult its
> [current CI results](https://github.com/FurkanKY/imece-ide/actions/workflows/verify.yml?query=branch%3Am2-run-manager)
> before integration. Provider and
> supported-platform acceptance remain pending; no live-provider or
> native-platform quality claim is made. `main` remains IDE-shaped and
> single-run.

- The main screen is a **task list**, not a role pipeline.
- At least **two independent Executions run concurrently**, each in its own
  worktree.
- The current **global single-active-run assumption is replaced** by a run
  manager; two runs must be live at once and independently completable.
- The branch implementation uses a **run-ID registry** with a bounded
  concurrency ceiling (e.g. at most 2 concurrent runs), **independent leases**
  per run, and explicit **cancel** and **apply** actions that address one run by
  ID. This remains the acceptance contract; branch implementation does not
  establish provider/platform acceptance.
- Acceptance remains pending: two Tasks must progress simultaneously with no
  shared worktree, no cross-talk and no lost results; provider/platform
  acceptance also remains pending.

### M3 — Durable tasks and results, controlled integration — **not started**

- Tasks, Executions and Results **survive a restart** and can be reopened,
  continued or re-run.
- A **verified CombinedCandidate** is integrated through an explicit,
  controlled decision path with **rollback**.
- Acceptance: close the app mid-task, reopen, continue the task, integrate a
  verified candidate, roll it back cleanly.

### M4 — Two people: secure LAN pairing and control — **not started**

- **Secure LAN pairing and control**, plus **proposal transport on a channel
  distinct from control/events**. Control/coordination and code proposals stay
  **logically distinct** — the separation is a property of the model, not a
  premature ban on any particular transport. Whether they may ride the same
  wire is a concrete design question deferred to M4, not a rule decided here.
- Acceptance: two machines on a LAN pair with authentication, exchange control
  state, transport a proposal on the separate channel, and integrate it
  explicitly — with no unauthenticated listener ever exposed.

### M5 — Supported-platform acceptance and packaged release — **not started**

- **End-to-end acceptance on the supported platform set** (Windows first,
  Linux as supported today), then **packaged releases**.
- Acceptance: a clean machine installs the package, runs a real task on a real
  provider, and the result is accepted or rolled back — on every supported
  platform.

**Value ladder:** M1–M3 are useful **solo**. M4 makes it **team-complete**.
M5 is the **release** gate. M1's acceptance gate is still open (deferred,
tracked); M2 is implemented on a separate branch and awaits acceptance/merge.

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

- On `main`, the **application shell is still IDE-shaped** — project explorer, Monaco
  editor, terminal, Git surface, LSP, debugger — and those stay as
  **secondary** tools. The **task-first main screen and real concurrency (M2)
  are implemented on the separate `m2-run-manager` branch**, not merged; the
  global single-active-run assumption still holds in `main`.
- **Restart durability (M3) is not done.** Tasks and results do not yet
  survive an app restart and get reopened/continued.
- **M3, M4 and M5 are not started. M2 implementation is on a separate branch;
  provider/platform acceptance is pending.** M1's deferred acceptance gate remains open and stays a release
  gate before M5.

### Collaboration honesty

- **Collaboration is an implemented experimental foundation, not a team
  product.** The metadata-first session, explicit private proposals, combined
  candidates and the loopback control core exist and are locally exercised
  ([COLLABORATION.md](COLLABORATION.md)). They are **not integrated with the
  default single-agent flow** and are **not** M4's two-machine product. The
  loopback listener is `127.0.0.1`-only; **no LAN pairing, LAN/WAN deployment or
  TLS exists**, and two-computer, LAN and Windows end-to-end are **unverified**.
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

1. **M2 — implemented on the separate `m2-run-manager` branch, not merged.** Task-first main screen plus a run
   manager: a **run-ID registry** with a bounded concurrency ceiling,
   **independent per-run leases**, and explicit **cancel** / **apply** addressed
   to one run by ID, replacing the current global single-active-run assumption.
   Branch validation is fixture/mock-based; integration depends on current CI
   results, and provider/platform acceptance remains pending. This does not claim
   live-provider or native-platform quality, nor that `main` has M2.
2. **M1's deferred acceptance gate is not dropped.** Actual-provider and
   supported-platform validation is **tracked validation debt**, deferred
   because those environments were unavailable. It is a **release gate before
   M5** and does not block M2. M1 stays **IN PROGRESS, not closed** — the
   deferral is a scheduling fact, not an acceptance.
3. **No new collaboration features** while M2 is in progress; only **blocking
   bug fixes** in existing features. The existing collaboration foundation is
   frozen as-is until M4 owns it.
4. Then M3, M4, M5 in order — all currently **not started**.
