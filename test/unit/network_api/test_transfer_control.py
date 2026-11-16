"""Contracts for typed transfer control and connection-scoped endpoint identity."""

import json
import socket
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from itertools import product

import pytest

from PyFlow.network_api.connect_tcp import (
    TCP_Client_Base,
    TCP_Server_Base,
    _TransferControlRegistry,
    _control_key,
    _is_control_endpoint,
    _parse_transfer_control_command,
    _socket_local_endpoint,
    _socket_peer_endpoint,
    _transfer_control_key,
)

PEER = ("127.0.0.1", 6000)


@pytest.mark.parametrize("name,paused", [("pause_trans", True), ("start_trans", False)])
@pytest.mark.parametrize("fid", [0, (1 << 63) + 42])
@pytest.mark.parametrize("trans_type", [0, 1])
@pytest.mark.parametrize("direction", [0, 1])
def test_control_parser_preserves_leg_identity(name, paused, fid, trans_type, direction):
    parsed = _parse_transfer_control_command(
        f"/{name.upper()} {fid} {trans_type} {direction}"
    )
    assert parsed == (paused, fid, trans_type, direction)
    # A received direction names the issuer's role, so the local leg is opposite.
    _, parsed_fid, parsed_type, wire_direction = parsed
    key = _transfer_control_key(PEER, parsed_fid, parsed_type, 1 - wire_direction)
    assert key == (PEER, fid, trans_type, 1 - direction)


@pytest.mark.parametrize("line", [
    "", " ", "/pause_trans", "/pause_trans 42", "/start_trans 42 0",
    "/pause_trans 42 0 1 extra", "/resume_trans 42 0 1",
    "/pause_trans -1 0 0", "/pause_trans nope 0 0", "/pause_trans 1.5 0 0",
    "/pause_trans 42 -1 0", "/pause_trans 42 2 0", "/pause_trans 42 normal 0",
    "/start_trans 42 0 -1", "/start_trans 42 0 2", "/start_trans 42 0 sender",
    "/pause_trans 42 0 'unterminated", None, b"/pause_trans 42 0 0",
])
def test_control_parser_rejects_old_or_malformed_commands(line):
    assert _parse_transfer_control_command(line) is None


@pytest.mark.parametrize("fid,trans_type,direction", [
    (-1, 0, 0), (True, 0, 0), (1.0, 0, 0), ("1", 0, 0), (None, 0, 0),
    (42, -1, 0), (42, 2, 0), (42, False, 0), (42, 0.0, 0), (42, "0", 0),
    (42, 0, -1), (42, 0, 2), (42, 0, True), (42, 0, 1.0), (42, 0, "1"),
])
def test_local_control_identity_rejects_coercion_and_out_of_range_values(
    fid, trans_type, direction
):
    with pytest.raises(ValueError):
        _transfer_control_key(PEER, fid, trans_type, direction)


def test_control_keys_isolate_peer_fid_type_and_both_local_roles():
    legs = {
        _transfer_control_key(peer, fid, trans_type, direction): False
        for peer in (PEER, ("127.0.0.2", PEER[1]))
        for fid in (42, 43)
        for trans_type in (0, 1)
        for direction in (0, 1)
    }
    mutable_peer = list(PEER)
    selected = _transfer_control_key(mutable_peer, 42, 1, 0)
    mutable_peer[0] = "other-host"
    legs[selected] = True
    assert legs[(PEER, 42, 1, 0)] is True
    assert all(not paused for key, paused in legs.items() if key != selected)
    assert legs[(PEER, 42, 0, 0)] is False
    assert legs[(PEER, 42, 1, 1)] is False
    assert legs[(("127.0.0.2", PEER[1]), 42, 1, 0)] is False
    assert legs[(PEER, 43, 1, 0)] is False


