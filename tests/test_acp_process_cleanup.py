"""terminate_process_tree — extracted from process_runtime/runner.py with no
behavioral redesign. See spec section 6/39."""

import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process_runtime.cleanup import ProcessTreeSnapshot, capture_process_tree, terminate_process_tree  # noqa: E402
from process_runtime.errors import ProcessCleanupError  # noqa: E402


def _spawn(*args):
    return subprocess.Popen((sys.executable, "-c", *args), start_new_session=True)


def _alive(pid):
    return psutil.pid_exists(pid)


def test_already_dead_pid_is_harmless():
    process = _spawn("pass")
    process.wait()
    pid = process.pid
    time.sleep(0.1)
    terminate_process_tree(pid)  # must not raise


def test_root_process_is_terminated():
    process = _spawn("import time; time.sleep(30)")
    try:
        assert _alive(process.pid)
        terminate_process_tree(process.pid)
        process.wait(timeout=2)
        assert not _alive(process.pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_descendant_is_also_terminated(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    parent = _spawn(
        "import subprocess, sys, time; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"open({str(child_pid_file)!r}, 'w').write(str(p.pid)); "
        "time.sleep(30)"
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text().strip())
        deadline = time.monotonic() + 5
        while not _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _alive(child_pid)

        terminate_process_tree(parent.pid)
        parent.wait(timeout=2)

        deadline = time.monotonic() + 3
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child_pid)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child_pid is not None and _alive(child_pid):
            terminate_process_tree(child_pid)


def test_capture_process_tree_on_dead_pid_returns_empty_snapshot():
    process = _spawn("pass")
    process.wait()
    pid = process.pid
    time.sleep(0.1)
    snapshot = capture_process_tree(pid)
    assert isinstance(snapshot, ProcessTreeSnapshot)
    assert snapshot.root is None
    assert snapshot.descendants == ()


def test_capture_process_tree_includes_root_and_descendant(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    parent = _spawn(
        "import subprocess, sys, time; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"open({str(child_pid_file)!r}, 'w').write(str(p.pid)); "
        "time.sleep(30)"
    )
    child_pid = None
    snapshot = None
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child_pid = int(child_pid_file.read_text().strip())
        deadline = time.monotonic() + 5
        while not _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        snapshot = capture_process_tree(parent.pid)
        assert snapshot.root is not None
        assert snapshot.root.pid == parent.pid
        assert any(p.pid == child_pid for p in snapshot.descendants)
    finally:
        if snapshot is not None:
            try:
                terminate_process_tree(parent.pid, snapshot=snapshot)
            except ProcessCleanupError:
                pass
        elif parent.poll() is None:
            parent.kill()
            parent.wait()
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child_pid is not None and _alive(child_pid):
            terminate_process_tree(child_pid)


def test_terminate_process_tree_with_no_snapshot_preserves_old_behavior(tmp_path):
    """No-snapshot call site (existing ProcessRunner callers) must behave
    identically to before the snapshot parameter was added."""
    process = _spawn("import time; time.sleep(30)")
    try:
        assert _alive(process.pid)
        terminate_process_tree(process.pid)  # positional-only, no snapshot kwarg
        process.wait(timeout=2)
        assert not _alive(process.pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_snapshot_catches_descendant_reparented_after_root_exit(tmp_path):
    """This is the exact hardening-round-2 blocker-1 race, reproduced at the
    process_runtime.cleanup level: capture a snapshot while the root and its
    descendant both still exist, let the root exit and the descendant become
    a reparented orphan (no longer discoverable via a fresh tree-walk from
    the now-dead root pid), and prove the snapshot alone still finds and
    kills it."""
    child_pid_file = tmp_path / "child.pid"
    parent = _spawn(
        "import subprocess, sys, time; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True); "
        f"open({str(child_pid_file)!r}, 'w').write(str(p.pid)); "
        "time.sleep(0.3)"  # exits quickly, orphaning the child
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child_pid = int(child_pid_file.read_text().strip())
        deadline = time.monotonic() + 5
        while not _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _alive(child_pid)

        # Capture BEFORE the root exits.
        snapshot = capture_process_tree(parent.pid)
        assert any(p.pid == child_pid for p in snapshot.descendants)

        # Let the root actually exit (it was scripted to exit quickly).
        parent.wait(timeout=5)
        deadline = time.monotonic() + 3
        while _alive(parent.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(parent.pid)
        assert _alive(child_pid), "test setup invariant: child must still be alive/orphaned at this point"

        # A no-snapshot rescan from the now-dead root pid must NOT find the
        # orphan (proving the race is real without the snapshot).
        rescan = capture_process_tree(parent.pid)
        assert rescan.root is None

        # With the pre-exit snapshot, termination must still reach the orphan.
        terminate_process_tree(parent.pid, snapshot=snapshot)
        deadline = time.monotonic() + 3
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child_pid)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child_pid is not None and _alive(child_pid):
            terminate_process_tree(child_pid)


def test_survivor_raises_process_cleanup_error(monkeypatch):
    process = _spawn("import time; time.sleep(30)")
    try:
        assert _alive(process.pid)

        real_wait_procs = psutil.wait_procs

        def _fake_wait_procs(procs, timeout=None):
            return [], list(procs)

        monkeypatch.setattr(psutil, "wait_procs", _fake_wait_procs)
        try:
            with pytest.raises(ProcessCleanupError):
                terminate_process_tree(process.pid)
        finally:
            monkeypatch.setattr(psutil, "wait_procs", real_wait_procs)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
