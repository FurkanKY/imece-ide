# M4 owner-side LAN hosting slice

The participant delivery and owner-selected integration workflow is documented
in [M4-DELIVERY.md](M4-DELIVERY.md).

This is an opt-in, experimental owner-side native UI for the two existing TLS
listeners. It advances M4 engineering; it is **not two-machine/LAN acceptance**.
M3 remains in progress with Windows/native desktop acceptance explicitly deferred
at the user's direction; it is not marked complete.

## Explicit owner flow

- Configure or select a metadata session, then open the LAN TLS disclosure.
  The listener starts only after the owner supplies a literal private IPv4
  address and explicit certificate/private-key paths. The native manager starts
  the authenticated TLS control listener and the separate proposal TLS listener
  over the same in-memory Coordinator/session. The default loopback start path
  and its HTTP URL remain unchanged.
- LAN startup creates only the owner's in-memory credential. Configured peer
  identities are reserved in the session metadata but receive no credential
  until the owner explicitly issues a five-minute, one-use, member-bound invite
  and the peer redeems it over pinned TLS. Both listeners load one bounded private
  identity snapshot outside Source; the advertised pin is its first certificate,
  including when the selected PEM chain is renewed during startup. Temporary
  key copies are owner-private and removed before listeners start.
- The returned local owner status contains the project/session/member identity,
  listener endpoints, certificate SHA-256 and lifecycle state. It contains no
  credential, private key or metadata-store/hub path in LAN mode. The out-of-band
  invite bundle exists only in component memory; the owner must separately
  authorize reveal before reading or copying it. UI clears it on project change,
  stop, expiry and unmount. Share the bundle only with its named member over a
  separately trusted channel and verify the displayed certificate pin.
- Revocation removes pending invites and an ephemeral member credential under
  the same pairing lock. Stopping drains both TLS listeners and revokes pairing
  identities. If either listener fails to close, manager retains the resources
  in `cleanup_failed`; stop can be explicitly retried. A root-generation race
  after startup triggers epoch/project-bound stale cleanup rather than returning
  old-root secrets or closing a newer session. Stale invitation responses cancel
  only their own pending nonce, never a replacement member credential. Invitation
  and revocation handlers validate the captured project and owner epoch. Older
  status responses cannot overwrite a later startup receipt.
- Existing `shareOnce` and in-app local-preview handoff are rejected in LAN mode;
  the owner credential is never exported as a peer credential. No code starts
  from the UI, no proposal is automatically selected/published, and no shared
  context is silently enabled.

## Narrow tests

`tests/test_collab_owner_regressions.py` creates disposable OpenSSL certificates,
real temporary Git metadata sessions and actual TLS listeners on `127.0.0.1` only.
`web/ui/scripts/m4-owner-acceptance.mjs` exercises the opt-in UI with an explicit
controlled mock bridge (no socket/server/provider): no auto-start, endpoint/pin
visibility, invite reveal/copy gate, project-switch secret clearing and narrow
layout, delayed status fencing, A→B→A project changes, nonce-only cleanup and
absence of secrets in browser storage. These
fixtures do not test a real LAN, remote peer device, Windows TLS behavior,
certificate enrollment/trust distribution, firewall/NIC isolation or a real
provider. The code certificate path is an explicit local trust decision and is
not protected by OS keychain enrollment in this slice.

### Reproduce the verified development scope

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS \\
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen \\
  PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q -p no:cacheprovider \\
  tests/test_collab_owner.py tests/test_collab_owner_application_e2e.py \\
  tests/test_collab_owner_regressions.py tests/test_collab_owner_bridge.py \\
  tests/test_collab_lan.py tests/test_collab_lan_proposals.py \\
  tests/test_collab_transport.py tests/test_collab_coordinator.py \\
  tests/test_collab_proposals.py tests/test_collab_candidates.py \\
  tests/test_combined_candidate.py tests/test_run_restart.py \\
  tests/test_workspace_ownership.py tests/test_collab_product.py \\
  tests/test_collab_product_bridge.py tests/test_collab_task_creation.py \\
  tests/test_collab_task_creation_regressions.py
```

Executed on this Linux development environment: **538 passed, 1 Windows-only
skip**. TLS fixtures require an already installed OpenSSL; they skip rather than
install it when unavailable. Frontend checks from `web/ui`:
`npm run typecheck`, `npm run build`, `node scripts/m4-owner-acceptance.mjs`.
The UI fixture is wired into CI; these results are not supported-platform or
real two-machine acceptance.

Remote peer join UX, proposal capture/publish/fetch UI, cross-host candidate
selection/verification/apply/rollback, operational threat review and two-machine
acceptance remain open M4 work. M4 is not accepted/closed.
