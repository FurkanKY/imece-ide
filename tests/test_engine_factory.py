"""engine_factory — motor seçimi + pipeline port fabrikası birim testleri.

Qt'siz, saf pytest testleri. Gerçek ağ/süreç çağrısı yapılmaz: backend/ACP
istemci fabrikaları enjekte edilerek sahte (Scripted) nesnelerle değiştirilir.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine_factory  # noqa: E402
from executor_runtime import AcpReviewAttemptRunner, AcpWorkerAttemptAdapter, NativeReviewAttemptAdapter, NativeVerificationAttemptAdapter, NativeWorkerAttemptAdapter  # noqa: E402
from pipeline_runtime.acp_planner import AcpPlanAttemptRunner  # noqa: E402
from pipeline_runtime.native_planner import NativePlanAttemptRunner  # noqa: E402
from run_runtime import RunRuntime, RunStore  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git yok")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


def _all_native_routing():
    return {"planner": "gemini", "coder": "deepseek", "reviewer": "openai"}


# ---------------- select_engine ----------------

def test_select_engine_non_git_falls_back_to_legacy(tmp_path):
    selection = engine_factory.select_engine(tmp_path, _all_native_routing())
    assert selection.engine == "legacy"
    assert "Git deposu" in selection.reason


def test_select_engine_git_repo_all_native_uses_pipeline(git_repo):
    selection = engine_factory.select_engine(git_repo, _all_native_routing())
    assert selection.engine == "pipeline"
    assert selection.reason is None


def test_select_engine_claude_and_codex_cli_supported(git_repo):
    routing = {"planner": "claude", "coder": "codex-cli", "reviewer": "claude"}
    selection = engine_factory.select_engine(git_repo, routing)
    assert selection.engine == "pipeline"


def test_select_engine_unsupported_cli_role_falls_back_to_legacy(git_repo):
    routing = {**_all_native_routing(), "coder": "qwen-code"}
    selection = engine_factory.select_engine(git_repo, routing)
    assert selection.engine == "legacy"
    assert "desteklenmiyor" in selection.reason


def test_select_engine_gemini_cli_supported(git_repo):
    routing = {**_all_native_routing(), "reviewer": "gemini-cli"}
    selection = engine_factory.select_engine(git_repo, routing)
    assert selection.engine == "pipeline"
    assert selection.reason is None


def test_select_engine_anthropic_api_supported(git_repo):
    routing = {**_all_native_routing(), "planner": "anthropic"}
    selection = engine_factory.select_engine(git_repo, routing)
    assert selection.engine == "pipeline"
    assert selection.reason is None


def test_select_engine_explicit_legacy_pref_always_legacy(git_repo):
    selection = engine_factory.select_engine(
        git_repo, _all_native_routing(), ai_engine_pref="legacy",
    )
    assert selection.engine == "legacy"
    assert selection.reason is None


def test_role_supported_unknown_provider():
    ok, why = engine_factory.role_supported("no-such-provider")
    assert ok is False
    assert "Bilinmeyen" in why


# ---------------- build_pipeline_ports ----------------

class _FakeBackend:
    pass


class _FakeAcpClient:
    async def run(self, *a, **k):  # pragma: no cover - never actually called
        raise AssertionError("gerçek ACP çağrısı yapılmamalı")


def _runtime_with_run(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="görev")
    run = runtime.create_run(task_id=task.task_id)
    from run_runtime import RunEventType
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run.run_id


def test_build_pipeline_ports_native_for_openai_roles(tmp_path):
    runtime, run_id = _runtime_with_run(tmp_path)
    seen_providers = []

    def backend_factory(provider_id):
        seen_providers.append(provider_id)
        return _FakeBackend()

    ports = engine_factory.build_pipeline_ports(
        runtime, run_id, _all_native_routing(), backend_factory=backend_factory,
    )
    assert isinstance(ports.planner, NativePlanAttemptRunner)
    assert isinstance(ports.worker, NativeWorkerAttemptAdapter)
    assert isinstance(ports.reviewer, NativeReviewAttemptAdapter)
    assert isinstance(ports.verification, NativeVerificationAttemptAdapter)
    assert seen_providers == ["gemini", "deepseek", "openai"]
    assert ports.worker._safe_point is None


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_build_pipeline_ports_injects_safe_point_only_into_native_worker(tmp_path, provider):
    runtime, run_id = _runtime_with_run(tmp_path)
    safe_point = object()
    routing = {"planner": "gemini", "coder": provider, "reviewer": "openai"}
    ports = engine_factory.build_pipeline_ports(
        runtime, run_id, routing, backend_factory=lambda _pid: _FakeBackend(),
        worker_safe_point=safe_point,
    )
    assert isinstance(ports.worker, NativeWorkerAttemptAdapter)
    assert ports.worker._safe_point is safe_point
    assert isinstance(ports.planner, NativePlanAttemptRunner)
    assert isinstance(ports.reviewer, NativeReviewAttemptAdapter)


def test_native_worker_safe_point_allows_acp_planner_and_reviewer(tmp_path):
    runtime, run_id = _runtime_with_run(tmp_path)
    safe_point = object()
    ports = engine_factory.build_pipeline_ports(
        runtime, run_id, {"planner": "claude", "coder": "openai", "reviewer": "codex-cli"},
        backend_factory=lambda _pid: _FakeBackend(), acp_client_factory=_FakeAcpClient,
        worker_safe_point=safe_point,
    )
    assert isinstance(ports.planner, AcpPlanAttemptRunner)
    assert isinstance(ports.worker, NativeWorkerAttemptAdapter)
    assert ports.worker._safe_point is safe_point
    assert isinstance(ports.reviewer, AcpReviewAttemptRunner)


@pytest.mark.parametrize("worker", ["claude", "codex-cli", "gemini-cli"])
@pytest.mark.parametrize("planner", ["openai", "claude"])
def test_safe_point_rejects_acp_worker_before_any_factory(tmp_path, worker, planner):
    runtime, run_id = _runtime_with_run(tmp_path)
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("factory must not run")

    routing = {"planner": planner, "coder": worker, "reviewer": "openai"}
    with pytest.raises(engine_factory.EngineUnsupportedError,
                       match="Collaboration safe points require a native worker provider"):
        engine_factory.build_pipeline_ports(
            runtime, run_id, routing, backend_factory=forbidden,
            acp_client_factory=forbidden, worker_safe_point=object(),
        )
    assert calls == []


def test_safe_point_factory_never_owns_or_reads_consumer(tmp_path):
    runtime, run_id = _runtime_with_run(tmp_path)

    class Spy:
        def __getattr__(self, name):
            if name in {"start", "peek", "status", "reset", "acknowledge_at_safe_point", "close"}:
                raise AssertionError(f"factory accessed lifecycle method {name}")
            raise AttributeError(name)

    safe_point = Spy()
    ports = engine_factory.build_pipeline_ports(
        runtime, run_id, _all_native_routing(), backend_factory=lambda _pid: _FakeBackend(),
        worker_safe_point=safe_point,
    )
    assert ports.worker._safe_point is safe_point


def test_safe_point_factory_does_not_close_external_resource_on_builder_failure(tmp_path):
    runtime, run_id = _runtime_with_run(tmp_path)
    closed = []
    resource = type("Resource", (), {"close": lambda self: closed.append(True)})()

    def fail(_provider):
        raise RuntimeError("builder failed")

    with pytest.raises(RuntimeError, match="builder failed"):
        engine_factory.build_pipeline_ports(
            runtime, run_id, _all_native_routing(), backend_factory=fail,
            worker_safe_point=resource,
        )
    assert closed == []


def test_build_pipeline_ports_acp_for_claude_and_codex(tmp_path):
    runtime, run_id = _runtime_with_run(tmp_path)
    routing = {"planner": "claude", "coder": "codex-cli", "reviewer": "claude"}
    ports = engine_factory.build_pipeline_ports(
        runtime, run_id, routing, acp_client_factory=_FakeAcpClient,
    )
    assert isinstance(ports.planner, AcpPlanAttemptRunner)
    assert isinstance(ports.worker, AcpWorkerAttemptAdapter)
    assert isinstance(ports.reviewer, AcpReviewAttemptRunner)


def test_opt_out_keeps_original_native_worker_constructor_call_shape(tmp_path, monkeypatch):
    runtime, run_id = _runtime_with_run(tmp_path)
    calls = []
    worker = object()

    def original_constructor(runtime_arg, run_id_arg, backend_arg):
        calls.append((runtime_arg, run_id_arg, backend_arg))
        return worker

    monkeypatch.setattr(engine_factory, "NativeWorkerAttemptAdapter", original_constructor)
    ports = engine_factory.build_pipeline_ports(
        runtime, run_id, _all_native_routing(), backend_factory=lambda _pid: _FakeBackend(),
    )
    assert ports.worker is worker
    assert len(calls) == 1
    assert calls[0][:2] == (runtime, run_id)


def test_build_pipeline_ports_acp_for_gemini_cli(tmp_path):
    runtime, run_id = _runtime_with_run(tmp_path)
    routing = {"planner": "gemini", "coder": "deepseek", "reviewer": "gemini-cli"}
    ports = engine_factory.build_pipeline_ports(
        runtime, run_id, routing,
        backend_factory=lambda pid: _FakeBackend(),
        acp_client_factory=_FakeAcpClient,
    )
    assert isinstance(ports.reviewer, AcpReviewAttemptRunner)


def test_build_pipeline_ports_native_for_anthropic(tmp_path):
    runtime, run_id = _runtime_with_run(tmp_path)
    routing = {"planner": "anthropic", "coder": "deepseek", "reviewer": "openai"}
    seen_providers = []

    def backend_factory(provider_id):
        seen_providers.append(provider_id)
        return _FakeBackend()

    ports = engine_factory.build_pipeline_ports(
        runtime, run_id, routing, backend_factory=backend_factory,
    )
    assert isinstance(ports.planner, NativePlanAttemptRunner)
    assert seen_providers[0] == "anthropic"


def test_build_pipeline_ports_unsupported_role_raises(tmp_path):
    runtime, run_id = _runtime_with_run(tmp_path)
    routing = {**_all_native_routing(), "coder": "qwen-code"}
    calls = []

    def forbidden(*_args):
        calls.append(True)

    with pytest.raises(engine_factory.EngineUnsupportedError):
        engine_factory.build_pipeline_ports(
            runtime, run_id, routing, backend_factory=forbidden, acp_client_factory=forbidden,
        )
    assert calls == []


# ---------------- worktree yaşam döngüsü ----------------

def test_create_and_dispose_pipeline_workspace(git_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: tmp_path / "workspaces")
    ws = engine_factory.create_pipeline_workspace(git_repo, "run-1")
    assert ws.root.is_dir()
    ws.dispose()


def test_prune_startup_workspaces_removes_stale_entries(git_repo, tmp_path, monkeypatch):
    base = tmp_path / "workspaces"
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: base)
    ws = engine_factory.create_pipeline_workspace(git_repo, "stale-run")
    worktree_dir = base / "stale-run"
    assert worktree_dir.is_dir()
    # Uygulamanın çöktüğünü simüle et: dispose() ÇAĞRILMADAN worktree kalır.
    del ws

    engine_factory.prune_startup_workspaces()
    assert not worktree_dir.exists()
    # git worktree list artık bu yolu bilmemeli.
    out = subprocess.run(
        ["git", "worktree", "list"], cwd=git_repo, capture_output=True, text=True,
    ).stdout
    assert "stale-run" not in out


def test_prune_startup_workspaces_best_effort_on_garbage_entry(tmp_path, monkeypatch):
    base = tmp_path / "workspaces"
    base.mkdir(parents=True)
    (base / "garbage").mkdir()
    (base / "garbage" / "not-a-worktree.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: base)
    engine_factory.prune_startup_workspaces()  # exception fırlatmamalı
    assert not (base / "garbage").exists()
