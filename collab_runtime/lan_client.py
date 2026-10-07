"""Direct-IP TLS client with out-of-band leaf-certificate pinning."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl

from collab_runtime.errors import ValidationError
from collab_runtime.commands import TaskCommandError
from collab_runtime.client import LoopbackSnapshotClient
from collab_runtime.consumer import RevisionConsumer, _BoundedResponse, _ProtocolError
from collab_runtime.models import MAX_JSON_BYTES, canonical_json_bytes, parse_json_bytes, parse_state_dict, safe_id, sha_hex

_PIN = re.compile(r"[0-9a-f]{64}", re.ASCII)
_CRED = re.compile(r"[A-Za-z0-9_-]{32,256}", re.ASCII)


class PairingCommandError(ValidationError):
    """Fixed safe pairing/leave failure; mutation outcome may be unknown."""
    def __init__(self, code: str, *, outcome_uncertain: bool):
        self.code = code
        self.outcome_uncertain = outcome_uncertain
        super().__init__("LAN pairing outcome is unknown; ask the owner to revoke and reissue the invitation."
                         if outcome_uncertain else "LAN pairing request was rejected.")


class PinnedLanClient:
    """Finite opt-in metadata client. No proxy, DNS, redirect, or code route."""
    def __init__(self, base_url: str, *, certificate_sha256: str, session_id: str):
        match = re.fullmatch(r"https://([0-9.]+):([1-9][0-9]{0,4})", base_url) if isinstance(base_url, str) else None
        try:
            ip = ipaddress.ip_address(match[1]) if match else None
            if (match is None or not isinstance(ip, ipaddress.IPv4Address)
                    or not (ip.is_loopback or ip in ipaddress.ip_network("10.0.0.0/8")
                            or ip in ipaddress.ip_network("172.16.0.0/12")
                            or ip in ipaddress.ip_network("192.168.0.0/16"))
                    or int(match[2]) > 65535 or not _PIN.fullmatch(certificate_sha256)):
                raise ValueError()
            self.session_id = safe_id(session_id, "session id")
        except Exception:
            raise ValidationError("a pinned HTTPS literal private IPv4 endpoint and session are required.") from None
        self.host, self.port, self.pin = str(ip), int(match[2]), certificate_sha256

    def _request(self, route: str, payload: dict, credential: str):
        if route not in {"/v1/pair", "/v1/member/leave", "/v1/snapshot", "/v1/task-status"}:
            raise ValidationError("unsupported LAN control route.")
        if not isinstance(credential, str) or not _CRED.fullmatch(credential):
            raise ValidationError("invalid LAN credential.")
        body = canonical_json_bytes(payload)
        if len(body) > MAX_JSON_BYTES:
            raise ValidationError("LAN request exceeds the protocol limit.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=5, context=context)
        connection.response_class = _BoundedResponse
        request_started = False
        try:
            connection.connect()
            cert = connection.sock.getpeercert(binary_form=True)
            if not cert or not hashlib.sha256(cert).hexdigest() == self.pin:
                raise ValidationError("LAN peer certificate pin did not match.")
            # No credential or invitation is emitted before the pin check.
            request_started = True
            connection.request("POST", route, body=body, headers={
                "Authorization": "Bearer " + credential,
                "X-Imece-Session": self.session_id,
                "Content-Type": "application/json", "Connection": "close",
            })
            response = connection.getresponse()
            headers = RevisionConsumer._response_headers(response)
            length = LoopbackSnapshotClient._content_length(headers, MAX_JSON_BYTES, allow_zero=False)
            raw = LoopbackSnapshotClient._read_exact(response, length)
            result = parse_json_bytes(raw, what="LAN control response", max_bytes=MAX_JSON_BYTES)
            if not isinstance(result, dict):
                raise ValueError()
            if response.status == 200 and route == "/v1/task-status":
                if set(result) != {"revision"}:
                    raise _ProtocolError()
                sha_hex(result["revision"], "revision")
            if response.status != 200:
                if route in {"/v1/pair", "/v1/member/leave"}:
                    if response.status in (400, 401, 403):
                        raise PairingCommandError("rejected", outcome_uncertain=False)
                    raise PairingCommandError("outcome_unknown", outcome_uncertain=True)
                if route == "/v1/task-status":
                    code = result.get("error", {}).get("code") if isinstance(result.get("error"), dict) else None
                    expected = {401: ("access_denied", "access_denied"), 403: ("access_denied", "access_denied"),
                                400: ("invalid_request", "invalid_request"), 409: ("stale_revision", "stale_revision"),
                                503: ("session_unavailable", "session_unavailable"),
                                500: ("server_error", "internal_error")}.get(response.status)
                    if expected is not None and code == expected[1]:
                        raise TaskCommandError(expected[0], outcome_uncertain=response.status in (500, 503))
                    raise TaskCommandError("protocol_error", outcome_uncertain=True)
                raise ValidationError("LAN control response was rejected.")
            return result
        except (TaskCommandError, PairingCommandError):
            raise
        except _ProtocolError:
            if route in {"/v1/pair", "/v1/member/leave"} and request_started:
                raise PairingCommandError("protocol_error", outcome_uncertain=True) from None
            if route == "/v1/task-status" and request_started:
                raise TaskCommandError("protocol_error", outcome_uncertain=True) from None
            raise ValidationError("LAN control response was rejected.") from None
        except ValidationError:
            if route in {"/v1/pair", "/v1/member/leave"} and request_started:
                raise PairingCommandError("protocol_error", outcome_uncertain=True) from None
            if route == "/v1/task-status" and request_started:
                raise TaskCommandError("protocol_error", outcome_uncertain=True) from None
            raise
        except Exception:
            if route in {"/v1/pair", "/v1/member/leave"} and request_started:
                raise PairingCommandError("outcome_unknown", outcome_uncertain=True) from None
            if route == "/v1/task-status" and request_started:
                raise TaskCommandError("outcome_unknown", outcome_uncertain=True) from None
            raise ValidationError("LAN control connection or protocol failed.") from None
        finally:
            connection.close()

    def leave(self, credential: str) -> None:
        result = self._request("/v1/member/leave", {}, credential)
        if set(result) != {"left"} or result.get("left") is not True:
            raise PairingCommandError("protocol_error", outcome_uncertain=True)

    def pair(self, invitation: str, member_id: str) -> str:
        member_id = safe_id(member_id, "member id")
        result = self._request("/v1/pair", {"member_id": member_id}, invitation)
        if set(result) != {"session_id", "member_id", "credential"}:
            raise PairingCommandError("protocol_error", outcome_uncertain=True)
        if result.get("session_id") != self.session_id or result.get("member_id") != member_id:
            raise PairingCommandError("protocol_error", outcome_uncertain=True)
        credential = result.get("credential")
        if not isinstance(credential, str) or not _CRED.fullmatch(credential):
            raise PairingCommandError("protocol_error", outcome_uncertain=True)
        return credential

    def snapshot(self, credential: str):
        result = self._request("/v1/snapshot", {}, credential)
        if set(result) != {"revision", "state"}:
            raise ValidationError("LAN snapshot response was invalid.")
        revision = sha_hex(result["revision"], "revision")
        state = parse_state_dict(result["state"])
        if state.session_id != self.session_id:
            raise ValidationError("LAN snapshot belongs to another session.")
        return revision, state

    def update_task_status(self, credential: str, *, task_id: str, status: str, expected_revision: str):
        task_id = safe_id(task_id, "task id")
        if status not in {"queued", "running", "waiting", "done"}:
            raise ValidationError("invalid task status.")
        revision = sha_hex(expected_revision, "expected revision")
        result = self._request("/v1/task-status", {
            "task_id": task_id, "status": status, "expected_revision": revision,
        }, credential)
        return result["revision"]
