"""Read-only security/resource regression repros for the agent run path.

Two independent, fully local (no external API) concerns:

1. A secret-bearing exception raised by the real native model backend must
   not be persisted anywhere a Run keeps state: not in the canonical
   ModelFailed/ExecutionFailed event payloads and not in the on-disk store,
   and not in anything the UI receives (run.event / run.finished).
2. Repeated ``_workspace_inventory`` calls over a nested tree must not grow
   this process' owned file-descriptor count, and a FIFO in the tree must
   never block the inventory.

Both use the same ScriptedBackend shape as tests/test_agent_application_e2e
(only local fakes; no network, no provider key is read).
"""
import gc
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_execution_runtime.execution import _workspace_inventory  # noqa: E402
from executor_runtime.errors import ExecutorAdapterExecutionError  # noqa: E402
from executor_runtime.native_worker import NativeWorkerAttemptAdapter  # noqa: E402
from fix_runtime.models import InitialWorkerRequest  # noqa: E402
from run_runtime.events import RunEventType  # noqa: E402
from run_runtime.service import RunRuntime  # noqa: E402
from run_runtime.store import RunStore  # noqa: E402
from webhost import state  # noqa: E402
from workspace.worktree import GitWorktreeWorkspace  # noqa: E402

# Reuse the real-host bridge helpers/fixtures of the agent e2e suite.
from test_agent_application_e2e import (  # noqa: E402
    ScriptedBackend,
    _pump_until,
    bridge,  # noqa: F401  (fixture)
    isolated_state,  # noqa: F401  (autouse fixture)
    qapp,  # noqa: F401  (fixture)
)
from test_run_pipeline_bridge import git_repo, rpc  # noqa: F401,E402

import engine_factory  # noqa: E402

# A credential-shaped token: unique, greppable, never sent anywhere.
SECRET = "sk-regression-c0ffee-SECRET-4f21ba7e9d"


class SecretBearingBackend(ScriptedBackend):
    """tests/test_agent_application_e2e.ScriptedBackend, but every respond()
    raises an exception whose message carries a provider credential.

    Every model call fails, so no completion/tool turn can ever be produced.
    """

    def __init__(self, secret: str = SECRET) -> None:
        super().__init__()
        self.secret = secret
        self.responds = 0
        backend = self

        class Session:
            def respond(self, _input_items):
                backend.responds += 1
                raise RuntimeError(
                    "401 invalid_api_key: api_key=%s was rejected by the provider endpoint"
                    % backend.secret
                )

        self._session = Session()

    def open_session(self, **_kwargs):
        self.opens += 1
        return self._session


def _running_runtime(tmp_path, name="runs.sqlite3"):
    runtime = RunRuntime(RunStore(tmp_path / name))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run.run_id


def _worktree(git_repo, tmp_path, run_id):
    return GitWorktreeWorkspace.create(
        source_root=git_repo, run_id=run_id, base_dir=tmp_path / "worktrees"
    )


def test_native_backend_secret_stays_out_of_canonical_events_and_store(
    tmp_path, git_repo
):
    """Real NativeWorkerAttemptAdapter + failing backend: no secret on disk.

    The outer execute_task failure is generic ("Agent execution failed."); the
    canonical per-execution failure receipts are the ones that must also stay
    free of the raw backend exception text.
    """
    runtime, run_id = _running_runtime(tmp_path)
    workspace = _worktree(git_repo, tmp_path, "secret-adapter")
    backend = SecretBearingBackend()
    adapter = NativeWorkerAttemptAdapter(runtime, run_id, backend)

    with pytest.raises(ExecutorAdapterExecutionError):
        adapter.run(
            workspace,
            InitialWorkerRequest(
                task="create native.txt",
                rendered_input="ORIGINAL USER TASK: create native.txt",
            ),
            execution_id="exec_secret",
        )

    # The failure was reached through the backend, with no model turn produced.
    assert backend.opens == 1
    assert backend.responds == 1

    events = runtime.events(run_id, limit=500).events
    types = [event.type for event in events]
    assert RunEventType.MODEL_FAILED in types, types
    assert RunEventType.EXECUTION_FAILED in types, types
    assert RunEventType.MODEL_COMPLETED not in types
    assert RunEventType.EXECUTION_COMPLETED not in types

    failed = [
        event
        for event in events
        if event.type
        in (RunEventType.MODEL_FAILED, RunEventType.EXECUTION_FAILED, RunEventType.TOOL_FAILED)
    ]
    for event in failed:
        assert SECRET not in json.dumps(event.payload), (
            f"{event.type} payload persisted the backend credential"
        )
        assert event.payload.get("error_type")

    # Nothing secret reached the on-disk canonical store either.
    store_bytes = (tmp_path / "runs.sqlite3").read_bytes()
    assert SECRET.encode() not in store_bytes


