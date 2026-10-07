# Collaboration (metadata, explicit proposals + loopback control)

## Status, stated plainly

**This is an implemented experimental foundation, not a team product.**

- It is **implemented and locally exercised** — real Git, real SQLite metadata,
  real hub stores, a real verification subprocess — but only in **local,
  offline, single-machine** conditions.
- It is **not connected to the default single-agent flow** described in the
  [README](../README.md). An attached collaboration shared context is **refused
  explicitly** (`collab_unsupported`), never silently accepted.
- The M4 first engineering slice ([details](M4-LAN-FIRST-SLICE.md)) now
  supports an **explicitly constructed** TLS-only listener on a literal private
  IPv4 address, pinned client TLS, and
  one-use member pairing for bounded metadata control. Existing default listener
  remains literal `127.0.0.1` HTTP. The second opt-in slice now provides a
  **separate TLS proposal-byte listener** limited to explicit one-artifact publish
  and named fetch; control/events remain metadata-only. Neither listener writes
  Source or starts automatically. This is not yet the M4 two-machine product:
  local owner-side LAN start/invite/revoke UI is now available as a third
  experimental slice ([details](M4-OWNER-LAN.md)). Remote peer join, proposal
  capture/publish/fetch UI, explicit cross-host Source integration and actual
  LAN/two-machine acceptance remain open.
- **Two-computer/LAN and Windows end-to-end are unverified.** Local TLS tests
  use a temporary certificate and literal loopback only; they do not demonstrate
  firewall, real NIC, multi-host routing, or Windows-native behavior.
- The owner-facing **Ortak ürün** view is a **poll of local metadata at most
  every five seconds** while the tab is visible — a deliberately limited local
  experiment, not a synchronized multi-user view.
- The user has authorized the bounded M4 first slice while M3 platform/native
  acceptance is deferred. Further work remains opt-in and within
  [PRODUCT-PLAN.md](PRODUCT-PLAN.md); this document does not broaden the scope.
- **No pass count below is product progress.** Each section states *what* is
  covered rather than how many cases passed. To reproduce the whole offline
  collaboration scope:

  ```bash
  python -m pytest -q tests/ -k collab
  ```

  That is a **local, offline** scope. It is **not** a real-provider, LAN,
  Windows-native or platform-acceptance claim, and no number of passing cases
  is a milestone result. The milestone scope of record is
  [PRODUCT-PLAN.md](PRODUCT-PLAN.md).

`collab_runtime` lets two or more checkouts of the **same repository** share a
tiny, explicit, offline collaboration session: one shared goal/decisions/
interfaces record, one task list with advisory scopes, and a binding artifact
that agents read as clearly-labelled untrusted data.

This is deliberately **METADATA ONLY for its session commands** — and the
code steps that exist are EXPLICIT and separately named:

- **Metadata commands** (`init`, `join`, `status`, `sync`, `context-set`,
  `task-set`, `bind`) never read, transport or modify any code. Hubs are
  **local bare git repositories on a shared filesystem**. Network, SSH and
  URL-like Git remotes are rejected by design. These CLI commands do not start
  a server. The separate, explicitly started Python adapter below offers
  authenticated loopback metadata requests, not network Git or LAN deployment.
- Only the source checkout's **committed HEAD** is read, plus the one
  explicitly written, **git-ignored** binding artifact
  (`.imece/shared-context.json`). Nothing else in your checkout is touched:
  no index, no refs, no config, no branches.
- **Explicit code publication** is a separate, deliberate step
  (`proposal-publish`): it captures ONLY the file paths you name and
  publishes them as private immutable hub refs. The metadata session is
  updated alongside in one atomic push; **no source history is copied**, no
  unselected code is ever uploaded, and your index/HEAD/branches/config stay
  untouched. Scopes are **advisory** — out-of-scope selections are reported,
  never silently accepted. **No redaction ever modifies code**: anything you
  explicitly select (including embedded secrets inside those files) is
  shared verbatim — review your selection.
- `proposal-list` prints **metadata receipts only** (no file contents are
  returned, though each bounded artifact is currently fully transferred to
  validate them). The **candidate** step materializes explicitly selected
  proposals plus the session base's committed baseline into a NEW directory
  OUTSIDE your checkout; on conflict nothing is materialized; verification
  runs only with `--verify`, fingerprints the result (persistent changes
  only) and is not a process/filesystem sandbox.
- What you put into the shared context is **explicitly shared**: it becomes
  visible to collaborators on the hub and is subsequently included (as
  untrusted data) in prompts of the chosen model provider during agent runs.
- Context provenance (revision + content hash) is an **integrity checksum,
  not authenticity**. The hash covers the context sub-object ONLY: it
  **detects changes to the shared context relative to the supplied hash; it
  does not detect edits to other envelope fields and it is not
  authentication**. It never proves that a collaborator (or their model
  provider) is trustworthy. Treat collaborators as trusted parties; the
  runtime only guarantees bounded, well-formed transport.
- The worktree copy of the artifact is a **point-in-time capture**: a local
  actor with filesystem access can still edit the file afterwards; it is not
  immutable storage.

**Explicitly missing:** enforced scope locks; automatic task/proposal publication
or candidate application; Jev coordination; multi-session hubs; production
certificate/invitation UI; actual two-host LAN deployment/acceptance and full
cross-machine candidate-to-Source integration; pushing a candidate back to your
normal Git remote; Windows-native validation. Local TLS control/proposal tests do not imply
these capabilities. Later M4 slices build on the same hub format.

## Product direction and next slices

> **Product direction is decided in [PRODUCT-PLAN.md](PRODUCT-PLAN.md)**
> (the authoritative, tracked plan of record). Short form: an independent
> coding agent owns a task end to end — there is **no mandatory
> Planner/Coder/Reviewer role model** — and Imece owns work, isolation, shared
> knowledge, evidence, integration and human decisions. Entities are Project /
> Task / Execution / Result / CombinedCandidate. Milestones are M1 role-free
> vertical, M2 task-first screen with concurrent independent executions, M3
> durable tasks plus controlled candidate integration, M4 secure LAN
> pairing/control with proposal transport on a separate channel, M5
> end-to-end platform acceptance and packaged release.
>
> **M1's real-provider/supported-platform acceptance gate is deferred and still
> open** — tracked validation debt, not a closed item. M2 remains in development;
> M3 is in progress with Windows/native acceptance deferred, not completed. M4's
> opt-in TLS pairing/control first slice is implemented, while its two-machine
> and proposal-channel acceptance remain open. The table below is an inventory,
> not a promise that these slices are product-accepted.

The goal is an autonomous, shared development environment: people keep their
own agents and providers, while working toward one product goal with shared
context. Coordination should reduce repeated explanations and late integration
work, not require a human to supervise every agent step. Task scopes in this
slice describe intended work; they do not restrict an agent's tool permissions.

| Slice | Status | Deliverable |
|---|---|---|
| Shared session and context | Implemented, offline-tested | Two client stores, guarded metadata updates, task overlap reports, explicit context binding and prompt consumption. |
| Private code proposals | Implemented, offline-tested | Explicitly selected task changes published as private immutable hub refs with context provenance; metadata receipts on listing; nothing published to the project's normal remote. |
| Combined candidate | Implemented, offline-tested (local two-client) | Combine selected proposals with the committed baseline in a new isolated directory, real three-way merges with structured conflicts, optional explicit verification with honesty statuses. |
| Local metadata control core | Implemented, offline-tested | Owner-configured credential gate, owner/assignee permissions, strict revision writes and bounded restart/reconnect replay; Python interface only, no listener or CLI integration. |
| Loopback HTTP/JSON control | Implemented, locally HTTP-tested | Explicit native-client listener on literal 127.0.0.1, finite authenticated metadata operations, bounded replay, safe framing/errors and stop-and-replace credentials. Existing loopback defaults remain unchanged. |
| Native-client revision subscription | Implemented, locally socket-tested | Explicit authenticated SSE subscription with Git-backed replay observation, bounded workers/subscribers and drain-safe shutdown. No automatic client, prompt refresh, UI or selective routing. |
| Native revision consumer | Implemented, locally socket/checkpoint-tested | Explicit reconnect worker, bounded notification inbox and durable consumed cursor advanced only by host acknowledgement/reset assertion. By itself it does not apply context or prove a worker is idle. |
| Python native-worker safe point | Implemented as opt-in Python factory wiring; canonical binder, bounded replay-lag wait and explicit finite snapshot client | Host may provide `LoopbackSnapshotClient.snapshot` as the safe-point snapshot provider. No automatic startup, UI/CLI pause, terminal recovery, fresh CAS/publication authority, LAN or real-model E2E. |
| Read-only loopback snapshot client | Implemented, locally socket-tested | Explicit authenticated POST `/v1/snapshot`; one fresh AF_INET connection per call, fixed five-second connect/idle timeout and bounded strict response parsing. It does not start/own the consumer, retry, replay or acknowledge. |
| Explicit native task-status commands | Implemented, locally socket/Git-tested | Separate opt-in `LoopbackTaskClient`; exactly one authenticated status command with an explicitly reviewed CAS revision, immutable receipt and honest uncertain-outcome failures. No snapshot/retry, native acknowledgement or automatic run lifecycle integration. |
| First TLS LAN control slice | Implemented, local TLS-tested only | Separately documented opt-in literal-private-IPv4 TLS listener, pinned client, one-use pre-bound invitations, ephemeral member credentials, metadata snapshot/task status. No auto-start, proposal bytes, remote UI or two-machine acceptance. |
| Application native collaboration beta | Implemented, offline bridge/E2E and browser-mock tested | Composer preview + explicit local approval, credential-free opaque run handle, native-only fail-closed admission, per-execution consumer lifecycle, private leased cursors, follow-up replay and explicit reset consent. Loopback only; the Owner Session tab below can configure/start it explicitly. |
| Application shared delivery | Implemented, local two-checkout bridge/E2E and browser-mock tested | Accepted native worktree binding → selected immutable publication ticket → explicit private-hub publication → metadata proposal selection → new combined candidate with separately authorized verification. No normal-remote push, automatic publication, in-place apply or LAN/WAN. |
| Owner/session setup | Implemented, real local setup→run→candidate E2E and browser-mock tested | Existing application UI provides explicit local setup and loopback start/stop. The new TLS LAN listener is library-only in this first slice; it has no application UI or automatic startup, and does not mutate source. |
| Shared product view | Implemented, targeted local Git/bridge and browser-mock tested | Owner-facing goal/decisions/interfaces, member/task state, explicit new queued tasks, advisory overlaps, CAS context/status changes, on-demand proposal receipts and a scoped local candidate verification record. No remote participant UI, existing-task reassignment or automatic agent actions. |
| Broader entry points | Later | Easier remote connectivity and a limited new-project bootstrap feeding the same development loop. |

The first product experiment is two developers splitting frontend and backend
work on an existing repository. Measure repeated context explanations, late
incompatibilities, manual conflict-resolution time and human takeovers. The
local two-client tests below validate mechanics, not those real-world benefits.
Real two-computer/provider runs, Windows behavior and shared-filesystem failure
modes still need validation. Local HTTP mechanics are tested below; no live
model or LAN/WAN validation is implied.

### Next direction: lightweight coordination, not constant synchronization

The coordination direction is a lightweight coordinator hosted by the person
opening the session. Agents and model calls stay on each participant's machine;
the coordinator owns shared revisions and meaningful coordination events, not
every agent step. The local control core below implements the first narrow
contract, with an authenticated loopback HTTP adapter and explicit native-client
revision streams. LAN/WAN connectivity remains a design direction, not an
implemented mode.

- Separate a small control/event channel (goals, context changes, task impact,
  ready checkpoints) from the less frequent code-proposal channel. Reuse the
  existing proposal/context validation and publication guarantees.
- Prefer event delivery with reconnect/replay (for example HTTP commands plus
  SSE), not repeated full-context polling. Selectively notify affected tasks;
  unrelated metadata changes must not restart agents or trigger model calls.
- Keep a complete local context snapshot, inject task-relevant context into
  prompts, and consume updates at explicit safe points. Do not silently mutate
  an in-flight agent's context. Offline work can continue, but publication and
  claims about shared coordination require reconnecting and revalidation.
- Start with local/loopback integration tests and a bounded LAN connection
  design. Pairing/access control is required before exposing shared context or
  code; do not ship an unauthenticated network listener. WAN connectivity,
  relay deployment and a shared desktop UI remain separate later slices.

