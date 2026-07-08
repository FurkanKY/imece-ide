"""
project.py
----------
Lokal bir proje klasörü üzerinde çalışmak için ARAÇLAR.

Ajanların bir projeyi "görmesi" için dosyaları listeleyip okumamız; sonra
üretilen değişiklikleri DIFF (fark) olarak gösterip onay üzerine yazmamız gerekir.
Tüm yol işlemleri proje kökünün DIŞINA çıkamaz (güvenlik).
"""

import os
import shutil
import difflib

# Taramada atlanacak klasör ve uzantılar (gürültüyü ve ikili dosyaları eler).
IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".idea", ".vscode", "output", ".mypy_cache",
}
IGNORE_EXT = {
    ".pyc", ".exe", ".dll", ".so", ".o", ".class", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz",
    ".mp4", ".mp3", ".woff", ".woff2", ".ttf",
}
MAX_READ_CHARS = 20000   # tek dosyadan okunacak azami karakter (context koruması)


class Project:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def _safe(self, rel: str) -> str:
        """rel yolunu köke göre çöz ve kökün dışına çıkmadığını doğrula."""
        p = os.path.abspath(os.path.join(self.root, rel))
        if os.path.commonpath([p, self.root]) != self.root:
            raise ValueError(f"Proje dışına çıkılamaz: {rel}")
        return p

    def list_files(self, max_files: int = 500) -> list[str]:
        """Projedeki (ilgili) dosyaların göreli yollarını döndürür."""
        out = []
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for f in sorted(files):
                if os.path.splitext(f)[1].lower() in IGNORE_EXT:
                    continue
                rel = os.path.relpath(os.path.join(dirpath, f), self.root)
                out.append(rel.replace("\\", "/"))
                if len(out) >= max_files:
                    return out
        return out

    def read_file(self, rel: str) -> str:
        with open(self._safe(rel), encoding="utf-8", errors="replace") as fh:
            return fh.read()[:MAX_READ_CHARS]

    def exists(self, rel: str) -> bool:
        return os.path.isfile(self._safe(rel))

    def make_diff(self, rel: str, new_content: str) -> str:
        """Mevcut içerik ile önerilen içeriğin unified diff'ini üretir."""
        old = self.read_file(rel) if self.exists(rel) else ""
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        )
        return "".join(diff)

    def apply(self, rel: str, new_content: str, backup: bool = True) -> None:
        """Değişikliği diske yaz. Var olan dosyayı önce .bak olarak yedekler."""
        p = self._safe(rel)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        if backup and os.path.isfile(p):
            shutil.copy2(p, p + ".bak")
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_content if new_content.endswith("\n") else new_content + "\n")

    # ---------------- dosya işlemleri (gezgin sağ-tık) ----------------
    def is_dir(self, rel: str) -> bool:
        return os.path.isdir(self._safe(rel))

    def create_file(self, rel: str) -> str:
        """Boş dosya oluştur. Zaten varsa hata. Döner: göreli yol."""
        p = self._safe(rel)
        if os.path.exists(p):
            raise FileExistsError(f"Zaten var: {rel}")
        os.makedirs(os.path.dirname(p) or self.root, exist_ok=True)
        with open(p, "w", encoding="utf-8"):
            pass
        return rel.replace("\\", "/")

    def create_folder(self, rel: str) -> str:
        p = self._safe(rel)
        if os.path.exists(p):
            raise FileExistsError(f"Zaten var: {rel}")
        os.makedirs(p)
        return rel.replace("\\", "/")

    def rename(self, rel: str, new_name: str) -> str:
        """Aynı klasörde yeniden adlandır. new_name yalnız ad (yol değil). Döner: yeni göreli yol."""
        if not new_name or "/" in new_name or "\\" in new_name:
            raise ValueError("Geçersiz ad.")
        src = self._safe(rel)
        if not os.path.exists(src):
            raise FileNotFoundError(rel)
        dst = os.path.join(os.path.dirname(src), new_name)
        self._safe(os.path.relpath(dst, self.root))   # hedef de kök içinde mi?
        if os.path.exists(dst):
            raise FileExistsError(f"Zaten var: {new_name}")
        os.rename(src, dst)
        return os.path.relpath(dst, self.root).replace("\\", "/")

    def move(self, rel: str, new_dir: str) -> str:
        """Dosya/klasörü başka klasöre taşı (ad korunur). Döner: yeni göreli yol.
        Klasörün kendi altına taşınması engellenir."""
        src = self._safe(rel)
        if not os.path.exists(src):
            raise FileNotFoundError(rel)
        dst_dir = self._safe(new_dir) if new_dir else self.root
        if not os.path.isdir(dst_dir):
            raise NotADirectoryError(new_dir)
        if os.path.isdir(src):
            common = os.path.commonpath([src, dst_dir])
            if common == src:
                raise ValueError("Klasör kendi altına taşınamaz.")
        dst = os.path.join(dst_dir, os.path.basename(src))
        if os.path.abspath(dst) == src:
            return rel.replace("\\", "/")
        if os.path.exists(dst):
            raise FileExistsError(f"Hedefte zaten var: {os.path.basename(src)}")
        os.rename(src, dst)
        return os.path.relpath(dst, self.root).replace("\\", "/")

    def delete(self, rel: str) -> None:
        """Dosya veya klasörü (özyinelemeli) sil."""
        p = self._safe(rel)
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.exists(p):
            os.remove(p)
        else:
            raise FileNotFoundError(rel)
