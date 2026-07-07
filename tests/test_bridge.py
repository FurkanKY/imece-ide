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
