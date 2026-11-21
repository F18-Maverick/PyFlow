# Change note: server FID routes, sender ownership and socket/FID recovery

> Current lifecycle revision evidence is in
> `docs/plans/transfer-control-lifecycle-report.md`.
> The chronological entries below preserve earlier implementation history.
> Previous transfer-control C1–C4b commits and C5 acceptance are documented in
> `docs/plans/transfer-pause-start-direction-report.md`; those historical
> results are not verification of the current lifecycle revision.

## Resume formatter rejects invalid offsets

- **Core logic:** `format_resume_line()` accepts only a nonnegative Python
  integer offset, including zero. It raises `ValueError` before producing wire
  text for `None`, query markers, numeric strings, booleans, floats or negatives.
- **Modules and interactions:** Ordinary and relay recovery still format the
  same five-token `/resume_trans` line and use their existing `send_message`
  path. The parser, endpoint identities, FIDs and serialized progress are
  unchanged.
- **Assumptions:** The formatter must not silently coerce its argument: its
  output must be accepted by the numeric-offset parser.
- **Maintenance:** Resume offset validation belongs at the formatter boundary;
  no recovery retry, wire migration or sender/file-transfer behavior is added.

## Resume requests require framing when encryption is enabled

- **Core logic:** The normal Client and Server `send_message` path treats
  `/resume_trans` like `/pause_trans` and `/start_trans`. If no secure channel
  is available, the write fails without entering the plaintext writer or
  reporting a sent message. With encryption disabled the wire is unchanged.
- **Modules and interactions:** Ordinary receivers and Server relay recovery
  still use `format_resume_line` and the same send path. After selecting a
  framed channel, `require_secure=True` rechecks framing under the channel's
  final write lock; a write error keeps its existing Client/Server behavior.
- **Assumptions:** A successful `_wait_control_secure` is not a reservation:
  rekey can start before the write. Keep the rejection on the plaintext path,
  not before a second, independently changing `channel.secure` check.
- **Maintenance:** This does not change the raw handshake/port announcements,
  the receive-side plaintext whitelist, other business commands or the retry
  policy. The recovery tests observe incoming control bytes, decoded offsets
  and final ACKs; the rekey test also covers the async Server.

## New relay admission: reserve the upstream FID before publishing a route

- **Core logic:** A waiting ordinary listener owns its FID even before it has
  a progress row or data socket. A new relay must win a fresh receive
  reservation; it cannot adopt a pending ordinary request on the same session.
- **Modules and interactions:** Server `_forward_relay` claims the FID, then
  `_register_fid_route` checks the exact reservation before publishing the route
  and child indexes. Snapshot restore has no reservation and requires a free
  receive slot. Existing-route recovery still adopts its own reserved leg.
- **Assumptions:** Child FIDs are allocated outside the progress lock. Fresh
  admission holds that lock through live-leg registration, not through target
  negotiation or I/O. A rejected registration releases only its own record.
- **Maintenance:** Do not publish and then roll back: reconnect can observe a
  transient route and skip an ordinary partial file. Failure *after* admission
  (e.g. a refused target listener) still retains a recoverable route. The
  listener-first and competing-relay cases live in `test_tcp_transfer_resume.py`.

## Lifecycle revision C6: combined acceptance evidence

- The affected-scope V6 run completed with **692 passed, 1 skipped** on Linux
  x86_64 / Python 3.10.20. The skip is the plaintext rekey variant; encrypted
  rekey variants exercised the real key reader and framed control channel.
- Independent real-socket scenarios cover ordinary bidirectional recovery,
  old-session ownership, global pause/drain/pressure, independent Target flows,
  offline/new-session recovery, per-stream failure, explicit EOF confirmation
  and native self-target rejection. Content, actual ACKs and owner cleanup are
  recorded per scenario in the current report, not inferred from test counts.
- P05/P11/P13/P17/P20 retain explicit partial coverage. Native Windows, other
  Python versions, process restart, relay/terminal rekey and browser UI were
  not verified. Flask test-client requests exercised the Web backend and TCP,
  not a listening HTTP server or browser. Same-session/FID generation isolation
  and a successful final-ACK write lost before Source receives it stay deferred.
