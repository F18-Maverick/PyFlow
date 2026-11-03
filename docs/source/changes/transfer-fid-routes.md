# Change note: server FID routes and transfer lifecycle (2–3, O1–O4)

> Atomic changes 1 and 2 were reviewed and committed by the user. Atomic change
> 3 adds per-target completion and stable-ID replay as approved, and awaits
> review. The atomic-2 sections below record that step's original scope;
> the atomic-3 section supersedes their deliberately deferred completion behavior.
> Resume-command unification, handshake consolidation and receiver-owned
> allocation remain later, unapproved changes.

## Core logic of this change

- Problem: the server indexed the same forwarding record by an upstream fid and
  every target fid. Its snapshot duplicated that record; loading reconstructed
  independent copies. Lookup also lacked the receiver scope of a target fid.
- `_fid_routes[upstream_fid]` is the only authoritative route. It stores the
  original sender and a complete `{receiver: downstream_fid}` mapping.
- `_fid_route_index[(receiver, downstream_fid)]` points to the upstream fid,
  not a copied route. Scoped lookup and local upstream lookup return the same
  record. The route contains no file path, name, directory, size or byte-progress
  copies; those belong to the existing endpoint progress records.
- `_forward_relays` remains transient buffer/condition/pause state only. The
  server no longer has `_forward_resume_records`; the client's existing record
  remains until its separately approved resume cutover.

## Modules and interactions

- `PyFlow/network_api/connect_tcp.py` adds server registration, lookup and
  identity-safe removal of route/index owners under `_forward_relays_lock`.
  Registration is all-or-nothing when a primary or downstream key is occupied.
- Ordinary progress restoration skips only route-owned identities in the
  appropriate local or downstream namespace. The allocator consults both
  route tables alongside existing progress stores.
- Current `/resume_trans` handling resolves the requesting peer and child fid
  to an upstream fid, then sends the existing `/forward_resume` line to the
  original source. An unrelated peer cannot address another target's route.
- The `relays` JSON region now contains one row per upstream fid, with
  `originator` and `targets: [[receiver, fid], ...]`. Load rebuilds the derived
  index, counts authoritative routes, and does not overwrite live owners.
- Unsupported old server relay schemas are logged and preserved on load,
  persistence and shutdown; no migration shim or additional state file is
  introduced. The client's relay snapshot format remains unchanged.

## Implicit assumptions and easy-to-forget details

- Targets passed to the relay after reachability filtering form the accepted
  set. Duplicate addresses denote one target. Port, connection, crypto attach
  or start-sign failure must not shrink that set in the recovery route.
- Whole-route deletion compares acknowledged deliveries with all accepted
  targets, not merely the subset of target connections that opened.
- The route does not yet track individual completion transitions. If a subset
  succeeds, the entire accepted mapping remains. Per-target completion and
  final-source notification belong to atomic change 3.
- In particular, the source's existing data ACK can still report upload success
  despite an incomplete target set. Retaining the server route does not fix
  source-record lifetime; do not claim final end-to-end recovery is complete.
- A route must be removed only by its owning object. An old leg finishing after
  another registration cannot remove the new primary record or its indexes.
- Snapshot loading acquires the progress lock and then route lock; allocation
  must remain outside these locks. Removal releases the route lock before
  persisting, avoiding a reverse lock nesting.

## Changing this in six months

- Keep the real-socket regression where one target advertises a bound but
  non-listening port and another receives the complete file. It catches the
  misleading `delivered == len(target_conns)` cleanup condition.
- Preserve the full accepted mapping across restart and ensure all reverse
  lookups reach one authoritative object after loading.
- Do not reintroduce raw target-fid aliases: downstream identity always includes
  its receiver. Do not put file metadata back into the server routing record.
- `tfid`, fid kind, `/forward_resume` and `/forward_upload` remain current wire
  contracts for now. Their deletion requires the later approved changes.

## Atomic change 3: completion across stable-ID replay

- Forwarding completion uses the original final data ACK only. The server
  preserves the full accepted `targets` mapping and tracks `completed`; only an
  actual target final ACK adds that receiver to the set and releases its reverse
  index. Setup failure, interrupted input, stream exhaustion and error signs
  cannot count as target completion.
- After the current input stream is read in full and every accepted target has
  acknowledged, the server forwards the existing final ACK marker upstream
  through the normal per-hop send path. It drops the route only after the ACK
  write succeeds; a failed write leaves the completed route available for
  recovery. This is not byte-for-byte packet forwarding and adds no message.
- The source keeps its forwarding record and upload row unless its original
  final-ACK wait observes that marker. That wait remains 30 seconds plus 10
  seconds per 100 MiB. A long pause or slow downstream delivery can outlast it;
  timeout or a lost ACK conservatively retains source state even if the receiver
  completed. There is no receipt or automatic retry protocol.
- Replay adds optional `relay_fid=` alongside `resume=` in `/forward_item`. The
  server validates the original sender and unresolved target, then reuses the
  upstream and child IDs without replacing the accepted target mapping. Source
  retries for one upstream ID share a send lock; concurrent duplicates for the
  same target coalesce. Completed targets are not sent again, and queued replay
  cannot start after its record has been removed.
- A receiver that cannot write its final ACK retains the receive row even at
  EOF. A file or folder-item command matches fid, original source and basename;
  the incoming size must match before reopening the original full path.
  `_request_forward_resume` takes its offset from disk. The obsolete new-ID,
  same-path `_resume_pending_paths`/`_take_pending_resume` bridge is removed.
