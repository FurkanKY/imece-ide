import os
import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from collab_runtime.context import SharedSnapshot
from collab_runtime.delivery import DeliveryConflictError, DeliveryError, SharedDeliveryService
from collab_runtime.models import build_context, build_initial_state, build_task
from collab_runtime.store import GitStore
from workspace.worktree import GitWorktreeWorkspace


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@local",
                               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@local"}).stdout.decode().strip()


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git(["init", "-q"], root)
    git(["config", "user.name", "Test"], root)
    git(["config", "user.email", "test@local"], root)
    (root / "app.py").write_text("base\n")
    (root / "other.py").write_text("untouched\n")
    git(["add", "-A"], root)
    git(["commit", "-qm", "base"], root)
    base = git(["rev-parse", "HEAD"], root)
    (root / "app.py").write_text("source-only WIP\n")
    workspace = GitWorktreeWorkspace.create(
        source_root=root, run_id="delivery-run", base_dir=tmp_path / "worktrees")
    hub = GitStore.create_bare(tmp_path / "hub.git")
    store_dir = GitStore.create_bare(tmp_path / "store.git", what="store")
    store = GitStore(store=store_dir, remote=str(hub))
    rev = store.init_session(build_initial_state(session_id="s1", target_version="v1", base_commit=base))
    rev = store.update_context(build_context(goal="goal", decisions=[], interfaces={}), expected_revision=rev)
    rev = store.upsert_task(build_task(task_id="t1", owner="m1", goal="task", scopes=["app.py"],
                                       status="running", context_revision=rev), expected_revision=rev)
    head, state = store.fetch_state()
    binding = SharedSnapshot(head, state.context_hash, state, "t1")
    session = SimpleNamespace(project_root=root, run_id="run-1", active=False,
                              accepted_binding=binding,
                              status=lambda: {"sessionId": "s1", "taskId": "t1", "memberId": "m1"})
    yield SimpleNamespace(root=root, hub=hub, store_dir=store_dir, store=store, binding=binding,
                          workspace=workspace, session=session, base=base, tmp=tmp_path)
    workspace.dispose()


def source_state(root):
    return {
        "head": git(["rev-parse", "HEAD"], root),
        "status": git(["status", "--porcelain=v1", "-z"], root),
        "index": (root / ".git" / "index").read_bytes(),
        "refs": git(["for-each-ref", "--format=%(refname) %(objectname)"], root),
        "config": git(["config", "--local", "--list"], root),
        "files": ((root / "app.py").read_bytes(), (root / "other.py").read_bytes()),
    }


def test_preview_is_explicit_no_publish_and_confirm_uses_frozen_capture(world):
    service = SharedDeliveryService()
    workroot = world.workspace.root
    (workroot / "app.py").write_text("captured\n")
    (workroot / "other.py").write_text("not selected\n")
    before_source = source_state(world.root)
    before = world.store.fetch_state()[0]
    preview = service.preview_publication(world.session, world.workspace, store_path=world.store_dir,
        hub_path=world.hub, paths=["app.py"])
    assert world.store.fetch_state()[0] == before
    assert preview["paths"] == ["app.py"] and "fileContent" not in preview
    assert not world.store.remote_proposal_head(preview["proposalId"])
    (workroot / "app.py").write_text("changed after preview\n")
    published = service.confirm_publication(preview["previewId"], run_id="run-1", project_root=world.root)
    assert published["proposalId"] == preview["proposalId"]
    artifact = world.store.fetch_proposal_ref(preview["proposalId"])[1]
    content = base64.b64decode(json.loads(artifact)["files"][0]["after_base64"])
    assert content == b"captured\n"
    assert world.store.fetch_state()[0] == published["sessionRevision"]
    assert source_state(world.root) == before_source


def test_requires_successfully_accepted_binding_and_idle_run(world):
    service = SharedDeliveryService()
    world.session.accepted_binding = None
    with pytest.raises(DeliveryError):
        service.preview_publication(world.session, world.workspace, store_path=world.store_dir,
                                    hub_path=world.hub, paths=["app.py"])
    world.session.accepted_binding = world.binding
    world.session.active = True
    with pytest.raises(DeliveryError) as error:
        service.preview_publication(world.session, world.workspace, store_path=world.store_dir,
                                    hub_path=world.hub, paths=["app.py"])
    assert error.value.code == "busy"


def test_empty_capture_is_invalid_not_a_context_drift(world):
    (world.workspace.root / "app.py").write_text("base\n")
    with pytest.raises(DeliveryError) as error:
        SharedDeliveryService().preview_publication(
            world.session, world.workspace, store_path=world.store_dir,
            hub_path=world.hub, paths=["app.py"],
        )
    assert error.value.code == "invalid"


