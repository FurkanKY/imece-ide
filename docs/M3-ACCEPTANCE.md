# M3 engineering delivery and remaining acceptance

The `m3-task-history` development branch implements the bounded flows below.
This is **not** a claim that M3's native supported-platform acceptance is closed.
The authoritative milestone contract is [PRODUCT-PLAN.md](PRODUCT-PLAN.md).

## Implemented behavior

- Historical native tasks remain read-only; no old receipt creates apply,
  checkpoint, cancellation or continuation authority.
- Explicit retry keeps TaskID, creates a new RunID/monotonic attempt, reads the
  original full prompt/provider from SQLite and creates a fresh source snapshot.
  The current source's Git-visible dirty state is intentional; an old worktree,
  credential, mention list or proposal is never copied into retry authority.
- New native Git workspaces have canonical descriptors in the existing
  `workspace_snapshot_json` projection, derived from append-only events.
  A process-exclusive kernel lease is held while the workspace is owned.
  There is no new persistence layer or database migration.
- On Linux, each `ProcessRunner` command runs under a dedicated Python
  subreaper supervisor. It enables `PR_SET_CHILD_SUBREAPER` before launch,
  keeps stdout/stderr as ordinary bounded command output, and reports a private
  nonce-bound receipt only after the root exited and `waitpid(-1)` reached
  `ECHILD`. Detached/setsid descendants are adopted and reaped by that
  supervisor. Timeout/cancel asks the supervisor to kill/reap its children;
  supervisor startup/failure or malformed/forged receipt yields no authority.
  It also inherits the workspace lease descriptor so host loss alone does not
  release ownership while its supervisor is still alive.
- Native `run_process` and deterministic project-check results include this
  receipt in canonical completion-event metadata. On cancellation, the runner
  authenticates the receipt before raising cancellation (never returns success);
  canonical `tool.failed` or `verification.check_interrupted` records retain only
  the positive fact and bind it to call/check plus execution identity. Sealing
  requires a matching positive receipt for every started process/check,
  including an interrupted check; missing/mismatched/false proof remains busy.
  Cancelled verification is never converted into PASS. Process-free native SDK/file-tool histories are
  sealable without such receipts. Linux ACP stdio producers use the same
  supervisor around a byte-preserving bidirectional pipe proxy; their canonical
  completed/failed event carries a positive receipt only after authenticated
  supervisor exit and `ECHILD`. The workspace lease fd is inherited by the
  supervisor. Missing receipts remain fail-closed. Non-Linux process runners
  do not issue the receipt and fail closed for process-backed continuation.
- **Çalışma alanından devam et** explicitly claims a previously sealed workspace.
  The OS lease, exact project/task/provider, detached registered Git worktree,
  original baseline, unchanged Source and complete content fingerprint are
  checked again. Another live owner, wrong project, missing/tampered/symlinked
  workspace or stale source fails closed. The same RunID/TaskID receives a new
  execution; model conversation/credentials/old proposal authority are not
  restored. A new deterministic verification/evidence path must run before new
  proposals are presented. Process-local admission remains bounded to two owned
  native works per project; dormant read-only history is not automatic ownership.
- **Sonuçları birleştir** explicitly selects one or two owned, review-ready native
  results. Currently they must share a clean, top-level committed Git baseline;
  dirty-snapshot/subproject/binary changed-file cases are refused, not guessed.
  Unchanged baseline binary files may remain in the candidate. Existing bounded
  merge/materialization/fingerprint/verification machinery is reused without
  enabling collaboration transport. The output is outside Source, private and
  newly created. Overlapping conflicts do not create or verify a candidate.
- **Birleştir ve doğrula** explicitly authorizes running the candidate's project
  checks. `not_run`, failure, incomplete fingerprint or changed content never
  permits apply. Successful assembly alone is not verification.
- **Doğrulanmış adayı uygula** and **Birleşik adayı geri al** require separate
  user confirmations. Verified candidate bytes/modes, source baseline/hashes,
  project provenance and candidate lease are rechecked. A mode-aware checkpoint
  precedes Source writes; canonical failure triggers compensation. Rollback
  refuses post-apply edits or tampered checkpoint provenance/content. Receipts
  survive SQLite reopening; old browser/process-local tickets are not authority.
  The original result records remain independent; candidate integration does
  not silently mark their original proposals accepted or destroy their worktrees.

## Reproduce local checks

From the repository root, in the configured development environment:

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_run_store.py tests/test_run_retry_bridge.py \
  tests/test_run_history.py tests/test_run_registry.py \
  tests/test_agent_application_e2e.py tests/test_agent_bridge_regressions.py \
  tests/test_workspace_ownership.py tests/test_run_restart.py \
  tests/test_combined_candidate.py tests/test_checkpoints.py \
  tests/test_process_runtime.py tests/test_process_tool.py \
  tests/test_agent_session.py tests/test_native_verification_adapter.py \
  tests/test_verification_runtime.py tests/test_tool_dispatcher.py \
  tests/test_run_pipeline_bridge.py tests/test_window_close_lifecycle.py \
  tests/test_windows_safety.py

ACP-specific regression command:

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_acp_worker.py tests/test_acp_fake_agent_integration.py \
  tests/test_acp_client.py tests/test_native_verification_adapter.py
