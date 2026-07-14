"""runconfig.py testleri — komut sezgisi + .imece/run.json kalıcılığı."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runconfig  # noqa: E402


def test_file_command_by_extension():
    assert runconfig.file_command("src/main.py") == 'python "src/main.py"'
    assert runconfig.file_command("index.mjs") == 'node "index.mjs"'
    assert runconfig.file_command("notlar.md") is None


def test_detect_npm_dev_over_start(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"start": "x", "dev": "y"}}), encoding="utf-8")
    assert runconfig.detect_project_command(str(tmp_path)) == "npm run dev"


def test_detect_python_entry(tmp_path):
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    assert runconfig.detect_project_command(str(tmp_path)) == 'python "app.py"'
    (tmp_path / "main.py").write_text("", encoding="utf-8")
    assert runconfig.detect_project_command(str(tmp_path)) == 'python "main.py"'


def test_detect_none(tmp_path):
    assert runconfig.detect_project_command(str(tmp_path)) is None


def test_saved_command_wins_and_roundtrips(tmp_path):
    (tmp_path / "main.py").write_text("", encoding="utf-8")
    runconfig.save_project_command(str(tmp_path), "python -m paket --debug")
    assert runconfig.project_command(str(tmp_path)) == "python -m paket --debug"
    # dosya bozuksa sezgiye düşer
    (tmp_path / ".imece" / "run.json").write_text("{bozuk", encoding="utf-8")
    assert runconfig.project_command(str(tmp_path)) == 'python "main.py"'


def test_resolve_and_fingerprint_changes_when_command_changes(tmp_path):
    root = str(tmp_path)
    runconfig.save_project_command(root, "python main.py")
    info = runconfig.resolve(root)
    assert info == {"command": "python main.py", "source": "project_config"}
    before = runconfig.fingerprint(root, info["command"], info["source"])
    after = runconfig.fingerprint(root, "python other.py", info["source"])
    assert before != after
