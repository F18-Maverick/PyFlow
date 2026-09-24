"""Integration tests for the instance logging levels.

``is_print_log`` / ``is_debug`` drive how much the server and the client print:
with ``is_print_log=False`` an instance prints nothing, with ``is_print_log=True``
and ``is_debug=False`` it logs command content and execution results only, and
``is_debug=True`` adds the execution-process lines. The port range is announced
to the connection that just joined, never to every client.
"""

import contextlib
import io
import socket
import threading
import time

import pytest
from helpers import wait_until

from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base

_PORT_COUNTER = 64400
PROCESS_LINES = (
    "new connection:",
    "connection count mount:",
    "client disconnected:",
    "max clients mount:",
)
RESULT_LINES = (
    "TCP server deployed on",
    "msg send: hello world",
    "server time:",
)
CLIENT_ONLY_LINES = ("connecting to", "connect success!", "[server]")
SESSION_TOKENS = PROCESS_LINES + RESULT_LINES + CLIENT_ONLY_LINES + ("hello world",)


def _next_port():
    global _PORT_COUNTER
    _PORT_COUNTER += 1
    return _PORT_COUNTER


def _run_session(port, server_log=True, server_debug=False, client_log=True, client_debug=False):
    """Run one server/client session and return everything the two printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        server = TCP_Server_Base(
            host="127.0.0.1",
            port=port,
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=False,
            is_debug=server_debug,
            is_print_log=server_log,
        )
        threading.Thread(target=server.start_TCP_Server, daemon=True).start()
        assert wait_until(lambda: server.running), "server did not start"
        client = TCP_Client_Base(
            host="127.0.0.1",
            port=port,
            client_host="127.0.0.1",
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=False,
            is_debug=client_debug,
            is_print_log=client_log,
        )
        try:
            assert client.connect()
            assert wait_until(lambda: len(server.clients) == 1), "client was not registered"
            client.send_message(client.client_socket, "hello world")
            assert wait_until(lambda: len(server.messages_dict) == 1), "message was not stored"
            client.send_message(client.client_socket, "/time")
            assert wait_until(lambda: bool(server.events_dict)), "command was not stored"
            if client_log:  # the reply is echoed by the client, not by the server
                wait_until(lambda: "server time:" in out.getvalue(), timeout=5)
        finally:
            client.close()
            assert wait_until(lambda: not server.clients)
            server.stop()  # a blocked accept() is not woken by closing the socket
    return out.getvalue()


def _raw_connect(port, lines=3, timeout=10.0):
    """Connect a raw socket and return ``(socket, received_bytes)``."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    sock.settimeout(0.2)
    buf = b""
    deadline = time.monotonic() + timeout
    while buf.count(b"\n") < lines and time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            continue
        if not chunk:
            break
        buf += chunk
    return sock, buf


def test_print_log_false_prints_nothing():
    """``is_print_log=False`` silences the server and the client completely."""
    port = _next_port()
    text = _run_session(port, server_log=False, client_log=False)
    # every line this session could print carries its port or one of the tokens
    # above, so a stray thread of another test cannot pass for this session
    assert str(port) not in text
    for token in SESSION_TOKENS:
        assert token not in text, token


def test_default_logs_command_and_result_only():
    """``is_debug=False`` keeps the command/result lines and drops the process ones."""
    text = _run_session(_next_port())
    for token in RESULT_LINES:
        assert token in text, token
    for token in PROCESS_LINES:
        assert token not in text, token
    assert "b'" not in text  # no raw byte dumps


def test_debug_logs_the_execution_process():
    """``is_debug=True`` adds the execution-process lines and the raw dumps."""
    text = _run_session(_next_port(), server_debug=True)
    for token in RESULT_LINES + PROCESS_LINES:
        assert token in text, token
    assert "b'" in text  # received bytes are dumped in debug mode


def test_log_flags_are_per_instance():
    """A silent client logs nothing even while its server logs everything."""
    text = _run_session(_next_port(), server_log=True, client_log=False)
    assert "TCP server deployed on" in text  # the server kept logging
    assert "hello world" in text  # the server logs the line it received
    for token in CLIENT_ONLY_LINES:
        assert token not in text, token


@pytest.mark.parametrize("asynic", [False, True], ids=["threads", "asyncio"])
def test_port_range_announcement_is_per_connection(asynic):
    """Every client is told the port range on connect, and nobody else is."""
    port = _next_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        is_print_log=False,
        is_asynic_clients_io=asynic,
    )
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert wait_until(lambda: server.running), "server did not start"
    socks = []
    try:
        for _ in range(3):
            sock, greeting = _raw_connect(port)
            socks.append(sock)
            assert b"Welcome!" in greeting
            assert b"/crypto_mode 0" in greeting
            assert b"/client_alloc_port_range NO_LIMIT" in greeting

        # a new connection must not re-announce the range to the established ones
        socks[0].settimeout(1.0)
        try:
            extra = socks[0].recv(4096)
        except TimeoutError:
            extra = b""
        assert extra == b""
    finally:
        for sock in socks:
            sock.close()
        server.stop()
