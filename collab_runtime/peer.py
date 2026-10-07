"""Private, process-memory participant session capability (metadata only)."""
from __future__ import annotations

import copy
import ipaddress
import hmac
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from collab_runtime.errors import ValidationError
from collab_runtime.lan_client import PairingCommandError, PinnedLanClient
from collab_runtime.models import safe_id
from collab_runtime.store import GitStore

_PIN = re.compile(r"[0-9a-f]{64}", re.ASCII)
_REQUIRED = {"memberId", "code", "controlEndpoint", "proposalEndpoint", "certificateSha256", "sessionId"}
_OPTIONAL = {"expiresInSeconds", "epoch"}


def _endpoint(value: Any) -> tuple[str, int]:
    if type(value) is not str or len(value) > 64:
        raise ValidationError("the invitation bundle is invalid.")
    match = re.fullmatch(r"https://([0-9.]+):([1-9][0-9]{0,4})", value)
    try:
        ip = ipaddress.ip_address(match[1]) if match else None
        port = int(match[2]) if match else 0
    except ValueError:
        ip, port = None, 0
    allowed = isinstance(ip, ipaddress.IPv4Address) and (ip.is_loopback or
              ip in ipaddress.ip_network("10.0.0.0/8") or
              ip in ipaddress.ip_network("172.16.0.0/12") or
              ip in ipaddress.ip_network("192.168.0.0/16"))
    if not allowed or not 1 <= port <= 65535:
        raise ValidationError("the invitation bundle is invalid.")
    return str(ip), port


def validate_bundle(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, dict) or not _REQUIRED <= set(bundle) or set(bundle) - (_REQUIRED | _OPTIONAL):
        raise ValidationError("the invitation bundle is invalid.")
    try:
        member = safe_id(bundle["memberId"], "member id")
        session = safe_id(bundle["sessionId"], "session id")
        code = bundle["code"]
        expiry, epoch = bundle.get("expiresInSeconds"), bundle.get("epoch")
        if (type(code) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", code, re.ASCII)
                or (expiry is not None and (type(expiry) is not int or not 1 <= expiry <= 300))
                or (epoch is not None and (type(epoch) is not int or epoch < 0))
                or type(bundle["certificateSha256"]) is not str
                or not _PIN.fullmatch(bundle["certificateSha256"])):
            raise ValueError
        control, proposal = _endpoint(bundle["controlEndpoint"]), _endpoint(bundle["proposalEndpoint"])
        if control[0] != proposal[0] or control[1] == proposal[1]:
            raise ValueError
    except Exception:
        raise ValidationError("the invitation bundle is invalid.") from None
    return dict(bundle)


