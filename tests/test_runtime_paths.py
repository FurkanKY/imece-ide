"""Kaynak/paketli çalışma yol sözleşmesi."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime_paths  # noqa: E402


def test_source_mode_keeps_legacy_paths(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert runtime_paths.resource_root() == runtime_paths.SOURCE_ROOT
    assert runtime_paths.env_path() == runtime_paths.SOURCE_ROOT / ".env"
    assert runtime_paths.prefs_path() == Path.home() / ".multi_agent_ide" / "prefs.json"


def test_frozen_mode_separates_resources_and_writable_data(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle" / "_internal"
    local = tmp_path / "local"
    exe = tmp_path / "bundle" / "ImeceIDE.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    data = local / "ImeceIDE"
    assert runtime_paths.resource_path("web", "ui", "dist") == bundle / "web" / "ui" / "dist"
    assert runtime_paths.env_path() == data / ".env"
    assert runtime_paths.prefs_path() == data / "prefs.json"
    assert runtime_paths.log_path() == data / "logs" / "app.log"


def test_frozen_helper_resolves_next_to_executable(tmp_path, monkeypatch):
    suffix = ".exe" if os.name == "nt" else ""
    exe = tmp_path / f"ImeceIDE{suffix}"
    helper = tmp_path / f"imece-helper{suffix}"
    helper.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert runtime_paths.helper_executable("imece-helper") == helper
