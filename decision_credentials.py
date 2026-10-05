"""decision_credentials.py — Jev (TypeSafe) karar sağlayıcısının kimlik bilgileri.

Hafif ve ortak bir modül: anahtarın env değişkeni adı, değer doğrulaması ve
isteğe bağlı SDK bağlantı testi burada durur. Normal LLM sağlayıcı kataloğundan
(providers.py) BİLEŞİK DEĞİLDİR — Jev ayrı bir "karar sağlayıcısı"dır;
planner/coder/reviewer yönlendirmesi ve sağlayıcı listesi bu anahtara asla
bakmaz (S1b, docs/JEV-DESIGN.md; keys.status ayrı bir decisionProviders
sonucu döner).

Kurallar:
- Anahtar os.environ'da yaşar (runtime); UI'a asla dönmez, yalnız son dört
  haneli maske gösterilir. .env HİÇBİR ZAMAN buradan okunmaz.
- Doğrulama SDK'nın kendi kuralıyla uyumlu: yalnız boşluksuz PRINTABLE ASCII
  (SDK TypeSafeError'la reddediyor: "only printable ASCII characters without
  whitespace"). Ek olarak .env/yazım güvenliği için sorunlu karakterler
  ($ ve '${..}' genişlemesi, # yorum, tırnak işaretleri, ters eğik çizgi,
  &, ;, |, <, >, %) reddedilir — kaynak moddaki seri hale getirilmiş .env
  satırı bozulamaz. Hata metinleri anahtar DEĞERİNİ asla içermez.
- SDK yalnız decision_runtime.jev_backend._import_typesafe_sdk() dikişinden
  (tek seam; gövde-log filtresini de kurar) GECİKMELİ alınır; modül
  seviyesinde decision_runtime import EDİLMEZ.
- test_provider() yalnız kullanıcı açıkça "sına" dediğinde ağa çıkar; istek
  salt bağlantı testidir (GET /v1/models — modellerin listesi), proje
  içeriği taşımaz. ASYNC istemci (AsyncTypeSafeClient) içinde duvar saati
  deadline'ı çalışır (asyncio.run + asyncio.wait_for ~1.5 sn) — köprü/RPC
  ana iş parçacığını uzun "trickle" istek kilitlemez; backend
  (decision_runtime.jev_backend) ile aynı desen. close/aclose her durumda
  kendi bütçesiyle (0.5 sn) çalışır; iç içe event loop önceden ve güvenle
  reddedilir. SABİT taban uç: https://api.typesafe.ai — TYPESAFE_BASE_URL
  HONOR EDİLMEZ (anahtar sızdırma ucu riski). httpx2 zaman aşımı faz
  başınadır (katı duvar saati sınırı değil). SDK hataları sınıf/.status ile
  SABİT Türkçe metinlere eşlenir — SDK hata str'i HAM GÖVDE içerir
  (doğrulandı: "401 ham"), asla aktarılmaz.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os

TYPESAFE_ENV = "TYPESAFE_API_KEY"

# Karar sağlayıcı kataloğu — providers.CATALOG'a bilinçli olarak EKLENMEZ.
DECISION_PROVIDERS: list[dict] = [
    {
        "id": "typesafe",
        "label": "Jev (TypeSafe)",
        "key_env": TYPESAFE_ENV,
        "key_hint": "ts-…",
        "docs_url": "https://docs.typesafe.ai",
    },
]

DECISION_IDS: dict[str, dict] = {p["id"]: p for p in DECISION_PROVIDERS}

# Gerçek typesafe-sdk 0.7.2 (PyPI paket adı typesafe-sdk, import adı typesafe_sdk).
SDK_MODULE_NAME = "typesafe_sdk"

# SABİT, güvenilir uç — sdk.constants.DEFAULT_BASE_URL ile aynı; ortam
# değişkeniyle (TYPESAFE_BASE_URL) GEÇERSİZ KILINMAZ.
_TYPESAFE_BASE_URL = "https://api.typesafe.ai"

# Bağlantı testi için kısa üst sınırlar (backend desenine hizalı). DİKKAT:
# httpx2 zaman aşımı FAZ BAŞINA (connect/read/write/pool) geçerlidir; asıl
# katı sınır duvar saati deadline'ıdır (asyncio.wait_for). Ayarlar testi
# senkron ARAYÜZÜNE sahip ama içi async'tir: ana/RPC iş parçacığı uzun
# istekle kilitlenmez, en kötü duvar saati ≈ deadline + close bütçesi.
_TEST_TIMEOUT_S = 1.2       # faz başına httpx2 zaman aşımı
_TEST_DEADLINE_S = 1.5      # duvar saati deadline (asyncio.wait_for)
_TEST_CLOSE_BUDGET_S = 0.5  # aclose için ayrı bütçe

_SDK_MISSING_DETAIL = (
    "TypeSafe SDK kurulu değil. İsteğe bağlı kurulum: pip install -r requirements-jev.txt"
)

# .env / kabuk / DTO yazımını bozan karakterler (SDK printable ASCII'ye izin
# verir; biz kaynak moddaki düz .env gidiş-dönüşünü de korumak için bunları
# reddediyoruz). "=" dotenv'de değer içi olarak güvenli; reddedilmez.
_PROBLEMATIC_CHARS = set("#$\"'`\\&;|%<>")

# Yalnız ÇEVRİMDIŞI testler: httpx2.MockTransport enjeksiyon dikişi
# (sync + async istemciyi de besler — MockTransport ikisini de uygular).
# Üretimde None kalır; istemci gerçek ağa çıkar.
_transport_factory = None


def known_envs() -> tuple[str, ...]:
    """Gizli depo (.env/DPO) izin listesine katkı: karar sağlayıcı env adları."""
    return tuple(sorted({p["key_env"] for p in DECISION_PROVIDERS if p.get("key_env")}))


def key_vars() -> dict[str, str]:
    """keys.set kabulü: köprü sağlayıcı id'si → env değişkeni."""
    return {p["id"]: p["key_env"] for p in DECISION_PROVIDERS if p.get("key_env")}


