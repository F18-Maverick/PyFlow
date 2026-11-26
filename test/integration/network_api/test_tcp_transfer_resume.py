"""Integration tests for the transfer reconnect (resume) mechanism.

A transfer interrupted by a disconnect keeps its progress entry and its
partial file; both sides write the entries to ``.Flow`` when the connection
drops, and after a reconnect the receiver asks the sender to continue the file
from the bytes it already holds. The tests drive that exchange over real
sockets, with the interrupted state either produced by a real disconnected
session or loaded from the persisted file exactly as a disconnect leaves it.
"""

import builtins
import hashlib
import json
import os
import shlex
import socket
import threading
import time

import pytest

from helpers import (
    free_port,
    inject_wsaeacces_once,
    isolate_instance_files,
    request_transfer_listener,
    server_ready,
    wait_until,
)

from PyFlow.network_api import connect_tcp
from PyFlow.network_api.connect_tcp import (
    TRANSFER_SIGN_BUFFER_LIMIT,
    TCP_Client_Base,
    TCP_Server_Base,
    _TransferSignBuffer,
    _sending_transfer,
    _socket_local_endpoint,
    _socket_peer_endpoint,
    _start_resume_send,
    _retire_transfer_session,
    _transfer_handshake,
    format_resume_line,
)


def _isolate(instance, tmp_path, name):
    """Keep an instance's logs and its transfer progress file out of the repo."""
    isolate_instance_files(instance, tmp_path, name)



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


@pytest.mark.parametrize("kind", ["file", "folder"])
@pytest.mark.parametrize("bind_host", ["127.0.0.1", "localhost", "0.0.0.0"])
def test_forward_self_target_is_rejected_but_other_ports_still_receive(
    tmp_path, monkeypatch, kind, bind_host
):
    """Console forwarding rejects itself before allocation and keeps legal peers."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    try:
        source = TCP_Client_Base(
            host="127.0.0.1", port=server.port, client_host=bind_host, client_port=0,
            is_extend_command=True, is_input_command_in_console=False,
            is_enable_encrypto=False,
        )
        _isolate(source, tmp_path, "self_source")
        source_dir = tmp_path / "self_receive"
        source_dir.mkdir()
        source.file_transfer_dir = str(source_dir)
        clients.append(source)
        assert source.connect()
        peer = source.client_socket.getsockname()
        assert wait_until(lambda: peer in server.clients)
        source._resume_thread.join(timeout=10)
        directory = tmp_path / "bundle" / "nested"
        directory.mkdir(parents=True)
        path = directory / "payload.bin"
        payload = os.urandom(8192 + 17)
        path.write_bytes(payload)
        item = directory.parent if kind == "folder" else path
        command = source.forward_folder_console if kind == "folder" else source.forward_file_console
        outcomes, sent, rows = [], [], []
        real_forward, real_send = source._forward_one, source.send_message
        source_counter = source._fid_alloc._counter
        server_counter = server._fid_alloc._counter

        def forward(*args, **kwargs):
            result = real_forward(*args, **kwargs)
            outcomes.append(result)
            return result

        def send(sock, message, is_send_raw=False):
            if isinstance(message, str) and message.startswith("/forward_item "):
                sent.append(message)
                rows.append([list(row) for row in source.file_upload_progress.values()])
            return real_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(source, "_forward_one", forward)
        monkeypatch.setattr(source, "send_message", send)
        own_alias = (bind_host, peer[1])
        command(f"/forward_{kind} {shlex.quote(str(item))} {shlex.quote(str(own_alias))}")
        assert wait_until(lambda: outcomes, timeout=15)
        assert outcomes == [False]
        assert sent == []
        assert source._fid_alloc._counter == source_counter
        assert server._fid_alloc._counter == server_counter
        assert not source.file_upload_progress and not source.file_recv_progress
        assert not server._fid_routes and not server._fid_route_index
        assert not server._forward_relays and not server._receive_transfers
        assert list(source_dir.rglob("*")) == []

        target_dir = tmp_path / "other_receive"
        target_dir.mkdir()
        target = _start_client(tmp_path, server, name="other_target", recv_dir=target_dir)
        clients.append(target)
        target_peer = target.client_socket.getsockname()
        assert target_peer[0] == peer[0] and target_peer[1] != peer[1]
        assert wait_until(lambda: target_peer in server.clients)
        command(f"/forward_{kind} {shlex.quote(str(item))} "
                f"{shlex.quote(str(peer))} {shlex.quote(str(target_peer))}")
        assert wait_until(lambda: len(outcomes) == 2, timeout=15)
        assert outcomes == [False, True]
        assert len(sent) == 1
        assert rows[0][0][4] == target_peer
        tokens = shlex.split(sent[0])
        assert str(target_peer) in tokens and str(peer) not in tokens
        output = target_dir / ("bundle/nested/payload.bin" if kind == "folder" else path.name)
        assert output.read_bytes() == payload
        assert list(source_dir.rglob("*")) == []
        assert path.read_bytes() == payload
        assert wait_until(lambda: not server._forward_relays and not server._receive_transfers)
        assert not source.file_upload_progress and not target.file_recv_progress
        assert not server._fid_routes and not server._fid_route_index

        # A retained row from an older self-forward remains available; rejecting
        # its replay must neither contact the Server nor mark it complete.
        fid = 531
        key = ((source.host, source.port), fid)
        row = source.file_upload_progress[key] = [len(payload), len(payload), None, str(path), peer]
        source._persist_transfer_progress()
        assert source.resume_trans(peer, fid, len(payload))
        assert wait_until(lambda: len(outcomes) == 3 and not source._resume_sends)
        assert outcomes[-1] is False and len(sent) == 1
        assert source.file_upload_progress[key] is row
        assert source._fid_alloc._counter == source_counter + 1
        assert server._fid_alloc._counter == server_counter + 1
        assert not server._fid_routes and not server._receive_transfers
    finally:
        _close_socket_workers(server, clients, workers)


@pytest.mark.parametrize("occupied_port", [False, True], ids=["unbound", "bind-fallback"])
def test_forward_different_ip_same_port_survives_a_different_configured_host(
    tmp_path, monkeypatch, occupied_port
):
    """An unused configured IP is not an alias for the actual control socket."""
    workers = _track_socket_workers(monkeypatch)
    server = TCP_Server_Base(
        host="0.0.0.0", port=free_port(), is_extend_command=True,
        is_input_command_in_console=False, is_enable_encrypto=False,
    )
    _isolate(server, tmp_path, "wildcard_server")
    clients = []
    blocker = socket.socket()
    try:
        threading.Thread(target=server.start_TCP_Server, daemon=True).start()
        assert server_ready(server)
        blocker.bind(("127.0.0.2", 0))
        source = TCP_Client_Base(
            host="127.0.0.1", port=server.port, client_host="127.0.0.2",
            client_port=blocker.getsockname()[1] if occupied_port else None,
            is_extend_command=True, is_input_command_in_console=False,
            is_enable_encrypto=False,
        )
        _isolate(source, tmp_path, "unbound_source")
        clients.append(source)
        assert source.connect()
        actual = source.client_socket.getsockname()
        assert actual[0] == "127.0.0.1" and source.local_address is None
        blocker.close()
        directory = tmp_path / "other_ip"
        directory.mkdir()
        target = TCP_Client_Base(
            host="127.0.0.2", port=server.port, client_host="127.0.0.2",
            client_port=actual[1], is_extend_command=True,
            is_input_command_in_console=False, is_enable_encrypto=False,
        )
        _isolate(target, tmp_path, "other_ip_target")
        target.file_transfer_dir = str(directory)
        clients.append(target)
        assert target.connect()
        peer = target.client_socket.getsockname()
        assert peer == ("127.0.0.2", actual[1])
        assert wait_until(lambda: actual in server.clients and peer in server.clients)
        source._resume_thread.join(timeout=10)
        path = tmp_path / "same_port.bin"
        payload = b"different actual IP, same actual port"
        path.write_bytes(payload)
        assert source._forward_one("file", "", path.name, str(path), [peer]) is True
        assert (directory / path.name).read_bytes() == payload
        assert source._forward_one("file", "", path.name, str(path), [actual]) is False
        assert wait_until(lambda: not server._forward_relays and not server._receive_transfers)
        assert not source.file_upload_progress and not target.file_recv_progress
    finally:
        blocker.close()
        _close_socket_workers(server, clients, workers)


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
            assert server.pause_trans(fid, receiver, trans_type=0, direction=0) is False
            results[receiver] = request_transfer_listener(
                server, client, f"/file {shlex.quote(str(src))} {fid}"
            )

        # Both listeners share a numeric FID but have distinct request sockets.
        ports = {receiver: results.pop(receiver) for receiver in reversed(receivers)}
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
        assert server.pause_trans(fid, receivers[0], trans_type=0, direction=0) is False

        _retire_transfer_session(server, server.clients[receivers[0]]["socket"])
        assert sockets[0].fileno() == -1
        assert server._active_transfer_sockets[keys[1]] is sockets[1]
        assert keys[1] in server._active_transfer_fids

        # Release only the dead leg's waiter and let its real cleanup finish.
        # If start/pause or cleanup is fid-only, the other leg now sends or dies.
        server.start_trans(fid, receivers[0], trans_type=0, direction=0)
        senders[0].join(timeout=15)
        assert not senders[0].is_alive(), "the cleared upload did not stop"
        assert keys[0] not in server._active_transfer_sockets
        assert keys[0] not in server._active_transfer_fids
        assert results[receivers[0]] is False
        assert server.file_upload_progress[keys[1]] is rows[1]
        assert rows[1][0] == 0
        assert clients[1].file_recv_progress[fid][0] == 0
        assert server._transfer_pause[(*keys[1], 0, 0)] is True
        assert server._active_transfer_sockets[keys[1]] is sockets[1]
        assert senders[1].is_alive()

        assert server.start_trans(fid, receivers[1], trans_type=0, direction=0) is True
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
            server.start_trans(fid, receiver, trans_type=0, direction=0)
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
    """Actual old-session cleanup cannot close an equal-id new-session upload."""
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
            inbound_control, inbound_wire = socket.socketpair()
            sockets.extend((inbound_control, inbound_wire))
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
            control = (instance.clients[receiver]["socket"] if instance is server
                       else client.client_socket)
            control_key = (receiver, fid, 1, 0)
            assert instance._transfer_handshakes.track_socket(inbound_control, inbound)
            assert instance._transfer_handshakes.track_socket(control, outbound)
            state = instance._transfer_controls.register(control_key, control, outbound)
            assert state is not None
            assert instance.pause_trans(fid, receiver, trans_type=1, direction=0) is True
            assert len(recv_entry) == len(upload_entry) == 5
            assert upload_entry[4] == logical_peer

            _retire_transfer_session(instance, inbound_control)
            assert inbound_remote.recv(1) == b""
            assert instance._active_transfer_sockets[fid] is inbound
            assert fid in instance._active_transfer_fids
            instance._file_progress_finish(instance.file_recv_progress, fid, recv_entry)
            instance._release_transfer_socket(fid, inbound)
            instance._transfer_handshakes.release_socket(inbound_control, inbound)
            assert fid not in instance._active_transfer_sockets
            assert fid not in instance._active_transfer_fids
            assert fid not in instance.file_recv_progress
            assert instance.file_upload_progress[upload_key] is upload_entry
            assert upload_key in instance._active_transfer_fids
            assert instance._active_transfer_sockets[upload_key] is outbound
            assert instance._transfer_pause[control_key] is True
            assert instance._transfer_controls._legs[control_key] is state
            outbound.sendall(b"x")
            assert outbound_remote.recv(1) == b"x"

            # Retire the actual control owner, not the logical target in row[4].
            _retire_transfer_session(instance, control)
            assert outbound_remote.recv(1) == b""
            assert instance._active_transfer_sockets[upload_key] is outbound
            assert upload_key in instance._active_transfer_fids
            instance._file_progress_finish(
                instance.file_upload_progress, upload_key, upload_entry
            )
            instance._release_transfer_socket(upload_key, outbound)
            instance._transfer_handshakes.release_socket(control, outbound)
            assert upload_key not in instance._active_transfer_sockets
            assert upload_key not in instance._active_transfer_fids
            assert upload_key not in instance.file_upload_progress
            instance.start_trans(fid, receiver, trans_type=1, direction=0)
            instance._transfer_controls.release(state)
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

        transfer_port, _stamp = request_transfer_listener(client, server, f"/file {src} {fid}")

        client.pause_trans(fid, trans_type=0, direction=0)  # hold the leg before its first chunk
        sender = threading.Thread(
            target=client.file_transfer_mode,
            args=(str(src), "127.0.0.1", transfer_port, 0),
            kwargs={"fid": fid, "peer": (server.host, server.port),
                    "control_socket": client.client_socket, "crypto": _stamp},
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

        client.start_trans(fid, trans_type=0, direction=0)  # release the paused leg so the test can end
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
        transfer_port, _stamp = request_transfer_listener(
            client, server, f"/file {shlex.quote(str(src))} {fid}"
        )

        client.pause_trans(fid, trans_type=0, direction=0)  # freeze the leg, then give it a partial file
        sender = threading.Thread(
            target=client.file_transfer_mode,
            args=(str(src), "127.0.0.1", transfer_port, 0),
            kwargs={"fid": fid, "peer": (server.host, server.port),
                    "control_socket": client.client_socket, "crypto": _stamp},
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
        client.start_trans(fid, trans_type=0, direction=0)
        sender.join(timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


@pytest.mark.parametrize("kind", ["file", "folder"])
@pytest.mark.parametrize("ack_failure", ["false", "exception"])
def test_server_ack_failure_keeps_received_file_until_eof_confirmation(
    tmp_path, monkeypatch, kind, ack_failure
):
    """Failed final ACKs preserve the original receive row, even on EOF retry."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    try:
        recv_dir = tmp_path / "server_recv"
        recv_dir.mkdir()
        server.file_transfer_dir = str(recv_dir)
        client = _start_client(tmp_path, server)
        clients.append(client)
        peer = client.client_socket.getsockname()
        assert wait_until(lambda: peer in server.clients)
        src = tmp_path / "ack_failure.bin"
        payload = os.urandom(64 * 1024 + 37)
        src.write_bytes(payload)
        fid = 606
        output = recv_dir / "bundle/nested" / src.name if kind == "folder" else recv_dir / src.name
        upload_key = ((server.host, server.port), fid)
        attempts = []
        outcomes = []
        real_send = server.send_message
        real_transfer = client.file_transfer_mode

        def fail_final_ack(sock, message, is_send_raw=False):
            if message == server.server_received_file_data_sign:
                attempts.append(sock)
                if len(attempts) <= 2:
                    if ack_failure == "exception":
                        raise ConnectionError("injected Server final ACK write failure")
                    return False
            return real_send(sock, message, is_send_raw=is_send_raw)

        def observe_transfer(*args, **kwargs):
            result = real_transfer(*args, **kwargs)
            outcomes.append((kwargs.get("fid"), kwargs.get("resume_offset", 0), result))
            return result

        monkeypatch.setattr(server, "send_message", fail_final_ack)
        monkeypatch.setattr(client, "file_transfer_mode", observe_transfer)
        command = (
            f"/file_folder bundle/nested {src.name} {fid}" if kind == "folder"
            else f"/file {shlex.quote(str(src))} {fid}"
        )
        port, stamp = request_transfer_listener(client, server, command)
        assert client.file_transfer_mode(
            str(src), server.host, port, 0, control_socket=client.client_socket,
            crypto=stamp, fid=fid, peer=(server.host, server.port),
        ) is False
        for attempt in range(2):
            assert wait_until(lambda: fid not in server._active_transfer_sockets)
            assert len(attempts) == attempt + 1
            assert output.read_bytes() == payload
            assert server.file_recv_progress[fid] == [
                len(payload), len(payload), None, str(output), peer
            ]
            assert client.file_upload_progress[upload_key][0:2] == [len(payload), len(payload)]
            with open(server.file_trans_progress_file, encoding="utf-8") as stream:
                snapshot = json.load(stream)
            assert snapshot["download"][str(fid)] == [
                len(payload), len(payload), None, str(output), list(peer)
            ]
            assert server._request_resumes(peer) == 1
            assert wait_until(lambda: len(outcomes) == attempt + 2, timeout=15)
        assert outcomes == [
            (fid, 0, False), (fid, len(payload), False), (fid, len(payload), True)
        ]
        assert len(attempts) == 3
        assert wait_until(lambda: fid not in server._active_transfer_sockets)
        assert fid not in server.file_recv_progress
        assert upload_key not in client.file_upload_progress
        with open(server.file_trans_progress_file, encoding="utf-8") as stream:
            assert json.load(stream)["download"] == {}
        assert output.read_bytes() == payload
        if kind == "folder":
            assert not (recv_dir / src.name).exists()
    finally:
        _close_socket_workers(server, clients, workers)


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