- `relays` JSON adds `completed: [receiver, ...]`. Reload reconstructs only
  unresolved indexes; canonical snapshots lacking the optional field mean no
  completed targets. Invalid completed sets and unsupported older schemas are
  preserved, not overwritten. The client JSON shape is unchanged and excludes
  runtime locks/replay sets; reload restores its original upload row alongside relay
  metadata. Snapshot creation is serialized by the progress lock before
  acquiring the route/record lock, preventing delayed older snapshots from
  overwriting newer completion transitions.
- The pump keys queues by `(receiver, child-fid)`, accounts for failed-writer
  buffers, wakes backpressure waiters on reader failure, and bounds the final
  target-sign read. The sender does not treat an error sign as success. Existing
  direct sends and public-key pushes retain their data-ACK success contract;
  TOFU still precedes the key ACK. This change does not claim to fix the Server's
  ordinary-sender or receive-ACK gap. No CI, dependencies, coverage policy, file
  bytes, allocation authority, kind/tfid naming, `/forward_resume` or
  `/forward_upload` cutover is included.

Keep regressions for zero ACK, partial ACK, EOF confirmation, file/folder replay,
duplicate requests, lost final ACK, completed-state reload and malformed-state
file preservation. Runtime evidence and explicit exclusions are recorded in
`docs/plans/transfer-fid-and-resume-unify-atomic-3-report.md`.

## Follow-up O1: direct Server error signs never complete an upload

- Human approval covers O1 only, before the separately gated O2–O4 follow-ups.
- `TCP_Server_Base.file_transfer_mode` now returns `False` immediately on an
  error sign in both the startup wait and the final-data-ACK wait, rather than
  falling through after a `break`. Only the existing final ACK permits success.
- Existing owning-entry cleanup persists a resumable failed upload under its
  complete `(receiver, fid)` key. Startup failure creates no progress row and
  does not discard a row retained from an earlier attempt. No protocol marker,
  public signature, JSON shape, retry or timeout is added.
- A real-socket regression covers file/folder items at offsets zero and 4096,
  receiver rejection after the body, durable retention and successful original-ID
  EOF confirmation. Independent runtime checks also exercise startup rejection,
  successful transfers and framed/encrypted failure and recovery.
- Server receive-ACK retention (O2), progress inspection (O3) and `delete_config`
  (O4) are not implemented here. Evidence and limitations are recorded in
  `docs/plans/ordinary-transfer-progress-inspection-o1-report.md`.

## Follow-up O2: failed Server receive ACKs retain recovery state

- O1 passed human review; O2 was separately approved. Server receive completion
  no longer sets `finished_ok` immediately after writing the file. It records
  `bool(send_message(...))` for the existing final ACK, matching the Client
  receive path. A false result or exception keeps and persists the original
  tracked receive row with a peer, even when all bytes are already on disk.
- Notifications, event updates and public-key TOFU registration stay before
  the ACK. Key pushes remain untracked. No ACK receipt, retry protocol, timeout,
  new field, public signature or persistence format is introduced.
- The regression uses real file/folder uploads, both supported ACK failure
  forms, another failure at EOF and a successful EOF confirmation. It checks
  the original fid/path/content and durable removal only after success.
- Independent runtime checks cover encrypted and resumed uploads, fresh-process
  reload of failure snapshots, disabled progress tracking and deterministic
  key-ACK failures before handshake completion. Evidence is recorded in
  `docs/plans/ordinary-transfer-progress-inspection-o2-report.md`.
- Progress inspection (O3), `delete_config` (O4) and the original FID plan's
  later cutovers remain outside this change; O2 awaits human review.
## Follow-up O3: local transfer inspection

- O1 and O2 were separately reviewed before O3. Both TCP classes now expose
  `get_transfer_progress(*, include_persisted=True)`. It returns detached,
  JSON-ready memory rows and an optional separate read of the existing progress
  file; it never loads, persists, resumes or writes transfer state.
- Upload identity keeps the complete `(receiver, fid)` scope. Server and client
  relay rows retain their existing authority and shape. Runtime flags are
  observed only from local sets; disk rows use `null` for active/socket/pause
  state. Missing, empty, malformed and unreadable files are distinct outcomes.
- `/transfers` is intercepted by both local consoles, prints JSON even when
  normal logging is disabled, and rejects arguments without sending them over
  the network. Existing Web `/api/transfers` is unchanged.
- Real API/console evidence, mutation checks and limitations are recorded in
  `docs/plans/ordinary-transfer-progress-inspection-o3-report.md`. O4's
  `delete_config` remains unimplemented and O3 awaits human review.


## Follow-up O4: explicit named local state deletion

- O1–O3 passed human review before O4 was approved. Both classes now expose
  `delete_config(config_name=None, *, is_list_config=False)`. List mode returns
  sorted existing allowlisted regular-file names without side effects; deletion
  accepts only one current basename of progress or the four message/event logs.
- Running, not-explicitly-closed, starting/stopping and active-transfer/relay
  instances refuse deletion. Progress deletion shares the persist/load lock.
  A successful or already-absent progress deletion permanently disables old-object
  reload/publication/restart, preventing repeated shutdown or late finalization
  from resurrecting the abandoned snapshot. New instances behave normally.
- Log deletion checks its buffer under the existing record lock and does not
  permanently disable logging. Other files, keys, locks, transfer contents,
  remote protocols, JSON formats, CI and dependencies remain unchanged.
- API-boundary and write/delete race regressions live in `test_delete_config.py`;
  existing real transfer/ACK recovery regressions are reused unchanged. Runtime
  evidence and limitations are in the O4 report; the supplemental O1–O4 overview
  is `docs/plans/ordinary-transfer-progress-inspection-report.md`.

