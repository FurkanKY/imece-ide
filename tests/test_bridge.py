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
    ids = {p["id"] for p in r["result"]["providers"]}
    assert ids >= {"claude", "deepseek", "gemini", "anthropic"}
    routing = r["result"]["recommendedRouting"]
    assert set(routing) == {"planner", "coder", "reviewer"}
    # tek sağlayıcı üç role de atanır (bkz. providers.recommended_routing)
    assert len(set(routing.values())) == 1


def test_providers_list_contract(bridge, tmp_path, monkeypatch):
    import providers
    import webhost.api.providers  # noqa: F401 — handler kaydı
    monkeypatch.setattr(providers, "providers_config_path", lambda: tmp_path / "providers.json")
    r = rpc(bridge, "providers.list")
    assert r["ok"]
    by_id = {p["id"]: p for p in r["result"]["providers"]}
    assert by_id["deepseek"]["kind"] == "openai"
    assert by_id["claude"]["kind"] == "cli"
    assert "model" in by_id["gemini"] and "models" in by_id["gemini"]
    assert r["result"]["defaultRouting"]["planner"] == "claude"


def test_providers_custom_roundtrip(bridge, tmp_path, monkeypatch):
    import providers
    import webhost.api.providers  # noqa: F401
    monkeypatch.setattr(providers, "providers_config_path", lambda: tmp_path / "providers.json")
    r = rpc(bridge, "providers.addCustom", {
        "id": "sirket", "label": "Şirket LLM",
        "baseUrl": "https://llm.example.com/v1", "model": "ic-model",
    })
    assert r["ok"] and r["result"]["provider"]["custom"] is True
    r = rpc(bridge, "providers.addCustom", {"id": "sirket", "label": "x",
                                            "baseUrl": "https://a", "model": "m"})
    assert r["ok"] is False and r["error"]["code"] == "invalid"
    r = rpc(bridge, "providers.setModel", {"provider": "sirket", "model": "yeni-model"})
    assert r["ok"]
    r = rpc(bridge, "providers.removeCustom", {"provider": "sirket"})
    assert r["ok"]
    r = rpc(bridge, "providers.removeCustom", {"provider": "sirket"})
    assert r["ok"] is False and r["error"]["code"] == "unknown_provider"
    providers.refresh()


def test_keys_status_covers_catalog(bridge, tmp_path, monkeypatch):
    import providers
    import webhost.api.keys  # noqa: F401 — handler kaydı
    monkeypatch.setattr(providers, "providers_config_path", lambda: tmp_path / "providers.json")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-uzun-test-anahtari")  # gitleaks:allow
    r = rpc(bridge, "keys.status")
    assert r["ok"]
    provs = r["result"]["providers"]
    assert provs["deepseek"]["ok"] is True
    assert provs["deepseek"]["masked"].endswith(provs["deepseek"]["masked"][-4:])
    assert "sk-uzun" not in json.dumps(provs)  # anahtar asla köprüden dönmez
    assert provs["claude"]["kind"] == "cli"
    assert provs["anthropic"]["kind"] == "anthropic"
    r = rpc(bridge, "keys.test", {"provider": "boyle-biri-yok"})
    assert r["ok"] is False and r["error"]["code"] == "unknown_provider"


