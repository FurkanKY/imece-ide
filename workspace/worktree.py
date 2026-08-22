"""workspace.worktree — izole, Git worktree tabanlı ajan çalışma alanı.

Ajan çalıştırmaları kullanıcının asıl worktree'sini DOĞRUDAN değiştirmemeli;
bunun yerine burada "shadow" (gölge) denilen, ayrı bir detached linked Git
worktree üzerinde çalışır. Yaşam döngüsü:

  1. Kaynak repo/HEAD doğrulanır (bkz. _discover_repository, _check_no_conflicts).
  2. `git worktree add --detach <hedef> <source_head>` ile HEAD'e sabitli,
     dallanmamış bir linked worktree oluşturulur.
  3. Kullanıcının o anki Git-görünür çalışma durumu (staged + unstaged +
     tracked silmeler + ignore edilmeyen untracked dosyalar) shadow
     worktree'ye bindirilir (bkz. _overlay_working_state). Ignore edilen
     dosyalar (.env, node_modules, .venv, build çıktıları, ...) KOPYALANMAZ.
  4. Bindirilen durum, `git add -A` + `write-tree` + `commit-tree` plumbing
     komutlarıyla yerel/geçici bir "sentetik snapshot" commit'i olarak
     kaydedilir ve shadow worktree'nin HEAD'i bu commit'e taşınır. Bu commit
     hiçbir dala bağlanmaz, push edilmez, kullanıcı commit hook'ları
     çalışmaz. Kullanıcının asıl worktree'sinin HEAD/index/dalı DOKUNULMAZ.

Bu sayede ajan sonradan foo.py'yi B'den C'ye değiştirdiğinde, snapshot_commit
ile karşılaştırma B->C farkını verir; A->C değil (A = kullanıcının son
commit'i, B = ajan başlamadan HEMEN ÖNCEKİ çalışma kopyası).

BİLİNEN SINIRLAMA (v1): Git submodule/gitlink (mode 160000) içeren repolar
desteklenmez ve UnsupportedRepositoryStateError ile reddedilir (bkz.
_check_no_gitlinks). Submodule desteği kasıtlı olarak sonraki bir milestone'a
bırakıldı.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import workspaces_dir
from workspace.base import Workspace
from workspace.errors import (
    UnsupportedRepositoryStateError,
    WorkspaceCleanupError,
    WorkspaceCreationError,
    WorkspaceError,
    WorkspaceGitError,
)
from workspace.snapshot import WorkspaceSnapshot

_GIT_TIMEOUT = 30  # sn
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SYNTHETIC_MESSAGE = "Imece IDE synthetic workspace snapshot"
_SYNTHETIC_IDENTITY = {
    "GIT_AUTHOR_NAME": "Imece IDE",
    "GIT_AUTHOR_EMAIL": "imece@local",
    "GIT_COMMITTER_NAME": "Imece IDE",
    "GIT_COMMITTER_EMAIL": "imece@local",
}
_GITLINK_MODE = b"160000"  # git submodule/gitlink dizin girişi modu


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PYTHONUTF8": "1", "LC_ALL": "C.UTF-8"}


def _run_git(
    args: list[str], *, cwd: Path, env: dict[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    """git komutunu argüman dizisiyle (shell=True YOK) çalıştırır.

    Ham subprocess hatalarını (FileNotFoundError, TimeoutExpired, sıfır
    olmayan çıkış kodu) tipli WorkspaceGitError'a çevirir.
    """
    try:
        cp = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=env if env is not None else _git_env(),
            timeout=_GIT_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise WorkspaceGitError("git bulunamadı (PATH'te değil).") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceGitError(f"git {' '.join(args)} zaman aşımına uğradı.") from exc
    if check and cp.returncode != 0:
        stderr = cp.stderr.decode("utf-8", "replace").strip()
        raise WorkspaceGitError(f"git {' '.join(args)} başarısız: {stderr[:400]}")
    return cp


def _git_text(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    cp = _run_git(args, cwd=cwd, env=env)
    return cp.stdout.decode("utf-8", "replace")


def _git_paths(args: list[str], *, cwd: Path) -> list[str]:
    """NUL ayraçlı git çıktısını (`-z`) yol listesine çevirir.

    Dosya adları rastgele bayt dizileri olabileceğinden utf-8 yerine
    os.fsdecode kullanılır (dosya sistemi kodlamasıyla kayıpsız round-trip).
    """
    cp = _run_git(args, cwd=cwd)
    if not cp.stdout:
        return []
    return [os.fsdecode(tok) for tok in cp.stdout.split(b"\0") if tok]


def _validate_run_id(run_id: str) -> str:
    """run_id'yi dosya sistemi yolu bileşeni olarak kullanmadan önce doğrular.

    Yol ayırıcı, '..' veya boş girdi içeren run_id'ler reddedilir; böylece
    run_id üzerinden workspaces_dir() dışına çıkış (path traversal) engellenir.
    """
    if not run_id or not _RUN_ID_RE.match(run_id):
        raise WorkspaceCreationError(f"Geçersiz run_id: {run_id!r}")
    return run_id


def _discover_repository(source_root: Path) -> tuple[Path, str]:
    if not source_root.is_dir():
        raise WorkspaceCreationError(f"Kaynak klasör yok: {source_root}")

    cp = _run_git(["rev-parse", "--show-toplevel"], cwd=source_root, check=False)
    if cp.returncode != 0:
        raise UnsupportedRepositoryStateError(
            f"{source_root} bir Git çalışma ağacı içinde değil."
        )
    repo_root = Path(cp.stdout.decode("utf-8", "replace").strip()).resolve()

    cp = _run_git(["rev-parse", "--verify", "HEAD"], cwd=repo_root, check=False)
    if cp.returncode != 0:
        raise UnsupportedRepositoryStateError(
            "Repoda henüz bir HEAD commit'i yok (ilk commit yapılmamış)."
        )
    source_head = cp.stdout.decode("utf-8", "replace").strip()
    return repo_root, source_head


def _check_no_conflicts(repo_root: Path) -> None:
    """İndekste çözülmemiş (unmerged) giriş var mı diye doğrudan Git indeksine bakar.

    `git status --porcelain` çıktısını XY kodlarıyla ayrıştırmak yerine
    `git ls-files -u` kullanılır: unmerged bir yol varsa (stage 1/2/3 girişleri)
    bu komut o yol için en az bir satır üretir; çıktı boşsa çakışma yoktur.
    """
    cp = _run_git(["ls-files", "-u", "-z"], cwd=repo_root)
    if cp.stdout.strip(b"\0"):
        raise UnsupportedRepositoryStateError(
            "Repoda çözülmemiş merge çakışmaları var; bu sürümde desteklenmiyor."
        )


def _check_no_gitlinks(repo_root: Path) -> None:
    """Git submodule/gitlink (mode 160000) içeren repoları reddeder.

    Workspace Runtime Foundation v1 kapsam dışı: bir gitlink kaydı sıradan bir
    dosya/sembolik bağ değildir ve _overlay_working_state bunu doğru şekilde
    ele alamaz; ayrıca shadow worktree'nin submodule içeriği kullanıcının
    checkout ettiği içerikten farklı/eksik olabilir. Bu nedenle submodule
    içeren repolar şimdilik UnsupportedRepositoryStateError ile reddedilir.
    """
    cp = _run_git(["ls-files", "--stage", "-z"], cwd=repo_root)
    for entry in cp.stdout.split(b"\0"):
        if not entry:
            continue
        mode = entry.split(b" ", 1)[0]
        if mode == _GITLINK_MODE:
            raise UnsupportedRepositoryStateError(
                "Repo bir Git submodule/gitlink içeriyor; bu sürümde desteklenmiyor."
            )


def _copy_symlink_safely(src: Path, dst: Path) -> None:
    """Sembolik bağı, hedefini İZLEMEDEN yeniden oluşturur.

    Hedefi takip edip içeriğini kopyalasaydık, repo dışındaki keyfi bir
    dosyanın içeriği shadow worktree'ye sızabilirdi. Bunun yerine ham bağ
    hedefi (readlink) aynen yeniden yazılır — git zaten sembolik bağları bu
    şekilde (hedef metnini içeren bir blob olarak) izler.
    """
    target = os.readlink(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    os.symlink(target, dst)


def _overlay_working_state(
    repo_root: Path, worktree_root: Path, dirty_paths: list[str], untracked_paths: list[str]
) -> None:
    """Kullanıcının o anki Git-görünür çalışma durumunu shadow worktree'ye bindirir."""
    for rel in (*dirty_paths, *untracked_paths):
        src = repo_root / rel
        dst = worktree_root / rel
        if src.is_symlink():
            _copy_symlink_safely(src, dst)
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif dst.exists() or dst.is_symlink():
            # Tracked bir yol kaynakta silinmiş: shadow'da da kaldır.
            dst.unlink()


