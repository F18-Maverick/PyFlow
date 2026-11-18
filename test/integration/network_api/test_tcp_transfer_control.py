"""Typed live control over real ordinary transfer sockets and secure channels."""

import json
import os
import shlex
import socket
import threading
from pathlib import Path

import pytest
from helpers import free_port, isolate_instance_files, request_transfer_listener, server_ready, wait_until

from PyFlow.network_api import connect_tcp
from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base


@pytest.fixture(params=[
    (False, False, True, "127.0.0.1", "localhost"),
    (True, False, True, "127.0.0.1", "127.0.0.1"),
    (True, True, False, "0.0.0.0", "127.0.0.1"),
], ids=["plain-alias", "encrypted", "encrypted-async-untracked-wildcard"])
def channel(request, tmp_path, monkeypatch):
    encrypted, asynchronous, tracked, bind_host, configured_host = request.param
    if encrypted:
        from PyFlow.network_api import rsa_crypto
        try:
            rsa_crypto.load_library()
        except rsa_crypto.CryptoLibraryError:
            pytest.skip("crypto library unavailable")
    options = dict(is_extend_command=True, is_input_command_in_console=False,
                   is_enable_encrypto=encrypted, is_print_log=False,
                   is_enable_file_transfer_progress_bar=tracked)
    server = TCP_Server_Base(host=bind_host, port=free_port(),
                             is_asynic_clients_io=asynchronous, **options)
    client = TCP_Client_Base(host=configured_host, port=server.port,
                             client_host="127.0.0.1", **options)
    workers, releases = [], []
    real_start = threading.Thread.start

    def start(worker, *args, **kwargs):
        result = real_start(worker, *args, **kwargs)
        workers.append(worker)
        return result

    monkeypatch.setattr(threading.Thread, "start", start)
    for name, owner in (("server", server), ("client", client)):
        isolate_instance_files(owner, tmp_path, name)
        destination = tmp_path / name
        destination.mkdir()
        owner.file_transfer_dir = str(destination)
        if encrypted:
            crypto = owner.crypto
            crypto.pvt_key_dir = str(tmp_path / f"{name}_private")
            crypto.pub_key_dir = str(tmp_path / f"{name}_public")
            crypto.ssh_dir = str(tmp_path / "ssh")
            crypto.registry_path = os.path.join(crypto.pub_key_dir, "pub_key.json")
            for directory in (crypto.pvt_key_dir, crypto.pub_key_dir, crypto.ssh_dir):
                os.makedirs(directory, exist_ok=True)
    try:
        threading.Thread(target=server.start_TCP_Server, daemon=True).start()
        assert server_ready(server)
        assert client.connect()
        peer = client.client_socket.getsockname()
        assert wait_until(lambda: peer in server.clients)
        if encrypted:
            assert wait_until(lambda: client.client_socket in client._encrypted_sockets
                              and server.clients[peer]["socket"] in server._encrypted_sockets,
                              timeout=20)
        yield server, client, releases
    finally:
        for release in releases:
            release.set()
        client.close()
        server.stop()
        for worker in workers:
            worker.join(timeout=10)
        alive = [worker.name for worker in workers if worker.is_alive()]
        assert not alive, f"control fixture leaked workers: {alive}"


def _observe_target_writes(monkeypatch, output, byte_count):
    """Observe real writes without assuming buffered bytes are visible to stat."""
    written, received = bytearray(), threading.Event()
    real_open = connect_tcp._open_transfer_target

    def open_target(path, offset):
        target = real_open(path, offset)
        if Path(path) == output:
            real_write = target.write

            def write(data):
                count = real_write(data)
                written.extend(data[:count])
                if len(written) >= byte_count:
                    received.set()
                return count

            monkeypatch.setattr(target, "write", write)
        return target

    monkeypatch.setattr(connect_tcp, "_open_transfer_target", open_target)
    return written, received


def _leg(channel, tmp_path, monkeypatch, server_sends, *, fid=720, offset=0, payload=None):
    server, client, releases = channel
    sender, receiver = (server, client) if server_sends else (client, server)
    peer = client.client_socket.getsockname()
    control_peer = peer if server_sends else (client.host, client.port)
    receiver_peer = (client.host, client.port) if server_sends else peer
    control = server.clients[peer]["socket"] if server_sends else client.client_socket
    source = tmp_path / ("download.bin" if server_sends else "upload.bin")
    payload = os.urandom(128 * 1024 + 13) if payload is None else payload
    source.write_bytes(payload)
    output = Path(receiver.file_transfer_dir) / source.name
    if offset:
        output.write_bytes(payload[:offset])
        origin = client.client_socket.getpeername() if server_sends else peer
        receiver.file_recv_progress[fid] = [offset, len(payload), None, str(output), origin]
    sender_key, receiver_key = (control_peer, fid, 0, 0), (receiver_peer, fid, 0, 1)
    assert sender.pause_trans(fid, control_peer, trans_type=0, direction=0) is False
    port, stamp = request_transfer_listener(sender, receiver, f"/file {shlex.quote(str(source))} {fid}")
    entered, release = threading.Event(), threading.Event()
    releases.append(release)
    real_wait = connect_tcp._wait_transfer_control

    def hold(instance, state, leg_socket):
        if instance is sender and state is not None and state["key"] == sender_key:
            entered.set()
            assert release.wait(timeout=20), "body gate was not released"
        return real_wait(instance, state, leg_socket)

    monkeypatch.setattr(connect_tcp, "_wait_transfer_control", hold)
    if offset == len(payload):
        real_send = receiver.send_message

        def hold_ack(sock, message, is_send_raw=False):
            if message == receiver.server_received_file_data_sign:
                entered.set()
                assert release.wait(timeout=20), "final ACK gate was not released"
            return real_send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(receiver, "send_message", hold_ack)
    result = {}

    def send():
        result["ack"] = sender.file_transfer_mode(
            str(source), "127.0.0.1", port, 0, control_socket=control,
            crypto=stamp, fid=fid, peer=control_peer, resume_offset=offset,
        )

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    assert wait_until(lambda: sender_key in sender._transfer_controls._legs
                      and receiver_key in receiver._transfer_controls._legs, timeout=15)
    states = (sender._transfer_controls._legs[sender_key], receiver._transfer_controls._legs[receiver_key])
    assert wait_until(lambda: all(state["confirmed_state"] for state in states))
    assert entered.wait(timeout=10)
    return sender, receiver, states, release, worker, result, output, payload