@pytest.mark.parametrize("kind", ["file", "folder"])
@pytest.mark.parametrize("offset", [0, 4096], ids=["initial", "resumed"])
def test_server_final_error_keeps_upload_until_successful_resume(
    tmp_path, monkeypatch, kind, offset
):
    """A real error sign must preserve a direct upload, including an EOF replay."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    try:
        recv_dir = tmp_path / "receiver"
        recv_dir.mkdir()
        client = _start_client(tmp_path, server, recv_dir=recv_dir)
        clients.append(client)
        receiver = client.client_socket.getsockname()
        assert wait_until(lambda: receiver in server.clients)
        src = tmp_path / "rejected.bin"
        payload = os.urandom(64 * 1024 + 37)
        src.write_bytes(payload)
        fid = 604
        key = (receiver, fid)
        output = recv_dir / "bundle/nested" / src.name if kind == "folder" else recv_dir / src.name
        if offset:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(payload[:offset])
            client.file_recv_progress[fid] = [
                offset, len(payload), None, str(output), (server.host, server.port)
            ]
            server.file_upload_progress[key] = [
                offset, len(payload), None, str(src), receiver
            ]
        header_seen = threading.Event()
        rejected = threading.Event()
        real_receive = server.receive_message
        real_send = client.send_message

        def observe_header(sock, *args, **kwargs):
            data = real_receive(sock, *args, **kwargs)
            if data.strip() == server.server_received_file_header_sign.encode("utf-8"):
                header_seen.set()
            return data

        def reject_final_ack(sock, message, is_send_raw=False):
            if message == client.server_received_file_data_sign and not rejected.is_set():
                # Separate the two signs even on an unframed TCP stream.
                assert header_seen.wait(timeout=10)
                assert real_send(sock, client.error_sign)
                rejected.set()
                raise ConnectionError("injected receiver rejection after the file body")
            return real_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(server, "receive_message", observe_header)
        monkeypatch.setattr(client, "send_message", reject_final_ack)
        command = (
            f"/file_folder bundle/nested {src.name} {fid}" if kind == "folder"
            else f"/file {shlex.quote(str(src))} {fid}"
        )
        port, stamp = request_transfer_listener(server, client, command)
        result = server.file_transfer_mode(
            str(src), receiver[0], port, 0,
            control_socket=server.clients[receiver]["socket"], crypto=stamp,
            fid=fid, peer=receiver, resume_offset=offset,
        )
        assert rejected.is_set()
        assert result is False
        assert output.read_bytes() == payload
        assert server.file_upload_progress[key] == [
            len(payload), len(payload), None, str(src), receiver
        ]
        with open(server.file_trans_progress_file, encoding="utf-8") as stream:
            snapshot = json.load(stream)
        json_key = json.dumps(key, separators=(",", ":"))
        assert snapshot["upload"][json_key] == [
            len(payload), len(payload), None, str(src), list(receiver)
        ]
        assert wait_until(lambda: fid not in client._active_transfer_fids)
        assert client._request_resumes() == 1
        assert wait_until(lambda: key not in server.file_upload_progress, timeout=15)
        assert wait_until(lambda: fid not in client.file_recv_progress)
        assert output.read_bytes() == payload
        if kind == "folder":
            assert not (recv_dir / src.name).exists()
    finally:
        _close_socket_workers(server, clients, workers)


def _coalesce_receiver_signs(monkeypatch, sender, control, ack):
    """Deliver the real start line, then combine the real header and ACK."""
    real_receive = sender.receive_message
    pending = b""
    start_delivered = False
    merged = []

    def receive(sock, length):
        nonlocal pending, start_delivered
        if sock is control or merged:
            return real_receive(sock, length)
        while True:
            if not start_delivered and b"\n" in pending:
                start, pending = pending.split(b"\n", 1)
                start_delivered = True
                return start + b"\n"
            if start_delivered and pending.endswith(ack):
                merged.append(pending)
                pending = b""
                return merged[-1]
            chunk = real_receive(sock, length)
            if not chunk:
                data, pending = pending, b""
                return data
            pending += chunk

    monkeypatch.setattr(sender, "receive_message", receive)
    return merged


@pytest.mark.parametrize("server_sends", [False, True], ids=["upload", "download"])
@pytest.mark.parametrize("eof_replay", [False, True], ids=["initial", "eof"])
def test_coalesced_receiver_signs_keep_the_final_ack(
    tmp_path, monkeypatch, server_sends, eof_replay
):
    """Both senders retain a final ACK sharing a read with the header sign."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    try:
        client = _start_client(tmp_path, server)
        clients.append(client)
        client_addr = client.client_socket.getsockname()
        assert wait_until(lambda: client_addr in server.clients)
        sender, receiver = (server, client) if server_sends else (client, server)
        peer = client_addr if server_sends else (server.host, server.port)
        origin = (server.host, server.port) if server_sends else client_addr
        control = server.clients[client_addr]["socket"] if server_sends else client.client_socket
        recv_dir = tmp_path / "coalesced_recv"
        recv_dir.mkdir()
        receiver.file_transfer_dir = str(recv_dir)
        src = tmp_path / "coalesced.bin"
        payload = os.urandom(48 * 1024 + 11)
        src.write_bytes(payload)
        output = recv_dir / src.name
        fid = 707
        offset = len(payload) if eof_replay else 0
        if eof_replay:
            output.write_bytes(payload)
            receiver.file_recv_progress[fid] = [
                offset, len(payload), None, str(output), origin
            ]
            sender.file_upload_progress[(peer, fid)] = [
                offset, len(payload), None, str(src), peer
            ]
        header = (receiver.server_received_file_header_sign + "\n").encode("utf-8")
        ack = (receiver.server_received_file_data_sign + "\n").encode("utf-8")
        merged = _coalesce_receiver_signs(monkeypatch, sender, control, ack)
        port, stamp = request_transfer_listener(
            sender, receiver, f"/file {shlex.quote(str(src))} {fid}"
        )
        assert sender.file_transfer_mode(
            str(src), peer[0], port, 0, control_socket=control,
            crypto=stamp, fid=fid, peer=peer, resume_offset=offset,
        ) is True
        assert merged == [header + ack], "the real receiver signs did not share one read"
        assert output.read_bytes() == payload
        assert wait_until(lambda: fid not in receiver.file_recv_progress)
        assert (peer, fid) not in sender.file_upload_progress
    finally:
        _close_socket_workers(server, clients, workers)


