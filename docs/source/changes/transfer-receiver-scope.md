# Change note: receiver-scoped transfer state (atomic change 1)

> Historical scope of atomic change 1 in `docs/plans/transfer-fid-and-resume-unify-plan.md`.
> The atomic-4 section in `transfer-fid-routes.md` supersedes its old resume/kind descriptions;
> receiver-owned allocation and handshake consolidation remain later steps.

## Core logic of this change

- Problem: bare fid keys allowed two receivers' uploads to overwrite progress, a receive cleanup to close an equal-id send socket, and port waits to consume another receiver's announcement.
- Upload progress now uses `(receiver, fid)` keys. Download progress retains local integer fid keys. Values remain `[bytes, total, speed, local_path, logical_peer]`.
- Outbound activity, sockets and pause switches use the upload identity; inbound activity and sockets use the local integer key. Cleanup and ownership checks never cross those namespaces.
- The receiver is the actual next-hop control endpoint. Client uploads, including forwarded uploads, use the configured server. A forwarded upload's fifth progress item still names its original logical target.
- Port announcements carry their control peer in the local cache. Ordinary sends, resumes, public-key pushes and relay target collection match both peer and fid. The wire format is unchanged.

## Modules and interactions

- `PyFlow/network_api/connect_tcp.py`: scoped sender keys, receiver-specific pause/start, scoped port consumption, scoped upload snapshot keys and loading, allocator occupancy checks against scoped uploads.
- Server local calls now require `pause_trans(fid, peer)` / `start_trans(fid, peer)`. Client calls default to their configured server and reject another explicit receiver. Remote pause/start wire lines do not change.
- Both Web backends explicitly extract numeric fid from upload keys. `/api/transfers` fields and the frontend remain unchanged; equal numeric fids produce separate rows.
- Regression coverage: real equal-id sends to two clients, opposite-direction socket cleanup, scoped snapshot reload without overwriting a live receiver, historical snapshot preservation, Web API collision rows and allocator occupancy.

## Implicit assumptions and easy-to-forget details

- Never infer the receiver namespace from a forwarded upload's logical `peer` slot. Use its next-hop server.
- Do not remove outbound port announcements when opening an incoming listener with the same numeric fid.
- Upload JSON keys are compact JSON strings encoding `[receiver, fid]`, for example `[["127.0.0.1",6000],9]`. Download JSON keys remain decimal strings.
- Old bare upload-id snapshots cannot select a receiver unambiguously under this contract. Loading rejects them; persistence and shutdown preserve the original file rather than silently replacing it. Archive the file before persisting new tasks. No automatic migration or compatibility alias is supplied.
- The existing progress-recording flag still disables resumable progress when False. This does not disable in-flight scoped pauses or port matching.
- At this step ordinary/relay allocation, kind bits, tfid fields and `/forward_resume` remained in service; the atomic-4 cutover supersedes kind and resume-command handling only.

## Changing this in six months

- Keep the real-socket same-number regression: random epochs reduce collisions but do not provide identity isolation.
- Any new outbound progress, activity, pause or port lookup must carry its actual receiver namespace; numeric fid alone is not a complete outgoing identity.
- The server/client helper copies remain duplicated. This change intentionally does not introduce a mixin or a replacement record model.
- New protocol stages must migrate all callers without reintroducing a raw-fid fallback. Forward-route redesign and final-target completion semantics remain atomic changes 2 and 3.
