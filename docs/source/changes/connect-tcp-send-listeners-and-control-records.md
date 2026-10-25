# Change note: connect_tcp send listeners and control-only records

> Per-change note for the `connect_tcp` send-side listeners, the outbound
> record stores, the control-socket split, and the `is_send_raw` merge. No
> PR number is assigned yet; rename the file when the PR is opened.

## Core logic of this change
- Problem: observers could see inbound traffic only on the control socket.
  Outbound traffic had no listener, and transfer-layer bytes (file, folder,
  forward) were invisible, which blocks streaming on top of the transport.
- Approach: every write goes through one `send_message` path and every
  side-channel read through `receive_message`; the listener and record calls
  sit at those two points only. Records are kept for control sockets only, so
  large transfer payloads never enter a store.
- `_send_raw` is removed. Plaintext protocol lines are written by
  `send_message(..., is_send_raw=True)`.

## Modules and interactions
- `PyFlow/network_api/connect_tcp.py`: `TCP_Server_Base` and `TCP_Client_Base`
  each gain `_control_sockets`, `_send_message_listeners`, four record stores,
  `_write_message` / `_after_send` (send side), `_notify_transfer_chunk`
  (receive side for non-control sockets), and `_append_record`.
- Call direction: `send_message` -> `_write_message` -> `_after_send` ->
  `_record_sent` and `_notify_message_sent`. `receive_message` and
  `_async_receive` -> `_notify_transfer_chunk`. Control lines still go
  `_process_client_line` / `receive_messages` -> `_notify_message_received` ->
  `_record_message` / `_record_event`.
- Tests: `test/integration/network_api/test_send_message_store.py`,
  `test_side_channel_records.py`; `test_tcp_transfer_resume.py` waits for
  server registration and the transfer port.
- Docs: `Network_APIs/TCP_Server_APIs.rst`, `TCP_Client_APIs.rst`.

## Implicit assumptions and easy-to-forget details
- A socket is a control socket only while it is in `_control_sockets`. It is
  added on accept (server `_register_client`) and after a successful connect
  (client `connect`). It is removed in server `_unregister_client`, client
  `close`, and client `_release_client_socket`. A new control path that skips
  these points will either lose records or leak transfer chunks into a store.
- Transfer sign tokens start with `/` (for example `/FER`). They are not
  recorded because their socket is not a control socket. The `/` rule alone
  would be wrong here.
- A write with `is_send_raw=True` is neither reported nor recorded. Any new
  caller that sends a protocol line through it must accept that it is hidden
  from listeners.
- Listeners run on the thread that does the I/O. On the asynchronous server
  path, raw chunk listeners run on the event loop thread.
- `messages_dict` holds plain text: any inbound control line not starting with
  `/`. The console `/send_msg` strips its own command name and sends only the
  payload, so a chat line arrives here as plain text. A raw `/send_msg` line
  sent through the public `send_message` API would be an event instead.
- Flushing: the four stores share `_flush_dict_locked`. The sender-key cleanup
  keeps a socket key while any of the four stores buffers that socket.

## Changing this in six months
- Duplication: the record, listener and notify code is written twice (server
  and client). This was a deliberate deferral (decision a). Extracting a
  shared mixin is the next refactor and should be its own change.
- Encrypted path (P7) is not covered by a runtime test for control-only
  records or chunk notification. Add one before relying on it.
- Log files now include `sent_messages_log.json` and `sent_events_log.json`
  under `network_api/.Flow`. Anything reading the logs must accept four files.
- Translations: the `.po` catalogues under `docs/source/locale/` still describe
  the previous `send_message` signature and store list. They are not updated
  here.
- Constraint: `send_message(..., is_send_raw=True)` must remain the only way to
  send plaintext on an encrypted channel. Bypassing it breaks the handshake's
  framing order.