def _watch_sign_capacity(monkeypatch, sender, receiver, overflow, capacity_handled):
    """Hold body transmission while queued signs compact or a line overflows."""
    body_held = threading.Event()
    retained = []
    errors = []
    compactions = []
    real_feed = _TransferSignBuffer.feed
    real_wait = connect_tcp._wait_transfer_control
    real_scan = _TransferSignBuffer._scan_pending_locked
    real_send = receiver.send_message

    def observe_scan(signs):
        before = signs.pending_bytes
        real_scan(signs)
        if before >= TRANSFER_SIGN_BUFFER_LIMIT:
            compactions.append(before)
            capacity_handled.set()

    def observe_feed(signs, data):
        try:
            return real_feed(signs, data)
        except ValueError as exc:
            errors.append(str(exc))
            capacity_handled.set()
            raise
        finally:
            with signs.lock:
                retained.append(signs.pending_bytes + len(signs.partial))

    def inject_after_header(sock, message, is_send_raw=False):
        written = real_send(sock, message, is_send_raw=is_send_raw)
        if message == receiver.server_received_file_header_sign:
            assert body_held.wait(timeout=10)
            noise = (
                b"x" * TRANSFER_SIGN_BUFFER_LIMIT if overflow == "partial"
                else b"x\n" * (TRANSFER_SIGN_BUFFER_LIMIT // 2 + 1)
            )
            try:
                real_send(sock, noise)
            except OSError:
                pass  # the sender may already have rejected an overlong line
        return written

    def hold_body(instance, state, leg_socket):
        if instance is sender:
            body_held.set()
            assert capacity_handled.wait(timeout=10), "the buffer never reached or rejected its limit"
        return real_wait(instance, state, leg_socket)

    monkeypatch.setattr(_TransferSignBuffer, "feed", observe_feed)
    monkeypatch.setattr(_TransferSignBuffer, "_scan_pending_locked", observe_scan)
    monkeypatch.setattr(receiver, "send_message", inject_after_header)
    monkeypatch.setattr(connect_tcp, "_wait_transfer_control", hold_body)
    return retained, errors, compactions

@pytest.mark.parametrize("server_sends", [False, True], ids=["upload", "download"])
@pytest.mark.parametrize("overflow", ["partial", "pending"])
def test_sign_capacity_compacts_complete_rows_and_rejects_oversized_line(
    tmp_path, monkeypatch, server_sends, overflow
):
    """Full-line batches compact; a single unterminated line still fails."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    capacity_handled = threading.Event()
    try:
        client = _start_client(tmp_path, server)
        clients.append(client)
        client_addr = client.client_socket.getsockname()
        assert wait_until(lambda: client_addr in server.clients)
        sender, receiver = (server, client) if server_sends else (client, server)
        peer = client_addr if server_sends else (server.host, server.port)
        control = server.clients[client_addr]["socket"] if server_sends else client.client_socket
        recv_dir = tmp_path / "overflow_recv"
        recv_dir.mkdir()
        receiver.file_transfer_dir = str(recv_dir)
        src = tmp_path / "overflow.bin"
        payload = b"222\n\n111 " * 1024
        src.write_bytes(payload)
        fid = 709
        retained, errors, compactions = _watch_sign_capacity(
            monkeypatch, sender, receiver, overflow, capacity_handled
        )
        port, stamp = request_transfer_listener(sender, receiver, f"/file {src} {fid}")
        result = sender.file_transfer_mode(
            str(src), peer[0], port, 0, control_socket=control,
            crypto=stamp, fid=fid, peer=peer,
        )
        assert capacity_handled.is_set()
        assert retained and max(retained) <= TRANSFER_SIGN_BUFFER_LIMIT
        if overflow == "partial":
            assert result is False
            assert len(errors) == 1 and "unterminated transfer sign" in errors[0]
            assert not compactions
            row = sender.file_upload_progress[(peer, fid)]
            assert row[1] == len(payload) and row[3] == str(src) and row[4] == peer
            with open(sender.file_trans_progress_file, encoding="utf-8") as stream:
                persisted = json.load(stream)["upload"]
            assert json.dumps((peer, fid), separators=(",", ":")) in persisted
        else:
            assert result is True
            assert not errors and compactions and max(compactions) == TRANSFER_SIGN_BUFFER_LIMIT
            assert (recv_dir / src.name).read_bytes() == payload
            assert (peer, fid) not in sender.file_upload_progress
    finally:
        capacity_handled.set()
        _close_socket_workers(server, clients, workers)


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
        fid = 302
        tfid = 304  # route ownership, never a low-bit kind, identifies this leg
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
        assert server._register_fid_route(
            fid, forwarder_addr, {target_addr: tfid}
        ) is not None

        allocated_port = free_port()
        released_ports = []
        real_pfree = server.pfree

        def record_pfree(port):
            released_ports.append(port)
            return real_pfree(port)

        monkeypatch.setattr(server, "palloc", lambda: allocated_port)
        monkeypatch.setattr(server, "pfree", record_pfree)
        injected = inject_wsaeacces_once(
            monkeypatch,
            operation="bind",
            address=(server.host, allocated_port),
        )

        assert target._request_resumes() == 1

        def got():
            return partial.stat().st_size == len(payload)

        assert wait_until(got, timeout=20), "the forwarded resume did not finish"
        assert injected["raised"]
        assert wait_until(lambda: allocated_port in released_ports, timeout=10)
        bind_addresses = [call[1] for call in injected["calls"] if call[0] == "bind"]
        fixed_index = bind_addresses.index((server.host, allocated_port))
        assert (server.host, 0) in bind_addresses[fixed_index + 1:]
        assert partial.read_bytes() == payload
        assert wait_until(lambda: tfid not in target.file_recv_progress, timeout=10)
        assert wait_until(lambda: upload_key not in forwarder.file_upload_progress, timeout=10)
    finally:
        clients = [client for client in (forwarder, target) if client is not None]
        _close_socket_workers(server, clients, workers)


def test_sender_owned_fids_to_distinct_targets_survive_out_of_order_replies(
    tmp_path, monkeypatch
):
    """Keep source-owned upstream FIDs paired with targets after reply reordering."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    release_first = threading.Event()
    first_reply_waiting = threading.Event()
    first_port_blocked = threading.Event()
    second_reply_sent = threading.Event()
    request_lock = threading.Lock()
    request_names = {}
    fids_by_name = {}
    route_registrations = []
    server_allocations = []
    sender_threads = []
    try:
        source = _start_client(tmp_path, server, name="source")
        targets = []
        target_dirs = []
        for name in ("first", "second"):
            recv_dir = tmp_path / f"{name}_target"
            recv_dir.mkdir()
            target_dirs.append(recv_dir)
            targets.append(
                _start_client(tmp_path, server, name=f"{name}_target", recv_dir=recv_dir)
            )
        clients.extend((source, *targets))
        source_addr = source.client_socket.getsockname()
        target_addrs = [target.client_socket.getsockname() for target in targets]
        payloads = {
            "first.bin": os.urandom(96 * 1024 + 11),
            "second.bin": os.urandom(80 * 1024 + 7),
        }
        paths = {}
        for name, payload in payloads.items():
            path = tmp_path / name
            path.write_bytes(payload)
            paths[name] = path
        assert wait_until(lambda: all(
            addr in server.clients for addr in [source_addr, *target_addrs]
        ))

        real_source_send = source.send_message

        def capture_source_request(sock, message, is_send_raw=False):
            if isinstance(message, str) and message.startswith("/forward_item "):
                parts = shlex.split(message)
                name = next(name for name in payloads if name in parts)
                upstream_fid = int(next(part[4:] for part in parts if part.startswith("fid=")))
                upload_key = ((source.host, source.port), upstream_fid)
                assert source.file_upload_progress[upload_key][3] == str(paths[name])
                with request_lock:
                    request_names[upstream_fid] = name
                    fids_by_name[name] = upstream_fid
            return real_source_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(source, "send_message", capture_source_request)
        real_wait = source._transfer_handshakes.wait

        def observe_wait(record, timeout):
            with request_lock:
                name = request_names.get(record["key"][1])
            if name == "first.bin":
                first_reply_waiting.set()
            return real_wait(record, timeout)

        monkeypatch.setattr(source._transfer_handshakes, "wait", observe_wait)
        real_register = server._register_fid_route

        def capture_route(fid, originator, route_targets, completed=(), **kwargs):
            route_registrations.append((fid, tuple(originator), dict(route_targets)))
            return real_register(fid, originator, route_targets, completed, **kwargs)

        monkeypatch.setattr(server, "_register_fid_route", capture_route)
        real_allocate = server._fid_alloc.alloc

        def capture_allocation(*, receiver=None):
            fid = real_allocate(receiver=receiver)
            server_allocations.append((tuple(receiver) if receiver is not None else None, fid))
            return fid

        monkeypatch.setattr(server._fid_alloc, "alloc", capture_allocation)
        real_server_send = server.send_message

        def send_second_port_first(sock, message, is_send_raw=False):
            if isinstance(message, str) and message.startswith("/server_file_transfer_port "):
                parts = shlex.split(message)
                with request_lock:
                    name = request_names.get(int(parts[2]))
                if name == "first.bin":
                    first_port_blocked.set()
                    assert release_first.wait(timeout=20), "first port release was not signaled"
                    return real_server_send(sock, message, is_send_raw=is_send_raw)
                if name == "second.bin":
                    sent = real_server_send(sock, message, is_send_raw=is_send_raw)
                    second_reply_sent.set()
                    return sent
            return real_server_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(server, "send_message", send_second_port_first)
        outcomes = {}
        failures = []

        def forward(name, target_addr):
            try:
                outcomes[name] = source._forward_one(
                    "file", "", name, str(paths[name]), [target_addr]
                )
            except Exception as exc:
                failures.append((name, exc))

        first_sender = threading.Thread(
            target=forward, args=("first.bin", target_addrs[0]), daemon=True
        )
        sender_threads.append(first_sender)
        first_sender.start()
        assert first_reply_waiting.wait(timeout=15), "first upload never waited for its port"

        second_sender = threading.Thread(
            target=forward, args=("second.bin", target_addrs[1]), daemon=True
        )
        sender_threads.append(second_sender)
        second_sender.start()
        assert second_reply_sent.wait(timeout=20), "second target did not get its port first"
        second_output = target_dirs[1] / "second.bin"
        assert wait_until(
            lambda: second_output.exists() and second_output.read_bytes() == payloads["second.bin"],
            timeout=20,
        ), "the second target consumed the first target's transfer"
        assert first_port_blocked.wait(timeout=15), "first port response was not held"

        release_first.set()
        first_output = target_dirs[0] / "first.bin"
        assert wait_until(
            lambda: first_output.exists() and first_output.read_bytes() == payloads["first.bin"],
            timeout=20,
        ), "the first target did not receive its own delayed transfer"
        for sender in sender_threads:
            sender.join(timeout=10)
        assert not any(sender.is_alive() for sender in sender_threads)
        assert failures == []
        assert outcomes == {"first.bin": True, "second.bin": True}
        assert set(fids_by_name) == set(payloads)

        routes_by_fid = {
            fid: (originator, route_targets)
            for fid, originator, route_targets in route_registrations
        }
        assert len(routes_by_fid) == 2
        assert len(server_allocations) == 2
        for name, target_addr in zip(payloads, target_addrs, strict=True):
            upstream_fid = fids_by_name[name]
            assert routes_by_fid[upstream_fid][0] == source_addr
            assert set(routes_by_fid[upstream_fid][1]) == {target_addr}
        child_fids = {
            next(iter(route_targets.values()))
            for _, route_targets in routes_by_fid.values()
        }
        expected_allocations = {
            (target_addr, child_fid)
            for _, route_targets in routes_by_fid.values()
            for target_addr, child_fid in route_targets.items()
        }
        assert set(server_allocations) == expected_allocations
        assert {target for target, _ in server_allocations} == set(target_addrs)
        assert {fid for _, fid in server_allocations} == child_fids
        assert not source.file_upload_progress
        assert not server._fid_routes
        assert not server._forward_relays
    finally:
        release_first.set()
        _close_socket_workers(server, clients, workers)


def test_forward_resume_with_zero_offset_reuses_existing_route(tmp_path, monkeypatch):
    """A zero offset explicitly replays the existing route instead of allocating a new one."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    try:
        recv_dir = tmp_path / "zero_offset_target"
        recv_dir.mkdir()
        target = _start_client(tmp_path, server, name="zero_offset_target", recv_dir=recv_dir)
        source = _start_client(tmp_path, server, name="zero_offset_source")
        clients.extend((source, target))
        target_addr = target.client_socket.getsockname()
        source_addr = source.client_socket.getsockname()
        server_peer = (source.host, source.port)
        src = tmp_path / "empty.bin"
        src.write_bytes(b"")
        output = recv_dir / src.name
        output.write_bytes(b"")

        upstream_fid = source._fid_alloc.alloc(receiver=server_peer)
        child_fid = server._fid_alloc.alloc(receiver=target_addr)
        upload_key = (server_peer, upstream_fid)
        source.file_upload_progress[upload_key] = [0, 0, None, str(src), target_addr]
        target.file_recv_progress[child_fid] = [0, 0, None, str(output), source_addr]
        route = server._register_fid_route(
            upstream_fid, source_addr, {target_addr: child_fid}
        )
        assert route is not None

        commands = []
        real_send = source.send_message

        def capture_command(sock, message, is_send_raw=False):
            if isinstance(message, str) and message.startswith("/forward_item "):
                commands.append(shlex.split(message))
            return real_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(source, "send_message", capture_command)
        registrations = []
        real_register = server._register_fid_route

        def observe_registration(fid, originator, route_targets, completed=(), **kwargs):
            registrations.append(fid)
            return real_register(fid, originator, route_targets, completed, **kwargs)

        monkeypatch.setattr(server, "_register_fid_route", observe_registration)
        dropped_routes = []
        real_drop = server._drop_fid_route

        def observe_drop(fid, candidate):
            dropped_routes.append(candidate)
            return real_drop(fid, candidate)

        monkeypatch.setattr(server, "_drop_fid_route", observe_drop)
        allocations = []
        real_allocate = server._fid_alloc.alloc

        def observe_allocation(*, receiver=None):
            allocated = real_allocate(receiver=receiver)
            allocations.append((tuple(receiver) if receiver is not None else None, allocated))
            return allocated

        monkeypatch.setattr(server._fid_alloc, "alloc", observe_allocation)

        assert source._forward_one(
            "file", "", src.name, str(src), [target_addr],
            resume_offset=0, upstream_fid=upstream_fid,
        ) is True

        assert output.read_bytes() == b""
        assert commands and f"fid={upstream_fid}" in commands[0]
        assert "resume=0" in commands[0]
        assert registrations == []
        assert dropped_routes == [route]
        assert allocations == []
        assert upstream_fid not in server._fid_routes
        assert upload_key not in source.file_upload_progress
        assert child_fid not in target.file_recv_progress
    finally:
        _close_socket_workers(server, clients, workers)


def test_waiting_ordinary_listener_keeps_its_fid_and_resumes_after_relay_collision(
    tmp_path, monkeypatch
):
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    try:
        recv_dir = tmp_path / "ordinary_receive"
        recv_dir.mkdir()
        server.file_transfer_dir = str(recv_dir)
        prefix, tail = b"A partial file prefix\n", b"remaining body\n"
        src = tmp_path / "ordinary.bin"
        src.write_bytes(prefix + tail)
        uploader = _start_client(tmp_path, server, file_transfer_chunk_len=len(prefix))
        clients.append(uploader)
        forwarder = _start_client(tmp_path, server, name="forwarder")
        clients.append(forwarder)
        peer = uploader.client_socket.getsockname()
        server_peer = (server.host, server.port)
        fid = 606

        port, stamp = request_transfer_listener(uploader, server, f"/file {src.name} {fid}")
        record = server._receive_transfers[fid]
        assert record["owner"].getsockname()[1] == port
        assert fid not in server.file_recv_progress and fid not in server._active_transfer_sockets
        monkeypatch.setattr(forwarder._fid_alloc, "alloc", lambda *, receiver: fid)
        assert not forwarder._forward_one("file", "", src.name, str(src), [peer])
        assert server._receive_transfers[fid] is record and record["owner"].fileno() >= 0
        assert not server._fid_routes and not server._fid_route_index and not server._forward_relays

        # Use that same listener. Close just the data leg after the first chunk,
        # so the real receiver (rather than the test) creates the partial row.
        send = uploader.send_message

        def interrupt_tail(sock, message, is_send_raw=False):
            if message == tail:
                sock.shutdown(socket.SHUT_WR)
                return False
            return send(sock, message, is_send_raw=is_send_raw)

        with monkeypatch.context() as fault:
            fault.setattr(uploader, "send_message", interrupt_tail)
            assert not uploader.file_transfer_mode(
                str(src), server.host, port, 0, control_socket=uploader.client_socket,
                crypto=stamp, fid=fid, peer=server_peer,
            )
        assert wait_until(lambda: fid not in server._receive_transfers)
        output = recv_dir / src.name
        assert output.read_bytes() == prefix
        assert server.file_recv_progress[fid][0] == len(prefix)
        assert (server_peer, fid) in uploader.file_upload_progress

        completed = []
        transfer = uploader.file_transfer_mode

        def observe_ack(*args, **kwargs):
            result = transfer(*args, **kwargs)  # True only after consuming the final ACK
            completed.append((kwargs["fid"], kwargs["resume_offset"], result))
            return result

        monkeypatch.setattr(uploader, "file_transfer_mode", observe_ack)
        assert server._request_resumes(peer) == 1
        assert wait_until(lambda: completed, timeout=15)
        assert completed == [(fid, len(prefix), True)]
        assert output.read_bytes() == prefix + tail
        assert wait_until(lambda: not server.file_recv_progress and not server._receive_transfers)
        assert (server_peer, fid) not in uploader.file_upload_progress
    finally:
        _close_socket_workers(server, clients, workers)


def test_concurrent_new_relays_keep_one_owner_and_exclude_a_late_listener(tmp_path, monkeypatch):
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    release = threading.Event()
    try:
        recv_dir = tmp_path / "target_receive"
        recv_dir.mkdir()
        target = _start_client(tmp_path, server, name="target", recv_dir=recv_dir)
        clients.append(target)
        target_peer = target.client_socket.getsockname()
        fid = 606
        src = tmp_path / "relay.bin"
        src.write_bytes(b"only the admitted relay reaches the target")
        ready, refused = threading.Event(), threading.Event()
        start = threading.Barrier(2)
        outcomes = {}
        push = server.forward_item_to

        def hold_before_target_request(*args, **kwargs):
            ready.set()
            assert release.wait(timeout=15)
            return push(*args, **kwargs)

        def forward(source):
            start.wait(timeout=10)
            result = source._forward_one("file", "", src.name, str(src), [target_peer])
            outcomes[source.client_socket.getsockname()] = result
            if not result:
                refused.set()

        monkeypatch.setattr(server, "forward_item_to", hold_before_target_request)
        for name in ("first", "second"):
            source = _start_client(tmp_path, server, name=name)
            clients.append(source)
            monkeypatch.setattr(source._fid_alloc, "alloc", lambda *, receiver: fid)
        peers = {client.client_socket.getsockname() for client in clients}
        assert wait_until(lambda: peers.issubset(server.clients))
        for source in clients[1:]:
            threading.Thread(target=forward, args=(source,), daemon=True).start()
        assert ready.wait(timeout=10) and refused.wait(timeout=10)
        assert set(server._fid_routes) == set(server._receive_transfers) == {fid}
        route, record = server._fid_routes[fid], server._receive_transfers[fid]
        child = (target_peer, route["targets"][target_peer])
        assert server._fid_route_index == {child: fid}
        assert set(server._forward_relays) == {child}
        assert server._forward_relays[child] is record["owner"]
        assert record["control_socket"] is server.clients[route["originator"]]["socket"]

        # Drive the ordinary command while the relay still owns the FID. Its
        # listener setup must return without advertising a port or handing off.
        returned = threading.Event()
        receive = server.file_transfer_mode_recv

        def observe_listener(*args, **kwargs):
            receive(*args, **kwargs)
            returned.set()

        monkeypatch.setattr(server, "file_transfer_mode_recv", observe_listener)
        with _transfer_handshake(target, target.client_socket, fid) as request:
            assert connect_tcp._send_transfer_request(target, request, f"/file ordinary.bin {fid}")
            assert returned.wait(timeout=5)
            assert target._transfer_handshakes.wait(request, 0) is None
        assert server._receive_transfers[fid] is record and server._fid_routes[fid] is route
        assert fid not in server._active_transfer_sockets and not server.file_recv_progress

        release.set()
        assert wait_until(lambda: len(outcomes) == 2, timeout=15)
        assert sorted(outcomes.values()) == [False, True] and outcomes[route["originator"]]
        assert (recv_dir / src.name).read_bytes() == src.read_bytes()
        assert wait_until(lambda: not server._receive_transfers and not server._forward_relays)
        assert not server._fid_routes and not server._fid_route_index
    finally:
        release.set()
        _close_socket_workers(server, clients, workers)


def test_duplicate_upstream_fid_is_rejected_without_replacing_existing_route(
    tmp_path, monkeypatch
):
    """A duplicate source FID fails without mutating the existing route or progress."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    try:
        source = _start_client(tmp_path, server, name="first_source")
        duplicate = _start_client(tmp_path, server, name="duplicate_source")
        target = _start_client(tmp_path, server, name="collision_target")
        clients.extend((source, duplicate, target))
        target_addr = target.client_socket.getsockname()
        source_addr = source.client_socket.getsockname()
        duplicate_addr = duplicate.client_socket.getsockname()
        server_peer = (source.host, source.port)
        upstream_fid = source._fid_alloc.alloc(receiver=server_peer)
        child_fid = server._fid_alloc.alloc(receiver=target_addr)
        source_path = tmp_path / "original.bin"
        duplicate_path = tmp_path / "duplicate.bin"
        payload = os.urandom(4096)
        source_path.write_bytes(payload)
        duplicate_path.write_bytes(payload[::-1])
        source_upload_key = (server_peer, upstream_fid)
        source.file_upload_progress[source_upload_key] = [
            0, len(payload), None, str(source_path), target_addr
        ]
        route = server._register_fid_route(
            upstream_fid, source_addr, {target_addr: child_fid}
        )
        assert route is not None
        routes_before = dict(server._fid_routes)
        indexes_before = dict(server._fid_route_index)

        monkeypatch.setattr(
            duplicate._fid_alloc, "alloc", lambda *, receiver=None: upstream_fid
        )
        duplicate.is_enable_file_transfer_progress_bar = True
        errors = []
        real_server_send = server.send_message

        def observe_error(sock, message, is_send_raw=False):
            if isinstance(message, str) and message.startswith("/forward_error "):
                errors.append(shlex.split(message))
            return real_server_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(server, "send_message", observe_error)

        assert duplicate._forward_one(
            "file", "", duplicate_path.name, str(duplicate_path), [target_addr]
        ) is False

        assert wait_until(lambda: bool(errors))
        assert errors[0][0] == "/forward_error"
        assert errors[0][2] == "relay fid is already in use"
        assert server._fid_routes == routes_before
        assert server._fid_route_index == indexes_before
        assert server._fid_routes[upstream_fid] is route
        assert source.file_upload_progress[source_upload_key] == [
            0, len(payload), None, str(source_path), target_addr
        ]
        duplicate_upload_key = (server_peer, upstream_fid)
        assert duplicate.file_upload_progress[duplicate_upload_key] == [
            0, len(payload), None, str(duplicate_path), target_addr
        ]
        assert target.file_recv_progress == {}
        assert not server._forward_relays
        assert not server._receive_transfers  # failed registration released only its fresh claim
        assert duplicate_addr != source_addr
    finally:
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
    resume_requests = []
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
                "upstream_fid": kwargs.get("upstream_fid"),
                "offset": kwargs.get("resume_offset", 0),
            }
            replay_fid = call["upstream_fid"]
            if replay_fid is not None:
                call["upload"] = source.file_upload_progress.get(
                    ((source.host, source.port), replay_fid)
                )
            calls.append(call)
            result = real_forward(*args, **kwargs)
            outcomes.append(result)
            return result

        monkeypatch.setattr(source, "_forward_one", observe_forward)
        real_resume = source._resume_on_socket

        def observe_resume(target, fid, offset, control_socket, **kwargs):
            result = real_resume(target, fid, offset, control_socket, **kwargs)
            resume_requests.append((target, fid, offset))
            return result

        monkeypatch.setattr(source, "_resume_on_socket", observe_resume)
        upstream_acks = []
        terminal_controls = []
        source_control = server.clients[source_addr]["socket"]
        real_server_send = server.send_message

        def observe_upstream_ack(sock, message, is_send_raw=False):
            marker = message.encode("utf-8") if isinstance(message, str) else message
            written = real_server_send(sock, message, is_send_raw=is_send_raw)
            if (sock is source_control and isinstance(message, str)
                    and message.startswith(("/server_file_transfer_port ", "/forward_error "))):
                terminal_controls.append(shlex.split(message)[0])
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
        assert wait_until(lambda: not server._forward_relays and not server._receive_transfers, timeout=10)
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
        assert terminal_controls == ["/server_file_transfer_port"]
        upload_key = ((source.host, source.port), fid)
        assert set(source.file_upload_progress) == {upload_key}
        upload = source.file_upload_progress[upload_key]
        assert upload[0:2] == [len(payload), len(payload)]
        assert upload[3:] == [str(src), target_addrs[0]]
        assert len(upload) == 5
        assert source._transfer_progress_snapshot()["relays"] == {}
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

        for addr in failed:
            losing_ack[addr] = False
        for index, addr in enumerate(failed):
            target = receivers[target_addrs.index(addr)]
            child_fid = original_targets[addr]
            assert target._request_resumes() == 1
            if index == 0:
                assert resume_ack.wait(timeout=10)
                assert len(calls) == 2
                assert source.file_upload_progress[upload_key][0:2] == [len(payload), len(payload)]
                assert source.file_upload_progress[upload_key][3:] == [str(src), target_addrs[0]]
                # Removing a stale active marker cannot bypass the listener owner.
                target._active_transfer_fids.discard(child_fid)
                assert target._request_resumes() == 0
                assert resume_requests == [(addr, fid, len(payload))]
                assert len(calls) == 2, "a duplicate resume started another upload"
                assert source._resume_sends[(addr, fid)]["targets"] == {addr}
                release_ack.set()
            assert wait_until(lambda: len(outcomes) == index + 2, timeout=10)
            assert wait_until(lambda: not server._forward_relays and not server._receive_transfers
                              and not source._resume_sends, timeout=10)
            assert outcomes[-1] is (index == len(failed) - 1)
            assert len(upstream_acks) == (1 if index == len(failed) - 1 else 0)
            assert terminal_controls == ["/server_file_transfer_port"] * (index + 2)
            assert source.client_socket.fileno() >= 0 and source.running
            assert calls[-1]["upstream_fid"] == fid
            assert calls[-1]["targets"] == [addr]
            assert calls[-1]["offset"] == len(payload)
            assert calls[-1]["upload"] is not None
            assert len(calls[-1]["upload"]) == 5
            assert calls[-1]["upload"][3:] == [str(src), target_addrs[0]]
            assert source._transfer_progress_snapshot()["relays"] == {}
            assert route["targets"] == original_targets
            assert child_fid not in target.file_recv_progress
            if index != len(failed) - 1:
                assert server._fid_routes[fid] is route
                assert len(source.file_upload_progress[upload_key]) == 5
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
        assert source._transfer_progress_snapshot()["relays"] == {}
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