```

From `web/ui`:

```sh
npm run typecheck
npm run build
node scripts/run-state-acceptance.mjs
node scripts/mock-runs-acceptance.mjs
node scripts/task-workspace-acceptance.mjs
node scripts/run-history-acceptance.mjs
node scripts/run-retry-acceptance.mjs
node scripts/m3-acceptance.mjs
```

These use disposable real Git/SQLite/Qt fixtures, process-exclusive lease tests,
scripted native tools/real local verification commands and controlled browser
RPCs. `test_window_close_lifecycle.py` launches an isolated QApplication + real
`ShellWindow`, starts a native worker, calls `close()` through the actual
`closeEvent`, proves cancellation/drain + retained workspace, reopens the same
SQLite store/runtime, and explicitly continues the same TaskID/RunID. Candidate
selection/verification/apply/rollback is separately exercised through real host
RPC handlers in `test_run_restart.py` and browser controls in `m3-acceptance.mjs`. No real provider call, user database migration, automatic merge, remote
listener or release is involved.

Latest full focused validation: **510 passed, 7 platform-gated skips**. Portable
Git porcelain path parsing, Windows walker contracts, Job launch ordering and
bounded receipt tests ran here. Platform-gated tests include native Windows
Job Object process/cancellation, ACP Job containment, junction/reparse handling,
and seal/stash/adopt against real Win32 handles. The existing eight-shard Windows
CI job runs these tests, but it has not run in this session. UI typecheck, production build and all six
browser acceptance scripts passed after the backend changes.
Build retains non-fatal chunk-size/mixed-import warnings. These fixtures are not
a full-suite, actual-provider or supported native-desktop acceptance result.

Windows restore parses NUL-delimited `git worktree list --porcelain -z` records
and compares complete normalized paths (spaces/Unicode safe), rather than
substring-matching a newline-oriented listing. Before Git inspects worktree
identity, `.git` metadata is held open as a no-reparse regular file on Windows;
a junction/reparse substitution fails closed.

## Remaining gates and honest limits

- A Win32 handle-anchored no-reparse walker now supports bounded workspace and
  candidate fingerprints: it opens every path component with
  `FILE_FLAG_OPEN_REPARSE_POINT`, denies write/delete sharing while inspected,
  rejects reparse points, and hashes from held file handles. Portable contract
  tests and a native Windows junction test are wired into the existing Windows
  CI suite. A Windows `ProcessRunner` supervisor also creates targets suspended,
  assigns them to a non-breakaway kill-on-close Job Object before resume, and
  issues a nonce-bound receipt only after the root exits and the kernel reports
  zero active Job members. Cancellation/timeout terminates the Job and drains it.
  Windows ACP now receives its configuration over a dedicated inherited
  anonymous pipe, uses the same suspended-assignment Job supervisor, and passes
  stdio handles directly through to the agent; the supervisor closes its own
  copies to preserve protocol EOF. Because asyncio creates stdio handles inside
  `Popen`, Windows ACP uses the dedicated inheritable config handle plus only
  the redirected standard handles (`close_fds=False`, relying on PEP 446 for
  unrelated descriptors); the supervisor consumes config before spawning its
  Job-contained target. A Windows-only fake-agent test now exercises
  detached-child cleanup and the receipt. These native Win32 code paths have
  not executed in this Linux session; Windows CI/desktop acceptance is still
  required.
- Linux process-tool, verification, and ACP stdio orderly continuation now has
  subreaper/waitpid evidence. Shutdown kills and reaps direct/adopted descendants;
  it does not signal a stale process-group ID after the root has exited. This is
  process-tree quiescence at the supervisor boundary, not a sandbox or resistance
  to a privileged/malicious same-user process that deliberately attacks the
  supervisor. Configuration-pipe failure reaps the already-started supervisor;
  receipt reads occur only after its process exit, and target children cannot
  inherit the private receipt FD. Host `SIGKILL` leaves the independent supervisor
  alive holding the lease; it may eventually release it after reaping descendants. Abrupt supervisor
  death itself is not certified and must not be treated as a positive receipt.
  Windows Job Object `ProcessRunner` and ACP supervision implementations plus
  native integration regressions are in place but have not run in this Linux
  session. Windows msvcrt lease locks are not assumed to be inherited: an
  unsealed/busy descriptor remains non-adoptable after abrupt host loss. Other
  non-Linux process-backed histories remain fail-closed.
- The app's closeEvent → persisted host reopen → explicit continuation path now
  has a real offscreen QtWebEngine/ShellWindow fixture, and candidate apply/
  rollback has native host-handler coverage. Actual-provider and supported
  platform user-flow acceptance still needs explicit acceptance; scripted
  subprocesses and offscreen Qt are not that gate. M1's actual-provider/platform
  validation debt remains the separately tracked pre-M5 release gate.
- Fingerprints detect persistent content changes, not transient edits undone
  between snapshots. Source integration is not an OS/filesystem sandbox or a
  global transaction against unrelated external editors. Project checks and
  generated code execute with the user's OS privileges.
- Automatic pruning is disabled; retained workspaces/candidates consume disk.
  Do not delete a possibly live/unsealed worktree based solely on its age or
  absence from a process-local registry.