def _write_synthetic_snapshot(worktree_root: Path, source_head: str) -> str:
    """Bindirilen çalışma durumunu yerel bir sentetik commit olarak dondurur.

    `git add -A` + `write-tree` + `commit-tree` git plumbing'i kullanılır;
    normal `git commit` porselen davranışından (kullanıcı hook'ları vb.)
    kaçınılır. Kimlik yalnızca bu alt-süreçlerin ortam değişkenleriyle
    verilir — global/repo git config'i değiştirilmez.

    Bindirilen durum zaten HEAD ile aynıysa (ajan başlamadan önce çalışma
    kopyası tertemizse), gereksiz bir commit oluşturmak yerine source_head
    aynen döndürülür.
    """
    env = {**_git_env(), **_SYNTHETIC_IDENTITY}
    _run_git(["add", "-A"], cwd=worktree_root, env=env)
    tree = _git_text(["write-tree"], cwd=worktree_root, env=env).strip()
    head_tree = _git_text(["rev-parse", f"{source_head}^{{tree}}"], cwd=worktree_root, env=env).strip()
    if tree == head_tree:
        return source_head

    cp = _run_git(
        ["commit-tree", tree, "-p", source_head, "-m", _SYNTHETIC_MESSAGE],
        cwd=worktree_root,
        env=env,
    )
    commit = cp.stdout.decode("utf-8", "replace").strip()
    # Yalnızca BU linked worktree'nin (detached) HEAD dosyasını taşır;
    # kullanıcının asıl worktree'sindeki dal/HEAD etkilenmez.
    _run_git(["update-ref", "HEAD", commit], cwd=worktree_root, env=env)
    return commit


