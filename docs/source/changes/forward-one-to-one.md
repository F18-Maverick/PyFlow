# Native TCP file forwarding: shared server queue budget

`max_mem_buff` now bounds the total pending downstream bytes of all native file and folder relays on one `TCP_Server_Base`. Previously each relay could buffer up to its own high-water mark plus one fan-out enqueue. Operators running concurrent relays should size **one** budget for their server. The constructor still takes MiB (default 2048); no Client or wire change is required. Updating only the Client does not change an older Server's behavior.

Each header or body segment is charged per downstream copy, including a writer's current send. A full budget delays readers; it does not pause the user task or complete it early. The ordinary transfer path, encryption, and final recipient acknowledgements are unchanged. This is not a cap on process RSS.

## Opt-in one-to-one scheduling on the Client

`TCP_Client_Base(..., is_forward_one_to_one=True)` makes each file entry and distinct target a separate transfer. The sender uses its existing `max_thread_num` file slots for initial sends and forwarded replays. Each pair keeps its own transfer ID and holds one slot through pause and final ACK. Items waiting at disconnect are not persisted or submitted on a replacement connection. `False` remains the default, and neither setting changes a stored route or the wire format.

With tracking enabled, two rows for the same source pathname can therefore be correct. A confirmed delivery to B does not retire C's interrupted transfer. C resumes on its retained route and destination, including when it has reached EOF but has not acknowledged delivery. In the default shared mode, the source still waits for every accepted target's ACK.
