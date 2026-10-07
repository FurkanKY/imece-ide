"""Opt-in TLS LAN control pairing. Proposal bytes are deliberately out of scope."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field

from collab_runtime.errors import AccessDeniedError, ValidationError

_MAX_INVITATIONS = 8
_TTL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class Invitation:
    member_id: str
    code: str = field(repr=False)
    expires_in_seconds: int = int(_TTL_SECONDS)


class InvitationRegistry:
    """Bounded, one-use, pre-bound invitations with monotonic expiry."""
    def __init__(self, coordinator, *, clock=time.monotonic):
        self._coordinator = coordinator
        self._clock = clock
        self._lock = threading.RLock()
        self._pending: dict[bytes, tuple[str, float]] = {}
        self._ephemeral_members: dict[str, bytes] = {}
        self._closed = False

    @staticmethod
    def _digest(code: str) -> bytes:
        return hashlib.sha256(code.encode("ascii", "strict")).digest()

    def issue(self, owner_credential: str, member_id: str) -> Invitation:
        # Use the same registry -> coordinator order as redemption/revocation.
        with self._lock:
            if self._closed:
                raise ValidationError("pairing is closed.")
            self._coordinator.validate_new_member(owner_credential, member_id)
            now = self._clock()
            self._pending = {key: value for key, value in self._pending.items() if value[1] > now}
            if len(self._pending) >= _MAX_INVITATIONS:
                raise ValidationError("the pending invitation limit was reached.")
            if any(value[0] == member_id for value in self._pending.values()):
                raise ValidationError("a pending invitation already exists for this member.")
            code = secrets.token_urlsafe(32)
            self._pending[self._digest(code)] = (member_id, now + _TTL_SECONDS)
            return Invitation(member_id, code, int(_TTL_SECONDS))

    def check(self, code: str) -> None:
        if type(code) is not str or not 40 <= len(code) <= 128:
            raise AccessDeniedError("invalid invitation")
        try:
            digest = self._digest(code)
        except UnicodeEncodeError:
            raise AccessDeniedError("invalid invitation") from None
        with self._lock:
            item = next((value for key, value in self._pending.items() if hmac.compare_digest(key, digest)), None)
            if self._closed or item is None or item[1] <= self._clock():
                raise AccessDeniedError("invalid invitation")

    def redeem(self, code: str, member_id: str) -> str:
        try:
            digest = self._digest(code)
        except (AttributeError, UnicodeEncodeError):
            raise AccessDeniedError("invalid invitation") from None
        with self._lock:
            item = next(((key, value) for key, value in self._pending.items()
                         if hmac.compare_digest(key, digest)), None)
            if self._closed or item is None or item[1][1] <= self._clock() or item[1][0] != member_id:
                raise AccessDeniedError("invalid invitation")
            del self._pending[item[0]]
            credential = secrets.token_urlsafe(32)
            self._coordinator.register_member_credential_internal(member_id, credential)
            self._ephemeral_members[member_id] = hashlib.sha256(credential.encode("ascii")).digest()
            return credential

    def leave(self, credential: str) -> None:
        """Allow only an ephemeral member to revoke its own credential."""
        with self._lock:
            try:
                member_id = self._coordinator.authenticate_member(credential)
            except AccessDeniedError:
                raise AccessDeniedError("access denied") from None
            if member_id == self._coordinator._owner_id:
                raise AccessDeniedError("access denied")
            expected_hash = self._ephemeral_members.get(member_id)
            try:
                supplied_hash = hashlib.sha256(credential.encode("ascii", "strict")).digest()
            except (AttributeError, UnicodeEncodeError):
                raise AccessDeniedError("access denied") from None
            if expected_hash is None or not hmac.compare_digest(expected_hash, supplied_hash):
                raise AccessDeniedError("access denied")
            self._coordinator.revoke_member_credential_internal(member_id, expected_hash=expected_hash)
            self._ephemeral_members.pop(member_id, None)

    def revoke(self, owner_credential: str, member_id: str) -> None:
        """Serialize revocation with redemption using registry -> coordinator lock order."""
        with self._lock:
            self._coordinator.revoke_member_credential(owner_credential, member_id)
            self._ephemeral_members.pop(member_id, None)
            self._pending = {key: value for key, value in self._pending.items()
                             if value[0] != member_id}

    def cancel(self, owner_credential: str, code: str) -> None:
        """Discard one pending nonce; never revoke a replacement member token."""
        with self._lock:
            if self._coordinator.authenticate_member(owner_credential) != self._coordinator._owner_id:
                raise AccessDeniedError("access denied")
            if type(code) is not str or not 40 <= len(code) <= 128:
                raise ValidationError("invalid invitation")
            self._pending.pop(self._digest(code), None)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            members = tuple(self._ephemeral_members.items())
            self._ephemeral_members.clear()
            # Keep the pairing lock until revocations commit. Redeem uses the
            # same registry -> coordinator lock order, so close cannot race a
            # credential registration past its cleanup snapshot.
            for member, expected_hash in members:
                self._coordinator.revoke_member_credential_internal(member, expected_hash=expected_hash)
