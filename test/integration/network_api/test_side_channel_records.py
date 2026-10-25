"""Side-channel reads and the control/transfer split of the message stores.

Contract on both TCP classes:
- a control socket's inbound lines are recorded (messages_dict / events_dict);
- a side-channel (transfer) socket's chunks are reported to the message
  listeners as raw bytes and are never recorded in any store, including the
  ``sign`` tokens the transfer protocol exchanges on that socket;
- outbound writes on a side-channel socket are reported to the send listeners
  but never recorded in sent_messages_dict / sent_events_dict, while outbound
  control commands are recorded in sent_events_dict.
"""

import os
import threading

import pytest

from helpers import free_port, server_ready, wait_until

from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base


@pytest.fixture
def pair(tmp_path):
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    server.messages_log_file = str(tmp_path / "server_messages_log.json")
    server.events_log_file = str(tmp_path / "server_events_log.json")
    server.sent_messages_log_file = str(tmp_path / "server_sent_messages_log.json")
    server.sent_events_log_file = str(tmp_path / "server_sent_events_log.json")
    server.file_trans_progress_file = str(tmp_path / "server_file_trans_progress.json")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    client.messages_log_file = str(tmp_path / "client_messages_log.json")
    client.events_log_file = str(tmp_path / "client_events_log.json")
    client.sent_messages_log_file = str(tmp_path / "client_sent_messages_log.json")
    client.sent_events_log_file = str(tmp_path / "client_sent_events_log.json")
    client.file_trans_progress_file = str(tmp_path / "client_file_trans_progress.json")
    recv_dir = tmp_path / "recv"
    recv_dir.mkdir()
    client.file_transfer_dir = str(recv_dir)
    assert client.connect()
    yield server, client, tmp_path
    client.close()
    server.stop()


def _server_sock(server, client):
    return server.clients[client.client_socket.getsockname()]["socket"]


def _push_command(client, src):
    """Build the console-form push command the suite uses for a client address."""
    addr = client.client_socket.getsockname()
    return '/file "{}" "({}, {})"'.format(src, repr(addr[0]), addr[1])


def test_server_push_chunks_reach_client_listeners_and_no_client_store(pair):
    """A large server->client push moves raw chunks over the side channel.

    The chunks reach the client's message listeners as bytes, and nothing the
    transfer exchanges on that socket lands in a client store.
    """
    server, client, tmp_path = pair
    size = 512 * 1024
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(size))
    chunks = []
    client.add_message_listener(lambda sender, msg: chunks.append(msg))
    before = {
        (id(store), key): len(entries)
        for store in client._record_dicts()
        for key, entries in store.items()
    }
    server.file_transfer_server_recv_client_start(
        _push_command(client, src), file_folder_abspath=None
    )

    def raw_bytes():
        return sum(len(m) for m in chunks if isinstance(m, bytes))

    assert wait_until(lambda: raw_bytes() >= size, timeout=30), (
        f"listeners saw {raw_bytes()} of {size} bytes"
    )
    for store in client._record_dicts():
        for key, entries in store.items():
            for entry in entries[before.get((id(store), key), 0):]:
                assert isinstance(entry[0], str)
                assert not entry[0].startswith(
                    (client.send_file_header_sign, client.send_file_data_sign)
                )


def test_side_channel_sign_reported_but_not_recorded_on_server(pair):
    """Side-channel sign writes are reported to the send listeners and never
    recorded. The control push command, sent on the control socket, is
    recorded in sent_events_dict under its own wire form."""
    server, client, tmp_path = pair
    sent = []
    server.add_send_message_listener(lambda target, msg: sent.append(msg))
    src = tmp_path / "small.bin"
    src.write_bytes(b"x" * 4096)
    server.file_transfer_server_recv_client_start(
        _push_command(client, src), file_folder_abspath=None
    )
    server_sock = _server_sock(server, client)
    assert wait_until(
        lambda: any(
            e[0].startswith("/file ") and str(src) in e[0]
            for e in server.sent_events_dict.get(server_sock, [])
        ),
        timeout=15,
    ), "the control push command was not recorded in sent_events_dict"
    assert wait_until(lambda: server.error_sign in sent, timeout=15), (
        "the side-channel sign was never reported to the send listeners"
    )
    assert not any(
        e[0].startswith(server.error_sign) for e in server.sent_events_dict.get(server_sock, [])
    ), "a side-channel sign was recorded in sent_events_dict"


def test_control_lines_still_recorded_on_both_sides(pair):
    """Control traffic keeps the record stores: a plain line lands in the
    messages store and a command in the events store, on each side."""
    server, client, _ = pair
    client.send_message(client.client_socket, "control plain")
    client.send_message(client.client_socket, "/time")
    sock = _server_sock(server, client)
    assert wait_until(
        lambda: any(e[0] == "control plain" for e in server.messages_dict.get(sock, []))
    ), "control line not recorded on the server"
    assert wait_until(
        lambda: any(e[0] == "/time" for e in server.events_dict.get(sock, []))
    ), "control command not recorded on the server"
    own = client.client_socket
    assert any(e[0] == "control plain" for e in client.sent_messages_dict.get(own, []))
    assert any(e[0] == "/time" for e in client.sent_events_dict.get(own, []))
