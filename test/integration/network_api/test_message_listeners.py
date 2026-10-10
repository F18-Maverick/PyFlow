"""Tests for the inbound message listeners (``add_message_listener``).

Contract on both TCP classes: every line received from the peer is reported
to the listeners, protocol commands included, so a listener observes all
inbound traffic while command dispatch stays unaffected. The file listener
keeps its own contract and is covered by the file-transfer tests.
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
    assert client.connect()
    yield server, client
    client.close()
    server.stop()


def _server_sock(server, client):
    """The server-side socket for a connected client."""
    return server.clients[client.client_socket.getsockname()]["socket"]


def _client_key(client):
    ip, port = client.client_socket.getsockname()
    return f"{ip}:{port}"


def test_server_listener_reports_commands_and_plain_messages(pair):
    """Every line a client sends is reported, a command and a plain
    message alike, attributed to the client's "ip:port"."""
    server, client = pair
    received = []
    server.add_message_listener(lambda client_id, text: received.append((client_id, text)))
    key = _client_key(client)
    client.send_message(client.client_socket, "/time")
    client.send_message(client.client_socket, "hello server")
    assert wait_until(
        lambda: (key, "/time") in received and (key, "hello server") in received
    ), f"lines never arrived {received=}"
    assert received.count((key, "/time")) == 1  # reported once, not per dispatch branch
    assert received.count((key, "hello server")) == 1


def test_server_listener_does_not_swallow_commands(pair):
    """Reporting a command leaves dispatch intact: the /time reply still
    reaches the client."""
    server, client = pair
    received = []
    server.add_message_listener(lambda client_id, text: received.append((client_id, text)))
    client.send_message(client.client_socket, "/time")
    assert wait_until(lambda: (_client_key(client), "/time") in received), (
        f"command never reported {received=}"
    )
    assert wait_until(
        lambda: any(
            e[0].startswith("server time:")
            for e in client.messages_dict.get(client.client_socket, [])
        )
    ), "the /time reply never reached the client"


def test_client_listener_reports_commands_and_plain_messages(pair):
    """Every line the server pushes is reported with a None sender, a
    protocol command and a plain message alike."""
    server, client = pair
    received = []
    client.add_message_listener(lambda sender, text: received.append((sender, text)))
    sock = _server_sock(server, client)
    server.send_message(sock, "/client_alloc_port_range NO_LIMIT")
    server.send_message(sock, "hello client")
    assert wait_until(
        lambda: (None, "/client_alloc_port_range NO_LIMIT") in received
        and (None, "hello client") in received
    ), f"lines never arrived {received=}"
    assert received.count((None, "hello client")) == 1


def test_client_listener_does_not_swallow_commands(pair):
    """Reporting a command leaves dispatch intact: a registered command
    handler still runs for the line the listener saw."""
    server, client = pair
    handled = []
    client.register_command(
        "/probe",
        lambda sock, addr, cmd: handled.append(cmd),
        where_to_run="server",
        run_in_thread=False,
    )
    received = []
    client.add_message_listener(lambda sender, text: received.append((sender, text)))
    server.send_message(_server_sock(server, client), "/probe 1")
    assert wait_until(lambda: (None, "/probe 1") in received), (
        f"command never reported {received=}"
    )
    assert received.count((None, "/probe 1")) == 1  # reported once, then dispatched
    assert wait_until(lambda: handled == ["/probe 1"]), f"handler never ran {handled=}"
