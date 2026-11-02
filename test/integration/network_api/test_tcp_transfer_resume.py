"""Integration tests for the transfer reconnect (resume) mechanism.

A transfer interrupted by a disconnect keeps its progress entry and its
partial file; both sides write the entries to ``.Flow`` when the connection
drops, and after a reconnect the receiver asks the sender to continue the file
from the bytes it already holds. The tests drive that exchange over real
sockets, with the interrupted state either produced by a real disconnected
session or loaded from the persisted file exactly as a disconnect leaves it.
"""

import json
import os
import shlex
import socket
import threading
import time

import pytest

from helpers import free_port, isolate_instance_files, server_ready, wait_until

from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base


def _isolate(instance, tmp_path, name):
    """Keep an instance's logs and its transfer progress file out of the repo."""
    isolate_instance_files(instance, tmp_path, name)


def _wait_port(instance, timeout=10.0):
    """Wait for the transfer port the instance's peer announced."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with instance.file_transfer_server_port_lock:
            ports = list(instance.file_server_port_list)
        if ports:
            return ports[0][0]
        time.sleep(0.05)
    return None


def _redirect_crypto(crypto, tmp_path, ssh_dir, subdir="pub_key"):
    """Keep one instance's key material inside the test's temp directory."""
    crypto.pvt_key_dir = str(tmp_path / "pvt_key")
    crypto.pub_key_dir = str(tmp_path / subdir)
    crypto.ssh_dir = str(ssh_dir)
    crypto.registry_path = os.path.join(crypto.pub_key_dir, "pub_key.json")
    os.makedirs(crypto.pvt_key_dir, exist_ok=True)
    os.makedirs(crypto.pub_key_dir, exist_ok=True)


def _start_server(tmp_path, name="server", **kwargs):
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        **kwargs,
    )
    _isolate(server, tmp_path, name)
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    return server


def _start_client(tmp_path, server, name="client", recv_dir=None, **kwargs):
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=server.port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        **kwargs,
    )
    _isolate(client, tmp_path, name)
    if recv_dir is not None:
        client.file_transfer_dir = str(recv_dir)
    assert client.connect()
    return client


def _track_socket_workers(monkeypatch):
    """Retain real control, listener, relay and file workers for teardown."""
    workers = []
    real_start = threading.Thread.start

    def start(worker, *args, **kwargs):
        result = real_start(worker, *args, **kwargs)
        workers.append(worker)
        return result

    monkeypatch.setattr(threading.Thread, "start", start)
    return workers


def _close_socket_workers(server, clients, workers):
    for client in clients:
        client.close()
    server.stop()
    deadline = time.monotonic() + 20
    # Joining parents also lets any final child workers enter this list.
    for worker in workers:
        worker.join(timeout=max(0, deadline - time.monotonic()))
    assert not any(worker.is_alive() for worker in workers), (
        "socket workers survived teardown",
        [worker.name for worker in workers if worker.is_alive()],
    )


def test_progress_entries_round_trip_through_the_progress_file(tmp_path):
    """Both stores are written with their peer tuples and read back intact."""
    server = _start_server(tmp_path)
    receiver = ("127.0.0.1", 6000)
    upload_key = (receiver, 9)
    upload_json_key = json.dumps(upload_key, separators=(",", ":"))
    try:
        server.file_recv_progress[7] = [128, 1024, None, "/tmp/part.bin", ("127.0.0.1", 5000)]
        server.file_upload_progress[upload_key] = [64, 512, None, "/tmp/src.bin", receiver]
        server._persist_transfer_progress()

        with open(server.file_trans_progress_file, "r", encoding="utf-8") as f:
            written = json.load(f)
        assert written["download"]["7"] == [
            128,
            1024,
            None,
            "/tmp/part.bin",
            ["127.0.0.1", 5000],
        ]
        assert written["upload"][upload_json_key] == [
            64, 512, None, "/tmp/src.bin", list(receiver)
        ]

        server.file_recv_progress.clear()
        server.file_upload_progress.clear()
        assert server._load_transfer_progress() == 2  # noqa: PLR2004
        assert server.file_recv_progress[7] == [
            128,
            1024,
            None,
            "/tmp/part.bin",
            ("127.0.0.1", 5000),
        ]
        assert server.file_upload_progress[upload_key] == [
            64,
            512,
            None,
            "/tmp/src.bin",
            ("127.0.0.1", 6000),
        ]

        # a live transfer owns its fid: a load must not overwrite it
        server.file_recv_progress.clear()
        server.file_upload_progress.clear()
        server.file_recv_progress[7] = [999, 1024, None, "/tmp/live.bin", None]
        assert server._load_transfer_progress() == 1
        assert server.file_recv_progress[7] == [999, 1024, None, "/tmp/live.bin", None]
        assert server.file_upload_progress[upload_key][0] == 64  # noqa: PLR2004
    finally:
        server.stop()


def test_legacy_upload_progress_is_rejected_without_overwriting_snapshot(tmp_path):
    """Neither TCP class may silently migrate or replace a bare-fid upload file."""
    legacy_bytes = (
        b'{\n   "download": {},\n'
        b'   "upload": {"9": [64, 512, null, "/tmp/src.bin", ["127.0.0.1", 6000]]},\n'
        b'   "relays": {}\n}\n\n'
    )
    for name, tcp_class, persist_first in (
        ("server_load_first", TCP_Server_Base, False),
        ("server_persist_first", TCP_Server_Base, True),
        ("client_load_first", TCP_Client_Base, False),
        ("client_persist_first", TCP_Client_Base, True),
    ):
        # Keep connection/resume workers out of this on-disk preservation case.
        instance = tcp_class(
            host="127.0.0.1",
            port=free_port(),
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=False,
        )
        _isolate(instance, tmp_path, f"legacy_{name}")
        progress_path = tmp_path / f"legacy_{name}_file_trans_progress.json"
        messages = []
        real_log = instance._log

        def record(message):
            messages.append(message)
            real_log(message)

        instance._log = record
        try:
            progress_path.write_bytes(legacy_bytes)
            if persist_first:
                instance._persist_transfer_progress()
                assert progress_path.read_bytes() == legacy_bytes
                assert any(
                    "unsupported" in message.lower() and "upload" in message.lower()
                    for message in messages
                ), f"{name} did not log rejection before its first persistence"
            assert instance._load_transfer_progress() == 0
            assert instance.file_upload_progress == {}
            assert instance.file_recv_progress == {}
            assert any(
                "unsupported" in message.lower() and "upload" in message.lower()
                for message in messages
            ), f"{name} did not log rejection of the unsupported upload key"
            assert progress_path.read_bytes() == legacy_bytes

            # Persisting an empty current state must not erase rejected history.
            instance._persist_transfer_progress()
            assert progress_path.read_bytes() == legacy_bytes

            # A new scoped transfer is not permission to overwrite that history
            # either; ordinary shutdown persists the current stores once more.
            receiver = (
                (instance.host, instance.port)
                if tcp_class is TCP_Client_Base
                else ("127.0.0.1", 6000)
            )
            upload_key = (receiver, 9)
            instance.file_upload_progress[upload_key] = [
                0, 1024, None, str(tmp_path / f"{name}_new.bin"), receiver
            ]
            instance._persist_transfer_progress()
            assert progress_path.read_bytes() == legacy_bytes
        finally:
            if tcp_class is TCP_Server_Base:
                instance.stop()
            else:
                instance.close()
        assert progress_path.read_bytes() == legacy_bytes


def test_equal_server_upload_fids_are_isolated_by_receiver(tmp_path):
    """Pausing and clearing one real receiver leaves its equal-id sibling intact."""
    server = _start_server(tmp_path)
    clients = []
    senders = []
    receivers = []
    fid = 602
    results = {}
    try:
        sources = []
        payloads = []
        outputs = []
        for index in range(2):
            recv_dir = tmp_path / f"receiver_{index}"
            recv_dir.mkdir()
            client = _start_client(
                tmp_path, server, name=f"client_{index}", recv_dir=recv_dir
            )
            clients.append(client)
            receiver = client.client_socket.getsockname()
            receivers.append(receiver)
            assert wait_until(lambda: receiver in server.clients)
            src = tmp_path / f"payload_{index}.bin"
            payload = os.urandom((index + 1) * 128 * 1024 + 17)
            src.write_bytes(payload)
            sources.append(src)
            payloads.append(payload)
            outputs.append(recv_dir / src.name)
            assert server.pause_trans(fid, receiver) is False
            client.file_transfer_client_recv_server_start_thread(
                "cid", client.client_socket, f"/file {shlex.quote(str(src))} {fid}"
            )

        assert wait_until(lambda: len(server.file_server_port_list) == 2, timeout=10)
        # Both announcements have the same fid; select in reverse receiver
        # order so neither cache ordering nor a fid-only lookup can mask it.
        ports = {
            receiver: server._wait_transfer_server_port(fid, receiver, timeout=10)
            for receiver in reversed(receivers)
        }
        assert all(port is not None for port in ports.values())

        def send(receiver, src):
            port, stamp = ports[receiver]
            results[receiver] = server.file_transfer_mode(
                str(src), receiver[0], port, 0,
                control_socket=server.clients[receiver]["socket"],
                crypto=stamp, fid=fid, peer=receiver,
            )

        for receiver, src in zip(receivers, sources):
            sender = threading.Thread(target=send, args=(receiver, src), daemon=True)
            senders.append(sender)
            sender.start()

        keys = [(receiver, fid) for receiver in receivers]
        assert wait_until(
            lambda: all(key in server.file_upload_progress for key in keys), timeout=15
        ), "the equal-id uploads did not register independently"
        assert wait_until(
            lambda: all(fid in client.file_recv_progress for client in clients), timeout=10
        )
        rows = [server.file_upload_progress[key] for key in keys]
        assert rows[0] is not rows[1]
        for row, src, payload, receiver in zip(rows, sources, payloads, receivers):
            assert row == [0, len(payload), None, str(src), receiver]
        sockets = [server._active_transfer_sockets[key] for key in keys]
        assert sockets[0] is not sockets[1]
        assert all(key in server._active_transfer_fids for key in keys)
        assert server.pause_trans(fid, receivers[0]) is True

        assert server._drop_stale_transfers(receivers[0]) == 1
        assert sockets[0].fileno() == -1
        assert keys[0] not in server._active_transfer_sockets
        assert keys[0] not in server._active_transfer_fids
        assert server._active_transfer_sockets[keys[1]] is sockets[1]
        assert keys[1] in server._active_transfer_fids

        # Release only the dead leg's waiter and let its real cleanup finish.
        # If start/pause or cleanup is fid-only, the other leg now sends or dies.
        server.start_trans(fid, receivers[0])
        senders[0].join(timeout=15)
        assert not senders[0].is_alive(), "the cleared upload did not stop"
        assert results[receivers[0]] is False
        assert server.file_upload_progress[keys[1]] is rows[1]
        assert rows[1][0] == 0
        assert clients[1].file_recv_progress[fid][0] == 0
        assert server._transfer_pause[keys[1]] is True
        assert server._active_transfer_sockets[keys[1]] is sockets[1]
        assert senders[1].is_alive()

        assert server.start_trans(fid, receivers[1]) is True
        senders[1].join(timeout=20)
        assert not senders[1].is_alive(), "the unaffected receiver did not finish"
        assert results[receivers[1]] is True
        assert outputs[1].read_bytes() == payloads[1]
        assert rows[1][0] == len(payloads[1])
        assert wait_until(lambda: fid not in clients[1].file_recv_progress, timeout=10)
        assert keys[1] not in server.file_upload_progress
        assert keys[1] not in server._active_transfer_sockets
        assert keys[1] not in server._active_transfer_fids
    finally:
        for receiver in receivers:
            server.start_trans(fid, receiver)
        for client in clients:
            client.close()
        server.stop()
        for sender in senders:
            sender.join(timeout=10)


def test_equal_upload_fids_persist_and_reload_per_receiver(tmp_path):
    """Two real client addresses keep separate compact JSON keys and five fields."""
    server = _start_server(tmp_path)
    clients = []
    try:
        for index in range(2):
            clients.append(_start_client(tmp_path, server, name=f"client_{index}"))
        receivers = [client.client_socket.getsockname() for client in clients]
        assert receivers[0] != receivers[1]
        fid = 604
        keys = [(receiver, fid) for receiver in receivers]
        rows = [
            [128, 1024, None, str(tmp_path / "first.bin"), receivers[0]],
            [256, 2048, None, str(tmp_path / "second.bin"), receivers[1]],
        ]
        server.file_upload_progress.update(zip(keys, rows))
        server._persist_transfer_progress()
        with open(server.file_trans_progress_file, "r", encoding="utf-8") as f:
            written = json.load(f)
        json_keys = [json.dumps(key, separators=(",", ":")) for key in keys]
        assert set(written["upload"]) == set(json_keys)
        for json_key, row in zip(json_keys, rows):
            assert written["upload"][json_key] == row[:4] + [list(row[4])]

        server.file_upload_progress.clear()
        assert server._load_transfer_progress() == 2
        assert server.file_upload_progress == dict(zip(keys, rows))
        live = [512, 1024, None, str(tmp_path / "live.bin"), receivers[0]]
        server.file_upload_progress[keys[0]] = live
        del server.file_upload_progress[keys[1]]
        assert server._load_transfer_progress() == 1
        assert server.file_upload_progress[keys[0]] is live
        assert server.file_upload_progress[keys[1]] == rows[1]
    finally:
        for client in clients:
            client.close()
        server.stop()


def test_equal_inbound_and_outbound_fids_cleanup_only_their_own_sockets(tmp_path):
    """Receive cleanup cannot close an equal-id upload, including a client forward."""
    server = _start_server(tmp_path)
    client = None
    other = None
    sockets = []
    try:
        client = _start_client(tmp_path, server)
        other = _start_client(tmp_path, server, name="other")
        client_peer = client.client_socket.getsockname()
        server_peer = (client.host, client.port)
        forwarded_peer = other.client_socket.getsockname()
        cases = (
            (server, client_peer, forwarded_peer, forwarded_peer),
            (client, forwarded_peer, server_peer, forwarded_peer),
        )
        for instance, inbound_peer, receiver, logical_peer in cases:
            inbound, inbound_remote = socket.socketpair()
            outbound, outbound_remote = socket.socketpair()
            sockets.extend((inbound, inbound_remote, outbound, outbound_remote))
            inbound_remote.settimeout(2)
            outbound_remote.settimeout(2)
            fid = 606
            upload_key = (receiver, fid)
            recv_entry = instance._file_progress_start(
                instance.file_recv_progress, fid, 1024,
                str(tmp_path / "received.bin"), peer=inbound_peer,
            )
            upload_entry = instance._file_progress_start(
                instance.file_upload_progress, upload_key, 2048,
                str(tmp_path / "source.bin"), peer=logical_peer,
            )
            instance._register_transfer_socket(fid, inbound)
            instance._register_transfer_socket(upload_key, outbound)
            assert instance.pause_trans(fid, receiver) is True
            assert len(recv_entry) == len(upload_entry) == 5
            assert upload_entry[4] == logical_peer

            assert instance._drop_stale_transfers(inbound_peer) == 1
            assert inbound_remote.recv(1) == b""
            assert fid not in instance._active_transfer_sockets
            assert fid not in instance._active_transfer_fids
            instance._file_progress_finish(instance.file_recv_progress, fid, recv_entry)
            instance._release_transfer_socket(fid, inbound)
            assert fid not in instance.file_recv_progress
            assert instance.file_upload_progress[upload_key] is upload_entry
            assert upload_key in instance._active_transfer_fids
            assert instance._active_transfer_sockets[upload_key] is outbound
            assert instance._transfer_pause[upload_key] is True
            outbound.sendall(b"x")
            assert outbound_remote.recv(1) == b"x"

            # The client's receiver is its server even when row[4] names the
            # logical forward target; cleanup must use the scoped namespace.
            assert instance._drop_stale_transfers(receiver) == 1
            assert outbound_remote.recv(1) == b""
            assert upload_key not in instance._active_transfer_sockets
            assert upload_key not in instance._active_transfer_fids
            instance._file_progress_finish(
                instance.file_upload_progress, upload_key, upload_entry
            )
            instance._release_transfer_socket(upload_key, outbound)
            assert upload_key not in instance.file_upload_progress
            instance.start_trans(fid, receiver)
    finally:
        for sock in sockets:
            sock.close()
        if other is not None:
            other.close()
        if client is not None:
            client.close()
        server.stop()


def test_paused_transfer_is_persisted_when_the_connection_drops(tmp_path):
    """A disconnect during a transfer writes both stores for the next connect.

    The uploader is held paused, so the in-flight state is deterministic: the
    receiver keeps its (empty) partial file, and both sides persist their
    entries — the receiver with the sender's address, the uploader with the
    receiver's — under the protocol's ``.Flow`` directory.
    """
    server = _start_server(tmp_path)
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    client = None
    try:
        client = _start_client(tmp_path, server)
        src = tmp_path / "payload.bin"
        payload = os.urandom(64 * 1024)
        src.write_bytes(payload)
        fid = 76  # even low bit: a plain leg's id

        server_sock = server.clients[client.client_socket.getsockname()]["socket"]
        sender_id = "%s:%s" % client.client_socket.getsockname()
        server.file_transfer_server_recv_server_start_thread(
            sender_id, server_sock, f"/file {src} {fid}"
        )
        assert _wait_port(client) is not None

        client.pause_trans(fid)  # hold the leg before its first chunk
        sender = threading.Thread(
            target=client.file_transfer_mode,
            args=(str(src), "127.0.0.1", _wait_port(client) or 0, 0),
            kwargs={"fid": fid, "peer": (server.host, server.port)},
            daemon=True,
        )
        sender.start()
        assert wait_until(lambda: server.file_recv_progress.get(fid)), (
            "the receiver never registered the transfer"
        )

        # drop the control connection the way a network fault would; the
        # receive loop persists the transfers that died with it
        client.client_socket.shutdown(2)
        assert wait_until(
            lambda: os.path.exists(client.file_trans_progress_file), timeout=10
        ), "the client did not persist its interrupted transfers"

        with open(client.file_trans_progress_file, "r", encoding="utf-8") as f:
            client_state = json.load(f)
        upload_json_key = json.dumps(((client.host, client.port), fid), separators=(",", ":"))
        assert client_state["upload"][upload_json_key][3] == str(src)
        assert client_state["upload"][upload_json_key][4] == [server.host, server.port]

        assert wait_until(
            lambda: os.path.exists(server.file_trans_progress_file), timeout=10
        ), "the server did not persist its interrupted transfers"
        with open(server.file_trans_progress_file, "r", encoding="utf-8") as f:
            server_state = json.load(f)
        entry = server_state["download"][str(fid)]
        assert entry[3] == str(server_recv / "payload.bin")
        assert entry[4] == list(client.client_socket.getsockname())

        client.start_trans(fid)  # release the paused leg so the test can end
        sender.join(timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_upload_resumes_after_a_reconnect(tmp_path):
    """A client upload interrupted by a disconnect finishes after reconnecting.

    The client reconnects automatically (``is_auto_reconnect``); the server
    reads the persisted state when it comes back and asks it to continue the
    file from the bytes already on disk — the partially transferred file
    completes without any byte being sent twice.
    """
    server = _start_server(tmp_path)
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    client = None
    try:
        src = tmp_path / "payload.bin"
        payload = os.urandom(256 * 1024)
        src.write_bytes(payload)
        fid = 100  # even low bit: a plain leg's id

        # a fixed client port keeps the client's address stable across the
        # reconnect, so the server can match the interrupted transfer to it
        client = TCP_Client_Base(
            host="127.0.0.1",
            port=server.port,
            client_host="127.0.0.1",
            client_port=free_port(),
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=False,
            is_auto_reconnect=True,
        )
        _isolate(client, tmp_path, "client")
        threading.Thread(target=client.start_TCP_client, daemon=True).start()
        assert wait_until(lambda: client.running and client.client_socket is not None)
        client_addr = client.client_socket.getsockname()

        # the server registers an accepted client on its own thread: wait for it
        assert wait_until(lambda: client_addr in server.clients, timeout=10), (
            "server never registered the reconnecting client"
        )
        server_sock = server.clients[client_addr]["socket"]
        client_id = "%s:%s" % client_addr
        server.file_transfer_server_recv_server_start_thread(
            client_id, server_sock, f"/file {shlex.quote(str(src))} {fid}"
        )
        transfer_port = _wait_port(client)
        assert transfer_port is not None, "the client never announced a transfer port"

        client.pause_trans(fid)  # freeze the leg, then give it a partial file
        sender = threading.Thread(
            target=client.file_transfer_mode,
            args=(str(src), "127.0.0.1", transfer_port, 0),
            kwargs={"fid": fid, "peer": (server.host, server.port)},
            daemon=True,
        )
        sender.start()
        assert wait_until(lambda: server.file_recv_progress.get(fid)), (
            "the receiver never registered the transfer"
        )
        partial = server_recv / "payload.bin"
        half = len(payload) // 2
        with open(partial, "wb") as f:  # what an interrupted transfer leaves behind
            f.write(payload[:half])

        # a fault drops the control connection: the server's side goes away,
        # the way a crash looks to the client. The client's own release then
        # aborts the dead session (shutdown + linger-0 close), which keeps its
        # configured port out of TIME_WAIT: the reconnect must be able to take
        # the same port back (Windows refuses it otherwise) and the server must
        # match the stored transfer to the same peer address.
        dead_socket = client.client_socket
        server_side = server.clients[client_addr]["socket"]
        server_side.shutdown(2)  # a read blocked in the server only wakes on a shutdown
        server_side.close()
        assert wait_until(lambda: dead_socket.fileno() == -1, timeout=20), (
            "the reconnect did not release the dead session's socket"
        )
        assert wait_until(lambda: server.clients.get(client_addr) is not None, timeout=20), (
            "the client did not come back"
        )
        assert client.client_socket.getsockname() == client_addr, (
            "the client came back from a different port"
        )

        def got():
            return partial.exists() and partial.stat().st_size == len(payload)

        assert wait_until(got, timeout=30), "the resumed upload did not finish"
        assert partial.read_bytes() == payload
        assert wait_until(lambda: fid not in server.file_recv_progress, timeout=10)
        assert wait_until(
            lambda: ((client.host, client.port), fid) not in client.file_upload_progress,
            timeout=10,
        )
        client.start_trans(fid)
        sender.join(timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_download_resumes_from_the_partial_file(tmp_path):
    """The receiver drives a resume of a file the server was sending.

    The interrupted state is exactly what a disconnect leaves: the partial
    file on disk plus the persisted entry naming the server as the peer. The
    receiver asks the sender to continue from the partial file's length.
    """
    server = _start_server(tmp_path)
    client = None
    try:
        recv_dir = tmp_path / "client_recv"
        recv_dir.mkdir()
        client = _start_client(tmp_path, server, recv_dir=recv_dir)
        src = tmp_path / "download.bin"
        payload = os.urandom(128 * 1024)
        src.write_bytes(payload)
        fid = 202  # even low bit: a plain leg's id

        partial = recv_dir / "download.bin"
        with open(partial, "wb") as f:
            f.write(payload[:4096])
        client.file_recv_progress[fid] = [
            len(payload[:4096]),
            len(payload),
            None,
            str(partial),
            (server.host, server.port),
        ]
        upload_key = (client.client_socket.getsockname(), fid)
        server.file_upload_progress[upload_key] = [
            0,
            len(payload),
            None,
            str(src),
            client.client_socket.getsockname(),
        ]

        client._request_resumes()  # the same call connect() dispatches on reconnect

        def got():
            return partial.stat().st_size == len(payload)

        assert wait_until(got, timeout=20), "the resumed download did not finish"
        assert partial.read_bytes() == payload
        assert wait_until(lambda: fid not in client.file_recv_progress, timeout=10)
        assert wait_until(lambda: upload_key not in server.file_upload_progress, timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_forwarded_file_resumes_through_the_server(tmp_path, monkeypatch):
    """A target that lost a forwarded file gets the remainder re-sent.

    The target asks the server to continue the transfer; the server routes
    the request to the originator (its relay record names it) and the
    originator re-forwards only what the target is missing. Both the target's
    download entry and the originator's upload entry name the original peer,
    never the relay.
    """
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    recv_dir = tmp_path / "target_recv"
    recv_dir.mkdir()
    target = None
    forwarder = None
    try:
        target = _start_client(tmp_path, server, name="target", recv_dir=recv_dir)
        forwarder = _start_client(tmp_path, server, name="forwarder")
        src = tmp_path / "payload.bin"
        payload = os.urandom(96 * 1024)
        src.write_bytes(payload)
        fid = 303  # odd low bit: a relay leg's id
        tfid = 305  # odd low bit: a relay target's id
        target_addr = target.client_socket.getsockname()
        forwarder_addr = forwarder.client_socket.getsockname()

        # the state a disconnect leaves behind: the target's partial file and
        # both sides' persisted entries, plus the relay record the server
        # keeps for the dead relay
        partial = recv_dir / "payload.bin"
        with open(partial, "wb") as f:
            f.write(payload[:2048])
        target.file_recv_progress[tfid] = [
            2048,
            len(payload),
            None,
            str(partial),
            forwarder_addr,
        ]
        upload_key = ((forwarder.host, forwarder.port), fid)
        forwarder.file_upload_progress[upload_key] = [0, len(payload), None, str(src), target_addr]
        forwarder._forward_resume_records[fid] = {
            "fid": fid,
            "kind": "file",
            "rel_dir": "",
            "fname": "payload.bin",
            "path": str(src),
            "destination": None,
            "targets": [list(target_addr)],
        }
        assert server._register_fid_route(
            fid, forwarder_addr, {target_addr: tfid}
        ) is not None

        assert target._request_forward_resume(
            tfid, target.file_recv_progress[tfid], forwarder_addr
        )

        def got():
            return partial.stat().st_size == len(payload)

        assert wait_until(got, timeout=20), "the forwarded resume did not finish"
        assert partial.read_bytes() == payload
        assert wait_until(lambda: tfid not in target.file_recv_progress, timeout=10)
        assert wait_until(lambda: upload_key not in forwarder.file_upload_progress, timeout=10)
    finally:
        clients = [client for client in (forwarder, target) if client is not None]
        _close_socket_workers(server, clients, workers)


@pytest.mark.parametrize("kind", ["file", "folder"])
@pytest.mark.parametrize("successful_targets", [0, 1], ids=["zero-ack", "partial-ack"])
def test_forward_ack_loss_retains_state_and_resumes_original_ids(
    tmp_path, monkeypatch, kind, successful_targets
):
    """Only real final ACKs complete targets, including confirmation at EOF."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    release_ack = threading.Event()
    resume_ack = threading.Event()
    try:
        source = _start_client(tmp_path, server, name="source")
        clients.append(source)
        receivers = []
        outputs = []
        rel_dir = "bundle/nested" if kind == "folder" else ""
        src = tmp_path / "ack_lost.bin"
        payload = os.urandom(64 * 1024 + 37)
        src.write_bytes(payload)
        for index in range(2):
            recv_dir = tmp_path / f"ack_target_{index}"
            recv_dir.mkdir()
            target = _start_client(
                tmp_path, server, name=f"target_{index}", recv_dir=recv_dir
            )
            clients.append(target)
            receivers.append(target)
            outputs.append(recv_dir / rel_dir / src.name)
        target_addrs = [target.client_socket.getsockname() for target in receivers]
        source_addr = source.client_socket.getsockname()
        assert wait_until(lambda: all(
            addr in server.clients for addr in [source_addr, *target_addrs]
        ))
        failed = target_addrs[successful_targets:]
        losing_ack = {addr: True for addr in failed}
        ack_attempts = {addr: 0 for addr in target_addrs}
        held_target = failed[0]

        def intercept_ack(target, addr):
            real_send = target.send_message

            def send(sock, message, is_send_raw=False):
                if message == target.server_received_file_data_sign:
                    ack_attempts[addr] += 1
                    if losing_ack.get(addr, False):
                        # Both supported failure forms must retain the row after
                        # all bytes reached disk, without sending a final ACK.
                        if successful_targets == 0:
                            raise ConnectionError("injected final target ACK loss")
                        return False
                    if addr == held_target and ack_attempts[addr] == 2:
                        resume_ack.set()
                        if not release_ack.wait(timeout=10):
                            raise TimeoutError("the resumed final ACK was not released")
                return real_send(sock, message, is_send_raw=is_send_raw)

            monkeypatch.setattr(target, "send_message", send)

        for target, addr in zip(receivers, target_addrs):
            intercept_ack(target, addr)

        dispatches = []
        real_dispatch = server.forward_item_to

        def observe_dispatch(target, kind, rel_dir, fname, originator_addr, tfid,
                             destination_path=None):
            dispatches.append((target, tfid))
            return real_dispatch(
                target, kind, rel_dir, fname, originator_addr, tfid, destination_path
            )

        monkeypatch.setattr(server, "forward_item_to", observe_dispatch)
        calls = []
        outcomes = []
        real_forward = source._forward_one

        def observe_forward(*args, **kwargs):
            call = {
                "targets": list(args[4]),
                "relay_fid": kwargs.get("relay_fid"),
                "offset": kwargs.get("resume_offset", 0),
            }
            replay_fid = call["relay_fid"]
            if replay_fid is not None:
                call["record"] = source._forward_resume_records.get(replay_fid)
                call["upload"] = source.file_upload_progress.get(
                    ((source.host, source.port), replay_fid)
                )
            calls.append(call)
            result = real_forward(*args, **kwargs)
            outcomes.append(result)
            return result

        monkeypatch.setattr(source, "_forward_one", observe_forward)
        resume_requests = []
        real_resume = source._resume_forward

        def observe_resume(target, fid, offset):
            result = real_resume(target, fid, offset)
            resume_requests.append((target, fid, offset))
            return result

        monkeypatch.setattr(source, "_resume_forward", observe_resume)
        upstream_acks = []
        real_server_send = server.send_message

        def observe_upstream_ack(sock, message, is_send_raw=False):
            marker = message.encode("utf-8") if isinstance(message, str) else message
            written = real_server_send(sock, message, is_send_raw=is_send_raw)
            if marker == server.server_received_file_data_sign.encode("utf-8") and written:
                upstream_acks.append(marker)
            return written

        monkeypatch.setattr(server, "send_message", observe_upstream_ack)
        sender = threading.Thread(
            target=source._forward_one,
            args=(kind, rel_dir, src.name, str(src), target_addrs), daemon=True,
        )
        sender.start()
        sender.join(timeout=20)
        assert not sender.is_alive(), "the source attempt did not finish"
        assert outcomes == [False]
        assert wait_until(lambda: not server._forward_relays, timeout=10)
        assert len(server._fid_routes) == 1
        fid, route = next(iter(server._fid_routes.items()))
        original_targets = dict(route["targets"])
        assert set(original_targets) == set(target_addrs)
        assert route["originator"] == source_addr
        assert route["completed"] == set(target_addrs[:successful_targets])
        assert server._fid_route_index == {
            (addr, original_targets[addr]): fid for addr in failed
        }
        assert upstream_acks == []
        upload_key = ((source.host, source.port), fid)
        assert set(source.file_upload_progress) == {upload_key}
        upload = source.file_upload_progress[upload_key]
        assert upload[0:2] == [len(payload), len(payload)]
        assert upload[3:] == [str(src), target_addrs[0]]
        assert set(source._forward_resume_records) == {fid}
        record = source._forward_resume_records[fid]
        assert record["fid"] == fid
        assert record["targets"] == [list(addr) for addr in target_addrs]
        assert record["kind"] == kind
        assert record["rel_dir"] == rel_dir
        for target, addr, output in zip(receivers, target_addrs, outputs):
            assert output.read_bytes() == payload
            child_fid = original_targets[addr]
            assert wait_until(lambda: child_fid not in target._active_transfer_fids)
            if addr in failed:
                assert set(target.file_recv_progress) == {child_fid}
                entry = target.file_recv_progress[child_fid]
                assert entry == [len(payload), len(payload), None, str(output), source_addr]
            else:
                assert target.file_recv_progress == {}
        assert dispatches == [(addr, original_targets[addr]) for addr in target_addrs]

        for index, addr in enumerate(failed):
            target = receivers[target_addrs.index(addr)]
            child_fid = original_targets[addr]
            losing_ack[addr] = False
            # The receiver discovers the disk offset using its normal reconnect
            # path. A second request while its ACK is held must be coalesced.
            assert target._request_resumes() == 1
            if index == 0:
                assert resume_ack.wait(timeout=10)
                assert len(calls) == 2
                assert source._forward_resume_records[fid] is record
                assert target._request_forward_resume(
                    child_fid, target.file_recv_progress[child_fid], source_addr
                )
                assert wait_until(lambda: len(resume_requests) == 2, timeout=10)
                assert resume_requests == [(addr, fid, len(payload))] * 2
                assert len(calls) == 2, "a duplicate resume started another upload"
                release_ack.set()
            assert wait_until(lambda: len(outcomes) == index + 2, timeout=10)
            assert wait_until(lambda: not server._forward_relays, timeout=10)
            assert outcomes[-1] is (index == len(failed) - 1)
            assert len(upstream_acks) == (1 if index == len(failed) - 1 else 0)
            assert calls[-1]["relay_fid"] == fid
            assert calls[-1]["targets"] == [addr]
            assert calls[-1]["offset"] == len(payload)
            assert calls[-1]["record"] is record
            assert calls[-1]["upload"] is not None
            assert record["targets"] == [list(peer) for peer in target_addrs]
            assert route["targets"] == original_targets
            assert child_fid not in target.file_recv_progress
            if index != len(failed) - 1:
                assert server._fid_routes[fid] is route
                assert source._forward_resume_records[fid] is record
                assert upload_key in source.file_upload_progress
                assert route["completed"] == set(
                    target_addrs[:successful_targets] + failed[:index + 1]
                )
                assert server._fid_route_index == {
                    (peer, original_targets[peer]): fid for peer in failed[index + 1:]
                }

        assert dispatches == [
            (addr, original_targets[addr]) for addr in [*target_addrs, *failed]
        ], "an acknowledged target was sent again or a child id changed"
        assert ack_attempts == {
            addr: 2 if addr in failed else 1 for addr in target_addrs
        }
        assert upstream_acks == [server.server_received_file_data_sign.encode("utf-8")]
        assert server._fid_routes == {}
        assert server._fid_route_index == {}
        assert source._forward_resume_records == {}
        assert source.file_upload_progress == {}
        for target, output in zip(receivers, outputs):
            assert target.file_recv_progress == {}
            assert output.read_bytes() == payload
        with open(source.file_trans_progress_file, encoding="utf-8") as stream:
            source_snapshot = json.load(stream)
        assert source_snapshot["upload"] == {}
        assert source_snapshot["relays"] == {}
        with open(server.file_trans_progress_file, encoding="utf-8") as stream:
            assert json.load(stream)["relays"] == {}
    finally:
        release_ack.set()
        _close_socket_workers(server, clients, workers)