@pytest.mark.parametrize("kind,empty", [("file", False), ("folder", False), ("file", True)],
                         ids=["file", "folder", "empty"])
@pytest.mark.parametrize("ack_failure", ["false", "exception"])
def test_forward_ack_write_failure_retains_source_and_completed_route(
    tmp_path, monkeypatch, ack_failure, kind, empty
):
    """Failed upstream ACKs can be explicitly reconfirmed without reopening Targets."""
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
        payload = b"" if empty else os.urandom(64 * 1024 + 37)
        src.write_bytes(payload)
        rel_dir = "bundle/nested" if kind == "folder" else ""
        output = recv_dir / rel_dir / src.name
        failed_writes = []
        real_send = server.send_message

        def fail_upstream_ack(sock, message, is_send_raw=False):
            marker = message.encode("utf-8") if isinstance(message, str) else message
            if marker == server.server_received_file_data_sign.encode("utf-8"):
                failed_writes.append(sock)
                if len(failed_writes) <= 2:
                    if ack_failure == "exception":
                        raise ConnectionError("injected upstream final ACK write failure")
                    return False
            return real_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(server, "send_message", fail_upstream_ack)
        assert source._forward_one(kind, rel_dir, src.name, str(src), [target_addr]) is False
        assert len(failed_writes) == 1
        assert wait_until(lambda: not server._forward_relays and not server._receive_transfers, timeout=10)
        assert output.read_bytes() == payload
        assert wait_until(lambda: not target.file_recv_progress, timeout=10)
        assert len(source.file_upload_progress) == 1
        upload_key, upload = next(iter(source.file_upload_progress.items()))
        assert upload_key[0] == (source.host, source.port)
        fid = upload_key[1]
        assert len(upload) == 5 and upload[3:] == [str(src), target_addr]
        assert source.file_upload_progress[upload_key][0:2] == [len(payload), len(payload)]
        route = server._fid_routes[fid]
        assert route["completed"] == {target_addr}
        assert set(route["targets"]) == {target_addr}
        assert server._fid_route_index == {}
        with open(source.file_trans_progress_file, encoding="utf-8") as stream:
            snapshot = json.load(stream)
        assert len(snapshot["upload"]) == 1
        assert snapshot["relays"] == {}

        reloaded = TCP_Client_Base(
            host=source.host, port=source.port, is_extend_command=True,
            is_input_command_in_console=False, is_enable_encrypto=False,
        )
        _isolate(reloaded, tmp_path, "reloaded_source")
        reloaded.file_trans_progress_file = source.file_trans_progress_file
        try:
            assert reloaded._load_transfer_progress() == 1
            assert (
                reloaded.file_upload_progress[upload_key]
                == source.file_upload_progress[upload_key]
            )
            assert reloaded._transfer_progress_snapshot()["relays"] == {}
        finally:
            reloaded.file_trans_progress_file = str(tmp_path / "inspection_progress.json")
            reloaded.close()

        target.close()
        assert wait_until(lambda: target_addr not in server.clients)
        outcomes, headers = [], []
        real_forward, real_source_send = source._forward_one, source.send_message

        def forward(*args, **kwargs):
            result = real_forward(*args, **kwargs)
            outcomes.append(result)
            return result

        def observe_header(sock, message, is_send_raw=False):
            if isinstance(message, bytes):
                headers.append(message)
            return real_source_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(source, "_forward_one", forward)
        monkeypatch.setattr(source, "send_message", observe_header)
        assert source.resume_trans(target_addr, fid, len(payload)) is True
        assert wait_until(lambda: outcomes == [False] and not source._resume_sends, timeout=15)
        assert len(failed_writes) == 2
        assert server._fid_routes[fid] is route
        assert source.file_upload_progress[upload_key] is upload
        assert source.resume_trans(target_addr, fid, len(payload)) is True
        assert wait_until(lambda: outcomes == [False, True] and not source._resume_sends, timeout=15)
        assert len(failed_writes) == 3
        expected = [len(src.name.encode()).to_bytes(4, "big"), src.name.encode(),
                    len(payload).to_bytes(8, "big")]
        assert headers == expected * 2  # Both attempts sent a header and zero body bytes.
        assert output.read_bytes() == payload
        if kind == "folder":
            assert not (recv_dir / src.name).exists()
        assert not server._fid_routes and not server._fid_route_index
        assert not source.file_upload_progress
        assert wait_until(lambda: not server._receive_transfers and not server._forward_relays)
        with open(server.file_trans_progress_file, encoding="utf-8") as stream:
            assert json.load(stream)["relays"] == {}
        with open(source.file_trans_progress_file, encoding="utf-8") as stream:
            assert json.load(stream)["upload"] == {}
    finally:
        _close_socket_workers(server, clients, workers)


