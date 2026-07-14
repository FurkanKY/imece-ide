"""Checkpoint dosya geri dönüş testleri."""

import json
import os

import pytest

from checkpoints import CheckpointStore
from project import Project


def test_restore_returns_existing_and_new_file_to_original_state(tmp_path):
    (tmp_path / "a.py").write_text("before\n", encoding="utf-8")
    proj = Project(str(tmp_path))
    store = CheckpointStore(str(tmp_path))
    checkpoint = store.create(proj, ["a.py", "new.py"], "1")

    proj.apply("a.py", "after\n", backup=False)
    proj.apply("new.py", "created\n", backup=False)
    assert store.restore(proj, checkpoint["id"]) == ["a.py", "new.py"]

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "before\n"
    assert not (tmp_path / "new.py").exists()


def test_checkpoint_list_is_newest_first(tmp_path):
    proj = Project(str(tmp_path))
    store = CheckpointStore(str(tmp_path))
    first = store.create(proj, ["a.py"])
    second = store.create(proj, ["b.py"])
    assert [x["id"] for x in store.list()] == [second["id"], first["id"]]


def test_corrupt_checkpoint_does_not_modify_files(tmp_path):
    (tmp_path / "a.py").write_text("before\n", encoding="utf-8")
    proj = Project(str(tmp_path))
    store = CheckpointStore(str(tmp_path))
    checkpoint = store.create(proj, ["a.py"])
    (tmp_path / "a.py").write_text("current\n", encoding="utf-8")

    path = store._path(checkpoint["id"])
    record = json.loads(open(path, encoding="utf-8").read())
    record["files"][0]["content"] = "not-base64"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f)

    with pytest.raises(ValueError):
        store.restore(proj, checkpoint["id"])
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "current\n"


def test_restore_rejects_path_escape_before_writing(tmp_path):
    (tmp_path / "a.py").write_text("before\n", encoding="utf-8")
    proj = Project(str(tmp_path))
    store = CheckpointStore(str(tmp_path))
    checkpoint = store.create(proj, ["a.py"])
    (tmp_path / "a.py").write_text("current\n", encoding="utf-8")

    path = store._path(checkpoint["id"])
    record = json.loads(open(path, encoding="utf-8").read())
    record["files"][0]["path"] = "../outside.py"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f)

    with pytest.raises(ValueError):
        store.restore(proj, checkpoint["id"])
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "current\n"


def test_list_skips_a_single_malformed_checkpoint(tmp_path):
    proj = Project(str(tmp_path))
    store = CheckpointStore(str(tmp_path))
    valid = store.create(proj, ["a.py"])
    (tmp_path / ".imece" / "checkpoints" / "broken.json").write_text(
        "{broken", encoding="utf-8",
    )
    assert [x["id"] for x in store.list()] == [valid["id"]]


def test_restore_rolls_back_if_a_later_file_write_fails(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("checkpoint-a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("checkpoint-b\n", encoding="utf-8")
    proj = Project(str(tmp_path))
    store = CheckpointStore(str(tmp_path))
    checkpoint = store.create(proj, ["a.py", "b.py"])
    (tmp_path / "a.py").write_text("current-a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("current-b\n", encoding="utf-8")

    real_replace = os.replace
    calls = 0

    def fail_second_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk write failed")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_second_replace)
    with pytest.raises(OSError):
        store.restore(proj, checkpoint["id"])

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "current-a\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "current-b\n"
