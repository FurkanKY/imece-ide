"""engine_factory.py — Aşama 1 T1.2: "yeni" (pipeline_runtime tabanlı) AI
motoru ile mevcut "legacy" (project_runner tabanlı) motor arasında SEÇİM ve
yeni motorun rol bazlı port'larının (planner/worker/reviewer/verification/
change_provider) İNŞASI.

Bu modül webhost/PySide6'dan BAĞIMSIZDIR (yalnızca stdlib + zaten var olan
runtime paketleri) — böylece hem webhost/api/run.py hem de saf pytest testleri
tarafından Qt'siz kullanılabilir.

Sorumluluk sınırı: bu modül yalnızca "hangi motor" ve "hangi adapter" sorusuna
cevap verir; NE ZAMAN çalıştırılacağına veya bir Run'ın yaşam döngüsüne asla
karışmaz (bkz. webhost/api/run.py).

Rol yönlendirmesi (routing) providers.py kataloğundan gelir:
  - kind == "openai": agent_runtime.providers.ChatCompletionsBackend ile
    native adapter (NativePlanAttemptRunner / NativeWorkerAttemptAdapter /
    NativeReviewAttemptAdapter).
  - kind == "cli", id == "claude": ACP "claude-code" preseti (hesap girişi;
    API anahtarı YOK) ile ACP adapter (AcpPlanAttemptRunner /
    AcpWorkerAttemptAdapter / AcpReviewAttemptRunner).
  - kind == "cli", id == "codex-cli": ACP "codex" preseti ile aynı.
  - diğer CLI'lar (gemini-cli, qwen-code): yeni motor TARAFINDAN henüz
    desteklenmiyor -> EngineUnsupportedError.
  - Verification HER ZAMAN native'dir (NativeVerificationAttemptAdapter),
    hiçbir routing'e bağlı değildir.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import providers as provider_registry
from change_runtime import GitWorktreeChangeProvider
from change_runtime.provider import ChangeProvider
from chat_completions_catalog import (
    ProviderKeyMissingError,
    UnknownProviderError,
    chat_completions_backend_from_catalog,
)
from executor_runtime import (
    AcpReviewAttemptRunner,
    AcpWorkerAttemptAdapter,
    NativeReviewAttemptAdapter,
    NativeVerificationAttemptAdapter,
    NativeWorkerAttemptAdapter,
    claude_code_acp_launch_profile,
    codex_acp_launch_profile,
)
from pipeline_runtime.acp_planner import AcpPlanAttemptRunner
from pipeline_runtime.native_planner import NativePlanAttemptRunner
from pipeline_runtime.ports import PlanAttemptRunner
from review_runtime.runner import ReviewerRunner
from run_runtime.service import RunRuntime
from runtime_paths import workspaces_dir
from workspace.worktree import GitWorktreeWorkspace

# Yeni motorun desteklediği ajan CLI'ları -> ACP launch preset fabrikası.
_ACP_CLI_PRESETS: dict[str, Callable[[], object]] = {
    "claude": claude_code_acp_launch_profile,
    "codex-cli": codex_acp_launch_profile,
}

PIPELINE_ROLES = ("planner", "worker", "reviewer")
# Kanonik pipeline rolü -> legacy DEFAULT_ROUTING/agents.py anahtarı
# ("worker" kanonik, "coder" legacy — bkz. run_runtime/legacy.py).
_LEGACY_ROUTING_KEY = {"planner": "planner", "worker": "coder", "reviewer": "reviewer"}


class EngineUnsupportedError(Exception):
    """Yeni (pipeline) motor bu Run için kullanılamaz; nedeni `reason`'dadır."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class EngineSelection:
    engine: str  # "pipeline" | "legacy"
    reason: str | None = None  # legacy'ye düşüldüyse kullanıcıya gösterilecek Türkçe not


def _provider_kind(provider_id: str) -> tuple[dict | None, str | None]:
    entry = provider_registry.get(provider_id)
    if entry is None:
        return None, None
    return entry, entry.get("kind")


def role_supported(provider_id: str) -> tuple[bool, str]:
    """(destekleniyor mu, gerekçe) — provider_id yeni motorda bir rolü sürebilir mi?"""
    entry, kind = _provider_kind(provider_id)
    if entry is None:
        return False, f"Bilinmeyen sağlayıcı: {provider_id!r}."
    if kind == "openai":
        return True, "openai-uyumlu API"
    if kind == "cli":
        if provider_id in _ACP_CLI_PRESETS:
            return True, "hesap (ACP) girişi"
        return False, f"{entry.get('label', provider_id)} yeni motor tarafından henüz desteklenmiyor."
    return False, f"Bilinmeyen sağlayıcı türü: {kind!r}."