Jev is a possible **optional shadow-mode adviser** for ambiguous impact or
notification timing. Local rules handle exact dependencies, revision checks,
permissions, Git conflicts and test results. A model cannot cancel mandatory
notifications or safety checks. Jev must not block the fast coordination path:
use asynchronous, deduplicated decisions with a conservative local fallback.
Coordination needs its own versioned questions and remote-state allowlist; the
current Jev backend only triages failed verification. No live accuracy,
latency, cost advantage or coordination integration has been demonstrated.
Remote project-context submission requires explicit opt-in.

The narrow coordinator, loopback HTTP/JSON, native-client SSE, revision
consumer and opt-in native-worker safe-point contracts with their focused tests
are implemented below. SSE is selected for this local experiment, not settled
for broader connectivity. The consumer alone persists consumed cursors and
holds notifications; the separate opt-in safe-point helper can bind a
host-provided authoritative snapshot to the next native attempt. Neither layer
grants fresh CAS/publication authority. This does not add LAN pairing, shared
UI, routing or Jev integration.

## Shared product view — owner-side coordination

The AI panel's **Ortak ürün** tab reads an authoritative, detached snapshot for
the configured source root/session/base/version, with revision, context hash,
member configuration and all task states. **Oturum** remains the setup and local
task-preview entry point; **Ortak aday** remains the explicit publication and
candidate workflow. The horizontally scrollable tab bar preserves readable
labels in a narrow sidebar.

- Reads are bounded asynchronous bridge jobs; the fast owner `status()` remains
  memory-only during Git reads or listener drain. The board refreshes on opening,
  explicitly, and at most every five seconds while visible. This temporary
  owner-view polling does not replace the native worker's SSE/replay consumer.
- Context changes require a reviewed goal/decisions/interfaces draft and a
  separate confirmation. Task status changes require their own selected status
  and revision-bound confirmation. Both RPCs use the captured full revision,
  session and credential epoch; only the existing coordinator performs writes.
  The UI acts as the owner. The internal task-status seam also retains the
  coordinator's owner-or-assignee authorization, not a remote identity selector.
- Dirty drafts are never silently rebased by polling. A new revision clears
  confirmation and requires explicit reload/review. Read failure disables writes
  and trustworthy-pass styling until recovery. Duplicate/in-flight requests and
  late root/session/epoch/unmount replies cannot renew consent. A committed write
  returns its truthful receipt even if a later read fails; no automatic retry.
- Waiting tasks and intended scope intersections are deterministic advisory
  information, not agent locks or actual Git conflicts. New tasks are separately
  created through the explicit flow below. Existing tasks cannot be reassigned;
  no native run, approval, apply, publication or verification starts from the board.
- Proposal listing is separately requested. Its output is metadata-only, but
  strict provenance validation **fetches full artifacts**. Pre/post revision and
  identity checks reject a moving session rather than mislabel old receipts.
- The displayed candidate belongs only to the currently retained local delivery
  root/run/channel/session/base. Every verification outcome is shown honestly.
  Pass styling additionally requires a complete unchanged-content fingerprint
  and a matching freshly read context. It is a historical verification record,
  not a global candidate registry or proof that the current disk is unchanged.

Verification for this section (targeted offline scope — **not** a full suite, not
real-user/two-computer evidence):

```bash
python -m pytest -q tests/ -k "collab_product or collab_owner or \
collab_cleanup_contracts or collab_delivery"
```

Coverage: polling draft retention, explicit consent, duplicate requests, delayed
pre-commit reads, late unmounted errors, candidate honesty, failed-read recovery
and 320px overflow, plus browser-mock acceptance of the same behaviours. UI
typecheck/build passes. Browser-mock checks do **not** prove real Git,
networking or provider behaviour; backend tests use temporary real Git/loopback
with declared failure injections.

### Meaningful changes — bounded advisory history

**Anlamlı değişiklikler** adds an explicit read-only history load from the first
visible snapshot anchor. Existing first-parent Git replay supplies at most 16
revision transitions per page. The pure projection names goal/decision changes,
added/removed/changed interface keys and changed task fields/status/assignment;
it never returns goal/decision/interface values or file content. A shared context
change conservatively affects every currently active task because schema 1
declares no interface-to-task dependencies. Task-only changes remain isolated.
An unchanged state at a newer revision is only a metadata checkpoint, not proof
that a proposal was published.

Events cap task records and affected IDs independently at 32, with exact total
counts and explicit truncation flags. The UI retains at most 32 deduplicated
events and reports dropped local records. **Sonraki sayfa** advances only this
display cursor. It is never a native received/consumed acknowledgement, reset
assertion, fresh CAS authority or worker-idle proof. Late identity/unmount replies,
malformed page continuity and moving heads are rejected. Temporary failures keep
the visible evidence and cursor for retry; only a fixed replay-unavailable error
offers an explicit fresh-board anchor reset. That reset does not alter the draft,
confirmation, native cursor or model context. History is not automatically polled.

```bash
python -m pytest -q tests/ -k "collab_product_changes or collab_history or \
collab_coordination or collab_delivery or collab_subscription or collab_transport"
```

Coverage: explicit history load, conservative impact, deduplication, transient
failure vs reset, malformed cursor rejection and draft-preserving history reset,
prototype-named schema IDs treated as own data, and keyboard/ARIA tab navigation.
The tab strip uses a roving focus target, ArrowLeft/ArrowRight with wraparound and
Home/End navigation; one labelled panel stays mounted, and keyboard selection
disables stage auto-switching without automatic focus theft. UI typecheck/build
passes with existing bundle/import advisories. These remain targeted, local,
offline scopes — not a full suite, real-provider or two-computer result.

### New queued tasks in a running owner session

The board's **Yeni görev** action appends work without stopping the listener or
recreating metadata. Enter a new safe ID, an already configured member, goal and
up to 32 literal scopes. Review the displayed shared context and captured full
revision, then separately confirm **Görevi oluştur**. The task always starts
`queued`; scopes remain advisory. This is creation only, not an upsert: existing
IDs, reassignment, task removal, new-member enrollment and caller-supplied status
or context revision are rejected.

`Coordinator.create_task` authenticates the owner before state I/O, validates the
configured assignee, and publishes exactly the authorized CAS state. The new
task's `context_revision` is the reviewed published parent revision, not R0 or a
notification receipt. Both the 256-task and 64 KiB state budgets fail before
publication with typed capacity errors. The async owner bridge accepts no caller
credential/root/path override; immutable source/session/epoch guards remain in
place. A committed receipt stays truthful across project-generation changes and
failed later reads. Source files/HEAD/index/refs/config, credentials and native
cursor files remain untouched.

The creation review discloses the published goal and expandable decisions and
interfaces separately from the context-editing draft. Returned receipts are
cross-checked against the submitted identity, task, assignee and parent revision;
an unconfirmed/mismatched result preserves the draft and explicitly asks for
reconciliation rather than claiming the write failed or retrying it.

The creation draft retains its reviewed revision while polling. A newer revision
disables submission until **Görev taslağını güncel revizyonla yeniden incele**;
that action preserves fields and clears only task-creation confirmation. Every
attempt spends confirmation, including rejection; read recovery cannot retry
automatically. Unchanged polling does not revoke valid confirmation. Duplicate
and late identity/unmount replies are guarded, and successful creation clears
only the creation form, never the context draft or history. A new member/task
preview and native approval remain separate **Oturum** actions. The read-only
history subsequently reports the added task, not an agent start.

Targeted offline scope (see the command below) covering capacity, owner-only
authorization, append-only CAS, source/credential/cursor invariants,
concurrency/drain, truthful receipts, strict bridge validation and separate
preview/approval; browser-mock acceptance adds task draft/consent retention,
explicit stale review, rejection/recovery, double-submit guards,
parent-revision provenance, no implicit run/publication, duplicate-ID refusal,
published-context disclosure and explicit reconciliation after a mismatched
receipt. UI typecheck/build passes with existing import/chunk warnings. No
full-suite, actual model/provider, LAN or two-computer claim is implied.

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_task_creation.py tests/test_collab_task_creation_regressions.py \
  tests/test_collab_coordinator.py tests/test_collab_product_regressions.py \
  tests/test_collab_owner_regressions.py tests/test_collab_product_changes.py
```

### Explicit native participant task commands

`collab_runtime.commands.LoopbackTaskClient` is a separate opt-in Python seam for
an independently authenticated local member. `LoopbackSnapshotClient` remains
read-only. A command changes only an existing task's status, never its owner,
goal or scopes; owner-or-assignee permission is enforced by the coordinator.
Construction and invalid local parameters do no I/O. Each explicit valid call
uses one fresh literal IPv4 loopback connection, the existing finite route and
five-second connect/idle timeouts (not an overall deadline). Success returns a
frozen `TaskStatusReceipt(revision, task_id, status)`.

The caller explicitly reads/reviews a snapshot, then supplies its full revision:

```python
from collab_runtime.client import LoopbackSnapshotClient
from collab_runtime.commands import LoopbackTaskClient, TaskCommandError

# endpoint/member_credential come from explicit trusted local configuration;
# never put the credential into a prompt, preference or log.
reader = LoopbackSnapshotClient(endpoint, credential=member_credential)
commands = LoopbackTaskClient(endpoint, credential=member_credential)
reviewed = reader.snapshot()
try:
    receipt = commands.update_task_status(
        task_id="my-task", status="waiting", expected_revision=reviewed.revision,
    )
except TaskCommandError as error:
    if error.outcome_uncertain:
        # A separate explicit read can reconcile the observed state. Neither
        # this client nor this example retries the non-idempotent command.
        observed = reader.snapshot()
    else:
        # Fixed known rejection; review current state before another decision.
        pass
```

The command never fetches/rebases/retries by itself. Once transmission is
attempted, a lost/malformed/truncated reply may follow a committed write.
`outcome_uncertain=True` requires explicit reconciliation, not an automatic
retry. Valid 400/401/403/409 rejection envelopes are known rejections; **both
500 and 503 are uncertain**, because 503 also represents Git publication
failure and cannot prove the hub did not advance. No peer diagnostic, credential
or credential hash is returned. A task-status receipt is not a worker acceptance,
approval/reset assertion, code-publication authority or evidence a run completed.
No automatic app/run-status transitions or LAN mode are added by this seam.
The explicit participant application integration below is a separate layer.

### Participant own-task status preview and confirmation

An accepted native collaboration run waiting for the user's decision now exposes
**Görev durumum** under the collaboration controls. Its private run-bound member
credential, not the owner credential, issues an explicitly previewed and confirmed
status command. Only `queued`, `running`, and `waiting` are offered; `done` is not
offered because closing the task affects subsequent follow-up/delivery admission.

`collab.taskStatus.preview`, `confirm`, and `discard` accept only a run ID and
target status or opaque ticket. Callers cannot choose another member/task/root,
credential or revision. Preview reads fresh authenticated metadata, verifies the
pinned identity/assignee and source HEAD, and holds at most four in-memory tickets
for 300 seconds. Its context-difference flag compares actual accepted binding,
or reports unknown when none exists; approval is never passed off as acceptance.
Activation, reapproval and close invalidate tickets. Confirmation checks source
HEAD again and spends the ticket before one CAS command, never a hidden retry.

The async operation borrows the existing delivery lease, excluding apply/reject,
follow-up, new runs and disposal until it completes. Preview results crossing a
project-generation boundary are discarded; committed confirmation receipts remain
truthful. Uncertain results use a distinct error and require an explicit new
preview to reconcile. The UI spends consent, discards leftover tickets after
pre-execution rejection and ignores late receipts after run/identity changes.
Neither previews nor receipts alter accepted context, native cursor files or
pipeline state. No model call or automatic reconciliation is performed.

Targeted offline scope: participant core/bridge/regressions plus adjacent
host/run/cleanup/delivery/owner-application tests, with browser-mock acceptance
including isolated participant-component checks driven by an injected bridge
(**not** an actual native run in the browser). These are targeted offline
scopes — not product completion and not progress toward M1–M5 (see
[PRODUCT-PLAN.md](PRODUCT-PLAN.md)). UI typecheck/build passes with existing
import/chunk advisories. Real local backend tests and controlled
mocks/injections remain distinct from live provider, LAN, two-computer or
full-suite validation.

The command/client/transport/coordinator scope below covers: real Git plus real
loopback publishing one metadata child then deliberately dropping the reply —
exactly one command runs, an uncertain error is returned, and a separate
snapshot proves the single commit; another instrumented case publishes then
raises `GitOperationError`, proving why 503 is uncertain; and raw local TCP
peers exercise strict framing/status/code/JSON bounds, timeouts and resource
cleanup. These controlled failure injections are not a real-provider, LAN or
full-suite result.

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_commands.py tests/test_collab_command_regressions.py \
  tests/test_collab_client.py tests/test_collab_client_regressions.py \
  tests/test_collab_transport.py tests/test_collab_coordinator.py
```

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_product.py tests/test_collab_product_bridge.py \
  tests/test_collab_product_regressions.py tests/test_collab_owner.py \
  tests/test_collab_owner_bridge.py tests/test_collab_owner_regressions.py \
  tests/test_collab_owner_application_e2e.py tests/test_collab_coordinator.py \
  tests/test_collab_application_e2e.py tests/test_collab_cleanup_contracts.py \
  tests/test_collab_shared_delivery_e2e.py