def _control(owner, state, paused):
    peer, fid, trans_type, direction = state["key"]
    return (owner.pause_trans if paused else owner.start_trans)(
        fid, peer, trans_type=trans_type, direction=direction,
    )


def _confirmed(states, paused):
    assert wait_until(lambda: all(state["confirmed_state"] is paused for state in states), timeout=10)


def _complete(leg):
    sender, receiver, states, release, worker, result, output, payload = leg
    release.set()
    worker.join(timeout=15)
    assert not worker.is_alive()
    assert result == {"ack": True}, "sender must consume the real final ACK"
    assert output.read_bytes() == payload
    assert wait_until(lambda: all(state["key"] not in owner._transfer_controls._legs
                                  for owner, state in zip((sender, receiver), states)))
    fid = states[0]["key"][1]
    assert (states[0]["key"][0], fid) not in sender.file_upload_progress
    assert fid not in receiver.file_recv_progress
    assert states[0]["key"] not in sender._transfer_pause
    assert states[1]["key"] not in receiver._transfer_pause


@pytest.mark.parametrize("server_sends", [False, True], ids=["upload", "download"])
@pytest.mark.parametrize("receiver_initiates", [False, True], ids=["sender", "receiver"])
def test_pause_start_preserves_the_data_socket_and_final_ack(
    channel, tmp_path, monkeypatch, server_sends, receiver_initiates
):
    leg = _leg(channel, tmp_path, monkeypatch, server_sends)
    sender, receiver, states, release, worker, result, output, payload = leg
    data_key = states[0]["key"][:2]
    data_socket = sender._active_transfer_sockets[data_key]
    owner, state = (receiver, states[1]) if receiver_initiates else (sender, states[0])
    assert _control(owner, state, False) is True
    _confirmed(states, False)
    assert _control(owner, state, False) is False
    assert _control(owner, state, True) is True
    _confirmed(states, True)
    assert _control(owner, state, True) is False
    assert sender._transfer_pause == {states[0]["key"]: True}
    assert not receiver._transfer_pause
    assert sender._active_transfer_sockets[data_key] is data_socket
    assert worker.is_alive() and result == {}
    assert not output.exists() or output.stat().st_size == 0
    rows = sender.get_transfer_progress(include_persisted=False)["memory"]["uploading"]
    if sender.is_enable_file_transfer_progress_bar:
        assert len(rows) == 1 and rows[0]["paused"] is True
    else:
        assert rows == [] and receiver.file_recv_progress == {}
    # Release the observer gate while the actual sender pause remains asserted.
    release.set()
    assert _control(owner, state, False) is True
    _complete(leg)


@pytest.mark.parametrize("server_sends", [False, True], ids=["upload", "download"])
def test_receiver_unconfirmed_reverse_intent_and_cross_commands(
    channel, tmp_path, monkeypatch, server_sends
):
    leg = _leg(channel, tmp_path, monkeypatch, server_sends)
    sender, receiver, states, release, *_ = leg
    assert _control(sender, states[0], False)
    _confirmed(states, False)
    wire_entered, wire_release = threading.Event(), threading.Event()
    channel[2].append(wire_release)
    real_send = receiver.send_message
    pause = f"/pause_trans {states[1]['key'][1]} 0 1"

    def hold_request(sock, message, is_send_raw=False):
        if message == pause:
            wire_entered.set()
            assert wire_release.wait(timeout=10)
        return real_send(sock, message, is_send_raw=is_send_raw)

    monkeypatch.setattr(receiver, "send_message", hold_request)
    assert _control(receiver, states[1], True)
    assert wire_entered.wait(timeout=10)
    assert states[1]["confirmed_state"] is False
    assert states[1]["last_requested_state"] is True
    assert _control(receiver, states[1], False)
    assert states[1]["last_requested_state"] is False
    notifications = []
    sender.add_send_message_listener(lambda _peer, message: notifications.append(message))
    wire_release.set()
    # The last sender notification proves both receiver requests were processed.
    assert wait_until(lambda: f"/start_trans {states[0]['key'][1]} 0 0" in notifications)
    _confirmed(states, False)
    assert _control(sender, states[0], True)
    _confirmed(states, True)
    assert states[1]["last_requested_state"] is False
    assert _control(receiver, states[1], False)
    _confirmed(states, False)
    _complete(leg)


def test_same_fid_opposite_directions_and_invalid_wire_are_isolated(channel, tmp_path, monkeypatch):
    upload = _leg(channel, tmp_path, monkeypatch, False)
    download = _leg(channel, tmp_path, monkeypatch, True)
    server, client, _ = channel
    peer = client.client_socket.getsockname()
    for owner, sock, states in ((client, client.client_socket, download[2]),
                                (server, server.clients[peer]["socket"], upload[2])):
        keys = set(owner._transfer_controls._legs)
        for command in ("/pause_trans 720", "/start_trans 720 0", "/start_trans 720 0 0 extra",
                        "/start_trans 720 2 1", "/start_trans -1 0 1", "/start_trans 720 1 1",
                        "/start_trans 999 0 1"):
            assert owner.send_message(sock, command)
        remote = server if owner is client else client
        barrier = threading.Event()
        marker = "/control_test_barrier"
        remote.add_message_listener(lambda _peer, message: barrier.set() if message == marker else None)
        assert owner.send_message(sock, marker)
        assert barrier.wait(timeout=10)
        assert set(owner._transfer_controls._legs) == keys
        assert all(state["confirmed_state"] for state in states)
    assert _control(upload[0], upload[2][0], False)
    _complete(upload)
    assert all(state["confirmed_state"] for state in download[2])
    assert download[4].is_alive()
    # Both configured aliases and the concrete control endpoint address one key.
    concrete = client.client_socket.getpeername()
    assert client.start_trans(720, concrete, trans_type=0, direction=1)
    _complete(download)


