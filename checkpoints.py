"""Magent apply checkpoint'leri: Git'ten bağımsız dosya geri dönüş kaydı."""
from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
import time
import uuid

from project import Project


class CheckpointStore:
    def __init__(self, root: str):
        self.dir = os.path.join(root, ".magent", "checkpoints")

    def _path(self, checkpoint_id: str) -> str:
        if not checkpoint_id or any(c not in "0123456789abcdef-" for c in checkpoint_id):
            raise ValueError("Geçersiz checkpoint kimliği.")
        return os.path.join(self.dir, checkpoint_id + ".json")

    def create(self, proj: Project, paths: list[str], run_id: str | None = None) -> dict:
        files = []
        normalized = [rel.replace("\\", "/") for rel in paths]
        for rel in dict.fromkeys(normalized):
            full = proj._safe(rel)
            exists = os.path.isfile(full)
            if exists:
                with open(full, "rb") as f:
                    raw = f.read()
            else:
                raw = b""
            files.append({"path": rel, "exists": exists,
                          "content": base64.b64encode(raw).decode("ascii")})
        rec = {"id": str(uuid.uuid4()), "ts": time.time(), "runId": run_id, "files": files}
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".checkpoint-", suffix=".tmp", dir=self.dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path(rec["id"]))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return {"id": rec["id"], "ts": rec["ts"], "files": [f["path"] for f in files]}

    def list(self) -> list[dict]:
        try:
            out = []
            for name in os.listdir(self.dir):
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                        rec = json.load(f)
                    files = rec["files"]
                    if not isinstance(files, list):
                        continue
                    out.append({"id": rec["id"], "ts": rec["ts"], "runId": rec.get("runId"),
                                "files": [x["path"] for x in files]})
                except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                    # Tek bozuk kayıt tüm checkpoint geçmişini görünmez yapmasın.
                    continue
            return sorted(out, key=lambda x: x["ts"], reverse=True)
        except OSError:
            return []

    def restore(self, proj: Project, checkpoint_id: str) -> list[str]:
        with open(self._path(checkpoint_id), encoding="utf-8") as f:
            rec = json.load(f)

        prepared = []
        try:
            files = rec["files"]
            if not isinstance(files, list):
                raise ValueError("Checkpoint dosya listesi geçersiz.")
            for item in files:
                rel = item["path"]
                exists = item["exists"]
                if not isinstance(rel, str) or not isinstance(exists, bool):
                    raise ValueError("Checkpoint dosya kaydı geçersiz.")
                full = proj._safe(rel)
                raw = base64.b64decode(item["content"], validate=True)
                prepared.append((rel, full, exists, raw))
        except (TypeError, KeyError, binascii.Error) as e:
            raise ValueError("Checkpoint içeriği bozuk.") from e

        # Önce mevcut durumu bellekte tut; restore ortada hata verirse geri sar.
        before = []
        for rel, full, _exists, _raw in prepared:
            current_exists = os.path.isfile(full)
            current = b""
            if current_exists:
                with open(full, "rb") as f:
                    current = f.read()
            elif os.path.exists(full):
                raise IsADirectoryError(f"Dosya yolu klasöre dönüştü: {rel}")
            before.append((full, current_exists, current))

        try:
            for _rel, full, exists, raw in prepared:
                if exists:
                    self._write_atomic(full, raw)
                elif os.path.exists(full):
                    os.unlink(full)
        except Exception:
            for full, existed, raw in before:
                if existed:
                    self._write_atomic(full, raw)
                elif os.path.isfile(full):
                    os.unlink(full)
            raise
        return [rel for rel, _full, _exists, _raw in prepared]

    def drop(self, checkpoint_id: str) -> None:
        try:
            os.unlink(self._path(checkpoint_id))
        except FileNotFoundError:
            pass

    @staticmethod
    def _write_atomic(full: str, raw: bytes) -> None:
        parent = os.path.dirname(full)
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".magent-restore-", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, full)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
