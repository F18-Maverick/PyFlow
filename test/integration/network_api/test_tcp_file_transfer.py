"""Integration tests for file transfer (plain channel, no crypto)."""

import os
import threading
import time
import shlex

import pytest


from helpers import free_port, server_ready, wait_until

from PyFlow.network_api.connect_tcp import (
    TCP_Client_Base,
    TCP_Server_Base,
    parse_forward_originator,
)
from unittest.mock import MagicMock


def _isolate_log_files(instance, log_dir, name):
    """Keep an instance's message/event logs out of the shared project directory.

    Every instance defaults to the same ``messages_log.json`` /
    ``events_log.json`` under the project directory: concurrent writers then
    race on the same temp file and the losing flush is dropped with
    "log file update failed".
    """
    instance.messages_log_file = str(log_dir / f"{name}_messages_log.json")
    instance.events_log_file = str(log_dir / f"{name}_events_log.json")


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
    _isolate_log_files(server, tmp_path, "server")
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
    _isolate_log_files(client, tmp_path, "client")
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
    server.file_transfer_server_recv_server_start_thread("cid", server_sock, f"/file {src} 0")
    server_port = _wait_port(client)
    assert server_port is not None

    client.file_transfer_mode(str(src), "127.0.0.1", server_port, 0)

    assert wait_until(lambda: any(server_recv.iterdir()), timeout=10), (
        "server did not receive the file"
    )
    files = list(server_recv.iterdir())
    assert files[0].read_bytes() == payload


def test_transfer_thread_survives_a_stop_before_the_start_sign(pair, tmp_path):
    """A stop between the accept and the start sign must not kill the thread.

    ``send_message`` raises ``RuntimeError("connection error")`` once the
    instance stopped — the normal teardown signal between a closing thread and
    a receive thread — and an escape from this thread surfaces as
    ``PytestUnhandledThreadExceptionWarning`` (Windows CI saw it while a
    pub-key push was in flight during teardown).
    """
    server, client, _ = pair
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    src = tmp_path / "upload.bin"
    src.write_bytes(b"payload")

    escaped = []
    previous_hook = threading.excepthook

    def hook(args):
        escaped.append(args)
        previous_hook(args)

    real_send = server.send_message
    stopped_first = threading.Event()
    transfer_thread = []

    def stop_then_send(client_socket, message):
        if message is server.server_start_file_transfer_sign and not stopped_first.is_set():
            # the race under test: the stop lands after the accept and before
            # the transfer thread's start sign
            transfer_thread.append(threading.current_thread())
            stopped_first.set()
            server.stop()
        return real_send(client_socket, message)

    server.send_message = stop_then_send
    threading.excepthook = hook
    try:
        server_sock = server.clients[client.client_socket.getsockname()]["socket"]
        server.file_transfer_server_recv_server_start_thread("cid", server_sock, f"/file {src} 0")
        server_port = _wait_port(client)
        assert server_port is not None
        client.file_transfer_mode(str(src), "127.0.0.1", server_port, 0)
        assert wait_until(stopped_first.is_set, timeout=10), (
            "the server-side transfer never reached its start sign"
        )
        # this test's own thread, not a transfer thread another test left
        # parked; the excepthook stays installed until it is gone, or its
        # escape would be missed
        transfer_thread[0].join(timeout=10)
    finally:
        threading.excepthook = previous_hook
        client.close()
        server.stop()

    thread = transfer_thread[0]
    assert not thread.is_alive(), "the transfer thread did not end"
    failures = [args for args in escaped if args.thread is thread]
    assert not failures, (
        f"the transfer thread died with {failures[0].exc_type.__name__}: {failures[0].exc_value}"
    )