def validate_key(value: object) -> str:
    """Anahtar değerini temizler; güvenlik ihlali ValueError (değer metne
    yansıtılmaz). SDK kuralı + .env gidiş-dönüş güvenliği birlikte:
    boşluksuz printable ASCII, sorunlu karakter yok."""
    if not isinstance(value, str):
        raise ValueError("Anahtar bir metin olmalı.")
    key = value.strip()
    if not key:
        raise ValueError("Anahtar boş.")
    if any(ch.isspace() for ch in key):
        raise ValueError("Anahtar boşluk veya satır sonu karakteri içeremez.")
    if not key.isascii():
        raise ValueError("Anahtar yalnız ASCII karakterler içerebilir.")
    if not key.isprintable():
        raise ValueError("Anahtar yalnız yazdırılabilir karakterler içerebilir.")
    if any(ch in _PROBLEMATIC_CHARS for ch in key):
        raise ValueError(
            "Anahtar sorunlu karakter içeriyor (#, $, tırnak, ters eğik çizgi, &, ;, |, <, >, %)."
        )
    return key


def sdk_available() -> bool:
    """İsteğe bağlı TypeSafe SDK kurulu mu? (import edip yan etki üretmeden.)"""
    try:
        return importlib.util.find_spec(SDK_MODULE_NAME) is not None
    except (ImportError, ValueError):
        return False


def _import_typesafe_sdk():
    """Tek SDK import dikişi backend çalışanına aittir:
    decision_runtime.jev_backend._import_typesafe_sdk — SDK'yi import eder ve
    gövde-log filtresini kurar. Gecikmeli (lazy): keys.status açılışta bu
    modülü yüklerken SDK'yı yüklemez. UI bu modülü asla import etmez."""
    from decision_runtime.jev_backend import _import_typesafe_sdk as seam

    return seam()


def _open_typesafe_client(sdk, key: str):
    """ASYNC SDK istemcisi — belgelenmiş yapıcı, keyword-only; spekülatif
    fallback YOK. max_retries=0 (tek sınırlı deneme), Retry-After onurlanmaz,
    backoff yok; base_url SABİT güvenilir uç. Async: ayarlar bağlantı testi
    köprü ana iş parçacığını uzun istekle kilitlemesin."""
    kwargs: dict = {
        "api_key": key,
        "timeout": _TEST_TIMEOUT_S,
        "retry": sdk.RetryPolicy(
            max_retries=0,
            backoff_initial=0.0,
            backoff_max=0.0,
            respect_retry_after=False,  # sınırsız Retry-After beklenmez
            api_connection_error=False,
            api_timeout_error=False,
            timeout=_TEST_DEADLINE_S,
        ),
        "base_url": _TYPESAFE_BASE_URL,
    }
    if _transport_factory is not None:  # yalnız çevrimdışı testler
        kwargs["transport"] = _transport_factory()
    return sdk.AsyncTypeSafeClient(**kwargs)


