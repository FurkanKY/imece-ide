"""Magent apply checkpoint'leri: Git'ten bağımsız dosya geri dönüş kaydı."""
from __future__ import annotations

import base64
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
        for rel in dict.fromkeys(paths):
            full = proj._safe(rel)
            exists = os.path.isfile(full)
            if exists:
                with open(full, "rb") as f:
                    raw = f.read()
            else:
                raw = b""
            files.append({"path": rel.replace("\\", "/"), "exists": exists,
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
                with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                    rec = json.load(f)
                out.append({"id": rec["id"], "ts": rec["ts"], "runId": rec.get("runId"),
                            "files": [x["path"] for x in rec["files"]]})
            return sorted(out, key=lambda x: x["ts"], reverse=True)
        except OSError:
            return []

    def restore(self, proj: Project, checkpoint_id: str) -> list[str]:
        with open(self._path(checkpoint_id), encoding="utf-8") as f:
            rec = json.load(f)
        restored = []
        for item in rec["files"]:
            full = proj._safe(item["path"])
            if item["exists"]:
                os.makedirs(os.path.dirname(full) or proj.root, exist_ok=True)
                with open(full, "wb") as f:
                    f.write(base64.b64decode(item["content"]))
            elif os.path.exists(full):
                os.unlink(full)
            restored.append(item["path"])
        return restored