- This stage changes acceptance documents only: no production, tests, protocol,
  schema, dependencies or CI changes. Known 37 type diagnostics remain unrerun;
  results do not imply human final acceptance, merge or push approval.

## Lifecycle revision C5: refuse native self-target file forwarding

- Filter the Source's complete control endpoint in the shared Client per-file
  entry before FID allocation, progress creation or relay negotiation. Match
  the captured socket's actual IP/port; configured hosts must resolve to that
  IP, except wildcard, which aliases the actual port.
  An unused configured source IP after no bind or port fallback is not self.
- Self-only forwarding logs a refusal and returns no delivery. Mixed lists
  retain only legal peers, including same-IP/different-port and
  different-IP/same-port Clients; the upload row and wire use that same list.
- Native console, nested folder entries, Web forwarding and retained-row
  replay share the check. Rejected replay preserves its existing recovery row;
  the public resume boolean still reports worker reservation, not delivery.
- No signature, wire, persistence schema, dependency or CI changes. Message
  forwarding and the separate disk-forward extension retain their contracts.
  C6 combined acceptance and strict same-session/FID generation isolation are
  not implemented here. Actual evidence is in the lifecycle report.

## Lifecycle revision C4: explicit all-Target EOF reconfirmation

- After every accepted Target's real ACK, an upstream final-ACK write returning
  `False` or raising retains the existing Server route and Source shared upload
  row. Source explicitly calls `resume_trans(original_target, upstream_fid, total)`;
  its boolean reports worker registration, not completion. Live owners, including
  `finally`, reject this explicit operation as busy without closing or queueing;
  it can be repeated after cleanup.
- Reuse `_fid_routes[fid]` with `originator`, full `targets` and `completed`, and
  Source's existing upload row. Only Source→Server port negotiation, the complete
  existing filename/size header and final ACK are exchanged. Offset must equal
  this header's size exactly; there are zero body bytes, no new Target leg and
  no retransmission. Targets may be offline. No Server byte-progress or terminal
  table, field, snapshot schema or wire change is added.
- Another false/exception retains state. Only the owning route is removed after
  its successful ACK write; Source removes its shared row only on observing the
  ACK. Existing route/completion and upload snapshots reload with the same
  explicit recovery requirement, not automatic retries or TTL. This does not
  resolve a successful Server ACK write lost before Source receives it, and
  does not validate historical name/size/hash.
- Ordinary transfers, C3 unfinished-Target recovery and global pause/start stay
  unchanged. This extends transfer control without changing Source
  `file_transfer_mode` or `_send_file`. Strict same-session/fid generation
  isolation remains deferred; C5 self-target rejection and C6 combined acceptance
  remain later stages. No verification results are claimed in this entry.

## Lifecycle revision C3: numeric offsets and independent parallel recovery

- The user approved the revised requirements and plan. Old C3a/C3b offset
  queries, forced route teardown and one-active-Target/FIFO rounds are withdrawn
  history, not the current contract or current verification evidence.
- Receiver waits for its old owner, including final cleanup and file closure,
  then sends its actual nonnegative numeric disk offset. The sole resume line
  remains `/resume_trans <fid> <offset> <origin> <target>`; remove `?` queries,
  `offset=None` and query state/consumers without adding wire fields or commands.
- Server validates the actual Target socket and stable child route, queues the
  numeric request and waits for every old owner of the original route, including
  listeners, negotiations, reader/writers and `finally`, to finish naturally.
  No forced teardown, automatic start, global-lock wait or control-reader/event-
  loop blocking; healthy control and unrelated fids continue.
- Then each Target gets its own offset, Source→Server→Target stream, runtime
  progress, resources and failure handling. Original upstream/child fids, full
  paths and persistence schema remain. Bodies run in parallel: late Targets do
  not wait for an existing large body or ACK. The same actual socket+fid only
  serializes short port negotiation, never body transfer or final ACK.