@pytest.mark.parametrize("bind_host,configured_host", [
    ("127.0.0.1", "localhost"), ("0.0.0.0", "0.0.0.0"),
], ids=["configured-alias", "wildcard-bind"])
def test_control_endpoint_matches_only_this_connection_and_configuration(
    bind_host, configured_host
):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.settimeout(2)
        listener.bind((bind_host, 0))
        listener.listen(1)
        configured = (configured_host, listener.getsockname()[1])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(("127.0.0.1", configured[1]))
            accepted, _ = listener.accept()
            with accepted:
                local = _socket_local_endpoint(accepted)
                remote = _socket_peer_endpoint(client)
                assert local == remote == ("127.0.0.1", configured[1])
                for connected in (local, remote):
                    assert _is_control_endpoint(list(configured), configured, connected)
                    assert _is_control_endpoint(local, configured, connected)
                    assert not _is_control_endpoint(
                        ("127.0.0.2", configured[1]), configured, connected
                    )
                    assert not _is_control_endpoint(
                        ("127.0.0.1", configured[1] + 1), configured, connected
                    )
            client.close()
            assert _socket_local_endpoint(accepted) is None
            assert _socket_peer_endpoint(client) is None
            assert _is_control_endpoint(configured, configured, None)
            assert not _is_control_endpoint(local, configured, None)


class _ControlPair:
    def __init__(self):
        self.paused = [{}, {}]
        self.registries = [
            _TransferControlRegistry(flags, threading.Condition()) for flags in self.paused
        ]
        self.keys = [_transfer_control_key(PEER, 42, 0, role) for role in (0, 1)]
        self.sockets = [object(), object()]
        self.states = [registry.register(key, sock, object()) for registry, key, sock in zip(
            self.registries, self.keys, self.sockets
        )]
        self.pending = [deque(), deque()]

    def send(self, side):
        def enqueue(sock, line):
            assert sock is self.sockets[side]
            self.pending[1 - side].append(line)
            return True
        return enqueue

    def local(self, side, paused):
        return self.registries[side].local(self.keys[side], paused, self.send(side))

    def deliver(self, side):
        paused, fid, trans_type, direction = _parse_transfer_control_command(
            self.pending[side].popleft()
        )
        key = _transfer_control_key(PEER, fid, trans_type, 1 - direction)
        return self.registries[side].receive(key, self.sockets[side], paused, self.send(side))

    def drain(self):
        while any(self.pending):
            for side in (0, 1):
                if self.pending[side]:
                    self.deliver(side)


def test_duplicate_requests_still_confirm_and_receiver_preserves_its_last_intent():
    pair = _ControlPair()
    assert not pair.local(0, False) and not pair.local(1, False)
    assert pair.local(1, True) and pair.local(1, True)
    assert pair.states[1]["confirmed_state"] is False
    pair.deliver(0)
    pair.deliver(0)
    assert tuple(pair.pending[1]) == ("/pause_trans 42 0 0",) * 2
    pair.drain()
    assert pair.states[0]["confirmed_state"] is pair.states[1]["confirmed_state"] is True
    assert not pair.local(0, True) and not pair.local(1, True)
    assert pair.local(0, False)
    pair.drain()
    assert pair.states[1]["last_requested_state"] is True
    assert pair.local(1, True)  # The sender's start must not suppress another pause.
    assert pair.local(1, False)  # An unconfirmed pause must not swallow the reverse intent.
    pair.drain()
    assert pair.states[0]["confirmed_state"] is pair.states[1]["confirmed_state"] is False
    assert pair.paused == [{}, {}] and not any(pair.pending)


@pytest.mark.parametrize("operations", tuple(product(product((0, 1), (False, True)), repeat=3)))
def test_every_fifo_delivery_interleaving_converges_after_finite_input(operations):
    def explore(schedule=()):
        pair = _ControlPair()
        submitted = 0
        expected = False
        for action in schedule:
            if action == "local":
                if operations[submitted][0] == 0:
                    expected = operations[submitted][1]
                pair.local(*operations[submitted])
                submitted += 1
            else:
                if action == 0:
                    expected = _parse_transfer_control_command(pair.pending[0][0])[0]
                pair.deliver(action)
        actions = (["local"] if submitted < len(operations) else []) + [
            side for side in (0, 1) if pair.pending[side]
        ]
        if not actions:
            sender, receiver = pair.states
            assert sender["confirmed_state"] == receiver["confirmed_state"], schedule
            assert sender["confirmed_state"] == expected, schedule
            assert pair.paused[0].get(pair.keys[0], False) == sender["confirmed_state"]
            assert pair.paused[1] == {}
            return
        assert len(schedule) <= 3 * len(operations), schedule  # No notification loop.
        for action in actions:
            explore((*schedule, action))
    explore()


