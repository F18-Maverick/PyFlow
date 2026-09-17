"""Integration tests for the asynic clients io mode of ``TCP_Server_Base``.

With ``is_asynic_clients_io=True`` the server serves every connection from one
asyncio event loop instead of one thread per client, and ``max_clients`` no
longer caps the connection count.
"""

import contextlib
import io
import os
import socket
import threading
import time

import pytest
from helpers import wait_until

from PyFlow.network_api import rsa_crypto
from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base

try:
    rsa_crypto.load_library()
    HAVE_LIB = True
except rsa_crypto.CryptoLibraryError:
    HAVE_LIB = False

_PORT_COUNTER = 64100  # below the ephemeral range and clear of the other test files' bases


def _next_port():
    global _PORT_COUNTER
    _PORT_COUNTER += 1
    return _PORT_COUNTER


def _redirect_crypto(crypto, tmp_path, ssh_dir, subdir="pub_key"):
    """Point a crypto instance's key directories at a temporary location."""
    crypto.pvt_key_dir = str(tmp_path / "pvt_key")
    crypto.pub_key_dir = str(tmp_path / subdir)
    crypto.ssh_dir = str(ssh_dir)
    crypto.registry_path = os.path.join(crypto.pub_key_dir, "pub_key.json")
    os.makedirs(crypto.pvt_key_dir, exist_ok=True)
    os.makedirs(crypto.pub_key_dir, exist_ok=True)


def _read_lines(sock, lines, timeout=10.0):
    """Read until ``lines`` newline-terminated lines arrived."""
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
    return buf


def _read_until(sock, needle, timeout=10.0):
    """Read until ``needle`` is seen in the received bytes."""
    sock.settimeout(0.2)
    buf = b""
    deadline = time.monotonic() + timeout
    while needle not in buf and time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            continue
        if not chunk:
            break
        buf += chunk
    return buf


def _raw_connect(port, greeting_lines=2, timeout=10.0):
    """Connect a raw socket to the server and return ``(socket, greeting)``."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    return sock, _read_lines(sock, greeting_lines)


def _start_server(**kwargs):
    """Start a server with the shared test settings and return it with its thread."""
    kwargs.setdefault("max_clients", 10)
    kwargs.setdefault("is_asynic_clients_io", False)
    kwargs.setdefault("is_enable_encrypto", False)
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=_next_port(),
        is_extend_command=True,
        is_input_command_in_console=False,
        **kwargs,
    )
    thread = threading.Thread(target=server.start_TCP_Server, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server, thread):
    """Stop the server and assert its accept loop returned."""
    server.stop()
    thread.join(timeout=10)
    assert not thread.is_alive(), "start_TCP_Server did not return after stop"


@pytest.fixture
def asynic_server():
    """Provide a running server that serves clients from an asyncio event loop."""
    server, thread = _start_server(is_asynic_clients_io=True, max_clients=1)
    try:
        yield server
    finally:
        _stop_server(server, thread)


@pytest.fixture
def threaded_server():
    """Provide a running server that serves each client in its own thread."""
    server, _thread = _start_server(max_clients=1)
    try:
        yield server
    finally:
        server.stop()  # a blocked accept() is not woken by closing the socket


def test_serves_more_clients_than_max_clients(asynic_server):
    """``max_clients`` is ignored: every connection gets a greeting."""
    assert asynic_server.is_asynic_clients_io is True
    assert asynic_server.max_clients == 1  # noqa: PLR2004
    socks = []
    try:
        for _ in range(8):
            sock, greeting = _raw_connect(asynic_server.port)
            socks.append(sock)
            assert b"Welcome!" in greeting
            assert b"/crypto_mode 0" in greeting
        assert wait_until(lambda: len(asynic_server.clients) == len(socks)), asynic_server.clients
    finally:
        for sock in socks:
            sock.close()


def test_message_and_command_are_served(asynic_server):
    """A plain line is acknowledged and stored; a command gets its response."""
    sock, _ = _raw_connect(asynic_server.port)
    try:
        sock.sendall(b"hello from a coroutine client\n")
        assert b"msg send: hello from a coroutine client" in _read_until(
            sock, b"msg send: hello"
        )
        stored = [entry[0] for entries in asynic_server.messages_dict.values() for entry in entries]
        assert stored == ["hello from a coroutine client"]

        sock.sendall(b"/time\n")
        assert b"server time:" in _read_until(sock, b"server time:")

        sock.sendall(b"/clients\n")
        assert b"online clients (1)" in _read_until(sock, b"online clients")
    finally:
        sock.close()


def test_broadcast_reaches_every_client(asynic_server):
    """One push from the server reaches all coroutine clients."""
    socks = []
    try:
        for _ in range(3):
            sock, _ = _raw_connect(asynic_server.port)
            socks.append(sock)
        assert wait_until(lambda: len(asynic_server.clients) == 3)  # noqa: PLR2004

        asynic_server.broadcast("broadcast from server")
        for sock in socks:
            assert b"broadcast from server" in _read_until(sock, b"broadcast from server")
    finally:
        for sock in socks:
            sock.close()


def test_disconnected_client_is_dropped(asynic_server):
    """A peer that closes frees its slot; the other coroutines keep serving."""
    dead, _ = _raw_connect(asynic_server.port)
    alive, _ = _raw_connect(asynic_server.port)
    try:
        assert wait_until(lambda: len(asynic_server.clients) == 2)  # noqa: PLR2004
        dead.close()
        assert wait_until(lambda: len(asynic_server.clients) == 1)

        alive.sendall(b"still here\n")
        assert b"msg send: still here" in _read_until(alive, b"msg send: still here")
    finally:
        alive.close()


def test_file_transfer_over_coroutine_control_channel(asynic_server, tmp_path):
    """A file pushed on a transfer socket arrives while the client runs as a coroutine."""
    asynic_server.file_transfer_dir = str(tmp_path)
    payload = os.urandom(8192)
    src = tmp_path / "upload.bin"
    src.write_bytes(payload)
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=asynic_server.port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    try:
        assert client.connect()
        assert wait_until(lambda: len(asynic_server.clients) == 1)
        server_sock = asynic_server.clients[client.client_socket.getsockname()]["socket"]
        asynic_server.file_transfer_server_recv_server_start_thread(
            "cid", server_sock, f"/file {src} 0"
        )
        port = _wait_transfer_port(client)
        assert port is not None, "client did not advertise a transfer port"

        client.file_transfer_mode(str(src), "127.0.0.1", port, 0)

        assert wait_until(lambda: any(tmp_path.iterdir())), "file was not received"
        received = next(path for path in tmp_path.iterdir() if path.is_file())
        assert received.read_bytes() == payload
    finally:
        client.close()


def _wait_transfer_port(instance, timeout=10.0):
    """Wait until an instance advertised a file transfer port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with instance.file_transfer_server_port_lock:
            ports = list(instance.file_server_port_list)
        if ports:
            return ports[0][0]
        time.sleep(0.05)
    return None


