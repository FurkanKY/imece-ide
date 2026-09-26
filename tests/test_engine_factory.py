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


def test_build_pipeline_ports_acp_for_claude_and_codex(tmp_path):
    runtime, run_id = _runtime_with_run(tmp_path)
    routing = {"planner": "claude", "coder": "codex-cli", "reviewer": "claude"}
    ports = engine_factory.build_pipeline_ports(
        runtime, run_id, routing, acp_client_factory=_FakeAcpClient,
    )
    assert isinstance(ports.planner, AcpPlanAttemptRunner)
    assert isinstance(ports.worker, AcpWorkerAttemptAdapter)
    assert isinstance(ports.reviewer, AcpReviewAttemptRunner)


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
    with pytest.raises(engine_factory.EngineUnsupportedError):
        engine_factory.build_pipeline_ports(runtime, run_id, routing)


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