def _repo_root_for(project_root: str | Path) -> Path | None:
    if shutil.which("git") is None:
        return None
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(project_root), capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    root_text = cp.stdout.decode("utf-8", "replace").strip()
    return Path(root_text) if root_text else None


def select_engine(
    project_root: str | Path, routing: dict[str, str], *, ai_engine_pref: str = "auto",
) -> EngineSelection:
    """Bir run.start çağrısı için motor seçer.

    ai_engine_pref == "legacy" ise kullanıcı açıkça klasik motoru istemiştir —
    hiçbir gerekçe metni üretilmez (zaten kendi seçimidir).
    ai_engine_pref == "auto" (varsayılan) ise: proje bir Git deposu değilse
    VEYA routing'teki üç rolden (planner/worker/reviewer) biri bile yeni
    motorca desteklenmiyorsa, klasik motora KULLANICIYA GÖSTERİLECEK bir
    Türkçe gerekçeyle düşülür.
    """
    if ai_engine_pref not in ("auto", "legacy"):
        ai_engine_pref = "auto"
    if ai_engine_pref == "legacy":
        return EngineSelection(engine="legacy", reason=None)

    repo_root = _repo_root_for(project_root)
    if repo_root is None:
        return EngineSelection(
            engine="legacy",
            reason="Proje bir Git deposu değil — klasik motor kullanılıyor.",
        )

    for role in PIPELINE_ROLES:
        legacy_key = _LEGACY_ROUTING_KEY[role]
        provider_id = routing.get(legacy_key)
        if not provider_id:
            return EngineSelection(
                engine="legacy",
                reason=f"'{role}' rolü için sağlayıcı atanmamış — klasik motor kullanılıyor.",
            )
        supported, why = role_supported(provider_id)
        if not supported:
            return EngineSelection(engine="legacy", reason=f"{why} Klasik motor kullanılıyor.")

    return EngineSelection(engine="pipeline", reason=None)


@dataclass(frozen=True, slots=True)
class PipelinePorts:
    planner: PlanAttemptRunner
    worker: object
    reviewer: object
    verification: NativeVerificationAttemptAdapter
    change_provider: ChangeProvider


def build_pipeline_ports(
    runtime: RunRuntime,
    run_id: str,
    routing: dict[str, str],
    *,
    backend_factory: Callable[[str], object] | None = None,
    acp_client_factory: Callable[[], object] | None = None,
    change_provider: ChangeProvider | None = None,
) -> PipelinePorts:
    """routing'e göre gerçek (veya test enjeksiyonlu) pipeline port'larını inşa eder.

    `backend_factory(provider_id) -> ModelBackend` ve `acp_client_factory() ->
    ACP client (bkz. acp_runtime.client.AcpClientRuntime arayüzü)` testlerin
    gerçek ağ/süreç çağrısı yapmadan ScriptedBackend/sahte ACP istemcisi
    enjekte edebilmesi için vardır; üretimde ikisi de varsayılana düşer.

    Her rol İÇİN AYRI bir backend/acp_client örneği kurulur (paylaşılan
    mutable durum yok) — yalnızca `verification` hiçbir routing'e bağlı
    değildir ve her zaman native'dir.
    """
    backend_factory = backend_factory or _default_backend_factory
    acp_client_factory = acp_client_factory or _default_acp_client_factory

    resolved: dict[str, tuple[dict, str]] = {}
    for role in PIPELINE_ROLES:
        provider_id = routing.get(_LEGACY_ROUTING_KEY[role])
        if not provider_id:
            raise EngineUnsupportedError(f"'{role}' rolü için sağlayıcı atanmamış.")
        supported, why = role_supported(provider_id)
        if not supported:
            raise EngineUnsupportedError(why)
        entry, kind = _provider_kind(provider_id)
        resolved[role] = (entry, kind)

    planner = _build_planner(runtime, run_id, provider_id=routing[_LEGACY_ROUTING_KEY["planner"]],
                              entry=resolved["planner"][0], kind=resolved["planner"][1],
                              backend_factory=backend_factory, acp_client_factory=acp_client_factory)
    worker = _build_worker(runtime, run_id, provider_id=routing[_LEGACY_ROUTING_KEY["worker"]],
                            entry=resolved["worker"][0], kind=resolved["worker"][1],
                            backend_factory=backend_factory, acp_client_factory=acp_client_factory)
    reviewer = _build_reviewer(runtime, run_id, provider_id=routing[_LEGACY_ROUTING_KEY["reviewer"]],
                                entry=resolved["reviewer"][0], kind=resolved["reviewer"][1],
                                backend_factory=backend_factory, acp_client_factory=acp_client_factory)
    verification = NativeVerificationAttemptAdapter(runtime, run_id)
    return PipelinePorts(
        planner=planner, worker=worker, reviewer=reviewer, verification=verification,
        change_provider=change_provider or GitWorktreeChangeProvider(),
    )


