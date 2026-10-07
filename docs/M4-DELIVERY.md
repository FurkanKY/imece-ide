# M4 participant delivery and owner integration

Engineering scope: explicit participant join/metadata, selected-file proposal
capture/publication/receipt fetch, and owner-selected verified integration.
**M4 acceptance remains open:** local TLS fixtures and browser mocks are not
real two-machine LAN or native-desktop acceptance. M3 Windows/native acceptance
remains deferred. M5 packaging engineering has since started by explicit user
request, without closing these gates; see [M5-PACKAGING.md](M5-PACKAGING.md).
Nothing starts a listener, agent or
publication automatically.

## Operational flow

1. Both machines use top-level Git checkouts with the same committed session
   baseline. Owner metadata lives outside Source. Configure the owner session
   and explicitly start the two TLS listeners with a literal private IPv4 and
   certificate/key files. Allow only the selected two ports through the firewall;
   never port-forward them to the Internet.
2. Issue a member-bound invitation; convey it through a trusted private channel.
   Independently confirm the SHA-256 leaf certificate pin through a separate
   channel. Never paste the invitation into a prompt, issue or public chat.
3. On the participant's **Oturum** tab, paste the invitation, enter and confirm
   that pin, then pair. Inspect the shared goal, decisions, interfaces and tasks;
   refresh explicitly. Joining does not enable native SharedContext or execute
   an agent. Disconnect removes local capabilities and attempts authenticated
   leave. If pairing/leave is uncertain, ask the owner to revoke access and
   issue a new invitation rather than assuming the old one was unused.
4. Inside that same disclosure, select a task assigned to you and the local
   source (manual Source edits or an owned, quiescent native result). Enter the
   exact relative file paths, one per line. No file is automatically selected.
   Preview shows the immutable proposal ID, paths and SHA-256. Confirm sharing
   those contents as-is; separately confirm any out-of-scope paths. Credentials,
   `.git`, `.imece`, symlinks, traversal, oversized and unsupported files are
   rejected, but this is **not a content-secret scanner**. Review code yourself.
5. Publishing uses only the separate pinned proposal channel. A changed file,
   assignment, baseline or metadata revision invalidates the preview. Preview
   tickets are RAM-only, bounded to four and expire after five minutes. Ambiguous
   publications retain their ID without expiry and forbid blind new-ID sends;
   use **Aynı öneri kimliğini uzlaştır** to fetch and compare its immutable hash.
   A missing/mismatching receipt does not grant retry authority: keep the ID and
   ask the owner to inspect it, or explicitly disconnect/revoke the session.
   A single-ID fetch displays only the receipt metadata; it does not write code.
6. On the owner **Ürün** board, refresh the board and explicitly load proposals.
   Select one or two current-context proposals. Publication itself advances the
   metadata revision, so a proposal's capture revision need not equal the current
   board revision; persisted ancestor/ownership provenance is checked by backend.
   Click **Birleştir ve doğrula**. Assembly and configured verification happen
   in an isolated directory; overlapping incompatible contents fail without
   modifying Source. Verification executes repository commands: trust the project.
7. Open **Ortak adayları uygula / geri al** in that board (or the existing task
   workspace's candidate disclosure). Inspect paths/candidate location and
   explicitly confirm apply. The existing checkpoint/editor refresh path is used.
   Explicit rollback restores the recorded bytes/modes; later edits, changed
   candidate contents, Source baseline or owner session/context refuse authority.
   Durable receipts survive reopening SQLite; credentials and preview tickets do
   not. No historical agent result IDs are fabricated for shared proposals.
8. Stop the owner listeners explicitly; cleanup failures retain resources for
   retry, not a false stopped status. No capability is automatically recovered
   after process loss.

## Security boundary review

| Boundary | Enforcement / residual risk |
| --- | --- |
| Network authentication | TLS 1.2+, literal private IPv4, exact leaf pin before bearer transmission, session/member authorization before body and again before mutation. Private LAN itself is not trusted. |
| Capability lifetime | Member-bound one-use invites, ephemeral backend-only bearer, explicit revocation/leave, project-generation and exact-handle cleanup. A wire failure may already have changed remote state. |
| Code vs metadata | Separate authenticated proposal listener; no pairing, list-all, unrestricted Git URLs or Source writes on it. Explicit files only, bounded canonical artifacts/checksum receipts. |
| UI lifetime | RAM-only invite/input/preview state; admission, project epoch, handle and duplicate-click fences. Proposal uncertainty survives tab remount. No browser-storage secrets. |
| Source authority | Committed common baseline, owned native result/content/sequence guards, immutable provenance, isolated verification and durable confirmed apply/rollback. External editors are not globally transactional. |
| Resource exhaustion | Bounded requests/workers/tickets/files. An authenticated or reachable peer can still consume the available slots; no distributed-DoS guarantee. |
| Trusted local code | Verification commands and Git tooling are trusted execution, not a malicious-code sandbox. Privileged local users, compromised hosts and credential-bearing file contents are outside automatic protection. |

## Reproducible engineering checks

Using the existing development environment (no live provider/API calls):

```sh
env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS \
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen \
  PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_m4_delivery.py tests/test_m4_delivery_bridge.py \
  tests/test_shared_candidate.py tests/test_peer.py tests/test_peer_bridge.py \
  tests/test_peer_delivery_edges.py \
  tests/test_collab_proposals.py tests/test_collab_candidates.py \
  tests/test_collab_lan_proposals.py
cd web/ui
npm run typecheck
npm run build
node scripts/m4-peer-acceptance.mjs
node scripts/m4-owner-acceptance.mjs
node scripts/m3-acceptance.mjs
```

The Python scope above passed **108 tests** across the focused Linux batches.
An additional owner/product bridge and native-candidate regression batch passed
**67 tests**. It includes real pinned
loopback TLS across two separate checkouts, explicit selected publication,
ambiguous-write same-ID reconciliation, RPC completion, owner bridge preparation,
verification, apply, reopened SQLite and rollback, and stale Source/context
refusal. Browser fixtures cover consent, no automatic sends, remount identity,
exact stale cleanup, secret non-persistence and 320px layout. Production build
passes with the existing chunk/import warnings. These results do not close the
following acceptance checklist.

## Open real two-machine acceptance

- [ ] Two separately installed native desktop clients on a private LAN; explicit
  owner activation and independent certificate-pin confirmation.
- [ ] Pair, manually refresh goal/decisions/interfaces/tasks, and revoke/leave;
  expired/reused/wrong-pin/wrong-session invitations are refused.
- [ ] Participant explicitly selects code; only the separate proposal channel
  transports it; nonselected/credential files remain absent at the owner.
- [ ] Owner explicitly selects, assembles/verifies, confirms apply and rollback;
  Source remains unchanged before confirmation and later edits refuse rollback.
- [ ] Exercise interrupted publication/reconciliation and close/project switches
  while each operation is in flight, on both supported native platforms.
- [ ] Record actual machines/versions, outcomes and remaining risks before
  declaring M4 accepted or beginning M5 release acceptance.