- C1 actual-session ownership and C2 Source-arbitrated global pause/start remain.
  Shared task control state lives until its last owner exits; buffered bytes
  drain and user start cannot bypass local backpressure. One stream cannot
  overwrite sibling progress or prematurely delete the task upload row.
- Only real Target ACKs update `completed`. All accepted Targets must really
  acknowledge before task success; an intermediate Target cannot fabricate a
  whole-route ACK. Completed siblings are not replayed.
- Offline Source requests stay pending. Target disconnection discards old-socket
  requests; reconnect requires a fresh actual disk offset on the new session.
  Individual failure does not cancel siblings or start automatic retries.
- Strict same-session/fid generation isolation remains deferred. C4 all-Target
  upstream EOF reconfirmation is documented above; C5 self-target rejection and
  C6 combined acceptance remain later stages. No new wire, schema, dependencies
  or CI changes.
- This round is **pending unified verification**. Old C3a/C3b pass counts do not
  verify this replacement. Paths, risks and historical evidence are recorded in
  `docs/plans/transfer-control-lifecycle-report.md`.

## Lifecycle revision C2: Source-arbitrated global relay pause

- Source serializes all user decisions. Server translates validated requests
  to upstream fid and projects Source notifications onto each live child fid.
  Relay requests may repeat so an unseen opposite intent is not swallowed.
- Server writers no longer wait on per-Target user pauses; queued bytes drain
  on the existing sockets. Source alone stops before its next body block.
- Backpressure uses the local reader high/low-water gate and TCP flow control,
  not automatic user pause/start messages. Drain cannot cancel user pause.
- Stable FIDs, wire fields and real final ACKs remain unchanged. Issue 2 is
  deferred; route reconstruction and EOF reconfirmation remain later stages.
- Evidence: `docs/plans/transfer-control-lifecycle-report.md`.

## Lifecycle revision C1: retire actual control sessions

- Session cancellation owns listeners, data sockets and negotiations by actual
  control socket, independently of progress tracking. Reconnect no longer scans
  peer progress to close whichever sockets happen to be current.
- Resource registration remains until worker cleanup; recovery waits for captured
  old owners off the control reader. Delayed cleanup cannot close a newer session.
- Recovery workers keep their admitted control socket, including forwarded uploads.
  Stable FIDs, original paths, wire fields, persistence and real final ACKs remain.
- This does not isolate delayed pause/start across same-socket, same-FID data-leg
  replacement (deferred issue 2). Later lifecycle stages are not implemented here.
- Evidence: `docs/plans/transfer-control-lifecycle-report.md`.

## Revision R1: sender-owned relay upstream FID

- A relay source allocates and binds a fresh upstream FID before `/forward_item`.
  It sends the required `fid=<upstream_fid>` field; the Server preserves that ID
  in the route and listener response. The Server continues to allocate a distinct
  child FID for each target using the existing allocator and receiver scope.
- A new upload omits `resume=`. Every replay includes `resume=<offset>`, including
  `resume=0`; the Server accepts it only when the existing route belongs to the
  actual originator and contains every requested unresolved target. Upstream and
  child FIDs, progress rows, paths and final data ACK behavior remain stable.
- The Server rejects an upstream FID already used by a route, receive row, or a
  target-scoped active upload, socket, route index, relay or pause state. The
  FID-scoped `/forward_error` reports rejection; the existing owner's route,
  indexes and progress remain unchanged. Distinct receiver scopes remain distinct.
- Ordinary file, folder and public-key senders retain their allocation. R2 removes
  the historical UUID handshake without changing progress JSON or adding a route table.

R1 behavior evidence and remaining review items are in
`docs/plans/transfer-sender-fid-r1-report.md`.

## Revision R2: actual socket and sender FID correlation

- `/file` and `/file_folder` retain their fields and trailing sender FID;
  `/crypto_pub_key <fid>` and `/forward_item <kind> fid=<upstream_fid> [resume=<offset>] <fields>`
  contain no request marker. The terminal reply is exactly
  `/server_file_transfer_port <port> <fid> <stamp>` or `/forward_error <fid> <reason>`.