def test_custom_file_transfer_chunk_len_is_honored_in_both_directions(tmp_path):
    """A non-default ``file_transfer_chunk_len`` drives the stream.

    Both directions are covered: a payload split into many small chunks
    still arrives byte-identical on each receiver.
    """
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        file_transfer_chunk_len=1024,
    )
    _isolate_log_files(server, tmp_path, "server")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        file_transfer_chunk_len=1024,
    )
    _isolate_log_files(client, tmp_path, "client")
    recv_dir = tmp_path / "recv"
    recv_dir.mkdir()
    client.file_transfer_dir = str(recv_dir)
    assert client.connect()
    try:
        src = tmp_path / "chunked.bin"
        payload = os.urandom(64 * 1024 + 123)  # 64 chunks of 1 KiB, plus a tail
        src.write_bytes(payload)

        # server -> client (server sends, client writes to disk)
        client.file_transfer_client_recv_server_start_thread(
            "cid", client.client_socket, f"/file {src} 0"
        )
        server_port = _wait_port(server)
        assert server_port is not None
        server.file_transfer_mode(str(src), "127.0.0.1", server_port, 0)
        assert wait_until(lambda: any(recv_dir.iterdir()), timeout=15), (
            "client did not receive the file"
        )
        assert list(recv_dir.iterdir())[0].read_bytes() == payload

        # client -> server (client sends, server writes to disk)
        server_recv = tmp_path / "server_recv"
        server_recv.mkdir()
        server.file_transfer_dir = str(server_recv)
        server_sock = server.clients[client.client_socket.getsockname()]["socket"]
        server.file_transfer_server_recv_server_start_thread("cid", server_sock, f"/file {src} 0")
        client_port = _wait_port(client)
        assert client_port is not None
        client.file_transfer_mode(str(src), "127.0.0.1", client_port, 0)
        assert wait_until(lambda: any(server_recv.iterdir()), timeout=15), (
            "server did not receive the file"
        )
        assert list(server_recv.iterdir())[0].read_bytes() == payload
    finally:
        client.close()
        server.stop()


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


def test_server_receives_file_at_destination(pair, tmp_path):
    """Client /file with a destination dir: the server writes the file there
    instead of its default transfer directory."""
    server, client, _ = pair
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    src = tmp_path / "upload.bin"
    payload = os.urandom(8192)
    src.write_bytes(payload)
    dest = tmp_path / "dest"
    dest.mkdir()

    # paths must be quoted: shlex treats an unquoted backslash as an escape,
    # which mangles Windows paths like C:\Users\...
    server_sock = server.clients[client.client_socket.getsockname()]["socket"]
    server.file_transfer_server_recv_server_start_thread(
        "cid", server_sock, f'/file "{src}" "{dest}" 0'
    )
    server_port = _wait_port(client)
    assert server_port is not None

    client.file_transfer_mode(str(src), "127.0.0.1", server_port, 0)

    assert wait_until(lambda: any(dest.iterdir()), timeout=10), (
        "file was not written to the destination directory"
    )
    assert not any(server_recv.iterdir()), "file must not land in the default directory"
    files = list(dest.iterdir())
    assert files[0].read_bytes() == payload


def test_server_pushes_file_to_destination(pair, tmp_path):
    """Server /file with a destination dir: the client writes the file there
    instead of its default transfer directory."""
    server, client, _ = pair
    src = tmp_path / "payload.bin"
    payload = os.urandom(4096)
    src.write_bytes(payload)
    client_addr = client.client_socket.getsockname()
    dest = tmp_path / "dest"
    dest.mkdir()

    cmd = '/file "{}" "({}, {})" "{}"'.format(src, repr(client_addr[0]), client_addr[1], dest)
    server.file_transfer_server_recv_client_start(cmd, file_folder_abspath=None)

    assert wait_until(lambda: any(dest.iterdir()), timeout=10), (
        "client did not write the file to the destination directory"
    )
    files = list(dest.iterdir())
    assert files[0].read_bytes() == payload


def test_client_uploads_folder_to_destination(pair, tmp_path):
    """Client /file_folder with a destination dir: the folder structure is
    recreated under the destination on the server."""
    server, client, _ = pair
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    dest = tmp_path / "dest"
    dest.mkdir()
    folder = tmp_path / "data"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("hello")
    (folder / "sub" / "b.txt").write_text("world")

    client.folder_file_transfer_client_recv_client_start(f'/file_folder "{folder}" "{dest}"')

    # the folder keeps its own name under the destination (same structure as
    # the default receive directory, with the destination as its root)
    assert wait_until(lambda: (dest / "data" / "a.txt").exists(), timeout=10), (
        "a.txt not at destination"
    )
    assert wait_until(lambda: (dest / "data" / "sub" / "b.txt").exists(), timeout=10), (
        "b.txt not at destination"
    )
    assert (dest / "data" / "a.txt").read_text() == "hello"
    assert (dest / "data" / "sub" / "b.txt").read_text() == "world"
    assert not any(server_recv.iterdir()), "folder must not land in the default directory"