```

## Owner/session setup — current setup entry point

The AI panel's **Oturum** tab removes the need to construct a coordinator and
listener manually for the local experiment. This is an explicit **owner-side
loopback setup**, not remote pairing, a new-project code bootstrap or a general
credential-management service. The source must be a flat Git repository with
a committed HEAD; dirty WIP is allowed but never modified by setup.

### Setup and participant handoff

1. Choose **Yeni metadata**. Supply session id, version, shared goal, an owner
   id explicitly included in the member list, and assigned task rows. Task
   assignment does not enroll members or define transport permissions.
2. Choose **Kaynak HEAD önizlemesi al**. Review root/base/goal/member/task data.
   This plan is bounded, expires after 300 monotonic seconds, writes no
   repositories and starts no listener. Editing/switching root invalidates the
   UI draft; an expired/duplicate plan cannot silently create metadata.
3. Choose **Yeni metadata oturumu oluştur** explicitly. The owner service creates
   a new private 0700 namespace under application data, containing a distinct
   local bare hub/store. It initializes goal metadata, then tasks through the
   existing serial CAS chain with task context revisions tied to goal R0.
   Source HEAD/index/refs/config/code and `.imece` binding artifacts are untouched.
   An operational partial failure retains and truthfully reports its owned
   created paths; no overwrite, repair, migration or automatic deletion occurs.
4. Alternatively choose **Mevcut metadata** and explicitly select existing local
   bare store/hub paths outside/above neither source nor each other, with a
   source-base match and declared owner/member configuration. Symlink, URL/SSH,
   nested/occupied-creation and unsafe private-parent paths are rejected. Stop
   succeeds before reconfiguring; partial creation can be recovered explicitly
   without pretending a listener was closed.
5. Choose **Loopback sunucusunu başlat** separately. Literal `127.0.0.1` only;
   default port 0 selects an OS port. No model, task, proposal publication or
   verification starts. A complete distinct credential map is generated in
   memory for configured members; the coordinator owns hashes. Normal status,
   preview/error DTOs, metadata repositories, preferences and prompts contain
   neither raw credentials nor credential hashes.
6. For another **local** participant, select a member and explicitly consent to
   **Erişim bilgisini bir kez ver**. Only this secret API returns the member's
   credential, once per member/start epoch. The UI keeps it in transient local
   component state and a read-only password input, not Zustand/browser storage.
   Clipboard copy is a **second explicit click**; after copy/close/root/epoch/
   stop/unmount the secret state clears. The OS clipboard is outside the app's
   memory boundary and is not automatically erased. The JSON is loopback-only:
   it cannot connect a different computer to this `127.0.0.1` listener.
7. For this app, select a configured member and active assigned task, obtain
   **Görev önizlemesi al**, acknowledge seeing it and choose **Bu uygulamada
   kullan**. Authentication stays inside the host; this handoff returns no raw
   credential. It installs a regular collaboration preview, not approval or a
   run. The Composer still requires its separate explicit context approval.
   Store/hub paths are carried into **Ortak aday** without retyping: same-root
   run changes preserve only channel configuration, not tickets/selection/
   candidate/output/verification authority. A different root clears all paths.

The owner remains bound to its original project across project switches. A
running old-root owner is displayed, not silently retargeted; share/local task
handoff from another root is refused before burning a share. **Durdur** works
globally for that owned runtime. Owner status reads are in-memory and quick
while Git/network/start/stop drain executes asynchronously.

### Stop, retry and credential epochs

Start/stop/configuration are serialized; the currently owned listener is read
only after the operation lock is acquired. Concurrent stop/start cannot close
an old server then discard a newer live server reference. `stop()` uses the
existing drain-safe `LoopbackServer.close()` contract, including subscriptions,
finite workers and core Git calls. A failed drain keeps the server/coordinator/
credentials and `cleanup_failed` for explicit retry; it does not claim stopped
or revoked. Only successful close discards credential state. Restart constructs
a new coordinator/server and fresh epoch: old credentials fail even if the OS
reuses the same port. There is no overall Git deadline or hot remote rotation.
Application shutdown starts at most one owned stop job without constructing an
unused owner runtime; stop failure remains owned/retryable.

### Owner setup verification

The owner application end-to-end cases drive **real owner RPCs** — no
pre-seeded coordinator or listener — through create/start/local HTTP preview,
explicit approval, a native fake-model run, private proposal and candidate.
Candidate verification uses real temporary pytest, not the run's fake per-task
process runner. They verify source invariants, private metadata, R0 task
provenance, one-shot reveal, same-port new-epoch denial, stop retry, root changes,
shutdown and credential exclusion from all *non-secret* replies/events/storage/
model inputs. Intentional `shareOnce` output is explicitly exempt from no-token
reply assertions, not disguised as normal status.

The owner-specific subset can be rerun with:

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_owner_application_e2e.py tests/test_collab_owner_bridge.py \
  tests/test_collab_owner.py tests/test_collab_owner_regressions.py
```

These are targeted scopes, not the full suite. Browser-mock acceptance covers
explicit actions, transient masked share and second-click copy, stale
preview/handoff rejection, old-root listener visibility, config recovery and the
owner→run→Shared Delivery path-preservation regression. UI typecheck/build
passes with existing bundle/import advisories. Browser tests use mocked
owner/network/filesystem data and did **not** open an actual listener; the
backend end-to-end cases provide the real Git/HTTP/SSE proof, and owned test
processes are closed.

No installation, staging/commit/push, migration/deployment or external model/
service call occurred. Real participants/providers, simultaneous computers,
Windows E2E and secure LAN/WAN pairing/TLS remain unverified/later work.

## Application shared delivery — current product loop

The **Ortak aday** AI-panel tab connects the native collaboration beta to the
existing private proposal and candidate engines. It is usable only for an idle,
finished native collaboration execution whose canonical run is still
`WAITING_USER` with retained worktree proposals. Ordinary/legacy runs, disposed
worktrees and another project's run cannot silently become sharing sources.
An owner-started loopback metadata listener and **already-existing local bare
client store/hub** are required. The Owner Session tab above can create/select
them explicitly; shared delivery itself does not initialize hubs, issue
credentials or add code-write HTTP endpoints.

### Explicit capture, publication and candidate flow

1. Start the native run with the explicit context approval described below.
   Select the literal file paths in the local review surface, then open
   **Ortak aday** and supply existing local bare store/hub paths outside both
   the source checkout and isolated worktree. URL/SSH/network Git remotes are
   not accepted. These paths stay in memory, not preferences.
2. Choose **Önizleme al**. Capture reads the **isolated native worktree**, not
   current source WIP, against the session's committed base. At most 64 selected
   paths are accepted; unchanged-against-base selections may be omitted. The
   UI keeps the requested path authorization distinct from the captured subset.
   Selected files may cumulatively contain pre-run WIP; unselected files are
   never uploaded. Embedded secrets in selected code are shared verbatim, not
   redacted. Filename/symlink/UTF-8/file/artifact-size guards remain mandatory.
3. Review the immutable ticket's captured paths, digest, byte/file count,
   proposal id, recorded context and expected session revision. **This preview
   does not publish.** Choose **Bu bileti yayımla** explicitly; out-of-scope
   paths require separate consent because task scopes remain advisory, not
   locks or permissions. Confirmation publishes exactly the captured bytes,
   never files that changed after preview. A strict CAS/private-root + metadata
   child atomic push prevents orphan publication; stale/expired/failing tickets
   are not automatically retried or refreshed to newer authority.
4. Manually refresh **Gelen ekip önerileri**. The output contains validated
   metadata receipts, not file contents; validation still transfers bounded
   artifacts. Select at most 16 proposals, exactly one cumulative proposal per
   task. Stale-context receipts cannot silently be rebased or selected as current.
5. Supply a **new** candidate output directory outside checkout/worktree/store/
   hub and choose **Adayı birleştir**. The engine combines selected proposals
   with committed base objects only; source dirty/untracked state is excluded
   except explicitly published entries. Conflicts return structured paths,
   create no candidate directory and run no verification.
6. Verification is **off by default**. The separate checkbox authorizes trusted
   project code/commands in the new directory; it is not an OS/filesystem
   sandbox. `not_run`, `fail`, `timeout`, `error` and `invalidated` are never shown
   as verified. A verified label requires `verification.status == "pass"`,
   complete nested fingerprint evidence and unchanged candidate content. The
   receipt is point-in-time and does not apply/upload/push the candidate.

### Provenance and lifetime

The first approval snapshot and a stale `.imece/shared-context.json` artifact
are **not** the native code's last accepted context. `PreparedWorkerInput`
retains the strictly validated immutable binding; the native host records an
accepted binding receipt only after real safe-point acknowledgement succeeds.
Preview/approval/event arrival, input preparation alone, failed persistence or
session construction do not earn that receipt. It survives idle deactivation
for delivery but is not current CAS permission.

`capture_proposal(..., binding=SharedSnapshot)` and
`assemble_candidate(..., binding=SharedSnapshot)` are optional typed seams.
Without them the CLI/file-artifact behavior remains unchanged. Supplied values
are strictly reparsed and still require ancestry and **whole-state equality**
against published Git history, matching session/base/task/context and current
CAS. Hash equality alone is insufficient. Synthetic worktree HEADs are allowed
only by the existing base-ancestry check; monorepo subdirectory capture remains
unsupported in v1. No binding artifact is written to bypass provenance checks.

Publication tickets are bounded, expiring, memory-only immutable code artifacts;
their metadata DTOs contain no code/base64 or native credentials. The local
bare-Git channel trusts configured filesystem access; native HTTP credentials
do not grant filesystem/code-publication permissions. A read lease pins the
native session/worktree while async capture/list/assembly/verification runs.
Start/follow-up/apply/reject and cleanup cannot mutate/dispose that resource
mid-read. Shutdown retains it until the job releases the lease, then drains
owned resources. No metadata event causes model calls, publication or verify.

### Shared-delivery verification

The shared-delivery end-to-end cases drive real Qt bridge handlers, real
native host/worktrees, real loopback HTTP/SSE and two distinct source checkouts/
members/stores over one temporary hub. Models and per-task native verification
are fake; **candidate verification uses real temporary pytest subprocesses**.
Frontend-only and backend-only candidates fail; their combined candidate
passes. Coverage: frozen worktree bytes, no source HEAD/index/refs/config/file
mutation, accepted-binding provenance, no orphan ref under stale context,
read-lease writer exclusion/shutdown drain, explicit out-of-scope authorization,
structured conflict/no output and fixed safe errors.

**Scope:** one app host/process simulates two independent members sequentially,
not simultaneous processes, two machines or real providers. This demonstrates
mechanics, not product ROI or autonomous delivery with real models.

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_shared_delivery_e2e.py tests/test_collab_delivery_bridge.py \
  tests/test_collab_delivery.py tests/test_collab_worktree_binding.py \
  tests/test_collab_proposals.py tests/test_collab_candidates.py
