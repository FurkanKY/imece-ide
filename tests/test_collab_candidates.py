"""Two-clone, two-store local flow tests for collab_runtime.candidates + CLI.

Covers the REAL frontend/backend flow (individually incompatible proposals,
combined candidate passes pytest), default not_run, failing checks, conflict
abort (no output, no test execution, CLI 8), disjoint same-file merges,
identical-edit dedup, add/delete/mode-only, stale tokens/context/tasks/
provenance/base-OID rejection, superseded same-task selections, dirty source
immutability, protected/nonempty output rejection, malformed remote
artifacts, committed symlink baseline rejection and verification mutations
(invalidated status). No network, no user repos, no real model.
"""

import json
import os
import shutil
import subprocess
import sys
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collab_runtime.candidates import (  # noqa: E402
    CandidateConflictError,
    MAX_SELECTED_PROPOSALS,
    _fingerprint,
    assemble_candidate,
)
from collab_runtime.context import SharedSnapshot, parse_snapshot_bytes  # noqa: E402
from collab_runtime.errors import (  # noqa: E402
    CollabError,
    GitOperationError,
    StaleRevisionError,
    ValidationError,
)
from collab_runtime.models import (  # noqa: E402
    build_context,
    build_initial_state,
    build_task,
    canonical_json_bytes,
)
from collab_runtime.cli import main as cli_main  # noqa: E402
from collab_runtime.proposals import (  # noqa: E402
    capture_proposal,
    parse_proposal_dict,
    publish_proposal,
)  # noqa: E402
from collab_runtime.store import GitStore  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")

_PAIRING_TEST = (
    "from key import KEY\n"
    "from ui import LABEL\n"
    "\n"
    "def test_pairing():\n"
    "    assert (KEY == \"old-key\") == (LABEL.startswith(\"ui\"))\n"
)
_WRITE_FILE_TEST = (
    "from pathlib import Path\n"
    "\n"
    "def test_mutates():\n"
    "    Path(\"mutated.txt\").write_text(\"x\\n\")\n"
)
_TAMPER_TEST = (
    "from pathlib import Path\n"
    "\n"
    "def test_mutates():\n"
    "    Path(\"key.py\").write_text('KEY = \"tampered\"\\n')\n"
)
_SYMLINK_TEST = (
    "import os\n"
    "\n"
    "def test_escapes():\n"
    "    os.symlink(\"ui.py\", \"escape.py\")\n"
)
_HUGE_TEST = (
    "from pathlib import Path\n"
    "\n"
    "def test_writes_huge():\n"
    "    Path(\"huge.bin\").write_bytes(b\"x\" * (9 * 1024 * 1024))\n"
)
_MUTATE_CACHE_TEST = (
    "from pathlib import Path\n"
    "\n"
    "def test_mutates_cache_files():\n"
    "    Path(\"keep.pyc\").write_text(\"tampered\\n\")\n"
    "    Path(\"__pycache__\").mkdir(exist_ok=True)\n"
    "    Path(\"__pycache__/tracked.txt\").write_text(\"tampered\\n\")\n"
)
_UI_BASE = "from key import KEY\nLABEL = \"ui:\" + KEY\n\nFOOTER = \"a\"\n"


def test_fingerprint_entry_budget_applies_after_nested_walk(tmp_path, monkeypatch):
    import collab_runtime.candidates as candidates

    root = tmp_path / "fingerprint-budget"
    nested = root / "a-dir"
    nested.mkdir(parents=True)
    for name in ("a.py", "b.py", "c.py"):
        (nested / name).write_text("pass\n", encoding="utf-8")
    (root / "z.py").write_text("pass\n", encoding="utf-8")
    originals = ("a-dir/a.py", "a-dir/b.py", "a-dir/c.py", "z.py")
    # The directory and its children exhaust the budget before the root's
    # remaining file. A pre-enumerated sibling must not bypass that limit.
    monkeypatch.setattr(candidates, "_FP_ENTRY_BUDGET", 4)
    _digest, complete = candidates._fingerprint_records(root, originals)
    assert complete is False


def _env():
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(args, cwd, check=True):
    cp = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_env(), capture_output=True, timeout=30
    )
    if check and cp.returncode != 0:
        raise AssertionError(f"git {args} failed: {cp.stderr.decode('utf-8', 'replace')}")
    return cp.stdout.decode("utf-8", "replace")


def _write(root, rel, content, *, executable=False):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(0o755)
    return path


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _write_binding(root, revision, state, task_id):
    snapshot = SharedSnapshot(
        revision=revision, context_hash=state.context_hash, state=state, task_id=task_id,
    )
    raw = canonical_json_bytes(snapshot.to_dict())
    parse_snapshot_bytes(raw)  # sanity: the artifact must roundtrip
    (root / ".imece").mkdir(exist_ok=True)
    (root / ".imece" / "shared-context.json").write_bytes(raw)