def test_server_pushes_folder_to_destination(pair, tmp_path):
    """Server /file_folder with a destination dir: the folder structure is
    recreated under the destination on the client."""
    server, client, _ = pair
    folder = tmp_path / "data"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("hello")
    (folder / "sub" / "b.txt").write_text("world")
    client_addr = client.client_socket.getsockname()
    dest = tmp_path / "dest"
    dest.mkdir()

    cmd = '/file_folder "{}" "({}, {})" "{}"'.format(
        folder, repr(client_addr[0]), client_addr[1], dest
    )
    server.folder_file_transfer_server_recv_client_start(cmd)

    assert wait_until(lambda: (dest / "data" / "a.txt").exists(), timeout=10), (
        "a.txt not at destination"
    )
    assert wait_until(lambda: (dest / "data" / "sub" / "b.txt").exists(), timeout=10), (
        "b.txt not at destination"
    )
    assert (dest / "data" / "a.txt").read_text() == "hello"
    assert (dest / "data" / "sub" / "b.txt").read_text() == "world"


def test_client_uploads_multiple_files_to_destination(pair, tmp_path):
    """Client /multiple_file with a destination dir: every file is written
    under the destination on the server."""
    server, client, _ = pair
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    dest = tmp_path / "dest"
    dest.mkdir()
    f1 = tmp_path / "one.bin"
    f1.write_bytes(b"one")
    f2 = tmp_path / "two.bin"
    f2.write_bytes(b"two")

    client.multiple_file_transfer_client_recv_client_start(f'/multiple_file "{f1}" "{f2}" "{dest}"')

    assert wait_until(lambda: (dest / "one.bin").exists(), timeout=10), "one.bin not at destination"
    assert wait_until(lambda: (dest / "two.bin").exists(), timeout=10), "two.bin not at destination"
    assert (dest / "one.bin").read_bytes() == b"one"
    assert (dest / "two.bin").read_bytes() == b"two"


def test_client_uploads_multiple_folders_to_destination(pair, tmp_path):
    """Client /multiple_file_folder with a destination dir: every folder is
    recreated under the destination on the server."""
    server, client, _ = pair
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    # destination is a remote-only path: it must not exist locally, or the
    # multiple-folder parser would treat it as another folder to transfer
    dest = tmp_path / "dest"
    folder_a = tmp_path / "data_a"
    folder_a.mkdir()
    (folder_a / "a.txt").write_text("hello")
    folder_b = tmp_path / "data_b"
    folder_b.mkdir()
    (folder_b / "b.txt").write_text("world")

    client.multiple_folder_file_transfer_client_recv_client_start(
        f'/multiple_file_folder "{folder_a}" "{folder_b}" "{dest}"'
    )

    assert wait_until(lambda: (dest / "data_a" / "a.txt").exists(), timeout=10), (
        "a.txt not at destination"
    )
    assert wait_until(lambda: (dest / "data_b" / "b.txt").exists(), timeout=10), (
        "b.txt not at destination"
    )
    assert (dest / "data_a" / "a.txt").read_text() == "hello"
    assert (dest / "data_b" / "b.txt").read_text() == "world"


def test_windows_paths_need_quoting_through_shlex():
    """Unquoted ``C:\\dir`` is mangled by shlex (backslash = escape), so
    transfer commands must quote Windows paths; the destination parser then
    sees the path intact. Runs on every OS: shlex semantics are POSIX."""
    from PyFlow.network_api.connect_tcp import _parse_destination_path

    # quoted paths survive shlex, and the trailing client id is skipped
    tokens = shlex.split('/file "C:\\src.bin" "C:\\dest" 0')
    assert tokens == ["/file", "C:\\src.bin", "C:\\dest", "0"]
    assert _parse_destination_path(tokens) == "C:\\dest"
    # unquoted backslashes are eaten -> a mangled, non-absolute path
    tokens = shlex.split("/file C:\\src.bin C:\\dest 0")
    assert tokens == ["/file", "C:src.bin", "C:dest", "0"]
    assert _parse_destination_path(tokens) == "C:dest"
    # folder creation with a quoted Windows destination
    tokens = shlex.split('/file_folder "/data" "C:\\dest"')
    assert _parse_destination_path(tokens) == "C:\\dest"
    # the folder command name is matched case-insensitively: an uppercase
    # /FILE_FOLDER from a console must still deliver the destination
    tokens = shlex.split('/FILE_FOLDER "/data" "C:\\dest"')
    assert _parse_destination_path(tokens) == "C:\\dest"


