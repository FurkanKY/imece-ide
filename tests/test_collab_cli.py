"""End-to-end CLI tests for `python -m collab_runtime` (temp repos only):

two independent clones of a tiny committed source, one shared temporary bare
hub, two per-client stores: hub-init / init / join / context-set / task-set /
status / sync / bind, stale-context rejection, task-only updates not marking
context changed, real context changes marking the affected task, and clean
errors (no tracebacks, fixed messages) for every expected failure class.
No network, no user repos, no model providers.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collab_runtime.cli import main  # noqa: E402
from collab_runtime.context import ARTIFACT_RELPATH, parse_snapshot_bytes  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")

ROOT = Path(__file__).resolve().parents[1]


def _env():
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(args, cwd, check=True):
    cp = subprocess.run(["git", *args], cwd=str(cwd), env=_env(), capture_output=True, timeout=30)
    if check and cp.returncode != 0:
        raise AssertionError(f"git {args} failed: {cp.stderr.decode('utf-8', 'replace')}")
    return cp.stdout.decode("utf-8", "replace")


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _run_json(argv):
    code, out, err = _run(argv)
    assert code == 0, f"command failed: {argv}\nstderr: {err}"
    return json.loads(out)


def _make_origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q"], origin)
    _git(["config", "user.name", "Test"], origin)
    _git(["config", "user.email", "test@example.com"], origin)
    (origin / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (origin / ".gitignore").write_text(".imece/\n", encoding="utf-8")
    _git(["add", "-A"], origin)
    _git(["commit", "-q", "-m", "initial"], origin)
    return origin


def _clone(origin, path):
    _git(["clone", "-q", "--no-hardlinks", str(origin), str(path)], origin.parent)
    clone = Path(path)
    _git(["config", "user.name", "Test"], clone)
    _git(["config", "user.email", "test@example.com"], clone)
    return clone


def _head(repo):
    return _git(["rev-parse", "HEAD"], repo).strip()


@pytest.fixture
def clones(tmp_path):
    origin = _make_origin(tmp_path)
    return _clone(origin, tmp_path / "cloneA"), _clone(origin, tmp_path / "cloneB"), origin


@pytest.fixture
def hub(tmp_path):
    hub_path = tmp_path / "hub.git"
    assert _run(["hub-init", "--path", str(hub_path)])[0] == 0
    return hub_path


def _store(tmp_path, name):
    return tmp_path / name


def _ctx_file(tmp_path, goal="shared goal", name="context.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"goal": goal, "decisions": ["d1"], "interfaces": {}}), encoding="utf-8")
    return path


# ---------------- full two-client flow ----------------


def test_two_client_flow_init_join_context_task_sync_bind(tmp_path, clones, hub):
    a, b, _origin = clones
    store_a, store_b = _store(tmp_path, "storeA.git"), _store(tmp_path, "storeB.git")

    payload = _run_json([
        "init", "--store", str(store_a), "--remote", str(hub), "--project", str(a),
        "--session-id", "s1", "--target-version", "v0.1", "--goal", "ship it",
    ])
    assert payload["session_id"] == "s1"
    assert payload["base_commit"] == _head(a)

    joined = _run_json(["join", "--store", str(store_b), "--remote", str(hub), "--project", str(b)])
    assert joined["base_commit"] == payload["base_commit"]

    status = _run_json(["status", "--store", str(store_a), "--remote", str(hub)])
    rev = status["revision"]
    assert status["context_changed_tasks"] == {}

    ctx = _ctx_file(tmp_path)
    ctx_payload = _run_json([
        "context-set", "--store", str(store_a), "--remote", str(hub),
        "--context", str(ctx), "--expected-revision", rev,
    ])
    rev2 = ctx_payload["revision"]
    assert ctx_payload["context_hash"] != status["context_hash"]

    task_payload = _run_json([
        "task-set", "--store", str(store_b), "--remote", str(hub), "--task-id", "t-ui",
        "--owner", "bob", "--goal", "do ui", "--scope", "src/", "--status", "queued",
        "--expected-revision", rev2,
    ])
    rev3 = task_payload["revision"]

    synced = _run_json(["sync", "--store", str(store_a), "--remote", str(hub)])
    assert synced["revision"] == rev3
    # Task-only update: shared metadata hash identical after sync, context NOT marked changed.
    assert synced["context_hash"] == ctx_payload["context_hash"]
    assert synced["context_changed_tasks"] == {"t-ui": "unchanged"}
    assert synced["overlaps"] == []

    bound = _run_json(["bind", "--store", str(store_b), "--remote", str(hub), "--project", str(b), "--task-id", "t-ui"])
    assert bound["task_id"] == "t-ui"
    artifact = b / ARTIFACT_RELPATH
    assert artifact.is_file()
    envelope = parse_snapshot_bytes(artifact.read_bytes())
    assert envelope.revision == rev3
    assert envelope.selected_task.owner == "bob"
    # The artifact is ignored: git status stays clean, HEAD unchanged.
    assert _git(["status", "--porcelain"], b) == ""
    assert _head(b) == payload["base_commit"]

    # A real context change marks the affected task.
    ctx2 = _ctx_file(tmp_path, goal="revised goal", name="context2.json")
    rev4 = _run_json([
        "context-set", "--store", str(store_b), "--remote", str(hub),
        "--context", str(ctx2), "--expected-revision", rev3,
    ])["revision"]
    status_b = _run_json(["status", "--store", str(store_b), "--remote", str(hub)])
    assert status_b["context_changed_tasks"] == {"t-ui": "changed"}

    # Stale expected revision gets a clean fixed error (exit 3), no traceback.
    code, _out, err = _run([
        "context-set", "--store", str(store_a), "--remote", str(hub),
        "--context", str(ctx2), "--expected-revision", rev2,
    ])
    assert code == 3
    assert "sync and retry" in err
    assert "Traceback" not in err


def test_init_invalid_input_creates_no_store(tmp_path, clones, hub):
    a, _b, _origin = clones
    store_a = _store(tmp_path, "storeA.git")
    code, _out, err = _run(["init", "--store", str(store_a), "--remote", str(hub), "--project", str(a),
                            "--session-id", "bad id!", "--target-version", "v0.1", "--goal", "g"])
    assert code == 2 and err.strip()
    assert not store_a.exists()  # validated BEFORE creation: no occupied leftovers


def test_bind_atomic_exact_ignore_rule_and_failed_replace_preserves_bytes(tmp_path, clones, hub, monkeypatch):
    a, b, _origin = clones
    store_a, store_b = _store(tmp_path, "storeA.git"), _store(tmp_path, "storeB.git")
    _run_json(["init", "--store", str(store_a), "--remote", str(hub), "--project", str(a),
               "--session-id", "s1", "--target-version", "v0.1", "--goal", "g"])
    rev = _run_json(["join", "--store", str(store_b), "--remote", str(hub), "--project", str(b)])["revision"]
    _run_json(["task-set", "--store", str(store_a), "--remote", str(hub), "--task-id", "t1",
               "--owner", "bob", "--goal", "g", "--scope", "src/", "--status", "queued",
               "--expected-revision", rev])

    # EXACT-file ignore rule (not a whole .imece/ directory): the atomic
    # temp+replace write must still work and leave the artifact untracked-
    # invisible (the test's own .gitignore edit stays visible, as expected).
    (b / ".gitignore").write_text(".imece/shared-context.json\n", encoding="utf-8")
    bound = _run_json(["bind", "--store", str(store_b), "--remote", str(hub), "--project", str(b), "--task-id", "t1"])
    artifact = b / ARTIFACT_RELPATH
    first_bytes = artifact.read_bytes()
    assert parse_snapshot_bytes(first_bytes).task_id == "t1"
    assert _git(["status", "--porcelain"], b) == " M .gitignore\n"
    assert not [p for p in (b / ".imece").iterdir() if p.name != "shared-context.json"]

    # A failed os.replace must preserve the previous artifact bytes and
    # leave NO temp files behind.
    import collab_runtime.cli as cli_mod

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(cli_mod.os, "replace", boom)
    code, _out, err = _run(["bind", "--store", str(store_b), "--remote", str(hub), "--project", str(b), "--task-id", "t1"])
    monkeypatch.undo()
    assert code == 6
    assert "did not finish" in err and "Traceback" not in err
    assert artifact.read_bytes() == first_bytes
    assert not [p for p in (b / ".imece").iterdir() if p.name != "shared-context.json"]
    assert bound["task_id"] == "t1"


# ---------------- failure classes get clean, fixed errors ----------------


def test_stale_revision_and_malformed_context_file(tmp_path, clones, hub):
    a, b, _origin = clones
    store_a, store_b = _store(tmp_path, "storeA.git"), _store(tmp_path, "storeB.git")
    _run_json(["init", "--store", str(store_a), "--remote", str(hub), "--project", str(a),
               "--session-id", "s1", "--target-version", "v0.1", "--goal", "g"])
    rev = _run_json(["join", "--store", str(store_b), "--remote", str(hub), "--project", str(b)])["revision"]

    ctx1 = _ctx_file(tmp_path, goal="one")
    rev2 = _run_json(["context-set", "--store", str(store_a), "--remote", str(hub),
                      "--context", str(ctx1), "--expected-revision", rev])["revision"]

    code, _out, err = _run(["context-set", "--store", str(store_b), "--remote", str(hub),
                            "--context", str(_ctx_file(tmp_path, goal="two", name="c2.json")),
                            "--expected-revision", rev])
    assert code == 3 and err.strip() and "Traceback" not in err

    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    code, _out, err = _run(["context-set", "--store", str(store_b), "--remote", str(hub),
                            "--context", str(bad), "--expected-revision", rev2])
    assert code == 2 and err.strip() and "Traceback" not in err

    wrong_keys = tmp_path / "wrong.json"
    wrong_keys.write_text('{"goal": "g"}', encoding="utf-8")
    code, _out, err = _run(["context-set", "--store", str(store_b), "--remote", str(hub),
                            "--context", str(wrong_keys), "--expected-revision", rev2])
    assert code == 2 and err.strip()

    code, _out, err = _run(["context-set", "--store", str(store_b), "--remote", str(hub),
                            "--context", str(tmp_path / "absent.json"), "--expected-revision", rev2])
    assert code == 2 and err.strip()


def test_oversized_context_file_rejected(tmp_path, clones, hub):
    a, _b, _origin = clones
    store_a = _store(tmp_path, "storeA.git")
    _run_json(["init", "--store", str(store_a), "--remote", str(hub), "--project", str(a),
               "--session-id", "s1", "--target-version", "v0.1", "--goal", "g"])
    big = tmp_path / "big.json"
    big.write_bytes(b'{"goal":"' + b"x" * (65 * 1024) + b'"}')
    code, _out, err = _run(["context-set", "--store", str(store_a), "--remote", str(hub),
                            "--context", str(big), "--expected-revision", "a" * 40])
    assert code == 2 and err.strip()


def test_task_context_revision_must_be_session_history(tmp_path, clones, hub):
    a, _b, _origin = clones
    store_a = _store(tmp_path, "storeA.git")
    rev = _run_json(["init", "--store", str(store_a), "--remote", str(hub), "--project", str(a),
                     "--session-id", "s1", "--target-version", "v0.1", "--goal", "g"])["revision"]
    code, _out, err = _run([
        "task-set", "--store", str(store_a), "--remote", str(hub), "--task-id", "t1",
        "--owner", "alice", "--goal", "g", "--scope", "src/", "--status", "queued",
        "--expected-revision", rev, "--context-revision", "f" * 40,
    ])
    assert code == 2
    assert "published history" in err


def test_join_rejects_head_mismatch(tmp_path, clones, hub):
    a, b, _origin = clones
    store_a, store_b = _store(tmp_path, "storeA.git"), _store(tmp_path, "storeB.git")
    _run_json(["init", "--store", str(store_a), "--remote", str(hub), "--project", str(a),
               "--session-id", "s1", "--target-version", "v0.1", "--goal", "g"])
    (b / "extra.py").write_text("x\n", encoding="utf-8")
    _git(["add", "-A"], b)
    _git(["commit", "-q", "-m", "divergent"], b)
    code, _out, err = _run(["join", "--store", str(store_b), "--remote", str(hub), "--project", str(b)])
    assert code == 2
    assert "baseline" in err


def test_bind_failures_are_clean(tmp_path, clones, hub):
    a, b, origin = clones
    store_a, store_b = _store(tmp_path, "storeA.git"), _store(tmp_path, "storeB.git")
    _run_json(["init", "--store", str(store_a), "--remote", str(hub), "--project", str(a),
               "--session-id", "s1", "--target-version", "v0.1", "--goal", "g"])
    rev = _run_json(["join", "--store", str(store_b), "--remote", str(hub), "--project", str(b)])["revision"]
    _run_json(["task-set", "--store", str(store_a), "--remote", str(hub), "--task-id", "t1",
               "--owner", "bob", "--goal", "g", "--scope", "src/", "--status", "queued",
               "--expected-revision", rev])

    # HEAD mismatch: refuse and write nothing.
    (b / "extra.py").write_text("x\n", encoding="utf-8")
    _git(["add", "-A"], b)
    _git(["commit", "-q", "-m", "divergent"], b)
    code, _out, err = _run(["bind", "--store", str(store_b), "--remote", str(hub), "--project", str(b), "--task-id", "t1"])
    assert code == 2 and "base commit" in err
    assert not (b / ARTIFACT_RELPATH).exists()
    _git(["reset", "-q", "--hard", "HEAD~1"], b)

    # Unknown task id.
    code, _out, err = _run(["bind", "--store", str(store_b), "--remote", str(hub), "--project", str(b), "--task-id", "ghost"])
    assert code == 2 and "does not exist" in err

    # Store inside the repository toplevel (not just the supplied subdir).
    code, _out, err = _run(["bind", "--store", str(b / "nested" / "store.git"), "--remote", str(hub),
                            "--project", str(b / "nested" if (b / "nested").exists() else b), "--task-id", "t1"])
    assert code != 0

    # Symlinked .imece directory pointing inside the root is rejected.
    sub = b / "inside"
    sub.mkdir()
    (b / ".imece").symlink_to(sub)
    try:
        code, _out, err = _run(["bind", "--store", str(store_b), "--remote", str(hub), "--project", str(b), "--task-id", "t1"])
        assert code == 2 and "symlink" in err
        assert not (sub / "shared-context.json").exists()
    finally:
        (b / ".imece").unlink()

    # Missing ignore rule: exact actionable message, no ignores edited.
    (b / ".gitignore").write_text("other.txt\n", encoding="utf-8")
    code, _out, err = _run(["bind", "--store", str(store_b), "--remote", str(hub), "--project", str(b), "--task-id", "t1"])
    assert code == 2
    assert "add .imece/shared-context.json to ignore rules" in err
    assert "other.txt" in (b / ".gitignore").read_text(encoding="utf-8")


def test_init_joins_placement_and_occupancy_checks(tmp_path, clones, hub):
    a, b, _origin = clones
    store_a, store_b = _store(tmp_path, "storeA.git"), _store(tmp_path, "storeB.git")
    init_args = ["--store", str(store_a), "--remote", str(hub), "--project", str(a),
                 "--session-id", "s1", "--target-version", "v0.1", "--goal", "g"]

    # Store inside the source repository toplevel is rejected.
    inside = a / "sub"
    inside.mkdir()
    code, _out, err = _run(["init", "--store", str(inside / "store.git"), "--remote", str(hub),
                            "--project", str(inside), "--session-id", "s1", "--target-version", "v", "--goal", "g"])
    assert code == 2 and "outside" in err
    assert not (inside / "store.git").exists()

    # Hub inside the source repository toplevel is rejected (even though it
    # is a valid bare repository — hub-init itself cannot know the project).
    inside_hub = a / "hub.git"
    assert _run(["hub-init", "--path", str(inside_hub)])[0] == 0
    code, _out, err = _run(["init", "--store", str(store_a), "--remote", str(inside_hub),
                            "--project", str(a), "--session-id", "s1", "--target-version", "v", "--goal", "g"])
    assert code == 2 and "outside" in err

    # Occupied store path is refused.
    occupied = _store(tmp_path, "occupied.git")
    occupied.mkdir()
    (occupied / "junk").write_text("x", encoding="utf-8")
    code, _out, err = _run(["init", "--store", str(occupied), "--remote", str(hub), "--project", str(a),
                            "--session-id", "s1", "--target-version", "v", "--goal", "g"])
    assert code == 2 and "empty or non-existent" in err

    # Same path for hub and store is refused (an occupied path is caught by
    # the occupancy rule; the hub=store rule also guards bind).
    same = _store(tmp_path, "same.git")
    assert _run(["hub-init", "--path", str(same)])[0] == 0
    code, _out, err = _run(["init", "--store", str(same), "--remote", str(same), "--project", str(a),
                            "--session-id", "s1", "--target-version", "v", "--goal", "g"])
    assert code == 2 and ("different" in err or "empty or non-existent" in err)

    # Happy init + join; second init on the same hub is refused.
    _run_json(["init", *init_args])
    _run_json(["join", "--store", str(store_b), "--remote", str(hub), "--project", str(b)])
    code, _out, err = _run(["init", "--store", str(_store(tmp_path, "storeC.git")), "--remote", str(hub),
                            "--project", str(b), "--session-id", "s2", "--target-version", "v", "--goal", "g"])
    assert code == 4 and "already exists" in err


def test_hub_init_refuses_occupied_path(tmp_path):
    occupied = tmp_path / "busy.git"
    occupied.mkdir()
    (occupied / "file.txt").write_text("x", encoding="utf-8")
    code, _out, err = _run(["hub-init", "--path", str(occupied)])
    assert code == 2 and "empty or non-existent" in err


def test_unexpected_and_os_errors_have_no_traceback(tmp_path, clones):
    a, _b, _origin = clones
    code, _out, err = _run(["status", "--store", str(tmp_path / "nope.git"), "--remote", str(tmp_path / "nohub.git")])
    assert code != 0
    assert err.strip() and "Traceback" not in err and "Exception" not in err
    code, _out, err = _run(["status", "--store", str(a), "--remote", str(tmp_path / "nohub.git")])
    assert code != 0 and "Traceback" not in err


# ---------------- subprocess entry points ----------------


def _subprocess_run(args):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "collab_runtime", *args],
        cwd=str(ROOT), env=env, capture_output=True, timeout=60,
    )


def test_module_entry_help_works():
    cp = _subprocess_run(["--help"])
    assert cp.returncode == 0
    assert b"usage" in cp.stdout
    assert b"hub-init" in cp.stdout


def test_module_entry_failure_is_clean():
    cp = _subprocess_run(["status", "--store", "/nonexistent/store.git", "--remote", "/nonexistent/hub.git"])
    assert cp.returncode != 0
    assert b"Traceback" not in cp.stderr
    assert cp.stderr.strip()
