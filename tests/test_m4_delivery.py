"""Real two-checkout TLS sharing -> durable isolated candidate -> apply/rollback.
This is a loopback fixture, not real two-machine acceptance.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from change_runtime.candidate import CombinedCandidates
from collab_runtime.errors import ValidationError
from collab_runtime.lan_proposals import TlsProposalClient, ProposalChannelError
from collab_runtime.owner import OwnerSessionManager
from collab_runtime.peer import PeerSessionManager
from run_runtime import RunRuntime, RunStore


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def world(tmp_path):
    if not shutil.which('openssl'):
        pytest.skip('Existing OpenSSL required for disposable TLS fixture')
    source = tmp_path / 'owner-source'
    source.mkdir()
    git(source, 'init', '-q')
    git(source, 'config', 'user.name', 'Test')
    git(source, 'config', 'user.email', 'test@example.invalid')
    (source / 'a.txt').write_text('original\n')
    (source / '.gitignore').write_text('.imece/checkpoints/\n')
    (source / '.imece').mkdir()
    (source / '.imece/verify.json').write_text(json.dumps([{'id': 'content', 'title': 'Exact content',
        'argv': [sys.executable, '-c', "from pathlib import Path; assert Path('a.txt').read_text() == 'changed\\n'"],
        'timeout_ms': 5000}]))
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'common baseline')
    clone = tmp_path / 'peer-source'
    subprocess.run(['git', 'clone', '-q', str(source), str(clone)], check=True)
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
        '-out', str(cert), '-days', '1', '-subj', '/CN=localhost'], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    owner = OwnerSessionManager(tmp_path / 'metadata')
    preview = owner.preview_create(source, session_id='m4', target_version='v1', goal='Share safely',
        owner_id='owner', member_ids=['owner', 'peer'], tasks=[{'id': 'task-a', 'owner': 'peer',
        'goal': 'Edit A', 'scopes': ['a.txt']}])
    owner.create(preview['previewId'], source)
    owner.start(source, allow_lan=True, bind_address='127.0.0.1', certificate=str(cert), private_key=str(key))
    peer = PeerSessionManager()
    invite = owner.issue_lan_invitation('peer')
    joined = peer.join(clone, 1, invite, confirm_pin=True, pin=invite['certificateSha256'])
    (clone / 'a.txt').write_text('changed\n')
    (clone / 'unselected-secret.txt').write_text('DO_NOT_TRANSPORT')
    try:
        yield source, clone, owner, peer, joined
    finally:
        peer.disconnect()
        owner.stop()


def capture(world):
    _source, clone, _owner, peer, joined = world
    return peer.delivery.preview(clone, 1, joined['peerHandle'], task_id='task-a', paths=['a.txt'])


def test_real_tls_explicit_share_verified_apply_reopen_rollback(world, tmp_path):
    source, clone, owner, peer, joined = world
    preview = capture(world)
    assert preview['paths'] == ['a.txt'] and 'token' not in preview and 'code' not in preview
    with pytest.raises(ValidationError):
        peer.delivery.publish(clone, 1, joined['peerHandle'], preview['ticketId'], confirm=False)
    result = peer.delivery.publish(clone, 1, joined['peerHandle'], preview['ticketId'], confirm=True)
    assert result['state'] == 'published'
    fetched = peer.delivery.fetch(clone, 1, joined['peerHandle'], preview['proposalId'])
    assert fetched['paths'] == ['a.txt'] and fetched['artifactSha256'] == preview['artifactSha256']
    assert (source / 'a.txt').read_text() == 'original\n'
    assert not (source / 'unselected-secret.txt').exists()
    identity = owner.shared_candidate_store(source, expected_session_id='m4', expected_epoch=owner.status()['epoch'])
    runtime = RunRuntime(RunStore(tmp_path / 'runs.sqlite3'))
    context = lambda root, provenance: owner.validate_shared_candidate(root, provenance)
    service = CombinedCandidates(runtime, tmp_path / 'candidates', owner_context_supplier=context)
    receipt = service.prepare_shared(source, identity['store'], [preview['proposalId']], verify=True,
        expected_revision=identity['revision'], session_id='m4', epoch=identity['epoch'],
        store_path=identity['storePath'], hub_path=identity['hubPath'])
    assert receipt['verification']['status'] == 'pass'
    assert (source / 'a.txt').read_text() == 'original\n'
    assert (clone / 'unselected-secret.txt').read_text() == 'DO_NOT_TRANSPORT'
    reopened = CombinedCandidates(RunRuntime(RunStore(runtime.store.db_path)), service.output_base,
                                  owner_context_supplier=context)
    reopened.apply(str(source), receipt['candidateId'])
    assert (source / 'a.txt').read_text() == 'changed\n'
    reopened.rollback(str(source), receipt['candidateId'])
    assert (source / 'a.txt').read_text() == 'original\n'
    assert not git(source, 'status', '--porcelain')


@pytest.mark.parametrize('change', ['source', 'shared_context'])
def test_published_shared_candidate_refuses_stale_apply(world, tmp_path, change):
    source, clone, owner, peer, joined = world
    preview = capture(world)
    peer.delivery.publish(clone, 1, joined['peerHandle'], preview['ticketId'], confirm=True)
    identity = owner.shared_candidate_store(source, expected_session_id='m4', expected_epoch=owner.status()['epoch'])
    service = CombinedCandidates(RunRuntime(RunStore(tmp_path / 'runs.sqlite3')), tmp_path / 'candidates',
        owner_context_supplier=lambda root, p: owner.validate_shared_candidate(root, p))
    receipt = service.prepare_shared(source, identity['store'], [preview['proposalId']], verify=True,
        expected_revision=identity['revision'], session_id='m4', epoch=identity['epoch'],
        store_path=identity['storePath'], hub_path=identity['hubPath'])
    if change == 'source':
        (source / 'a.txt').write_text('newer user edit\n')
    else:
        owner._coordinator.update_context(owner._credentials['owner'],
            {'goal': 'Changed intent', 'decisions': [], 'interfaces': {}}, expected_revision=identity['revision'])
    before = (source / 'a.txt').read_bytes()
    with pytest.raises(Exception):
        service.apply(str(source), receipt['candidateId'])
    assert (source / 'a.txt').read_bytes() == before


def test_changed_preview_and_uncertain_publication_same_id_reconcile(world):
    _source, clone, _owner, peer, joined = world
    preview = capture(world)
    (clone / 'a.txt').write_text('later\n')
    with pytest.raises(ValidationError):
        peer.delivery.publish(clone, 1, joined['peerHandle'], preview['ticketId'], confirm=True)
    (clone / 'a.txt').write_text('changed\n')
    actual = peer.delivery.factory
    class LostReceipt(TlsProposalClient):
        def publish(self, *args, **kwargs):
            super().publish(*args, **kwargs)
            raise ProposalChannelError('outcome_unknown', outcome_uncertain=True)
    peer.delivery.factory = LostReceipt
    with pytest.raises(ValidationError, match='peer_publish_uncertain'):
        peer.delivery.publish(clone, 1, joined['peerHandle'], preview['ticketId'], confirm=True)
    with pytest.raises(ValidationError, match='Reconcile'):
        capture(world)
    peer.delivery.factory = actual
    reconciled = peer.delivery.reconcile(clone, 1, joined['peerHandle'], preview['ticketId'])
    assert reconciled['state'] == 'published' and reconciled['proposalId'] == preview['proposalId']


def test_wrong_owner_and_private_paths_never_share(world):
    _source, clone, _owner, peer, joined = world
    for paths, task in [(['../outside'], 'task-a'), (['.env'], 'task-a'), (['a.txt'], 'no-task')]:
        with pytest.raises(ValidationError):
            peer.delivery.preview(clone, 1, joined['peerHandle'], task_id=task, paths=paths)