- Waiters match actual control socket plus FID. A sent request without a terminal
  result on timeout or cancellation leaves its FID unresolved for that session;
  late replies cannot unblock it, but unrelated FIDs and commands continue.
  Natural disconnect cleans that session's waiters, unresolved marks, listeners
  and data legs; reconnect can resume with the same FID. Data or ACK failure
  does not disconnect healthy control. Each negotiation has one terminal reply.

## Historical atomic change 5: superseded listener-correlation protocol

- Ordinary file/folder pushes, public-key legs, relay target listeners and relay
  source uploads use a one-shot `request=<uuidhex>` marker. The marker belongs to
  one control request only: it is not a FID, is not persisted, and is not reused
  for a retry. `/file`, `/file_folder` and `/crypto_pub_key` put the marker
  before the trailing FID; `/forward_item` puts it immediately after its kind.
  The common reply is `/server_file_transfer_port <port> <fid> <stamp> <request_id>`.
- The requester registers before sending and dispatches only when the actual
  control socket identity, request ID and expected FID match. Relay upload
  accepts its server-assigned upstream FID without changing allocation authority.
  Relay targets register independently and may reply in any order. Success
  consumes registration; failure, timeout, cancellation and teardown retire it
  and wake its waiter. Late, duplicate or mismatched replies are discarded,
  never cached, with no fallback or alias. Forward setup errors carry their
  request ID and cannot wake another upload.
- `/forward_upload`, its FIFO queue and the shared `_forward_error` slot are
  removed. Stable upstream/child FIDs, progress rows, route state, transfer
  payload, ACK behavior and crypto framing are unchanged. Public-key transfer
  bytes remain plaintext; the temporary server-assigned port is associated by
  request ID without changing the key-exchange order.
- Keep the real-socket concurrent-forward regression that deliberately blocks
  the first port response while the second request completes. It catches
  request-agnostic FIFO consumption; also retain timeout, cancellation, wrong
  peer/FID, duplicate response and public-key/async regressions.
- Locale cleanup removes only 54 obsolete extracted gettext blocks from 19
  catalogs: the File_Transfer and TCP_Client_APIs/TCP_Server_APIs catalogs for
  `ja`, `ko`, `ru`, `zh_CN` and `zh_TW`, plus the connect_tcp API catalogs for
  `ko`, `ru`, `zh_CN` and `zh_TW`. Removed blocks teach the legacy port cache,
  dedicated forwarding-upload command or outdated port response. No global
  regeneration or replacement translations are supplied; new source text uses
  English fallback pending translation.

## Historical atomic change 4: one recovery command and one source of file metadata

- The sole resume wire is `/resume_trans <fid> <offset> <origin> <target>`, with
  shell-quoted tuple endpoints. Server checks target against the actual requester,
  then either selects its own scoped upload or validates the downstream route
  and origin before translating to the upstream fid. Client accepts only its
  own current connection identity as origin. No old command alias remains.
- Client recovery reads only existing five-item upload/download rows. Forwarded
  folder items replay as files into the target's retained full path; no copied
  targets/path/destination/folder metadata table remains. Client `relays` is empty;
  nonempty legacy client relay snapshots are rejected and preserved on writes
  and shutdown. Server routes/completed sets retain their existing authority.
- Transient per-upload locks and target sets serialize distinct targets and
  coalesce duplicates for both ordinary and forwarded resumes; they are released
  after use and never persisted. Failed sends retain source progress. Server
  offline requests coalesce to the latest offset per upstream fid/target; failed
  delivery keeps them queued, while completed/missing routes cannot be replayed.
- Fids carry no plain/relay kind; allocation calls have no kind argument, the
  reserved low bit is zero, and diagnostic formatting is `e<epoch>.<counter>`.
  Allocation authority, `tfid` naming and `relay_fid=` remain unchanged until
  separately approved step 6. Atomic 5 removes the old upload handshake.
  Final data-ACK aggregation and stable upstream/downstream IDs are unchanged.

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
- A receiver takes its offset from disk through the unified command; stable-ID
  replay uses the existing upload row. No new-ID or same-path bridge is used.
