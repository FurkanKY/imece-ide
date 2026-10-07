# M4 engineering in progress: opt-in pinned TLS metadata control

This is an experimental library/transport and explicit participant metadata UI
slice, not a finished LAN product. The milestone contract remains
[PRODUCT-PLAN.md](PRODUCT-PLAN.md). M3 native acceptance remains **deferred**;
this work does not close M3. Real two-machine M4 acceptance remains open.

## Surface and guarantees

`LoopbackServer(coordinator)` is unchanged by default: HTTP on literal
`127.0.0.1`. LAN requires every caller to explicitly pass
`allow_lan=True`, a literal RFC1918 IPv4 address (or loopback for local tests),
and explicit certificate/private-key paths. Wildcards, DNS names, public IPs,
missing TLS material, TLS below 1.2, and plaintext fallback are refused. TLS
handshakes run inside the existing bounded request workers, not the accept loop.
There is no import-time listener, application auto-start or UI enablement.

`PinnedLanClient` accepts only a literal private IPv4 HTTPS URL, expected
session ID, and out-of-band lowercase SHA-256 of the expected leaf certificate
in DER form. It uses a direct socket (no proxy/redirect), pins the certificate
before sending even an invitation credential, has finite socket and framed-body
limits, and only exposes pairing, metadata snapshot, and task-status update
methods. The participant UI accepts a pasted owner bundle only after explicit
out-of-band pin confirmation, and exposes metadata refresh/disconnect only. Its
bearer capability is process-memory-only. Self-leave revokes only that ephemeral
member identity; no participant task/context writes are exposed. Shared-context
writes remain owner-only; task status retains Coordinator's existing member/task-owner checks.

The local owner explicitly calls `issue_invitation(owner_credential, member_id)`.
Invites contain 256 bits of random entropy, expire after five monotonic minutes,
are pre-bound to a member ID, and are one-use with at most eight pending. Redeem
creates a distinct random member credential stored only as a hash in Coordinator.
No owner credential is shared. Redemption, explicit revocation and close cleanup
serialize under a single pairing-to-coordinator lock order, so concurrent redeem
cannot escape revocation. A server close revokes all identities minted by that
listener and clears unused invites; pairing does not persist or resume after
restart. Explicit owner revocation is available while the listener is active.
TLS private-key loading uses a noninteractive empty-password callback; encrypted
keys are refused instead of prompting from a listener setup path. Every LAN
request carries exactly one configured session-binding header, checked before
body parsing/domain I/O.

Explicit proposal bytes now use the separate second listener described in
[M4-PROPOSAL-CHANNEL.md](M4-PROPOSAL-CHANNEL.md); they are not smuggled through
metadata/control messages. Both local integration fixtures use a temporary
self-signed certificate and only `127.0.0.1`. They do not test real NIC/firewall,
multiple machines, public networks, Windows or actual providers. Certificate
creation/distribution/trust UX, remote participant UI, durable member management,
LAN operational threat review, full Source integration and two-host acceptance
remain open, including real two-machine M4 acceptance. M3 supported-platform/native
acceptance remains deferred and is not complete.

## Reproduce

```sh
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen \
  PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_lan.py tests/test_collab_commands.py \
  tests/test_collab_command_regressions.py tests/test_collab_coordinator.py \
  tests/test_collab_client_regressions.py tests/test_collab_transport.py
```

Tests generate a temporary certificate with the system OpenSSL executable; no
certificate, private key, credential, invite or test database is retained in the
repository. Do not expose the experimental listener to an untrusted network.