@pytest.mark.parametrize("ack_failure", ["false", "exception"])
def test_forward_ack_write_failure_retains_source_and_completed_route(
    tmp_path, monkeypatch, ack_failure
):
    """A failed original ACK write keeps source metadata and the Server route."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    try:
        source = _start_client(tmp_path, server, name="source")
        clients.append(source)
        recv_dir = tmp_path / "ack_write_failure_recv"
        recv_dir.mkdir()
        target = _start_client(tmp_path, server, name="target", recv_dir=recv_dir)
        clients.append(target)
        target_addr = target.client_socket.getsockname()
        assert wait_until(lambda: target_addr in server.clients)
        src = tmp_path / "ack_write_failure.bin"
        payload = os.urandom(64 * 1024 + 37)
        src.write_bytes(payload)
        failed_writes = []
        real_send = server.send_message

        def fail_upstream_ack(sock, message, is_send_raw=False):
            marker = message.encode("utf-8") if isinstance(message, str) else message
            if marker == server.server_received_file_data_sign.encode("utf-8"):
                failed_writes.append(sock)
                if ack_failure == "exception":
                    raise ConnectionError("injected upstream final ACK write failure")
                return False
            return real_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(server, "send_message", fail_upstream_ack)
        assert source._forward_one("file", "", src.name, str(src), [target_addr]) is False
        assert len(failed_writes) == 1
        assert wait_until(lambda: not server._forward_relays, timeout=10)
        assert (recv_dir / src.name).read_bytes() == payload
        assert wait_until(lambda: not target.file_recv_progress, timeout=10)
        assert len(source._forward_resume_records) == 1
        fid, record = next(iter(source._forward_resume_records.items()))
        assert record["targets"] == [list(target_addr)]
        upload_key = ((source.host, source.port), fid)
        assert set(source.file_upload_progress) == {upload_key}
        assert source.file_upload_progress[upload_key][0:2] == [len(payload), len(payload)]
        route = server._fid_routes[fid]
        assert route["completed"] == {target_addr}
        assert set(route["targets"]) == {target_addr}
        assert server._fid_route_index == {}
        with open(source.file_trans_progress_file, encoding="utf-8") as stream:
            snapshot = json.load(stream)
        assert len(snapshot["upload"]) == 1
        assert set(snapshot["relays"]) == {str(fid)}

        reloaded = TCP_Client_Base(
            host=source.host, port=source.port, is_extend_command=True,
            is_input_command_in_console=False, is_enable_encrypto=False,
        )
        _isolate(reloaded, tmp_path, "reloaded_source")
        reloaded.file_trans_progress_file = source.file_trans_progress_file
        try:
            assert reloaded._load_transfer_progress() == 2
            assert (
                reloaded.file_upload_progress[upload_key]
                == source.file_upload_progress[upload_key]
            )
            loaded_record = reloaded._forward_resume_record(fid)
            assert loaded_record["targets"] == [target_addr]
        finally:
            reloaded.file_trans_progress_file = str(tmp_path / "inspection_progress.json")
            reloaded.close()
    finally:
        _close_socket_workers(server, clients, workers)


def test_forward_waits_for_last_target_ack_before_upstream_ack(tmp_path, monkeypatch):
    """The source remains pending until the last real target ACK is released."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    release_ack = threading.Event()
    target_bytes_read = threading.Event()
    upstream_acks = []
    try:
        source = _start_client(tmp_path, server, name="source")
        clients.append(source)
        recv_dir = tmp_path / "held_final_ack_recv"
        recv_dir.mkdir()
        target = _start_client(tmp_path, server, name="target", recv_dir=recv_dir)
        clients.append(target)
        target_addr = target.client_socket.getsockname()
        assert wait_until(lambda: target_addr in server.clients)
        real_target_send = target.send_message
        real_server_send = server.send_message

        def hold_target_ack(sock, message, is_send_raw=False):
            if message == target.server_received_file_data_sign:
                target_bytes_read.set()
                if not release_ack.wait(timeout=10):
                    raise TimeoutError("the final target ACK was not released")
            return real_target_send(sock, message, is_send_raw=is_send_raw)

        def observe_upstream_ack(sock, message, is_send_raw=False):
            marker = message.encode("utf-8") if isinstance(message, str) else message
            written = real_server_send(sock, message, is_send_raw=is_send_raw)
            if marker == server.server_received_file_data_sign.encode("utf-8") and written:
                upstream_acks.append(marker)
            return written

        monkeypatch.setattr(target, "send_message", hold_target_ack)
        monkeypatch.setattr(server, "send_message", observe_upstream_ack)
        src = tmp_path / "held_final_ack.bin"
        payload = os.urandom(64 * 1024 + 37)
        src.write_bytes(payload)
        result = []
        sender = threading.Thread(
            target=lambda: result.append(
                source._forward_one("file", "", src.name, str(src), [target_addr])
            ), daemon=True,
        )
        sender.start()
        assert target_bytes_read.wait(timeout=10), "target did not read all forwarded bytes"
        assert wait_until(lambda: (recv_dir / src.name).exists())
        assert (recv_dir / src.name).stat().st_size == len(payload)
        assert sender.is_alive(), "source returned before the final target ACK"
        assert source.client_socket.fileno() >= 0
        assert upstream_acks == []
        assert len(source._forward_resume_records) == 1
        fid = next(iter(source._forward_resume_records))
        upload_key = ((source.host, source.port), fid)
        assert upload_key in source.file_upload_progress
        assert source.file_upload_progress[upload_key][0:2] == [len(payload), len(payload)]
        route = server._fid_routes[fid]
        assert route["completed"] == set()

        release_ack.set()
        sender.join(timeout=15)
        assert not sender.is_alive()
        assert result == [True]
        assert upstream_acks == [target.server_received_file_data_sign.encode("utf-8")]
        assert server._fid_routes == {}
        assert server._fid_route_index == {}
        assert source._forward_resume_records == {}
        assert source.file_upload_progress == {}
    finally:
        release_ack.set()
        _close_socket_workers(server, clients, workers)


