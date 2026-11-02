# Change note: one authoritative server FID route (atomic change 2)

> Implements only atomic change 2 of `docs/plans/transfer-fid-and-resume-unify-plan.md`.
> Atomic change 1 was reviewed and committed by the user. Source completion
> notifications, resume-protocol unification and receiver-owned allocation are
> separate, not-yet-approved changes.

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