```

The command above is the delivery-specific subset; the broader
application/native/host group is `python -m pytest -q tests/ -k collab`. All of
these remain targeted scopes, not the full repository suite.

Browser-mock acceptance (local Vite **mock bridge**) covers: explicit frozen
publication, separate out-of-scope consent, no code/credential publication
params or stored bare paths, selection limits and subset handling, one proposal
per task, busy writer exclusion, nested real-shaped fingerprint display, truthful
non-pass/conflict output and escaped data. **No actual hub or verification
command was called from that browser test.** `npm run typecheck` and
`npm run build` pass; existing large-chunk and static/dynamic-import warnings
remain. Owned test server/browser resources are closed. No commit, installation,
external model/service, migration or deployment occurs.

The delivery UI reduces manual CLI/code movement but does **not** yet make
publication or candidate selection/application autonomous. Secure remote
connectivity, real concurrent participant/provider experiments,
Windows E2E and Jev coordination remain separate later work.

## Application native collaboration beta — context entry point

The application now has an explicit **loopback-only** collaboration path in
the Composer's **Ortak bağlam (deneysel)** section. This builds on, rather than
replaces, the protocol/helper contracts below. A configured owner-side
`LoopbackServer` must be running with locally distributed credentials; the
Owner Session tab above now supplies explicit local setup/start/share. The
native run path does not itself create/start a hub/listener, invite/pair remote
participants, expose LAN/WAN routes or transport code.

### User flow

1. Open an existing Git project whose committed HEAD is the shared session
   base. Use a native API coder provider; an explicit legacy engine or an ACP
   coder is rejected for this opt-in, not silently used as a fallback. Planner
   and reviewer routes may still be independent; **only native Worker context
   participates in live safe-point binding**.
2. Enable **Bu görevde ortak bağlamı kullan**, enter the exact literal loopback
   endpoint, member id, assigned active task id and masked credential, then
   choose **Önizleme al**. The password input clears on submission/project
   change. Preview is an authenticated read, not approval.
3. Review the session id, project root, base commit, target version, metadata
   revision, task owner/goal/scopes and shared context. Choose **Gördüm, bu
   bağlamı onayla** explicitly. Context is rendered as escaped untrusted data;
   task scopes are advisory labels, not file locks or tool permissions.
4. Start the task. The local bridge receives only an opaque approval handle,
   never the credential, in `run.start`. The host revalidates project/session/
   task binding and supplies the stable Worker safe-point proxy. Unsupported,
   stale or failed opted-in admission cannot fall back to an uncoordinated
   legacy run.
5. Observe connection state, pending notifications, received revision and
   consumed revision separately. Receiving an event does not prove accepted
   context or current CAS/publication authority. An execution that leaves a
   proposal closes its subscription; a healthy **follow-up** reactivates the
   same run from its durable consumed cursor without demanding another local
   approval merely because the previous consumer is `closed`/`inactive`.

### Approval, lifecycle and recovery boundaries

`collab_runtime/host.py` owns a bounded, memory-only preview/approval registry.
The local `collab.preview` / `collab.approve` bridge handlers use bounded async
work; `collab.discard` forgets an unbound credential record, and `collab.status`
reads a detached in-memory DTO without fetching snapshots or invoking models.
Project generation/root checks reject late replies after project switches.
Neither credentials nor approval handles are written to preferences, browser
storage, model prompts, canonical run payloads or cursor JSON. Credentials
remain in the trusted host's memory for the owned run; shared context itself is
still explicitly shared data, not a general secret-redaction facility.

Cursor namespaces are hashed from project root + session/base/version + member
and task and are created privately outside the checkout. Existing directory/file
symlinks or unsafe permissions are rejected, not silently repaired. An
exclusive OS-advisory lock prevents concurrent writers to the same checkpoint;
it does not lock task scopes or make collaborators trustworthy. POSIX
cross-process contention is tested; Windows behavior/E2E is not verified.

The native QThread owns `activate()` before pipeline execution and
`deactivate()` in `finally`, including cancellation/failure. Every follow-up
gets a new consumer from the saved consumed cursor, preserving replay/redelivery
semantics. Terminal status is retained for the UI after consumer closure.
Apply/reject guard against a still-running Worker **before** changing source
files or canonical state. Shutdown signals cancellation and retains the
worktree/session/QThread until worker exit rather than disposing in-flight
resources. Blocking model/provider/Git/filesystem calls remain cooperative;
this is not forced thread termination or a hard whole-run deadline.

No terminal gate is automatically reset. For `resnapshot_required`, obtain and
review a new preview and explicitly consent to rebuild the replay cursor at
**that displayed revision**. The consent is spent only at the next idle Worker
input boundary, after canonical context binding/bounds validation and successful
reset persistence. Preview, approval and reapproval alone never advance the
consumed cursor; failed binding/reset retains the cursor and reset intent.
Credential denial can be reapproved without forcing a cursor reset. A changed
task owner/goal/scopes or session/base/version cannot silently retarget an
existing run; it requires an appropriate fresh run/binding. Native metadata
writes and code publication still need their own server-side permission/CAS
checks and explicit operations.

### Completed beta verification

The application end-to-end cases drive the real headless Qt bridge through
preview/approval/start/follow-up/cancel/apply/reject using temporary real
Git/HTTP/SSE/leased checkpoints and fake model/process backends. Coverage:
in-flight arrival vs next-attempt acceptance, no event-driven model calls,
original prompt/plan/pins, durable follow-up replay, task drift, explicit
reset, credential-free replies/events/storage, native-only refusal and
source/workspace shutdown ownership.

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_application_e2e.py tests/test_collab_bridge.py tests/test_collab_run_bridge.py \
  tests/test_collab_host.py tests/test_collab_host_lifecycle.py \
  tests/test_collab_client.py tests/test_collab_client_regressions.py tests/test_collab_runtime.py \
  tests/test_collab_safe_point_wait.py tests/test_collab_safe_point_wait_regressions.py \
  tests/test_collab_factory.py tests/test_collab_binding.py tests/test_collab_safe_point.py \
  tests/test_native_worker_adapter.py tests/test_context_rules.py tests/test_collab_context.py \
  tests/test_fix_models.py tests/test_fix_prompt.py tests/test_planner_prompt.py tests/test_review_prompt.py \
  tests/test_pipeline_runner.py tests/test_fix_loop_runner.py tests/test_fix_loop_decision_gate.py \
  tests/test_pipeline_integration.py tests/test_pipeline_acp_integration.py tests/test_acp_worker.py \
  tests/test_engine_factory.py tests/test_engine_factory_decision_layer.py tests/test_run_pipeline_bridge.py \
  tests/test_bridge.py tests/test_webhost_state_runtime.py tests/test_runtime_paths.py \
  tests/test_run_error_mapping.py tests/test_activity_streamer.py
```

The consumer/subscription/transport/coordinator/history group is a separate
targeted scope; run `python -m pytest -q tests/ -k collab` for the whole offline
collaboration set. These are not the full suite.

Browser acceptance uses existing system Chrome + Playwright against the local
Vite **mock bridge** with all external hosts blocked. Coverage: real UI/store
actions, explicit approval, password clearing and
storage/URL/DOM/console checks, healthy follow-up, late reply/project races,
separate reset consent, recovery actions, escaped hostile context and responsive
layout. It is not a browser-to-Qt live-model test. `npm run typecheck` and
`npm run build` pass. Existing large-chunk/static-dynamic import build warnings
and a reproduced baseline Monaco cancellation message remain; no collaboration
runtime failure is attributed to them. Owned test servers/browsers are closed.

No installation, staging/commit/push, external service/model call, migration or
deployment occurred. Real two-computer/provider, LAN/WAN and Windows E2E remain
unverified. Pairing/TLS, listener/credential issuance UI, remote code proposals,
Jev coordination and measured product benefits remain later work.

### Beta closure: committed decisions and retryable cleanup

A subsequent focused closure audit found and fixed four beta edge cases:

- Once Apply/Reject has committed its filesystem/canonical decision, a later
  collaboration cleanup failure is **not** returned as a false decision failure.
  Apply still returns the real applied paths/checkpoint and emits `fs.changed`;
  the separate collaboration status reports `cleanup_failed`.
- Retained consumer/session/workspace ownership now has an explicit serialized
  drain pass on a later start/shutdown and worker exit. Only absent or actually
  finished workers can be drained. Failed close/dispose keeps references and
  the checkpoint lease; successful retry releases them. The original decision/
  shutdown/cancellation reason is retained rather than rewritten as generic
  `run_finished`. This is not an automatic command/model retry loop.
- Terminal consumer recovery state survives run-level caching and cleanup
  retry. A cleanup error can carry `recoveryState`/`recoveryCode`; completion of
  cleanup cannot silently remove a known resnapshot/auth/protocol gate.
- Cache project matching resolves both roots consistently, including symlink
  aliases. Status-read failure yields a complete `unavailable` DTO, with
  unknown activity and no manufactured cursor progress. The UI marks pending/
  activity as unverified and preserves any known recovery requirement.

Closure verification: the application/native/host command above with
`tests/test_collab_cleanup_contracts.py` added, including a real POSIX lease held
through an injected close failure and freed on retry. The separate
consumer/subscription/transport/coordinator/history control group was unchanged
by this run/state-only closure. Browser-mock acceptance covers the owner→run→
shared-delivery path-preservation regression; UI typecheck and build pass. These
remain targeted, offline results — **not** full-suite, Windows, real-model or
two-computer end-to-end claims.

The native beta's identified implementation/lifecycle/UI closure items are
now closed. Remaining product work is intentionally distinct: easier owner
listener/session setup, the shared task→proposal→combined-candidate delivery
loop in the application, and secure remote connectivity/real-user validation.
Those require the next planning decision, not silent expansion of this beta.

### Native-client revision subscription v1 contract

This narrow slice selects **SSE for the local native-client adapter only**.
**Implemented and locally socket-tested** in `collab_runtime/transport.py`.
It is not a LAN/WAN protocol commitment or a desktop/CLI integration. Native
callers open the subscription explicitly; the consumer below is a separate
module, and no reconnect loop or inbox is automatically started by the application.

- Add `POST /v1/subscribe` with exactly `{after_revision}` as strict JSON.
  Use the same authority, header/body budgets, Content-Type, Content-Length,
  auth-before-body/Git and fixed-error guards as the finite routes. Every
  connection/reconnect requires its own Bearer header. No query credentials,
  browser EventSource/CORS or automatic reconnect client is added. Reject
  `Last-Event-ID` on this route: the explicit JSON cursor is the only cursor.
- Validate the first existing `Coordinator.replay` page before sending 200.
  Invalid cursors get 400; unavailable/expired cursors get the existing 410.
  Success is an HTTP/1.0 close-delimited `text/event-stream` response with
  no-store/nosniff and no Content-Length. An initial `: ready` comment and
  subsequent heartbeat comments carry no revision and never advance a cursor.
- Each transition is `event: revision`, `id: <revision>` and one compact JSON
  `data:` line containing exactly the existing ReplayEvent fields. Process
  ordered pages of at most 32 events; retain at most one page, with no growing
  subscriber queue. Persist an id only after consuming its event. Reconnect
  may redeliver; no exactly-once, delivery acknowledgement or automatic write
  retry is promised. No-op commits still advance the cursor.
- Once streaming, a cursor overtaken by the 64-transition window gets a
  terminal `event: resnapshot_required` with the fixed replay_unavailable
  error object and **no id**, then EOF. Other core failures get a terminal
  `event: error` with the existing fixed error object and no id, then EOF.
  Transport failure/slow-reader timeout may instead yield EOF without a
  terminal event; clients replay their last consumed id or resnapshot on 410.
- Observe Git-backed replay approximately once per second per caught-up
  subscriber, outside network writes/waits; pending pages are read immediately.
  This observes legacy CLI/private proposal metadata commits as well as HTTP
  writes, within the same replay window. It does not observe arbitrary Git
  refs, promise instantaneous delivery, or bound total Git fetch/history cost.
  It polls bounded replay, not full context snapshots, and is a small local
  correctness experiment rather than an optimized shared event dispatcher.
- Replace the single request worker with at most **12 active request workers**
  and at most **8 authenticated subscriptions**. Thus streams alone cannot
  occupy every worker. Subscriber-limit exhaustion is fixed HTTP 503
  `subscriber_limit` before replay/stream headers; worker-limit exhaustion
  closes the new socket without parsing/authentication/metadata. There is no
  unbounded executor queue. Finite requests or malicious local connections can
  still exhaust capacity: this is not availability protection against the host.
- The existing 5-second socket timeout also bounds blocked stream writes.
  All network writes, heartbeat waits and backpressure happen outside the
  coordinator lock. A slow subscriber holds only its bounded replay page and
  can lose its window; it must not prevent commands from acquiring the lock.
- Shutdown stops admission, wakes idle subscriptions and shuts down their
  sockets to interrupt blocked writes. Subscription registration must race
  safely with shutdown. `close()` drains all workers, including any in-flight
  Git/core call and finite command, before successful completion. Concurrent
  closers share the drain; failure requires retry. It is not an overall Git
  execution deadline. Stop-and-replace credential safety remains unchanged;
  close from the accept thread or a request worker must fail rather than deadlock.

Acceptance tests use temporary real loopback sockets and bare Git stores:
auth-before-body/Git (including reconnect), cursor validation and replay paging,
snapshot/subscribe gap, no-op progress and legacy publication, live window
expiry/resnapshot, two competing writes with an open stream, subscriber/worker
admission limits, slow-reader lock independence, disconnect slot reclamation,
idle/blocked-write shutdown, concurrent close plus finite-request drain and
credential replacement. Terminal errors must not echo context, secrets, paths
or diagnostics. No in-flight prompt mutation, agent restart, task impact
routing, LLM call, UI, code transport, LAN/TLS/pairing or Jev is in this slice.