def test_preset_pause_is_adopted_once_and_start_never_leaves_a_false_entry():
    pair = _ControlPair()
    registry, key, sock = pair.registries[0], pair.keys[0], pair.sockets[0]
    assert registry.release(pair.states[0])
    assert not registry.local(key, True, pair.send(0))
    assert not registry.local(pair.keys[1], True, pair.send(0))
    assert pair.paused[0] == {key: True} and not any(pair.pending)
    state = registry.register(key, sock, object())
    assert state["confirmed_state"] is True
    assert registry.register(key, sock, state["owner"]) is state
    assert registry.announce(state, pair.send(0))
    assert not registry.announce(state, pair.send(0))
    pair.drain()
    assert pair.states[1]["confirmed_state"] is True
    assert pair.local(0, False)
    pair.drain()
    assert pair.paused == [{}, {}]
    assert registry.release(state)
    assert not registry.local(key, True, pair.send(0))
    assert not registry.local(key, False, pair.send(0))
    fresh = registry.register(key, sock, object())
    assert fresh["confirmed_state"] is False and not registry.announce(fresh, pair.send(0))


def test_control_namespace_and_session_ownership_isolate_active_legs():
    pair = _ControlPair()
    registry, key, sock = pair.registries[0], pair.keys[0], pair.sockets[0]
    unknown = _transfer_control_key(("127.0.0.2", 6000), 42, 0, 0)
    relay = _transfer_control_key(PEER, 42, 1, 0)
    reverse = _transfer_control_key(PEER, 42, 0, 1)
    others = [registry.register(other, object(), object()) for other in (relay, reverse)]
    assert not registry.receive(unknown, sock, True, pair.send(0))
    assert not registry.receive(key, object(), True, pair.send(0))
    others.append(registry.register(unknown, object(), object()))
    assert registry.register(key, sock, object()) is None
    assert registry.register(key, object(), pair.states[0]["owner"]) is None
    assert pair.local(0, True)
    assert all(state["confirmed_state"] is False for state in others)
    assert pair.paused[0] == {key: True}
    assert registry.discard_socket(sock) == 1
    new = registry.register(key, object(), object())
    assert not registry.release(pair.states[0]) and registry.discard_socket(sock) == 0
    assert new["confirmed_state"] is False and pair.paused[0] == {}
    assert registry.receive(key, new["control_socket"], True, lambda *_: True)
    assert new["confirmed_state"] is True and pair.paused[0] == {key: True}


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("raises", [False, True])
def test_failed_control_write_retires_owner_without_retry(side, raises):
    pair = _ControlPair()
    def fail(sock, line):
        if raises:
            raise BrokenPipeError("closed control socket")
        return False
    registry, key, state = pair.registries[side], pair.keys[side], pair.states[side]
    if raises:
        with pytest.raises(BrokenPipeError):
            registry.local(key, True, fail)
    else:
        assert registry.local(key, True, fail) is False
    assert pair.paused[side] == {} and not any(pair.pending)
    assert state["confirmed_state"] is state["last_requested_state"] is False
    assert registry.register(key, object(), object())["confirmed_state"] is False



def test_start_before_initial_publication_cancels_the_adopted_pause():
    pair = _ControlPair()
    registry, key = pair.registries[0], pair.keys[0]
    assert registry.release(pair.states[0])
    assert not registry.local(key, True, pair.send(0))
    state = registry.register(key, pair.sockets[0], object())
    assert pair.local(0, False)
    assert not registry.announce(state, pair.send(0))
    pair.drain()
    assert state["confirmed_state"] is pair.states[1]["confirmed_state"] is False
    assert pair.paused == [{}, {}]


@pytest.mark.parametrize("sock,owner", [(None, object()), (object(), None)])
def test_leg_registration_requires_both_session_and_owner(sock, owner):
    registry = _TransferControlRegistry({}, threading.Condition())
    with pytest.raises(ValueError):
        registry.register(_transfer_control_key(PEER, 42, 0, 0), sock, owner)