def test_keys_set_and_status_for_anthropic(bridge, tmp_path, monkeypatch):
    import os
    import providers
    import webhost.api.keys as keys_module
    monkeypatch.setattr(providers, "providers_config_path", lambda: tmp_path / "providers.json")
    monkeypatch.setattr(keys_module, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        r = rpc(bridge, "keys.status")
        assert r["result"]["providers"]["anthropic"]["ok"] is False

        r = rpc(bridge, "keys.set", {"anthropic": "sk-ant-gizli-uzun"})
        assert r["ok"]
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-gizli-uzun"

        r = rpc(bridge, "keys.status")
        assert r["result"]["providers"]["anthropic"]["ok"] is True
        assert r["result"]["providers"]["anthropic"]["masked"].endswith("uzun")
        assert "sk-ant-gizli-uzun" not in json.dumps(r["result"])
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


# ---------------- Jev (TypeSafe) karar sağlayıcısı anahtarı (S1b) ----------------

def test_keys_status_decision_providers_separate_from_catalog(bridge, tmp_path, monkeypatch):
    """Jev (TypeSafe) keys.status'ta AYRI decisionProviders sonucunda döner;
    normal sağlayıcı kataloğuna ve yönlendirmeye hiçbir zaman karışmaz."""
    import providers
    import webhost.api.keys as keys_module
    import webhost.api.providers  # noqa: F401 — handler kaydı
    monkeypatch.setattr(providers, "providers_config_path", lambda: tmp_path / "providers.json")
    monkeypatch.setattr(keys_module, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    r = rpc(bridge, "keys.status")
    assert r["ok"]
    res = r["result"]
    assert "typesafe" not in res["providers"]  # katalog listesi değişmedi
    dp = res["decisionProviders"]
    assert dp["typesafe"]["id"] == "typesafe"
    assert dp["typesafe"]["label"] == "Jev (TypeSafe)"
    assert dp["typesafe"]["ok"] is False          # anahtar kayıtlı değil
    assert dp["typesafe"]["masked"] == ""
    assert isinstance(dp["typesafe"]["sdkAvailable"], bool)  # SDK/bağlantı ayrı durum
    # yönlendirme kataloğu (providers.list) da TypeSafe'i İÇERMEZ
    r2 = rpc(bridge, "providers.list", call_id=2)
    assert r2["ok"]
    assert "typesafe" not in {p["id"] for p in r2["result"]["providers"]}


def test_keys_set_typesafe_validates_and_stores(bridge, tmp_path, monkeypatch):
    """typesafe → TYPESAFE_API_KEY; satır enjeksiyonu/boşluk İÇEREN anahtar
    reddedilir — dosyaya yazılmaz, os.environ güncellenmez, değer hata
    metnine asla yansımaz."""
    import os
    import providers
    import webhost.api.keys as keys_module
    monkeypatch.setattr(providers, "providers_config_path", lambda: tmp_path / "providers.json")
    monkeypatch.setattr(keys_module, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    try:
        r = rpc(bridge, "keys.set", {"typesafe": "ts-gizli-uzun"})
        assert r["ok"]
        assert os.environ["TYPESAFE_API_KEY"] == "ts-gizli-uzun"
        assert "TYPESAFE_API_KEY=ts-gizli-uzun" in env_file.read_text(encoding="utf-8")

        r = rpc(bridge, "keys.status", call_id=2)
        dp = r["result"]["decisionProviders"]["typesafe"]
        assert dp["ok"] is True
        assert dp["masked"].endswith("uzun")
        assert "ts-gizli-uzun" not in json.dumps(r["result"])  # anahtar köprüden dönmez
        assert "typesafe" not in r["result"]["providers"]

        content_before = env_file.read_text(encoding="utf-8")
        r = rpc(bridge, "keys.set", {"typesafe": "ts-yeni-deger\nEKSTRA=1"}, call_id=3)
        assert r["ok"] is False and r["error"]["code"] == "invalid_key"
        assert "ts-yeni-deger" not in r["error"]["message"] and "EKSTRA" not in r["error"]["message"]
        assert env_file.read_text(encoding="utf-8") == content_before  # dosyaya YAZILMADI
        assert os.environ["TYPESAFE_API_KEY"] == "ts-gizli-uzun"       # env güncellenmedi
    finally:
        os.environ.pop("TYPESAFE_API_KEY", None)


def test_keys_test_routes_typesafe_to_decision_backend(bridge, tmp_path, monkeypatch):
    """keys.test 'typesafe' çağrısını decision_credentials backend'ine yöneltir;
    normal sağlayıcılar providers.test_provider'da kalır."""
    import providers
    import decision_credentials
    import webhost.api.keys as keys_module  # noqa: F401 — handler kaydı
    monkeypatch.setattr(providers, "providers_config_path", lambda: tmp_path / "providers.json")
    calls: list[tuple] = []

    def fake_decision_test(provider_id, api_key):
        calls.append((provider_id, api_key))
        return {"ok": True, "code": "", "detail": "sentinel-jev"}

    monkeypatch.setattr(decision_credentials, "test_provider", fake_decision_test)
    r = rpc(bridge, "keys.test", {"provider": "typesafe", "key": "ts-anahtar"})
    assert r["ok"] and r["result"]["detail"] == "sentinel-jev"
    assert calls == [("typesafe", "ts-anahtar")]

    def fake_provider_test(provider_id, api_key):
        if providers.get(provider_id) is None:
            raise ValueError(f"Test edilemeyen sağlayıcı: {provider_id}")
        calls.append((provider_id, api_key))
        return {"ok": False, "code": "network", "detail": "sentinel-provider"}

    monkeypatch.setattr(providers, "test_provider", fake_provider_test)
    r = rpc(bridge, "keys.test", {"provider": "deepseek"}, call_id=2)
    assert r["ok"] and r["result"]["detail"] == "sentinel-provider"
    r = rpc(bridge, "keys.test", {"provider": "boyle-biri-yok"}, call_id=3)
    assert r["ok"] is False and r["error"]["code"] == "unknown_provider"


def test_keys_status_typesafe_packaged_store_migration(bridge, tmp_path, monkeypatch):
    """Paketli depo geçişi (düz .env → şifreli) TYPESAFE_API_KEY'i de taşır."""
    import os
    import providers
    import webhost.api.keys as keys_module
    monkeypatch.setattr(providers, "providers_config_path", lambda: tmp_path / "providers.json")
    monkeypatch.setattr(keys_module, "ENV_PATH", tmp_path / ".env")

    class _FakeStore:
        def __init__(self):
            self.data: dict = {}
            self.saved: list[dict] = []

        def load(self):
            return dict(self.data)

        def save(self, updates):
            self.saved.append(dict(updates))
            self.data.update(updates)

    fake = _FakeStore()
    monkeypatch.setattr(keys_module, "packaged_store", lambda: fake)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-migrate-1234")
    try:
        r = rpc(bridge, "keys.status")
        assert r["ok"]
        dp = r["result"]["decisionProviders"]["typesafe"]
        assert dp["ok"] is True
        assert dp["masked"].endswith("1234")
        assert fake.saved and fake.saved[0].get("TYPESAFE_API_KEY") == "ts-migrate-1234"
        assert "ts-migrate-1234" not in json.dumps(r["result"])
    finally:
        os.environ.pop("TYPESAFE_API_KEY", None)


def test_typesafe_test_provider_sanitized_via_missing_sdk(bridge, tmp_path, monkeypatch):
    """SDK yok: köprüden net, yerel, eyleme dönük Türkçe mesaj — ağ yok.
    (Gerçek SDK sınıf hataları — 200/401/429/timeout/close — gerçek SDK ile
    tests/test_keys.py'de httpx2.MockTransport üzerinden sınanır.)"""
    import providers
    monkeypatch.setattr(providers, "providers_config_path", lambda: tmp_path / "providers.json")
    import decision_credentials

    def _raise():
        raise ImportError("no typesafe_sdk")

    monkeypatch.setattr(decision_credentials, "_import_typesafe_sdk", _raise)
    r = rpc(bridge, "keys.test", {"provider": "typesafe", "key": "ts-gizli-uzun"})
    assert r["ok"]
    assert r["result"]["ok"] is False and r["result"]["code"] == "sdk_missing"
    assert "requirements-jev" in r["result"]["detail"]
    assert "ts-gizli-uzun" not in r["result"]["detail"]


def test_typesafe_test_provider_no_key_via_bridge(bridge, tmp_path, monkeypatch):
    """Köprü üzerinden anahtarsız 'sına' → no_key; ağa çıkmaz, dosya yazmaz.
    Anahtar yolu istemci açmadan döner — SDK seam asla çağrılmamalı."""
    import providers
    import decision_credentials
    monkeypatch.setattr(providers, "providers_config_path", lambda: tmp_path / "providers.json")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(decision_credentials, "_import_typesafe_sdk",
                        lambda: pytest.fail("anahtar yokken SDK yüklenmemeli"))
    r = rpc(bridge, "keys.test", {"provider": "typesafe"})
    assert r["ok"]
    assert r["result"] == {"ok": False, "code": "no_key", "detail": "Önce bir anahtar kaydedin."}
    assert not (tmp_path / ".env").exists()


def test_settings_routing_and_ai_engine_persist_roundtrip(bridge, tmp_path, monkeypatch):
    """Composer'ın routing seçimi ve 'AI motoru' tercihi ui_prefs.json'a yazılıp
    aynı biçimde geri okunmalı (bkz. ui_prefs.DEFAULTS: 'routing'/'ai_engine')."""
    import ui_prefs
    import webhost.api.settings  # noqa: F401 — handler kaydı
    monkeypatch.setattr(ui_prefs, "_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(ui_prefs, "_DIR", str(tmp_path))

    r = rpc(bridge, "settings.get")
    assert r["result"]["routing"] is None
    assert r["result"]["aiEngine"] == "auto"

    routing = {"planner": "anthropic", "coder": "anthropic", "reviewer": "anthropic"}
    r = rpc(bridge, "settings.set", {**r["result"], "routing": routing, "aiEngine": "legacy"})
    assert r["ok"]

    r = rpc(bridge, "settings.get")
    assert r["result"]["routing"] == routing
    assert r["result"]["aiEngine"] == "legacy"


def test_settings_decision_layer_persists_roundtrip(bridge, tmp_path, monkeypatch):
    """"Karar katmanı" tercihi (off | rules | jev) de aynı ui_prefs.json'a
    yazılıp geri okunmalı (bkz. ui_prefs.DEFAULTS: 'decision_layer')."""
    import ui_prefs
    import webhost.api.settings  # noqa: F401 — handler kaydı
    monkeypatch.setattr(ui_prefs, "_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(ui_prefs, "_DIR", str(tmp_path))

    r = rpc(bridge, "settings.get")
    assert r["result"]["decisionLayer"] == "off"

    r = rpc(bridge, "settings.set", {**r["result"], "decisionLayer": "rules"})
    assert r["ok"]

    r = rpc(bridge, "settings.get")
    assert r["result"]["decisionLayer"] == "rules"


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


# ---------------- debug domain'i (oturumsuz sözleşme; tam akış test_dap.py'de) ----------------

def test_debug_contract_without_session(bridge, tmp_path):
    import webhost.api.debug  # noqa: F401
    from webhost import state
    state._active = None
    r = rpc(bridge, "debug.start", {"rel": "a.py"})
    assert r["ok"] is False and r["error"]["code"] == "no_project"

    state.set_project(str(tmp_path))
    # python olmayan dosya reddedilir
    r = rpc(bridge, "debug.start", {"rel": "a.txt"}, call_id=2)
    assert r["ok"] is False and r["error"]["code"] == "not_python"
    # oturum yokken durum/adım sözleşmesi
    r = rpc(bridge, "debug.status", call_id=3)
    assert r["ok"] and r["result"] == {"active": False, "stopped": False}
    r = rpc(bridge, "debug.continue", call_id=4)
    assert r["ok"] is False and r["error"]["code"] == "not_stopped"
    # oturum yokken breakpoint çağrısı yereldekini aynen döndürür
    r = rpc(bridge, "debug.setBreakpoints", {"path": "a.py", "lines": [3, 7]}, call_id=5)
    assert r["ok"] and r["result"]["lines"] == [3, 7]
    # stop oturumsuz da güvenli
    r = rpc(bridge, "debug.stop", call_id=6)
    assert r["ok"]
    state._active = None


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