def test_server_console_file_command_preserves_path_case(pair, tmp_path, monkeypatch):
    """The server console matches command names case-insensitively (an
    uppercase /FILE must dispatch) while the file path reaches the transfer
    unchanged (POSIX paths are case-sensitive). A mixed-case path typed on
    the console used to fail with FileNotFoundError."""
    server, client, recv_dir = pair
    src = tmp_path / "MiXeD_Case.bin"
    payload = os.urandom(2048)
    src.write_bytes(payload)
    client_addr = client.client_socket.getsockname()
    cmd = '/FILE "{}" "({}, {})"'.format(src, repr(client_addr[0]), client_addr[1])
    inputs = iter([cmd, "/stop"])

    def fake_input():
        # "/stop" flips server.running False, which makes send_message refuse
        # the still-running transfer; only stop the console after the file
        # has arrived
        value = next(inputs)
        if value == "/stop":
            assert wait_until(lambda: any(recv_dir.iterdir()), timeout=10), (
                "client did not receive the mixed-case-path transfer"
            )
        return value

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(server, "stop", MagicMock())
    server.console_input()
    files = list(recv_dir.iterdir())
    assert files[0].name == "MiXeD_Case.bin"
    assert files[0].read_bytes() == payload


def _forward_client(server, recv_dir):
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=server.port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    _isolate_log_files(client, recv_dir.parent, recv_dir.name)
    client.file_transfer_dir = str(recv_dir)
    assert client.connect()
    return client


def test_parse_forward_originator():
    """The originator tuple in a pushed /file or /file_folder command is
    recovered; a direct send (the receiver's own address) is filtered out."""
    # the wire form produced by forward_target_command (tuple shlex-quoted)
    cmd = lambda *a: TCP_Server_Base.forward_target_command(None, *a)
    assert parse_forward_originator(cmd("file", "", "a.txt", ("127.0.0.1", 3000), 7)) == (
        "127.0.0.1:3000"
    )
    assert (
        parse_forward_originator(cmd("file", "", "a.txt", ("127.0.0.1", 3000), 7, "dest"))
        == "127.0.0.1:3000"
    )
    assert (
        parse_forward_originator(cmd("folder", "d", "a.txt", ("127.0.0.1", 3000), 7))
        == "127.0.0.1:3000"
    )
    assert (
        parse_forward_originator(cmd("folder", "d", "a.txt", ("127.0.0.1", 3000), 7, "dest"))
        == "127.0.0.1:3000"
    )
    # direct send: the tuple is the receiver's own address -> filtered
    assert (
        parse_forward_originator(
            cmd("file", "", "a.txt", ("127.0.0.1", 3000), 7),
            own_address="127.0.0.1:3000",
        )
        is None
    )
    # no tuple: plain direct send or a non-transfer command
    assert parse_forward_originator("/file 'a.txt' 7") is None
    assert parse_forward_originator("/pause_trans 3") is None


def test_forwarded_file_carries_originator(pair, tmp_path):
    """A streamed forward tags the pushed /file command with the originator's
    address: the receiver's file listener can attribute the transfer."""
    server, target, recv_dir = pair
    src = tmp_path / "payload.bin"
    payload = os.urandom(4096)
    src.write_bytes(payload)
    forwarder = _forward_client(server, tmp_path / "fwd")
    commands = []
    target.add_file_listener(lambda full_path, name, size, command: commands.append(command))
    t = target.client_socket.getsockname()
    cmd = '/forward_file "{}" "({}, {})"'.format(src, repr(t[0]), t[1])
    forwarder.forward_file_console(cmd)
    assert wait_until(lambda: (recv_dir / "payload.bin").exists(), timeout=15), (
        "forwarded file not received"
    )
    assert wait_until(lambda: len(commands) >= 1), f"no file event {commands=}"
    a = forwarder.client_socket.getsockname()
    own = target.client_socket.getsockname()
    originator = parse_forward_originator(commands[0], own_address=f"{own[0]}:{own[1]}")
    assert originator == f"{a[0]}:{a[1]}"
    forwarder.close()


