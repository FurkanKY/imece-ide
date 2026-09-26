"""POSIX PTY terminal testleri — ptyprocess backend'i (webhost/api/terminal.py).

Kabuk seçim mantığı ($SHELL → bash → sh) ve gerçek bir PTY üzerinden
create/write/read/exit tur-testi. Windows'ta anlamsız; atlanır.

Çalıştırma:  python -m pytest tests/test_terminal_posix.py -q
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
pytest.importorskip("ptyprocess")

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX PTY testi")

from PySide6.QtCore import QCoreApplication  # noqa: E402

import webhost.api.terminal as terminal  # noqa: E402
from webhost.bridge import HostBridge  # noqa: E402


# ---------------- kabuk seçimi ----------------

def test_prefers_shell_env(monkeypatch, tmp_path):
    fake = tmp_path / "myshell"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("SHELL", str(fake))
    assert terminal._posix_shell() == str(fake)


def test_falls_back_to_bash_when_shell_env_invalid(monkeypatch):
    monkeypatch.setenv("SHELL", "/does/not/exist")
    result = terminal._posix_shell()
    assert result in ("/bin/bash", "/bin/sh")


def test_falls_back_to_sh_when_shell_env_unset(monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    result = terminal._posix_shell()
    assert os.path.isfile(result) and os.access(result, os.X_OK)


# ---------------- create/write/read/exit tur-testi ----------------

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


def _pump_until(qapp, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_pty_roundtrip_write_echo_and_exit(bridge, qapp, tmp_path):
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))

    r = rpc(bridge, "terminal.create", {"cwd": str(tmp_path), "cols": 80, "rows": 24})
    assert r["ok"], r
    term_id = r["result"]["termId"]
    shell_name = r["result"]["shell"]
    assert shell_name in ("bash", "sh") or shell_name  # env kabuğu farklı olabilir

    try:
        marker = "IMECE_PTY_MARKER_42"
        r = rpc(bridge, "terminal.write", {"termId": term_id, "data": f"echo {marker}\n"}, call_id=2)
        assert r["ok"], r

        def saw_marker():
            return any(
                e["channel"] == "terminal.data"
                and e["payload"].get("termId") == term_id
                and marker in e["payload"].get("data", "")
                for e in events
            )

        assert _pump_until(qapp, saw_marker), "PTY echo'sunda marker görülmedi"

        r = rpc(bridge, "terminal.write", {"termId": term_id, "data": "exit\n"}, call_id=3)
        assert r["ok"], r

        def saw_exit():
            return any(
                e["channel"] == "terminal.exit" and e["payload"].get("termId") == term_id
                for e in events
            )

        assert _pump_until(qapp, saw_exit), "terminal.exit olayı gelmedi"
        exit_evt = next(e for e in events if e["channel"] == "terminal.exit")
        assert exit_evt["payload"]["code"] == 0
    finally:
        rpc(bridge, "terminal.kill", {"termId": term_id}, call_id=99)
        terminal.shutdown()
