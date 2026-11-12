"""Lifecycle tests for the TCP listener delivery contract.

``close()`` must join the client receive thread and both ``close()`` and
``stop()`` must stop new listener deliveries.  A stopped instance keeps no
listener callbacks, so nothing can be invoked after the call returns.
"""

import threading

import pytest

from helpers import free_port, isolate_instance_files, server_ready, wait_until

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
        is_print_log=False,
    )
    isolate_instance_files(server, tmp_path, "server")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        is_print_log=False,
    )
    isolate_instance_files(client, tmp_path, "client")
    assert client.connect()
    yield server, client
    client.close()
    server.stop()


def _server_sock(server, client):
    """Return the server-side socket for a connected client."""
    return server.clients[client.client_socket.getsockname()]["socket"]


def test_close_joins_receive_thread_and_clears_listeners(pair):
    """close() waits for the receive thread and stops delivering events."""
    _server, client = pair
    events = []
    client.add_message_listener(lambda sender, message: events.append(("msg", message)))
    client.add_send_message_listener(lambda target, message: events.append(("send", message)))
    client.add_file_listener(lambda *args: events.append(("file", args)))
    assert client.receive_thread is not None and client.receive_thread.is_alive()

    client.close()

    assert not client.receive_thread.is_alive()
    assert client._listeners_stopped is True
    assert client._message_listeners == []
    assert client._send_message_listeners == []
    assert client._file_listeners == []

    before = len(events)
    client._notify_message_received("peer", "late")
    client._notify_transfer_chunk(client.client_socket, b"late")
    client._notify_message_sent(client.client_socket, "late")
    client._notify_file_received("path", "name", 1, "/cmd")
    assert len(events) == before
    client.close()  # idempotent


def test_close_from_receive_listener_does_not_self_join(pair):
    """close() called on the receive thread must not join itself."""
    server, client = pair
    closed = threading.Event()

    def listener(sender, message):
        if message == "close-now" and not closed.is_set():
            client.close()
            closed.set()

    second_called = threading.Event()

    def second_listener(sender, message):
        if message == "close-now":
            second_called.set()

    client.add_message_listener(listener)
    client.add_message_listener(second_listener)
    server.send_message(_server_sock(server, client), "close-now")

    assert closed.wait(3.0), "close() from the receive listener did not return"
    assert not second_called.is_set(), "a later listener ran after close()"
    assert wait_until(lambda: not client.receive_thread.is_alive(), timeout=3.0)


def test_close_does_not_deliver_inflight_chunk_after_return(pair):
    """A chunk already read by the receive thread must not reach a listener."""
    server, client = pair
    marker_events = []

    def listener(sender, message):
        if isinstance(message, bytes):
            if b"RACE-MARKER" in message:
                marker_events.append(("bytes", message))
        elif "RACE-MARKER" in message:
            marker_events.append(("str", message))

    client.add_message_listener(listener)
    entered = threading.Event()
    release = threading.Event()
    original_measure = client._measure_network_speed

    def measure(payload, is_upload=False):
        if isinstance(payload, (bytes, bytearray)) and b"RACE-MARKER" in payload:
            entered.set()
            release.wait(5.0)
        return original_measure(payload, is_upload=is_upload)

    client._measure_network_speed = measure
    server_sock = _server_sock(server, client)
    server.send_message(server_sock, "RACE-MARKER")
    assert entered.wait(2.0), "the marker chunk never reached the client receive path"

    close_thread = threading.Thread(target=client.close, daemon=True)
    close_thread.start()
    try:
        assert wait_until(lambda: client._listeners_stopped, timeout=1.0)
        assert close_thread.is_alive(), "close() returned before joining the receive thread"
    finally:
        release.set()
    close_thread.join(3.0)

    assert not close_thread.is_alive()
    assert not client.receive_thread.is_alive()
    assert marker_events == []


def test_close_before_connect_is_idempotent(tmp_path):
    """close() without a session is a safe no-op and still stops listeners."""
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=free_port(),
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        is_print_log=False,
    )
    isolate_instance_files(client, tmp_path, "client")
    events = []
    client.add_message_listener(lambda sender, message: events.append(message))

    client.close()
    client.close()

    assert client.receive_thread is None
    assert client._listeners_stopped is True
    assert client._message_listeners == []
    assert events == []


def test_stop_clears_listeners_and_blocks_new_delivery(tmp_path):
    """stop() drops every listener and refuses any later notification."""
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=free_port(),
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        is_print_log=False,
    )
    isolate_instance_files(server, tmp_path, "server")
    events = []
    server.add_message_listener(lambda *args: events.append("msg"))
    server.add_send_message_listener(lambda *args: events.append("send"))
    server.add_file_listener(lambda *args: events.append("file"))

    server.stop()

    assert server._listeners_stopped is True
    assert server._message_listeners == []
    assert server._send_message_listeners == []
    assert server._file_listeners == []

    before = len(events)
    server._notify_message_received("peer", "late")
    server._notify_transfer_chunk(object(), b"late")
    server._notify_message_sent(object(), "late")
    server._notify_file_received("peer", "path", "name", 1, "/cmd")
    assert len(events) == before
