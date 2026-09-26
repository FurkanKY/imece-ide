"""exec.stop POSIX süreç-ağacı testi — webview'suz, headless.

F5 çalıştırmasında kabuk (`shell=True`) çocuklarını da (npm/node/arka plan
`&` süreçleri) beraberinde ölmeli; salt kabuğu öldürmek yetmez — `sh -c
'sleep 60 & sleep 60'` örneğinde arka plandaki `sleep`ler kabuğun kendisi
bitince öksüz kalıp stdout borusunun yazma ucunu açık tutabilir (debugpy
adapter'ında görülen aynı sınıf sorun, bkz. test_dap.py). `exec.stop` bu
yüzden process_runtime.cleanup.terminate_process_tree ile TÜM soyu (ppid
zinciriyle bulunabilen) öldürmeli.

Çalıştırma:  python -m pytest tests/test_exec_posix_kill.py -q
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
psutil = pytest.importorskip("psutil")

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX süreç-ağacı testi")

from PySide6.QtCore import QCoreApplication  # noqa: E402

import webhost.api.exec as execmod  # noqa: E402,F401 — handler kaydı
from webhost import state  # noqa: E402
from webhost.bridge import HostBridge  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture()
def bridge(qapp):
    return HostBridge()


def rpc(bridge, method, params=None, call_id=1):
    out = []
    bridge.reply.connect(lambda raw: out.append(json.loads(raw)))
    bridge.call(json.dumps({"id": call_id, "method": method, "params": params or {}}))
    assert out, f"{method}: yanıt gelmedi"
    return out[-1]


@pytest.fixture()
def project(tmp_path):
    state.set_project(str(tmp_path))
    yield tmp_path
    state._active = None
    execmod._active["exec"] = None


def test_stop_kills_whole_process_tree(bridge, project):
    r = rpc(bridge, "exec.run", {"command": "sh -c 'sleep 60 & sleep 60; wait'"})
    assert r["ok"], r
    exec_id = r["result"]["execId"]

    ex = execmod._active["exec"]
    assert ex is not None and ex.id == exec_id
    shell_pid = ex.proc.pid

    # sleep çocukları doğsun diye kısa bir bekleme.
    deadline = time.monotonic() + 3
    descendants = []
    while time.monotonic() < deadline:
        try:
            descendants = psutil.Process(shell_pid).children(recursive=True)
        except psutil.NoSuchProcess:
            break
        if len(descendants) >= 2:
            break
        time.sleep(0.05)
    assert len(descendants) >= 2, "sleep alt-süreçleri doğmadı (test ortamı beklenmedik)"

    watch_pids = [shell_pid] + [d.pid for d in descendants]

    r = rpc(bridge, "exec.stop", {}, call_id=2)
    assert r["ok"], r

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(psutil.pid_exists(p) for p in watch_pids):
        time.sleep(0.05)

    survivors = [p for p in watch_pids if psutil.pid_exists(p)]
    assert not survivors, f"öldürülmeyen süreçler: {survivors}"