class PeerSessionManager:
    """One explicitly admitted peer; credentials never leave this object."""
    def __init__(self, *, client_factory=PinnedLanClient):
        self._factory = client_factory
        self._lock = threading.RLock()
        self._active: dict[str, Any] | None = None
        self._closed = False
        self._cleanup_slots = threading.BoundedSemaphore(2)
        self._fence = 0
        self._joining = False
        from collab_runtime.peer_delivery import PeerDelivery
        self.delivery = PeerDelivery(self)

    @staticmethod
    def _root(root: str | Path) -> tuple[Path, str]:
        try:
            supplied = Path(root).resolve(strict=True)
            top = GitStore.project_toplevel(supplied)
            if supplied != top:
                raise ValueError
            head = GitStore.project_head(top)
            if not re.fullmatch(r"[0-9a-f]{40}", head):
                raise ValueError
            return top, head
        except Exception:
            raise ValidationError("a committed Git repository top-level is required.") from None

    @staticmethod
    def _metadata(handle: str, root: Path, epoch: int, member: str, bundle: dict,
                  revision: str, state) -> dict:
        return {"peerHandle": handle, "projectRoot": str(root), "sessionId": state.session_id,
                "baseCommit": state.base_commit, "memberId": member, "revision": revision,
                "context": state.context.to_dict(),
                "tasks": [{"id": task.id, "owner": task.owner, "goal": task.goal,
                           "scopes": list(task.scopes), "status": task.status,
                           "contextRevision": task.context_revision}
                          for task in state.tasks.values()],
                "controlEndpoint": bundle["controlEndpoint"],
                "proposalEndpoint": bundle["proposalEndpoint"],
                "certificateSha256": bundle["certificateSha256"], "epoch": epoch,
                "state": "active", "error": None}

    def join(self, root: str | Path, generation: int, bundle: Any, *, confirm_pin: bool,
             pin: str | None = None) -> dict:
        parsed = validate_bundle(bundle)
        if (confirm_pin is not True or type(pin) is not str or not _PIN.fullmatch(pin)
                or not hmac.compare_digest(pin, parsed["certificateSha256"])):
            raise ValidationError("confirm the certificate fingerprint out of band before pairing.")
        top, head = self._root(root)
        with self._lock:
            if self._closed:
                raise ValidationError("participant sessions are unavailable during shutdown.")
            if self._joining or self._active is not None:
                raise ValidationError("disconnect the existing participant session before joining another.")
            self._joining = True
            fence = self._fence
        client = None
        token = None
        try:
            client = self._factory(parsed["controlEndpoint"], certificate_sha256=parsed["certificateSha256"],
                                   session_id=parsed["sessionId"])
            token = client.pair(parsed["code"], parsed["memberId"])
            after_root, after_head = self._root(top)
            if after_root != top or after_head != head:
                raise ValidationError("the source project HEAD changed during pairing.")
            revision, state = client.snapshot(token)
            after_root, after_head = self._root(top)
            if after_root != top or after_head != head or state.base_commit != head or state.session_id != parsed["sessionId"]:
                raise ValidationError("the shared session does not match this project's committed HEAD.")
            handle = uuid.uuid4().hex
            epoch = parsed.get("epoch", 0)
            record = {"handle": handle, "root": str(top), "generation": generation,
                      "member": parsed["memberId"], "session": parsed["sessionId"],
                      "base": state.base_commit, "epoch": epoch,
                      # Never keep the invitation code after redemption.
                      "bundle": {key: value for key, value in parsed.items() if key != "code"},
                      "client": client, "token": token,
                      "metadata": self._metadata(handle, top, epoch, parsed["memberId"], parsed, revision, state)}
            with self._lock:
                if self._closed or fence != self._fence or not self._joining or self._active is not None:
                    raise ValidationError("project changed while pairing; participant admission was canceled.")
                self._active = record
                token = None  # capability now owned by exact active record
            return copy.deepcopy(record["metadata"])
        except Exception as exc:
            if isinstance(exc, PairingCommandError) and exc.outcome_uncertain:
                raise ValidationError("peer_pairing_uncertain: pairing may have reached the owner; ask the owner to revoke this invitation and issue a new one.") from None
            if token is not None and client is not None:
                try:
                    client.leave(token)
                except Exception:
                    raise ValidationError("peer_cleanup_uncertain: pairing was not confirmed and leave could not be verified; ask the owner to revoke access.") from None
            raise
        finally:
            with self._lock:
                self._joining = False

    def refresh(self, root: str | Path, generation: int, handle: str) -> dict:
        with self._lock:
            record = self._record(root, generation, handle)
        try:
            top, head = self._root(root)
            if str(top) != record["root"] or head != record["base"]:
                raise ValidationError("the source project HEAD changed; participant metadata is unavailable.")
            revision, state = record["client"].snapshot(record["token"])
            top_after, head_after = self._root(root)
            if (str(top_after) != record["root"] or head_after != record["base"]
                    or state.session_id != record["session"] or state.base_commit != record["base"]):
                raise ValidationError("the shared session identity changed; participant metadata is unavailable.")
            metadata = self._metadata(handle, top, record["epoch"], record["member"],
                                      record["bundle"], revision, state)
            with self._lock:
                if self._active is not record:
                    raise ValidationError("the participant session was detached during refresh.")
                record["metadata"] = metadata
                return copy.deepcopy(metadata)
        except Exception as exc:
            with self._lock:
                if self._active is record:
                    record["metadata"] = {"peerHandle": handle, "projectRoot": record["root"],
                                           "sessionId": record["session"], "memberId": record["member"],
                                           "epoch": record["epoch"], "state": "unknown", "error": "refresh_failed"}
            if isinstance(exc, ValidationError) and str(exc).startswith("peer_cleanup_uncertain:"):
                raise
            raise ValidationError("participant metadata could not be refreshed; reconnect or ask the owner to revoke access.") from None

    def status(self, root: str | Path, generation: int, handle: str) -> dict:
        with self._lock:
            record = self._record(root, generation, handle)
        try:
            top, head = self._root(root)
            source_changed = str(top) != record["root"] or head != record["base"]
        except ValidationError:
            source_changed = True
        with self._lock:
            if self._active is not record:
                raise ValidationError("the participant session is no longer active for this project.")
            if source_changed:
                record["metadata"] = {"peerHandle": handle, "projectRoot": record["root"],
                                       "sessionId": record["session"], "memberId": record["member"],
                                       "epoch": record["epoch"], "state": "unknown", "error": "source_changed"}
            return copy.deepcopy(record["metadata"])

    def _detach(self, handle: str | None = None):
        with self._lock:
            record = self._active
            if record is None or (handle is not None and handle != record["handle"]):
                return None
            self._active = None
            self.delivery.forget()
            record["token"], token = None, record["token"]
            return record, token

    @staticmethod
    def _leave(detached) -> dict:
        if detached is None:
            return {"disconnected": True, "warning": None}
        record, token = detached
        try:
            record["client"].leave(token)
            warning = None
        except Exception:
            warning = "peer_cleanup_uncertain: local access was forgotten; ask the owner to revoke access."
        return {"disconnected": True, "warning": warning}

    def disconnect(self, root: str | Path | None = None, generation: int | None = None,
                   handle: str | None = None) -> dict:
        with self._lock:
            record = self._active
            if record is None or (handle is not None and handle != record["handle"]):
                return {"disconnected": True, "warning": None}
            if root is not None and (record["root"] != str(Path(root).resolve()) or record["generation"] != generation):
                raise ValidationError("the participant session is no longer active for this project.")
        return self._leave(self._detach(handle))

    def _schedule_leave(self, detached) -> None:
        if detached is None or not self._cleanup_slots.acquire(blocking=False):
            return
        def work():
            try:
                self._leave(detached)
            finally:
                self._cleanup_slots.release()
        try:
            threading.Thread(target=work, name="peer-leave", daemon=True).start()
        except Exception:
            self._cleanup_slots.release()

    def forget_project(self, root: str | Path | None = None) -> dict | None:
        with self._lock:
            self._fence += 1
            record = self._active
            if record is None or (root is not None and record["root"] != str(Path(root).resolve())):
                return None
            handle = record["handle"]
        detached = self._detach(handle)
        if detached is None:
            return None
        self._schedule_leave(detached)
        return {"disconnected": True, "warning": None}

    def shutdown(self) -> dict | None:
        with self._lock:
            if self._closed:
                return None
            self._closed = True
            self._fence += 1
            handle = self._active["handle"] if self._active else None
        if handle is None:
            return None
        detached = self._detach(handle)
        self._schedule_leave(detached)
        return {"disconnected": True, "warning": None}

    def _record(self, root: str | Path, generation: int, handle: str) -> dict:
        item = self._active
        if (item is None or type(handle) is not str or item["handle"] != handle
                or item["root"] != str(Path(root).resolve()) or item["generation"] != generation):
            raise ValidationError("the participant session is no longer active for this project.")
        return item
