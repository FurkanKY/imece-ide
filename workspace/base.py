"""workspace.base — dosya sistemi/yaşam döngüsü sınırı olarak soyut Workspace.

Bu katman şimdilik SADECE dosya okuma/yazma ve güvenli yol çözümüyle
ilgilenir. Ajan/LLM kavramları (prompt, context karakter sınırı, shell/grep
araçları, review mantığı) kasıtlı olarak burada YOK — onlar gelecekteki bir
ContextManager/ToolRuntime katmanına ait. Özellikle read_text, Project
sınıfının 20.000 karakterlik LLM context sınırını UYGULAMAZ; dosyanın tam
içeriğini döndürür.

ÖNEMLİ SINIR: buradaki yol sınırı denetimleri (resolve_within_workspace) yalnızca
DOSYA SİSTEMİ erişimini kapsar; bir işlem/sandbox izolasyonu DEĞİLDİR. Bu katman
hiçbir shell/süreç çalıştırmaz. Gelecekte eklenecek shell/araç çalıştırma
katmanı (Tool Runtime), kendi izolasyon politikasını (örn. çalışma dizini
kısıtlaması, ortam değişkeni temizliği, ayrı süreç/sandbox) kendisi uygulamak
zorundadır — bu sınıfın root/.git korumasına güvenemez.
"""

from __future__ import annotations

import re
import shutil
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from pathlib import Path

from workspace.errors import WorkspaceBoundaryError

_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_ENV_PATH_RE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*(?:/|$)")


def _looks_absolute(raw: str) -> bool:
    """POSIX ('/...'), Windows sürücü ('C:\\...') ve UNC ('\\\\server\\...') yollarını yakalar."""
    normalized = raw.replace("\\", "/")
    return normalized.startswith("/") or bool(_WINDOWS_DRIVE_RE.match(normalized))


