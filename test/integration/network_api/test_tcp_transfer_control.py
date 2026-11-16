"""Typed live control over real ordinary transfer sockets and secure channels."""

import os
import shlex
import threading
from pathlib import Path

import pytest
from helpers import free_port, isolate_instance_files, request_transfer_listener, server_ready, wait_until

from PyFlow.network_api import connect_tcp
from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base


@pytest.fixture(params=[
    (False, False, True, "127.0.0.1", "localhost"),
    (True, False, True, "127.0.0.1", "127.0.0.1"),
    (True, True, False, "0.0.0.0", "0.0.0.0"),
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
        assert not any(worker.is_alive() for worker in workers)


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
    leg = _leg(channel, tmp_path, monkeypatch, server_sends)
    sender, receiver, states, body_release, _, _, output, _ = leg
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
    assert wait_until(lambda: output.exists() and output.stat().st_size > 0), (
        "receiver pause blocked a chunk already admitted by the sender"
    )
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
    waiting = threading.Event()
    real_wait, real_pump = connect_tcp._wait_transfer_control, server._forward_pump
    real_read = server._forward_recv_exact

    def hold(instance, state, sock):
        if state is not None and state["key"][2:] == (1, 0):
            index = 0 if instance is source else (
                1 + addresses.index(state["key"][0]) if instance is server else None)
            if index is not None:
                entered[index].set()
                assert gates[index].wait(timeout=30), "relay body gate was not released"
                if index == 0 and pressure and observed.get("source_entered"):
                    assert tail.wait(timeout=30), "source tail gate was not released"
                if index == 0:
                    observed["source_entered"] = True
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

        def send(sock, message, is_send_raw=False, send=real_send):
            if isinstance(message, str) and message.startswith(("/pause_trans ", "/start_trans ")):
                observed["controls"].append((message, is_send_raw))
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


def test_relay_child_and_upstream_alias_control_only_the_selected_writer(
    relay_clients, tmp_path, monkeypatch
):
    run = _relay(relay_clients, tmp_path, monkeypatch)
    server, source, targets, _ = relay_clients
    relay, sender, gates, _, result, _, _, _ = run
    first, second = [relay["controls"][target.client_socket.getsockname()] for target in targets]
    receiver = targets[0]._transfer_controls._legs[
        ((source.host, source.port), first["key"][1], 1, 1)]
    data_socket = first["owner"]
    assert _control(targets[0], receiver, True)
    _confirmed((first, receiver), True)
    assert server.start_trans(relay["fid"], first["key"][0], trans_type=1, direction=0)
    _confirmed((first, receiver), False)
    assert server.pause_trans(relay["fid"], first["key"][0], trans_type=1, direction=0)
    _confirmed((first, receiver), True)
    for owner, command in (
        (source, f"/start_trans {first['key'][1]} 1 1"),
        (targets[1], f"/start_trans {first['key'][1]} 1 1"),
        (targets[0], f"/start_trans {first['key'][1]} 0 1"),
        (targets[0], f"/start_trans {first['key'][1]} 1 0"),
    ):
        barrier = threading.Event()
        marker = f"/relay_barrier_{id(barrier)}"
        server.add_message_listener(lambda _peer, line, marker=marker, event=barrier:
                                    event.set() if line == marker else None)
        assert owner.send_message(owner.client_socket, command)
        assert owner.send_message(owner.client_socket, marker)
        assert barrier.wait(timeout=10)
        assert first["confirmed_state"] and not second["confirmed_state"]
        assert not sender["confirmed_state"] and not relay["upstream"]["confirmed_state"]
    gates[0].set()
    gates[2].set()
    second_peer = targets[1].client_socket.getsockname()
    assert wait_until(lambda: second_peer in relay["route"]["completed"], timeout=15)
    assert (Path(targets[1].file_transfer_dir) / "relay.bin").read_bytes() == run[5]
    assert result == {} and first["owner"] is data_socket and data_socket.fileno() >= 0
    assert _control(targets[0], receiver, False)
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
    _confirmed((sender, upstream), True)
    assert buffer["backpressured"] and upstream["backpressure"]
    assert _control(server, upstream, True) is False
    assert upstream["user_paused"]
    assert _control(source, sender, False)
    _confirmed((sender, upstream), False)
    assert _control(server, upstream, False)
    assert not upstream["user_paused"] and upstream["last_requested_state"]
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
    assert _control(server, upstream, True) is False
    if close_target:
        targets[0].close()
    gates[1].set()
    gates[2].set()
    assert wait_until(lambda: not buffer["backpressured"], timeout=10)
    assert upstream["user_paused"] and not upstream["backpressure"]
    assert upstream["last_requested_state"] and sender["confirmed_state"]
    assert buffer["peak"] <= buffer["high_water"] + buffer["max_enqueue"]
    assert buffer["peak"] > buffer["high_water"]
    assert all(state["owner"] is sock for state, sock in zip(relay["controls"].values(), sockets))
    assert _control(server, upstream, False)
    _relay_done(relay_clients, run, failed=close_target)
    if close_target:
        route = relay["route"]
        assert route["completed"] == {targets[1].client_socket.getsockname()}
        assert set(route["targets"]) - route["completed"]