- ``relays`` JSON adds ``completed: [receiver, ...]``. Reload reconstructs only
  unresolved indexes; canonical snapshots lacking the optional field mean no
  completed targets. Invalid completed sets and unsupported older schemas are
  preserved, not overwritten. The current Client snapshot is empty and has no
  replay metadata; runtime locks and target sets are not persisted. Snapshot
  creation is serialized by the progress lock before route state is read.
- The pump keys queues by ``(receiver, child-fid)``, accounts for failed-writer
  buffers, wakes backpressure waiters on reader failure, and bounds the final
  target-sign read. The sender does not treat an error sign as success. Existing
  direct sends and public-key pushes retain their data-ACK success contract;
  TOFU still precedes the key ACK. This change does not claim to fix the
  Server's ordinary-sender or receive-ACK gap. No CI, dependencies, coverage
  policy, file bytes, allocation authority, or ``/forward_upload`` cutover is
  included.

Keep regressions for zero ACK, partial ACK, EOF confirmation, file/folder replay,
duplicate requests, lost final ACK, completed-state reload and malformed-state
file preservation. Runtime evidence and explicit exclusions are recorded in
``docs/plans/transfer-fid-and-resume-unify-atomic-3-report.md``.

## Atomic change 2 history

### Core logic of this change

- Problem: the server indexed the same forwarding record by an upstream fid and
  every target fid. Its snapshot duplicated that record; loading reconstructed
  independent copies. Lookup also lacked the receiver scope of a target fid.
- `_fid_routes[upstream_fid]` is the only authoritative route. It stores the
  original sender and a complete `{receiver: downstream_fid}` mapping.
- `_fid_route_index[(receiver, downstream_fid)]` points to the upstream fid,
  not a copied route. Scoped lookup and local upstream lookup return the same
  record. The route contains no file path, name, directory, size or byte-progress
  copies; those belong to the existing endpoint progress records.
- `_forward_relays` remains transient buffer/condition/pause state only. Client
  forwarding recovery now uses its ordinary upload row; no client relay record
  exists.

### Modules and interactions

- `PyFlow/network_api/connect_tcp.py` adds server registration, lookup and
  identity-safe removal of route/index owners under `_forward_relays_lock`.
  Registration is all-or-nothing when a primary or downstream key is occupied.
- Ordinary progress restoration skips only route-owned identities in the
  appropriate local or downstream namespace. The allocator consults both
  route tables alongside existing progress stores.
- Unified `/resume_trans` handling validates the requesting peer, origin and
  target, then routes either to the local scoped upload or the authoritative
  upstream route. It no longer emits a second resume command.
- The `relays` JSON region now contains one row per upstream fid, with
  `originator` and `targets: [[receiver, fid], ...]`. Load rebuilds the derived
  index, counts authoritative routes, and does not overwrite live owners.
- Unsupported old server relay schemas are logged and preserved on load,
  persistence and shutdown. Client relay snapshots are empty; nonempty legacy
  client metadata is rejected and preserved.

### Implicit assumptions and easy-to-forget details

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

### Changing this in six months

- Keep the real-socket regression where one target advertises a bound but
  non-listening port and another receives the complete file. It catches the
  misleading `delivered == len(target_conns)` cleanup condition.
- Preserve the full accepted mapping across restart and ensure all reverse
  lookups reach one authoritative object after loading.
- Do not reintroduce raw target-fid aliases: downstream identity always includes
  its receiver. `tfid` remains until step 6; `/forward_upload`, kind bits and
  `/forward_resume` are not current contracts.


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


## Follow-up: resume identity uses the connected endpoint

- `/resume_trans` keeps its five tokens and both fields remain `(ip, port)`
  tuples. What changed is the value source: the server writes the concrete
  local endpoint of the accepted control connection (`getsockname()`) as
  `target`, and a client records the connected server endpoint
  (`getpeername()`) as the peer it later sends as `origin`.