def test_forward_route_survives_a_refused_target_listener_and_reload(tmp_path, monkeypatch):
    """One real completed target cannot erase another accepted target's route."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    restored = None
    # Bound but deliberately not listening: this port cannot be stolen, and
    # the relay's real connect() receives a deterministic connection refusal.
    refused_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    refused_listener.bind(("127.0.0.1", 0))
    advertised = threading.Event()
    try:
        good_dir = tmp_path / "good_recv"
        bad_dir = tmp_path / "refused_recv"
        good_dir.mkdir()
        bad_dir.mkdir()
        source = _start_client(tmp_path, server, name="source")
        clients.append(source)
        good = _start_client(tmp_path, server, name="good", recv_dir=good_dir)
        clients.append(good)
        bad = _start_client(tmp_path, server, name="refused", recv_dir=bad_dir)
        clients.append(bad)
        source_addr = source.client_socket.getsockname()
        good_addr = good.client_socket.getsockname()
        bad_addr = bad.client_socket.getsockname()
        assert wait_until(lambda: all(
            peer in server.clients for peer in (source_addr, good_addr, bad_addr)
        ))

        def advertise_refused_listener(client_id, client_socket, command,
                                       new_save_path=None, file_name=None):
            tfid = int(shlex.split(command)[-1])
            stamp = bad._crypto_transfer_stamp(client_socket)
            bad.send_message(
                client_socket,
                f"/server_file_transfer_port {refused_listener.getsockname()[1]} {tfid} {stamp}\n",
                is_send_raw=True,
            )
            advertised.set()

        monkeypatch.setattr(
            bad, "file_transfer_client_recv_server_start", advertise_refused_listener
        )
        src = tmp_path / "partially_forwarded.bin"
        payload = os.urandom(96 * 1024)
        src.write_bytes(payload)
        sender = threading.Thread(
            target=source._forward_one,
            args=("file", "", src.name, str(src), [good_addr, bad_addr]),
            daemon=True,
        )
        sender.start()
        assert advertised.wait(timeout=10), "the faulty target did not advertise its port"
        output = good_dir / src.name
        assert wait_until(
            lambda: output.exists() and output.stat().st_size == len(payload), timeout=20
        ), "the healthy target did not receive the full forwarded file"
        assert output.read_bytes() == payload
        sender.join(timeout=20)
        assert not sender.is_alive(), "the real source transfer did not finish"
        assert wait_until(lambda: not server._forward_relays, timeout=10)
        assert wait_until(lambda: not good.file_recv_progress, timeout=10)
        assert not (bad_dir / src.name).exists()

        assert len(server._fid_routes) == 1
        fid, route = next(iter(server._fid_routes.items()))
        assert route["originator"] == source_addr
        assert set(route["targets"]) == {good_addr, bad_addr}
        assert server._lookup_fid_route(None, fid) == (fid, route)
        assert route["completed"] == {good_addr}
        assert server._lookup_fid_route(good_addr, route["targets"][good_addr]) is None
        found = server._lookup_fid_route(bad_addr, route["targets"][bad_addr])
        assert found[0] == fid
        assert found[1] is route
        expected_index = {
            (bad_addr, route["targets"][bad_addr]): fid
        }
        assert server._fid_route_index == expected_index

        server._persist_transfer_progress()
        with open(server.file_trans_progress_file, "r", encoding="utf-8") as stream:
            snapshot = json.load(stream)
        assert snapshot["relays"] == {
            str(fid): {
                "originator": list(source_addr),
                "targets": [
                    [list(target), tfid] for target, tfid in route["targets"].items()
                ],
                "completed": [list(good_addr)],
            }
        }
        restored = TCP_Server_Base(
            host="127.0.0.1", port=free_port(), is_extend_command=True,
            is_input_command_in_console=False, is_enable_encrypto=False,
        )
        _isolate(restored, tmp_path, "restored")
        restored.file_trans_progress_file = server.file_trans_progress_file
        assert restored._load_transfer_progress() == 1
        restored_route = restored._fid_routes[fid]
        assert restored_route == route
        assert restored._fid_route_index == expected_index
        assert restored._lookup_fid_route(None, fid)[1] is restored_route
        assert restored._lookup_fid_route(good_addr, route["targets"][good_addr]) is None
        found = restored._lookup_fid_route(bad_addr, route["targets"][bad_addr])
        assert found[0] == fid
        assert found[1] is restored_route

        # Drive both rejection and offline routing through real control sockets.
        # Waiting for the real handler to return avoids timing-based negatives.
        source.close()
        assert wait_until(lambda: source_addr not in server.clients, timeout=10)
        handled = []
        real_resume_request = server._resume_request

        def observe_resume(peer, requested_fid, offset):
            try:
                return real_resume_request(peer, requested_fid, offset)
            finally:
                handled.append((peer, requested_fid, offset))

        monkeypatch.setattr(server, "_resume_request", observe_resume)
        bad_fid = route["targets"][bad_addr]
        unknown_fid = max([fid, *route["targets"].values()]) + 2
        assert good.send_message(good.client_socket, f"/resume_trans {bad_fid} 0")
        assert bad.send_message(bad.client_socket, f"/resume_trans {unknown_fid} 0")
        assert wait_until(lambda: len(handled) == 2, timeout=10)
        assert set(handled) == {(good_addr, bad_fid, 0), (bad_addr, unknown_fid, 0)}
        assert server._pending_relay_resumes == {}
        assert bad.send_message(bad.client_socket, f"/resume_trans {bad_fid} 0")
        assert wait_until(lambda: len(handled) == 3, timeout=10)
        assert server._pending_relay_resumes == {source_addr: [(fid, bad_addr, 0)]}
        assert server._fid_routes[fid] is route
    finally:
        refused_listener.close()
        if restored is not None:
            restored.stop()
        _close_socket_workers(server, clients, workers)


def test_forward_route_is_removed_only_after_all_real_targets_complete(tmp_path, monkeypatch):
    """A completed sibling leaves the route live until the held writer finishes."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    held = {}
    try:
        source = _start_client(tmp_path, server, name="source")
        clients.append(source)
        targets = []
        outputs = []
        for index in range(2):
            recv_dir = tmp_path / f"forward_target_{index}"
            recv_dir.mkdir()
            target = _start_client(
                tmp_path, server, name=f"target_{index}", recv_dir=recv_dir
            )
            clients.append(target)
            targets.append(target.client_socket.getsockname())
            outputs.append(recv_dir / "complete_forward.bin")
        assert wait_until(lambda: all(target in server.clients for target in targets))
        real_forward_item_to = server.forward_item_to

        def hold_second_writer(target, kind, rel_dir, fname, originator_addr, tfid,
                               destination_path=None):
            if target == targets[1]:
                held["fid"] = tfid
                held["paused"] = server.pause_trans(tfid, target)
            return real_forward_item_to(
                target, kind, rel_dir, fname, originator_addr, tfid, destination_path
            )

        monkeypatch.setattr(server, "forward_item_to", hold_second_writer)
        src = tmp_path / "complete_forward.bin"
        payload = os.urandom(96 * 1024)
        src.write_bytes(payload)
        sender = threading.Thread(
            target=source._forward_one,
            args=("file", "", src.name, str(src), targets), daemon=True,
        )
        sender.start()
        assert wait_until(
            lambda: outputs[0].exists() and outputs[0].stat().st_size == len(payload),
            timeout=20,
        ), "the unpaused target did not finish"
        assert outputs[0].read_bytes() == payload
        assert wait_until(lambda: not clients[1].file_recv_progress, timeout=10)
        assert held["paused"] is True
        assert not outputs[1].exists() or outputs[1].stat().st_size == 0
        assert len(server._fid_routes) == 1
        fid, route = next(iter(server._fid_routes.items()))
        assert set(route["targets"]) == set(targets)
        assert wait_until(lambda: route["completed"] == {targets[0]}, timeout=10)
        expected_index = {(targets[1], route["targets"][targets[1]]): fid}
        assert server._fid_route_index == expected_index
        assert server._lookup_fid_route(targets[0], route["targets"][targets[0]]) is None
        assert server._lookup_fid_route(targets[1], route["targets"][targets[1]])[1] is route

        assert server.start_trans(held["fid"], targets[1]) is True
        assert wait_until(
            lambda: outputs[1].exists() and outputs[1].stat().st_size == len(payload),
            timeout=20,
        ), "the released target did not finish"
        assert outputs[1].read_bytes() == payload
        sender.join(timeout=20)
        assert not sender.is_alive(), "the real source transfer did not finish"
        assert wait_until(lambda: not server._forward_relays, timeout=10)
        assert wait_until(lambda: not clients[2].file_recv_progress, timeout=10)
        assert server._fid_routes == {}
        assert server._fid_route_index == {}
        assert server._lookup_fid_route(None, fid) is None
        for target, tfid in route["targets"].items():
            assert server._lookup_fid_route(target, tfid) is None
    finally:
        if held:
            server.start_trans(held["fid"], targets[1])
        _close_socket_workers(server, clients, workers)