def _remove_worktree(repo_root: Path, worktree_dir: Path) -> None:
    if not worktree_dir.exists():
        _run_git(["worktree", "prune"], cwd=repo_root, check=False)
        return
    cp = _run_git(["worktree", "remove", "--force", str(worktree_dir)], cwd=repo_root, check=False)
    if cp.returncode != 0 or worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)
        _run_git(["worktree", "prune"], cwd=repo_root, check=False)
        if worktree_dir.exists():
            stderr = cp.stderr.decode("utf-8", "replace").strip()
            raise WorkspaceCleanupError(f"Workspace temizlenemedi: {worktree_dir}: {stderr[:400]}")


def _best_effort_cleanup(repo_root: Path, worktree_dir: Path, registered: bool) -> None:
    """Kısmi oluşturma başarısız olduğunda artık dizin/worktree bırakmamak için.

    Burada oluşan ikincil hatalar kasıtlı olarak yutulur: orijinal oluşturma
    hatası zaten çağırana fırlatılacak, bu yalnızca en iyi çaba temizliğidir.
    """
    try:
        if registered:
            _run_git(["worktree", "remove", "--force", str(worktree_dir)], cwd=repo_root, check=False)
        if worktree_dir.exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)
        _run_git(["worktree", "prune"], cwd=repo_root, check=False)
    except Exception:
        pass


class GitWorktreeWorkspace(Workspace):
    """HEAD + kullanıcının o anki çalışma durumundan türetilmiş, izole shadow worktree."""

    def __init__(
        self,
        *,
        root: Path,
        worktree_dir: Path,
        repo_root: Path,
        snapshot: WorkspaceSnapshot,
    ):
        self._root = root
        self._worktree_dir = worktree_dir
        self._repo_root = repo_root
        self._snapshot = snapshot
        self._disposed = False

    @property
    def root(self) -> Path:
        return self._root

    @property
    def snapshot(self) -> WorkspaceSnapshot:
        return self._snapshot

    @classmethod
    def create(
        cls,
        *,
        source_root: str | Path,
        run_id: str,
        base_dir: str | Path | None = None,
    ) -> "GitWorktreeWorkspace":
        run_id = _validate_run_id(run_id)
        source_root = Path(source_root).resolve()
        base = Path(base_dir).resolve() if base_dir is not None else workspaces_dir()
        base.mkdir(parents=True, exist_ok=True)

        worktree_dir = base / run_id
        if worktree_dir.exists():
            raise WorkspaceCreationError(f"Workspace zaten var: {worktree_dir}")

        repo_root, source_head = _discover_repository(source_root)
        _check_no_conflicts(repo_root)
        _check_no_gitlinks(repo_root)
        project_relative = source_root.relative_to(repo_root)

        registered = False
        try:
            _run_git(
                ["worktree", "add", "--detach", str(worktree_dir), source_head],
                cwd=repo_root,
            )
            registered = True

            dirty = _git_paths(["diff", "--name-only", "--no-renames", "-z", "HEAD"], cwd=repo_root)
            untracked = _git_paths(["ls-files", "--others", "--exclude-standard", "-z"], cwd=repo_root)
            _overlay_working_state(repo_root, worktree_dir, dirty, untracked)
            snapshot_commit = _write_synthetic_snapshot(worktree_dir, source_head)
        except Exception as exc:
            _best_effort_cleanup(repo_root, worktree_dir, registered)
            if isinstance(exc, WorkspaceError):
                raise
            raise WorkspaceCreationError(f"Workspace oluşturulamadı: {exc}") from exc

        workspace_root = (worktree_dir / project_relative).resolve()
        snapshot = WorkspaceSnapshot(
            run_id=run_id,
            source_root=source_root,
            repository_root=repo_root,
            source_head=source_head,
            snapshot_commit=snapshot_commit,
            project_relative_root=project_relative,
            tracked_dirty_paths=tuple(dirty),
            untracked_paths=tuple(untracked),
            created_at=datetime.now(timezone.utc),
        )
        return cls(root=workspace_root, worktree_dir=worktree_dir, repo_root=repo_root, snapshot=snapshot)

    def dispose(self) -> None:
        if self._disposed:
            return
        # Yalnızca temizlik GERÇEKTEN başarılı olduktan sonra işaretlenir;
        # _remove_worktree hata fırlatırsa bu obje "disposed" sayılmaz ve bir
        # sonraki dispose() çağrısı temizliği yeniden dener.
        _remove_worktree(self._repo_root, self._worktree_dir)
        self._disposed = True