@pytest.mark.parametrize("server_sends", [False, True], ids=["upload", "download"])
@pytest.mark.parametrize("payload", [b"", b"already received"], ids=["empty", "eof"])
def test_empty_and_eof_progress_complete_with_real_ack(channel, tmp_path, monkeypatch, server_sends, payload):
    # EOF resume needs its durable receiver row even when tracking is disabled.
    leg = _leg(channel, tmp_path, monkeypatch, server_sends, offset=len(payload), payload=payload)
    sender, receiver, states, _, worker, result, *_ = leg
    assert worker.is_alive() and result == {}
    if sender.is_enable_file_transfer_progress_bar:
        row = sender.file_upload_progress[states[0]["key"][:2]]
        assert row[:2] == [len(payload), len(payload)]
        assert receiver.file_recv_progress[states[1]["key"][1]][:2] == [len(payload), len(payload)]
    assert _control(sender, states[0], False)
    _confirmed(states, False)
    _complete(leg)


@pytest.mark.parametrize("server_sends", [False, True], ids=["upload", "download"])
def test_receiver_keeps_writing_inflight_data_after_confirmed_pause(
    channel, tmp_path, monkeypatch, server_sends
):
    receiver = channel[1] if server_sends else channel[0]
    output = Path(receiver.file_transfer_dir) / ("download.bin" if server_sends else "upload.bin")
    sender = channel[0] if server_sends else channel[1]
    written, received = _observe_target_writes(monkeypatch, output, sender.file_transfer_chunk_len)
    leg = _leg(channel, tmp_path, monkeypatch, server_sends)
    sender, receiver, states, body_release, _, _, output, payload = leg
    inflight, send_release = threading.Event(), threading.Event()
    channel[2].append(send_release)
    real_send = sender.send_message

    def hold_first_chunk(sock, message, is_send_raw=False):
        if sock is states[0]["owner"] and isinstance(message, bytes) and not inflight.is_set():
            inflight.set()
            assert send_release.wait(timeout=10)
        return real_send(sock, message, is_send_raw=is_send_raw)

    monkeypatch.setattr(sender, "send_message", hold_first_chunk)
    assert _control(sender, states[0], False)
    _confirmed(states, False)
    body_release.set()
    assert inflight.wait(timeout=10)
    assert _control(receiver, states[1], True)
    _confirmed(states, True)
    send_release.set()
    assert received.wait(timeout=10), (
        "receiver pause blocked a chunk already admitted by the sender"
    )
    assert written == payload[:sender.file_transfer_chunk_len]
    assert sender._transfer_pause == {states[0]["key"]: True}
    assert states[1]["key"] not in receiver._transfer_pause
    assert _control(receiver, states[1], False)
    _complete(leg)


def test_receiver_pause_start_waits_for_real_rekey_without_blocking_key_reader(
    channel, tmp_path, monkeypatch
):
    server, client, releases = channel
    if not client.is_enable_encrypto:
        pytest.skip("re-key requires the encrypted fixture variants")
    leg = _leg(channel, tmp_path, monkeypatch, False)
    sender, receiver, states, *_ = leg
    assert _control(sender, states[0], False)
    _confirmed(states, False)
    peer = client.client_socket.getsockname()
    control = server.clients[peer]["socket"]
    channels = (client._channels[client.client_socket], server._channels[control])
    nonce = client._crypto_my_nonce
    key_held, key_release, requested = (threading.Event() for _ in range(3))
    releases.append(key_release)
    real_push = server._crypto_push_pub_to_client
    wire, control_writes, notifications = [], [], []

    def hold_key(sock, address):
        key_held.set()
        assert key_release.wait(timeout=20), "re-key publication was not released"
        return real_push(sock, address)

    monkeypatch.setattr(server, "_crypto_push_pub_to_client", hold_key)
    for secure in channels:
        real_sendall = secure._sendall

        def observe_frame(data, sendall=real_sendall):
            wire.append(bytes(data))
            return sendall(data)

        monkeypatch.setattr(secure, "_sendall", observe_frame)
    for owner in (server, client):
        real_send = owner.send_message

        def observe_control(sock, message, is_send_raw=False, send=real_send):
            if isinstance(message, str) and message.startswith(("/pause_trans ", "/start_trans ")):
                control_writes.append((message, is_send_raw))
            return send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(owner, "send_message", observe_control)
    receiver.add_message_listener(lambda _peer, message: notifications.append(message))
    exchange = threading.Thread(target=client._crypto_exchange_thread,
                                kwargs={"force": True}, daemon=True)
    exchange.start()
    assert key_held.wait(timeout=10)
    assert wait_until(lambda: all(not secure.frames_allowed.is_set() for secure in channels))
    results = []

    def request_both():
        results.extend((_control(receiver, states[1], True), _control(receiver, states[1], False)))
        requested.set()

    threading.Thread(target=request_both, daemon=True).start()
    assert requested.wait(timeout=5), "public controls blocked on re-key readiness"
    assert results == [True, True]
    assert states[1]["confirmed_state"] is False and states[1]["last_requested_state"] is False
    # The reader must still accept the client's real key while control writes wait.
    assert wait_until(lambda: server._crypto_state[peer].get("peer_pub_ok") is True, timeout=15)
    assert not control_writes
    key_release.set()
    exchange.join(timeout=20)
    assert not exchange.is_alive() and client.running
    assert client._crypto_my_nonce != nonce
    assert wait_until(lambda: all(secure.frames_allowed.is_set() for secure in channels), timeout=15)
    assert wait_until(lambda: f"/start_trans {states[0]['key'][1]} 0 0" in notifications)
    _confirmed(states, False)
    assert [line for line, _raw in control_writes if line.endswith(" 0 1")] == [
        f"/pause_trans {states[1]['key'][1]} 0 1", f"/start_trans {states[1]['key'][1]} 0 1",
    ]
    assert control_writes and not any(raw for _line, raw in control_writes)
    assert wire and all(data.startswith(connect_tcp.FRAME_MAGIC) for data in wire)
    assert not any(b"/pause_trans" in data or b"/start_trans" in data for data in wire)
    _complete(leg)