@pytest.mark.parametrize("finish", ["start", "release"])
def test_start_and_release_wake_a_sender_without_reordering_control_writes(finish):
    pair = _ControlPair()
    registry, state = pair.registries[0], pair.states[0]
    entered, proceed, attempted, woke, ready = (threading.Event() for _ in range(5))
    class ObservedLock:
        def __enter__(self):
            if entered.is_set():
                attempted.set()
            return self.lock.__enter__()

        def __exit__(self, *args):
            return self.lock.__exit__(*args)

    observed = ObservedLock()
    observed.lock = state["lock"]
    state["lock"] = observed
    condition, key = registry._condition, pair.keys[0]
    def send(sock, line):
        entered.set()
        assert proceed.wait(2)
        return pair.send(0)(sock, line)
    def waiter():
        with condition:
            ready.set()
            assert condition.wait(timeout=2)
            assert key not in pair.paused[0]
        woke.set()
    def complete():
        if finish == "start":
            registry.receive(key, pair.sockets[0], False, pair.send(0))
        else:
            registry.release(state)
    with ThreadPoolExecutor(max_workers=3) as workers:
        first = workers.submit(registry.local, key, True, send)
        try:
            assert entered.wait(2)
            waiting = workers.submit(waiter)
            assert ready.wait(2)
            second = workers.submit(complete)
            assert attempted.wait(2)
            assert state["confirmed_state"] is True and pair.paused[0] == {key: True}
        finally:
            proceed.set()
        for worker in (first, second, waiting):
            worker.result(timeout=2)
    assert woke.is_set() and pair.paused[0] == {}
    if finish == "start":
        pair.drain()
        assert pair.states[1]["confirmed_state"] is False
    else:
        assert tuple(pair.pending[1]) == ("/pause_trans 42 0 0",)


@pytest.mark.parametrize("owner_type", [TCP_Server_Base, TCP_Client_Base])
def test_control_state_never_enters_the_persisted_progress_snapshot(owner_type, tmp_path):
    owner = owner_type(host="127.0.0.1", port=0, is_extend_command=True,
                       is_enable_encrypto=False, is_input_command_in_console=False,
                       is_print_log=False)
    for attr in ("messages_log_file", "events_log_file", "sent_messages_log_file",
                 "sent_events_log_file", "file_trans_progress_file"):
        setattr(owner, attr, str(tmp_path / f"{attr}.json"))
    owner.file_upload_progress[(PEER, 42)] = [3, 10, None, "/send/file.bin", PEER]
    before = owner._transfer_progress_snapshot()
    try:
        key = _transfer_control_key(PEER, 42, 0, 0)
        controls = owner._transfer_controls
        assert not controls.local(key, True, lambda *_: True)
        controls.register(key, object(), object())
        owner._persist_transfer_progress()
        with open(owner.file_trans_progress_file, encoding="utf-8") as stream:
            assert json.load(stream) == before
    finally:
        owner.stop() if owner_type is TCP_Server_Base else owner.close()


@pytest.mark.parametrize("owner_type", [TCP_Server_Base, TCP_Client_Base])
def test_public_control_requires_typed_keywords_and_presets_only_exact_senders(owner_type, tmp_path):
    owner = owner_type(host=PEER[0], port=PEER[1], is_extend_command=True,
                       is_enable_encrypto=False, is_input_command_in_console=False,
                       is_print_log=False)
    for attr in ("messages_log_file", "events_log_file", "sent_messages_log_file",
                 "sent_events_log_file", "file_trans_progress_file"):
        setattr(owner, attr, str(tmp_path / f"{attr}.json"))
    try:
        for operation in (owner.pause_trans, owner.start_trans):
            with pytest.raises(TypeError):
                operation(42, PEER)
            with pytest.raises(TypeError):
                operation(42, PEER, 0, 0)
            with pytest.raises(ValueError):
                operation(42, PEER, trans_type=2, direction=0)
        if owner_type is TCP_Server_Base:
            with pytest.raises(ValueError):
                owner.pause_trans(42, None, trans_type=0, direction=0)
        else:
            assert not owner.pause_trans(42, ("127.0.0.2", 6000), trans_type=0, direction=0)
        assert not owner.pause_trans(42, PEER, trans_type=0, direction=1)
        assert not owner.pause_trans(42, PEER, trans_type=1, direction=1)
        assert owner._transfer_pause == {}
        for kind in (0, 1):
            assert not owner.pause_trans(42, PEER, trans_type=kind, direction=0)
        assert owner._transfer_pause == {(PEER, 42, kind, 0): True for kind in (0, 1)}
        assert not owner.start_trans(42, PEER, trans_type=0, direction=0)
        assert owner._transfer_pause == {(PEER, 42, 1, 0): True}
        assert not owner.start_trans(42, PEER, trans_type=1, direction=0)
        assert owner._transfer_pause == {} and owner._transfer_controls._legs == {}
    finally:
        owner.stop() if owner_type is TCP_Server_Base else owner.close()