def test_stop_ends_the_event_loop(asynic_server):
    """`stop` releases the accept loop, so the listener stops accepting."""
    port = asynic_server.port
    asynic_server.stop()
    assert wait_until(lambda: not asynic_server.running)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2)


def test_thread_mode_still_refuses_clients_beyond_max_clients(threaded_server):
    """The default mode keeps its ``max_clients`` limit and refusal message."""
    first, _ = _raw_connect(threaded_server.port)
    try:
        assert wait_until(lambda: len(threaded_server.clients) == 1)
        second, refusal = _raw_connect(threaded_server.port, greeting_lines=1)
        try:
            assert b"Max connection mount" in refusal
            assert len(threaded_server.clients) == 1
        finally:
            second.close()
    finally:
        first.close()


@pytest.mark.skipif(
    not HAVE_LIB,
    reason="libcrypto_api not built (run cmake -S . -B build && cmake --build build first)",
)
def test_encrypted_channel_round_trip(tmp_path):
    """A coroutine client completes the RSA handshake and exchanges messages."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    server, thread = _start_server(
        is_asynic_clients_io=True,
        max_clients=1,
        is_enable_encrypto=True,
    )
    _redirect_crypto(server.crypto, tmp_path, ssh_dir, "pub_key")
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=server.port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(client.crypto, tmp_path, ssh_dir, "pub_key_client")
    try:
        assert client.connect()
        assert wait_until(lambda: client.client_socket in client._encrypted_sockets, timeout=20)
        assert wait_until(lambda: len(server._encrypted_sockets) == 1, timeout=20)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            client.send_message(client.client_socket, "encrypted hello")
            assert wait_until(
                lambda: sum(len(v) for v in server.messages_dict.values()) == 1,
                timeout=10,
            )
        stored = [entry[0] for entries in server.messages_dict.values() for entry in entries]
        assert stored == ["encrypted hello"]
        assert "msg send: encrypted hello" in buf.getvalue()
    finally:
        client.close()
        _stop_server(server, thread)
