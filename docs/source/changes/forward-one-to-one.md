# Native TCP file forwarding: shared server queue budget

`max_mem_buff` now bounds the total pending downstream bytes of all native file and folder relays on one `TCP_Server_Base`. Previously each relay could buffer up to its own high-water mark plus one fan-out enqueue. Operators running concurrent relays should size **one** budget for their server. The constructor still takes MiB (default 2048); no Client or wire change is required. Updating only the Client does not change an older Server's behavior.

Each header or body segment is charged per downstream copy, including a writer's current send. A full budget delays readers; it does not pause the user task or complete it early. The ordinary transfer path, encryption, and final recipient acknowledgements are unchanged. This is not a cap on process RSS.