Coverage beyond the list above: real blocked socket writes (synthetic bounded
replay pages for wire pressure), real second-store legacy publications,
concurrent CAS writes with an open stream, release of the initial replay page
before fetching another, advancement between initial replay and stream headers,
both sides of subscription-registration/shutdown races, and stream drain before
credential replacement. This is the narrower subscription/control scope, **not**
the full collaboration group or the full repository suite. No external
service/model, installation, migration, staging or commit occurs.

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_subscription.py tests/test_collab_transport.py \
  tests/test_collab_coordinator.py tests/test_collab_history.py
```

Real two-computer/provider, LAN/WAN and Windows E2E remain unverified. Per-stream
Git polling and the OS socket buffers have no measured latency/throughput or
transfer advantage. Subscriber caps reserve request capacity against streams
alone, not against arbitrary finite requests or malicious local processes.

### Native revision consumer v1 contract

**Implemented and locally socket/checkpoint-tested** as one explicit stdlib
native consumer in `collab_runtime/consumer.py`. The module alone implies no host,
CLI, prompt or UI integration; the separate application beta above supplies its
explicit lifecycle. It only reads revision notifications,
never publishes metadata/code or calls a model.

**Trusted host interface:**

```python
consumer = RevisionConsumer(
    listener.base_url,
    credential=local_token,
    member_id="alice",
    checkpoint_path=private_existing_directory / "revision-cursor.json",
    initial_snapshot=approved_snapshot,
)
consumer.start()  # explicit; construction does not start network activity
consumer.peek()  # immutable pending ReplayEvents, no acknowledgement
consumer.status()  # detached state, consumed revision, pending count, safe code
consumer.acknowledge_at_safe_point(last_handled_revision)
consumer.reset_at_safe_point(approved_fresh_snapshot)
consumer.close()
```

- `initial_snapshot` is an existing `Snapshot` value from
  `collab_runtime.coordinator`, obtained by the trusted host
  from an authoritative authenticated snapshot operation. Validate its schema1
  state/revision and pin session id/base/version. Construction neither fetches
  a snapshot nor applies its context. If no checkpoint exists, the host-approved
  snapshot revision establishes the consumed baseline. If a checkpoint exists,
  its consumed revision wins, **not** the newer supplied snapshot revision;
  session identity and member id must match exactly. A snapshot is data, not
  proof of server authenticity; the host must select the correct session.
  `member_id` is a trusted local checkpoint namespace, not a principal proved
  by the subscription protocol; the host must configure the matching credential.
- The endpoint is only exact `http://127.0.0.1:<port>` (port 1–65535), with no
  userinfo, query, fragment, path, hostname, wildcard, IPv6 or HTTPS variant.
  Use `http.client` without proxy/environment lookup or redirect following.
  Every POST connection carries Bearer authentication and the explicit JSON
  cursor; no credential enters URLs, checkpoints, logs, statuses or errors.
- One explicitly started worker owns the subscription/reconnect loop. Parsed
  revision events enter an inbox of at most **32 entries**. Merely receiving,
  peeking, retrying, heartbeating, closing or resetting a connection must not
  advance the durable consumed cursor. No callbacks, automatic prompt refresh,
  task impact decisions, agent restart or LLM call is performed.
  `status()` returns a detached dict with `state`, `consumed_revision`,
  `received_revision`, `pending_count` and a fixed safe `code`. States are
  `stopped`, `connecting`, `streaming`, `retrying`, `inbox_full`, `access_denied`,
  `resnapshot_required`, `protocol_error`, `server_error` and `closed`.
- Keep separate consumed and volatile received cursors. Reconnect within the
  same instance uses the last accepted inbox tail, preserving pending entries;
  reconstructing after close/process restart uses only the persisted consumed
  cursor, so unacknowledged work can be redelivered. Validate every event's
  revision/id and `previous_revision` chain; no silent gaps/reordering. Exact
  duplicates of the last accepted event may be ignored; inconsistent duplicate
  data is a protocol failure. This is not exactly-once application of context.
- `peek()` returns a detached immutable tuple. `acknowledge_at_safe_point`
  accepts only the consumed cursor (idempotent) or a currently pending revision,
  then acknowledges that ordered prefix. Persist the new cursor atomically
  before removing the prefix/updating the in-memory consumed cursor. Failed
  persistence leaves that cursor and inbox unchanged. The method's name marks
  the host's assertion of a safe point; this module cannot prove the agent is
  idle or apply/rebind its context for it. Acknowledgement is not a fresh CAS
  token or permission to publish without server revalidation.
  Read-only `session_identity` (`(session_id, base_commit, target_version)`) and
  `member_id` properties expose the pinned checkpoint namespace for host-side
  safe-point binding; member id is not proof of subscription authentication.
- On full inbox, close the stream and park until space is acknowledged or an
  explicit reset/close occurs; no extra event queue or snapshot polling. A
  delayed consumer can lose its server window. HTTP 410 or a terminal
  `resnapshot_required` parks in that state and preserves pending entries and
  the consumed cursor, never silently skipping them to a fresh head.
- `reset_at_safe_point(snapshot)` is the only explicit resnapshot acceptance:
  stop/join the current subscription, validate identical pinned session
  identity, persist the host-approved revision, then discard pending events and
  establish the new consumed/received baseline. It does not apply snapshot
  context, and does not automatically restart; the host calls `start()` again.
  Failure preserves the old cursor/inbox and leaves network activity stopped.
  A pre-existing terminal recovery gate also remains blocked on failed reset;
  only a successful reset clears it.
- Network EOF/timeouts/connection failures and HTTP 503/5xx retry with
  interruptible backoff (0.25 seconds initially, capped at 5 seconds). One
  worker and one active socket only, no busy loop or retry queue. HTTP 401/403
  parks as `access_denied` without automatic retry; HTTP 410 parks as
  `resnapshot_required`. Bad status/headers/framing/JSON/events park as
  `protocol_error`; safe server `error` events stop as `server_error`. Do not
  echo server message text, HTTP diagnostics, paths or credentials. Credential
  replacement requires closing the old consumer and constructing another;
  persisted cursors are namespaced by session identity/member, not the token.
  Truncated error bodies at EOF fail as `protocol_error`, not as validated
  denial/resnapshot responses. HTTP 401/403 require `access_denied`, and HTTP
  410 requires `replay_unavailable` in the strict error object; inconsistent
  status/code pairs fail closed. A transport failure while reading a denial
  likewise cannot accept its unvalidated body as a valid recovery response.
- Bound response status+headers to 64 KiB while reading, individual SSE frames
  and lines to 64 KiB + 256 framing bytes, and any error JSON to 64 KiB. Require
  the server's HTTP/1.0 close-delimited `text/event-stream` response, no chunked
  transfer or Content-Length on a successful stream. Parse strict UTF-8 JSON
  using existing guards. Revision frames have exactly one event/id/data line,
  the four existing ReplayEvent fields, strict bool context_changed and sorted
  unique safe task ids (at most 512 for the union of two bounded states).
  Comments have no id/data and cannot advance a cursor. Terminal frames have
  no id and the fixed error-object shape; unknown/ambiguous fields fail closed.
  Partial frames at EOF/timeout are discarded, never acknowledged.
- Socket idle/connect timeout is 5 seconds, not an overall server/Git deadline.
  Do not hold the inbox/lifecycle state lock while waiting for network input or
  backoff. Save the active socket so close can `shutdown` even when HTTP/1.0
  hands socket ownership to its response reader. `close()` interrupts socket,
  full-inbox waits and backoff, joins the worker, is idempotent and serializes
  concurrent closers. No start/reset/ack after completed close. The worker must
  never acquire the lifecycle lock that joins it.
- Checkpoint JSON is bounded to 4096 bytes with strict version 1, pinned session
  id/base/version, member id and consumed_revision only; no context or token.
  The caller chooses an existing trusted local parent directory with one writer
  per checkpoint. Reject symlink/nonregular/oversized/corrupt/mismatched files;
  reads are no-follow where supported and existing files must be private on
  POSIX. Create a private same-directory temporary file (0600), flush/fsync it
  and atomically replace the checkpoint; do not migrate an existing format.
  No interprocess locking or OS/filesystem/power-loss durability is claimed.
  Local same-user edits remain outside this protection.

Acceptance tests: real temporary loopback listener + Git history for arrival vs
ack, readonly peek, ordered prefix ack, restart/redelivery, full inbox/commands,
EOF reconnect, 401/replacement, window expiry/explicit reset and disconnect/close;
scripted local socket peers only for malformed/bounded wire data and partial
frames. Check identity binding, atomic persistence failure/no cursor loss,
checkpoint privacy/symlinks, socket/backoff/full-inbox shutdown, concurrent
closers and ack/reset races. No installation, external model/service or real
Git area mutation is required.

The implementation tightens incomplete and inconsistent HTTP error handling,
preserves terminal recovery gates across a failed reset, and adds deterministic
wire-arrival/persistence and concurrent ack/reset regressions. Two separate
offline regression groups cover this:

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_consumer.py tests/test_collab_subscription.py \
  tests/test_collab_transport.py tests/test_collab_coordinator.py \
  tests/test_collab_history.py
