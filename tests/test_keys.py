"""keys.py .env yazıcısı testleri — yorum/bilinmeyen satır korunur, anahtar güncellenir.

S1b ekleri: Jev (TypeSafe) karar sağlayıcısı anahtarı — id→env eşlemesi ve
yazdırılabilir/boşluksuz değer doğrulaması (satır enjeksiyonu önlenir).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import decision_credentials  # noqa: E402
from webhost.api.keys import write_env, clear_env_keys, _mask, _key_vars  # noqa: E402


def test_write_env_updates_existing_and_preserves_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# yorum satırı\nDEEPSEEK_API_KEY=eski\nGEMINI_MODEL=gemini-2.5-flash\n",
                 encoding="utf-8")
    write_env(p, {"DEEPSEEK_API_KEY": "yeni-123"})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# yorum satırı"
    assert "DEEPSEEK_API_KEY=yeni-123" in lines
    assert "GEMINI_MODEL=gemini-2.5-flash" in lines
    assert "eski" not in p.read_text(encoding="utf-8")


def test_write_env_appends_missing_and_creates_file(tmp_path):
    p = tmp_path / ".env"
    write_env(p, {"GEMINI_API_KEY": "abc"})
    assert p.read_text(encoding="utf-8") == "GEMINI_API_KEY=abc\n"
    write_env(p, {"DEEPSEEK_API_KEY": "xyz"})
    content = p.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=abc" in content and "DEEPSEEK_API_KEY=xyz" in content


def test_mask_never_leaks_short_keys():
    assert _mask("kisa") == "••••"
    assert _mask("cok-uzun-anahtar-1234").endswith("1234")
    assert "cok-uzun" not in _mask("cok-uzun-anahtar-1234")


def test_clear_env_keys_keeps_models_and_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# sakla\nDEEPSEEK_API_KEY=secret\nDEEPSEEK_MODEL=deepseek-chat\n", encoding="utf-8")
    clear_env_keys(p, {"DEEPSEEK_API_KEY"})
    assert p.read_text(encoding="utf-8") == "# sakla\nDEEPSEEK_API_KEY=\nDEEPSEEK_MODEL=deepseek-chat\n"


# ---------------- Jev (TypeSafe) karar sağlayıcısı (S1b) ----------------

def test_key_vars_includes_typesafe(tmp_path, monkeypatch):
    """typesafe → TYPESAFE_API_KEY eşlemesi keys.set kabulüne girer; id normal
    katalogda DEĞİLDİR (katalog testleri bkz. test_bridge.py)."""
    import providers
    monkeypatch.setattr(providers, "providers_config_path", lambda: tmp_path / "providers.json")
    vars_ = _key_vars()
    assert vars_["typesafe"] == "TYPESAFE_API_KEY"
    assert "typesafe" not in {e["id"] for e in providers.catalog()}


def test_write_env_typesafe_roundtrip(tmp_path):
    p = tmp_path / ".env"
    write_env(p, {"TYPESAFE_API_KEY": "ts-ok-1234"})
    assert "TYPESAFE_API_KEY=ts-ok-1234" in p.read_text(encoding="utf-8")


def test_validate_key_accepts_clean_value():
    assert decision_credentials.validate_key(" ts-abc-123 ") == "ts-abc-123"
    assert decision_credentials.validate_key("sk-or-v1-ABCdef012_-.~") == "sk-or-v1-ABCdef012_-.~"


@pytest.mark.parametrize(
    "bad",
    [
        "ts-abc\nTYPESAFE_API_KEY=evil",  # satır enjeksiyonu
        "ts abc", "ts\ttab", "  ", "",  # boşluk/satır sonu
        "ts-key\x00null",  # yazdırılamaz kontrol karakteri (ASCII)
        "ts-ünïcødé",  # ASCII dışı — SDK da reddeder
        'ts-key"quoted"', "ts-'single'",  # tırnaklar
        "ts-key#comment",  # .env yorum başlatır
        "ts-key$HOME", "ts-key${EXPAND}",  # dotenv değişken genişlemesi
        "ts-key&cmd", "ts-key;cmd", "ts-key|cmd",  # kabuk bozan
        "ts-key<danger>", "ts-key%var%", "ts-key\\escape", "ts-key`tick",
    ],
)
def test_validate_key_rejects_injection_and_whitespace(bad):
    with pytest.raises(ValueError):
        decision_credentials.validate_key(bad)


def test_validate_key_rejects_non_string_and_never_echoes_value():
    with pytest.raises(ValueError):
        decision_credentials.validate_key(123)  # type: ignore[arg-type]
    try:
        decision_credentials.validate_key("ts-gizli-deger\nEKSTRA=1")
    except ValueError as e:
        # hata metni anahtar değerini ASLA içermez
        assert "ts-gizli-deger" not in str(e)
        assert "EKSTRA" not in str(e)
    else:
        pytest.fail("ValueError bekleniyordu")


# ---------------------------------------------------------------------------
# GERÇEK typesafe-sdk 0.7.2 ile ÇEVRİMDIŞI bağlantı testi
# (httpx2.MockTransport — hiçbir canlı çağrı yok).
#
# SDK isteğe bağlıdır (pip install -r requirements-jev.txt): kurulu değilse
# bu bölüm importorskip ile ATLANIR — sıradan test koşusu etkilenmez.
# Dış dizin/sys.path enjeksiyonu YOKTUR.
# ---------------------------------------------------------------------------


@pytest.fixture()
def offline_transport(monkeypatch):
    """httpx2.MockTransport enjeksiyonu — test_provider hiç ağa çıkmaz."""
    pytest.importorskip("typesafe_sdk")
    pytest.importorskip("httpx2")
    import httpx2

    def install(handler):
        monkeypatch.setattr(
            decision_credentials, "_transport_factory",
            lambda: httpx2.MockTransport(handler),
        )

    return install


@pytest.fixture()
def close_spy(monkeypatch):
    """aclose() çağrısını kaydeder (gerçek aclose yine çalışır)."""
    calls: list[bool] = []
    original = decision_credentials._aclose_quietly

    async def spy(client):
        calls.append(True)
        await original(client)

    monkeypatch.setattr(decision_credentials, "_aclose_quietly", spy)
    return calls


def test_real_sdk_import_seam_and_availability():
    """sdk_available() gerçek SDK'yı bulur; seam (jev_backend) modülü döndürür
    ve gövde-log filtresi kurulu olur (SDK DEBUG gövde kayıtları düşürülür)."""
    pytest.importorskip("typesafe_sdk")
    import logging

    assert decision_credentials.sdk_available() is True
    sdk = decision_credentials._import_typesafe_sdk()
    assert sdk.TypeSafeClient is not None
    assert sdk.RetryPolicy is not None
    sdk_logger = logging.getLogger("typesafe_sdk")
    assert any(
        isinstance(f, logging.Filter) and type(f).__name__ == "_SdkWireBodyLogFilter"
        for f in sdk_logger.filters
    )


def test_real_sdk_models_list_request_contract(offline_transport, close_spy):
    """GET {sabit base_url}/v1/models, Bearer anahtarı, TEK çağrı
    (max_retries=0), ok sonuç, close garantili."""
    pytest.importorskip("typesafe_sdk")
    import httpx2

    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={"models": [{"name": "jev-latest", "description": "d", "release_date": "2026-09-01"}]},
        )

    offline_transport(handler)
    r = decision_credentials.test_provider("typesafe", "ts-gizli-uzun")
    assert r == {"ok": True, "code": "", "detail": ""}
    assert close_spy == [True]
    assert len(seen) == 1  # max_retries=0 — tek deneme
    assert seen[0].method == "GET"
    assert str(seen[0].url) == "https://api.typesafe.ai/v1/models"  # SABİT uç
    assert seen[0].headers.get("Authorization") == "Bearer ts-gizli-uzun"


def test_real_sdk_401_sanitized_no_body_leak(offline_transport, close_spy):
    pytest.importorskip("typesafe_sdk")
    import httpx2

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"detail": "HAM GÖVDE ts-gizli-uzun sızma"})

    offline_transport(handler)
    r = decision_credentials.test_provider("typesafe", "ts-gizli-uzun")
    assert r == {"ok": False, "code": "auth", "detail": "Anahtar reddedildi (401/403)."}
    assert close_spy == [True]  # hata durumunda da close


def test_real_sdk_429_sanitized(offline_transport, close_spy):
    pytest.importorskip("typesafe_sdk")
    import httpx2

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json={"detail": "HAM GÖVDE"}, headers={"Retry-After": "999"})

    offline_transport(handler)
    r = decision_credentials.test_provider("typesafe", "ts-gizli-uzun")
    assert r["ok"] is False and r["code"] == "http"
    assert "429" in r["detail"] and "999" not in r["detail"]
    assert "HAM GÖVDE" not in r["detail"]
    assert close_spy == [True]


def test_real_sdk_500_single_attempt_sanitized(offline_transport, close_spy):
    """500 → TypeSafeAPIError; max_retries=0 sayesinde TEK çağrı; sabit metin."""
    pytest.importorskip("typesafe_sdk")
    import httpx2

    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(500, json={"error": "HAM SUNUCU METNİ"})

    offline_transport(handler)
    r = decision_credentials.test_provider("typesafe", "ts-gizli-uzun")
    assert len(seen) == 1  # yeniden deneme yok
    assert r["ok"] is False and r["code"] == "http"
    assert r["detail"] == "Beklenmeyen yanıt: HTTP 500"
    assert "HAM SUNUCU" not in r["detail"]
    assert close_spy == [True]


def test_real_sdk_read_timeout_sanitized(offline_transport, close_spy):
    """Okuma zaman aşımı → TypeSafeAPITimeoutError → network, sabit metin."""
    pytest.importorskip("typesafe_sdk")
    import httpx2

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("read timed out")

    offline_transport(handler)
    r = decision_credentials.test_provider("typesafe", "ts-gizli-uzun")
    assert r == {"ok": False, "code": "network", "detail": "Bağlantı zaman aşımına uğradı."}
    assert close_spy == [True]


def test_real_sdk_connection_error_sanitized(offline_transport, close_spy):
    pytest.importorskip("typesafe_sdk")
    import httpx2

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("bağlantı yok")

    offline_transport(handler)
    r = decision_credentials.test_provider("typesafe", "ts-gizli-uzun")
    assert r == {"ok": False, "code": "network", "detail": "Bağlantı kurulamadı."}
    assert close_spy == [True]


def test_validate_key_aligns_with_sdk_rule():
    """SDK kendi kuralıyla aynı: boşluksuz printable ASCII kabul; boşluk/
    ASCII dışı reddet. validate_key GEÇERLİ anahtarı SDK yapıcısına verir —
    istemci (çevrimdışı) kurulur, TypeSafeError YOK."""
    pytest.importorskip("typesafe_sdk")
    key = decision_credentials.validate_key(" ts-official-ABC012_-.~ ")
    sdk = decision_credentials._import_typesafe_sdk()
    client = sdk.TypeSafeClient(api_key=key)  # ağ yok — yalnız yapım
    client.close()
    for bad in ("boşluk anahtar", "ts-ünïcødé", "ts-key\ttab"):
        with pytest.raises(sdk.TypeSafeError):
            sdk.TypeSafeClient(api_key=bad)


def test_real_sdk_async_wallclock_deadline(offline_transport, close_spy):
    """Asılı asynç yanıt: duvar saati deadline'ı (1.5 sn) duvar saatinde
    ateşlenir — 5 sn'lik asılı handler beklemez; sonuç network/zaman aşımı;
    aclose yine çağrılır; fazladan iş parçacığı bırakmaz."""
    import asyncio
    import threading
    import time

    import httpx2

    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)  # deadline'dan uzun — asla tamamlanmamalı
        return httpx2.Response(200, json={"models": []})

    offline_transport(handler)
    threads_before = threading.active_count()
    started = time.monotonic()
    r = decision_credentials.test_provider("typesafe", "ts-gizli-uzun")
    elapsed = time.monotonic() - started
    assert r["ok"] is False and r["code"] == "network"
    assert r["detail"] == "Bağlantı zaman aşımına uğradı."
    assert elapsed < 3.0, f"deadline duvar saatinde ateşlenmedi: {elapsed:.2f}s"
    assert close_spy == [True]
    assert threading.active_count() == threads_before  # arka plan iş parçacığı yok


def test_real_sdk_async_no_extra_threads_and_closed_on_success(offline_transport, close_spy):
    """Başarılı asynç çağrı da iş parçacığı bırakmaz; aclose garanti."""
    import threading

    import httpx2

    def handler(request):
        return httpx2.Response(
            200,
            json={"models": [{"name": "jev-latest", "description": "d", "release_date": "2026-09-01"}]},
        )

    offline_transport(handler)
    threads_before = threading.active_count()
    r = decision_credentials.test_provider("typesafe", "ts-gizli-uzun")
    assert r == {"ok": True, "code": "", "detail": ""}
    assert close_spy == [True]
    assert threading.active_count() == threads_before


def test_real_sdk_nested_event_loop_refused_safely(offline_transport):
    """İç içe event loop (backend deseni): temiz, sabit ret — RuntimeError
    veya beklemeden kalan coroutine YOK; ağa çıkış da olmaz."""
    import asyncio

    def handler(request):
        raise AssertionError("iç içe döngü reddi ağa çıkmamalı")

    offline_transport(handler)

    async def inner():
        return decision_credentials.test_provider("typesafe", "ts-gizli-uzun")

    r = asyncio.run(inner())
    assert r == {
        "ok": False, "code": "sdk",
        "detail": "Bağlantı testi bu bağlamda çalıştırılamaz.",
    }
