"""run_runtime.jsonutil — run_runtime genelinde paylaşılan, TEK sıkı kanonik JSON sözleşmesi.

Kalıcı bir JSON değeri YALNIZCA şunları içerebilir:

    dict[str, JsonValue]
    list[JsonValue]
    str
    int
    sonlu (finite) float
    bool
    None

REDDEDİLİR: NaN, +Infinity, -Infinity, tuple, set, bytes, Path, datetime,
keyfi Python nesneleri, ve str OLMAYAN dict anahtarları. pickle KULLANILMAZ.

Bu modül, RunEvent.payload ile RunRecord.routing/budget/workspace_snapshot
için AYNI doğrulama/serileştirme sözleşmesini sağlar (bkz. events.py, models.py,
store.py).
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

from run_runtime.errors import EventValidationError


def validate_json_value(value: Any, *, path: str = "$") -> None:
    """`value`nun kanonik JSON sözleşmesine uyduğunu ÖZYİNELEMELİ olarak doğrular.

    bool önce kontrol edilir (bool, int'in alt sınıfı olduğundan isinstance(x, int)
    True/False'u da kabul eder — burada True/False zaten geçerli bir JSON
    değeridir, bu yüzden ayrı bir dal olarak ele alınır, ama bir 'int' testinden
    ÖNCE gelmesi bilinçli bir sıralama tercihidir, işlevsel bir fark yaratmaz).
    """
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EventValidationError(f"{path}: sonlu olmayan sayı kabul edilmiyor: {value!r}")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventValidationError(f"{path}: dict anahtarı string olmalı: {key!r}")
            validate_json_value(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_value(item, path=f"{path}[{index}]")
        return
    raise EventValidationError(f"{path}: JSON-uyumlu olmayan tür: {type(value).__name__}")


def validate_json_dict(value: Any, *, field: str) -> dict[str, Any]:
    """`value`nun (yalnızca str anahtarlı, JSON-uyumlu) bir dict olduğunu doğrular.

    None dahil dict OLMAYAN her şeyi reddeder — isteğe bağlı None'lu alanlar
    için bkz. canonical_optional_dict_copy.
    """
    if not isinstance(value, dict):
        raise EventValidationError(f"{field} bir dict olmalı, alındı: {type(value).__name__}")
    validate_json_value(value, path=field)
    return value


def canonical_dict_copy(value: dict[str, Any], *, field: str) -> dict[str, Any]:
    """`value`yu doğrular ve iç içe konteynerleri paylaşmayan derin bir kopyasını döndürür.

    Böylece çağıranın elindeki (mutasyona açık) orijinal nesne, döndürülen
    değeri sonradan dolaylı olarak değiştiremez.
    """
    validate_json_dict(value, field=field)
    return copy.deepcopy(value)


def canonical_optional_dict_copy(value: dict[str, Any] | None, *, field: str) -> dict[str, Any] | None:
    """budget/workspace_snapshot gibi None'a izin verilen alanlar için canonical_dict_copy."""
    if value is None:
        return None
    return canonical_dict_copy(value, field=field)


def canonical_json_dumps(value: Any) -> str:
    """Kanonik, deterministik JSON metnine serileştirir.

    allow_nan=False: NaN/Infinity SESSİZCE geçmez (json.dumps'ın varsayılan
    davranışının aksine). sort_keys=True: anahtar sırası deterministiktir.
    Serileştirmeden ÖNCE değerin zaten validate_json_value'dan geçtiği
    varsayılır (bkz. çağıranlar); bu fonksiyon ayrıca kendi güvenlik ağını da
    (allow_nan=False) tutar.
    """
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