def test_eof_confirmation_ignores_old_recovery_owner_for_another_target(tmp_path, monkeypatch):
    """A completed prior route's scoped finally cannot block a different Target."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    held, release = threading.Event(), threading.Event()
    try:
        old_source = _start_client(tmp_path, server, name="old_source")
        clients.append(old_source)
        old_dir = tmp_path / "old_target"
        old_dir.mkdir()
        old_target = _start_client(tmp_path, server, name="old_target", recv_dir=old_dir)
        clients.append(old_target)
        old_peer = old_target.client_socket.getsockname()
        path = tmp_path / "scoped_confirmation.bin"
        payload = b"confirmed by two separate routes"
        path.write_bytes(payload)
        target_acks = []
        real_target_send = old_target.send_message

        def target_send(sock, message, is_send_raw=False):
            if message == old_target.server_received_file_data_sign:
                target_acks.append(sock)
                if len(target_acks) == 1:
                    return False
            return real_target_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(old_target, "send_message", target_send)
        assert old_source._forward_one("file", "", path.name, str(path), [old_peer]) is False
        fid = next(iter(server._fid_routes))
        assert wait_until(lambda: not server._forward_relays and not server._receive_transfers)
        assert wait_until(lambda: not old_target._receive_transfers)
        real_release = connect_tcp._release_receive

        def release_receive(instance, key, record):
            if instance is server and key == (old_peer, fid):
                held.set()
                assert release.wait(timeout=30)
            return real_release(instance, key, record)

        monkeypatch.setattr(connect_tcp, "_release_receive", release_receive)
        assert old_target._request_resumes() == 1
        assert held.wait(timeout=15)
        assert fid not in server._fid_routes
        old_record = server._receive_transfers[(old_peer, fid)]
        assert wait_until(lambda: not old_source._resume_sends)

        source = _start_client(tmp_path, server, name="new_source")
        clients.append(source)
        new_dir = tmp_path / "new_target"
        new_dir.mkdir()
        target = _start_client(tmp_path, server, name="new_target", recv_dir=new_dir)
        clients.append(target)
        peer = target.client_socket.getsockname()
        control = server.clients[source.client_socket.getsockname()]["socket"]
        # Independent Source allocators can choose the same value after the
        # previous route has been dropped; the live key is still Target-scoped.
        monkeypatch.setattr(source._fid_alloc, "alloc", lambda **kwargs: fid)
        failures = []
        real_send = server.send_message

        def server_send(sock, message, is_send_raw=False):
            record = server._receive_transfers.get(fid)
            if (message == server.server_received_file_data_sign and not failures
                    and record is not None and record["control_socket"] is control):
                failures.append(sock)
                return False
            return real_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(server, "send_message", server_send)
        assert source._forward_one("file", "", path.name, str(path), [peer]) is False
        assert len(failures) == 1
        assert wait_until(lambda: fid not in server._receive_transfers and not server._forward_relays)
        assert server._fid_routes[fid]["completed"] == {peer}
        outcomes = []
        real_forward = source._forward_one

        def forward(*args, **kwargs):
            result = real_forward(*args, **kwargs)
            outcomes.append(result)
            return result

        monkeypatch.setattr(source, "_forward_one", forward)
        assert source.resume_trans(peer, fid, len(payload))
        assert wait_until(lambda: not source._resume_sends, timeout=10)
        assert outcomes == [True]
        assert server._receive_transfers[(old_peer, fid)] is old_record
        assert not release.is_set(), "unrelated finally was needed for confirmation"
        assert fid not in server._fid_routes and not server._fid_route_index
        assert not source.file_upload_progress and not old_source.file_upload_progress
        assert (old_dir / path.name).read_bytes() == payload
        assert (new_dir / path.name).read_bytes() == payload
        release.set()
        assert wait_until(lambda: not server._receive_transfers)
    finally:
        release.set()
        _close_socket_workers(server, clients, workers)


def test_eof_confirmation_rejects_bad_headers_and_waits_for_finally(tmp_path, monkeypatch):
    """An EOF request cannot bypass header validation or a finalizing owner."""
    from PyFlow.network_api.connect_tcp import _send_transfer_request

    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    source = _start_client(tmp_path, server)
    released, cleaning = threading.Event(), threading.Event()
    sockets = []
    fid, target = 713, ("127.0.0.1", free_port())
    try:
        source._resume_thread.join(timeout=10)
        origin, control = source.client_socket.getsockname(), source.client_socket
        route = server._register_fid_route(fid, origin, {target: 714}, (target,))
        path = tmp_path / "confirmed.bin"
        path.write_bytes(b"abc")
        key = ((source.host, source.port), fid)
        row = source.file_upload_progress[key] = [3, 3, None, str(path), target]
        source._persist_transfer_progress()
        real_release = connect_tcp._release_receive

        def release(instance, identity, record):
            if instance is server and identity == fid and not released.is_set():
                cleaning.set()
                assert released.wait(timeout=20)
            return real_release(instance, identity, record)

        def request(offset):
            with _transfer_handshake(source, control, fid) as handshake:
                command = (f"/forward_item file fid={fid} resume={offset} "
                           f"confirmed.bin {shlex.quote(repr(target))}")
                assert _send_transfer_request(source, handshake, command)
                return source._transfer_handshakes.wait(handshake, 5)

        header = (len(path.name).to_bytes(4, "big") + path.name.encode()
                  + (3).to_bytes(8, "big"))
        for offset, body in [(2, header), (4, header), (3, header[:-1])]:
            result = request(offset)
            assert result is not None and result[0] == "port"
            data = socket.create_connection((source.host, result[2]), timeout=5)
            sockets.append(data)
            assert server._forward_read_sign(data, server.server_start_file_transfer_sign)
            if not cleaning.is_set():
                monkeypatch.setattr(connect_tcp, "_release_receive", release)
            data.sendall(body)
            data.shutdown(socket.SHUT_WR)
            with data.makefile("rb") as stream:
                reply = stream.read()
            assert reply == (server.error_sign + "\n").encode()
            if not released.is_set():
                assert cleaning.wait(timeout=5)
                blocked = request(3)
                assert blocked is not None and blocked[0] == "error"
                assert server._receive_transfers[fid]["owner"] is not None
                released.set()
            assert wait_until(lambda: not server._receive_transfers and not server._forward_relays)
            assert server._fid_routes[fid] is route and route["completed"] == {target}
            assert source.file_upload_progress[key] is row
            data.close()
        assert source.resume_trans(target, fid, 3)
        assert wait_until(lambda: key not in source.file_upload_progress, timeout=10)
        assert wait_until(lambda: not server._fid_routes and not server._receive_transfers)
        assert not server.file_upload_progress and not server.file_recv_progress
    finally:
        released.set()
        for data in sockets:
            data.close()
        _close_socket_workers(server, [source], workers)


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
        assert len(source.file_upload_progress) == 1
        upload_key, upload = next(iter(source.file_upload_progress.items()))
        assert upload_key[0] == (source.host, source.port)
        fid = upload_key[1]
        assert len(upload) == 5 and upload[3:] == [str(src), target_addr]
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
        assert source._transfer_progress_snapshot()["relays"] == {}
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
            parts = shlex.split(command)
            tfid = int(parts[-1])
            stamp = bad._crypto_transfer_stamp(client_socket)
            bad.send_message(
                client_socket,
                f"/server_file_transfer_port {refused_listener.getsockname()[1]} "
                f"{tfid} {stamp}\n",
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

        def observe_resume(client_socket, peer, requested_fid, offset, origin, target):
            try:
                return real_resume_request(
                    client_socket, peer, requested_fid, offset, origin, target
                )
            finally:
                handled.append((peer, requested_fid, offset))

        monkeypatch.setattr(server, "_resume_request", observe_resume)
        bad_fid = route["targets"][bad_addr]
        unknown_fid = max([fid, *route["targets"].values()]) + 2
        assert good.send_message(
            good.client_socket, format_resume_line(bad_fid, 0, source_addr, good_addr)
        )
        assert bad.send_message(
            bad.client_socket, format_resume_line(unknown_fid, 0, source_addr, bad_addr)
        )
        assert wait_until(lambda: len(handled) == 2, timeout=10)
        assert set(handled) == {(good_addr, bad_fid, 0), (bad_addr, unknown_fid, 0)}
        assert server._relay_recoveries == {}
        assert bad.send_message(
            bad.client_socket, format_resume_line(bad_fid, 0, source_addr, bad_addr)
        )
        assert wait_until(lambda: len(handled) == 3, timeout=10)
        recovery = server._relay_recoveries[fid]
        assert recovery["route"] is route and recovery["active"] == {}
        assert recovery["pending"] == {bad_addr: (server.clients[bad_addr]["socket"], 0)}
        assert server._fid_route_index == expected_index
        assert route["completed"] == {good_addr}
        assert server._fid_routes[fid] is route
    finally:
        refused_listener.close()
        if restored is not None:
            restored.stop()
        _close_socket_workers(server, clients, workers)


@pytest.mark.parametrize("at_eof", [False, True], ids=["partial", "before-final-ack"])
def test_forward_one_to_one_session_recovery(tmp_path, monkeypatch, at_eof):
    """B's final ACK cannot erase C's same-path row; only C replays after reconnect."""
    chunk = 8192
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path, file_transfer_chunk_len=chunk)
    clients = []
    b_ready, c_ready, replay_ready = (threading.Event() for _ in range(3))
    finish_b, retire_c, finish_replay = (threading.Event() for _ in range(3))

    def gate_ack(target, ready, release):
        send = target.send_message

        def gated(sock, message, is_send_raw=False):
            if message == target.server_received_file_data_sign and not release.is_set():
                ready.set()
                assert release.wait(20), "final target ACK was not released"
            return send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(target, "send_message", gated)

    try:
        source = _start_client(tmp_path, server, name="source", is_forward_one_to_one=True,
                               max_thread_num=2, file_transfer_chunk_len=chunk)
        clients.append(source)
        for name in ("B", "C"):
            clients.append(_start_client(tmp_path, server, name=name, recv_dir=tmp_path / name,
                                         client_port=free_port(), file_transfer_chunk_len=chunk))
        _, b, c = clients
        origin, bp, cp = (node.client_socket.getsockname() for node in clients)
        assert wait_until(lambda: {origin, bp, cp} <= server.clients.keys())
        path = tmp_path / "same source.bin"
        payload = os.urandom(4 * chunk + 17)
        path.write_bytes(payload)
        destination = "original/nested path"
        c_output = tmp_path / "C" / destination / path.name
        gate_ack(b, b_ready, finish_b)
        if at_eof:
            gate_ack(c, c_ready, retire_c)
        else:
            receive = c.receive_message

            def stop_after_prefix(sock, size):
                if sock is not c.client_socket and not retire_c.is_set() and any(
                    row[0] >= chunk for row in c.file_recv_progress.copy().values()
                ):
                    c_ready.set()
                    assert retire_c.wait(20), "partial receiver was not released"
                return receive(sock, size)

            monkeypatch.setattr(c, "receive_message", stop_after_prefix)

        outcomes, dispatches = [], []
        real_forward = source._forward_one

        def observe_forward(*args, **kwargs):
            ok = real_forward(*args, **kwargs)
            outcomes.append((tuple(args[4]), ok))
            return ok

        def observe_dispatch(peer, msg):
            if isinstance(msg, str) and msg.startswith("/file "):
                dispatches.append((peer, int(shlex.split(msg)[-1])))

        monkeypatch.setattr(source, "_forward_one", observe_forward)
        server.add_send_message_listener(observe_dispatch)
        source.forward_file_console(shlex.join(["/forward_file", str(path), repr(bp),
                                                repr(cp), destination]))
        assert b_ready.wait(15) and c_ready.wait(15)
        rows = source.file_upload_progress.copy()
        assert len(rows) == 2 and all(row[3] == str(path) for row in rows.values())
        keys = {row[4]: key for key, row in rows.items()}
        b_key, c_key = keys[bp], keys[cp]
        assert b_key[0] == c_key[0] == (source.host, source.port) and b_key != c_key
        route = server._fid_routes[c_key[1]]
        child = route["targets"][cp]
        b_child = server._fid_routes[b_key[1]]["targets"][bp]
        assert child != b_child and set(route["targets"]) == {cp}
        assert wait_until(lambda: source.file_upload_progress[c_key][0] == len(payload))
        old = c.client_socket
        # Server FIN first keeps C's configured port reusable. Its native
        # control teardown closes the blocked read/ACK socket, not B's leg.
        server.clients[cp]["socket"].shutdown(socket.SHUT_WR)
        c.receive_thread.join(10)
        assert not c.receive_thread.is_alive() and not c.running
        c._release_client_socket()
        retire_c.set()
        assert wait_until(lambda: outcomes == [((cp,), False)] and cp not in server.clients
                          and not c._receive_transfers, timeout=10)
        offset = c_output.stat().st_size
        if at_eof:
            assert offset == len(payload)
        else:
            assert 0 < offset < len(payload)
        assert c_output.read_bytes() == payload[:offset]
        row = c.file_recv_progress[child]
        assert row[:3] == [offset, len(payload), None]
        # The destination from the wire can retain forward slashes on Windows.
        assert os.path.normpath(row[3]) == str(c_output)
        assert row[4:] == [origin]
        finish_b.set()
        assert wait_until(lambda: outcomes == [((cp,), False), ((bp,), True)]
                          and not server._forward_relays, timeout=15)
        assert (tmp_path / "B" / destination / path.name).read_bytes() == payload
        assert server._fid_routes == {c_key[1]: route} and not route["completed"]
        assert server._fid_route_index == {(cp, child): c_key[1]}
        assert set(source.file_upload_progress) == {c_key} and not b.file_recv_progress
        with open(source.file_trans_progress_file, encoding="utf-8") as snapshot:
            assert json.load(snapshot)["upload"] == {
                json.dumps(c_key, separators=(",", ":")):
                    [len(payload), len(payload), None, str(path), list(cp)]
            }

        # Count successful replay traffic, not attempted sends. C must request
        # its disk offset even though the source had already uploaded everything.
        requests, sizes = [], []

        def observe_replay(_, msg):
            if isinstance(msg, bytes):
                sizes.append(len(msg))
            elif msg.startswith("/forward_item "):
                requests.append(shlex.split(msg))

        source.add_send_message_listener(observe_replay)
        gate_ack(c, replay_ready, finish_replay)
        assert c.connect() and c.client_socket is not old and c.client_socket.getsockname() == cp
        assert replay_ready.wait(15)
        assert requests == [["/forward_item", "file", f"fid={c_key[1]}", f"resume={offset}",
                             path.name, repr(cp)]]
        assert server._fid_routes[c_key[1]] is route and not route["completed"]
        assert c_key in source.file_upload_progress and len(outcomes) == 2
        finish_replay.set()
        assert wait_until(lambda: outcomes == [((cp,), False), ((bp,), True), ((cp,), True)]
                          and not server._forward_relays, timeout=15)
        assert sum(sizes) == 4 + len(path.name.encode()) + 8 + len(payload) - offset
        initial = [(f"{bp[0]}:{bp[1]}", b_child), (f"{cp[0]}:{cp[1]}", child)]
        assert sorted(dispatches[:2]) == sorted(initial) and dispatches[2:] == [initial[1]]
        assert c_output.read_bytes() == payload
        assert not server._fid_routes and not server._fid_route_index
        assert wait_until(lambda: not source.file_upload_progress and not c.file_recv_progress
                          and not server._receive_transfers and not source._resume_sends)
    finally:
        retire_c.set()
        finish_b.set()
        finish_replay.set()
        _close_socket_workers(server, clients, workers)


def test_forward_route_is_removed_only_after_all_real_targets_complete(tmp_path, monkeypatch):
    """A completed sibling leaves the route live until the other real ACK arrives."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    held, release = threading.Event(), threading.Event()
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
        real_send = clients[2].send_message

        def hold_second_ack(sock, message, is_send_raw=False):
            if message == clients[2].server_received_file_data_sign:
                held.set()
                assert release.wait(20), "second Target ACK was not released"
            return real_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(clients[2], "send_message", hold_second_ack)
        src = tmp_path / "complete_forward.bin"
        payload = os.urandom(96 * 1024)
        src.write_bytes(payload)
        result = {}
        senders = []
        transfer = source.file_transfer_mode

        def observe_upload(*args, **kwargs):
            senders.append(threading.current_thread())
            result["ack"] = transfer(*args, **kwargs)
            return result["ack"]

        monkeypatch.setattr(source, "file_transfer_mode", observe_upload)
        source.forward_file_console(shlex.join(["/forward_file", str(src), *map(repr, targets)]))
        assert held.wait(15), "the second Target never reached its final ACK"
        sender, = senders  # The default public command opened one shared upload.
        assert sender.is_alive() and result == {}
        assert len(server._fid_routes) == 1
        fid, route = next(iter(server._fid_routes.items()))
        assert set(route["targets"]) == set(targets)
        assert wait_until(lambda: route["completed"] == {targets[0]}, timeout=10)
        assert outputs[0].read_bytes() == outputs[1].read_bytes() == payload
        assert wait_until(lambda: not clients[1].file_recv_progress, timeout=10)
        expected_index = {(targets[1], route["targets"][targets[1]]): fid}
        assert server._fid_route_index == expected_index
        assert server._lookup_fid_route(targets[0], route["targets"][targets[0]]) is None
        assert server._lookup_fid_route(targets[1], route["targets"][targets[1]])[1] is route

        release.set()
        sender.join(timeout=20)
        assert not sender.is_alive(), "the real source transfer did not finish"
        assert result == {"ack": True}
        assert wait_until(lambda: not server._forward_relays, timeout=10)
        assert wait_until(lambda: not clients[2].file_recv_progress, timeout=10)
        assert server._fid_routes == {}
        assert server._fid_route_index == {}
        assert server._lookup_fid_route(None, fid) is None
        for target, tfid in route["targets"].items():
            assert server._lookup_fid_route(target, tfid) is None
    finally:
        release.set()
        _close_socket_workers(server, clients, workers)


