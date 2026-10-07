"""Bounded local ticket lifecycle and explicit out-of-scope publication."""
import pytest

from collab_runtime.errors import ValidationError
from tests.test_m4_delivery import world


def test_expired_preview_can_be_discarded_without_recapturing(world):
    _source, clone, _owner, peer, joined = world
    ticket = peer.delivery.preview(clone, 1, joined['peerHandle'], task_id='task-a', paths=['a.txt'])
    now = peer.delivery.clock()
    peer.delivery.clock = lambda: now + 301
    with pytest.raises(ValidationError, match='expired'):
        peer.delivery.publish(clone, 1, joined['peerHandle'], ticket['ticketId'], confirm=True)
    peer.delivery.discard(clone, 1, joined['peerHandle'], ticket['ticketId'])
    assert not peer.delivery._tickets


def test_detach_during_capture_cannot_reinstall_preview(world):
    _source, clone, _owner, peer, joined = world
    calls = []
    def guard():
        calls.append(1)
        if len(calls) == 2:
            peer.forget_project(None)
    with pytest.raises(ValidationError, match='access changed'):
        peer.delivery.preview(clone, 1, joined['peerHandle'], task_id='task-a', paths=['a.txt'], source_guard=guard)
    assert peer._active is None and not peer.delivery._tickets


def test_out_of_scope_requires_separate_confirmation(world):
    _source, clone, _owner, peer, joined = world
    ticket = peer.delivery.preview(clone, 1, joined['peerHandle'], task_id='task-a', paths=['unselected-secret.txt'])
    assert ticket['outOfScopePaths'] == ['unselected-secret.txt']
    with pytest.raises(ValidationError, match='out-of-scope'):
        peer.delivery.publish(clone, 1, joined['peerHandle'], ticket['ticketId'], confirm=True)
    result = peer.delivery.publish(clone, 1, joined['peerHandle'], ticket['ticketId'], confirm=True, allow_out_of_scope=True)
    assert result['state'] == 'published'