def test_out_of_scope_requires_explicit_confirmation_and_failure_discards_ticket(world):
    service = SharedDeliveryService()
    (world.workspace.root / "other.py").write_text("changed\n")
    preview = service.preview_publication(world.session, world.workspace, store_path=world.store_dir,
        hub_path=world.hub, paths=["other.py"])
    assert preview["outOfScopePaths"] == ["other.py"]
    with pytest.raises(DeliveryError):
        service.confirm_publication(preview["previewId"], run_id="run-1", project_root=world.root)
    # The missing acknowledgment is a correctable user choice, so the ticket
    # stays usable only for explicit path-by-path acknowledgement.
    service.confirm_publication(preview["previewId"], run_id="run-1", project_root=world.root,
                                allow_out_of_scope=True)
    assert world.store.remote_proposal_head(preview["proposalId"])


@pytest.mark.parametrize("path", [".env", "../outside", "*.py", ".imece/shared-context.json"])
def test_unsafe_selection_rejected(world, path):
    with pytest.raises(DeliveryError):
        SharedDeliveryService().preview_publication(world.session, world.workspace,
            store_path=world.store_dir, hub_path=world.hub, paths=[path])


def test_ticket_ttl_and_run_root_binding(world):
    clock = [10.0]
    service = SharedDeliveryService(clock=lambda: clock[0], ttl_seconds=3)
    (world.workspace.root / "app.py").write_text("changed\n")
    preview = service.preview_publication(world.session, world.workspace, store_path=world.store_dir,
        hub_path=world.hub, paths=["app.py"])
    with pytest.raises(DeliveryError):
        service.confirm_publication(preview["previewId"], run_id="other", project_root=world.root)
    preview = service.preview_publication(world.session, world.workspace, store_path=world.store_dir,
        hub_path=world.hub, paths=["app.py"])
    clock[0] += 4
    with pytest.raises(DeliveryError) as error:
        service.confirm_publication(preview["previewId"], run_id="run-1", project_root=world.root)
    assert error.value.code == "expired"


def test_listing_metadata_only_and_candidate_no_verification_by_default(world):
    service = SharedDeliveryService()
    (world.workspace.root / "app.py").write_text("change\n")
    preview = service.preview_publication(world.session, world.workspace, store_path=world.store_dir,
        hub_path=world.hub, paths=["app.py"])
    service.confirm_publication(preview["previewId"], run_id="run-1", project_root=world.root)
    receipts = service.list_shared_proposals(store_path=world.store_dir, hub_path=world.hub,
                                             project_root=world.root, binding=world.binding)
    assert receipts[0]["proposalId"] == preview["proposalId"]
    assert "files" not in receipts[0] and "content" not in receipts[0]
    output = world.tmp / "candidate"
    receipt = service.assemble_shared_candidate(project_root=world.root, store_path=world.store_dir,
        hub_path=world.hub, binding=world.binding, proposal_ids=[preview["proposalId"]], output_path=output)
    assert (output / "app.py").read_text() == "change\n"
    assert receipt["verification"]["status"] == "not_run"


def test_context_mutation_stales_publication_without_orphan_ref(world):
    service = SharedDeliveryService()
    (world.workspace.root / "app.py").write_text("change\n")
    preview = service.preview_publication(world.session, world.workspace, store_path=world.store_dir,
        hub_path=world.hub, paths=["app.py"])
    head = world.store.fetch_state()[0]
    world.store.update_context(build_context(goal="new context", decisions=[], interfaces={}),
                               expected_revision=head)
    with pytest.raises(DeliveryError):
        service.confirm_publication(preview["previewId"], run_id="run-1", project_root=world.root)
    assert not world.store.remote_proposal_head(preview["proposalId"])


def test_source_commit_after_preview_rejects_without_publication(world):
    service = SharedDeliveryService()
    (world.workspace.root / "app.py").write_text("captured\n")
    preview = service.preview_publication(world.session, world.workspace, store_path=world.store_dir,
        hub_path=world.hub, paths=["app.py"])
    (world.root / "new-source-file.txt").write_text("user commit\n")
    git(["add", "new-source-file.txt"], world.root)
    git(["commit", "-qm", "user commit"], world.root)
    with pytest.raises(DeliveryError) as error:
        service.confirm_publication(preview["previewId"], run_id="run-1", project_root=world.root)
    assert error.value.code == "stale"
    assert not world.store.remote_proposal_head(preview["proposalId"])


def test_constructor_and_verification_flag_are_strict(world):
    with pytest.raises(ValueError):
        SharedDeliveryService(ticket_limit=0)
    for invalid_ttl in (float("nan"), float("inf"), 10 ** 1000, 601):
        with pytest.raises(ValueError):
            SharedDeliveryService(ttl_seconds=invalid_ttl)
    with pytest.raises(ValueError):
        SharedDeliveryService(clock=None)
    (world.workspace.root / "app.py").write_text("change\n")
    service = SharedDeliveryService()
    preview = service.preview_publication(world.session, world.workspace, store_path=world.store_dir,
        hub_path=world.hub, paths=["app.py"])
    service.confirm_publication(preview["previewId"], run_id="run-1", project_root=world.root)
    with pytest.raises(DeliveryError):
        service.assemble_shared_candidate(project_root=world.root, store_path=world.store_dir,
            hub_path=world.hub, binding=world.binding, proposal_ids=[preview["proposalId"]],
            output_path=world.tmp / "bad-candidate", verify=1)
