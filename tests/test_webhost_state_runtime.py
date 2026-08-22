"""webhost.state — paylaşılan RunRuntime yaşam döngüsü testleri (PySide6 GEREKTİRMEZ).

webhost/state.py hiçbir PySide6 sembolü import etmez; bu testler webhost
paketinin diğer (PySide6 bağımlı) alt modüllerine dokunmadan çalışır.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webhost.state as state  # noqa: E402
from run_runtime.service import RunRuntime  # noqa: E402
from run_runtime.store import RunStore  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_runtime():
    """Her test öncesi/sonrası paylaşılan durumu sıfırlar — testler arası sızıntı olmaz."""
    state.set_run_runtime(None)
    yield
    state.set_run_runtime(None)


def test_get_run_runtime_is_none_until_first_call():
    assert state._run_runtime is None


def test_get_run_runtime_returns_same_instance_across_repeated_calls(tmp_path):
    injected = RunRuntime(RunStore(tmp_path / "runtime.sqlite3"))
    state.set_run_runtime(injected)

    first = state.get_run_runtime()
    second = state.get_run_runtime()
    third = state.get_run_runtime()

    assert first is injected
    assert first is second is third
    assert first.bus is second.bus  # aynı EventBus — abone bölünmesi YOK
    assert first.store is second.store


def test_injected_runtime_does_not_touch_the_real_app_database(tmp_path, monkeypatch):
    # Enjeksiyon kullanılırken get_run_runtime'ın varsayılan (gerçek) yolu
    # HİÇ ÇAĞIRMADIĞINI kanıtlamak için run_runtime_db_path'i "asla çağrılmamalı"
    # bir sentinel ile değiştiriyoruz.
    import webhost.state as state_module

    def _boom():
        raise AssertionError("run_runtime_db_path enjeksiyon varken çağrılmamalıydı")

    monkeypatch.setattr(state_module, "run_runtime_db_path", _boom)

    injected = RunRuntime(RunStore(tmp_path / "runtime.sqlite3"))
    state.set_run_runtime(injected)
    assert state.get_run_runtime() is injected


def test_get_run_runtime_lazily_creates_default_instance_at_configured_path(tmp_path, monkeypatch):
    import webhost.state as state_module

    fake_db_path = tmp_path / "runtime.sqlite3"
    monkeypatch.setattr(state_module, "run_runtime_db_path", lambda: fake_db_path)

    assert not fake_db_path.exists()  # yalnızca yol çözümlemesi/örnekleme dosya OLUŞTURMAZ

    first = state.get_run_runtime()
    second = state.get_run_runtime()

    assert first is second
    assert isinstance(first, RunRuntime)
    assert not fake_db_path.exists()  # RunStore de tembeldir: gerçek bir işlem olmadan dosya YOK

    first.store.create_task(project_root="/tmp/proj", prompt="p")  # ilk gerçek işlem
    assert fake_db_path.exists()


def test_set_run_runtime_none_forces_lazy_recreation_on_next_call(tmp_path, monkeypatch):
    import webhost.state as state_module

    monkeypatch.setattr(state_module, "run_runtime_db_path", lambda: tmp_path / "runtime.sqlite3")

    first = state.get_run_runtime()
    state.set_run_runtime(None)
    second = state.get_run_runtime()

    assert first is not second  # sıfırlamadan sonra YENİ bir örnek oluşturuldu
