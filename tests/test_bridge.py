"""
test_bridge.py — köprü sözleşme testleri (webview'suz, headless).

HostBridge.call() doğrudan sürülür; reply sinyali yakalanıp JSON zarfı doğrulanır.
Domain handler'ları büyüdükçe buraya golden testler eklenir (plan §4.4).

Çalıştırma:  python -m pytest tests/test_bridge.py -q
"""

import json

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

import ui_prefs  # noqa: E402
from webhost.bridge import HostBridge, handler, BridgeError  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture()
def bridge(qapp):
    return HostBridge()


def rpc(bridge, method, params=None, call_id=1):
    """call() sür, reply zarfını yakala."""
    out = []
    bridge.reply.connect(lambda raw: out.append(json.loads(raw)))
    bridge.call(json.dumps({"id": call_id, "method": method, "params": params or {}}))
    assert out, f"{method}: yanıt gelmedi"
    return out[-1]


# ---------------- dispatcher mekaniği ----------------

def test_unknown_method(bridge):
    r = rpc(bridge, "yok.boyle.metot")
    assert r["ok"] is False
    assert r["error"]["code"] == "unknown_method"
    assert r["id"] == 1


def test_handler_result_roundtrip(bridge):
    @handler("test.echo")
    def _echo(params, ctx):
        return {"got": params.get("x")}

    r = rpc(bridge, "test.echo", {"x": "merhaba"}, call_id=7)
    assert r == {"id": 7, "ok": True, "result": {"got": "merhaba"}}


def test_handler_bridge_error(bridge):
    @handler("test.kizil")
    def _fail(params, ctx):
        raise BridgeError("not_found", "Yol bulunamadı.")

    r = rpc(bridge, "test.kizil")
    assert r["ok"] is False
    assert r["error"] == {"code": "not_found", "message": "Yol bulunamadı."}


def test_handler_crash_becomes_internal_error(bridge):
    @handler("test.coker")
    def _crash(params, ctx):
        raise RuntimeError("patladı")

    r = rpc(bridge, "test.coker")
    assert r["ok"] is False
    assert r["error"]["code"] == "internal"
    assert "patladı" in r["error"]["message"]


def test_broken_envelope_is_ignored(bridge):
    bridge.call("bu json değil")  # exception fırlatmamalı
    bridge.call(json.dumps({"method": "id.yok"}))


def test_event_envelope(bridge):
    out = []
    bridge.event.connect(lambda raw: out.append(json.loads(raw)))
    bridge.emit_event("test.kanal", {"a": 1})
    assert out == [{"channel": "test.kanal", "payload": {"a": 1}}]


# ---------------- settings domain'i ----------------

def test_run_providers(bridge):
    import webhost.api.run  # noqa: F401 — handler kaydı
    r = rpc(bridge, "run.providers")
    assert r["ok"]
    assert set(r["result"]["providers"]) >= {"claude", "deepseek", "gemini"}
    assert r["result"]["defaultRouting"]["coder"] == "deepseek"


def test_run_and_history_require_project(bridge):
    import webhost.api.run      # noqa: F401
    import webhost.api.history  # noqa: F401
    from webhost import state
    state._active = None  # projeyi sıfırla
    r = rpc(bridge, "run.start", {"task": "x"})
    assert r["ok"] is False and r["error"]["code"] == "no_project"
    r = rpc(bridge, "history.list")
    assert r["ok"] is False and r["error"]["code"] == "no_project"


# ---------------- scm domain'i (geçici git deposuyla uçtan uca) ----------------

import shutil  # noqa: E402
import subprocess  # noqa: E402