def test_forward_file_to_multiple_clients(pair, tmp_path):
    """/forward_file relays one file from a client to several clients, with
    the server acting as an in-memory relay (no server-side disk write)."""
    server, target1, recv1 = pair
    recv2 = tmp_path / "recv2"
    recv2.mkdir()
    target2 = _forward_client(server, recv2)
    src = tmp_path / "payload.bin"
    payload = os.urandom(64 * 1024 + 123)
    src.write_bytes(payload)
    forwarder = _forward_client(server, tmp_path / "fwd")

    a1 = target1.client_socket.getsockname()
    a2 = target2.client_socket.getsockname()
    cmd = '/forward_file "{}" "({}, {})" "({}, {})"'.format(
        src, repr(a1[0]), a1[1], repr(a2[0]), a2[1]
    )
    forwarder.forward_file_console(cmd)

    # size-aware wait: exists() can be true while the receiver is still
    # writing; reading mid-write would see a truncated file
    def got(path):
        return path.exists() and path.stat().st_size == len(payload)

    assert wait_until(lambda: got(recv1 / "payload.bin"), timeout=15), (
        "target 1 did not receive the forwarded file"
    )
    assert wait_until(lambda: got(recv2 / "payload.bin"), timeout=15), (
        "target 2 did not receive the forwarded file"
    )
    assert (recv1 / "payload.bin").read_bytes() == payload
    assert (recv2 / "payload.bin").read_bytes() == payload
    target2.close()
    forwarder.close()


def test_forward_folder_to_client(pair, tmp_path):
    """/forward_folder relays a folder (structure preserved) from a client to
    another client through the in-memory relay."""
    server, target, recv_dir = pair
    folder = tmp_path / "data"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("hello")
    (folder / "sub" / "b.txt").write_text("world")
    forwarder = _forward_client(server, tmp_path / "fwd")

    a = target.client_socket.getsockname()
    cmd = '/forward_folder "{}" "({}, {})"'.format(folder, repr(a[0]), a[1])
    forwarder.forward_folder_console(cmd)

    assert wait_until(lambda: (recv_dir / "data" / "a.txt").exists(), timeout=15), (
        "a.txt not forwarded"
    )
    assert wait_until(lambda: (recv_dir / "data" / "sub" / "b.txt").exists(), timeout=15), (
        "sub/b.txt not forwarded"
    )
    assert (recv_dir / "data" / "a.txt").read_text() == "hello"
    assert (recv_dir / "data" / "sub" / "b.txt").read_text() == "world"
    forwarder.close()


def test_forward_skips_unreachable_and_server_targets(pair, tmp_path):
    """Unreachable addresses and the server itself are skipped; the reachable
    target still receives the file."""
    server, target, recv_dir = pair
    src = tmp_path / "payload.bin"
    payload = os.urandom(2048)
    src.write_bytes(payload)
    forwarder = _forward_client(server, tmp_path / "fwd")

    a = target.client_socket.getsockname()
    bad = ("127.0.0.1", 39999)  # not connected
    server_addr = (server.host, server.port)  # the server itself
    cmd = '/forward_file "{}" "({}, {})" "({}, {})" "({}, {})"'.format(
        src,
        repr(a[0]),
        a[1],
        repr(bad[0]),
        bad[1],
        repr(server_addr[0]),
        server_addr[1],
    )
    forwarder.forward_file_console(cmd)

    def got(path):
        return path.exists() and path.stat().st_size == len(payload)

    assert wait_until(lambda: got(recv_dir / "payload.bin"), timeout=15), (
        "reachable target did not receive the forwarded file"
    )
    assert (recv_dir / "payload.bin").read_bytes() == payload
    forwarder.close()