@pytest.fixture
def relay_clients(channel, tmp_path):
    server, source, releases = channel
    targets = []
    try:
        for name in ("target_a", "target_b"):
            target = TCP_Client_Base(
                host=source.host, port=source.port, client_host="127.0.0.1",
                is_extend_command=True, is_input_command_in_console=False,
                is_enable_encrypto=source.is_enable_encrypto, is_print_log=False,
                is_enable_file_transfer_progress_bar=source.is_enable_file_transfer_progress_bar,
            )
            targets.append(target)
            isolate_instance_files(target, tmp_path, name)
            destination = tmp_path / name
            destination.mkdir()
            target.file_transfer_dir = str(destination)
            if target.is_enable_encrypto:
                crypto = target.crypto
                crypto.pvt_key_dir = str(tmp_path / f"{name}_private")
                crypto.pub_key_dir = str(tmp_path / f"{name}_public")
                crypto.ssh_dir = str(tmp_path / "ssh")
                crypto.registry_path = os.path.join(crypto.pub_key_dir, "pub_key.json")
                for directory in (crypto.pvt_key_dir, crypto.pub_key_dir, crypto.ssh_dir):
                    os.makedirs(directory, exist_ok=True)
            assert target.connect()
            peer = target.client_socket.getsockname()
            assert wait_until(lambda: peer in server.clients)
            if target.is_enable_encrypto:
                assert wait_until(lambda: target.client_socket in target._encrypted_sockets
                                  and server.clients[peer]["socket"] in server._encrypted_sockets,
                                  timeout=20)
        yield server, source, targets, releases
    finally:
        for release in releases:
            release.set()
        for target in targets:
            target.close()


def _relay(relay_clients, tmp_path, monkeypatch, *, pressure=False):
    server, source, targets, releases = relay_clients
    addresses = [target.client_socket.getsockname() for target in targets]
    payload = os.urandom(16 * 1024 + 7)
    path = tmp_path / "relay.bin"
    path.write_bytes(payload)
    server.max_mem_buff = 64 if pressure else len(payload) * 4
    server.file_transfer_chunk_len = source.file_transfer_chunk_len = 1024
    entered = [threading.Event() for _ in range(3)]
    gates = [threading.Event() for _ in range(3)]
    releases.extend(gates)
    observed = {"reads": 0, "waits": 0, "controls": [], "frames": []}
    tail = threading.Event()
    releases.append(tail)
    observed["tail"] = tail
    observed["tail_waiting"], observed["pause_waiting"] = threading.Event(), threading.Event()
    waiting = threading.Event()
    real_wait, real_pump = connect_tcp._wait_transfer_control, server._forward_pump
    real_read = server._forward_recv_exact

    def hold(instance, state, sock):
        if instance is source and state is not None and state["key"][2:] == (1, 0):
            entered[0].set()
            assert gates[0].wait(timeout=30), "source body gate was not released"
            if observed.get("source_entered"):
                observed["tail_waiting"].set()
                assert tail.wait(timeout=30), "source tail gate was not released"
            observed["source_entered"] = True
            if state["confirmed_state"]:
                observed["pause_waiting"].set()
        return real_wait(instance, state, sock)

    def pump(up, connections, control, fid, name, relay, offset=0):
        observed["relay"] = relay
        real_condition_wait = relay["cond"].wait

        def pressure_wait(timeout=None):
            if relay["buffer"]["backpressured"]:
                observed["waits"] += 1
                waiting.set()
            return real_condition_wait(timeout)

        monkeypatch.setattr(relay["cond"], "wait", pressure_wait)
        return real_pump(up, connections, control, fid, name, relay, offset)

    def read(sock, count):
        observed["reads"] += 1
        return real_read(sock, count)

    monkeypatch.setattr(connect_tcp, "_wait_transfer_control", hold)
    monkeypatch.setattr(server, "_forward_pump", pump)
    monkeypatch.setattr(server, "_forward_recv_exact", read)
    for owner in (server, source, *targets):
        real_send = owner.send_message

        def send(sock, message, is_send_raw=False, send=real_send, instance=owner):
            if isinstance(message, str) and message.startswith(("/pause_trans ", "/start_trans ")):
                observed["controls"].append((message, is_send_raw))
            if instance is server and isinstance(message, bytes) and "relay" in observed:
                for index, child in enumerate(observed["relay"]["controls"].values(), 1):
                    if sock is child["owner"]:
                        entered[index].set()
                        assert gates[index].wait(timeout=30), "target writer gate was not released"
            return send(sock, message, is_send_raw=is_send_raw)

        monkeypatch.setattr(owner, "send_message", send)
        if owner.is_enable_encrypto:
            for secure in list(owner._channels.values()):
                real_sendall = secure._sendall

                def frame(data, sendall=real_sendall):
                    observed["frames"].append(bytes(data))
                    return sendall(data)

                monkeypatch.setattr(secure, "_sendall", frame)
    result = {}
    worker = threading.Thread(target=lambda: result.update(ack=source._forward_one(
        "file", "", path.name, str(path), addresses)), daemon=True)
    worker.start()
    assert all(event.wait(timeout=20) for event in entered)
    relay = observed["relay"]
    upstream = relay["upstream"]
    sender = source._transfer_controls._legs[((source.host, source.port), relay["fid"], 1, 0)]
    _confirmed((sender, upstream), False)
    return relay, sender, gates, worker, result, payload, observed, waiting


def _relay_done(relay_clients, run, *, failed=False):
    server, source, targets, _ = relay_clients
    relay, sender, gates, worker, result, payload, observed, _ = run
    for gate in gates:
        gate.set()
    observed["tail"].set()
    worker.join(timeout=20)
    assert not worker.is_alive() and result == {"ack": not failed}
    buffer = relay["buffer"]
    assert buffer["complete"] and not buffer["failed"] and buffer["buffered"] == 0
    assert buffer["peak"] <= buffer["high_water"] + buffer["max_enqueue"]
    assert wait_until(lambda: not server._forward_relays)
    for target in targets[1:] if failed else targets:
        assert (Path(target.file_transfer_dir) / "relay.bin").read_bytes() == payload
    assert wait_until(lambda: all(not owner._transfer_controls._legs
                                  and not owner._transfer_pause
                                  for owner in (server, source, *targets)))
    assert observed["controls"] and not any(raw for _, raw in observed["controls"])
    if source.is_enable_encrypto:
        assert observed["frames"] and all(frame.startswith(connect_tcp.FRAME_MAGIC)
                                          for frame in observed["frames"])
        assert not any(b"/pause_trans" in frame or b"/start_trans" in frame
                       for frame in observed["frames"])
    if failed:
        assert server._fid_routes[relay["fid"]] is relay["route"]
    else:
        assert not server._fid_routes and not server._fid_route_index