def test_pause_and_start_trans_control_a_plain_transfer(tmp_path):
    """A sender preset holds the body until start releases the same data leg."""
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

        transfer_port, _stamp = request_transfer_listener(client, server, f"/file {src} {fid}")

        assert client.pause_trans(fid, trans_type=0, direction=0) is False  # armed before the leg starts
        sender = threading.Thread(
            target=client.file_transfer_mode,
            args=(str(src), "127.0.0.1", transfer_port, 0),
            kwargs={"fid": fid, "peer": (server.host, server.port),
                    "control_socket": client.client_socket, "crypto": _stamp},
            daemon=True,
        )
        sender.start()
        upload_key = ((client.host, client.port), fid)
        assert wait_until(lambda: client.file_upload_progress.get(upload_key)), (
            "the uploader never registered the transfer"
        )
        control_key = ((client.host, client.port), fid, 0, 0)
        assert wait_until(lambda: control_key in client._transfer_controls._legs)
        output = server_recv / "payload.bin"
        assert not output.exists() or output.stat().st_size == 0, "the paused leg sent data"

        assert client.start_trans(fid, trans_type=0, direction=0) is True  # a live leg is released
        assert wait_until(
            lambda: output.exists() and output.stat().st_size == len(payload), timeout=20
        ), "the released transfer did not finish"
        assert output.read_bytes() == payload
        sender.join(timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


def _watch_resume_request(monkeypatch, receiver, control):
    """Observe the bytes entering this control decoder and the resulting resume line."""
    wire, requests = bytearray(), []
    channel = receiver._channels[control]
    feed = channel.feed

    def observe_bytes(data):
        wire.extend(data)
        return feed(data)

    def observe_line(_peer, message):
        if isinstance(message, str) and message.startswith("/resume_trans "):
            requests.append(connect_tcp.parse_resume_command(message))

    monkeypatch.setattr(channel, "feed", observe_bytes)
    receiver.add_message_listener(observe_line)
    return wire, requests


@pytest.mark.parametrize("forwarded", [False, True], ids=["ordinary", "relay"])
def test_download_resumes_over_the_encrypted_channel(tmp_path, monkeypatch, forwarded):
    """Each hop delivers the exact numeric request in a frame, through the final ACK."""
    workers = _track_socket_workers(monkeypatch)
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
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    clients = []

    def connect_peer(name, recv_dir):
        peer = TCP_Client_Base(
            host="127.0.0.1",
            port=port,
            client_host="127.0.0.1",
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=True,
        )
        clients.append(peer)
        _isolate(peer, tmp_path, name)
        _redirect_crypto(peer.crypto, tmp_path / name, ssh_dir)
        recv_dir.mkdir()
        peer.file_transfer_dir = str(recv_dir)
        assert peer.connect()
        peer._resume_thread.join(timeout=20)  # finish the initial empty reconnect scan
        assert not peer._resume_thread.is_alive()
        control = peer.client_socket
        assert wait_until(lambda: control in peer._encrypted_sockets, timeout=15)
        assert peer._wait_control_secure(control)
        assert server._wait_control_secure(server.clients[control.getsockname()]["socket"])
        return peer

    try:
        recv_dir = tmp_path / "client_recv"
        client = connect_peer("receiver", recv_dir)
        target = client.client_socket.getsockname()
        sender = connect_peer("source", tmp_path / "source_recv") if forwarded else server
        server_peer = (server.host, server.port)
        origin = sender.client_socket.getsockname() if forwarded else server_peer

        src = tmp_path / "download.bin"
        payload = os.urandom(64 * 1024)
        src.write_bytes(payload)
        fid = 908  # ID on the receiver's leg
        partial = recv_dir / "download.bin"
        with open(partial, "wb") as f:
            f.write(payload[:1024])
        client.file_recv_progress[fid] = [1024, len(payload), None, str(partial), origin]
        downstream = _watch_resume_request(monkeypatch, server, server.clients[target]["socket"])
        probes = [(downstream, fid)]
        if forwarded:
            upstream = 906
            assert server._register_fid_route(upstream, origin, {target: fid}) is not None
            source_probe = _watch_resume_request(monkeypatch, sender, sender.client_socket)
            probes.append((source_probe, upstream))
            upload_key = (server_peer, upstream)
            send_method = "_forward_one"
        else:
            upload_key = (target, fid)
            send_method = "file_transfer_mode"
        sender.file_upload_progress[upload_key] = [0, len(payload), None, str(src), target]

        send = getattr(sender, send_method)
        completed = []

        def observe_ack(*args, **kwargs):
            result = send(*args, **kwargs)
            completed.append((kwargs["resume_offset"], result))
            return result

        monkeypatch.setattr(sender, send_method, observe_ack)
        assert client._request_resumes() == 1
        assert wait_until(lambda: partial.stat().st_size == len(payload), timeout=20), (
            "the encrypted resume did not finish"
        )
        assert partial.read_bytes() == payload
        assert wait_until(lambda: fid not in client.file_recv_progress, timeout=10)
        assert wait_until(lambda: upload_key not in sender.file_upload_progress, timeout=10)
        assert wait_until(lambda: completed, timeout=10)
        assert completed == [(1024, True)]
        for (wire, requests), request_fid in probes:
            assert requests == [(request_fid, 1024, origin, target)]
            assert connect_tcp.FRAME_MAGIC in wire and b"/resume_trans" not in wire
    finally:
        _close_socket_workers(server, clients, workers)


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


@pytest.mark.parametrize("operation", ["bind", "connect"])
def test_client_falls_back_when_fixed_port_returns_wsaeacces(
    tmp_path, monkeypatch, operation
):
    server = _start_server(tmp_path)
    client_port = free_port()
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=server.port,
        client_host="127.0.0.1",
        client_port=client_port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    _isolate(client, tmp_path, "client")
    address = (
        ("127.0.0.1", client_port)
        if operation == "bind"
        else ("127.0.0.1", server.port)
    )
    injected = inject_wsaeacces_once(
        monkeypatch, operation=operation, address=address
    )
    try:
        assert client.connect(), "the client did not connect after WSAEACCES"
        assert injected["raised"]
        assert client.client_socket.getsockname()[1] != client_port
        assert wait_until(
            lambda: server.clients.get(client.client_socket.getsockname()) is not None
        ), "the server never saw the client"
    finally:
        client.close()
        server.stop()


@pytest.mark.parametrize("client_port", [None, 0], ids=["unbound", "os-assigned"])
def test_client_does_not_fallback_on_wsaeacces_without_fixed_port(
    tmp_path, monkeypatch, client_port
):
    server = _start_server(tmp_path)
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=server.port,
        client_host="127.0.0.1",
        client_port=client_port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        is_wait_server=False,
    )
    _isolate(client, tmp_path, "client")
    server_address = ("127.0.0.1", server.port)
    injected = inject_wsaeacces_once(
        monkeypatch, operation="connect", address=server_address
    )
    try:
        assert client.connect() is False
        assert injected["raised"]
        attempts = [
            call
            for call in injected["calls"]
            if call[:2] == ("connect", server_address)
        ]
        assert len(attempts) == 1
        bind_attempts = [call for call in injected["calls"] if call[0] == "bind"]
        if client_port is None:
            assert not bind_attempts
        else:
            assert [call[1] for call in bind_attempts] == [("127.0.0.1", 0)]
    finally:
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
        assert server.pause_trans(fid, client_addr, trans_type=0, direction=0) is False, "the leg was already running"

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
        assert wait_until(lambda: dead_leg.fileno() == -1, timeout=10), (
            "the disconnected control session left its paused data socket open"
        )
        # the reconnecting client sweeps the dead session's legs and then asks
        # for the remainder; only the swept leg may be released, or it would
        # write from its own offset into the file the resumed leg continues
        assert wait_until(
            lambda: server._active_transfer_sockets.get(upload_key) is not dead_leg, timeout=20
        ), "the dead session's leg was never cleared"
        server.start_trans(fid, client_addr, trans_type=0, direction=0)  # the test, not the protocol, parked this leg
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