```

Real temporary listeners/Git histories cover normal flow, legacy publications,
reconnect, bounded inbox, credential replacement and cursor-window loss. Scripted
local socket peers cover malformed/bounded wire data, not external services.
Checkpoints are tested in temporary private directories; no existing Git area
was migrated or deployed. This is not the full suite, nor two-computer/provider,
LAN/WAN or Windows E2E validation.
Power-loss durability, multiple checkpoint writers and actual agent safe-point
context application remain outside this module's guarantees.

### Opt-in Python native-worker safe-point slice

`NativeWorkerAttemptAdapter` now accepts an optional host-owned
`NativeWorkerSafePoint`; omitting it preserves the existing attempt behavior.
The host explicitly starts/closes the `RevisionConsumer`, supplies its private
checkpoint configuration, an authenticated authoritative snapshot provider
(for example `lambda: coordinator.snapshot(credential)`), the pinned initial
snapshot/member/task approval. `NativeWorkerSafePoint` now defaults to the
ready-to-use `collab_runtime.binding.bind_worker_input`; an explicit
`bind_input` callback remains available as a trusted compatibility override.
The adapter never starts networking or reads `.imece/shared-context.json` as
authority. Render metadata is captured with production worker requests: the
initial request stores only bounded detached verification-preview text and
ordered pins (never a verification `ProcessRequest`), while fix requests store
their attempt budget, ordered pins and attempt-specific classification. The
default binder reloads project rules at this explicit boundary while replacing
the on-disk snapshot section with the validated immutable `SharedSnapshot`,
then rerenders the full canonical request. Legacy requests without metadata
fail closed under the default binder; explicit callbacks may still handle
them. The runtime verifies request identity/metadata bounds and rejects
stale/missing/duplicate canonical snapshot rendering. The runtime reconstructs
the returned request dataclass to re-run its bounds; if the current canonical
snapshot cannot fit exactly in that prompt's remaining budget, binding fails
closed before acknowledgement.

The request's immutable `render_context` is captured at all three production
sites: the initial pipeline attempt, regular fix-loop attempts and the
verification-less user-feedback follow-up (whose budget is exactly one).
Classification is saved for the same attempt before the loop clears it; it
must not leak to subsequent attempts. Metadata allows at most 256 ordered pins
of 1024 characters each, with no empty/NUL paths. Direct metadata constructors
are strict. Production capture is best-effort and catches only metadata
validation failures: it sets `render_context=None` rather than breaking a
previously valid opt-out prompt. The default opted-in binder then refuses that
request before acknowledgement/model activity instead of inventing omitted
information. Empty classification remains valid and capture uses the fix
renderer's existing 128-character display clipping. Preview facts include
command argv, which was already explicitly displayed; this is not secret
redaction for credentials embedded in those arguments.

At each opt-in attempt boundary, the helper captures one immutable pending
prefix, validates authenticated snapshot identity and task owner/status/goal/
scopes against the host-approved baseline, checks that the worker workspace's
original `source_head` equals the pinned session base (not its synthetic dirty
snapshot commit), and by default requires snapshot revision
equality with the pending tail (or the consumed cursor when there are no
pending events). The default is immediate equality-only behavior. A trusted
host may opt into a bounded (0–5 second) replay-lag wait: snapshots covering a
known captured inbox prefix are accepted by inbox position, while an unknown
revision is treated only as a target to await, never as permission to skip or
reset. Idle waiting uses 50 ms public status/inbox poll intervals (with a shorter
final deadline wait); progress and acceptance gates are rechecked immediately.
It fetches a
fresh authenticated snapshot only after a target arrives; it uses one deadline
and does not create a resumable UI pause. Terminal consumer states still require
explicit host recovery/reset. The synchronous deadline is cooperative: it cannot
preempt a blocking snapshot provider, Git operation, binder, or filesystem
operation; a provider returning late while reconciliation is needed fails
closed. Session construction, binding and acknowledgement/persistence themselves
are not preempted by that replay-wait budget. Healthy initial exact-tail
acceptance remains immediate and is not subject to a new general operation
deadline.
Cancellation interrupts waits and remains a distinct cancellation outcome.
Opt in with `replay_wait_timeout=2.0` when constructing `NativeWorkerSafePoint`;
the default is `0.0`. The setting accepts only finite built-in int/float values
from 0 through 5, not booleans or numeric strings. An unknown target that cannot
fit into an already-full 32-event inbox fails immediately: waiting never
acknowledges or resets merely to make space. All snapshot/session/task/base and
binding checks remain mandatory after replay catches up. A changed consumed
cursor or terminal/stopped/closed state blocks acceptance, rather than being
treated as transient lag. Provider, validation and binding failures are not
automatic retry candidates.
The helper does not start, close, acknowledge, or reset the host-owned consumer
while waiting. The bound request and
`AgentSession` are prepared before the prefix is acknowledged; persistence
failure therefore prevents `session.start()` and leaves consumer cursor/inbox
unchanged. Accepted context is used only for that next attempt; arrivals during
an active attempt remain buffered. Retrying a failed binding is at-least-once,
not rollback of effects performed by trusted host callbacks. Terminal consumer
states require explicit host recovery. Explicit reset validates and binds before
calling the consumer's existing reset contract, retaining that contract's
failed-reset preservation semantics. Reset is still only the host's safe-point
assertion: it does not prove an agent is idle, recover terminal gates
automatically, or provide fresh CAS/publication authority. Use one host/run
writer for safe-point/ack/reset operations; no consumer lock is held through an
in-flight model attempt.

Cancellation is checked around preparation, session construction and
acknowledgement. If cancellation races after the durable acknowledgement
commits, the cursor is not rolled back; the opted-in adapter rechecks cancellation
before `session.start()`, and the existing `AgentSession` guard checks before
model responses. This is a cooperative, nontransactional
boundary, not a claim that acknowledgement and cancellation are atomic.

The helper's `accept_reset(snapshot, request, workspace)` is a separate,
explicit host call, never an automatic fallback from `prepare()`. A successful
reset still stops subscription activity; the host calls `consumer.start()`
explicitly before another opted-in attempt. Like the consumer's reset API, it
relies on host approval of the supplied authoritative snapshot, not lexical SHA
ordering or a claim of monotonic revisions.

`engine_factory.build_pipeline_ports(..., worker_safe_point=safe_point)` now
provides native-worker-only opt-in injection. The factory performs route
validation before constructing ports, rejects a safe point with an ACP worker,
and never starts, observes, acknowledges, resets or closes the host-owned object.
The host retains lifecycle ownership, including when port construction fails:

```python
snapshot_client = LoopbackSnapshotClient(loopback_base_url, credential=member_credential)
candidate_snapshot = snapshot_client.snapshot()  # authenticated read, not approval
# The host locally checks/pins session, base, target version, member and task,
# then explicitly approves this returned snapshot for the safe-point lifecycle.
approved_snapshot = candidate_snapshot  # only after those trusted host checks
consumer = RevisionConsumer(..., initial_snapshot=approved_snapshot)
try:
    consumer.start()
    safe_point = NativeWorkerSafePoint(
        consumer, snapshot_client.snapshot,
        initial_snapshot=approved_snapshot, member_id=approved_member,
        approved_task=approved_task,
    )
    ports = build_pipeline_ports(
        runtime, run_id, routing, worker_safe_point=safe_point,
    )
    # Compose/run the PipelineRunner with these ports.
finally:
    consumer.close()
```

The snapshot client stores only its endpoint/credential configuration and does
no constructor I/O. Each invocation sends one authenticated `POST /v1/snapshot`
with `{}` and closes its fresh literal-IPv4 socket. It never retries, follows
redirects, starts or manages the `RevisionConsumer`, reads replay, acknowledges,
or performs a fresh compare-and-swap. Its five-second connect and idle timeouts
are not a total slow-response deadline; a synchronous request is cooperatively
bounded, not cancellation-registered. Success is a strict schema-1 envelope;
the canonical state is limited to 64 KiB (the response envelope may use the
small additional framing allowance). Errors have fixed local codes and never
expose peer text. Loopback plaintext trusts the host OS and participating
processes: a valid authenticated snapshot response is not TLS/mutual-auth proof
of the server principal, nor publisher/CAS authority. Schema/checksum validity
alone does not approve a session or task. The host must locally select and pin
the intended session, base and target version, and approve the member/task before
using the snapshot; snapshot context remains untrusted data.

Successful responses allow **64 KiB + 256 bytes** for the snapshot envelope,
with a separate **64 KiB canonical state** cap; error bodies remain limited to
64 KiB. Status plus headers are bounded to 64 KiB during parsing. The JSON
parser's default remains 64 KiB; its optional bounded framing allowance does
not relax duplicate-key, UTF-8, nonstandard-constant, surrogate or nesting
guards. Both success-envelope parsing and that optional parser maximum use the
same framing-budget constant.

Client errors carry fixed local codes: `access_denied`, `session_unavailable`,
`server_error`, `protocol_error` and `connection_error`. HTTP 401/403, 503 and
500 require the corresponding strict error envelope/code; server message text
is ignored. Wrong status/code pairs, unexpected status, ambiguous framing and
incomplete bodies fail as protocol errors, never as validated denial. Malformed
HTTP and transport failures after a valid status line are likewise protocol
errors; connection failure/EOF before a valid status remains a connection
error. No error category triggers an automatic retry, context acceptance or
consumer reset. This client does not implement write or replay control routes.

Only the native Worker context participates. Planner/reviewer provenance is
not automatically revalidated, no synchronized whole-agent-team behavior is
claimed, and an in-flight/generated plan is not made authoritative or shared-current
by this integration. There is no automatic fallback when opted-in validation
fails.

These foundational seams are Python-native opt-in wiring. The separate
application beta above now supplies Composer approval, host lifecycle and
private checkpoint ownership. CLI/listener startup, synchronized planner/
reviewer context, broader CAS/code publication, real two-machine/Windows and
real-model E2E remain unimplemented/unverified. The existing rule-discovery
path's project snapshot is untrusted display data, not an authoritative provider.

Two separate offline regression groups cover this:

- The exact consumer/subscription/transport/coordinator/history command recorded
  above.
- The safe-point/adapter/context and adjacent prompt/pipeline/fix-loop group
  below. This includes a real temporary Git hub
  pinned to the worker's actual source baseline, an authenticated loopback SSE
  consumer, and a fake model publishing two context revisions during an active
  attempt. Only the next attempt binds that context and acknowledges the
  captured prefix. Additional cases cover wrong workspace base, stale binding,
  snapshot-ahead rejection, session construction and persistence failures,
  terminal states during binding, captured-tail acknowledgement and external
  cursor changes. The helper reset-failure case uses a consumer double; the
  existing consumer/subscription group covers real consumer reset/checkpoint failures.

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_safe_point.py tests/test_native_worker_adapter.py \
  tests/test_context_rules.py tests/test_collab_context.py \
  tests/test_fix_prompt.py tests/test_planner_prompt.py tests/test_review_prompt.py \
  tests/test_pipeline_runner.py tests/test_fix_loop_runner.py
```

These are targeted groups, not the full suite.
No real model/external service, installation, migration, deployment, staging or
commit was performed. Real two-computer/provider and Windows E2E remain
unverified.

### Canonical binding regression verification

After the default binder and immutable render metadata were added, the offline
group below covers same-context byte equality, original-plan mutation and strict
metadata-constructor regressions.
Tests explicitly cover metadata at all three production request sites,
classification `[None, "code_bug", None]` across three attempts, ordered pins,
detached preview facts without retaining environment/cwd, and replacement of
stale on-disk snapshots. The real Git/loopback/native fake-model safe-point test
now uses the default binder without a host-specific callback. Native and ACP
pipeline compatibility are included.

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_binding.py tests/test_collab_safe_point.py \
  tests/test_native_worker_adapter.py tests/test_context_rules.py \
  tests/test_collab_context.py tests/test_fix_models.py tests/test_fix_prompt.py \
  tests/test_planner_prompt.py tests/test_review_prompt.py \
  tests/test_pipeline_runner.py tests/test_fix_loop_runner.py \
  tests/test_fix_loop_decision_gate.py tests/test_pipeline_integration.py \
  tests/test_pipeline_acp_integration.py tests/test_acp_worker.py \
  tests/test_engine_factory.py
```

This is a different targeted scope from the consumer/subscription/transport group
above, not the full suite. Consumer/transport core behavior was not changed in
this binding slice, so that control group was not rerun here. Startup,
UI/CLI wiring, a resumable UI pause or automatic command-level retry, LAN/WAN, code transport, Jev coordination,
real two-machine/provider and Windows E2E remain out of scope. No real model or
external service, installation, staging/commit, migration or deployment occurs.

### Native factory integration verification

The subsequent Python factory slice added the explicit `worker_safe_point`
argument and native-only preflight guard; the group below covers it alongside
the existing S1b factory behaviour and its decision-layer factory regressions.
No Jev coordination was added.

The real temporary Git/loopback/consumer test runs factory-built native ports
through `PipelineRunner`: initial attempt, failed fake verification, one fix
attempt, passing fake verification, review and the existing `WAITING_USER`
proposal hand-off. Two shared revisions arriving during the first attempt stay
pending through its second model turn. The next attempt binds the latest
context and acknowledges before its first model response. Exactly two fresh
Worker sessions and four fake-model responses are observed, not event-triggered
model calls. Original task, advisory plan, verification preview, ordered pins,
attempt budget and source-checkout isolation are checked. A second real-consumer
test confirms ACP-worker rejection calls neither backend factory nor snapshot
provider and leaves consumer closure to the host's `finally` block. Opt-out
preserves the previous native adapter constructor call shape.

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_factory.py tests/test_collab_binding.py \
  tests/test_collab_safe_point.py tests/test_native_worker_adapter.py \
  tests/test_context_rules.py tests/test_collab_context.py tests/test_fix_models.py \
  tests/test_fix_prompt.py tests/test_planner_prompt.py tests/test_review_prompt.py \
  tests/test_pipeline_runner.py tests/test_fix_loop_runner.py \
  tests/test_fix_loop_decision_gate.py tests/test_pipeline_integration.py \
  tests/test_pipeline_acp_integration.py tests/test_acp_worker.py \
  tests/test_engine_factory.py tests/test_engine_factory_decision_layer.py
```

`test_collab_transport.py` + `test_collab_consumer.py` is a separate core
subset. None of these results
is a full-suite, live-model, two-computer/provider or Windows E2E claim. Consumer
startup/close/reset and credential configuration still require explicit trusted
host ownership; desktop/CLI startup, a resumable UI pause or automatic command-level retry, synchronized
planner/reviewer context, LAN/WAN, code transport and Jev remain outside this
slice. The host must not treat an opted-in factory error as permission to fall
back to an uncoordinated engine. No commit, installation, external service/model,
migration or deployment occurs.

### Bounded replay-wait regression verification

After adding opt-in replay-lag waiting, the deadline/state/cancellation gates
below are hardened with moving-head, binding/ack cancellation and real
post-checkpoint cancellation checks. The consumer, transport and cancellation
primitive themselves were not
changed.

Deterministic clocks verify the original deadline survives successive target
changes and idle waits do not refetch snapshots. Real temporary Git/socket
tests gate replay pages **after** coordinator reads release their lock and
observe actual wait entry before releasing delivery. The first model response
sees the accepted cursor, while checkpoint bytes stay unchanged during lag.
An actual factory-built pipeline reports `CANCELLED`, not `FAILED`, when its
wait is interrupted; a cancellation observed after a real checkpoint commit
preserves that accepted cursor and prevents opening the model session. These
checks do not claim preemption of blocking provider/Git/binder/persistence calls
or atomic cancellation/acknowledgement.

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_safe_point_wait.py tests/test_collab_safe_point_wait_regressions.py \
  tests/test_collab_factory.py tests/test_collab_binding.py tests/test_collab_safe_point.py \
  tests/test_native_worker_adapter.py tests/test_context_rules.py tests/test_collab_context.py \
  tests/test_fix_models.py tests/test_fix_prompt.py tests/test_planner_prompt.py \
  tests/test_review_prompt.py tests/test_pipeline_runner.py tests/test_fix_loop_runner.py \
  tests/test_fix_loop_decision_gate.py tests/test_pipeline_integration.py \
  tests/test_pipeline_acp_integration.py tests/test_acp_worker.py \
  tests/test_engine_factory.py tests/test_engine_factory_decision_layer.py
