"""End-to-end file-transfer tests (plain channel, no crypto)."""

import os
import sys
import threading
import time

import pytest

package_dictionary = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if package_dictionary not in sys.path:
    sys.path.insert(0, package_dictionary)

from test_util import server_ready, wait_until

from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base  # noqa: E402

_PORT_COUNTER = 65420


def _next_port():
    global _PORT_COUNTER
    _PORT_COUNTER += 1
    return _PORT_COUNTER


@pytest.fixture
def pair(tmp_path):
    port = _next_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
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
    recv_dir = tmp_path / "recv"
    recv_dir.mkdir()
    client.file_transfer_dir = str(recv_dir)
    assert client.connect()
    yield server, client, recv_dir
    client.close()
    server.stop()


def _wait_port(client, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with client.file_transfer_server_port_lock:
            ports = list(client.file_server_port_list)
        if ports:
            return ports[0][0]
        time.sleep(0.05)
    return None


def test_client_receives_file_from_server(pair, tmp_path):
    server, client, recv_dir = pair
    src = tmp_path / "payload.bin"
    payload = os.urandom(2048)
    src.write_bytes(payload)

    # the client side of the transfer: it believes the server sent a
    # "/file" command, so it opens a temporary server and advertises the
    # port to the server over the control channel
    client.file_transfer_client_recv_server_start_thread(
        "cid", client.client_socket, f"/file {src} 0"
    )
    server_port = _wait_port(server)
    assert server_port is not None

    server.file_transfer_mode(str(src), "127.0.0.1", server_port, 0)

    assert wait_until(lambda: any(recv_dir.iterdir()), timeout=10), "file was not received"
    files = list(recv_dir.iterdir())
    assert files[0].read_bytes() == payload


def test_client_receives_large_file_from_server(pair, tmp_path):
    """A payload larger than one TCP chunk is reassembled correctly."""
    server, client, recv_dir = pair
    src = tmp_path / "big.bin"
    payload = os.urandom(300 * 1024)  # ~300 KB, many chunks
    src.write_bytes(payload)

    client.file_transfer_client_recv_server_start_thread(
        "cid", client.client_socket, f"/file {src} 1"
    )
    server_port = _wait_port(server)
    assert server_port is not None

    server.file_transfer_mode(str(src), "127.0.0.1", server_port, 0)

    assert wait_until(lambda: any(recv_dir.iterdir()), timeout=15), "file was not received"
    files = list(recv_dir.iterdir())
    assert files[0].read_bytes() == payload


def test_server_receives_file_from_client(pair, tmp_path):
    """Client pushes a file to the server (server-side receive path)."""
    server, client, _ = pair
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    src = tmp_path / "upload.bin"
    payload = os.urandom(8192)
    src.write_bytes(payload)

    # the server believes the client sent a "/file" command: it opens a
    # temporary server and advertises the port back to the client
    server_sock = server.clients[client.client_socket.getsockname()]["socket"]
    server.file_transfer_server_recv_server_start_thread(
        "cid", server_sock, f"/file {src} 0"
    )
    server_port = _wait_port(client)
    assert server_port is not None

    client.file_transfer_mode(str(src), "127.0.0.1", server_port, 0)

    assert wait_until(lambda: any(server_recv.iterdir()), timeout=10), (
        "server did not receive the file"
    )
    files = list(server_recv.iterdir())
    assert files[0].read_bytes() == payload


def test_server_pushes_file_with_quoted_client_addr(pair, tmp_path):
    """The console-style command with quoted parameters works end to end:
    /file "<path>" "('127.0.0.1', <port>)" - the quoted tuple survives
    shlex and ast.literal_eval.
    """
    server, client, recv_dir = pair
    src = tmp_path / "quoted.bin"
    payload = os.urandom(4096)
    src.write_bytes(payload)
    client_addr = client.client_socket.getsockname()
    # handle_client already registered the client with the server-side socket
    assert client_addr in server.clients
    cmd = '/file "{}" "({}, {})"'.format(src, repr(client_addr[0]), client_addr[1])
    server.file_transfer_server_recv_client_start(cmd, file_folder_abspath=None)
    assert wait_until(lambda: any(recv_dir.iterdir()), timeout=10), (
        "client did not receive the quoted-address transfer"
    )
    files = list(recv_dir.iterdir())
    assert files[0].read_bytes() == payload