@pytest.mark.parametrize("late_second_request", [False, True], ids=["queued-together", "late-target"])
@pytest.mark.parametrize("fail_first_again", [False, True], ids=["both-finish", "first-fails-again"])
def test_forward_recovery_waits_for_old_finally_and_runs_targets_independently(
    tmp_path, monkeypatch, late_second_request, fail_first_again
):
    """Real independent bodies/ACKs survive a held or failed sibling and old cleanup."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    original_held, original_release = threading.Event(), threading.Event()
    finally_held, finally_release = threading.Event(), threading.Event()
    first_held, first_release = threading.Event(), threading.Event()
    second_written, second_ack = threading.Event(), threading.Event()
    second_body_held, second_body_release = threading.Event(), threading.Event()
    local = threading.local()
    try:
        source = _start_client(tmp_path, server, name="parallel_source")
        clients.append(source)
        source.file_transfer_chunk_len = server.file_transfer_chunk_len = 8192
        payload = os.urandom(128 * 1024 + 29)
        digest = hashlib.sha256(payload).digest()
        src = tmp_path / "parallel_recovery.bin"
        src.write_bytes(payload)
        receivers, outputs = [], []
        for index in range(2):
            directory = tmp_path / f"parallel_target_{index}"
            directory.mkdir()
            target = _start_client(
                tmp_path, server, name=f"parallel_target_{index}", recv_dir=directory
            )
            target.file_transfer_chunk_len = 8192
            clients.append(target)
            receivers.append(target)
            outputs.append(directory / "original/nested" / src.name)
        origin = source.client_socket.getsockname()
        targets = [target.client_socket.getsockname() for target in receivers]
        assert wait_until(lambda: all(peer in server.clients for peer in [origin, *targets]))
        controls = {peer: server.clients[peer]["socket"] for peer in [origin, *targets]}
        client_controls = [owner.client_socket for owner in (source, *receivers)]
        forwards, outcomes, dispatches, opened, reads, source_files = [], [], [], [], [], []
        source_sockets, handled, target_acks, upstream_acks = {}, [], [], []
        initial = {}
        offsets = [8192, 3 * 8192]
        real_wait = connect_tcp._wait_transfer_control
        real_open_target = connect_tcp._open_transfer_target
        real_release = connect_tcp._release_receive
        real_forward, real_dispatch = source._forward_one, server.forward_item_to
        real_resume_request = server._resume_request
        real_server_send = server.send_message

        def hold_body(instance, state, sock):
            if instance is source:
                peer, offset = getattr(local, "stream", (None, 0))
                if peer is None:
                    initial.setdefault("socket", sock)
                    initial["waits"] = initial.get("waits", 0) + 1
                    if initial["waits"] == 4:
                        original_held.set()
                        assert original_release.wait(timeout=30)
                else:
                    source_sockets.setdefault((peer, offset), sock)
                    if peer == targets[0] and offset == offsets[0]:
                        first_held.set()
                        assert first_release.wait(timeout=30)
                        if fail_first_again:
                            sock.shutdown(socket.SHUT_RDWR)
                            sock.close()
                            raise ConnectionError("the first recovery data connection failed again")
                    if peer == targets[1] and fail_first_again:
                        second_body_held.set()
                        assert second_body_release.wait(timeout=30)
            return real_wait(instance, state, sock)

        def source_open(path, mode="r", *args, **kwargs):
            file = builtins.open(path, mode, *args, **kwargs)
            if str(path) == str(src) and mode == "rb":
                stream = getattr(local, "stream", (None, 0))
                source_files.append((stream, file))
                real_read = file.read

                def read(size=-1):
                    start = file.tell()
                    data = real_read(size)
                    reads.append((stream, start, len(data)))
                    return data

                monkeypatch.setattr(file, "read", read)
            return file

        def open_target(path, offset):
            file = real_open_target(path, offset)
            names = [str(output) for output in outputs]
            if str(path) not in names:
                return file
            index = names.index(str(path))
            opened.append((index, offset))
            real_write, received = file.write, 0

            def write(data):
                nonlocal received
                count = real_write(data)
                received += count
                file.flush()
                if offset == 0 and received == offsets[index]:
                    child = next(iter(receivers[index]._active_transfer_sockets))
                    sock = receivers[index]._active_transfer_sockets[child]
                    sock.shutdown(socket.SHUT_RDWR)
                    sock.close()
                    if index == 1:
                        second_written.set()
                return count

            monkeypatch.setattr(file, "write", write)
            return file

        def release_receive(instance, key, record):
            if instance is server and key == initial.get("fid"):
                finally_held.set()
                assert finally_release.wait(timeout=30)
            return real_release(instance, key, record)

        def forward(*args, **kwargs):
            resumed = kwargs.get("upstream_fid")
            peer = args[4][0] if resumed is not None else None
            offset = kwargs.get("resume_offset", 0)
            local.stream = (peer, offset)
            forwards.append((list(args[4]), resumed, offset))
            try:
                result = real_forward(*args, **kwargs)
                outcomes.append((peer, offset, result))
                return result
            finally:
                del local.stream

        def dispatch(target, kind, rel_dir, fname, originator, child, destination=None):
            dispatches.append((target, child))
            return real_dispatch(target, kind, rel_dir, fname, originator, child, destination)

        def resume_request(sock, peer, child, offset, originator, target):
            result = real_resume_request(sock, peer, child, offset, originator, target)
            handled.append((peer, child, offset, result))
            return result

        final_ack_failed = []

        def server_send(sock, message, is_send_raw=False):
            if (late_second_request and message == server.server_received_file_data_sign
                    and not final_ack_failed):
                final_ack_failed.append(sock)
                return False
            result = real_server_send(sock, message, is_send_raw=is_send_raw)
            if result and message == server.server_received_file_data_sign:
                upstream_acks.append(sock)
            return result

        monkeypatch.setattr(connect_tcp, "_wait_transfer_control", hold_body)
        monkeypatch.setattr(connect_tcp, "_open_transfer_target", open_target)
        monkeypatch.setattr(connect_tcp, "_release_receive", release_receive)
        monkeypatch.setattr(connect_tcp, "open", source_open, raising=False)
        monkeypatch.setattr(source, "_forward_one", forward)
        monkeypatch.setattr(server, "forward_item_to", dispatch)
        monkeypatch.setattr(server, "_resume_request", resume_request)
        monkeypatch.setattr(server, "send_message", server_send)
        for index, target in enumerate(receivers):
            real_send = target.send_message

            def send(sock, message, is_send_raw=False, *, index=index, real_send=real_send):
                written = real_send(sock, message, is_send_raw=is_send_raw)
                if written and message == receivers[index].server_received_file_data_sign:
                    target_acks.append(index)
                    if index == 1:
                        second_ack.set()
                return written

            monkeypatch.setattr(target, "send_message", send)

        sender = threading.Thread(
            target=source._forward_one,
            args=("folder", "original/nested", src.name, str(src), targets), daemon=True,
        )
        sender.start()
        assert original_held.wait(timeout=15) and second_written.wait(timeout=15)
        fid, route = next(iter(server._fid_routes.items()))
        initial["fid"] = fid
        old = next(iter(server._forward_relays.values()))
        children = dict(route["targets"])
        upload_key = ((source.host, source.port), fid)
        assert wait_until(lambda: all(
            children[peer] not in target._receive_transfers
            for peer, target in zip(targets, receivers)
        ))
        assert [output.read_bytes() for output in outputs] == [payload[:offset] for offset in offsets]
        # The wire carries the receiver's disk offset, not an offset query. The
        # live shared route must not be cancelled merely because A asks again.
        assert receivers[0]._request_resumes() == 1
        assert wait_until(lambda: len(handled) == 1)
        assert handled[0] == (targets[0], children[targets[0]], offsets[0], True)
        recovery = server._relay_recoveries[fid]
        assert recovery["pending"][targets[0]] == (controls[targets[0]], offsets[0])
        assert recovery["active"] == {}
        assert not old["done"].is_set()
        assert old["sockets"] and initial["socket"].fileno() >= 0
        assert forwards == [(targets, None, 0)]
        if not late_second_request:
            assert receivers[1]._request_resumes() == 1
            assert wait_until(lambda: len(handled) == 2)
            assert recovery["pending"][targets[1]] == (controls[targets[1]], offsets[1])
        original_release.set()
        assert finally_held.wait(timeout=15)
        assert server._receive_transfers[fid]["owner"] is old
        assert forwards == [(targets, None, 0)], "recovery raced the old owner's finally"
        assert recovery["active"] == {}
        finally_release.set()
        assert first_held.wait(timeout=15)
        assert wait_until(lambda: (None, 0, False) in outcomes)
        assert fid not in server._receive_transfers
        assert server._receive_transfers[(targets[0], fid)]["owner"] is not old
        assert not first_release.is_set()
        # A is blocked before its first resumed source read. A late B must still
        # obtain its own source file/socket and complete a real Target ACK now.
        if late_second_request:
            assert receivers[1]._request_resumes() == 1
        # Both receiver ownership and Server admission reject an active duplicate.
        assert receivers[0]._request_resumes() == 0
        assert not server._resume_request(
            controls[targets[0]], targets[0], children[targets[0]], offsets[0], origin, targets[0]
        )
        assert forwards.count(([targets[0]], fid, offsets[0])) == 1
        if fail_first_again:
            assert second_body_held.wait(timeout=15)
            first_release.set()
            assert wait_until(lambda: (targets[0], offsets[0], False) in outcomes)
            assert upload_key in source.file_upload_progress and not route["completed"]
            second_body_release.set()
        assert second_ack.wait(timeout=15)
        assert wait_until(lambda: route["completed"] == {targets[1]})
        assert wait_until(lambda: (targets[1], offsets[1], False) in outcomes)
        assert first_release.is_set() is fail_first_again
        assert target_acks == [1]
        assert upstream_acks == []
        assert outputs[1].read_bytes() == payload
        assert hashlib.sha256(outputs[1].read_bytes()).digest() == digest
        assert outputs[0].read_bytes() == payload[:offsets[0]]
        assert children[targets[1]] not in receivers[1].file_recv_progress
        assert server._fid_routes[fid] is route and route["targets"] == children
        assert server._fid_route_index == {(targets[0], children[targets[0]]): fid}
        upload = source.file_upload_progress[upload_key]
        assert len(upload) == 5 and upload[1] == len(payload)
        assert upload[3:] == [str(src), targets[0]]
        assert set(source.file_upload_progress) == {upload_key}
        assert source_sockets[(targets[0], offsets[0])] is not source_sockets[(targets[1], offsets[1])]
        resumed_files = [(stream, file) for stream, file in source_files if stream[0] is not None]
        assert {stream for stream, _ in resumed_files} == {(targets[0], offsets[0]), (targets[1], offsets[1])}
        assert len({id(file) for _, file in resumed_files}) == 2
        assert not any(stream == (targets[0], offsets[0]) for stream, _, _ in reads)
        assert next(start for stream, start, _ in reads if stream == (targets[1], offsets[1])) == offsets[1]
        assert sum(count for stream, _, count in reads if stream == (targets[1], offsets[1])) == len(payload) - offsets[1]
        first_release.set()
        if fail_first_again:
            assert wait_until(lambda: (targets[0], offsets[0], False) in outcomes)
            assert wait_until(lambda: not server._forward_relays and not source._resume_sends
                              and not recovery["active"])
            assert route["completed"] == {targets[1]} and target_acks == [1]
            assert server._fid_routes[fid] is route
            assert upload_key in source.file_upload_progress
            assert outputs[0].read_bytes() == payload[:offsets[0]]
            assert wait_until(lambda: children[targets[0]] not in receivers[0]._receive_transfers)
            # A's explicit later retry cannot replay already-ACKed B.
            fail_first_again = False
            assert receivers[0]._request_resumes() == 1
        assert wait_until(lambda: route["completed"] == set(targets), timeout=20)
        if late_second_request:
            assert wait_until(lambda: final_ack_failed and not server._forward_relays
                              and not server._receive_transfers and not source._resume_sends)
            assert (targets[0], offsets[0], False) in outcomes
            assert upstream_acks == [] and source.file_upload_progress[upload_key] is upload
            assert server._fid_routes[fid] is route
            assert source.resume_trans(targets[0], fid, len(payload))
        assert wait_until(lambda: (targets[0], len(payload) if late_second_request else offsets[0], True)
                          in outcomes, timeout=15)
        assert next(start for stream, start, _ in reads if stream == (targets[0], offsets[0])) == offsets[0]
        assert sum(count for stream, _, count in reads if stream == (targets[0], offsets[0])) == len(payload) - offsets[0]
        assert target_acks == [1, 0]
        assert len(upstream_acks) == 1
        assert wait_until(lambda: not server._forward_relays and not server._receive_transfers
                          and not server._relay_recoveries and not source._resume_sends)
        assert not server._fid_routes and not server._fid_route_index and not source.file_upload_progress
        assert route["targets"] == children
        assert all(not target.file_recv_progress for target in receivers)
        for output in outputs:
            assert output.read_bytes() == payload
            assert hashlib.sha256(output.read_bytes()).digest() == digest
        assert set(opened) == {(0, 0), (0, offsets[0]), (1, 0), (1, offsets[1])}
        assert dispatches.count((targets[1], children[targets[1]])) == 2
        assert all(child == children[peer] for peer, child in dispatches)
        assert [owner.client_socket for owner in (source, *receivers)] == client_controls
        assert all(server.clients[peer]["socket"] is sock and sock.fileno() >= 0
                   for peer, sock in controls.items())
    finally:
        original_release.set()
        finally_release.set()
        first_release.set()
        second_body_release.set()
        _close_socket_workers(server, clients, workers)



def test_concurrent_forward_upload_handshakes_match_out_of_order_replies(tmp_path, monkeypatch):
    """A later relay reply cannot hand its port/FID to the first waiting upload."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    release_first = threading.Event()
    first_reply_waiting = threading.Event()
    first_port_blocked = threading.Event()
    second_reply_sent = threading.Event()
    request_names = {}
    request_lock = threading.Lock()
    sender_threads = []
    try:
        source = _start_client(tmp_path, server, name="ordered_source")
        target_dir = tmp_path / "ordered_target"
        target_dir.mkdir()
        target = _start_client(
            tmp_path, server, name="ordered_target", recv_dir=target_dir
        )
        clients.extend((source, target))
        source_addr = source.client_socket.getsockname()
        target_addr = target.client_socket.getsockname()
        payloads = {
            "first.bin": os.urandom(96 * 1024 + 11),
            "second.bin": os.urandom(80 * 1024 + 7),
        }
        paths = {}
        for name, payload in payloads.items():
            path = tmp_path / name
            path.write_bytes(payload)
            paths[name] = path
        assert wait_until(lambda: source_addr in server.clients and target_addr in server.clients)

        original_client_send = source.send_message

        def track_forward_request(sock, message, is_send_raw=False):
            if isinstance(message, str) and message.startswith("/forward_item "):
                parts = shlex.split(message)
                fid = int(next(part[4:] for part in parts if part.startswith("fid=")))
                name = next(item for item in payloads if item in parts)
                with request_lock:
                    request_names[fid] = name
            return original_client_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(source, "send_message", track_forward_request)
        original_wait = source._transfer_handshakes.wait

        def observe_wait(record, timeout):
            with request_lock:
                name = request_names.get(record["key"][1])
            if name == "first.bin":
                first_reply_waiting.set()
            return original_wait(record, timeout)

        monkeypatch.setattr(source._transfer_handshakes, "wait", observe_wait)
        original_server_send = server.send_message

        def send_second_port_first(sock, message, is_send_raw=False):
            if isinstance(message, str) and message.startswith("/server_file_transfer_port "):
                parts = shlex.split(message)
                with request_lock:
                    name = request_names.get(int(parts[2]))
                if name == "first.bin":
                    first_port_blocked.set()
                    assert release_first.wait(timeout=20), "first port response was not held"
                    return original_server_send(sock, message, is_send_raw=is_send_raw)
                if name == "second.bin":
                    sent = original_server_send(sock, message, is_send_raw=is_send_raw)
                    second_reply_sent.set()
                    return sent
            return original_server_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(server, "send_message", send_second_port_first)
        outcomes = {}
        failures = []

        def forward(name):
            try:
                outcomes[name] = source._forward_one(
                    "file", "", name, str(paths[name]), [target_addr]
                )
            except Exception as exc:
                failures.append((name, exc))

        first_sender = threading.Thread(target=forward, args=("first.bin",), daemon=True)
        sender_threads.append(first_sender)
        first_sender.start()
        assert first_reply_waiting.wait(timeout=15), "first upload never waited for its port"

        second_sender = threading.Thread(target=forward, args=("second.bin",), daemon=True)
        sender_threads.append(second_sender)
        second_sender.start()
        assert second_reply_sent.wait(timeout=20), "second upload did not get its port first"
        second_output = target_dir / "second.bin"
        assert wait_until(
            lambda: second_output.exists() and second_output.read_bytes() == payloads["second.bin"],
            timeout=20,
        ), "second transfer consumed the first request's port/FID"
        assert first_port_blocked.wait(timeout=15), "first port response was not held"

        release_first.set()
        first_output = target_dir / "first.bin"
        assert wait_until(
            lambda: first_output.exists() and first_output.read_bytes() == payloads["first.bin"],
            timeout=20,
        ), "first transfer did not resume on its own delayed port/FID"
        for sender in sender_threads:
            sender.join(timeout=10)
        assert not any(sender.is_alive() for sender in sender_threads)
        assert failures == []
        assert outcomes == {"first.bin": True, "second.bin": True}
        assert set(request_names.values()) == {"first.bin", "second.bin"}
        assert len(request_names) == 2
        assert not source.file_upload_progress
        assert not server._fid_routes
        assert not server._forward_relays
    finally:
        release_first.set()
        _close_socket_workers(server, clients, workers)