def test_relay_control_prefers_exact_child_and_requires_one_live_same_peer_alias():
    server = TCP_Server_Base.__new__(TCP_Server_Base)
    server._forward_relays, server._forward_relays_lock = {}, threading.Lock()
    server._fid_route_index = {}
    server._transfer_pause_cond = threading.Condition()
    registry = _TransferControlRegistry({}, server._transfer_pause_cond)
    server._transfer_controls = registry
    sock = object()
    keys = [(PEER, child, 1, 0) for child in (42, 100, 43)]
    states = [registry.register(key, sock, object()) for key in keys]
    relays = [{"fid": fid, "route": {"targets": {PEER: key[1]}},
               "controls": {PEER: state}}
              for fid, key, state in zip((100, 200, 200), keys, states)]
    server._forward_relays.update(zip(((PEER, 42), (PEER, 100)), relays))

    assert _control_key(server, PEER, 100, 1, 0) == keys[1]
    assert _control_key(server, PEER, 200, 1, 0) == keys[1]
    assert registry.receive(_control_key(server, PEER, 100, 1, 0), sock, True,
                            lambda *_: True)
    assert [state["confirmed_state"] for state in states] == [False, True, False]
    for peer, fid, kind, role in [(("127.0.0.2", 6000), 200, 1, 0),
                                  (PEER, 200, 0, 0), (PEER, 200, 1, 1),
                                  (PEER, 999, 1, 0)]:
        assert _control_key(server, peer, fid, kind, role) == (peer, fid, kind, role)
        assert not registry.receive(_control_key(server, peer, fid, kind, role),
                                    sock, True, lambda *_: True)
    server._forward_relays[(PEER, 43)] = relays[2]
    assert not registry.receive(_control_key(server, PEER, 200, 1, 0), sock, True,
                                lambda *_: True)  # Ambiguous upstream alias.
    assert registry.release(states[2])
    assert _control_key(server, PEER, 200, 1, 0) == keys[1]
    assert registry.release(states[1])
    assert _control_key(server, PEER, 200, 1, 0) == (PEER, 200, 1, 0)
    assert not registry.receive(_control_key(server, PEER, 200, 1, 0), sock, True,
                                lambda *_: True)  # Stale relay mappings cannot route.
    assert states[0]["confirmed_state"] is False


def test_receiver_pressure_and_user_intents_survive_notifications_and_stale_owner():
    pair = _ControlPair()
    registry, key, state = pair.registries[1], pair.keys[1], pair.states[1]
    pressure = lambda paused: registry.local(
        key, paused, pair.send(1), backpressure=True, owner=state["owner"])
    assert pressure(True)
    pair.drain()
    pair.local(1, True)
    pressure(False)
    pair.drain()
    assert state["user_paused"] and not state["backpressure"]
    assert state["confirmed_state"] and state["last_requested_state"]
    assert pair.local(0, False)
    pair.drain()
    assert state["user_paused"] and not state["backpressure"]
    assert state["last_requested_state"] and not state["confirmed_state"]
    pressure(True)
    pair.local(1, False)
    pair.drain()
    assert not state["user_paused"] and state["backpressure"]
    assert state["confirmed_state"] and state["last_requested_state"]
    assert pair.local(0, False)
    pair.drain()
    assert not state["confirmed_state"] and state["backpressure"]
    assert state["last_requested_state"] and not state["user_paused"]
    pressure(False)
    pair.drain()
    assert not state["last_requested_state"] and pair.paused == [{}, {}]
    assert registry.release(state)
    replacement = registry.register(key, pair.sockets[1], object())
    assert not pressure(True)
    assert not replacement["backpressure"] and not replacement["user_paused"]
    assert not replacement["last_requested_state"] and not any(pair.pending)
