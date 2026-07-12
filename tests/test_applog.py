"""applog.py testleri — dosya log'u + excepthook (Qt'siz)."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webhost import applog  # noqa: E402


def test_setup_writes_and_excepthook_logs(tmp_path, capsys):
    log = tmp_path / "app.log"
    applog.setup(log_path=log)
    try:
        applog.logger.info("merhaba log")
        # excepthook: loglar + varsayılan hook'a zincirler (stderr'e basar)
        try:
            raise ValueError("kasıtlı test hatası")
        except ValueError:
            applog._excepthook(*sys.exc_info())
        content = log.read_text(encoding="utf-8")
        assert "merhaba log" in content
        assert "YAKALANMAMIŞ İSTİSNA" in content
        assert "kasıtlı test hatası" in content
        assert "ValueError" in content
    finally:
        # test handler'ını sök — diğer testler gerçek yola yazmasın
        for h in list(applog.logger.handlers):
            applog.logger.removeHandler(h)
            h.close()
        sys.excepthook = sys.__excepthook__


def test_bridge_notified_on_exception(tmp_path):
    log = tmp_path / "app2.log"
    applog.setup(log_path=log)

    class FakeBridge:
        def __init__(self):
            self.events = []

        def emit_event_from_any_thread(self, channel, payload):
            self.events.append((channel, payload))

    fake = FakeBridge()
    applog.attach_bridge(fake)
    try:
        try:
            raise RuntimeError("köprü duyurusu")
        except RuntimeError:
            applog._excepthook(*sys.exc_info())
        assert fake.events, "app.error olayı yayınlanmadı"
        channel, payload = fake.events[0]
        assert channel == "app.error"
        assert payload["message"] == "köprü duyurusu"
        assert payload["logPath"].endswith("app2.log")
    finally:
        applog.attach_bridge(None)
        for h in list(applog.logger.handlers):
            applog.logger.removeHandler(h)
            h.close()
        sys.excepthook = sys.__excepthook__