@pytest.mark.parametrize("server_sends", [False, True], ids=["upload", "download"])
def test_unresolved_listener_isolates_same_session_fid(tmp_path, monkeypatch, server_sends):
    """A late terminal response cannot reopen an uncertain same-session FID."""
    from PyFlow.network_api.connect_tcp import _send_transfer_request, _transfer_handshake

    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    old_reply = []
    try:
        recv_dir = tmp_path / "late_ports"
        recv_dir.mkdir()
        client = _start_client(tmp_path, server, name="late_ports", recv_dir=recv_dir)
        clients.append(client)
        server.file_transfer_dir = str(recv_dir)
        peer = client.client_socket.getsockname()
        assert wait_until(lambda: peer in server.clients)
        sender, receiver = (server, client) if server_sends else (client, server)
        control = server.clients[peer]["socket"] if server_sends else client.client_socket
        actual_send = receiver.send_message
        fid = 806
        held = threading.Event()

        orphan_src = tmp_path / "orphan.bin"
        orphan_src.write_bytes(b"orphan listener payload")

        def hold_first_listener_reply(sock, message, is_send_raw=False):
            if isinstance(message, str) and message.startswith("/server_file_transfer_port "):
                if not old_reply:
                    old_reply.append(message)
                    held.set()
                    return True
            return actual_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(receiver, "send_message", hold_first_listener_reply)
        with _transfer_handshake(sender, control, fid) as record:
            assert _send_transfer_request(
                sender, record, f"/file {shlex.quote(str(orphan_src))} {fid}"
            )
            assert held.wait(timeout=10), "the original listener response was not held"
            assert sender._transfer_handshakes.wait(record, 0) is None
        assert old_reply
        with _transfer_handshake(sender, control, fid) as blocked:
            assert blocked is None
            assert not _send_transfer_request(sender, blocked, f"/file retry.bin {fid}")

        late_port = old_reply[0]
        late_error = f"/forward_error {fid} {shlex.quote('late listener error')}"
        if server_sends:
            server.handle_command(control, peer, late_port)
            assert not sender._transfer_handshakes.resolve_error(
                control, fid, "late listener error"
            )
        else:
            client.handle_server_command(late_port, control_socket=control)
            client.handle_server_command(late_error, control_socket=control)

        src = tmp_path / "fresh.bin"
        payload = os.urandom(96 * 1024 + 13)
        src.write_bytes(payload)
        port, stamp = request_transfer_listener(sender, receiver, f"/file {src} {fid + 1}")
        assert sender.file_transfer_mode(
            str(src), "127.0.0.1", port, 0, control_socket=control, crypto=stamp,
            fid=fid + 1, peer=peer if server_sends else (server.host, server.port),
        )
        assert (recv_dir / src.name).read_bytes() == payload
        command_dir = recv_dir / "made" / "by" / "command"
        assert sender.send_message(control, "/file_folder /made/by/command")
        assert wait_until(command_dir.is_dir)
        assert sender._transfer_handshakes.register(control, fid) is None
    finally:
        _close_socket_workers(server, clients, workers)


@pytest.mark.parametrize("server_sends", [False, True], ids=["upload", "download"])
def test_reconnected_handshake_rejects_reply_from_old_control_socket(tmp_path, monkeypatch, server_sends):
    """An old-session callback cannot resolve a new-session listener request."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    try:
        recv_dir = tmp_path / "session_ports"
        recv_dir.mkdir()
        client = _start_client(
            tmp_path, server, name="session_ports", recv_dir=recv_dir, client_port=free_port()
        )
        clients.append(client)
        server.file_transfer_dir = str(recv_dir)
        old_client = client.client_socket
        old_peer = old_client.getsockname()
        assert wait_until(lambda: old_peer in server.clients)
        old_server = server.clients[old_peer]["socket"]
        old_sender = server if server_sends else client
        old_receiver = client if server_sends else server
        old_control = old_server if server_sends else old_client
        old_receiver_control = old_client if server_sends else old_server
        old_src = tmp_path / "old_session.bin"
        old_src.write_bytes(b"old session listener")
        old_port, old_stamp = request_transfer_listener(
            old_sender, old_receiver, f"/file {shlex.quote(str(old_src))} 808"
        )
        old_callback = f"/server_file_transfer_port {old_port} 808 {old_stamp}\n"
        old_resources = tuple(
            old_receiver._transfer_handshakes._resources[old_receiver_control]
        )
        old_client.shutdown(socket.SHUT_RDWR)
        assert wait_until(lambda: not client.running and old_peer not in server.clients)
        client.receive_thread.join(timeout=10)
        client._release_client_socket()
        assert old_resources and all(sock.fileno() == -1 for sock in old_resources)
        assert client.connect()
        peer = client.client_socket.getsockname()
        assert wait_until(lambda: peer in server.clients)
        sender, receiver = (server, client) if server_sends else (client, server)
        control = server.clients[peer]["socket"] if server_sends else client.client_socket
        assert control is not old_control
        actual_send = receiver.send_message
        rejected = []

        def replay_old_session_callback(sock, message, is_send_raw=False):
            if isinstance(message, str) and message.startswith("/server_file_transfer_port "):
                if server_sends:
                    server.handle_command(old_server, old_peer, old_callback)
                else:
                    client.handle_server_command(old_callback, control_socket=old_client)
                record = sender._transfer_handshakes._pending[(control, 808)]
                assert record["result"] is None
                rejected.append(record)
            return actual_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(receiver, "send_message", replay_old_session_callback)
        src = tmp_path / "new_session.bin"
        payload = os.urandom(32781)
        src.write_bytes(payload)
        port, stamp = request_transfer_listener(
            sender, receiver, f"/file {shlex.quote(str(src))} 808"
        )
        assert len(rejected) == 1
        assert sender.file_transfer_mode(
            str(src), "127.0.0.1", port, 0, control_socket=control, crypto=stamp, fid=808,
            peer=peer if server_sends else (server.host, server.port),
        )
        assert (recv_dir / src.name).read_bytes() == payload
        assert not sender._transfer_handshakes._pending
    finally:
        _close_socket_workers(server, clients, workers)


@pytest.mark.parametrize(
    "server_host,client_host",
    [("127.0.0.1", "localhost"), ("0.0.0.0", "127.0.0.1")],
    ids=["localhost-alias", "wildcard-bind"],
)
@pytest.mark.parametrize(
    "recorded_peer", ["configured", "connected"], ids=["legacy-row", "new-row"]
)
def test_resume_identity_uses_the_connected_endpoint(
    tmp_path, server_host, client_host, recorded_peer
):
    """Resume works when the configured host differs from the connected one.

    The configured address can name a wildcard bind or a host alias; the
    concrete endpoint the kernel selected for the control connection is what
    both sides can compare. A legacy direct row still records the configured
    server address and is migrated in memory, without touching the file format.
    """
    port = free_port()
    server = TCP_Server_Base(
        host=server_host,
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    _isolate(server, tmp_path, f"server_{server_host}_{recorded_peer}")
    server_recv = tmp_path / f"server_recv_{server_host}_{recorded_peer}"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server)

    client = TCP_Client_Base(
        host=client_host,
        port=port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    _isolate(client, tmp_path, f"client_{client_host}_{recorded_peer}")
    client_recv = tmp_path / f"client_recv_{client_host}_{recorded_peer}"
    client_recv.mkdir()
    client.file_transfer_dir = str(client_recv)
    try:
        assert client.connect()
        peer = client.client_socket.getsockname()
        assert wait_until(lambda: peer in server.clients, timeout=10)
        src = tmp_path / f"identity_{recorded_peer}.bin"
        payload = os.urandom(64 * 1024)
        src.write_bytes(payload)
        half = len(payload) // 2
        configured_peer = (client.host, client.port)
        connected_peer = client.client_socket.getpeername()

        # client -> server: the server asks the reconnected client to continue
        partial = server_recv / src.name
        partial.write_bytes(payload[:half])
        upload_key = (configured_peer, 701)
        client.file_upload_progress[upload_key] = [
            half, len(payload), None, str(src), configured_peer,
        ]
        server.file_recv_progress[701] = [
            half, len(payload), None, str(partial), peer,
        ]
        assert server._request_resumes(peer) == 1
        assert wait_until(lambda: 701 not in server.file_recv_progress, timeout=20)
        assert partial.read_bytes() == payload
        assert wait_until(lambda: upload_key not in client.file_upload_progress, timeout=10)

        # server -> client: the client asks the connected server to continue
        partial2 = client_recv / src.name
        partial2.write_bytes(payload[:half])
        recorded = configured_peer if recorded_peer == "configured" else connected_peer
        server.file_upload_progress[(peer, 702)] = [
            half, len(payload), None, str(src), peer,
        ]
        client.file_recv_progress[702] = [
            half, len(payload), None, str(partial2), recorded,
        ]
        assert client._request_resumes() == 1
        assert wait_until(lambda: 702 not in client.file_recv_progress, timeout=20)
        assert partial2.read_bytes() == payload
        assert wait_until(lambda: (peer, 702) not in server.file_upload_progress, timeout=10)
    finally:
        client.close()
        server.stop()


def test_closed_control_socket_falls_back_to_the_configured_endpoint(tmp_path):
    """A socket that is already gone falls back to the configured identity."""
    server = _start_server(tmp_path)
    dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dead.close()
    try:
        assert _socket_local_endpoint(dead) is None
        assert _socket_peer_endpoint(dead) is None
        assert server._control_local_endpoint(dead) == (server.host, server.port)
        assert server._is_control_local_endpoint((server.host, server.port), dead)
        assert not server._is_control_local_endpoint(("127.0.0.1", 1), dead)
    finally:
        server.stop()


@pytest.mark.parametrize("sender_side", ["server", "client"])
def test_paused_leg_is_released_by_session_retirement(tmp_path, monkeypatch, sender_side):
    """Retire the actual session, then recover stable IDs with a real final ACK.

    Closing wakes the pause wait; reconnect must wait for the original owner
    without closing a replacement session's data sockets.
    """
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    try:
        server_recv = tmp_path / "server_recv"
        server_recv.mkdir()
        server.file_transfer_dir = str(server_recv)
        client_recv = tmp_path / "client_recv"
        client_recv.mkdir()
        client = _start_client(tmp_path, server, name="client", recv_dir=client_recv,
                               client_port=free_port())
        clients.append(client)
        peer = client.client_socket.getsockname()
        assert wait_until(lambda: peer in server.clients)

        src = tmp_path / "paused.bin"
        src.write_bytes(os.urandom(64 * 1024))
        fid = 4322
        if sender_side == "server":
            sender, receiver = server, client
            command = (
                f"/file {shlex.quote(str(src))} {shlex.quote(repr(peer))} {fid}"
            )
            key = (peer, fid)
            target = peer
            assert sender.pause_trans(fid, peer, trans_type=0, direction=0) is False
        else:
            sender, receiver = client, server
            command = f"/file {shlex.quote(str(src))} {fid}"
            key = ((client.host, client.port), fid)
            target = (client.host, client.port)
            assert sender.pause_trans(fid, trans_type=0, direction=0) is False
        port, stamp = request_transfer_listener(sender, receiver, command)
        control = (
            server.clients[peer]["socket"] if sender is server else client.client_socket
        )
        result = {}

        def send_leg():
            with _sending_transfer(sender, key, control_socket=control), _transfer_handshake(
                sender, control, fid
            ):
                return sender.file_transfer_mode(
                    str(src),
                    "127.0.0.1",
                    port,
                    0,
                    control_socket=control,
                    crypto=stamp,
                    fid=fid,
                    peer=target,
                )

        leg = threading.Thread(
            target=lambda: result.setdefault("value", send_leg()), daemon=True
        )
        leg.start()
        assert wait_until(
            lambda: key in sender._active_transfer_sockets
            and key in sender.file_upload_progress,
            timeout=10,
        ), "the paused leg never registered"
        control_key = (*key, 0, 0)
        assert sender._transfer_pause.get(control_key) is True
        _retire_transfer_session(sender, control)
        assert wait_until(lambda: not leg.is_alive(), timeout=10), (
            "the retired leg stayed paused"
        )
        assert result.get("value") is False
        assert control_key not in sender._transfer_pause
        assert key in sender.file_upload_progress

        assert not _start_resume_send(sender, key, target, lambda row: pytest.fail("retired owner"),
                                      control_socket=control)
        # Let the server send FIN first: an active close on the client can leave
        # its fixed port in TIME_WAIT on Windows, changing the resume identity.
        server.clients[peer]["socket"].shutdown(socket.SHUT_WR)
        client.receive_thread.join(timeout=10)
        assert not client.receive_thread.is_alive()
        assert not client.running
        client._release_client_socket()
        assert wait_until(lambda: peer not in server.clients)
        assert client.connect()
        assert client.client_socket.getsockname() == peer, "client endpoint changed on reconnect"
        output = (client_recv if receiver is client else server_recv) / src.name
        assert wait_until(lambda: key not in sender.file_upload_progress
                          and fid not in receiver.file_recv_progress, timeout=20)
        assert output.read_bytes() == src.read_bytes()
    finally:
        _close_socket_workers(server, clients, workers)