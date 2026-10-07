"""Delivery RPC completion and durable shared-candidate owner authorization."""
import threading
from types import SimpleNamespace

import pytest

from collab_runtime.errors import ValidationError
from webhost import state
from webhost.api import peer as peer_api
from webhost.api import candidate as candidate_api
from tests.test_m4_delivery import world


class Context:
    def __init__(self):
        self.event = threading.Event()
        self.result = self.error = None
    def resolve(self, value):
        self.result = value
        self.event.set()
    def fail(self, code, message):
        self.error = (code, message)
        self.event.set()


@pytest.mark.parametrize('route,params', [
    ('_preview_proposal', {'taskId': 'task-a', 'paths': ['a.txt']}),
    ('_publish_proposal', {'ticketId': 'b' * 32, 'confirm': True}),
    ('_reconcile_proposal', {'ticketId': 'b' * 32}),
    ('_fetch_proposal', {'proposalId': 'proposal-a'}),
    ('_discard_proposal', {'ticketId': 'b' * 32}),
])
def test_each_async_delivery_failure_completes_rpc(tmp_path, monkeypatch, route, params):
    root = str(tmp_path.resolve())
    monkeypatch.setattr(state, 'get_project', lambda: SimpleNamespace(root=root))
    monkeypatch.setattr(state, 'project_generation', lambda: 11)
    def fail(*args, **kwargs):
        raise ValidationError('peer_publish_uncertain: SECRET_CODE_DO_NOT_EXPOSE')
    delivery = SimpleNamespace(preview=fail, publish=fail, reconcile=fail, fetch=fail, discard=fail)
    monkeypatch.setattr(state, 'get_peer_manager', lambda: SimpleNamespace(delivery=delivery))
    ctx = Context()
    getattr(peer_api, route)({**params, 'peerHandle': 'a' * 32}, ctx)
    assert ctx.event.wait(3)
    assert ctx.error[0] == 'peer_publish_uncertain'
    assert 'SECRET_CODE' not in ctx.error[1]


def test_shared_prepare_rpc_then_apply_uses_owner_context_return(world, tmp_path, monkeypatch):
    source, clone, owner, peer, joined = world
    preview = peer.delivery.preview(clone, 1, joined['peerHandle'], task_id='task-a', paths=['a.txt'])
    peer.delivery.publish(clone, 1, joined['peerHandle'], preview['ticketId'], confirm=True)
    state.set_project(str(source))
    monkeypatch.setattr(state, 'get_owner_manager', lambda: owner)
    from run_runtime import RunRuntime, RunStore
    runtime = RunRuntime(RunStore(tmp_path / 'runs.sqlite3'))
    monkeypatch.setattr(state, 'get_run_runtime', lambda: runtime)
    monkeypatch.setattr(candidate_api, 'workspaces_dir', lambda: tmp_path / 'workspaces')
    identity = owner.shared_candidate_store(source, expected_session_id='m4', expected_epoch=owner.status()['epoch'])
    ctx = Context()
    candidate_api._prepare_shared({'projectRoot': str(source), 'expectedSessionId': 'm4',
        'expectedEpoch': identity['epoch'], 'expectedRevision': identity['revision'],
        'proposalIds': [preview['proposalId']], 'verify': True}, ctx)
    assert ctx.event.wait(15), 'asynchronous prepare never completed'
    assert ctx.error is None, ctx.error
    root, service = candidate_api._context()
    receipt = ctx.result['candidate']
    service.apply(root, receipt['candidateId'])
    assert (source / 'a.txt').read_text() == 'changed\n'
    service.rollback(root, receipt['candidateId'])
    assert (source / 'a.txt').read_text() == 'original\n'