@pytest.mark.parametrize("issuer", ["source", "server-upstream", "server-alias", "target-a", "target-b"])
def test_relay_global_pause_drains_queued_bytes_and_any_target_can_start(
    relay_clients, tmp_path, monkeypatch, issuer
):
    writes = [_observe_target_writes(monkeypatch, tmp_path / name / "relay.bin", 1024)
              for name in ("target_a", "target_b")]
    run = _relay(relay_clients, tmp_path, monkeypatch)
    server, source, targets, _ = relay_clients
    relay, sender, gates, _, _, payload, observed, _ = run
    children = list(relay["controls"].values())
    receivers = [target._transfer_controls._legs[
        ((source.host, source.port), child["key"][1], 1, 1)]
        for target, child in zip(targets, children)]
    states = [sender, relay["upstream"], *children, *receivers]
    sockets = [state["owner"] for state in states]
    gates[0].set()
    assert observed["tail_waiting"].wait(10)  # Exactly one real source block is in flight.
    if issuer == "server-alias":
        assert server.pause_trans(relay["fid"], children[0]["key"][0], trans_type=1, direction=0)
    else:
        owner, state = {"source": (source, sender), "server-upstream": (server, relay["upstream"]),
                        "target-a": (targets[0], receivers[0]), "target-b": (targets[1], receivers[1])}[issuer]
        assert _control(owner, state, True)
    _confirmed(states, True)
    for owner, command in (
        (source, f"/start_trans {children[0]['key'][1]} 1 1"),
        (targets[1], f"/start_trans {children[0]['key'][1]} 1 1"),
        (targets[0], f"/start_trans {children[0]['key'][1]} 0 1"),
        (targets[0], f"/start_trans {children[0]['key'][1]} 1 0"),
    ):
        processed = threading.Event()
        marker = f"/relay_barrier_{id(processed)}"
        server.add_message_listener(lambda peer, line, marker=marker, event=processed:
                                    event.set() if line == marker else None)
        assert owner.send_message(owner.client_socket, command)
        assert owner.send_message(owner.client_socket, marker)
        assert processed.wait(10)
        assert all(state["confirmed_state"] for state in states)
    gates[1].set()
    gates[2].set()
    for written, received in writes:
        assert received.wait(10)
        assert written == payload[:1024]  # Paused writers must still drain queued bytes.
    observed["tail"].set()
    assert observed["pause_waiting"].wait(10)
    assert all(state["owner"] is sock and sock.fileno() >= 0 for state, sock in zip(states, sockets))
    assert _control(targets[1], receivers[1], False)
    _confirmed(states, False)
    _relay_done(relay_clients, run)


def test_relay_cross_target_start_and_unconfirmed_reverse_intent_reach_source(
    relay_clients, tmp_path, monkeypatch
):
    run = _relay(relay_clients, tmp_path, monkeypatch)
    server, source, targets, releases = relay_clients
    relay, sender = run[:2]
    children = list(relay["controls"].values())
    receivers = [target._transfer_controls._legs[
        ((source.host, source.port), child["key"][1], 1, 1)]
        for target, child in zip(targets, children)]
    entered, release = threading.Event(), threading.Event()
    releases.append(release)
    real_send = source.send_message

    def delay(sock, message, is_send_raw=False):
        if isinstance(message, str) and message.startswith("/pause_trans "):
            entered.set()
            assert release.wait(20)
        return real_send(sock, message, is_send_raw=is_send_raw)

    monkeypatch.setattr(source, "send_message", delay)
    assert _control(targets[0], receivers[0], True)
    assert entered.wait(10)
    assert not receivers[1]["confirmed_state"]
    assert _control(targets[1], receivers[1], False)  # Must not deduplicate an unseen opposite request.
    assert _control(targets[0], receivers[0], False)
    assert wait_until(lambda: not sender["confirmed_state"])
    release.set()
    _confirmed([sender, relay["upstream"], *children, *receivers], False)
    _relay_done(relay_clients, run)


@pytest.mark.parametrize("close_target", [False, True], ids=["drain", "target-close"])
def test_relay_pressure_preserves_user_intent_and_source_start_cannot_open_reader(
    relay_clients, tmp_path, monkeypatch, close_target
):
    run = _relay(relay_clients, tmp_path, monkeypatch, pressure=True)
    server, source, targets, _ = relay_clients
    relay, sender, gates, _, _, _, observed, waiting = run
    upstream, buffer = relay["upstream"], relay["buffer"]
    sockets = [state["owner"] for state in relay["controls"].values()]
    gates[0].set()
    assert waiting.wait(timeout=10)
    _confirmed((sender, upstream), False)
    assert buffer["backpressured"]
    assert _control(server, upstream, True)
    _confirmed((sender, upstream), True)
    assert _control(source, sender, False)
    _confirmed((sender, upstream), False)
    with relay["cond"]:
        reads = observed["reads"]
        waiting.clear()
        relay["cond"].notify_all()
    assert waiting.wait(timeout=10), "reader did not re-enter its pressure gate"
    assert observed["reads"] == reads and buffer["backpressured"]
    assert _control(source, sender, True)
    _confirmed((sender, upstream), True)
    assert _control(server, upstream, True)
    if close_target:
        targets[0].close()
    gates[1].set()
    gates[2].set()
    assert wait_until(lambda: not buffer["backpressured"], timeout=10)
    assert sender["confirmed_state"] and upstream["confirmed_state"]
    assert buffer["peak"] <= buffer["high_water"] + buffer["max_enqueue"]
    assert buffer["peak"] > buffer["high_water"]
    assert all(state["owner"] is sock for state, sock in zip(relay["controls"].values(), sockets))
    assert _control(server, upstream, False)
    _relay_done(relay_clients, run, failed=close_target)
    if close_target:
        route = relay["route"]
        assert route["completed"] == {targets[1].client_socket.getsockname()}
        assert set(route["targets"]) - route["completed"]