@pytest.fixture()
def git_repo(tmp_path):
    """1 commit'li mini git deposu; aktif proje olarak ayarlanır."""
    if shutil.which("git") is None:
        pytest.skip("git yok")
    def g(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL,
                       encoding="utf-8", errors="replace")
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (tmp_path / "a.py").write_text("eski = 1\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "ilk")
    from webhost import state
    state.set_project(str(tmp_path))
    yield tmp_path
    state._active = None


def test_fs_move(bridge, tmp_path):
    import webhost.api.fs  # noqa: F401
    from webhost import state
    state.set_project(str(tmp_path))
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    r = rpc(bridge, "fs.move", {"rel": "a.txt", "newDir": "sub"})
    assert r["ok"] and r["result"]["rel"] == "sub/a.txt"
    assert (tmp_path / "sub" / "a.txt").exists()
    # klasör kendi altına taşınamaz
    (tmp_path / "d1" / "d2").mkdir(parents=True)
    r = rpc(bridge, "fs.move", {"rel": "d1", "newDir": "d1/d2"}, call_id=2)
    assert r["ok"] is False
    # hedefte aynı ad varsa hata
    (tmp_path / "b.txt").write_text("y", encoding="utf-8")
    (tmp_path / "sub" / "b.txt").write_text("z", encoding="utf-8")
    r = rpc(bridge, "fs.move", {"rel": "b.txt", "newDir": "sub"}, call_id=3)
    assert r["ok"] is False
    state._active = None


def test_scm_not_a_repo(bridge, tmp_path):
    import webhost.api.scm  # noqa: F401
    from webhost import state
    state.set_project(str(tmp_path))
    r = rpc(bridge, "scm.status")
    assert r["ok"] and r["result"]["isRepo"] is False
    state._active = None


def test_scm_status_stage_commit_flow(bridge, git_repo):
    import webhost.api.scm  # noqa: F401
    # değişiklik + izlenmeyen dosya
    (git_repo / "a.py").write_text("yeni = 2\n", encoding="utf-8")
    (git_repo / "b.py").write_text("b = 1\n", encoding="utf-8")

    r = rpc(bridge, "scm.status")
    assert r["ok"] and r["result"]["isRepo"] and r["result"]["branch"] == "main"
    un = {c["path"]: c["status"] for c in r["result"]["unstaged"]}
    assert un == {"a.py": "M", "b.py": "U"}
    assert r["result"]["staged"] == []

    # diff (çalışma ağacı): orijinal HEAD/indeks, yeni worktree
    r = rpc(bridge, "scm.diff", {"path": "a.py"}, call_id=2)
    assert r["ok"]
    assert r["result"]["original"] == "eski = 1\n"
    assert r["result"]["modified"] == "yeni = 2\n"
    # izlenmeyen dosya: orijinal boş
    r = rpc(bridge, "scm.diff", {"path": "b.py"}, call_id=3)
    assert r["ok"] and r["result"]["original"] == "" and r["result"]["modified"] == "b = 1\n"

    # stage → staged'e taşınır (U → A)
    r = rpc(bridge, "scm.stage", {"paths": ["a.py", "b.py"]}, call_id=4)
    assert r["ok"]
    r = rpc(bridge, "scm.status", call_id=5)
    st = {c["path"]: c["status"] for c in r["result"]["staged"]}
    assert st == {"a.py": "M", "b.py": "A"}
    assert r["result"]["unstaged"] == []

    # staged diff: HEAD ↔ indeks
    r = rpc(bridge, "scm.diff", {"path": "a.py", "staged": True}, call_id=6)
    assert r["ok"] and r["result"]["original"] == "eski = 1\n"

    # unstage b.py → tekrar U
    r = rpc(bridge, "scm.unstage", {"paths": ["b.py"]}, call_id=7)
    assert r["ok"]
    r = rpc(bridge, "scm.status", call_id=8)
    assert [c["path"] for c in r["result"]["staged"]] == ["a.py"]
    assert {c["path"]: c["status"] for c in r["result"]["unstaged"]} == {"b.py": "U"}

    # commit → temiz staged; boş mesaj reddedilir
    r = rpc(bridge, "scm.commit", {"message": ""}, call_id=9)
    assert r["ok"] is False and r["error"]["code"] == "bad_request"
    r = rpc(bridge, "scm.commit", {"message": "değişiklik: a.py"}, call_id=10)
    assert r["ok"] and r["result"]["summary"]
    r = rpc(bridge, "scm.status", call_id=11)
    assert r["result"]["staged"] == []

    # discard: b.py izlenmiyor → silinir
    r = rpc(bridge, "scm.discard", {"path": "b.py", "untracked": True}, call_id=12)
    assert r["ok"]
    assert not (git_repo / "b.py").exists()
    # tracked discard: a.py'yi boz, geri al
    (git_repo / "a.py").write_text("bozuk\n", encoding="utf-8")
    r = rpc(bridge, "scm.discard", {"path": "a.py"}, call_id=13)
    assert r["ok"]
    assert (git_repo / "a.py").read_text(encoding="utf-8") == "yeni = 2\n"


def test_settings_roundtrip(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(ui_prefs, "_DIR", str(tmp_path))
    monkeypatch.setattr(ui_prefs, "_PATH", str(tmp_path / "prefs.json"))
    import webhost.api.settings  # noqa: F401 — handler kaydı

    r = rpc(bridge, "settings.get")
    assert r["ok"] and r["result"]["accent"] == "blue"
    assert r["result"]["enterToSend"] is True  # camelCase dönüşümü

    r = rpc(bridge, "settings.set", {
        "accent": "violet", "enterToSend": False,
        "recentProjects": [{"path": "C:/p", "name": "p", "lastOpened": "2026-07-05"}],
    }, call_id=2)
    assert r["ok"]

    r = rpc(bridge, "settings.get", call_id=3)
    assert r["result"]["accent"] == "violet"
    assert r["result"]["enterToSend"] is False
    assert r["result"]["recentProjects"][0]["name"] == "p"
    # verilmeyen alanlar korunur (merge semantiği)
    assert r["result"]["density"] == "comfortable"