- Both sides accept either the concrete endpoint or the configured
  `(host, port)` as "this peer", so a wildcard bind (`0.0.0.0`) or a host alias
  (`localhost` vs `127.0.0.1`) no longer breaks a reconnect. A legacy direct
  row that recorded the configured server address is migrated in memory on
  read; the progress JSON shape is unchanged.
- Forwarded resume keeps its existing actual-address route scoping; no wire
  command, FID allocation, relay record or public signature changes.
- NAT endpoints and a changed `client_port` remain unsupported identities.

## Follow-up: reconnect sweep releases paused legs

This describes the pre-control-registry sweep. C2b replaces its wait helper;
C4a retains ownership until final cleanup and rejects active resume requests.

- `_drop_stale_transfers` now clears the pause switch of every upload leg it
  sweeps (after closing the socket, outside the progress lock) and wakes the
  pause condition. `_wait_transfer_paused` also takes the leg's own transfer
  socket and exits once that socket is closed, because the sweep removes the
  socket from `_active_transfer_sockets` before closing it.
- A leg paused for relay backpressure or by `pause_trans` therefore ends when
  its session is swept instead of waiting forever while holding the per-fid
  send lock. A resume request already queued for that fid runs after the lock
  is released; no manual `start_trans` is needed.
- The send loop stops on the first failed write, so it does not keep reading
  the rest of the file after the peer is gone. A final ACK already in flight is
  still consumed by the existing ACK wait. Wire commands, progress rows,
  public signatures and the pause/start semantics of a live session are
  unchanged.

## Transfer control C1: typed identity and shared endpoint matching

- Private foundations parse four-token pause/start commands and validate a
  nonnegative integer FID, transfer type (ordinary/forwarded) and local role
  (sender/receiver). Runtime keys include peer, FID, type and local direction;
  progress keys and JSON remain unchanged.
- Server-local and Client resume identity checks share the same exact
  configured-or-connected endpoint predicate. Wildcard configuration does not
  admit unrelated addresses with the same port; closed sockets retain the
  configured fallback. No additional endpoint alias table is introduced.
- The new parser is not yet used by production dispatch. Existing pause/start
  wire messages and public APIs remain in use until the separately permitted
  C2b cutover. C1's targeted run passed 81 tests, including real bidirectional
  resume with configured aliases and wildcard binds. Evidence and exclusions
  are in `docs/plans/transfer-pause-start-direction-report.md`.

## Transfer control C2a: sender-authoritative control state

- Server and Client initialize a shared private control-registry implementation.
  Each typed leg binds its control socket and owner; a per-leg lock serializes
  state decisions and control writes. Same-state receiver requests still get
  an actual-state notification; receivers track confirmed and last-requested
  state separately and never echo notifications.
- Sender presets are adopted by the matching leg and published once when the
  peer can locate it. Start removes the pause key. Owner/session release or a
  failed write clears control intent and wakes waiting senders; stale owners
  cannot remove a replacement record. Control state stays out of progress JSON.
- Deterministic FIFO interleavings, blocked-write ordering and lifecycle tests
  passed with existing progress-query contracts (154 tests). A pytest iterable
  deprecation was fixed; the affected 64 scheduling cases then passed without
  warnings. Four plaintext socketpair runs exercised the real send_message
  paths in both endpoint roles and both transfer types, converging with no
  pause keys. The implementation report records evidence and limitations.
- Production leg registration, pause/start API and protocol cutover, secure
  dispatch, data-loop integration and real-file control remain at the C2b gate.

## Transfer control C2b: unified bidirectional production control

- Public pause/start now require keyword-only `trans_type` (0 ordinary,
  1 forwarded) and `direction` (0 local sender, 1 local receiver). The wire is
  strictly `/pause_trans <fid> <type> <issuer_role>` or its start equivalent.
  The old remote-only APIs and fid-only parser are removed; all production
  callers and API documentation use the unified entry.
- Ordinary and relay legs bind actual control sockets and data-socket owners
  to typed control records independently of progress tracking. Per-leg FIFO
  control workers wait for peer leg registration and existing crypto readiness
  before calling `send_message`; control readers never wait for their own key
  exchange. Control frames cannot fall back to plaintext while encryption is
  unavailable, including a re-exchange between readiness and frame creation.