```

This is not the consumer/subscription control group or a full suite. No real
model/external service, installation, staging/commit, migration or deployment
occurs. Startup/CLI/UI resumable pauses, command retry, automatic terminal
recovery, LAN/WAN, code transfer, Jev coordination and real two-machine/provider
or Windows E2E remain outside this slice.

### Read-only snapshot-client regression verification

After adding the finite client and small JSON framing-budget seam, two separate
offline groups cover it:

- The native/client group below: snapshot wire, strict JSON-budget and existing
  collaboration-runtime cases. Real local socket peers cover malformed and
  bounded responses, full 64 KiB state plus envelope, denial truncation,
  status/code mismatch, authentication on every fresh request, DNS/proxy traps,
  close/replacement credentials and socket cleanup. The real factory/pipeline
  fixture now obtains its initial and worker safe-point snapshots through HTTP
  rather than a direct coordinator callback. Models remain fake.
- The exact consumer/subscription/transport/coordinator/history command above,
  which also asserts received/consumed cursors, an unchanged durable checkpoint
  and a single pending event before/after real duplicate handling. The consumer
  runtime was not changed to make this pass: a genuinely conflicting duplicate
  still fails as `protocol_error`, and the valid `streaming` phase is accepted on
  reconnect.

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_client.py tests/test_collab_client_regressions.py \
  tests/test_collab_runtime.py tests/test_collab_safe_point_wait.py \
  tests/test_collab_safe_point_wait_regressions.py tests/test_collab_factory.py \
  tests/test_collab_binding.py tests/test_collab_safe_point.py \
  tests/test_native_worker_adapter.py tests/test_context_rules.py tests/test_collab_context.py \
  tests/test_fix_models.py tests/test_fix_prompt.py tests/test_planner_prompt.py \
  tests/test_review_prompt.py tests/test_pipeline_runner.py tests/test_fix_loop_runner.py \
  tests/test_fix_loop_decision_gate.py tests/test_pipeline_integration.py \
  tests/test_pipeline_acp_integration.py tests/test_acp_worker.py \
  tests/test_engine_factory.py tests/test_engine_factory_decision_layer.py
```

These are targeted regression groups, not a full suite, performance benchmark,
live-model or real two-machine/provider/Windows E2E result. The host still
chooses, pins and approves the session/task/member and owns consumer lifecycle.
No new endpoint, metadata write/retry, automatic startup, terminal recovery,
UI/CLI, LAN/WAN, code transport or Jev coordination was added. No staging,
commit/push, installation, external service/model, migration or deployment
occurred.

### First coordinator contract: local metadata control core

**Implemented and offline-tested** in `collab_runtime/coordinator.py`. This is a transport-neutral
Python module, not HTTP/SSE, a listener, pairing or a deployable network mode.
It wraps an existing owner-side `GitStore`; schema1, the private proposal
channel and existing CLI commands stay unchanged. Agents and model calls stay
independent. There are no model calls, background workers, automatic prompt
refreshes or code-publication methods in this interface.

Construct `Coordinator(store, *, session_id, owner_id, member_credentials)`
with a trusted owner-local configuration: an expected `session_id`,
the owner's principal id and a mapping of member ids to distinct opaque
credentials. At most 256 principals are accepted, including the owner. Ids
use existing safe-id validation; credentials are 32–256 URL-safe ASCII
characters, intended to be generated with `secrets.token_urlsafe(32)`. Length
validation does not prove entropy. The module copies this configuration and
keeps only credential hashes in memory; it does not persist or log secrets.
There is no anonymous access or request-supplied role/owner override.

Trusted local construction pins the session's id/base/version from the hub.
Every operation authenticates **before Git/state I/O** and checks the pinned
session identity before returning metadata or publishing a change:

| Operation | Permission and result |
|---|---|
| `snapshot(credential)` | Any configured member: detached current revision and complete schema1 metadata snapshot. |
| `update_context(credential, context, *, expected_revision)` | Owner only: replace the shared context using the existing strict current-revision CAS. |
| `update_task_status(credential, *, task_id, status, expected_revision)` | Owner or the task's current assignee only: change one existing task's status; owner, goal, scopes and `context_revision` are preserved. No task creation or reassignment. |
| `replay(credential, *, after_revision, limit=32)` | Any configured member: ordered metadata event page after a durable Git revision cursor. |

Changing task status is **not** acknowledging a newer context. Scopes remain
advisory, not tool permissions or file locks. Commands sharing this module
are serialized locally; concurrent trusted legacy publishers are still
guarded by Git CAS/fast-forward publication. There is no automatic write retry
or exactly-once acknowledgement claim. After an ambiguous transport failure,
reconnect, inspect the authoritative snapshot/replay and revalidate before
deciding whether to issue another command.

Replay is derived from immutable session commits, not an in-memory journal.
Reconstructing the module with the same trusted configuration keeps cursors
usable. Each call pins one fetched head, enumerates at most **65 revisions
(64 transitions)** and reads only the cursor plus up to **32 following
states**, using existing bounded schema1 parsing. Recent merged/noncontiguous
history is rejected. Invalid cursors/limits fail validation; an unknown,
foreign or too-old cursor raises `ReplayUnavailableError`: **fetch a new
snapshot**, never silently skip events. Page limits are strict integers 1–32
(booleans are not integers for this contract).

These limits bound replay enumeration and per-page state reads, not the
existing Git fetch's total history transfer or traversal cost. The window is
not a history-retention policy. No network-transfer or latency advantage has
been measured.

Each event contains only `previous_revision`, `revision`, `context_changed`
and sorted `changed_task_ids` (including removed ids). It contains no context
text, scopes or code. Even a semantically unchanged commit produces an event
so the cursor progresses. Pages return `head_revision`, `next_revision`,
`events` and `has_more`. Clients persist `next_revision` after consuming the
page; delivery is replayable, not exactly once. A concurrent update after the
pinned head remains for the next call. If the moving window overtakes a slow
client, that client must obtain a fresh snapshot. No selective impact routing
or automatic agent restart is implied by an event.

**Access boundary:** this gate protects only the control interface (including
the explicit loopback adapter below). Anyone
with direct access to the trusted hub/store filesystem or the existing CLI
can bypass it. It is not global single-authority enforcement, secure pairing,
TLS, OS isolation or production network authentication. Remote credential
issuance, pairing, live rotation/revocation and protected storage remain
separate work. The loopback adapter adds framing/exposure guards and a local
stop-and-replace lifecycle; it does not make this core safe for LAN/WAN
hosting. The code-proposal channel remains explicit and separate.

Local checks cover authentication-before-I/O, owner/assignee permission
denials, preserved task provenance (including rejection of foreign task
context revisions), malformed directly-constructed context objects,
restart/paging/reset replay, no-op cursor progress, bounded state reads,
session/corrupt-history rejection, same-revision competing writes, external
reassignment during publication and replay while the hub advances.

The relevant collaboration group below is a targeted regression scope, not the
full repository suite or a performance benchmark:

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_coordinator.py tests/test_collab_history.py \
  tests/test_collab_runtime.py tests/test_collab_context.py tests/test_collab_cli.py \
  tests/test_collab_proposals.py tests/test_collab_candidates.py
```

Real two-machine/provider, LAN/WAN and Windows validation remain pending.

### Loopback HTTP/JSON v1 contract

**Implemented and locally HTTP-tested** in `collab_runtime/transport.py`.
Its four original routes remain a finite metadata request/response channel.
The subscription extension is specified separately above; neither channel adds
pairing, LAN/WAN hosting, the shared UI, prompt refresh or code transport.
The HTTP listener uses the Python standard library; no dependency is needed.
Its request lifecycle is explicit: no framework-driven `100 Continue` before
authentication, post-response body draining or process exit on bind failure.
The local Git hub and source checkouts are not migrated or published anywhere.

Trusted local code constructs
`LoopbackServer(coordinator, *, port=0)` from an already configured
`Coordinator`. Construction binds **only literal IPv4 `127.0.0.1`**; there is
no host argument, hostname resolution, wildcard or IPv6 binding. The port is
a strict integer 0–65535 (not bool); 0 selects a fresh ephemeral port. The
module lives in `collab_runtime/transport.py`; it is not imported into the
desktop host, pure-model package exports or CLI. `start()` explicitly starts
serving and returns the server; `base_url` identifies the bound authority.
`close()` stops, drains in-flight requests and closes the listener. It is
idempotent, works before start, and a closed server cannot be restarted.
Context-manager entry/exit start/close the server. Concurrent `close()` callers
all wait for the same drain; completion, not merely initiating shutdown, is
the credential replacement safe point. Bind failure raises a fixed local
`ValidationError`, never `SystemExit` or request diagnostics. The listener now
uses bounded request workers (12 total, at most 8 subscriptions), with a
5-second socket idle timeout, not an overall execution deadline or a process
sandbox. Successful close also interrupts streams and joins all request workers.

The sole authentication carrier is one
`Authorization: Bearer <member credential>` header. Credentials are never
accepted in query strings, JSON, cookies or paths. All query strings are
rejected. The authority must be exactly `127.0.0.1:<bound port>` in a single
`Host` header; `Origin` is rejected even for localhost or `null`. This is a
native-client experiment, not a browser interface. No CORS, proxy-header
trust, redirects, cookies, static files or access/request/error logs are
enabled. Fixed JSON errors must not echo URLs, paths, bodies, credentials,
tracebacks or Git diagnostics, including parser-level HTTP errors.
The HTTP auth scheme is case-insensitive and allows one or more separating
spaces; the credential itself remains case-sensitive. Empty connections or
blank request lines may close without a reply; HEAD responses have no body.

`Coordinator.check_access(credential)` is a small authentication-only seam:
it acquires the same lock, returns no identity or metadata, and does no Git
I/O. The adapter uses it **before reading/parsing a body**; the four domain
operations still authenticate again and enforce their own permissions. All
requests need authentication, including unknown routes and unsupported
methods. Missing, malformed, duplicate or unknown Authorization is HTTP 401;
a configured member denied a domain operation gets 403. Host/origin/query
and framing guards may reject without authentication but never read metadata.

The four finite POST routes below and the separately specified subscription
route are accepted. All bodies are strict
UTF-8 JSON objects, with duplicate keys, nonstandard constants and unknown
fields rejected through the existing bounded parser. `Content-Type` must be
exactly `application/json` (no media-type parameters in v1); one decimal
`Content-Length` is required on every request, including unsupported methods
(use 0 for no body), at most **65536 bytes for the entire JSON envelope**.
Request line plus headers have a separate **65536-byte limit**, enforced
while reading. Transfer-Encoding, `Expect`, duplicate
Content-Length/Host headers and ambiguous framing are rejected; no chunked
body or keep-alive is supported. Only HTTP/1.0 and HTTP/1.1 request versions
are accepted. Finite responses close the connection; streams are close-delimited.
Every response
has `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

| POST route | Exact body | Success JSON |
|---|---|---|
| `/v1/snapshot` | `{}` | Existing snapshot `{revision, state}`. |
| `/v1/context` | `{expected_revision, context}` | `{revision}`; owner permission remains in the core. |
| `/v1/task-status` | `{expected_revision, task_id, status}` | `{revision}`; owner/assignee checks remain in the core. |
| `/v1/replay` | `{after_revision}` plus optional `limit` | Existing page `{head_revision, next_revision, events, has_more}`. |

Errors have the fixed shape `{error: {code, message}}`. A stale write is 409
(`stale_revision`); an unavailable replay cursor is 410
(`replay_unavailable`, fetch a new snapshot); invalid data is 400
(`invalid_request`). Unsupported media is 415, oversized bodies are 413,
authenticated unknown routes/methods are 404/405. 401 carries a fixed Bearer
challenge and 405 carries `Allow: POST`. An unavailable session or
Git operation is 503 (`session_unavailable`); unexpected exceptions are 500
(`internal_error`). Authentication/permission errors use `access_denied`.
No automatic write retry or exactly-once response guarantee is introduced.
After a lost acknowledgement, inspect snapshot/replay and revalidate first.