def test_run_finished_error_from_failing_native_backend_carries_no_secret(
    monkeypatch, bridge, qapp, git_repo  # noqa: F811
):
    """Full host path: failing native backend -> UI run.event + run.finished."""
    backend = SecretBearingBackend()
    monkeypatch.setattr(engine_factory, "_default_backend_factory", lambda _pid: backend)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))

    reply = rpc(bridge, "run.start", {"task": "create native.txt", "providerId": "openai"})
    run_id = reply["result"]["runId"]
    assert _pump_until(qapp, lambda: any(e.get("channel") == "run.finished" for e in events))

    finished = next(e for e in events if e.get("channel") == "run.finished")
    assert finished["payload"]["status"] == "failed"
    assert SECRET not in json.dumps(finished["payload"])
    assert backend.opens == 1
    assert backend.responds == 1

    # No UI event of any channel may carry the credential.
    assert SECRET not in json.dumps(events), [
        e["channel"] for e in events if SECRET in json.dumps(e)
    ]

    canonical = state.get_run_runtime().events(run_id).events
    assert any(event.type == RunEventType.MODEL_FAILED for event in canonical)
    assert SECRET not in json.dumps([event.payload for event in canonical])
    # The outer execute_task failure is generic on purpose; keep it that way.
    run_failed = [e for e in canonical if e.type == RunEventType.RUN_FAILED]
    assert run_failed, "the failing worker attempt must settle the Run as failed"
    assert run_failed[-1].payload["error_message"] == "Agent execution failed."


_FD_PROC = "/proc/self/fd"


def _open_fd_count():
    return len(os.listdir(_FD_PROC))


@pytest.mark.skipif(
    not os.path.isdir(_FD_PROC)
    or os.scandir not in os.supports_fd
    or os.open not in os.supports_dir_fd,
    reason="dir_fd/scandir-fd inventory accounting needs a Linux /proc/self/fd",
)
def test_repeated_workspace_inventory_keeps_owned_fd_count_stable(tmp_path):
    """10 inventories of a 3-level tree (plus a FIFO) must not grow the FDs."""
    root = tmp_path / "tree"
    for rel in ("a", "a/b", "a/b/c"):
        (root / rel).mkdir(parents=True)
        (root / rel / "f.txt").write_text("x", encoding="utf-8")
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unsupported")
    os.mkfifo(root / "pipe")
    workspace = SimpleNamespace(root=root)

    paths, complete = _workspace_inventory(workspace)
    assert set(paths) == {"a/f.txt", "a/b/f.txt", "a/b/c/f.txt"}
    assert complete is False, "a FIFO is not inventoryable and must be reported"

    directories = 4  # root, a, a/b, a/b/c
    gc.collect()
    before = _open_fd_count()
    started = time.monotonic()
    for _ in range(10):
        last_paths, last_complete = _workspace_inventory(workspace)
    elapsed = time.monotonic() - started
    after = _open_fd_count()

    assert set(last_paths) == {"a/f.txt", "a/b/f.txt", "a/b/c/f.txt"}
    assert last_complete is False
    # A FIFO must never block the walk (special files are never opened).
    assert elapsed < 5.0, f"inventory blocked on a FIFO for {elapsed:.2f}s"
    leaked = after - before
    assert leaked <= 0, (
        f"owned fd count grew by {leaked} over 10 inventories of {directories} "
        f"directories ({before} -> {after})"
    )