- Send loops inspect their bound pause state before each new block. Receivers
  continue draining and writing in-flight data. Start retains the same socket;
  empty files, EOF replay and final ACKs do not wait on a nonexistent next block.
  Disabled tracking creates no new progress rows; a resumed receiver retains
  ownership of its existing row so successful EOF confirmation removes it.
- Relay writers use the same control registry; high/low-water requests use
  type 1 and the local receiver role. The reader's buffer gate stays separate
  from control state. Progress queries project active typed sender records onto
  the existing upload rows, and owner/session cleanup releases controls.
- Runtime verification covered real upload/download control and encrypted
  re-exchange. The first targeted run found two untracked EOF cleanup failures;
  both passed after the ownership fix. Existing affected regressions passed
  159 tests, and the owner-isolation check passed 134. Full commands, the one
  plaintext re-key skip and review-size deviation are in the implementation
  report. Complex multi-target routing/backpressure acceptance and object-level
  resume eligibility retain their C3/C4 review gates.

## Transfer control C3: scoped relay control and independent backpressure

- Relay control indexes contain only canonical target/child-FID pairs. An
  exact child takes precedence; an upstream alias resolves only to one live
  writer for that control peer. Wrong peers, types and roles never fall back
  to a global FID match. Writer controls are registered before data connect
  and released on setup failure.
- Relay receiver state keeps user pause and automatic backpressure as separate
  intents. Their OR determines upstream requests; low-water release cannot
  cancel a user pause. Notifications preserve both intents, and an old pump
  cannot mutate a replacement owner.
- The reader's high-water gate remains independent of source pause/start.
  Headers also obey the gate; queued bytes are bounded by the threshold plus
  one enqueue's fan-out bytes. Each writer pauses before real data, while END
  and final ACK handling do not wait for a nonexistent next block.
- Presets, False flags and records without a live data owner do not reserve
  routes. Actual setup sockets and existing active/progress/route ownership
  still refuse replacement. Runtime acceptance and remaining limitations are
  recorded in the C3 implementation report.
- C3 verification passed 217 control/routing tests (three existing re-key
  cases deselected) and 20 affected forwarding/resume regressions (73 unrelated
  cases deselected). New real two-target scenarios cover independent writers,
  user/pressure crossings, encrypted frames, bounded buffering and target-close
  failure without a false upstream success ACK. C4 resume eligibility remains
  outside this change.

## Transfer control C4a: ordinary per-object resume eligibility

- Ordinary resume atomically reserves one recovery worker only after the
  scoped data owner, negotiation and prior cleanup have finished. Running,
  paused, negotiating and duplicate requests return False without closing or
  queueing. Independent files and opposite directions do not block recovery.
- Receive requests reserve the object before sending; the same session's
  listener and data owner adopt the reservation. Failed sends, listener
  failures and session closure release it; pending unanswered requests use the
  existing accept-timeout window. Reconnect waits for old owner cleanup, while
  manual busy requests return immediately.
- A failed data connection can recover on the same live control connection
  from the receiver's disk offset and original FID. New owners do not inherit
  pause state. Tracking-off recovery adopts retained progress rows for final
  cleanup without creating new tracked rows.
- New eligibility/real-connection checks passed 42 tests; the affected existing
  run had 139 passes and two obsolete immediate-sweep-release assertions, both
  passing after migration to final owner release. Reconnect ownership checks
  passed 34 tests, and the final listener-handoff check passed both directions.
  Full commands and exclusions are in the C4a report. Relay recovery remains C4b.

## Transfer control C4b: shared-source relay resume eligibility

- Direct and relay recovery share the existing per-object reservation gate.
  Active, paused, negotiating or finalizing upstream owners reject recovery
  for any target without queueing or tearing down the live relay. A skipped
  target request must be issued again after that owner exits; it does not
  promise eventual automatic replay.
