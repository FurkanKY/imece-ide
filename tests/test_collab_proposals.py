"""Two-source-repo, two-store tests for collab_runtime.proposals (temp hub only).

Covers: capture of additions/modifications/deletions/mode-only changes (incl.
missing final newlines), publish + cross-client read with identical bytes and
provenance, hub artifact commits without source history, source-checkout
immutability (staged edits included), only-selected-changes transport,
ignored/credential/symlink/binary/oversize rejection, strict artifact
parsing, duplicate ids, a REAL concurrent publish race (loser stale, no
orphan proposal ref), binding staleness/tampering, task-only drift
acceptance, and sizecheck-before-read. No network, no user repos.
"""

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collab_runtime.context import SharedSnapshot, parse_snapshot_bytes  # noqa: E402
from collab_runtime.errors import (  # noqa: E402
    CollabError,
    GitOperationError,
    ProjectStateError,
    StaleRevisionError,
    ValidationError,
)
from collab_runtime.models import (  # noqa: E402
    build_context,
    build_initial_state,
    build_task,
    canonical_json_bytes,
)
from collab_runtime.proposals import (  # noqa: E402
    MAX_FILE_BYTES,
    MAX_PROPOSAL_JSON_BYTES,
    capture_proposal,
    list_proposals,
    parse_proposal_bytes,
    proposal_bytes,
    proposal_file_path,
    publish_proposal,
    read_proposal,
)
from collab_runtime.store import GitStore  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")

FE_SCOPES = ["app.py", "gone.txt", "new.py", "run.sh", "nl.txt", "lib.py", ":(top)tracked.txt", "ab:c.txt"]
BE_SCOPES = ["api.py", "db.py"]
CTX = dict(goal="ship it", decisions=["d1"], interfaces={"I": "x"})


def _env():
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(args, cwd, check=True, input=None):
    cp = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_env(), input=input, capture_output=True, timeout=30
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


def _clone(origin, target):
    _git(["clone", "-q", str(origin), str(target)], origin)
    _git(["config", "user.name", "Test"], target)
    _git(["config", "user.email", "test@example.com"], target)
    return target


def _write_binding(root, revision, state, task_id):
    snapshot = SharedSnapshot(
        revision=revision, context_hash=state.context_hash, state=state, task_id=task_id,
    )
    raw = canonical_json_bytes(snapshot.to_dict())
    parse_snapshot_bytes(raw)  # sanity: the artifact must roundtrip
    (root / ".imece").mkdir(exist_ok=True)
    (root / ".imece" / "shared-context.json").write_bytes(raw)