@pytest.mark.parametrize("server_sends", [False, True], ids=["upload", "download"])
def test_online_resume_reserves_one_owner_and_uses_receiver_disk_offset(
    channel, tmp_path, monkeypatch, server_sends
):
    other = _leg(channel, tmp_path, monkeypatch, not server_sends)
    receiver = channel[1] if server_sends else channel[0]
    output = Path(receiver.file_transfer_dir) / ("download.bin" if server_sends else "upload.bin")
    sender = channel[0] if server_sends else channel[1]
    written, received = _observe_target_writes(monkeypatch, output, sender.file_transfer_chunk_len)
    leg = _leg(channel, tmp_path, monkeypatch, server_sends, offset=1)
    sender, receiver, states, body_release, worker, result, output, payload = leg
    peer, fid = key = states[0]["key"][:2]
    old_socket, control = states[0]["owner"], states[0]["control_socket"]
    old_other_socket = other[2][0]["owner"]
    # Retained rows remain resumable with tracking off; no new tracked leg is invented.
    old_progress = sender.file_upload_progress.setdefault(
        key, [1, len(payload), None, str(tmp_path / output.name), peer],
    )
    partial, interrupt, cleaning, cleaned, negotiating, negotiated, listening, announced, resumed, finish = (
        threading.Event() for _ in range(10)
    )
    channel[2].extend((interrupt, cleaned, negotiated, announced, finish))
    real_wait, real_release = connect_tcp._wait_transfer_control, sender._release_transfer_socket

    def hold_body(instance, state, sock):
        if instance is sender and state is not None and state["key"] == states[0]["key"]:
            entered, release = (partial, interrupt) if state is states[0] else (resumed, finish)
            entered.set()
            assert release.wait(timeout=20)
        return real_wait(instance, state, sock)

    def hold_cleanup(transfer_key, sock):
        if transfer_key == key and sock is old_socket:
            cleaning.set()
            assert cleaned.wait(timeout=20)
        return real_release(transfer_key, sock)

    monkeypatch.setattr(connect_tcp, "_wait_transfer_control", hold_body)
    monkeypatch.setattr(sender, "_release_transfer_socket", hold_cleanup)
    assert sender.resume_trans(peer, fid, 0) is False  # paused live owner
    assert _control(sender, states[0], False)
    _confirmed(states, False)
    assert sender.resume_trans(peer, fid, 0) is False  # running, before first body write
    body_release.set()
    assert partial.wait(timeout=10)  # the second wait follows one real chunk
    disk = 1 + sender.file_transfer_chunk_len
    assert received.wait(timeout=10)
    assert written == payload[1:disk]
    assert _control(sender, states[0], True)
    _confirmed(states, True)
    old_socket.shutdown(socket.SHUT_RDWR)  # keep the existing control session
    interrupt.set()
    assert cleaning.wait(timeout=10)
    assert old_socket.fileno() < 0 and sender._active_transfer_sockets[key] is old_socket
    assert sender.resume_trans(peer, fid, disk) is False  # closed, but old finally still owns it
    cleaned.set()
    worker.join(timeout=10)
    assert not worker.is_alive() and result == {"ack": False}
    assert wait_until(lambda: fid not in receiver._active_transfer_sockets
                      and fid not in receiver._receive_transfers)
    assert sender.file_upload_progress[key] is old_progress
    assert output.read_bytes() == payload[:disk]
    old_progress[0] = len(payload) - 1  # sender bookkeeping cannot choose the receiver's seek
    receiver.file_recv_progress[fid][0] = 1
    requests, calls, resumed_result = [], [], {}
    real_request = connect_tcp._send_transfer_request
    real_send, real_mode = receiver.send_message, sender.file_transfer_mode

    def hold_request(instance, record, command):
        if instance is sender and record["key"] == (control, fid):
            requests.append(command)
            negotiating.set()
            assert negotiated.wait(timeout=20)
        return real_request(instance, record, command)

    def hold_listener(sock, message, is_send_raw=False):
        port = connect_tcp._parse_transfer_port_announcement(message) if isinstance(message, str) else None
        if port is not None and port[1] == fid:
            listening.set()
            assert announced.wait(timeout=20)
        return real_send(sock, message, is_send_raw=is_send_raw)

    def observe_mode(*args, **kwargs):
        calls.append((kwargs["fid"], kwargs["resume_offset"], threading.current_thread()))
        resumed_result["ack"] = real_mode(*args, **kwargs)
        return resumed_result["ack"]

    monkeypatch.setattr(connect_tcp, "_send_transfer_request", hold_request)
    monkeypatch.setattr(receiver, "send_message", hold_listener)
    monkeypatch.setattr(sender, "file_transfer_mode", observe_mode)
    barrier, accepted = threading.Barrier(3), []

    def resume():
        barrier.wait(timeout=10)
        accepted.append(sender.resume_trans(peer, fid, disk))

    contenders = [threading.Thread(target=resume, daemon=True) for _ in range(2)]
    for contender in contenders:
        contender.start()
    barrier.wait(timeout=10)
    for contender in contenders:
        contender.join(timeout=10)
        assert not contender.is_alive()
    assert sorted(accepted) == [False, True]
    assert negotiating.wait(timeout=10)
    assert sender._transfer_handshakes.occupied(control, fid)
    assert sender.resume_trans(peer, fid, disk) is False
    request_resumes = (lambda: receiver._request_resumes(states[1]["key"][0])) if receiver is channel[0] else receiver._request_resumes
    assert request_resumes() == 1
    assert receiver.file_recv_progress[fid][0] == disk
    assert request_resumes() == 0  # pending receiver request
    negotiated.set()
    assert listening.wait(timeout=10)
    assert request_resumes() == 0  # existing listener, before its real announcement
    assert sender.resume_trans(peer, fid, disk) is False
    announced.set()
    assert resumed.wait(timeout=15)
    assert wait_until(lambda: all(state["key"] in owner._transfer_controls._legs
                                 for owner, state in zip((sender, receiver), states)))
    new_states = tuple(owner._transfer_controls._legs[state["key"]]
                       for owner, state in zip((sender, receiver), states))
    _confirmed(new_states, False)  # the interrupted owner's pause was discarded
    assert all(new["owner"] is not old["owner"] and new["control_socket"] is old["control_socket"]
               for new, old in zip(new_states, states))
    assert sender._active_transfer_sockets[key] is new_states[0]["owner"]
    assert len(calls) == 1 and calls[0][:2] == (fid, disk)
    assert sender.resume_trans(peer, fid, disk) is False
    assert _control(sender, new_states[0], True)
    _confirmed(new_states, True)
    assert sender.resume_trans(peer, fid, disk) is False
    assert request_resumes() == 0
    assert len(requests) == 1 and shlex.split(requests[0])[-1] == str(fid)
    assert all(state["confirmed_state"] for state in other[2])
    assert other[0]._active_transfer_sockets[other[2][0]["key"][:2]] is old_other_socket
    assert _control(sender, new_states[0], False)
    _complete((sender, receiver, new_states, finish, calls[0][2], resumed_result, output, payload))
    assert wait_until(lambda: key not in sender._resume_sends
                      and key not in sender._active_transfer_sockets
                      and fid not in receiver._receive_transfers)
    assert len(requests) == len(calls) == 1
    assert _control(other[0], other[2][0], False)
    _complete(other)


