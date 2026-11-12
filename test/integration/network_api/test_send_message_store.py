"""Tests for the outbound side: send listeners and the sent_* stores.

Contract on both TCP classes:
- every payload written by ``send_message`` (plaintext) is reported to the
  send listeners, text and bytes alike;
- ``is_send_raw`` protocol lines are neither reported nor recorded;
- ``sent_events_dict`` holds outbound "/" commands;
- ``sent_messages_dict`` holds outbound plain text;
- bytes payloads are never recorded in a store.
"""

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
    assert client.connect()
    yield server, client
    client.close()
    server.stop()


def _server_sock(server, client):
    return server.clients[client.client_socket.getsockname()]["socket"]


def _client_key(client):
    ip, port = client.client_socket.getsockname()
    return f"{ip}:{port}"


def test_server_send_listener_sees_text_and_commands(pair):
    server, client = pair
    sent = []
    server.add_send_message_listener(lambda target, msg: sent.append((target, msg)))
    sock = _server_sock(server, client)
    server.send_message(sock, "hello client")
    server.send_message(sock, "/client_alloc_port_range NO_LIMIT")
    assert wait_until(lambda: len(sent) >= 2), f"send listener not called {sent=}"
    assert (_client_key(client), "hello client") in sent
    assert (_client_key(client), "/client_alloc_port_range NO_LIMIT") in sent


def test_server_sent_stores_split_text_and_commands(pair):
    server, client = pair
    sock = _server_sock(server, client)
    server.send_message(sock, "plain outbound")
    server.send_message(sock, "/probe 1")
    assert wait_until(
        lambda: any(e[0] == "/probe 1" for e in server.sent_events_dict.get(sock, []))
    ), "command not recorded in sent_events_dict"
    texts = [e[0] for e in server.sent_messages_dict.get(sock, [])]
    assert "plain outbound" in texts
    assert all(e[0].startswith("/") for e in server.sent_events_dict[sock])


def test_is_send_raw_is_not_reported_or_recorded(pair):
    server, client = pair
    sent = []
    server.add_send_message_listener(lambda target, msg: sent.append(msg))
    sock = _server_sock(server, client)
    server.send_message(sock, "/crypto_mode 0", is_send_raw=True)
    server.send_message(sock, "after raw")
    assert wait_until(lambda: "after raw" in sent), f"listener never fired {sent=}"
    assert "/crypto_mode 0" not in sent
    assert not any(e[0] == "/crypto_mode 0" for e in server.sent_events_dict.get(sock, []))


def test_bytes_are_reported_but_not_recorded(pair):
    server, client = pair
    sent = []
    server.add_send_message_listener(lambda target, msg: sent.append(msg))
    sock = _server_sock(server, client)
    chunk = b"\x2fbinary-chunk"  # starts with "/" as a byte: must not be recorded
    server.send_message(sock, chunk)
    server.send_message(sock, "marker")
    assert wait_until(lambda: "marker" in sent), f"listener never fired {sent=}"
    assert chunk in sent
    recorded = [e[0] for e in server.sent_events_dict.get(sock, [])]
    recorded += [e[0] for e in server.sent_messages_dict.get(sock, [])]
    assert "binary-chunk" not in " ".join(recorded)


def test_client_send_listener_and_sent_stores(pair):
    server, client = pair
    sent = []
    client.add_send_message_listener(lambda target, msg: sent.append((target, msg)))
    client.send_message(client.client_socket, "hello server")
    client.send_message(client.client_socket, "/time")
    assert wait_until(lambda: len(sent) >= 2), f"client send listener not called {sent=}"
    sock = client.client_socket
    assert [e[0] for e in client.sent_messages_dict.get(sock, [])] == ["hello server"]
    assert [e[0] for e in client.sent_events_dict.get(sock, [])] == ["/time"]


def test_failed_send_is_not_reported(pair):
    server, client = pair
    sent = []
    client.add_send_message_listener(lambda target, msg: sent.append(msg))
    client.running = False  # client refuses the write: nothing was sent
    assert client.send_message(client.client_socket, "never sent") is False
    assert sent == []
    recorded = client.sent_messages_dict.get(client.client_socket, [])
    assert not any(e[0] == "never sent" for e in recorded)
