"""Checkpoint dosya geri dönüş testleri."""

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