def normalize_workspace_relative_path(raw: str, *, allow_root: bool = False) -> str:
    """Normalize a workspace-relative path without resolving traversal away."""
    if not isinstance(raw, str):
        raise WorkspaceBoundaryError("Workspace yolu string olmalı.")
    if "\x00" in raw:
        raise WorkspaceBoundaryError("Workspace yolu NUL karakteri içeremez.")
    normalized = raw.replace("\\", "/")
    if _looks_absolute(normalized):
        raise WorkspaceBoundaryError(f"Mutlak workspace yolu kabul edilmiyor: {raw!r}")
    if normalized.startswith("~/") or normalized == "~" or _ENV_PATH_RE.match(normalized):
        raise WorkspaceBoundaryError(f"Workspace yolu genişletilemez: {raw!r}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise WorkspaceBoundaryError(f"'..' workspace yolunda kullanılamaz: {raw!r}")
    if any(part.casefold() == ".git" for part in parts):
        raise WorkspaceBoundaryError(f".git workspace yolu kabul edilmiyor: {raw!r}")
    if not parts:
        if allow_root and normalized in (".", "./"):
            return "."
        raise WorkspaceBoundaryError("Boş workspace yolu kabul edilmiyor.")
    return "/".join(parts)


def _reject_symlink_components(
    root: Path, parts: list[str], *, include_leaf: bool = True
) -> None:
    """Reject existing symlink components without resolving them."""
    current = root
    last_index = len(parts) if include_leaf else max(len(parts) - 1, 0)
    for index, part in enumerate(parts):
        current = current / part
        if index < last_index and current.is_symlink():
            raise WorkspaceBoundaryError(
                f"Sembolik bağ üzerinden workspace erişimi engellendi: {part!r}"
            )


def resolve_within_workspace(
    root: Path,
    relative_path: str,
    *,
    resolve_final: bool = True,
    reject_symlinks: bool = False,
    allow_final_symlink: bool = False,
) -> Path:
    """`relative_path`i `root`a göre çözer; kök dışına veya `.git` altına çıkışı reddeder.

    Sadece string ön-eki karşılaştırması YAPMAZ: sembolik bağlar gerçek yola
    çözülür ve sonuç, çözülmüş kökün altında kalmıyorsa reddedilir.

    `..` bileşenine sahip HİÇBİR yol kabul edilmez (konumu fark etmez —
    "..", "sub/..", "foo/../bar" hepsi reddedilir). Bu özellikle
    resolve_final=False durumunda kritiktir: son bileşen kasıtlı olarak
    çözülmediği için (sembolik bağın kendisini hedefleyebilmek adına),
    lexical bir ".." son bileşeni üst dizin çözümünü atlatıp kök dışında bir
    silme işlemine yol açabilirdi. Ajan/araç çağıranları normalize edilmiş,
    workspace-göreli yollar kullanmalıdır.

    resolve_final=False, sembolik bağın KENDİSİNİ (hedefini değil) hedef alan
    işlemler (örn. silme) için kullanılır: yalnızca üst dizinler gerçek yol
    çözümünden geçirilir, son bileşen izlenmez — böylece workspace içindeki bir
    bağ, dışarıyı gösterse bile bağın kendisi güvenle kaldırılabilir.
    """
    if not relative_path:
        raise WorkspaceBoundaryError("Boş yol verilemez.")
    if _looks_absolute(relative_path):
        raise WorkspaceBoundaryError(f"Mutlak yol kabul edilmiyor: {relative_path!r}")

    parts = [p for p in relative_path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        raise WorkspaceBoundaryError(f"Geçersiz yol: {relative_path!r}")
    if any(p == ".." for p in parts):
        raise WorkspaceBoundaryError(f"'..' yol bileşenine izin verilmiyor: {relative_path!r}")
    if any(p.casefold() == ".git" for p in parts):
        # Windows'ta dosya sistemleri genelde büyük/küçük harf duyarsızdır;
        # .GIT / .Git gibi varyantlar da (lexical olarak, çözümden ÖNCE) engellenir.
        raise WorkspaceBoundaryError(
            f".git dizinine genel dosya erişimi engellendi: {relative_path!r}"
        )

    if reject_symlinks:
        _reject_symlink_components(root.resolve(strict=True), parts, include_leaf=not allow_final_symlink)

    resolved_root = root.resolve(strict=True)

    if resolve_final:
        candidate = resolved_root.joinpath(*parts).resolve(strict=False)
        boundary_check = candidate
    else:
        parent = resolved_root.joinpath(*parts[:-1]).resolve(strict=False)
        candidate = parent / parts[-1]
        boundary_check = parent

    try:
        rel_parts = boundary_check.relative_to(resolved_root).parts
    except ValueError:
        raise WorkspaceBoundaryError(
            f"Yol workspace dışına çıkıyor: {relative_path!r}"
        ) from None

    if not resolve_final:
        rel_parts = (*rel_parts, parts[-1])

    if any(p.casefold() == ".git" for p in rel_parts):
        # Bir sembolik bağ çözümü (gerçek yol) .git'e yönlendirmiş olabilir;
        # bunu lexical denetim yakalayamaz, bu yüzden çözülmüş yol üzerinde
        # aynı (büyük/küçük harf duyarsız) kontrol tekrar uygulanır.
        raise WorkspaceBoundaryError(
            f".git dizinine genel dosya erişimi engellendi: {relative_path!r}"
        )

    return candidate


class Workspace(ABC):
    """Bir ajan çalıştırmasının GÖRDÜĞÜ dosya sistemi sınırı.

    Bu sürümde yalnızca dosya okuma/yazma/varlık/silme ve yaşam döngüsüyle
    (dispose) ilgilenir; model/sağlayıcı kavramı içermez.
    """

    @property
    @abstractmethod
    def root(self) -> Path:
        """Workspace kökünün mutlak, çözülmüş yolu."""

    def read_text(self, relative_path: str) -> str:
        """Dosyanın TAM içeriğini döndürür (context karakter sınırı burada uygulanmaz)."""
        path = self._resolve(relative_path, reject_symlinks=True)
        return path.read_text(encoding="utf-8")

    def write_text(self, relative_path: str, content: str) -> None:
        path = self._resolve(relative_path, reject_symlinks=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    def exists(self, relative_path: str) -> bool:
        try:
            path = self._resolve(relative_path)
        except WorkspaceBoundaryError:
            return False
        return path.exists()

    def delete_path(self, relative_path: str) -> None:
        """Dosyayı/klasörü siler.

        Hedef bir sembolik bağsa, hedefi değil bağın KENDİSİNİ kaldırır.
        """
        path = self._resolve(
            relative_path,
            resolve_final=False,
            reject_symlinks=True,
            allow_final_symlink=True,
        )
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        else:
            raise FileNotFoundError(relative_path)

    @abstractmethod
    def dispose(self) -> None:
        """Bu workspace'in ayırdığı kaynakları serbest bırakır. İdempotent olmalıdır."""

    def iter_files(
        self,
        relative_scope: str = ".",
        *,
        excluded_dirs: Iterable[str] = (),
    ) -> Iterator[str]:
        """Yield regular, non-symlink files under a workspace-relative scope.

        Traversal is deliberately owned by Workspace so alternate workspace
        implementations can replace the filesystem mechanism without changing
        tool contracts. Directory exclusions are names, not .gitignore rules.
        """
        scope_parts = [
            part
            for part in relative_scope.replace("\\", "/").split("/")
            if part not in ("", ".")
        ]
        if not scope_parts:
            if relative_scope.replace("\\", "/") not in (".", "./"):
                raise WorkspaceBoundaryError(f"Geçersiz scope: {relative_scope!r}")
            scope = self.root.resolve(strict=True)
        else:
            scope = resolve_within_workspace(
                self.root,
                relative_scope,
                reject_symlinks=True,
            )

        excluded = {name.casefold() for name in excluded_dirs}
        root = self.root.resolve(strict=True)

        def visit(current: Path) -> Iterator[str]:
            if current.is_symlink():
                return
            if current.is_file():
                yield current.relative_to(root).as_posix()
                return
            if not current.is_dir():
                return
            entries = sorted(current.iterdir(), key=lambda entry: entry.name.casefold())
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name.casefold() in excluded:
                        continue
                    yield from visit(entry)
                elif entry.is_file():
                    yield entry.relative_to(root).as_posix()

        yield from visit(scope)

    def _resolve(
        self,
        relative_path: str,
        *,
        resolve_final: bool = True,
        reject_symlinks: bool = False,
        allow_final_symlink: bool = False,
    ) -> Path:
        return resolve_within_workspace(
            self.root,
            relative_path,
            resolve_final=resolve_final,
            reject_symlinks=reject_symlinks,
            allow_final_symlink=allow_final_symlink,
        )

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.dispose()