- Server pending delivery reserves the upstream object before control writes;
  relay setup adopts that record and releases it after final cleanup. Target
  receive requests/listeners reuse the same ownership mechanism. Offline-source
  pending offsets are retained, failed writes release reservations without
  dropping pending work, and completed/missing routes are revalidated.
- Real source reconnect reuses upstream/child FIDs and retained route completion,
  starts new owners running, and ignores late cleanup of old records. Stop
  releases active ownership before configuration deletion while preserving the
  existing route snapshot and in-memory deletion semantics.
- The first targeted run passed 137 tests and found six test-barrier failures;
  all six passed after placing the barrier after the real first payload write.
  Existing forwarding/recovery/handshake checks passed 39 tests. Full commands,
  exclusions and runtime evidence are in the C4b report; combined acceptance
  remains the C5 review stage.

## Transfer control C5: current contract and combined acceptance

- Current File_Transfer and TCP API documentation now distinguishes sender
  changes from receiver requests, including unconfirmed duplicate requests.
  Recovery documentation states the per-object busy rule, no active-source
  queue, no wildcard resume-all command and no new data-only retry loop.
- Historical queued/coalesced recovery and immediate sweep-release notes are
  explicitly superseded by C4a/C4b. The implementation report maps P01–P18 to
  runtime evidence, records the six implementation commits and retains known
  limitations rather than treating unit passes as global-property proof.
- Public signatures and RST transfer targets passed mechanical checks. Sphinx
  generated the HTML documentation successfully, and the actual control/API
  pages were inspected in a browser. Existing static-path and docstring
  warnings are retained in the report; no unrelated configuration was changed.
- The planned combined regression ran once: 640 passed and one skipped in
  761.28 seconds. The skip is the plaintext fixture's inapplicable re-key case;
  encrypted variants ran. C5 changes documentation only; final human acceptance
  and any commit, merge or push remain separate gates.
- After C5 verification, the user separately authorized increasing the CI Test
  job timeout from 10 to 30 minutes. Test commands, matrix and coverage policy
  are unchanged.

## Transfer control CI follow-up: portable write and connection barriers

- Live receive tests observe the real target write and verify admitted payload
  bytes instead of treating stat growth on an open buffered file as completion.
  Python 3.14's larger default buffer can retain the first 64 KiB chunk until
  close; resumed partial-file contents and offsets are still checked after the
  old receiver exits, and final content/ACK assertions remain intact.
- Wildcard scenarios retain the server's 0.0.0.0 bind while clients connect to
  loopback, which is also valid on Windows. Source-disconnect injection shuts
  down the actual client socket and still waits for both ends to exit.
- Production control and persistence behavior, test timeouts, crypto/async
  coverage, CI configuration and skip policy are unchanged. Native Windows
  results must come from a subsequent CI run; Linux is not Windows evidence.
- Verification passed 18 affected cases on Python 3.10 and the full control
  file on Linux CPython 3.14.4 with coverage: 71 passed, one existing plaintext
  re-key skip in 331.25 seconds. The subset's coverage is not a full-suite
  measurement. Native Windows still requires a subsequent Actions run.

## Windows reconnect follow-up: preserve the source's passive close

- The first CI follow-up's client-side shutdown let the source initiate FIN;
  the next Windows run connected from a fallback port instead of the retained
  route identity. The relay fault now half-closes the server's write side,
  waits for source EOF/receive-loop exit, releases the client socket normally,
  then waits for server unregister. The server reader keeps its own socket
  until its finally block, including asyncio/Proactor reads.
- Original endpoint, FID/route, content, ACK and worker-exit checks remain.
  Teardown failures now name surviving workers instead of reporting a bare
  boolean. No longer joins, retries, skips or production behavior changes.
- Linux verification cannot establish Windows port reuse or Proactor cleanup;
  native Windows Actions must confirm both after this change.
- Linux CPython 3.14.4 with coverage passed all three affected source-reconnect
  variants in 27.90 seconds, retaining original endpoint, content/ACK and
  worker-exit assertions. An intermediate immediate-close attempt failed the
  async unregister check; that result remains in the report. Windows native
  verification is still pending.