def _world(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q"], origin)
    _git(["config", "user.name", "Test"], origin)
    _git(["config", "user.email", "test@example.com"], origin)
    _write(origin, ".gitignore", ".imece/\nbuild/\n")
    _write(origin, "app.py", "A\n")
    _write(origin, "lib.py", "L\n")
    _write(origin, "gone.txt", "G\n")
    _write(origin, "run.sh", "#!/bin/sh\necho hi\n")
    _write(origin, "nl.txt", "x\n")
    _write(origin, "huge.txt", b"y" * (MAX_FILE_BYTES + 1))
    _write(origin, "sub/keep.txt", "K\n")
    _write(origin, ":(top)tracked.txt", "T\n")  # pathspec-shaped literal filename
    _write(origin, "ab:c.txt", "C\n")  # colon filename (cat-file rev:path separator)
    os.symlink("app.py", str(origin / "linked.py"))
    _git(["add", "-A"], origin)
    _git(["commit", "-q", "-m", "base"], origin)
    base = _git(["rev-parse", "HEAD"], origin).strip()

    frontend = _clone(origin, tmp_path / "frontend")
    _write(frontend, "ui.py", "U\n")
    _git(["add", "-A"], frontend)
    _git(["commit", "-q", "-m", "frontend work"], frontend)
    backend = _clone(origin, tmp_path / "backend")
    _write(backend, "api.py", "B\n")
    _git(["add", "-A"], backend)
    _git(["commit", "-q", "-m", "backend work"], backend)

    hub = GitStore.create_bare(tmp_path / "hub.git", what="hub")
    store_a = GitStore(store=GitStore.create_bare(tmp_path / "a.store.git", what="store"), remote=str(hub))
    store_b = GitStore(store=GitStore.create_bare(tmp_path / "b.store.git", what="store"), remote=str(hub))

    ctx = build_context(**CTX)
    rev0 = store_a.init_session(build_initial_state(
        session_id="demo-1", target_version="v1 demo", base_commit=base))
    rev1 = store_a.update_context(ctx, expected_revision=rev0)
    rev2 = store_a.upsert_task(build_task(
        task_id="t-fe", owner="alice", goal="ui work", scopes=FE_SCOPES,
        status="running", context_revision=rev1), expected_revision=rev1)
    _, state_fe = store_a.fetch_state()
    rev3 = store_b.upsert_task(build_task(
        task_id="t-be", owner="bob", goal="api work", scopes=BE_SCOPES,
        status="queued", context_revision=rev1), expected_revision=rev2)
    _, state_be = store_b.fetch_state()
    _write_binding(frontend, rev2, state_fe, "t-fe")
    _write_binding(backend, rev3, state_be, "t-be")
    return SimpleNamespace(
        hub=hub, store_a=store_a, store_b=store_b, frontend=frontend, backend=backend,
        base=base, rev0=rev0, rev1=rev1, rev2=rev2, rev3=rev3,
    )


def _mutate_frontend(world):
    """The dirty working-tree state every capture test reads (idempotent)."""
    frontend = world.frontend
    _write(frontend, "app.py", "A2\n")
    if (frontend / "gone.txt").exists():
        (frontend / "gone.txt").unlink()
    _write(frontend, "new.py", "N\n")
    (frontend / "run.sh").chmod(0o755)
    _write(frontend, "nl.txt", "x")
    return frontend


def _capture_frontend(world, proposal_id="prop-fe-1", paths=None, expected=None):
    return capture_proposal(
        world.store_a, world.frontend, proposal_id, "t-fe",
        paths if paths is not None else ["app.py", "gone.txt", "new.py", "run.sh", "nl.txt", "lib.py"],
        expected if expected is not None else world.rev3,
    )


def _hub_ref_sha(hub, ref):
    out = _git(["ls-remote", str(hub), ref], hub).strip()
    return out.split("\t", 1)[0] if out else None


def _hub_proposal_bytes(hub, proposal_id):
    sha = _hub_ref_sha(hub, f"refs/heads/imece-proposals/{proposal_id}")
    cp = subprocess.run(
        ["git", "cat-file", "blob", f"{sha}:proposal.json"],
        cwd=str(hub), env=_env(), capture_output=True, timeout=30,
    )
    assert cp.returncode == 0
    return cp.stdout


def test_capture_accepts_validated_binding_without_project_artifact(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    artifact = w.frontend / ".imece" / "shared-context.json"
    binding = parse_snapshot_bytes(artifact.read_bytes())
    artifact.unlink()

    proposal = capture_proposal(
        w.store_a, w.frontend, "bound-capture", "t-fe", ["app.py"], w.rev3,
        binding=binding,
    )
    assert proposal.context_revision == binding.revision
    with pytest.raises(CollabError):
        capture_proposal(
            w.store_a, w.frontend, "bad-binding", "t-fe", ["app.py"], w.rev3,
            binding=binding.to_dict(),
        )


# ---------------- capture: add / modify / delete / mode-only ----------------


def test_capture_add_modify_delete_and_mode_only(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    proposal = _capture_frontend(w)

    assert [f.path for f in proposal.files] == ["app.py", "gone.txt", "new.py", "nl.txt", "run.sh"]
    assert proposal.out_of_scope_paths == ()
    assert proposal.proposal_id == "prop-fe-1" and proposal.task_id == "t-fe"
    assert proposal.owner == "alice" and proposal.session_id == "demo-1"
    assert proposal.base_commit == w.base
    # provenance records the BINDING revision (what the agent saw), not the head
    assert proposal.context_revision == w.rev2
    assert proposal.context_hash == build_context(**CTX).content_hash

    (app, gone, new, nl, run) = proposal.files
    assert (app.before_oid, app.before_mode) == (_blob_sha(b"A\n"), "100644")
    assert app.after_bytes == b"A2\n" and app.after_mode == "100644"
    # deletion: before present, after both None
    assert gone.before_oid == _blob_sha(b"G\n") and gone.after_base64 is None and gone.after_mode is None
    # addition: before both None
    assert new.before_oid is None and new.before_mode is None
    assert new.after_bytes == b"N\n" and new.after_mode == "100644"
    # missing final newline is captured accurately
    assert nl.after_bytes == b"x" and nl.before_oid == _blob_sha(b"x\n")
    # mode-only change: identical content, both modes recorded
    assert run.before_mode == "100644" and run.after_mode == "100755"
    assert run.after_bytes == b"#!/bin/sh\necho hi\n"
    # unchanged selection omitted; capture is read-only for the project
    assert (w.frontend / "app.py").read_text(encoding="utf-8") == "A2\n"


def test_empty_and_unchanged_selections_rejected(tmp_path):
    w = _world(tmp_path)
    with pytest.raises(ValidationError):
        capture_proposal(w.store_a, w.frontend, "p", "t-fe", [], expected_revision=w.rev3)
    with pytest.raises(ValidationError):
        capture_proposal(w.store_a, w.frontend, "p", "t-fe", ["lib.py"], expected_revision=w.rev3)
    with pytest.raises(ValidationError):
        capture_proposal(w.store_a, w.frontend, "p", "t-fe", ["phantom.txt"], expected_revision=w.rev3)
    with pytest.raises(ValidationError):
        capture_proposal(w.store_a, w.frontend, "p", "t-fe", ["x" for _ in range(65)], expected_revision=w.rev3)


def test_out_of_scope_paths_reported_not_rejected(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    _write(w.frontend, "sneaky.txt", "S\n")
    proposal = capture_proposal(
        w.store_a, w.frontend, "prop-scope", "t-fe", ["app.py", "sneaky.txt"], expected_revision=w.rev3,
    )
    assert [f.path for f in proposal.files] == ["app.py", "sneaky.txt"]
    assert proposal.out_of_scope_paths == ("sneaky.txt",)


def test_capture_requires_project_toplevel_and_repo(tmp_path):
    w = _world(tmp_path)
    (w.frontend / "deep").mkdir()
    with pytest.raises(ValidationError):
        capture_proposal(w.store_a, w.frontend / "deep", "p", "t-fe", ["app.py"], expected_revision=w.rev3)
    with pytest.raises(ProjectStateError):
        capture_proposal(w.store_a, tmp_path / "norepo", "p", "t-fe", ["app.py"], expected_revision=w.rev3)
    with pytest.raises(ProjectStateError):
        capture_proposal(w.store_a, tmp_path / "missing", "p", "t-fe", ["app.py"], expected_revision=w.rev3)


def test_base_not_ancestor_of_head_rejected(tmp_path):
    w = _world(tmp_path)
    # an unrelated orphan history makes the session base a non-ancestor
    _git(["checkout", "-q", "--orphan", "diverged"], w.frontend)
    _git(["add", "-A"], w.frontend)
    _git(["commit", "-q", "-m", "diverged"], w.frontend)
    _write(w.frontend, "app.py", "A2\n")
    with pytest.raises(StaleRevisionError):
        capture_proposal(w.store_a, w.frontend, "p", "t-fe", ["app.py"], expected_revision=w.rev3)


# ---------------- source checkout immutability ----------------


def test_source_checkout_head_index_refs_status_config_untouched(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    _write(w.frontend, "staged.py", "S\n")
    _git(["add", "staged.py"], w.frontend)
    _write(w.frontend, "untracked.txt", "U\n")
    before_head = _git(["rev-parse", "HEAD"], w.frontend).strip()
    before_status = _git(["status", "--porcelain=v1"], w.frontend)
    before_config = _git(["config", "--local", "--list"], w.frontend)
    before_refs = _git(["for-each-ref", "--format=%(refname) %(objectname)"], w.frontend).splitlines()
    before_remotes = _git(["remote", "-v"], w.frontend)

    proposal = _capture_frontend(w, paths=["app.py"])
    publish_proposal(w.store_a, proposal, expected_revision=w.rev3)

    assert _git(["rev-parse", "HEAD"], w.frontend).strip() == before_head
    assert _git(["status", "--porcelain=v1"], w.frontend) == before_status
    assert _git(["config", "--local", "--list"], w.frontend) == before_config
    assert _git(["for-each-ref", "--format=%(refname) %(objectname)"], w.frontend).splitlines() == before_refs
    assert _git(["remote", "-v"], w.frontend) == before_remotes


# ---------------- publish / read across clients ----------------


def test_publish_and_read_across_clients_roundtrip(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    proposal = _capture_frontend(w)
    child, root = publish_proposal(w.store_a, proposal, expected_revision=w.rev3)

    # the hub carries exactly the canonical captured bytes
    assert _hub_proposal_bytes(w.hub, "prop-fe-1") == proposal_bytes(proposal)
    assert w.store_a.fetch_state()[0] == child

    read = read_proposal(w.store_b, "prop-fe-1")
    assert read.to_dict() == proposal.to_dict()
    assert proposal_bytes(read) == proposal_bytes(proposal)

    receipts = list_proposals(w.store_b)
    assert [r.proposal_id for r in receipts] == ["prop-fe-1"]
    receipt = receipts[0]
    assert (receipt.task_id, receipt.owner, receipt.file_count) == ("t-fe", "alice", 5)
    assert receipt.session_id == "demo-1" and receipt.base_commit == w.base
    assert receipt.context_hash == proposal.context_hash
    assert receipt.commit == root and receipt.context_revision == w.rev2
    # no temporary mirror refs are left behind by reads
    assert _git(["for-each-ref", "refs/imece-mirror/"], w.store_b.store_path) == ""

    # the second client can publish its own proposal on the same hub
    _write(w.backend, "api.py", "B2\n")
    be = capture_proposal(w.store_b, w.backend, "prop-be-1", "t-be", ["api.py"], expected_revision=child)
    be_child, be_root = publish_proposal(w.store_b, be, expected_revision=child)
    assert [r.proposal_id for r in list_proposals(w.store_a)] == ["prop-be-1", "prop-fe-1"]
    assert w.store_a.fetch_state()[0] == be_child
    assert _hub_proposal_bytes(w.hub, "prop-be-1") == proposal_bytes(be)


def test_only_selected_changes_reach_the_hub(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    _write(w.frontend, "secret_new_file.txt", "S\n")
    proposal = _capture_frontend(w, paths=["app.py"])
    child, _root = publish_proposal(w.store_a, proposal, expected_revision=w.rev3)

    parsed = parse_proposal_bytes(_hub_proposal_bytes(w.hub, "prop-fe-1"))
    assert [f.path for f in parsed.files] == ["app.py"]
    assert read_proposal(w.store_b, "prop-fe-1").to_dict() == proposal.to_dict()

    # git-ignored current paths without a tracked baseline are never uploaded
    _write(w.frontend, "build/out.txt", "deps\n")
    with pytest.raises(ValidationError):
        capture_proposal(w.store_a, w.frontend, "p2", "t-fe", ["build/out.txt"], expected_revision=child)
    # credential filenames are always rejected
    _write(w.frontend, ".env", "SECRET=1\n")
    for bad in (".env", ".env.local", "server.pem", "id_ed25519", ".imece/shared-context.json"):
        with pytest.raises(ValidationError):
            capture_proposal(w.store_a, w.frontend, "p2", "t-fe", [bad], expected_revision=child)


def test_hub_artifact_commit_has_no_source_history(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    proposal = _capture_frontend(w)
    _child, root = publish_proposal(w.store_a, proposal, expected_revision=w.rev3)
    sha = _hub_ref_sha(w.hub, "refs/heads/imece-proposals/prop-fe-1")
    assert sha == root

    raw = subprocess.run(
        ["git", "cat-file", "commit", sha], cwd=str(w.hub), env=_env(), capture_output=True, timeout=30,
    ).stdout
    assert b"parent " not in raw  # root commit: no source ancestors
    tree = raw.split(b"\n", 1)[0].split(b" ", 1)[1].strip()
    listing = subprocess.run(
        ["git", "ls-tree", tree], cwd=str(w.hub), env=_env(), capture_output=True, timeout=30,
    ).stdout
    assert listing.startswith(b"100644 blob ") and listing.rstrip().endswith(b"proposal.json")
    assert len([line for line in listing.splitlines() if line.strip()]) == 1


def test_duplicate_proposal_id_rejected_and_ref_immutable(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    proposal = _capture_frontend(w)
    child, root = publish_proposal(w.store_a, proposal, expected_revision=w.rev3)

    (w.frontend / "app.py").write_text("A3\n", encoding="utf-8")
    # the publication child commit is now the head: a fresh token is required
    again = _capture_frontend(w, expected=child)
    with pytest.raises(ValidationError):
        publish_proposal(w.store_a, again, expected_revision=child)
    assert _hub_ref_sha(w.hub, "refs/heads/imece-proposals/prop-fe-1") == root


def test_concurrent_publish_race_loser_rejected_no_orphan(tmp_path, monkeypatch):
    w = _world(tmp_path)
    _mutate_frontend(w)
    fe = _capture_frontend(w)
    be = _capture_frontend(w, proposal_id="prop-race")
    import collab_runtime.store as store_mod

    real_run_git = store_mod._run_git
    loser_entered, winner_done = threading.Event(), threading.Event()

    def run_git_with_parked_push(args, *, cwd, stdin=None, env=None, check=True):
        if args[0] == "push" and Path(cwd) == w.store_a.store_path:
            loser_entered.set()
            assert winner_done.wait(timeout=30)
        return real_run_git(args, cwd=cwd, stdin=stdin, env=env, check=check)

    monkeypatch.setattr(store_mod, "_run_git", run_git_with_parked_push)
    outcome = {}

    def run_loser():
        try:
            outcome["loser"] = publish_proposal(w.store_a, fe, expected_revision=w.rev3)
        except CollabError as exc:
            outcome["loser"] = exc

    thread = threading.Thread(target=run_loser)
    thread.start()
    assert loser_entered.wait(timeout=30)

    winner_child, winner_root = publish_proposal(w.store_b, be, expected_revision=w.rev3)
    winner_done.set()
    thread.join(timeout=30)

    assert isinstance(outcome["loser"], StaleRevisionError)
    # no orphan proposal ref: the atomic push took all refs or none
    assert [r.proposal_id for r in list_proposals(w.store_b)] == ["prop-race"]
    assert _hub_ref_sha(w.hub, "refs/heads/imece-proposals/prop-race") == winner_root
    assert w.store_a.fetch_state()[0] == winner_child


# ---------------- binding validity ----------------


def test_task_only_drift_keeps_binding_but_token_must_be_fresh(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    # a task-only publication (status of the OTHER task) advances the head;
    # the old binding stays usable WITHOUT a rebind, but the CAS token must
    # be the fresh head — a stale token is rejected even for task-only drift
    head = w.store_a.fetch_state()[0]
    _ = w.store_a.upsert_task(build_task(
        task_id="t-be", owner="bob", goal="api work", scopes=BE_SCOPES,
        status="running", context_revision=w.rev1), expected_revision=head)
    proposal = _capture_frontend(w, expected=w.store_a.fetch_state()[0])
    assert proposal.files
    # provenance is the binding revision (what the agent saw), never the head
    assert proposal.context_revision == w.rev2
    assert proposal.context_hash == build_context(**CTX).content_hash

    # stale CAS tokens are rejected: task-only drift is no exemption
    with pytest.raises(StaleRevisionError):
        _capture_frontend(w, expected=w.rev3)
    with pytest.raises(StaleRevisionError):
        _capture_frontend(w, expected=w.rev1)
    with pytest.raises(StaleRevisionError):
        _capture_frontend(w, expected="f" * 40)

    # a context change makes the binding itself stale until it is re-rendered
    stale_head = w.store_a.fetch_state()[0]
    w.store_a.update_context(build_context(goal="changed", decisions=[], interfaces={}), expected_revision=stale_head)
    with pytest.raises(StaleRevisionError):
        _capture_frontend(w, expected=w.store_a.fetch_state()[0])


def test_capture_then_context_change_rejects_publish(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    proposal = _capture_frontend(w)
    stale_head = w.store_a.fetch_state()[0]
    w.store_a.update_context(build_context(goal="changed", decisions=[], interfaces={}), expected_revision=stale_head)
    with pytest.raises(StaleRevisionError):
        publish_proposal(w.store_a, proposal, expected_revision=w.store_a.fetch_state()[0])


def test_tampered_binding_state_rejected(tmp_path):
    w = _world(tmp_path)
    artifact = w.frontend / ".imece" / "shared-context.json"
    tampered = parse_snapshot_bytes(artifact.read_bytes()).to_dict()
    tampered["state"]["tasks"]["t-fe"]["goal"] = "EVIL GOAL"
    raw = canonical_json_bytes(tampered)
    parse_snapshot_bytes(raw)  # the checksum alone does NOT catch this tamper
    artifact.write_bytes(raw)
    _write(w.frontend, "app.py", "A2\n")
    with pytest.raises(ValidationError):
        capture_proposal(w.store_a, w.frontend, "p", "t-fe", ["app.py"], expected_revision=w.rev3)


def test_publish_rejects_other_session_and_reassignment(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    proposal = _capture_frontend(w)
    # a second, independent hub/session cannot receive this proposal
    hub2 = GitStore.create_bare(tmp_path / "hub2.git", what="hub")
    store_c = GitStore(store=GitStore.create_bare(tmp_path / "c.store.git", what="store"), remote=str(hub2))
    rev = store_c.init_session(build_initial_state(
        session_id="other", target_version="v1 demo", base_commit=w.base))
    with pytest.raises(ValidationError):
        publish_proposal(store_c, proposal, expected_revision=rev)
    # task reassignment on the real hub blocks publication
    head = w.store_a.fetch_state()[0]
    w.store_a.upsert_task(build_task(
        task_id="t-fe", owner="mallory", goal="ui work", scopes=FE_SCOPES,
        status="running", context_revision=w.rev1), expected_revision=head)
    with pytest.raises(StaleRevisionError):
        publish_proposal(w.store_a, proposal, expected_revision=w.store_a.fetch_state()[0])


def test_publish_rejects_forged_provenance_and_superseded_task(tmp_path):
    import dataclasses

    w = _world(tmp_path)
    _mutate_frontend(w)
    proposal = _capture_frontend(w)
    assert proposal.context_revision == w.rev2 and proposal.context_hash == build_context(**CTX).content_hash

    # a forged context_revision that is not part of the published history
    with pytest.raises(ValidationError):
        publish_proposal(w.store_a, dataclasses.replace(proposal, context_revision="f" * 40),
                         expected_revision=w.rev3)
    # a recorded hash that the historical state at the recorded revision
    # refutes: the live context is unchanged, so the live-context check passes
    # and the hash-vs-history check catches the forgery (rev0 had an empty context)
    with pytest.raises(ValidationError):
        publish_proposal(w.store_a, dataclasses.replace(proposal, context_revision=w.rev0),
                         expected_revision=w.rev3)
    # a superseded task goal (owner unchanged) can never be published
    head = w.store_a.fetch_state()[0]
    w.store_a.upsert_task(build_task(
        task_id="t-fe", owner="alice", goal="EVIL GOAL", scopes=FE_SCOPES,
        status="running", context_revision=w.rev1), expected_revision=head)
    with pytest.raises(StaleRevisionError):
        publish_proposal(w.store_a, proposal, expected_revision=w.store_a.fetch_state()[0])


def test_old_proposals_stay_readable_after_context_changes(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    proposal = _capture_frontend(w)
    publish_proposal(w.store_a, proposal, expected_revision=w.rev3)
    head = w.store_a.fetch_state()[0]
    w.store_a.update_context(build_context(goal="changed", decisions=[], interfaces={}), expected_revision=head)

    # reads/listings are historical artifacts: recorded provenance vs history,
    # never the live context
    read = read_proposal(w.store_b, "prop-fe-1")
    assert read.context_revision == w.rev2
    assert read.context_hash == build_context(**CTX).content_hash
    assert [r.proposal_id for r in list_proposals(w.store_b)] == ["prop-fe-1"]


def test_read_and_list_reject_mismatched_ids_and_broken_provenance(tmp_path):
    w = _world(tmp_path)
    real_hash = build_context(**CTX).content_hash

    def planted(**overrides):
        return canonical_json_bytes(_valid_payload(**overrides))

    _plant_hub_commit(w.hub, "wrong-id", payload=planted(
        proposal_id="other-id", task_id="t-fe", owner="alice", session_id="demo-1",
        base_commit=w.base, context_revision=w.rev2, context_hash=real_hash))
    _plant_hub_commit(w.hub, "ghost-task", payload=planted(
        proposal_id="ghost-task", task_id="t-ghost", owner="alice", session_id="demo-1",
        base_commit=w.base, context_revision=w.rev2, context_hash=real_hash))
    _plant_hub_commit(w.hub, "foreign-rev", payload=planted(
        proposal_id="foreign-rev", task_id="t-fe", owner="alice", session_id="demo-1",
        base_commit=w.base, context_revision="f" * 40, context_hash=real_hash))
    _plant_hub_commit(w.hub, "bad-hash", payload=planted(
        proposal_id="bad-hash", task_id="t-fe", owner="alice", session_id="demo-1",
        base_commit=w.base, context_revision=w.rev2, context_hash="d" * 64))

    with pytest.raises(GitOperationError):
        read_proposal(w.store_a, "wrong-id")     # embedded id != ref id
    with pytest.raises(GitOperationError):
        read_proposal(w.store_a, "ghost-task")   # task absent from historical state
    with pytest.raises(GitOperationError):
        read_proposal(w.store_a, "foreign-rev")  # revision outside published history
    with pytest.raises(GitOperationError):
        read_proposal(w.store_a, "bad-hash")     # hash refuted by historical state
    with pytest.raises(GitOperationError):
        list_proposals(w.store_a)                # receipts validate the same provenance


# ---------------- unsafe / unsupported paths and contents ----------------


def test_unsafe_and_unsupported_paths_rejected(tmp_path):
    w = _world(tmp_path)
    os.symlink("/etc/hostname", str(w.frontend / "link.py"))
    os.symlink("realdir", str(w.frontend / "linkdir"))
    (w.frontend / "realdir").mkdir()
    _write(w.frontend, "realdir/f.txt", "F\n")
    _write(w.frontend, "bin.py", b"\x00\x01\x02")
    _write(w.frontend, "bad.py", b"\xff\xfe")
    _write(w.frontend, "big.txt", b"x" * (MAX_FILE_BYTES + 1))

    bad_paths = [
        "../outside.txt", "a/../b.txt", "/absolute.txt", "C:/drive.txt",
        ".git/config", ".gitignore/../../../x", "sub/", "", ".", "a*b", "a?b",
        "link.py", "linkdir/f.txt", "bin.py", "bad.py", "big.txt",
        ".env.production", "host.key", "creds.pem", "credentials.json",
        "a\x00b", "a\nb",
        # committed in the base tree as a directory / a symlink: unsupported v1 shapes
        "sub", "linked.py",
    ]
    for bad in bad_paths:
        with pytest.raises(ValidationError):
            capture_proposal(w.store_a, w.frontend, "p-bad", "t-fe", [bad], expected_revision=w.rev3)


def test_literal_pathspec_filenames_captured_exactly(tmp_path):
    w = _world(tmp_path)
    _write(w.frontend, ":(top)tracked.txt", "T2\n")
    _write(w.frontend, "ab:c.txt", "C2\n")
    proposal = capture_proposal(
        w.store_a, w.frontend, "prop-literal", "t-fe",
        [":(top)tracked.txt", "ab:c.txt"], expected_revision=w.rev3,
    )
    # pathspec magic must never leak into the query: exact literal baselines
    top, colon = proposal.files
    assert (top.path, colon.path) == (":(top)tracked.txt", "ab:c.txt")
    assert (top.before_oid, top.before_mode) == (_blob_sha(b"T\n"), "100644")
    assert top.after_bytes == b"T2\n"
    assert colon.before_oid == _blob_sha(b"C\n") and colon.after_bytes == b"C2\n"


def test_store_and_hub_inside_source_rejected_at_capture(tmp_path):
    w = _world(tmp_path)
    _mutate_frontend(w)
    hub_inside = GitStore.create_bare(w.frontend / "hub-inside.git", what="hub")
    store_for_inside_hub = GitStore(
        store=GitStore.create_bare(tmp_path / "s-hub.git", what="store"), remote=str(hub_inside))
    with pytest.raises(ValidationError):
        capture_proposal(store_for_inside_hub, w.frontend, "p", "t-fe", ["app.py"], expected_revision=w.rev3)
    store_in_source = GitStore(
        store=GitStore.create_bare(w.frontend / "store-inside.git", what="store"), remote=str(w.hub))
    with pytest.raises(ValidationError):
        capture_proposal(store_in_source, w.frontend, "p", "t-fe", ["app.py"], expected_revision=w.rev3)
    assert list_proposals(w.store_b) == []  # nothing was published anywhere


def test_lstat_permission_error_is_validation_error_not_deletion(tmp_path, monkeypatch):
    import collab_runtime.proposals as proposals_mod

    w = _world(tmp_path)
    _write(w.frontend, "locked.txt", "L\n")
    real_lstat = os.lstat

    def deny(path, *args, **kwargs):
        if str(path).endswith("locked.txt"):
            raise PermissionError(13, "permission denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(proposals_mod.os, "lstat", deny)
    with pytest.raises(ValidationError) as excinfo:
        capture_proposal(w.store_a, w.frontend, "p", "t-fe", ["locked.txt"], expected_revision=w.rev3)
    # an access error must never degrade into an empty/deletion capture
    assert "empty" not in str(excinfo.value)
    assert list_proposals(w.store_b) == []  # nothing was published


def test_oversize_baseline_rejected_for_modification_allowed_for_deletion(tmp_path):
    w = _world(tmp_path)
    # huge.txt (> MAX_FILE_BYTES) is tracked in the session base commit
    _write(w.frontend, "huge.txt", b"z" * 10)
    with pytest.raises(ValidationError):
        capture_proposal(w.store_a, w.frontend, "p1", "t-fe", ["huge.txt"], expected_revision=w.rev3)
    (w.frontend / "huge.txt").unlink()
    proposal = capture_proposal(w.store_a, w.frontend, "p2", "t-fe", ["huge.txt"], expected_revision=w.rev3)
    assert proposal.files[0].after_base64 is None
    assert proposal.files[0].before_mode == "100644"


# ---------------- strict artifact parsing ----------------


def _valid_payload(**overrides):
    payload = {
        "schema": 1,
        "proposal_id": "prop-1", "task_id": "t-fe", "owner": "alice",
        "session_id": "demo-1", "base_commit": "a" * 40,
        "context_revision": "b" * 40, "context_hash": "c" * 64,
        "files": [
            {"path": "a.txt", "before_oid": None, "before_mode": None,
             "after_base64": base64.b64encode(b"hi\n").decode("ascii"), "after_mode": "100644"},
        ],
    }
    payload.update(overrides)
    return payload


def test_proposal_parser_accepts_valid_and_shapes(tmp_path):
    parsed = parse_proposal_bytes(canonical_json_bytes(_valid_payload()))
    assert parsed.files[0].after_bytes == b"hi\n"
    shapes = [
        {"path": "a.txt", "before_oid": "a" * 40, "before_mode": "100755",
         "after_base64": None, "after_mode": None},  # deletion
        {"path": "b.txt", "before_oid": "a" * 40, "before_mode": "100644",
         "after_base64": base64.b64encode(b"x").decode("ascii"), "after_mode": "100755"},  # mode-only
    ]
    payload = _valid_payload(files=shapes)
    parsed = parse_proposal_bytes(canonical_json_bytes(payload))
    assert [f.path for f in parsed.files] == ["a.txt", "b.txt"]


def test_proposal_parser_rejects_hostile_payloads():
    good = canonical_json_bytes(_valid_payload())
    parse_proposal_bytes(good)  # baseline sanity

    bad_payloads = [
        _valid_payload(schema="1"),
        _valid_payload(schema=True),
        _valid_payload(extra=1),
        _valid_payload(files=[{"path": "a.txt", "before_oid": None, "before_mode": None,
                               "after_base64": base64.b64encode(b"h").decode(), "after_mode": "100644",
                               "extra": 1}]),
        _valid_payload(files=[]),
        _valid_payload(files=[{"path": "a.txt"}]),
        _valid_payload(files=[
            {"path": "b.txt", "before_oid": None, "before_mode": None,
             "after_base64": base64.b64encode(b"x").decode(), "after_mode": "100644"},
            {"path": "a.txt", "before_oid": None, "before_mode": None,
             "after_base64": base64.b64encode(b"x").decode(), "after_mode": "100644"},
        ]),  # unsorted
        _valid_payload(files=[
            {"path": "a.txt", "before_oid": None, "before_mode": None,
             "after_base64": base64.b64encode(b"x").decode(), "after_mode": "100644"},
            {"path": "a.txt", "before_oid": None, "before_mode": None,
             "after_base64": base64.b64encode(b"y").decode(), "after_mode": "100644"},
        ]),  # duplicate path
        _valid_payload(files=[{"path": "a.txt", "before_oid": None, "before_mode": "100644",
                               "after_base64": None, "after_mode": None}]),  # mixed before
        _valid_payload(files=[{"path": "a.txt", "before_oid": None, "before_mode": None,
                               "after_base64": None, "after_mode": None}]),  # neither add nor delete
        _valid_payload(files=[{"path": "a.txt", "before_oid": "zz", "before_mode": "100644",
                               "after_base64": base64.b64encode(b"x").decode(), "after_mode": "100644"}]),
        _valid_payload(files=[{"path": "a.txt", "before_oid": None, "before_mode": None,
                               "after_base64": "aGVsbG9=", "after_mode": "100644"}]),  # bad padding
        _valid_payload(files=[{"path": "a.txt", "before_oid": None, "before_mode": None,
                               "after_base64": "AB==", "after_mode": "100644"}]),  # non-canonical
        _valid_payload(files=[{"path": "a.txt", "before_oid": None, "before_mode": None,
                               "after_base64": base64.b64encode(b"\x00x").decode(), "after_mode": "100644"}]),
        _valid_payload(files=[{"path": "a.txt", "before_oid": None, "before_mode": None,
                               "after_base64": base64.b64encode(b"x" * (MAX_FILE_BYTES + 1)).decode(),
                               "after_mode": "100644"}]),  # over per-file limit
        _valid_payload(files=[{"path": "a.txt", "before_oid": None, "before_mode": None,
                               "after_base64": base64.b64encode(b"x").decode(), "after_mode": "100754"}]),
        _valid_payload(proposal_id="../evil"),
        _valid_payload(context_hash="c" * 63),
        _valid_payload(files=[{"path": "../evil.txt", "before_oid": None, "before_mode": None,
                               "after_base64": base64.b64encode(b"x").decode(), "after_mode": "100644"}]),
        _valid_payload(files=[{"path": ".env", "before_oid": None, "before_mode": None,
                               "after_base64": base64.b64encode(b"x").decode(), "after_mode": "100644"}]),
    ]
    for payload in bad_payloads:
        with pytest.raises(ValidationError):
            parse_proposal_bytes(canonical_json_bytes(payload))

    hostile = [
        b'{"schema":1,"schema":2,"files":[]}',  # duplicate keys
        b'{"schema":1,"files":[],"x":1.5}',  # float value
        b'{"schema":NaN,"files":[]}',  # NaN constant
        b'{"schema":1,"proposal_id":"\\ud800","files":[]}',  # unpaired surrogate
        b'{"schema":1,"big":' + b"9" * 5000 + b"}",  # over-limit integer literal
        b"{not json",
        b"[]" * 10,
        b'{"schema":1,' + b'"x":1,' * 100 + b'}',  # unknown field
    ]
    for raw in hostile:
        with pytest.raises(ValidationError):
            parse_proposal_bytes(raw)
    with pytest.raises(ValidationError):
        parse_proposal_bytes(b"{" + b"x" * (MAX_PROPOSAL_JSON_BYTES + 1) + b"}")  # over 2MiB
    # a valid-charset base64 string longer than the per-file bound is rejected
    # BEFORE any decoding work
    oversize_b64 = base64.b64encode(b"z" * (MAX_FILE_BYTES + 1)).decode("ascii")
    payload = _valid_payload(files=[{
        "path": "a.txt", "before_oid": None, "before_mode": None,
        "after_base64": oversize_b64, "after_mode": "100644",
    }])
    with pytest.raises(ValidationError):
        parse_proposal_bytes(canonical_json_bytes(payload))


def test_proposal_file_path_validation():
    for good in ("a/b.txt", "Ünïcode.txt", "sp ace.txt", "a.b-c_d.txt", ".hidden.txt", "a.envx"):
        assert proposal_file_path(good) == good
    for bad in ("../x", "a/../b", "/abs", "C:/x", ".git/config", ".imece/x", ".env", ".env.local",
                "a.pem", "a.KEY", "id_rsa", "id_ed25519", "credentials.json", "dir/", "", ".",
                "a*b", "a?b", "a[b", "a\x00b", "a\nb", "a\x7fb", 5, None):
        with pytest.raises(ValidationError):
            proposal_file_path(bad)


# ---------------- sizecheck-before-read on the hub ----------------


def _plant_hub_commit(hub, proposal_id, *, payload, parents=(), extra_names=()):
    env = _env()
    cp = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(hub), input=payload,
        env=env, capture_output=True, timeout=30,
    )
    assert cp.returncode == 0
    blob = cp.stdout.decode("ascii").strip()
    lines = [f"100644 blob {blob}\tproposal.json"]
    lines += [f"100644 blob {blob}\t{name}" for name in extra_names]
    cp = subprocess.run(["git", "mktree"], cwd=str(hub), input="\n".join(lines).encode() + b"\n",
                        env=env, capture_output=True, timeout=30)
    assert cp.returncode == 0
    tree = cp.stdout.decode("ascii").strip()
    args = ["commit-tree", tree, "-m", "planted"]
    for parent in parents:
        args += ["-p", parent]
    cp = subprocess.run(["git", *args], cwd=str(hub), env=env, capture_output=True, timeout=30)
    assert cp.returncode == 0
    commit = cp.stdout.decode("ascii").strip()
    _git(["update-ref", f"refs/heads/imece-proposals/{proposal_id}", commit], hub)
    return commit


def test_oversize_artifact_rejected_before_read(tmp_path):
    w = _world(tmp_path)
    huge = b'{"schema":1,"x":"' + b"y" * (MAX_PROPOSAL_JSON_BYTES + 1) + b'"}'
    _plant_hub_commit(w.hub, "too-big", payload=huge)
    with pytest.raises(ValidationError):
        read_proposal(w.store_a, "too-big")


def test_structurally_invalid_artifacts_rejected(tmp_path):
    w = _world(tmp_path)
    session_head = _hub_ref_sha(w.hub, "refs/heads/imece-session")
    payload = canonical_json_bytes(_valid_payload())
    _plant_hub_commit(w.hub, "with-parent", payload=payload, parents=[session_head])
    _plant_hub_commit(w.hub, "extra-tree", payload=payload, extra_names=["other.json"])
    for bad in ("with-parent", "extra-tree"):
        with pytest.raises(GitOperationError):
            read_proposal(w.store_a, bad)


# ---------------- misc transport guarantees ----------------


def test_list_and_read_require_existing_proposal(tmp_path):
    w = _world(tmp_path)
    assert list_proposals(w.store_a) == []
    with pytest.raises(ValidationError):
        read_proposal(w.store_a, "ghost")