def _default_backend_factory(provider_id: str):
    try:
        return chat_completions_backend_from_catalog(provider_id)
    except (UnknownProviderError, ProviderKeyMissingError) as exc:
        raise EngineUnsupportedError(str(exc)) from exc


def _default_acp_client_factory():
    from acp_runtime.client import AcpClientRuntime
    return AcpClientRuntime()


def _acp_launch_profile(provider_id: str):
    return _ACP_CLI_PRESETS[provider_id]()


def _build_planner(runtime, run_id, *, provider_id, entry, kind, backend_factory, acp_client_factory):
    if kind == "openai":
        backend = backend_factory(provider_id)
        return NativePlanAttemptRunner(runtime, run_id, backend)
    launch_profile = _acp_launch_profile(provider_id)
    acp_client = acp_client_factory()
    return AcpPlanAttemptRunner(runtime, run_id, launch_profile, acp_client)


def _build_worker(runtime, run_id, *, provider_id, entry, kind, backend_factory, acp_client_factory):
    if kind == "openai":
        backend = backend_factory(provider_id)
        return NativeWorkerAttemptAdapter(runtime, run_id, backend)
    launch_profile = _acp_launch_profile(provider_id)
    acp_client = acp_client_factory()
    return AcpWorkerAttemptAdapter(runtime, run_id, launch_profile, acp_client)


def _build_reviewer(runtime, run_id, *, provider_id, entry, kind, backend_factory, acp_client_factory):
    if kind == "openai":
        backend = backend_factory(provider_id)
        return NativeReviewAttemptAdapter(runtime, run_id, ReviewerRunner(backend))
    launch_profile = _acp_launch_profile(provider_id)
    acp_client = acp_client_factory()
    return AcpReviewAttemptRunner(runtime, run_id, launch_profile, acp_client)


# ---------------------------------------------------------------------------
# Worktree yaşam döngüsü: oluşturma + başlangıçta artık (stale) temizliği.
# ---------------------------------------------------------------------------

def create_pipeline_workspace(project_root: str | Path, run_id: str) -> GitWorktreeWorkspace:
    return GitWorktreeWorkspace.create(
        source_root=project_root, run_id=run_id, base_dir=workspaces_dir(),
    )


def prune_startup_workspaces() -> None:
    """Uygulama başlarken önceki bir çökme sonrası kalmış olabilecek izole
    worktree'leri (yalnızca BİZİM workspaces_dir()'ımız altındakileri)
    temizler. En iyi çabadır: tek bir bozuk girdi tüm taramayı durdurmaz."""
    base = workspaces_dir()
    if not base.is_dir():
        return
    for entry in sorted(base.iterdir()):
        try:
            _prune_one_workspace(entry)
        except Exception:
            continue


def _prune_one_workspace(worktree_dir: Path) -> None:
    if not worktree_dir.is_dir():
        return
    repo_root = _discover_repo_root_for_worktree(worktree_dir)
    if repo_root is not None:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            cwd=str(repo_root), capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "worktree", "prune"], cwd=str(repo_root), capture_output=True, timeout=30,
        )
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)


def _discover_repo_root_for_worktree(worktree_dir: Path) -> Path | None:
    """worktree_dir'in bağlı olduğu ASIL repo kökünü (varsa) bulur.

    Linked bir worktree'nin `.git` dosyası `gitdir: <repo>/.git/worktrees/<id>`
    biçimindedir; buradan asıl repo kökü türetilir. Okunamazsa None döner
    (yalnızca dizin best-effort silinir, hiçbir git komutu çalıştırılmaz)."""
    dotgit = worktree_dir / ".git"
    try:
        text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text.split(":", 1)[1].strip())
    # .../<repo>/.git/worktrees/<id>  ->  <repo>
    parts = gitdir.parts
    try:
        idx = parts.index("worktrees")
        git_dir = Path(*parts[:idx])  # .../<repo>/.git
        return git_dir.parent
    except (ValueError, IndexError):
        return None