def test_forward_flow_control_pauses_uploader(pair, tmp_path, monkeypatch):
    """When the server's in-memory buffer exceeds max_mem_buff it pauses the
    uploader with /pause_trans and resumes it with /start_trans; the file is
    still delivered intact."""
    server, target, recv_dir = pair
    # strictly below one 64 KiB relay chunk, so every chunk overflows the
    # buffer and a /pause_trans -> /start_trans cycle is guaranteed (a limit
    # of exactly one chunk sits at the > comparison boundary and makes the
    # pause depend on thread scheduling)
    server.max_mem_buff = 32 * 1024
    src = tmp_path / "big.bin"
    payload = os.urandom(300 * 1024)
    src.write_bytes(payload)
    forwarder = _forward_client(server, tmp_path / "fwd")

    sent = []
    real_send = server.send_message

    def spy(sock, message):
        sent.append(message if isinstance(message, str) else "")
        return real_send(sock, message)

    monkeypatch.setattr(server, "send_message", spy)
    a = target.client_socket.getsockname()
    cmd = '/forward_file "{}" "({}, {})"'.format(src, repr(a[0]), a[1])
    forwarder.forward_file_console(cmd)

    def got(path):
        return path.exists() and path.stat().st_size == len(payload)

    assert wait_until(lambda: got(recv_dir / "big.bin"), timeout=25), (
        "forwarded file was not received"
    )
    assert (recv_dir / "big.bin").read_bytes() == payload
    assert any(str(m).startswith("/pause_trans") for m in sent), "expected a /pause_trans"
    assert any(str(m).startswith("/start_trans") for m in sent), "expected a /start_trans"
    forwarder.close()


def test_forward_file_with_destination(pair, tmp_path):
    """/forward_file with a trailing destination dir: targets write the
    forwarded file under the destination instead of their default dir."""
    server, target, recv_dir = pair
    src = tmp_path / "payload.bin"
    payload = os.urandom(8192)
    src.write_bytes(payload)
    dest = tmp_path / "dest"
    forwarder = _forward_client(server, tmp_path / "fwd")

    a = target.client_socket.getsockname()
    cmd = '/forward_file "{}" "({}, {})" "{}"'.format(src, repr(a[0]), a[1], dest)
    forwarder.forward_file_console(cmd)

    def got(path):
        return path.exists() and path.stat().st_size == len(payload)

    assert wait_until(lambda: got(dest / "payload.bin"), timeout=15), (
        "file not written to the destination"
    )
    assert (dest / "payload.bin").read_bytes() == payload
    assert not any(recv_dir.iterdir()), "default directory must stay empty"
    forwarder.close()


def test_forward_folder_with_destination(pair, tmp_path):
    """/forward_folder with a trailing destination dir: the folder structure
    is recreated under the destination on the target."""
    server, target, recv_dir = pair
    folder = tmp_path / "data"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("hello")
    (folder / "sub" / "b.txt").write_text("world")
    dest = tmp_path / "dest"
    forwarder = _forward_client(server, tmp_path / "fwd")

    a = target.client_socket.getsockname()
    cmd = '/forward_folder "{}" "({}, {})" "{}"'.format(folder, repr(a[0]), a[1], dest)
    forwarder.forward_folder_console(cmd)

    assert wait_until(lambda: (dest / "data" / "a.txt").exists(), timeout=15), (
        "a.txt not at the destination"
    )
    assert wait_until(lambda: (dest / "data" / "sub" / "b.txt").exists(), timeout=15), (
        "sub/b.txt not at the destination"
    )
    assert (dest / "data" / "a.txt").read_text() == "hello"
    assert (dest / "data" / "sub" / "b.txt").read_text() == "world"
    assert not any(recv_dir.iterdir()), "default directory must stay empty"
    forwarder.close()