@pytest.mark.parametrize("server_sends", [False, True], ids=["upload", "download"])
def test_control_disconnect_releases_paused_ordinary_workers(channel, tmp_path, monkeypatch, server_sends):
    leg = _leg(channel, tmp_path, monkeypatch, server_sends)
    sender, receiver, states, release, worker, result, *_ = leg
    server, client, _ = channel
    peer = client.client_socket.getsockname()
    client.client_socket.shutdown(socket.SHUT_RDWR)
    assert wait_until(lambda: not client.running and peer not in server.clients)
    assert wait_until(lambda: all(state["key"] not in owner._transfer_controls._legs
                                 for owner, state in zip((sender, receiver), states)))
    release.set()
    worker.join(timeout=15)
    assert not worker.is_alive() and result == {"ack": False}
    assert wait_until(lambda: all(not owner._active_transfer_sockets and not owner._receive_transfers
                                 and not owner._transfer_controls._legs and not owner._resume_sends
                                 and not owner._transfer_pause for owner in (sender, receiver)))
    assert all(state["owner"].fileno() < 0 and not state["confirmed_state"]
               and not state["last_requested_state"] for state in states)


def test_disconnected_relay_target_cannot_queue_behind_live_source(relay_clients, tmp_path, monkeypatch):
    run = _relay(relay_clients, tmp_path, monkeypatch)
    server, source, targets, _ = relay_clients
    relay, sender, gates, worker, result, payload, _, _ = run
    fid, route = relay["fid"], relay["route"]
    origin = source.client_socket.getsockname()
    first, second = [target.client_socket.getsockname() for target in targets]
    control = server.clients[first]["socket"]
    sockets = [state["owner"] for state in (sender, relay["upstream"], *relay["controls"].values())]
    source.file_upload_progress.setdefault(sender["key"][:2],
        [0, len(payload), None, str(tmp_path / "relay.bin"), first])
    assert _control(source, sender, True)
    _confirmed((sender, relay["upstream"]), True)
    assert source.resume_trans(second, fid, 0) is False
    targets[0].close()
    assert wait_until(lambda: first not in server.clients and relay["controls"][first]["owner"].fileno() < 0)
    assert server._resume_request(control, first, route["targets"][first], 0, origin, first) is False
    assert server._queue_resume_to_originator(fid, route, first, 1) is False
    assert source.resume_trans(first, fid, 0) is False
    assert not server._pending_originator_resumes
    assert server._receive_transfers[fid]["owner"] is relay
    assert sender["owner"] is sockets[0] and sockets[0].fileno() >= 0
    assert relay["controls"][second]["owner"] is sockets[-1] and sockets[-1].fileno() >= 0
    assert worker.is_alive() and result == {} and not source._resume_sends.get(sender["key"][:2], {}).get("targets")
    assert _control(source, sender, False)
    _relay_done(relay_clients, run, failed=True)
    assert all(sock.fileno() < 0 for sock in sockets)
    assert wait_until(lambda: all(not owner._receive_transfers and not owner._active_transfer_sockets
                                  and not owner._resume_sends for owner in (server, source, *targets)))


