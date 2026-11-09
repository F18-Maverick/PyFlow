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

from helpers import (
    free_port,
    inject_wsaeacces_once,
    isolate_instance_files,
    request_transfer_listener,
    server_ready,
    wait_until,
)

from PyFlow.network_api.connect_tcp import (
    TRANSFER_SIGN_BUFFER_LIMIT,
    TCP_Client_Base,
    TCP_Server_Base,
    _TransferSignBuffer,
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

        transfer_port, _stamp = request_transfer_listener(client, server, f"/file {src} {fid}")

        client.pause_trans(fid)  # hold the leg before its first chunk
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
        transfer_port, _stamp = request_transfer_listener(
            client, server, f"/file {shlex.quote(str(src))} {fid}"
        )

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

    def hold_body(_key):
        body_held.set()
        assert capacity_handled.wait(timeout=10), "the buffer never reached or rejected its limit"

    monkeypatch.setattr(_TransferSignBuffer, "feed", observe_feed)
    monkeypatch.setattr(_TransferSignBuffer, "_scan_pending_locked", observe_scan)
    monkeypatch.setattr(receiver, "send_message", inject_after_header)
    monkeypatch.setattr(sender, "_wait_transfer_paused", hold_body)
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

        def capture_route(fid, originator, route_targets, completed=()):
            route_registrations.append((fid, tuple(originator), dict(route_targets)))
            return real_register(fid, originator, route_targets, completed)

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

        def observe_registration(fid, originator, route_targets, completed=()):
            registrations.append(fid)
            return real_register(fid, originator, route_targets, completed)

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
        resume_requests = []
        real_resume = source.resume_trans

        def observe_resume(target, fid, offset):
            result = real_resume(target, fid, offset)
            resume_requests.append((target, fid, offset))
            return result

        monkeypatch.setattr(source, "resume_trans", observe_resume)
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
                assert source.file_upload_progress[upload_key][0:2] == [len(payload), len(payload)]
                assert source.file_upload_progress[upload_key][3:] == [str(src), addr]
                # The first request's data socket is still registered. Replay
                # the reconnect request after clearing that stale session marker.
                target._active_transfer_fids.discard(child_fid)
                assert target._request_resumes() == 1
                assert wait_until(lambda: len(resume_requests) == 2, timeout=10)
                assert resume_requests == [(addr, fid, len(payload))] * 2
                assert len(calls) == 2, "a duplicate resume started another upload"
                release_ack.set()
            assert wait_until(lambda: len(outcomes) == index + 2, timeout=10)
            assert wait_until(lambda: not server._forward_relays, timeout=10)
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


@pytest.mark.parametrize("ack_failure", ["false", "exception"])
def test_forward_ack_write_failure_retains_source_and_completed_route(
    tmp_path, monkeypatch, ack_failure
):
    """A failed original ACK write keeps the source upload and the Server route."""
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

        def observe_resume(peer, requested_fid, offset, origin, target):
            try:
                return real_resume_request(peer, requested_fid, offset, origin, target)
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
        assert server._pending_originator_resumes == {}
        assert bad.send_message(
            bad.client_socket, format_resume_line(bad_fid, 0, source_addr, bad_addr)
        )
        assert wait_until(lambda: len(handled) == 3, timeout=10)
        assert server._pending_originator_resumes == {source_addr: {(fid, bad_addr): 0}}
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

        transfer_port, _stamp = request_transfer_listener(client, server, f"/file {src} {fid}")

        assert client.pause_trans(fid) is False  # armed before the leg starts
        sender = threading.Thread(
            target=client.file_transfer_mode,
            args=(str(src), "127.0.0.1", transfer_port, 0),
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
        assert wait_until(lambda: dead_leg.fileno() == -1, timeout=10), (
            "the disconnected control session left its paused data socket open"
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


def test_unified_resume_serializes_distinct_offsets_without_local_fid_fallback(tmp_path, monkeypatch):
    """One upload serves queued targets once, preserving paths and endpoint scope."""
    workers = _track_socket_workers(monkeypatch)
    server = _start_server(tmp_path)
    clients = []
    held = threading.Event()
    release = threading.Event()
    try:
        source = _start_client(tmp_path, server, name="queued_source")
        clients.append(source)
        receivers, outputs = [], []
        payload = os.urandom(96 * 1024 + 19)
        src = tmp_path / "queued.bin"
        src.write_bytes(payload)
        source_addr = source.client_socket.getsockname()
        offsets = [12345, 54321]
        for index, offset in enumerate(offsets):
            recv_dir = tmp_path / f"queued_{index}"
            recv_dir.mkdir()
            target = _start_client(tmp_path, server, name=f"queued_{index}", recv_dir=recv_dir)
            clients.append(target)
            receivers.append(target)
            output = recv_dir / "original/nested" / src.name
            output.parent.mkdir(parents=True)
            output.write_bytes(payload[:offset])
            target.file_recv_progress[502 + index * 2] = [
                offset, len(payload), None, str(output), source_addr,
            ]
            outputs.append(output)
        first, second = receivers
        first_addr, second_addr = [target.client_socket.getsockname() for target in receivers]
        assert wait_until(lambda: all(peer in server.clients for peer in (
            source_addr, first_addr, second_addr
        )))
        route = server._register_fid_route(500, source_addr, {first_addr: 502, second_addr: 504})
        key = ((source.host, source.port), 500)
        source.file_upload_progress[key] = [0, len(payload), None, str(src), first_addr]
        local_path = tmp_path / "unrelated.bin"
        local_path.write_bytes(b"unrelated local upload")
        local_row = [0, local_path.stat().st_size, None, str(local_path), first_addr]
        server.file_upload_progress[(first_addr, 502)] = local_row
        original_send = first.send_message
        original_mode = source.file_transfer_mode
        transfers = []
        def hold_ack(sock, message, is_send_raw=False):
            if message == first.server_received_file_data_sign:
                held.set()
                assert release.wait(timeout=10)
            return original_send(sock, message, is_send_raw=is_send_raw)

        def observe_mode(*args, **kwargs):
            transfers.append((kwargs.get("fid"), kwargs.get("resume_offset", 0)))
            return original_mode(*args, **kwargs)

        monkeypatch.setattr(first, "send_message", hold_ack)
        monkeypatch.setattr(source, "file_transfer_mode", observe_mode)
        assert first._request_resumes() == 1
        assert held.wait(timeout=10), "the first target never reached its ACK barrier"
        assert second._request_resumes() == 1
        assert first.send_message(
            first.client_socket, format_resume_line(502, 999, source_addr, first_addr)
        )
        assert wait_until(
            lambda: source._resume_sends.get(key, {}).get("targets") == {first_addr, second_addr}
        )
        assert wait_until(lambda: transfers == [(500, offsets[0])])
        release.set()
        assert wait_until(lambda: key not in source.file_upload_progress, timeout=15)
        assert wait_until(lambda: not source._resume_sends and not server._fid_routes)
        assert transfers == [(500, offset) for offset in offsets]
        assert route["targets"] == {first_addr: 502, second_addr: 504}
        assert route["completed"] == {first_addr, second_addr}
        assert server.file_upload_progress[(first_addr, 502)] == local_row
        assert source._transfer_progress_snapshot()["relays"] == {}
        for target, child, output in zip(receivers, (502, 504), outputs):
            assert wait_until(lambda: child not in target.file_recv_progress)
            assert output.read_bytes() == payload
            assert not (output.parents[2] / src.name).exists()
    finally:
        release.set()
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