def _world(
    tmp_path, *, test_body="pairing", committed_symlink=False, imece_ignored=True,
    verify_plan=None, tracked_cache=False, backslash_filename=False,
):
    """Two clones, two stores, one hub; bindings at the final task revision."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q"], origin)
    _git(["config", "user.name", "Test"], origin)
    _git(["config", "user.email", "test@example.com"], origin)
    _write(origin, ".gitignore", "" if not imece_ignored else ".imece/\n__pycache__/\n.pytest_cache/\n")
    if verify_plan is not None:
        _write(origin, ".imece/verify.json", verify_plan)
    _write(origin, "key.py", "KEY = \"old-key\"\n")
    _write(origin, "ui.py", _UI_BASE)
    if test_body is not None:
        _write(origin, "test_ui.py", {
            "pairing": _PAIRING_TEST, "write_file": _WRITE_FILE_TEST,
            "tamper": _TAMPER_TEST, "symlink": _SYMLINK_TEST,
            "huge": _HUGE_TEST, "mutate_cache": _MUTATE_CACHE_TEST,
        }[test_body])
    _write(origin, "gone.txt", "G\n")
    _write(origin, "run.sh", "#!/bin/sh\n")
    if committed_symlink:
        os.symlink("ui.py", str(origin / "linked.py"))
    if backslash_filename:
        _write(origin, "a\\b.txt", "X\n")
    if tracked_cache:
        _write(origin, "keep.pyc", "original\n")
        _write(origin, "__pycache__/tracked.txt", "original\n")
    _git(["add", "-A"], origin)
    if tracked_cache:
        _git(["add", "-f", "keep.pyc", "__pycache__/tracked.txt"], origin)  # tracked despite ignore rules
    _git(["commit", "-q", "-m", "base"], origin)
    base = _git(["rev-parse", "HEAD"], origin).strip()

    frontend = _clone(origin, tmp_path / "frontend")
    backend = _clone(origin, tmp_path / "backend")

    hub = GitStore.create_bare(tmp_path / "hub.git", what="hub")
    store_a = GitStore(store=GitStore.create_bare(tmp_path / "a.store.git", what="store"), remote=str(hub))
    store_b = GitStore(store=GitStore.create_bare(tmp_path / "b.store.git", what="store"), remote=str(hub))

    rev0 = store_a.init_session(build_initial_state(
        session_id="demo-1", target_version="v1", base_commit=base))
    rev1 = store_a.update_context(
        build_context(goal="ship the demo", decisions=[], interfaces={}), expected_revision=rev0)
    rev2 = store_a.upsert_task(build_task(
        task_id="t-ui", owner="alice", goal="ui work", scopes=["ui.py"],
        status="running", context_revision=rev1), expected_revision=rev1)
    rev3 = store_b.upsert_task(build_task(
        task_id="t-be", owner="bob", goal="backend work", scopes=["key.py"],
        status="running", context_revision=rev1), expected_revision=rev2)
    _head_a, state_a = store_a.fetch_state()
    _head_b, state_b = store_b.fetch_state()
    _write_binding(frontend, rev3, state_a, "t-ui")
    _write_binding(backend, rev3, state_b, "t-be")
    return SimpleNamespace(
        hub=hub, store_a=store_a, store_b=store_b,
        frontend=frontend, backend=backend, base=base,
        rev1=rev1, rev3=rev3,
    )


def _clone(origin, target):
    _git(["clone", "-q", str(origin), str(target)], origin)
    _git(["config", "user.name", "Test"], target)
    _git(["config", "user.email", "test@example.com"], target)
    return target


def _fe_edit(w):
    """Frontend changes the ui label text->message (individually incompatible)."""
    _write(w.frontend, "ui.py", _FE_EDITED)


_FE_EDITED = "from key import KEY\nLABEL = \"message:\" + KEY\n\nFOOTER = \"a\"\n"


def _be_edit(w):
    """Backend changes the backend key value (individually incompatible)."""
    _write(w.backend, "key.py", "KEY = \"new-key\"\n")


def _be_footer_edit(w):
    """Backend additionally changes a DISJOINT ui.py hunk (advisory out-of-scope)."""
    _write(w.backend, "ui.py", "from key import KEY\nLABEL = \"ui:\" + KEY\n\nFOOTER = \"b\"\n")


def _capture_publish(store, clone, proposal_id, task_id, paths):
    """Capture then publish with a freshly fetched CAS token (two explicit
    steps; each re-fetches, so sequential proposals stay usable)."""
    fresh, _state = store.fetch_state()
    proposal = capture_proposal(store, clone, proposal_id, task_id, paths, expected_revision=fresh)
    fresh, _state = store.fetch_state()
    _child, _commit = publish_proposal(store, proposal, expected_revision=fresh)
    return _child


def _head(store):
    return store.fetch_state()[0]


def _candidate(w, ids, output, *, verify=False, store=None, expected=None):
    return assemble_candidate(
        store if store is not None else w.store_a, w.frontend, ids, output,
        expected_revision=expected if expected is not None else _head(w.store_a),
        verify=verify,
    )


def _verification_diagnostic(verification):
    """Safe, useful CI context: do not include check output or project data."""
    return {
        "status": verification.get("status"),
        "fingerprint_complete": verification.get("fingerprint_complete"),
        "checks": verification.get("checks"),
    }


def _skip_unavailable_process_claim(verification):
    """Skip only process-effect assertions when Windows evidence is incomplete."""
    if (os.name == "nt" and verification.get("status") == "error"
            and verification.get("fingerprint_complete") is False
            and verification.get("checks") == []):
        pytest.skip(
            "Windows candidate no-follow file-identity inspection is incomplete; "
            "BEFORE checks were correctly not run, "
            "so process-effect claims are unavailable (safe metadata: "
            f"{_verification_diagnostic(verification)!r})"
        )


# ---------------- real two-clone flow: incompatible apart, together pass ----


def test_pairwise_flow_individually_fail_together_pass(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _be_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    _capture_publish(w.store_b, w.backend, "prop-be", "t-be", ["key.py"])
    fresh = _head(w.store_a)

    # frontend-only candidate: pytest FAILS (individually incompatible)
    only_fe = _candidate(w, ["prop-fe"], tmp_path / "cand-fe", verify=True)
    assert only_fe["conflicts"] == [] and only_fe["candidate_dir"] == str((tmp_path / "cand-fe").resolve())
    # backend-only candidate: pytest FAILS
    only_be = _candidate(w, ["prop-be"], tmp_path / "cand-be", verify=True)
    # combined candidate: pytest PASSES; both changes materialized
    both = _candidate(w, ["prop-fe", "prop-be"], tmp_path / "cand-both", verify=True)
    assert both["proposal_ids"] == ["prop-be", "prop-fe"]
    cand = tmp_path / "cand-both"
    assert (cand / "ui.py").read_text(encoding="utf-8") == "from key import KEY\nLABEL = \"message:\" + KEY\n\nFOOTER = \"a\"\n"
    assert (cand / "key.py").read_text(encoding="utf-8") == "KEY = \"new-key\"\n"
    assert (cand / "test_ui.py").exists() and (cand / ".gitignore").exists()
    assert both["file_count"] == 6
    assert both["binding_revision"] == w.rev3 and both["context_hash"]
    assert (cand / "run.sh").read_bytes() == b"#!/bin/sh\n"  # unchanged baseline file materialized as-is
    assert os.access(cand / "ui.py", os.R_OK)
    assert (w.frontend / "ui.py").read_text(encoding="utf-8") == _FE_EDITED
    for payload, expected in ((only_fe, "fail"), (only_be, "fail"), (both, "pass")):
        _skip_unavailable_process_claim(payload["verification"])
        assert payload["verification"]["status"] == expected, (
            _verification_diagnostic(payload["verification"])
        )
    assert both["verification"]["changed_content"] is False


def test_candidate_accepts_validated_binding_without_project_artifact(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "bound-fe", "t-ui", ["ui.py"])
    artifact = w.frontend / ".imece" / "shared-context.json"
    binding = parse_snapshot_bytes(artifact.read_bytes())
    artifact.unlink()

    receipt = assemble_candidate(
        w.store_a, w.frontend, ["bound-fe"], tmp_path / "bound-candidate",
        expected_revision=_head(w.store_a), binding=binding,
    )
    assert receipt["binding_revision"] == binding.revision
    with pytest.raises(CollabError):
        assemble_candidate(
            w.store_a, w.frontend, ["bound-fe"], tmp_path / "invalid-candidate",
            expected_revision=_head(w.store_a), binding=binding.to_dict(),
        )
    assert (tmp_path / "bound-candidate" / "ui.py").exists()


def test_default_no_verify_is_not_run(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=False)
    assert payload["verification"]["status"] == "not_run"
    assert payload["verification"]["checks"] == []
    assert (tmp_path / "cand" / "test_ui.py").exists()


# ---------------- merge semantics ---------------- --------------------------


def test_disjoint_same_file_hunks_merge_and_identical_edits_dedup(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)             # changes LABEL (line 2)
    _be_edit(w)             # changes key.py
    _be_footer_edit(w)      # backend ALSO changes FOOTER (disjoint hunk, advisory)
    # identical edit dedup: both tasks make the same keep edit
    _write(w.frontend, "gone.txt", "G2\n")
    _write(w.backend, "gone.txt", "G2\n")
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py", "gone.txt"])
    _capture_publish(w.store_b, w.backend, "prop-be", "t-be", ["key.py", "ui.py", "gone.txt"])
    payload = _candidate(w, ["prop-be", "prop-fe"], tmp_path / "cand", verify=True)
    cand = tmp_path / "cand"
    assert (cand / "ui.py").read_text(encoding="utf-8") == (
        "from key import KEY\nLABEL = \"message:\" + KEY\n\nFOOTER = \"b\"\n"
    )
    assert (cand / "gone.txt").read_text(encoding="utf-8") == "G2\n"
    assert (cand / "key.py").read_text(encoding="utf-8") == "KEY = \"new-key\"\n"
    fe_entry = next(p for p in payload["proposals"] if p["proposal_id"] == "prop-be")
    assert fe_entry["paths"] == ["gone.txt", "key.py", "ui.py"]
    _skip_unavailable_process_claim(payload["verification"])
    assert payload["verification"]["status"] == "pass", (
        _verification_diagnostic(payload["verification"])
    )


def test_add_delete_and_mode_only_materialize(tmp_path):
    w = _world(tmp_path)
    _write(w.frontend, "new.py", "N\n")
    (w.frontend / "gone.txt").unlink()
    (w.frontend / "run.sh").chmod(0o755)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["new.py", "gone.txt", "run.sh"])
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand")
    cand = tmp_path / "cand"
    assert not (cand / "gone.txt").exists()
    assert (cand / "new.py").read_bytes() == b"N\n"
    assert os.access(cand / "run.sh", os.X_OK)
    assert payload["file_count"] == 6  # 5 baseline files + new.py - gone.txt... (5+1-1=6? see below)
    assert payload["verification"]["status"] == "not_run"


def test_conflicting_overlapping_edits_abort_cleanly(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)  # LABEL -> message:KEY
    # backend ALSO changes the SAME ui.py line (overlapping text edit)
    _write(w.backend, "ui.py", "from key import KEY\nLABEL = \"banner:\" + KEY\n\nFOOTER = \"a\"\n")
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    _capture_publish(w.store_b, w.backend, "prop-be", "t-be", ["ui.py"])
    output = tmp_path / "cand"
    with pytest.raises(CandidateConflictError) as excinfo:
        _candidate(w, ["prop-be", "prop-fe"], output)
    assert excinfo.value.conflict_paths == ["ui.py"]
    assert not output.exists()  # NOTHING materialized, no partial output


def test_conflicting_add_add_abort(tmp_path):
    w = _world(tmp_path)
    _write(w.frontend, "added.txt", "F1\n")
    _write(w.backend, "added.txt", "B1\n")
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["added.txt"])
    _capture_publish(w.store_b, w.backend, "prop-be", "t-be", ["added.txt"])
    with pytest.raises(CandidateConflictError) as excinfo:
        _candidate(w, ["prop-be", "prop-fe"], tmp_path / "cand")
    assert excinfo.value.conflict_paths == ["added.txt"]
    assert not (tmp_path / "cand").exists()


def test_delete_modify_abort(tmp_path):
    w = _world(tmp_path)
    (w.frontend / "gone.txt").unlink()
    _write(w.backend, "gone.txt", "M2\n")
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["gone.txt"])
    _capture_publish(w.store_b, w.backend, "prop-be", "t-be", ["gone.txt"])
    with pytest.raises(CandidateConflictError):
        _candidate(w, ["prop-be", "prop-fe"], tmp_path / "cand")
    assert not (tmp_path / "cand").exists()


# ---------------- verification honesty ---------------- ---------------------


def test_verification_failure_is_fail_not_pass(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=True)
    assert (tmp_path / "cand").exists()  # failed/incomplete verify preserves candidate
    _skip_unavailable_process_claim(payload["verification"])
    status = payload["verification"]["status"]
    check = payload["verification"]["checks"][0]
    assert status == "fail" and check["status"] == "fail" and check["exit_code"] != 0
    assert set(check) == {"check_id", "status", "exit_code", "timed_out"}


def test_verification_bad_plan_config_is_error(tmp_path):
    w = _world(tmp_path, imece_ignored=False, verify_plan="{\"id\": 1}\n")
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=True)
    assert payload["verification"]["status"] == "error"
    assert payload["verification"]["checks"] == []


def test_test_mutates_candidate_code_is_invalidated(tmp_path):
    w = _world(tmp_path, test_body="write_file")
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=True)
    assert (tmp_path / "cand" / "ui.py").exists()
    _skip_unavailable_process_claim(payload["verification"])
    assert payload["verification"]["status"] == "invalidated"
    assert payload["verification"]["changed_content"] is True
    assert (tmp_path / "cand" / "mutated.txt").exists()
    assert payload["verification"]["checks"][0]["status"] == "pass"  # checks ran fine


def test_test_tampers_tracked_candidate_is_invalidated(tmp_path):
    w = _world(tmp_path, test_body="tamper")
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=True)
    assert (tmp_path / "cand" / "key.py").exists()
    _skip_unavailable_process_claim(payload["verification"])
    assert payload["verification"]["status"] == "invalidated"
    assert (tmp_path / "cand" / "key.py").read_text(encoding="utf-8") == "KEY = \"tampered\"\n"


def test_test_symlink_escape_is_invalidated(tmp_path):
    w = _world(tmp_path, test_body="symlink")
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=True)
    assert (tmp_path / "cand" / "ui.py").exists()
    _skip_unavailable_process_claim(payload["verification"])
    assert payload["verification"]["status"] == "invalidated"
    assert (tmp_path / "cand" / "escape.py").is_symlink()


# ---------------- selection discipline & staleness ---------------- --------


def test_two_cumulative_proposals_for_same_task_rejected(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _write(w.frontend, "gone.txt", "G2\n")
    _capture_publish(w.store_a, w.frontend, "prop-fe-1", "t-ui", ["ui.py"])
    _capture_publish(w.store_a, w.frontend, "prop-fe-2", "t-ui", ["gone.txt"])
    with pytest.raises(ValidationError) as excinfo:
        _candidate(w, ["prop-fe-1", "prop-fe-2"], tmp_path / "cand")
    assert "cumulative" in str(excinfo.value)
    assert not (tmp_path / "cand").exists()


def test_distinct_ids_required_and_max_bound(tmp_path):
    w = _world(tmp_path)
    with pytest.raises(ValidationError):
        _candidate(w, ["prop-x", "prop-x"], tmp_path / "cand")
    with pytest.raises(ValidationError):
        _candidate(w, [f"prop-{i}" for i in range(MAX_SELECTED_PROPOSALS + 1)], tmp_path / "cand")
    with pytest.raises(ValidationError):
        _candidate(w, [], tmp_path / "cand")


def test_stale_expected_revision_rejected(tmp_path):
    w = _world(tmp_path)
    with pytest.raises(StaleRevisionError):
        _candidate(w, ["prop-fe"], tmp_path / "cand", expected="0" * 40)


def test_stale_context_and_superseded_task_rejected(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    # context changed after the proposal was recorded
    w.store_a.update_context(
        build_context(goal="changed goal", decisions=[], interfaces={}),
        expected_revision=_head(w.store_a))
    _write_binding(w.frontend, _head(w.store_a), w.store_a.fetch_state()[1], "t-ui")
    with pytest.raises(StaleRevisionError):
        _candidate(w, ["prop-fe"], tmp_path / "cand")


def test_superseded_task_owner_rejected(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    fresh = _head(w.store_a)
    w.store_b.upsert_task(build_task(
        task_id="t-ui", owner="carol", goal="stolen work", scopes=["ui.py"],
        status="running", context_revision=w.rev1), expected_revision=fresh)
    with pytest.raises(StaleRevisionError):
        _candidate(w, ["prop-fe"], tmp_path / "cand")


def test_corrupt_before_oid_rejected(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    fresh, _state = w.store_a.fetch_state()
    proposal = capture_proposal(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"], expected_revision=fresh)
    fresh, _state = w.store_a.fetch_state()
    wire = proposal.to_dict()
    wire["files"][0]["before_oid"] = hashlib.sha1(b"WRONG\n").hexdigest()
    tampered = parse_proposal_dict(wire)
    publish_proposal(w.store_a, tampered, expected_revision=fresh)
    with pytest.raises(ValidationError) as excinfo:
        _candidate(w, ["prop-fe"], tmp_path / "cand")
    assert "before identity" in str(excinfo.value)
    assert not (tmp_path / "cand").exists()


def test_addition_claiming_existing_baseline_rejected(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    fresh, _state = w.store_a.fetch_state()
    proposal = capture_proposal(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"], expected_revision=fresh)
    fresh, _state = w.store_a.fetch_state()
    wire = proposal.to_dict()
    wire["files"][0]["before_oid"] = None
    wire["files"][0]["before_mode"] = None
    publish_proposal(w.store_a, parse_proposal_dict(wire), expected_revision=fresh)
    with pytest.raises(ValidationError):
        _candidate(w, ["prop-fe"], tmp_path / "cand")
    assert not (tmp_path / "cand").exists()


# ---------------- source immutability, placement, baseline v1 ---------------


def test_dirty_source_untouched_by_candidate(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    index_before = (w.frontend / ".git" / "index").read_bytes()
    config_before = (w.frontend / ".git" / "config").read_bytes()
    status_before = _git(["status", "--porcelain"], w.frontend)
    head_before = _git(["rev-parse", "HEAD"], w.frontend).strip()
    _candidate(w, ["prop-fe"], tmp_path / "cand")
    assert (w.frontend / ".git" / "index").read_bytes() == index_before
    assert (w.frontend / ".git" / "config").read_bytes() == config_before
    assert _git(["status", "--porcelain"], w.frontend) == status_before
    assert _git(["rev-parse", "HEAD"], w.frontend).strip() == head_before
    assert _git(["rev-parse", "--verify", "refs/heads/imece-session"], w.frontend, check=False) == ""


def test_protected_and_nonempty_output_rejected(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    pre = tmp_path / "occupied"
    pre.mkdir()
    _write(pre, "user.txt", "keep\n")
    with pytest.raises(ValidationError):
        _candidate(w, ["prop-fe"], pre)
    assert (pre / "user.txt").read_text(encoding="utf-8") == "keep\n"
    # inside the source checkout
    with pytest.raises(ValidationError):
        _candidate(w, ["prop-fe"], w.frontend / "cand-inside")
    # inside the hub (and inside the client store)
    with pytest.raises(ValidationError):
        _candidate(w, ["prop-fe"], w.hub / "cand-inside")
    with pytest.raises(ValidationError):
        _candidate(w, ["prop-fe"], w.store_a.store_path / "cand-inside")
    # symlink ancestor
    link_root = tmp_path / "linkroot"
    real = tmp_path / "real"
    real.mkdir()
    link_root.mkdir()
    os.symlink(str(real), str(link_root / "sub"))
    with pytest.raises(ValidationError):
        _candidate(w, ["prop-fe"], link_root / "sub" / "cand")
    assert not (real / "cand").exists()


def test_committed_symlink_baseline_rejected_before_output(tmp_path):
    w = _world(tmp_path, committed_symlink=True)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    output = tmp_path / "cand"
    with pytest.raises(ValidationError) as excinfo:
        _candidate(w, ["prop-fe"], output)
    assert "symbolic link" in str(excinfo.value) and "v1" in str(excinfo.value)
    assert not output.exists()


def test_malformed_remote_artifact_rejected(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    bad = tmp_path / "badrepo"
    bad.mkdir()
    _git(["init", "-q"], bad)
    _write(bad, "proposal.json", "{}\n")
    _write(bad, "extra.json", "{}\n")
    _git(["add", "-A"], bad)
    _git(["commit", "-q", "-m", "bad"], bad)
    sha = _git(["rev-parse", "HEAD"], bad).strip()
    _git(["push", "-q", str(w.hub), f"{sha}:refs/heads/imece-proposals/prop-bad"], bad)
    with pytest.raises(GitOperationError):
        _candidate(w, ["prop-bad"], tmp_path / "cand")
    assert not (tmp_path / "cand").exists()


# ---------------- CLI (in-process main + subprocess dispatch) ----------------


def _cli(argv):
    import io
    from contextlib import redirect_stdout, redirect_stderr

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli_main(argv)
    return code, out.getvalue(), err.getvalue()


def test_cli_conflict_exit_8_and_no_output(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _write(w.backend, "ui.py", "from key import KEY\nLABEL = \"banner:\" + KEY\n\nFOOTER = \"a\"\n")
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    _capture_publish(w.store_b, w.backend, "prop-be", "t-be", ["ui.py"])
    code, out, err = _cli([
        "candidate",
        "--store", str(w.store_a.store_path), "--remote", str(w.hub),
        "--project", str(w.frontend),
        "--proposal", "prop-fe", "--proposal", "prop-be",
        "--output", str(tmp_path / "cand"),
        "--expected-revision", _head(w.store_a),
    ])
    assert code == 8
    payload = json.loads(out)
    assert payload["conflicts"] == ["ui.py"]
    assert payload["conflict_paths"] == ["ui.py"]  # retained for compatibility
    assert payload["candidate_dir"] is None
    assert payload["verification"]["status"] == "not_run"
    assert payload["verification"]["checks"] == []
    assert "conflict" in err and not (tmp_path / "cand").exists()


def test_cli_candidate_not_run_exit_0_and_verification_fail_exit_9(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    base = [
        "candidate",
        "--store", str(w.store_a.store_path), "--remote", str(w.hub),
        "--project", str(w.frontend),
        "--proposal", "prop-fe",
        "--output", str(tmp_path / "cand"),
        "--expected-revision", _head(w.store_a),
    ]
    code, out, _err = _cli(base)
    assert code == 0 and json.loads(out)["verification"]["status"] == "not_run"
    shutil.rmtree(tmp_path / "cand")  # test-owned dir, recreated for the failing check run
    code, out, _err = _cli([*base, "--verify"])
    verification = json.loads(out)["verification"]
    # An incomplete BEFORE fingerprint deliberately prevents execution.  It is
    # still a verification error (CLI 9), never an expected check failure.
    assert code == 9, _verification_diagnostic(verification)
    assert verification["status"] in {"fail", "error"}, _verification_diagnostic(verification)
    if verification["status"] == "error":
        assert verification["checks"] == []
        assert verification["fingerprint_complete"] is False


def test_cli_proposal_publish_list_and_stale_publish(tmp_path):
    w = _world(tmp_path)
    _fe_edit(w)
    common = ["--store", str(w.store_a.store_path), "--remote", str(w.hub)]
    code, out, _err = _cli([
        "proposal-list", *common,
    ])
    assert code == 0 and json.loads(out)["proposals"] == []
    code, out, _err = _cli([
        "proposal-publish", *common,
        "--project", str(w.frontend),
        "--proposal-id", "prop-fe", "--task-id", "t-ui", "--path", "ui.py",
        "--expected-revision", w.rev3,
    ])
    assert code == 0
    payload = json.loads(out)
    assert payload["changed_paths"] == ["ui.py"] and "message:" not in out
    assert payload["revision"] and payload["proposal_commit"]
    code, out, _err = _cli(["proposal-list", *common])
    assert code == 0
    receipts = json.loads(out)["proposals"]
    assert receipts[0]["proposal_id"] == "prop-fe" and receipts[0]["file_count"] == 1
    # stale token: publish the same id again -> clean nonzero, never printed code
    code, _out, err = _cli([
        "proposal-publish", *common,
        "--project", str(w.frontend),
        "--proposal-id", "prop-fe-2", "--task-id", "t-ui", "--path", "ui.py",
        "--expected-revision", w.rev3,
    ])
    assert code != 0 and "current" in err


def _subprocess_run(args):
    env = _env()
    env["PATH"] = str(Path(sys.executable).resolve().parent) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, "-m", "collab_runtime", *args],
        env=env, capture_output=True, timeout=120,
    )


def test_cli_subprocess_entry_points(tmp_path):
    cp = _subprocess_run(["--help"])
    assert cp.returncode == 0 and b"candidate" in cp.stdout and b"proposal-publish" in cp.stdout
    w = _world(tmp_path)
    _fe_edit(w)
    common = ["--store", str(w.store_a.store_path), "--remote", str(w.hub)]
    cp = _subprocess_run(["proposal-list", *common])
    assert cp.returncode == 0 and json.loads(cp.stdout)["proposals"] == []
    cp = _subprocess_run([
        "proposal-publish", *common,
        "--project", str(w.frontend),
        "--proposal-id", "prop-fe", "--task-id", "t-ui", "--path", "ui.py",
        "--expected-revision", w.rev3,
    ])
    assert cp.returncode == 0 and json.loads(cp.stdout)["changed_paths"] == ["ui.py"]
    cp = _subprocess_run([
        "candidate", *common,
        "--project", str(w.frontend),
        "--proposal", "prop-fe",
        "--output", str(tmp_path / "cand"),
        "--expected-revision", _head(w.store_a),
    ])
    assert cp.returncode == 0
    payload = json.loads(cp.stdout)
    assert payload["verification"]["status"] == "not_run"
    assert (tmp_path / "cand" / "test_ui.py").exists()


# ---------------- review regressions (merge/path/fingerprint/CAS) -----------


def test_add_empty_vs_add_nonempty_conflict(tmp_path):
    """Divergent add/add conflicts even when one added file is EMPTY (an
    empty-base merge must never silently accept)."""
    w = _world(tmp_path)
    _write(w.frontend, "added.txt", "")
    _write(w.backend, "added.txt", "B1\n")
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["added.txt"])
    _capture_publish(w.store_b, w.backend, "prop-be", "t-be", ["added.txt"])
    output = tmp_path / "cand"
    with pytest.raises(CandidateConflictError) as excinfo:
        _candidate(w, ["prop-be", "prop-fe"], output)
    assert excinfo.value.conflict_paths == ["added.txt"]
    assert not output.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="backslash is a path separator on Windows, not a POSIX filename",
)
def test_backslash_baseline_filename_rejected(tmp_path):
    """A committed POSIX filename containing a backslash is NEVER silently
    normalized into another path ('a\\b.txt' must not appear as 'a/b.txt')."""
    w = _world(tmp_path, backslash_filename=True)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    output = tmp_path / "cand"
    with pytest.raises(ValidationError) as excinfo:
        _candidate(w, ["prop-fe"], output)
    assert "non-canonical" in str(excinfo.value)
    assert not output.exists()
    assert not (tmp_path / "a").exists()


def test_output_inside_unrelated_git_rejected(tmp_path):
    """`.git` components of ANY repository are rejected lexically and left
    untouched (not just source/hub/store containment)."""
    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    other = tmp_path / "otherrepo"
    other.mkdir()
    _git(["init", "-q"], other)
    git_dir = other / ".git"
    before = sorted(entry.name for entry in git_dir.iterdir())
    with pytest.raises(ValidationError) as excinfo:
        _candidate(w, ["prop-fe"], git_dir / "cand")
    assert ".git" in str(excinfo.value)
    assert sorted(entry.name for entry in git_dir.iterdir()) == before


def test_tracked_cache_looking_files_modification_invalidated(tmp_path):
    """Originally TRACKED files that look like runtime caches (.pyc, cache
    dirs) are ALWAYS fingerprinted: tampering with them is invalidated, and
    ordinary new pytest/pycache output stays ignored (flagship pass proves
    that part)."""
    w = _world(tmp_path, tracked_cache=True, test_body="mutate_cache")
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=True)
    if payload["verification"]["status"] == "error":
        assert (tmp_path / "cand" / "keep.pyc").read_text(encoding="utf-8") == "original\n"
    _skip_unavailable_process_claim(payload["verification"])
    assert payload["verification"]["status"] == "invalidated"
    assert payload["verification"]["changed_content"] is True
    assert (tmp_path / "cand" / "keep.pyc").read_text(encoding="utf-8") == "tampered\n"
    assert (tmp_path / "cand" / "__pycache__" / "tracked.txt").read_text(encoding="utf-8") == "tampered\n"


def test_fingerprint_root_symlink_never_reads_target(tmp_path):
    """A fingerprint of a symlinked ROOT is a stable marker hash: the
    symlink target is never read, so external content cannot influence it."""
    real_a = tmp_path / "real-a"
    real_b = tmp_path / "real-b"
    real_a.mkdir()
    real_b.mkdir()
    (real_a / "secret.txt").write_text("external-content-a\n")
    (real_b / "secret.txt").write_text("totally-different-b\n")
    link_a = tmp_path / "link-a"
    link_b = tmp_path / "link-b"
    os.symlink(str(real_a), str(link_a))
    os.symlink(str(real_b), str(link_b))
    fp_a = _fingerprint(link_a, ("secret.txt",))
    fp_b = _fingerprint(link_b, ("secret.txt",))
    assert fp_a == fp_b  # stable marker: target content never read
    assert fp_a != _fingerprint(real_a, ("secret.txt",))


def test_large_introduced_file_bounded_invalidation(tmp_path):
    """A check that writes an over-limit file produces a bounded honest
    invalidation (size marker instead of an unbounded read)."""
    w = _world(tmp_path, test_body="huge")
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=True)
    assert (tmp_path / "cand" / "test_ui.py").exists()
    _skip_unavailable_process_claim(payload["verification"])
    assert payload["verification"]["status"] == "invalidated"
    assert payload["verification"]["changed_content"] is True
    assert payload["verification"]["fingerprint_complete"] is False
    assert (tmp_path / "cand" / "huge.bin").stat().st_size > 8 * 1024 * 1024


def test_incomplete_before_fingerprint_errors_without_checks(tmp_path, monkeypatch):
    """An INCOMPLETE before fingerprint refuses to execute any check at all
    (entry budget monkeypatched small instead of a heavy fixture)."""
    import collab_runtime.candidates as candidates_module

    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    monkeypatch.setattr(candidates_module, "_FP_ENTRY_BUDGET", 2)
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=True)
    assert payload["verification"]["status"] == "error"
    assert payload["verification"]["checks"] == []  # never executed
    assert payload["verification"]["fingerprint_complete"] is False
    assert payload["verification"]["status"] != "pass"


def test_incomplete_after_fingerprint_never_passes_even_equal_digest(tmp_path, monkeypatch):
    """An incomplete AFTER fingerprint is invalidated even when both digests
    are equal (never a pass on incomplete evidence)."""
    import collab_runtime.candidates as candidates_module

    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    calls = {"n": 0}
    stable_digest = "ab" * 32

    def fake_records(root, originals=()):
        calls["n"] += 1
        if calls["n"] <= 2:      # content_fingerprint call + the BEFORE fingerprint
            return stable_digest, True   # complete before
        return stable_digest, False          # SAME digest, incomplete after

    monkeypatch.setattr(candidates_module, "_fingerprint_records", fake_records)
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=True)
    assert calls["n"] >= 2  # before AND after were both taken
    assert payload["verification"]["status"] == "invalidated"
    assert payload["verification"]["fingerprint_complete"] is False
    assert payload["verification"]["fingerprint_before"] == stable_digest
    assert payload["verification"]["fingerprint_after"] == stable_digest
    assert payload["verification"]["checks"]  # the checks DID execute


def test_original_dir_overhead_within_budget_passes(tmp_path):
    """Original files inside cache-named directories stay fingerprinted and
    the entry budget covers originals PLUS directory overhead: a fully
    examined candidate with tracked cache files still verifies honestly.
    (gone.txt is edited instead of ui.py so the committed pairing test
    still passes in the candidate.)"""
    w = _world(tmp_path, tracked_cache=True)  # pairing test body, no mutation
    _write(w.frontend, "gone.txt", "G2\n")
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["gone.txt"])
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=True)
    assert (tmp_path / "cand" / "keep.pyc").read_text(encoding="utf-8") == "original\n"
    # the tracked originals were materialized inside the protected cache dir
    assert (tmp_path / "cand" / "__pycache__" / "tracked.txt").read_text(encoding="utf-8") == "original\n"
    _skip_unavailable_process_claim(payload["verification"])
    assert payload["verification"]["status"] == "pass"
    assert payload["verification"]["fingerprint_complete"] is True
    assert payload["verification"]["changed_content"] is False


def test_verify_without_detected_plan_not_run(tmp_path):
    """--verify with NO detected plan is honestly not_run, never pass."""
    w = _world(tmp_path, test_body=None)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    payload = _candidate(w, ["prop-fe"], tmp_path / "cand", verify=True)
    assert payload["verification"]["status"] == "not_run"
    assert payload["verification"]["checks"] == []
    assert payload["verification"]["status"] != "pass"
    assert not (tmp_path / "cand" / "test_ui.py").exists()


def test_verify_must_be_exact_bool(tmp_path):
    """A truthy string must never authorize verification execution."""
    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    output = tmp_path / "cand"
    with pytest.raises(ValidationError):
        _candidate(w, ["prop-fe"], output, verify="false")
    assert not output.exists()


def test_race_advancing_hub_rejected_before_output(tmp_path, monkeypatch):
    """A session that advances DURING preflight/merge is rejected stale with
    no output (the head is re-checked immediately before creation)."""
    import collab_runtime.candidates as candidates_module

    w = _world(tmp_path)
    _fe_edit(w)
    _capture_publish(w.store_a, w.frontend, "prop-fe", "t-ui", ["ui.py"])
    real_merge = candidates_module._merge_changes

    def racing_merge(store, baseline, proposals):
        fresh, _state = w.store_b.fetch_state()
        w.store_b.upsert_task(build_task(
            task_id="t-x", owner="eve", goal="race", scopes=["x.py"],
            status="queued", context_revision=w.rev1,
        ), expected_revision=fresh)
        return real_merge(store, baseline, proposals)

    monkeypatch.setattr(candidates_module, "_merge_changes", racing_merge)
    output = tmp_path / "cand"
    with pytest.raises(StaleRevisionError):
        _candidate(w, ["prop-fe"], output)
    assert not output.exists()