def test_pause_and_start_trans_control_a_plain_transfer(tmp_path):
    """``pause_trans``/``start_trans`` hold and release any outbound leg.

    The receiver stops reading until the sender is released, so the pause
    works on a plain (non-forwarded) transfer: nothing arrives while the leg
    is held, and the file completes after ``start_trans``.
    """
    server = _start_server(tmp_path)
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    client = None
    try:
        client = _start_client(tmp_path, server)
        src = tmp_path / "payload.bin"
        payload = os.urandom(256 * 1024)
        src.write_bytes(payload)
        fid = 404  # even low bit: a plain leg's id

        server_sock = server.clients[client.client_socket.getsockname()]["socket"]
        server.file_transfer_server_recv_server_start_thread(
            "cid", server_sock, f"/file {src} {fid}"
        )
        assert _wait_port(client) is not None

        assert client.pause_trans(fid) is False  # armed before the leg starts
        sender = threading.Thread(
            target=client.file_transfer_mode,
            args=(str(src), "127.0.0.1", _wait_port(client) or 0, 0),
            kwargs={"fid": fid, "peer": (server.host, server.port)},
            daemon=True,
        )
        sender.start()
        upload_key = ((client.host, client.port), fid)
        assert wait_until(lambda: client.file_upload_progress.get(upload_key)), (
            "the uploader never registered the transfer"
        )
        time.sleep(0.5)
        output = server_recv / "payload.bin"
        assert not output.exists() or output.stat().st_size == 0, "the paused leg sent data"

        assert client.start_trans(fid) is True  # a live leg is released
        assert wait_until(
            lambda: output.exists() and output.stat().st_size == len(payload), timeout=20
        ), "the released transfer did not finish"
        assert output.read_bytes() == payload
        sender.join(timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_download_resumes_over_the_encrypted_channel(tmp_path):
    """The resume exchange travels on the framed channel like any transfer line.

    With encryption enabled the resume drive waits for the handshake to finish
    before it writes ``/resume_trans``, so the request and the resumed leg
    both speak frames.
    """
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _isolate(server, tmp_path, "server")
    _redirect_crypto(server.crypto, tmp_path, ssh_dir, "pub_key")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    client = None
    try:
        recv_dir = tmp_path / "client_recv"
        recv_dir.mkdir()
        client = TCP_Client_Base(
            host="127.0.0.1",
            port=port,
            client_host="127.0.0.1",
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=True,
        )
        _isolate(client, tmp_path, "client")
        _redirect_crypto(client.crypto, tmp_path, ssh_dir, "pub_key_client")
        client.file_transfer_dir = str(recv_dir)
        assert client.connect()
        assert wait_until(lambda: client.client_socket in client._encrypted_sockets, timeout=15), (
            "the channel never flipped to frames"
        )

        src = tmp_path / "download.bin"
        payload = os.urandom(64 * 1024)
        src.write_bytes(payload)
        fid = 908  # even low bit: a plain leg's id
        partial = recv_dir / "download.bin"
        with open(partial, "wb") as f:
            f.write(payload[:1024])
        client.file_recv_progress[fid] = [
            1024,
            len(payload),
            None,
            str(partial),
            (server.host, server.port),
        ]
        upload_key = (client.client_socket.getsockname(), fid)
        server.file_upload_progress[upload_key] = [
            0,
            len(payload),
            None,
            str(src),
            client.client_socket.getsockname(),
        ]

        client._request_resumes()  # the same call connect() dispatches on reconnect
        assert wait_until(lambda: partial.stat().st_size == len(payload), timeout=20), (
            "the encrypted resume did not finish"
        )
        assert partial.read_bytes() == payload
        assert wait_until(lambda: fid not in client.file_recv_progress, timeout=10)
        assert wait_until(lambda: upload_key not in server.file_upload_progress, timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_client_connects_when_its_configured_port_is_taken(tmp_path):
    """A configured client port that is held elsewhere does not stop the client.

    The port can be stuck after a session ends (TIME_WAIT on Windows), so the
    client must still connect — from an OS-assigned port — instead of never
    coming back.
    """
    server = _start_server(tmp_path)
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    taken = free_port()
    holder.bind(("127.0.0.1", taken))  # no SO_REUSEADDR: the bind must fail
    client = None
    try:
        client = TCP_Client_Base(
            host="127.0.0.1",
            port=server.port,
            client_host="127.0.0.1",
            client_port=taken,
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=False,
        )
        _isolate(client, tmp_path, "client")
        assert client.connect(), "the client did not connect around the taken port"
        assert client.client_socket.getsockname()[1] != taken, (
            "the client reported the port it could not bind"
        )
        assert wait_until(
            lambda: server.clients.get(client.client_socket.getsockname()) is not None
        ), "the server never saw the client"
    finally:
        holder.close()
        if client is not None:
            client.close()
        server.stop()


def test_resume_with_a_different_total_is_refused_and_keeps_the_partial_file(tmp_path):
    """A continued leg whose size disagrees with the progress entry is refused.

    The sender answers ``/resume_trans`` with a file whose length differs from
    the one the interrupted entry recorded. The receiver must refuse the leg and
    leave the partial bytes and the progress entry untouched, so a later
    reconnect still has what it needs to decide.
    """
    server = _start_server(tmp_path)
    client = None
    try:
        recv_dir = tmp_path / "client_recv"
        recv_dir.mkdir()
        client = _start_client(tmp_path, server, recv_dir=recv_dir)
        src = tmp_path / "download.bin"
        payload = os.urandom(128 * 1024)
        src.write_bytes(payload)
        fid = 302  # even low bit: a plain leg's id
        kept = payload[:4096]
        partial = recv_dir / "download.bin"
        with open(partial, "wb") as f:
            f.write(kept)
        stale_total = len(payload) + 1  # the entry recorded a different file of this name
        client.file_recv_progress[fid] = [
            len(kept),
            stale_total,
            None,
            str(partial),
            (server.host, server.port),
        ]
        upload_key = (client.client_socket.getsockname(), fid)
        server.file_upload_progress[upload_key] = [
            0,
            len(payload),
            None,
            str(src),
            client.client_socket.getsockname(),
        ]
        messages = []
        log = client._log

        def record(message):
            messages.append(message)
            log(message)

        client._log = record
        client._request_resumes()
        assert wait_until(lambda: any("resume rejected" in m for m in messages), timeout=20), (
            "the mismatched resume was not refused"
        )
        assert partial.read_bytes() == kept
        assert client.file_recv_progress[fid][1] == stale_total
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_download_resumes_after_connect_without_a_manual_call(tmp_path):
    """A download interrupted by a disconnect resumes via the client's own reconnect.

    Nothing calls the resume driver by hand: the client reconnects
    (``is_auto_reconnect``) and its connect path asks the server to continue the
    file from the bytes already on disk. This covers the trigger itself, which
    the partial-file tests inject directly.
    """
    server = _start_server(tmp_path)
    recv_dir = tmp_path / "client_recv"
    recv_dir.mkdir()
    client = None
    try:
        src = tmp_path / "payload.bin"
        payload = os.urandom(32 * 1024 * 1024)  # still in flight when the fault hits
        src.write_bytes(payload)

        # a fixed client port keeps its address stable, so the server can match
        # the interrupted download to the reconnecting peer
        client = TCP_Client_Base(
            host="127.0.0.1",
            port=server.port,
            client_host="127.0.0.1",
            client_port=free_port(),
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=False,
            is_auto_reconnect=True,
        )
        _isolate(client, tmp_path, "client")
        client.file_transfer_dir = str(recv_dir)
        threading.Thread(target=client.start_TCP_client, daemon=True).start()
        assert wait_until(lambda: client.running and client.client_socket is not None)
        client_addr = client.client_socket.getsockname()
        assert wait_until(lambda: client_addr in server.clients, timeout=10), (
            "server never registered the client"
        )

        partial = recv_dir / src.name
        # Predict the next server-allocated id and arm this receiver's pause
        # before the leg starts, so the fault lands while it is incomplete.
        alloc = server._fid_alloc
        with alloc._lock:
            fid = ((alloc._counter + 1) << 32) | (alloc._epoch << 1)
        upload_key = (client_addr, fid)
        assert server.pause_trans(fid, client_addr) is False, "the leg was already running"

        # the server pushes the file: the client opens its listener and the
        # server connects from a send leg
        server.file_transfer_server_recv_client_start_thread(
            "/file {} {}".format(shlex.quote(str(src)), shlex.quote(repr(client_addr)))
        )
        assert wait_until(lambda: server.file_upload_progress.get(upload_key), timeout=30), (
            "the server never registered the outgoing transfer"
        )
        # the leg is parked before its first chunk, so the receiver holds an
        # empty target; give it the bytes an interrupted transfer would have
        # left behind (the leg's own writes would only be the same file)
        assert wait_until(lambda: partial.exists(), timeout=20), (
            "the receiver never created the target file"
        )
        half = len(payload) // 2
        partial.write_bytes(payload[:half])

        # a fault drops the control connection from the server side; the parked
        # leg cannot finish the file, so the partial stays on disk
        dead_leg = server._active_transfer_sockets.get(upload_key)
        assert dead_leg is not None, "the send leg was not registered"
        dead_socket = client.client_socket
        server_sock = server.clients[client_addr]["socket"]
        server_sock.shutdown(2)
        server_sock.close()
        assert wait_until(lambda: dead_socket.fileno() == -1, timeout=20), (
            "the reconnect did not release the dead session's socket"
        )
        # the reconnecting client sweeps the dead session's legs and then asks
        # for the remainder; only the swept leg may be released, or it would
        # write from its own offset into the file the resumed leg continues
        assert wait_until(
            lambda: server._active_transfer_sockets.get(upload_key) is not dead_leg, timeout=20
        ), "the dead session's leg was never cleared"
        server.start_trans(fid, client_addr)  # the test, not the protocol, parked this leg
        assert wait_until(lambda: server.clients.get(client_addr) is not None, timeout=20), (
            "the client did not come back"
        )
        assert client.client_socket.getsockname() == client_addr, (
            "the client came back from a different port"
        )

        assert wait_until(lambda: partial.stat().st_size == len(payload), timeout=30), (
            "the resumed download did not finish"
        )
        assert partial.read_bytes() == payload, "the resumed download did not continue the partial"
    finally:
        if client is not None:
            client.close()
        server.stop()