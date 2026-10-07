import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_supervisor_argv_uses_script_in_source_mode(monkeypatch):
    from process_runtime.supervisor_launch import supervisor_argv

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert supervisor_argv("a", "b") == (
        sys.executable, str(ROOT / "process_runtime" / "supervisor.py"), "a", "b"
    )


def test_supervisor_argv_uses_frozen_dispatch(monkeypatch):
    from process_runtime.supervisor_launch import DISPATCH_FLAG, supervisor_argv

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert supervisor_argv("a", "b") == (sys.executable, DISPATCH_FLAG, "a", "b")


def test_supervisor_dispatch_runs_before_ui_and_preserves_exit_code(monkeypatch):
    import shell
    import process_runtime.supervisor as supervisor
    from process_runtime.supervisor_launch import DISPATCH_FLAG

    monkeypatch.setattr(sys, "argv", ["imece.exe", DISPATCH_FLAG, "bad"], raising=False)
    monkeypatch.setattr(supervisor, "main", lambda: 37)
    assert shell.main() == 37


def test_malformed_supervisor_dispatch_fails_closed(monkeypatch):
    import shell
    from process_runtime.supervisor_launch import DISPATCH_FLAG

    monkeypatch.setattr(sys, "argv", ["imece.exe", "--dev", DISPATCH_FLAG], raising=False)
    assert shell._run_packaged_helper() == 125


def test_valid_dispatch_with_malformed_arguments_never_starts_ui(monkeypatch):
    import shell
    from process_runtime.supervisor_launch import DISPATCH_FLAG
    monkeypatch.setattr(sys, "argv", ["imece.exe", DISPATCH_FLAG, "bad"])
    assert shell.main() == 125


def test_package_import_resolves_windows_job_without_top_level_module(tmp_path, monkeypatch):
    import process_runtime.supervisor as supervisor
    import process_runtime.windows_job as job
    nonce = "a" * 64
    receipt = tmp_path / "receipt.json"
    payload = {"argv": ["cmd.exe"], "cwd": str(tmp_path), "env": {}, "stdio": "process"}
    called = []
    def fake_job(configuration, cancel):
        called.append(configuration)
        return 7, False
    monkeypatch.setattr(job, "run_in_job", fake_job)
    monkeypatch.setattr(sys, "argv", ["imece.exe", "--windows-process", str(receipt), str(tmp_path / "cancel"), nonce])
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})())
    assert supervisor._windows_process_main() == 0
    assert called == [payload]
    assert json.loads(receipt.read_text())["exit_code"] == 7


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux inherited-FD dispatch fixture")
def test_real_shell_dispatch_emits_authenticated_quiescence_without_ui(tmp_path):
    from process_runtime.supervisor_launch import DISPATCH_FLAG
    config_read, config_write = os.pipe()
    receipt_read, receipt_write = os.pipe()
    nonce = "a" * 64
    try:
        process = subprocess.Popen([sys.executable, str(ROOT / "shell.py"), DISPATCH_FLAG,
            str(config_read), str(receipt_write), nonce], pass_fds=(config_read, receipt_write),
            cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.close(config_read); config_read = None
        os.close(receipt_write); receipt_write = None
        payload = {"argv": [sys.executable, "-c", "print('BOOTSTRAP_OK'); raise SystemExit(7)"],
                   "cwd": str(tmp_path), "env": {"PYTHONDONTWRITEBYTECODE": "1"}, "stdio": "process"}
        with os.fdopen(config_write, "wb") as stream:
            config_write = None
            stream.write(json.dumps(payload).encode() + b"\n")
        try:
            stdout, stderr = process.communicate(timeout=15)
        except BaseException:
            process.kill(); process.wait()
            raise
        assert process.returncode == 0 and b"BOOTSTRAP_OK" in stdout, stderr
        assert json.loads(os.read(receipt_read, 1024)) == {"nonce": nonce, "exit_code": 7, "quiescent": True}
        assert not list(tmp_path.iterdir())
    finally:
        for fd in (config_read, config_write, receipt_read, receipt_write):
            if fd is not None:
                os.close(fd)
