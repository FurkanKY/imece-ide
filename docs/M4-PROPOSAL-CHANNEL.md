# M4 second engineering slice: separate TLS proposal-byte channel

This is an opt-in experimental transport adapter, not an accepted two-machine
product. M3 remains **in progress** with Windows/native desktop acceptance
explicitly deferred; this work does not mark it complete. See
[PRODUCT-PLAN.md](PRODUCT-PLAN.md) for milestone authority.

## Interface and authority

`TlsProposalServer` is a distinct, explicitly constructed TLS listener, separate
from the metadata/control `LoopbackServer`. It requires the same kind of
literal private IPv4 bind and explicit TLS certificate/key. `TlsProposalClient`
uses a direct socket to a literal private IP and verifies the out-of-band
SHA-256 DER leaf-certificate pin before sending any member credential or
proposal bytes. Both client and server bind every request to exactly one
configured session header. No listener starts from import, app startup, or a
control pairing action.

The only proposal routes are an explicit `POST /v1/proposal/publish` containing
one strict bounded Proposal artifact plus an expected metadata revision, and
`POST /v1/proposal/fetch/<one-id>` returning exactly that immutable artifact.
Both routes authenticate the member before body reads and Git operations;
publish rechecks authorization under the coordinator lock, binds `Proposal.owner`
to the authenticated principal, and delegates provenance, live task owner,
context/base, duplicate immutable ID and atomic metadata+private-ref publication
to the existing `publish_proposal` path. Fetch delegates to strict one-ref
`read_proposal`; there is no proposal-list/full-history/code-search route.
Responses contain bounded receipts/checksums, never source paths or credentials.
Credential revocation on the shared Coordinator immediately fences this listener.

Proposal bytes do not travel on metadata/control/events. The adapter never
writes a Source checkout and does not automatically integrate, verify, or
apply. Existing `assemble_candidate(..., verify=True)` is a separate explicit
next step; if the committed candidate has no detected verification plan, its
status remains `not_run`, not PASS. Source application remains separately
confirmed through the appropriate owner/native candidate flow. Publication
outcome transport failures expose `outcome_uncertain`; callers must reconcile
by fetching the same immutable proposal ID and checksum and must not blindly
retry with a new ID.

Artifacts remain subject to existing proposal limits (64 files, 256 KiB per
file, 2 MiB canonical artifact), source path/secret caveats, CAS and provenance
checks. Materialization refuses case-fold path collisions so Linux proposals
such as `A.txt` and `a.txt` cannot silently overwrite one another on Windows.
The TLS listener has a bounded worker count and finite socket timeouts. It is
not an OS sandbox; explicitly selected files may contain secrets.

## Local checks and remaining M4 work

Reproduce the local proposal/control scope with:

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \
  QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_collab_lan_proposals.py tests/test_collab_lan.py \
  tests/test_collab_proposals.py tests/test_collab_candidates.py \
  tests/test_collab_commands.py tests/test_collab_coordinator.py \
  tests/test_collab_transport.py
```

`tests/test_collab_lan_proposals.py` uses temporary Git checkouts/stores and a
temporary self-signed cert on literal loopback to cover selected publish/fetch,
wrong owner, stale revision, duplicate immutable IDs, revocation, session/auth
checks before body/Git I/O, no code routes on control, candidate assembly
remaining separate from Source, and case-fold collisions. It does **not** test
a real NIC/firewall, two machines, Windows, provider, production certificate
issuance/trust UX, app UI, operational threat model or release deployment.

Remaining M4 includes those usability/lifecycle/security review and two-host LAN
acceptance, UI for explicit invitation/pin management and proposal capture,
and the complete proposal-to-reviewed-candidate-to-explicit-Source-integration
user flow. No unauthenticated listener may be exposed at any stage.
