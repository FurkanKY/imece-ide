"""Explicit, bounded participant code transport; never writes Source."""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from pathlib import Path

from collab_runtime.errors import ValidationError
from collab_runtime.lan_proposals import TlsProposalClient, ProposalChannelError
from collab_runtime.proposals import capture_proposal_from_session_state, proposal_bytes


class PeerDelivery:
    def __init__(self, manager, *, client_factory=TlsProposalClient, clock=time.monotonic):
        self.manager, self.factory, self.clock = manager, client_factory, clock
        self._lock = threading.Lock()
        self._tickets = {}

    def forget(self):
        with self._lock:
            self._tickets.clear()

    def _session(self, root, generation, handle):
        with self.manager._lock:
            record = self.manager._record(root, generation, handle)
            if record['metadata'].get('state') != 'active' or not record['token']:
                raise ValidationError('Refresh participant access before sharing code.')
            token = record['token']
        top, head = self.manager._root(root)
        if str(top) != record['root'] or head != record['base']:
            raise ValidationError('Source baseline changed.')
        return record, token

    def _current(self, record):
        with self.manager._lock:
            if self.manager._active is not record or not record['token']:
                raise ValidationError('Participant access changed during sharing.')

    def _client(self, record):
        binding = record['bundle']
        return self.factory(binding['proposalEndpoint'], certificate_sha256=binding['certificateSha256'],
                            session_id=record['session'])

    @staticmethod
    def _digest(proposal):
        return hashlib.sha256(proposal_bytes(proposal)).hexdigest()

    def _capture(self, record, token, source, proposal_id, task_id, paths):
        revision, state = record['client'].snapshot(token)
        if state.session_id != record['session'] or state.base_commit != record['base']:
            raise ValidationError('Shared session identity changed.')
        task = state.tasks.get(task_id)
        if task is None or task.owner != record['member'] or task.status == 'done':
            raise ValidationError('Select an active shared task assigned to you.')
        self._current(record)
        proposal = capture_proposal_from_session_state(source, proposal_id, task_id, paths,
                                  session_state=state, context_revision=revision)
        self._current(record)
        return proposal, revision

    def preview(self, root, generation, handle, *, task_id, paths, source_root=None,
                source_guard=None, source_run_id=None):
        record, token = self._session(root, generation, handle)
        source = Path(source_root or root)
        guard = source_guard or (lambda: None)
        guard()
        proposal, revision = self._capture(record, token, source, 'proposal_' + uuid.uuid4().hex,
                                           task_id, paths)
        guard()
        ticket_id = uuid.uuid4().hex
        ticket = dict(id=ticket_id, record=record, proposal=proposal, revision=revision,
                      source=source, guard=guard, paths=tuple(paths), expires=self.clock() + 300,
                      source_run_id=source_run_id, state='preview', digest=self._digest(proposal))
        # Same lock order as detach: never reinstall a ticket after capability cleanup.
        with self.manager._lock:
            if self.manager._active is not record or not record['token']:
                raise ValidationError('Participant access changed during sharing.')
            with self._lock:
                if any(t['record'] is record and t['state'] in {'unknown', 'checking'} for t in self._tickets.values()):
                    raise ValidationError('Reconcile the previous immutable proposal ID before preparing another.')
                self._tickets = {key: t for key, t in self._tickets.items()
                                 if t['state'] in {'unknown', 'checking'} or t['expires'] > self.clock()}
                if len(self._tickets) >= 4:
                    raise ValidationError('Discard an unused proposal preview before preparing another.')
                self._tickets[ticket_id] = ticket
        return self._dto(ticket)

    @staticmethod
    def _dto(ticket):
        proposal = ticket['proposal']
        return {'ticketId': ticket['id'], 'proposalId': proposal.proposal_id,
                'taskId': proposal.task_id, 'owner': proposal.owner, 'sessionId': proposal.session_id,
                'contextRevision': proposal.context_revision, 'contextHash': proposal.context_hash,
                'paths': [file.path for file in proposal.files], 'outOfScopePaths': list(proposal.out_of_scope_paths),
                'fileCount': len(proposal.files), 'artifactSha256': ticket['digest'],
                'sourceRunId': ticket['source_run_id'], 'state': ticket['state']}

    def _ticket(self, root, generation, handle, ticket_id):
        record, token = self._session(root, generation, handle)
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None or ticket['record'] is not record:
                raise ValidationError('Proposal preview is not owned by this participant session.')
            if ticket['state'] != 'unknown' and ticket['expires'] <= self.clock():
                raise ValidationError('Proposal preview expired; explicitly capture a new preview.')
        return record, token, ticket

    def publish(self, root, generation, handle, ticket_id, *, confirm, allow_out_of_scope=False):
        record, token, ticket = self._ticket(root, generation, handle, ticket_id)
        with self._lock:
            if confirm is not True or ticket['state'] != 'preview':
                raise ValidationError('Explicit sharing confirmation and an unused preview are required.')
            if ticket['proposal'].out_of_scope_paths and allow_out_of_scope is not True:
                raise ValidationError('Explicitly acknowledge out-of-scope selected paths.')
            ticket['state'] = 'checking'
        sent = False
        try:
            ticket['guard']()
            fresh, revision = self._capture(record, token, ticket['source'], ticket['proposal'].proposal_id,
                                            ticket['proposal'].task_id, ticket['paths'])
            if revision != ticket['revision'] or self._digest(fresh) != ticket['digest']:
                raise ValidationError('Selected code or shared metadata changed after preview; capture again.')
            ticket['guard']()
            self._current(record)
            client = self._client(record)
            sent = True
            receipt = client.publish(ticket['proposal'], expected_revision=revision, credential=token)
            with self._lock:
                ticket['state'] = 'published'
            return {**self._dto(ticket), 'publication': receipt}
        except Exception as exc:
            uncertain = sent and (not isinstance(exc, ProposalChannelError) or exc.outcome_uncertain)
            with self._lock:
                ticket['state'] = 'unknown' if uncertain else 'preview'
            if uncertain:
                raise ValidationError('peer_publish_uncertain: reconcile the SAME proposal ID; do not blindly republish.') from None
            raise

    def reconcile(self, root, generation, handle, ticket_id):
        record, token, ticket = self._ticket(root, generation, handle, ticket_id)
        if ticket['state'] != 'unknown':
            raise ValidationError('Only an ambiguous publication needs reconciliation.')
        proposal = self._client(record).fetch(ticket['proposal'].proposal_id, credential=token)
        self._current(record)
        if self._digest(proposal) != ticket['digest']:
            raise ValidationError('The fetched immutable proposal does not match the captured artifact.')
        with self._lock:
            ticket['state'] = 'published'
        return self._dto(ticket)

    def fetch(self, root, generation, handle, proposal_id):
        record, token = self._session(root, generation, handle)
        proposal = self._client(record).fetch(proposal_id, credential=token)
        self._current(record)
        return {'proposalId': proposal.proposal_id, 'taskId': proposal.task_id, 'owner': proposal.owner,
                'sessionId': proposal.session_id, 'contextRevision': proposal.context_revision,
                'contextHash': proposal.context_hash, 'fileCount': len(proposal.files),
                'paths': [file.path for file in proposal.files], 'artifactSha256': self._digest(proposal)}

    def discard(self, root, generation, handle, ticket_id):
        # Local disposal stays possible after preview expiry or a HEAD change.
        with self.manager._lock:
            record = self.manager._record(root, generation, handle)
            with self._lock:
                ticket = self._tickets.get(ticket_id)
                if ticket is None or ticket['record'] is not record:
                    raise ValidationError('Proposal preview is not owned by this participant session.')
                if ticket['state'] in {'unknown', 'checking'}:
                    raise ValidationError('Reconcile ambiguous publication before discarding its identity.')
                self._tickets.pop(ticket_id, None)
        return {}
