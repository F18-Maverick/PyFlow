"""The server-side ``/quit`` handshake: reply, then end the session."""

import threading

import pytest
from helpers import server_ready, wait_until

from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base

_PORT = 65200


@pytest.fixture
def pair(tmp_path):
    """Build a real server/client pair with the encrypted channel disabled."""
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=_PORT,
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
        port=_PORT,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    client.messages_log_file = str(tmp_path / "client_messages_log.json")
    client.events_log_file = str(tmp_path / "client_events_log.json")
    assert client.connect(), "client did not connect"
    assert wait_until(lambda: len(server.clients) == 1), "server never registered the client"
    yield server, client
    client.close()
    server.stop()


def test_quit_disconnects_the_client(pair):
    """Answer ``/quit`` and close the session server-side.

    The client here keeps its socket open on purpose: without the shutdown it
    would stay connected to a server that only replied, and the server would
    keep a dead session in ``clients``.
    """
    server, client = pair
    assert client.send_message(client.client_socket, "/quit") is True
    assert wait_until(lambda: not server.clients), "server kept the session after /quit"
    assert client.running is False, "client did not see the server close the connection"