def _error_result(sdk, exc: BaseException) -> dict:
    """SDK hatalarını SINIF + .status ile SABİT, temizlenmiş metinlere eşler.
    SDK hata str'i ham yanıt gövdesini içerir — exc'in metni ve anahtar
    değeri asla aktarılmaz. TimeoutError: duvar saati deadline'ı doldu
    (asyncio.wait_for) — istek, okuma veya close bütçeyi aştı."""
    if isinstance(exc, TimeoutError):
        return {"ok": False, "code": "network", "detail": "Bağlantı zaman aşımına uğradı."}
    if isinstance(exc, (sdk.TypeSafeAuthenticationError, sdk.TypeSafePermissionDeniedError)):
        return {"ok": False, "code": "auth", "detail": "Anahtar reddedildi (401/403)."}
    if isinstance(exc, sdk.TypeSafeRateLimitError):
        return {
            "ok": False, "code": "http",
            "detail": "Hız sınırına takıldı (429). Kısa süre sonra yeniden deneyin.",
        }
    if isinstance(exc, sdk.TypeSafeAPITimeoutError):
        return {"ok": False, "code": "network", "detail": "Bağlantı zaman aşımına uğradı."}
    if isinstance(exc, sdk.TypeSafeAPIConnectionError):
        return {"ok": False, "code": "network", "detail": "Bağlantı kurulamadı."}
    if isinstance(exc, sdk.TypeSafeAPIError):
        status = getattr(exc, "status", None)
        if isinstance(status, int) and status > 0:
            return {"ok": False, "code": "http", "detail": f"Beklenmeyen yanıt: HTTP {status}"}
        return {"ok": False, "code": "http", "detail": "Beklenmeyen yanıt."}
    if isinstance(exc, sdk.TypeSafeError):
        return {"ok": False, "code": "sdk", "detail": "Bağlantı testi tamamlanamadı (SDK hatası)."}
    return {"ok": False, "code": "sdk", "detail": "Bağlantı testi tamamlanamadı."}


async def _aclose_quietly(client: object) -> None:
    """close/aclose her durumda çalışır, asla yükseltmez ve kendi bütçesiyle
    sınırlıdır (backend deseni): aclose tercih edilir, sync close tolere edilir."""
    try:
        closer = getattr(client, "aclose", None)
        if not callable(closer):
            closer = getattr(client, "close", None)
        if not callable(closer):
            return
        closing = closer()
        if inspect.isawaitable(closing):
            await asyncio.wait_for(closing, timeout=_TEST_CLOSE_BUDGET_S)
    except Exception:
        pass  # close asla yüzeye çıkmaz; kaynak en iyi çabayla bırakılır


async def _connection_check(sdk, key: str) -> dict:
    """Deadline'lı asynç çekirdek: istek+okuma asyncio.wait_for içinde;
    close/aclose finally'de — başarı, hata ve deadline'da her durumda."""
    client = None
    try:
        client = _open_typesafe_client(sdk, key)
        await asyncio.wait_for(client.models.list(), timeout=_TEST_DEADLINE_S)
    finally:
        await _aclose_quietly(client)


def _run_connection_check(sdk, key: str) -> dict:
    """Senkron köprü girişi → async çekirdek. İç içe event loop güvenli:
    çağıran bir döngü içindeyse (backend gibi) ÖNCEDEN ve temizce reddeder —
    asyncio.run RuntimeError'u veya beklemeden kalan coroutine olmaz."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # bu iş parçacığında döngü yok — normal köprü durumu
    else:
        return {"ok": False, "code": "sdk", "detail": "Bağlantı testi bu bağlamda çalıştırılamaz."}
    try:
        asyncio.run(_connection_check(sdk, key))
    except ImportError:
        return {"ok": False, "code": "sdk_missing", "detail": _SDK_MISSING_DETAIL}
    except Exception as exc:  # noqa: BLE001 — sınıf/status ile sabit eşlemeye gider
        return _error_result(sdk, exc)
    return {"ok": True, "code": "", "detail": ""}


def test_provider(provider_id: str, api_key: str | None = None) -> dict:
    """Ucuz canlı doğrulama: salt modellerin listesini ister (GET /v1/models)
    — proje içeriği yoktur. Anahtar verilmezse kayıtlı (env) anahtar denenir.
    Duvar saati deadline'lı ASYNC çağrı: köprü ana iş parçacığı kilitlenmez."""
    entry = DECISION_IDS.get(provider_id)
    if entry is None:
        raise ValueError(f"Test edilemeyen karar sağlayıcısı: {provider_id}")
    if isinstance(api_key, str) and api_key.strip():
        try:
            key = validate_key(api_key)
        except ValueError as e:
            raise ValueError("Geçersiz anahtar: " + str(e)) from e
    else:
        key = os.getenv(entry["key_env"], "").strip()
    if not key:
        return {"ok": False, "code": "no_key", "detail": "Önce bir anahtar kaydedin."}
    try:
        sdk = _import_typesafe_sdk()
    except ImportError:
        return {"ok": False, "code": "sdk_missing", "detail": _SDK_MISSING_DETAIL}
    return _run_connection_check(sdk, key)