@pytest.mark.parametrize("fault", ["source", "stop"])
def test_relay_offline_source_replays_stable_route_or_stops_cleanly(
    relay_clients, tmp_path, monkeypatch, fault
):
    workers, real_start = [], threading.Thread.start

    def start(worker, *args, **kwargs):
        if getattr(getattr(worker, "_target", None), "__name__", "") in {
            "_forward_relay", "writer", "file_transfer_client_recv",
            "file_transfer_client_recv_server_start", "receive_file_transfer_messages", "_flush",
        }:
            workers.append(worker)
        return real_start(worker, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", start)
    run = _relay(relay_clients, tmp_path, monkeypatch)
    server, source, targets, releases = relay_clients
    relay, sender, gates, worker, result, payload, observed, _ = run
    fid, route = relay["fid"], relay["route"]
    origin = source.client_socket.getsockname()
    first, second = [target.client_socket.getsockname() for target in targets]
    child = route["targets"][first]
    output = Path(targets[0].file_transfer_dir) / "relay.bin"
    source.file_upload_progress.setdefault(sender["key"][:2],
        [0, len(payload), None, str(tmp_path / "relay.bin"), first])
    partial, interrupt, resumed, finish = (threading.Event() for _ in range(4))
    releases.extend((interrupt, finish))
    real_wait, real_send = connect_tcp._wait_transfer_control, server.send_message
    real_progress = targets[0]._file_progress_start

    def retain_progress(store, transfer_id, size, path, peer=None, initial=0):
        return real_progress(store, transfer_id, size, path, peer, initial) or store.setdefault(
            transfer_id, [initial, size, None, str(path), peer])

    monkeypatch.setattr(targets[0], "_file_progress_start", retain_progress)

    def hold_payload(sock, message, is_send_raw=False):
        written = real_send(sock, message, is_send_raw=is_send_raw)
        if sock is relay["controls"][first]["owner"] and message == payload[:1024]:
            partial.set()
            assert interrupt.wait(timeout=30)
        return written

    monkeypatch.setattr(server, "send_message", hold_payload)

    def hold(instance, state, sock):
        if instance is source and state is not None and state["key"] == sender["key"] and state is not sender:
            resumed.set()
            assert finish.wait(timeout=30)
        return real_wait(instance, state, sock)

    monkeypatch.setattr(connect_tcp, "_wait_transfer_control", hold)
    observed["tail"].set()
    for gate in gates:
        gate.set()
    assert partial.wait(timeout=15)
    assert wait_until(lambda: targets[0].file_recv_progress.get(child, [None])[0] == 1024)
    assert wait_until(lambda: second in route["completed"], timeout=15)
    assert (Path(targets[1].file_transfer_dir) / "relay.bin").read_bytes() == payload
    receiver = targets[0]._transfer_controls._legs[((source.host, source.port), child, 1, 1)]
    assert _control(targets[0], receiver, True)
    _confirmed((receiver, relay["controls"][first]), True)
    old_states = [(owner, state) for owner in (server, source, *targets)
                  for state in owner._transfer_controls._legs.values()]
    old_receive = server._receive_transfers[fid]
    old_target_receive = targets[0]._receive_transfers[child]
    snapshot = server._transfer_progress_snapshot()["relays"]
    old_client, old_server = source.client_socket, server.clients[origin]["socket"]
    if fault == "stop":
        server.stop()
    else:
        # Peer FIN leaves source passive-close without invalidating the socket
        # owned by the server's pending recv (including asyncio/Proactor reads).
        old_server.shutdown(socket.SHUT_WR)
        assert wait_until(lambda: not source.running)
        source.receive_thread.join(timeout=10)
        assert not source.receive_thread.is_alive()
        source._release_client_socket()
        assert wait_until(lambda: origin not in server.clients)
    interrupt.set()
    worker.join(timeout=20)
    for old_worker in tuple(workers):
        old_worker.join(timeout=10)
        assert not old_worker.is_alive()
    assert not worker.is_alive() and result == {"ack": False}
    assert all(state["owner"].fileno() < 0 and not state["confirmed_state"] for _, state in old_states)
    assert wait_until(lambda: not server._forward_relays and all(
        not owner._transfer_controls._legs and not owner._transfer_pause
        and not owner._active_transfer_sockets and not owner._receive_transfers
        and not owner._resume_sends for owner in (server, source, *targets)))
    assert server._transfer_progress_snapshot()["relays"] == snapshot
    if fault == "stop":
        assert json.loads(Path(server.file_trans_progress_file).read_text())["relays"] == snapshot
        assert server.delete_config(Path(server.file_trans_progress_file).name)
        assert not Path(server.file_trans_progress_file).exists()
        assert server._transfer_progress_snapshot()["relays"] == snapshot
        server.stop()
        assert not Path(server.file_trans_progress_file).exists()
        return
    assert output.read_bytes() == payload[:1024]
    request_control = server.clients[first]["socket"]
    assert server._resume_request(request_control, first, child, 0, origin, first) is False
    assert server._resume_request(request_control, first, child, 1024, origin, first) is False
    assert server._pending_originator_resumes == {origin: {(fid, first): 1024}}
    calls, real_forward = [], source._forward_one

    def forward(*args, **kwargs):
        ack = real_forward(*args, **kwargs)
        calls.append((kwargs["upstream_fid"], kwargs["resume_offset"], ack))
        return ack

    monkeypatch.setattr(source, "_forward_one", forward)
    source.client_port = origin[1]
    assert source.connect() and source.client_socket.getsockname() == origin
    assert resumed.wait(timeout=20)
    replacement = observed["relay"]
    assert replacement is not relay and replacement["route"] is route
    assert replacement["fid"] == fid and replacement["controls"][first]["key"][1] == child
    assert set(replacement["controls"]) == {first} and route["completed"] == {second}
    new_sender = source._transfer_controls._legs[sender["key"]]
    assert new_sender["owner"] is not sender["owner"] and new_sender["control_socket"] is source.client_socket
    _confirmed((new_sender, replacement["upstream"], replacement["controls"][first]), False)
    assert source._transfer_controls.release(sender) is False
    source._transfer_controls.discard_socket(old_client)
    server._transfer_controls.discard_socket(old_server)
    connect_tcp._release_receive(server, fid, old_receive)
    connect_tcp._release_receive(targets[0], child, old_target_receive)
    assert server._receive_transfers[fid]["owner"] is replacement
    assert child in targets[0]._receive_transfers and source._transfer_controls._legs[sender["key"]] is new_sender
    assert server._flush_pending_originator_resumes(origin) == 0
    assert source.resume_trans(first, fid, 1024) is False
    finish.set()
    assert wait_until(lambda: calls == [(fid, 1024, True)], timeout=20)
    assert output.read_bytes() == payload and not server._fid_routes and not server._fid_route_index
    assert wait_until(lambda: not server._forward_relays and all(
        not owner._transfer_controls._legs and not owner._transfer_pause
        and not owner._active_transfer_sockets and not owner._receive_transfers
        and not owner._resume_sends for owner in (server, source, *targets)))
    assert not server._pending_originator_resumes


def test_reconnect_keeps_delayed_forward_resume_on_its_old_session(channel, tmp_path, monkeypatch):
    server, client, releases = channel
    source = tmp_path / "old-session.bin"
    source.write_bytes(b"retained forward source")
    fid, target = 734, ("127.0.0.1", free_port())
    key = ((client.host, client.port), fid)
    row = [0, source.stat().st_size, None, str(source), target]
    client.file_upload_progress[key] = row
    old_control = client.client_socket
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    releases.append(release)
    original, result = client._forward_one, {}

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(20), "old worker was not released"
        result["socket"] = kwargs.get("control_socket")
        result["sent"] = original(*args, **kwargs)
        done.set()

    monkeypatch.setattr(client, "_forward_one", delayed)
    assert client.resume_trans(target, fid, 0)
    assert entered.wait(10)
    assert client.connect(), "new control reader must not wait for the old worker"
    new_control = client.client_socket
    assert new_control is not old_control
    release.set()
    assert done.wait(10)
    assert result == {"socket": old_control, "sent": False}
    assert wait_until(lambda: not client._resume_sends)
    assert client.file_upload_progress[key] is row
    assert fid not in server._fid_routes
    peer = new_control.getsockname()
    assert wait_until(lambda: peer in server.clients)
    assert client._wait_control_secure(new_control)
    assert server._wait_control_secure(server.clients[peer]["socket"])
    # A delayed reconnect task must not sweep or adopt the current session.
    server._resume_after_connect(peer, old_control, ())
    received = threading.Event()
    client.add_message_listener(lambda sender, message: received.set()
                                if message == "session-still-live" else None)
    assert server.send_message(server.clients[peer]["socket"], "session-still-live")
    assert received.wait(10)
    assert client.client_socket is new_control and new_control.fileno() >= 0