def test_multiple_file_multiple_client_console_command(pair, tmp_path):
    """/multiple_file_multiple_client from the server console sends one file to
    every listed client (the console form used to be a silent no-op because
    the handler only dispatched /file- and /file_folder-prefixed messages)."""
    server, client, recv_dir = pair
    recv2 = tmp_path / "recv2"
    recv2.mkdir()
    client2 = TCP_Client_Base(
        host="127.0.0.1",
        port=server.port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    _isolate_log_files(client2, tmp_path, "client2")
    client2.file_transfer_dir = str(recv2)
    assert client2.connect()
    src = tmp_path / "broadcast.bin"
    payload = os.urandom(4096)
    src.write_bytes(payload)
    addr1 = client.client_socket.getsockname()
    addr2 = client2.client_socket.getsockname()
    dest = tmp_path / "dest"
    dest.mkdir()
    cmd = '/multiple_file_multiple_client "{}" "({}, {})" "({}, {})" "{}"'.format(
        src, repr(addr1[0]), addr1[1], repr(addr2[0]), addr2[1], dest
    )
    server.multiple_file_multiple_client_transfer_server_recv_client_start(cmd)
    assert wait_until(lambda: (dest / "broadcast.bin").exists(), timeout=10), (
        "client 1 did not receive the broadcast file at the destination"
    )
    assert not any(recv_dir.iterdir()), "default directory must stay empty on client 1"
    assert not any(recv2.iterdir()), "default directory must stay empty on client 2"
    assert (dest / "broadcast.bin").read_bytes() == payload
    client2.close()


def test_file_progress_and_speed_are_tracked(pair, tmp_path):
    """A running transfer is tracked in the progress stores of both sides.

    The uploader is held before its first data chunk (pause_fid), so the
    in-flight state — the entry at zero bytes with the full size — is observed
    deterministically; after the transfer the entries are gone, while the
    captured lists show every byte was folded in and the speed slots hold live
    values.
    """
    server, client, _ = pair
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    src = tmp_path / "upload.bin"
    payload = os.urandom(300 * 1024)
    src.write_bytes(payload)
    fid = 4242

    server_sock = server.clients[client.client_socket.getsockname()]["socket"]
    server.file_transfer_server_recv_server_start_thread("cid", server_sock, f"/file {src} {fid}")
    server_port = _wait_port(client)
    assert server_port is not None

    with client._forward_pause_cond:
        client._forward_pause[fid] = True
    sender = threading.Thread(
        target=client.file_transfer_mode,
        args=(str(src), "127.0.0.1", server_port, 0),
        kwargs={"pause_fid": fid, "fid": fid},
        daemon=True,
    )
    sender.start()
    try:
        assert wait_until(lambda: server.file_recv_progress.get(fid), timeout=10), (
            "the receive side did not register its progress"
        )
        assert wait_until(lambda: client.file_upload_progress.get(fid), timeout=10), (
            "the upload side did not register its progress"
        )
        recv_entry = server.file_recv_progress[fid]
        upload_entry = client.file_upload_progress[fid]
        assert recv_entry[:2] == [0, len(payload)]
        assert upload_entry[:2] == [0, len(payload)]
        assert recv_entry[3] == "upload.bin" and upload_entry[3] == "upload.bin"
    finally:
        with client._forward_pause_cond:
            client._forward_pause[fid] = False
            client._forward_pause_cond.notify_all()
        sender.join(timeout=15)
    assert not sender.is_alive(), "the upload did not finish"

    def got():
        path = server_recv / "upload.bin"
        return path.exists() and path.stat().st_size == len(payload)

    assert wait_until(got, timeout=15), "the server did not receive the file"
    assert (server_recv / "upload.bin").read_bytes() == payload
    # the captured entries tracked every chunk in place ...
    assert recv_entry[0] == len(payload) and upload_entry[0] == len(payload)
    assert recv_entry[2] > 0 and upload_entry[2] > 0
    # ... and were dropped when the transfer ended
    assert fid not in server.file_recv_progress
    assert fid not in client.file_upload_progress
    # the protocol-wide measurement covers the control layer as well
    assert server.recv_network_speed > 0
    assert client.upload_network_speed > 0


def test_download_progress_and_speed_are_tracked(pair, tmp_path):
    """The reverse direction is tracked too: the server uploads, the client
    downloads, and both progress stores follow the same fid.

    The server-side pause gate is created by the test because no server-side
    caller passes ``pause_fid`` (only forward uploads use the pause); the send
    loop reads both attributes like the client's.
    """
    server, client, recv_dir = pair
    src = tmp_path / "download.bin"
    payload = os.urandom(300 * 1024)
    src.write_bytes(payload)
    fid = 5150

    # the client side of the transfer: it believes the server sent a
    # "/file" command, so it opens a temporary server and advertises the
    # port to the server over the control channel
    client.file_transfer_client_recv_server_start_thread(
        "cid", client.client_socket, f"/file {src} {fid}"
    )
    server_port = _wait_port(server)
    assert server_port is not None

    server._forward_pause = {fid: True}
    server._forward_pause_cond = threading.Condition()
    sender = threading.Thread(
        target=server.file_transfer_mode,
        args=(str(src), "127.0.0.1", server_port, 0),
        kwargs={"pause_fid": fid, "fid": fid},
        daemon=True,
    )
    sender.start()
    try:
        assert wait_until(lambda: client.file_recv_progress.get(fid), timeout=10), (
            "the client did not register its download progress"
        )
        assert wait_until(lambda: server.file_upload_progress.get(fid), timeout=10), (
            "the server did not register its upload progress"
        )
        recv_entry = client.file_recv_progress[fid]
        upload_entry = server.file_upload_progress[fid]
        assert recv_entry[:2] == [0, len(payload)]
        assert upload_entry[:2] == [0, len(payload)]
        assert recv_entry[3] == "download.bin" and upload_entry[3] == "download.bin"
    finally:
        with server._forward_pause_cond:
            server._forward_pause[fid] = False
            server._forward_pause_cond.notify_all()
        sender.join(timeout=15)
    assert not sender.is_alive(), "the download did not finish"

    def got():
        path = recv_dir / "download.bin"
        return path.exists() and path.stat().st_size == len(payload)

    assert wait_until(got, timeout=15), "the client did not receive the file"
    assert (recv_dir / "download.bin").read_bytes() == payload
    assert recv_entry[0] == len(payload) and upload_entry[0] == len(payload)
    assert recv_entry[2] > 0 and upload_entry[2] > 0
    assert fid not in client.file_recv_progress
    assert fid not in server.file_upload_progress
    assert client.recv_network_speed > 0
    assert server.upload_network_speed > 0


def test_file_progress_and_speed_can_be_disabled(tmp_path):
    """With both switches off nothing is recorded: the progress stores stay
    empty and every speed value stays None, while the transfer still works."""
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        is_enable_file_transfer_progress_bar=False,
        is_enable_real_time_network_speed=False,
    )
    _isolate_log_files(server, tmp_path, "server")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        is_enable_file_transfer_progress_bar=False,
        is_enable_real_time_network_speed=False,
    )
    _isolate_log_files(client, tmp_path, "client")
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    assert client.connect()
    try:
        src = tmp_path / "upload.bin"
        payload = os.urandom(64 * 1024 + 7)
        src.write_bytes(payload)
        fid = 7

        server_sock = server.clients[client.client_socket.getsockname()]["socket"]
        server.file_transfer_server_recv_server_start_thread(
            "cid", server_sock, f"/file {src} {fid}"
        )
        server_port = _wait_port(client)
        assert server_port is not None

        with client._forward_pause_cond:
            client._forward_pause[fid] = True
        sender = threading.Thread(
            target=client.file_transfer_mode,
            args=(str(src), "127.0.0.1", server_port, 0),
            kwargs={"pause_fid": fid, "fid": fid},
            daemon=True,
        )
        sender.start()
        try:
            # the receiver is already in its data loop (its output file exists),
            # so the point where a tracked transfer would register has passed
            assert wait_until(lambda: any(server_recv.iterdir()), timeout=10), (
                "the receive side never opened its output file"
            )
            assert server.file_recv_progress == {}
            assert client.file_upload_progress == {}
        finally:
            with client._forward_pause_cond:
                client._forward_pause[fid] = False
                client._forward_pause_cond.notify_all()
            sender.join(timeout=15)
        assert not sender.is_alive(), "the upload did not finish"

        def got():
            path = server_recv / "upload.bin"
            return path.exists() and path.stat().st_size == len(payload)

        assert wait_until(got, timeout=15), "the server did not receive the file"
        assert (server_recv / "upload.bin").read_bytes() == payload
        assert server.file_recv_progress == {}
        assert client.file_upload_progress == {}
        assert server.upload_network_speed is None and server.recv_network_speed is None
        assert client.upload_network_speed is None and client.recv_network_speed is None
    finally:
        client.close()
        server.stop()
