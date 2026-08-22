"""run_runtime.errors — Task/Run/Event çalışma zamanı için tipli hata hiyerarşisi.

Alt katmanlar (store.py) ham sqlite3 hatalarını normal genel sözleşme olarak
DIŞARI SIZDIRMAZ; burada tanımlı tipli hatalara çevirir (uygun zincirlemeyle).
"""

from __future__ import annotations


class RunRuntimeError(Exception):
    """Tüm run_runtime hatalarının temel sınıfı."""


class RunNotFoundError(RunRuntimeError):
    """Belirtilen run_id için bir Run bulunamadı."""


class TaskNotFoundError(RunRuntimeError):
    """Belirtilen task_id için bir Task bulunamadı."""


class EventValidationError(RunRuntimeError):
    """Kalıcılıktan ÖNCE geçersiz bir değer bulundu.

    Yalnızca event payload'ları için değil; RunRecord.routing/budget/
    workspace_snapshot gibi kanonik JSON sözleşmesine tabi diğer alanlar için
    de kullanılır (bkz. run_runtime.jsonutil).
    """


class EventSequenceError(RunRuntimeError):
    """Bir event'in sıra numarası (seq) beklenen akışla tutarsız."""


class EventConflictError(RunRuntimeError):
    """(run_id, seq) çakışması — aynı sıra numarasına eşzamanlı/yinelenen yazım."""


class RunStoreError(RunRuntimeError):
    """Mağaza (SQLite) katmanında beklenmedik bir hata oluştu."""


class RunProjectionError(RunRuntimeError):
    """Bilinen bir event türü, projeksiyon için geçersiz/eksik bir payload içeriyor."""


class EventStreamClosedError(RunRuntimeError):
    """next_page(), kapatılmış bir DurableEventTail üzerinde çağrıldı.

    close() SERT bir kaynak sınırıdır: kapandıktan SONRA next_page(), SQLite'ta
    okunmamış dayanıklı event'ler olsa BİLE, hiçbir sorgu yapmadan HER ZAMAN bu
    hatayı fırlatır (backlog DRAIN EDİLMEZ) — bkz. run_runtime.service.DurableEventTail.
    """


class InvalidRunStateError(RunRuntimeError):
    """Bir işlem, Run'ın şu anki durumuyla (status) uyumsuz olduğu için reddedildi.

    Örn. Run WAITING_USER değilken proposal.applied/proposal.rejected
    yerleştirmeye (settle) çalışmak (bkz. run_runtime.legacy.LegacyRunCoordinator).
    """


class RunCompletionError(RunRuntimeError):
    """A requested Run terminal decision lacks valid canonical evidence."""
