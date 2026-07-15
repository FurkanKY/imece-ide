"""providers.* — sağlayıcı kataloğu yönetimi.

Katalog listesi, model seçimi ve özel (OpenAI-uyumlu) uç ekleme/çıkarma.
Anahtar durumu/kaydı keys.* altındadır; burada anahtar taşınmaz.
"""

import providers
from agents import DEFAULT_ROUTING
from webhost.bridge import handler, BridgeError


@handler("providers.list")
def _list(params, ctx):
    items = [providers.status_of(e) for e in providers.catalog()]
    return {"providers": items, "defaultRouting": dict(DEFAULT_ROUTING)}


@handler("providers.setModel")
def _set_model(params, ctx):
    pid = (params.get("provider") or "").strip()
    model = (params.get("model") or "").strip()
    if not model:
        raise BridgeError("empty", "Model adı boş.")
    try:
        providers.set_model(pid, model)
    except ValueError as e:
        raise BridgeError("unknown_provider", str(e))
    return {}


@handler("providers.addCustom")
def _add_custom(params, ctx):
    try:
        entry = providers.add_custom({
            "id": params.get("id", ""),
            "label": params.get("label", ""),
            "base_url": params.get("baseUrl", ""),
            "default_model": params.get("model", ""),
        })
    except ValueError as e:
        raise BridgeError("invalid", str(e))
    return {"provider": providers.status_of(providers.get(entry["id"]))}


@handler("providers.removeCustom")
def _remove_custom(params, ctx):
    pid = (params.get("provider") or "").strip()
    try:
        providers.remove_custom(pid)
    except ValueError as e:
        raise BridgeError("unknown_provider", str(e))
    return {}