**Credential lifecycle v1:** the trusted host generates high-entropy tokens
locally (for example `secrets.token_urlsafe(32)`) and supplies the existing
owner/member mapping without printing or putting tokens into CLI arguments.
This slice offers no remote issuance, invite, pairing, persistence, reload,
rotation or revocation endpoint. To rotate/revoke, finish `close()` on the
old listener, discard its coordinator, then construct a new coordinator with
the replacement mapping and a new server. Old tokens fail on the new
listener unless explicitly retained. This is a stop-and-replace lifecycle,
not instant revocation of a response already authorized/in flight. Git
revision cursors survive that replacement; clients reconfigure the endpoint
and credential, then replay or obtain a snapshot if their cursor is too old.
If `close()` fails, it reports a fixed local error and must be retried;
initiated or failed shutdown is not a completed replacement safe point.

The adapter has no automatic start or CLI entry point. Trusted Python host
code uses `with LoopbackServer(configured_coordinator) as listener:` and
native clients send the table's POST requests to `listener.base_url` with
their locally supplied credential. Construction already binds a socket, so
an unused/unstarted listener must also be closed. Do not print credentials,
put them into process arguments or send this plaintext endpoint through an
unreviewed public proxy/tunnel.

Loopback plaintext trusts the host OS and participating native processes.
Binding/access/framing checks are not TLS, secure pairing, protection against
malicious same-user processes or a production-hardened internet server.
Direct Git/filesystem/legacy CLI access still bypasses this interface. Before
any LAN/WAN mode, identity distribution, protected secret storage, transport
security and exposure limits need their own reviewed design. SSE/live revision
delivery is the separate extension above, using bounded Git replay rather than
full-context snapshot polling. No product latency/transfer benefit is claimed.

The transport cases below use real native HTTP/socket connections to temporary
loopback listeners, not a Flask test client or a mocked network. They cover the four
operations, auth-before-application-body-read/parse/Git, fixed errors and
silent failure paths, exact header/body budgets, no `100 Continue` or
keep-alive, actual owner/assignee permissions, same-principal credential
rotation, revocation, restart/replay/resnapshot, failed startup/close recovery,
competing writes and two close callers waiting for the same in-flight drain.
This is a targeted local regression scope, not the full repository suite,
external network/model validation or a latency/throughput benchmark:

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_transport.py tests/test_collab_coordinator.py \
  tests/test_collab_history.py tests/test_collab_runtime.py \
  tests/test_collab_context.py tests/test_collab_cli.py \
  tests/test_collab_proposals.py tests/test_collab_candidates.py
```

Two-computer/provider, LAN/WAN, protected credential distribution and Windows
E2E remain unverified. HTTP/JSON v1 is this local adapter's chosen contract,
not a commitment to a final LAN/WAN protocol. No listener is left running by
the regression tests.

---

## Runnable local demo: one repository, two clients, one combined candidate

Every command below runs from this imece-ide checkout (POSIX shell). The
demo is fully local: two checkouts, two client stores and one hub on a
shared filesystem. No network, no server, no destructive cleanup — every
path is created fresh by `mktemp` or by the commands themselves, and
nothing is ever deleted.

```sh
cd /path/to/imece-ide
PY=".venv/bin/python"          # use this checkout's existing virtual environment
export PATH="$PWD/.venv/bin:$PATH"  # detected checks must also use the venv
DEMO="$(mktemp -d /tmp/imece-demo-XXXX)"
echo "$DEMO"
```

### 1. Committed source baseline (skip if you already have a repo)

One tiny app: the backend carries the response dict, the frontend reads one
key from it, and one test asserts the rendered label.

```sh
git init "$DEMO/src"
git -C "$DEMO/src" config user.name "Dev A"
git -C "$DEMO/src" config user.email "a@example.local"

cat > "$DEMO/src/backend.py" <<'PYEOF'
RESPONSE = {"text": "hello"}
PYEOF
cat > "$DEMO/src/frontend.py" <<'PYEOF'
from backend import RESPONSE

LABEL = RESPONSE["text"]
PYEOF
cat > "$DEMO/src/test_app.py" <<'PYEOF'
from frontend import LABEL


def test_label():
    assert LABEL == "hello"
PYEOF
printf '.imece/\n__pycache__/\n.pytest_cache/\n' > "$DEMO/src/.gitignore"

git -C "$DEMO/src" add -A
git -C "$DEMO/src" commit -q -m baseline
```

This commit is the session baseline; nobody commits again in this demo —
proposal capture reads the DIRTY working tree cumulatively against it.

### 2. A second, independent clone with the same HEAD

```sh
git clone --no-hardlinks "$DEMO/src" "$DEMO/cloneB"
```

### 3. Hub and client stores (all outside every repository)

```sh
$PY -m collab_runtime hub-init --path "$DEMO/hub.git"

$PY -m collab_runtime init \
  --store "$DEMO/storeA.git" \
  --remote "$DEMO/hub.git" \
  --project "$DEMO/src" \
  --session-id demo-1 \
  --target-version v0.1 \
  --goal "Shared response key becomes message"

$PY -m collab_runtime join \
  --store "$DEMO/storeB.git" \
  --remote "$DEMO/hub.git" \
  --project "$DEMO/cloneB"
```

`init`/`join` validate placement against the checkout passed to that command:
the store and hub must live outside it, and `--store` must differ from
`--remote`. The demo places them outside both checkouts. A failed join after
fetching may leave its disposable client store; use a new empty store path
for a retry rather than deleting an existing directory.

### 4. Fresh revision helper (no hand-copied stale tokens)

`--expected-revision` is a compare-and-swap token that must equal the
CURRENT hub head. Read it fresh before every guarded command instead of
pasting stale values:

```sh
revision() {
  $PY -m collab_runtime status --store "$1" --remote "$DEMO/hub.git" \
    | $PY -c 'import json,sys; print(json.load(sys.stdin)["revision"])'
}
```

### 5. Publish shared context, then BOTH tasks (fresh token after each move)

```sh
cat > "$DEMO/context.json" <<'JSONEOF'
{"goal": "Shared response key becomes message", "decisions": [], "interfaces": {}}
JSONEOF

$PY -m collab_runtime context-set \
  --store "$DEMO/storeA.git" \
  --remote "$DEMO/hub.git" \
  --context "$DEMO/context.json" \
  --expected-revision "$(revision "$DEMO/storeA.git")"

$PY -m collab_runtime task-set \
  --store "$DEMO/storeA.git" \
  --remote "$DEMO/hub.git" \
  --task-id t-ui --owner alice \
  --goal "Rename the frontend read" --scope frontend.py \
  --status running \
  --expected-revision "$(revision "$DEMO/storeA.git")"

$PY -m collab_runtime task-set \
  --store "$DEMO/storeB.git" \
  --remote "$DEMO/hub.git" \
  --task-id t-be --owner bob \
  --goal "Rename the response key" --scope backend.py \
  --status running \
  --expected-revision "$(revision "$DEMO/storeB.git")"
```

Scopes are ADVISORY labels — they never lock files; out-of-scope selections
in proposals are reported as warnings, never silently accepted.

### 6. Bind each checkout to its task

`bind` writes the git-ignored `.imece/shared-context.json` artifact — only
if the artifact path is ignored, the checkout HEAD equals the session base,
and the task exists:

```sh
$PY -m collab_runtime bind \
  --store "$DEMO/storeA.git" --remote "$DEMO/hub.git" \
  --project "$DEMO/src" --task-id t-ui

$PY -m collab_runtime bind \
  --store "$DEMO/storeB.git" --remote "$DEMO/hub.git" \
  --project "$DEMO/cloneB" --task-id t-be
```

Agents read this artifact as clearly-labelled UNTRUSTED data (see
`context_runtime.rules.load_project_rules`); the hash is an integrity
checksum, never authentication.

### 7. The two edits (dirty working trees, no commits)

The shared goal renames the response key `text` -> `message`. Each change
ALONE breaks `test_app.py` (a KeyError in one direction or the other);
together they pass:

```sh
# client A (frontend task): read the renamed key
cat > "$DEMO/src/frontend.py" <<'PYEOF'
from backend import RESPONSE

LABEL = RESPONSE["message"]
PYEOF

# client B (backend task): rename the key
cat > "$DEMO/cloneB/backend.py" <<'PYEOF'
RESPONSE = {"message": "hello"}
PYEOF
```

### 8. Publish both proposals (exact paths, fresh CAS token each)

```sh
$PY -m collab_runtime proposal-publish \
  --store "$DEMO/storeA.git" \
  --remote "$DEMO/hub.git" \
  --project "$DEMO/src" \
  --proposal-id prop-ui-1 \
  --task-id t-ui \
  --path frontend.py \
  --expected-revision "$(revision "$DEMO/storeA.git")"

$PY -m collab_runtime proposal-publish \
  --store "$DEMO/storeB.git" \
  --remote "$DEMO/hub.git" \
  --project "$DEMO/cloneB" \
  --proposal-id prop-be-1 \
  --task-id t-be \
  --path backend.py \
  --expected-revision "$(revision "$DEMO/storeB.git")"
```

What this transports — and what it never does:

- ONLY the exact named paths are captured (cumulatively against the session
  base: dirty edits included) and published as private immutable refs
  `refs/heads/imece-proposals/<id>` plus one atomic metadata child commit.
  No source history is copied; no unselected code (dirty, ignored or
  otherwise) is ever uploaded; your index/HEAD/branches/config stay
  untouched.
- The receipt prints revisions and the selected changed PATHS — never file
  contents. Out-of-scope selections are reported as warnings.
- Files must be UTF-8 text without NUL bytes (<=64 files, <=256 KiB each,
  artifact <=2 MiB); credential filenames and `.git`/`.imece` paths are
  always rejected. **No redaction ever modifies code**: embedded secrets
  inside explicitly selected files are shared verbatim — review the
  selection before publishing.
- A stale token fails cleanly before anything is pushed: sync and retry.

### 9. List proposals (metadata receipts only)

```sh
$PY -m collab_runtime proposal-list \
  --store "$DEMO/storeB.git" \
  --remote "$DEMO/hub.git"
```

Prints id, task, owner, session/base/context provenance and file count for
`prop-be-1` and `prop-ui-1`. No file contents are returned (each bounded
artifact is currently fully transferred to validate the receipts).

### 10. Combined candidate: preview, then verified (new dirs, no cleanup)

```sh
$PY -m collab_runtime candidate \
  --store "$DEMO/storeB.git" \
  --remote "$DEMO/hub.git" \
  --project "$DEMO/cloneB" \
  --proposal prop-ui-1 \
  --proposal prop-be-1 \
  --output "$DEMO/candidate-preview" \
  --expected-revision "$(revision "$DEMO/storeB.git")"

$PY -m collab_runtime candidate \
  --store "$DEMO/storeB.git" \
  --remote "$DEMO/hub.git" \
  --project "$DEMO/cloneB" \
  --proposal prop-ui-1 \
  --proposal prop-be-1 \
  --output "$DEMO/candidate-verified" \
  --expected-revision "$(revision "$DEMO/storeB.git")" \
  --verify
```

- The candidate reads the COMMITTED baseline at the session base from git
  objects only, plus the selected proposal artifacts. Dirty/untracked/
  ignored source files are NOT included; dirty state or later commits never
  block assembly from the common base.
- Everything is validated BEFORE the output exists (session token, bound
  snapshot, every proposal's baseline identity, selection rules: distinct
  ids, at most 16, never two cumulative proposals for one task). Conflicts
  (overlapping edits, add/add, delete/modify) print structured conflict
  paths, materialize NOTHING and run no checks (exit 8).
- `$DEMO/candidate-preview` reports `verification: not_run`.
  `$DEMO/candidate-verified` runs the detected project checks INSIDE the
  new directory (`--verify` is explicit authorization, NOT a process or
  filesystem sandbox) and reports honest statuses: `pass` here — and
  `fail`/`error`/`invalidated` when a check fails or persistently mutates
  the candidate. The before/after fingerprints detect PERSISTENT changes
  only (a check that edits a file and restores it before finishing is NOT
  detected); no complete mutation monitoring is claimed. Candidate
  directories are created private (0700). Neither checkout is ever
  written to; the candidate is an isolated materialized output, not a
  linked git worktree, and nothing is uploaded from it.
- The intended loop: proposals that are incompatible alone (each alone
  fails the checks) pass together once combined. On conflict, the affected
  developer re-works and publishes a fresh proposal; nothing on either side
  was overwritten.

For scripts, candidate conflicts return exit code **8**. Verification
`fail`, `timeout`, `error` or `invalidated` returns **9** and preserves the
generated candidate for inspection. A successful assembly with `not_run`
returns **0**, but is not a verification pass.
