"""Unit tests for the server's authoritative, receiver-scoped FID routes."""

import json
import socket
import threading

import pytest

from PyFlow.network_api.connect_tcp import (
    TCP_Server_Base, _claim_receive, _release_receive, parse_resume_command,
)


# The server's control side as the unit tests drive `_resume_request` by hand.
SERVER_LOCAL = ("127.0.0.1", 65432)
ORIGINATOR = ("127.0.0.1", 50001)
TARGET_1 = ("127.0.0.1", 50002)
TARGET_2 = ("127.0.0.1", 50003)
TARGET_3 = ("127.0.0.1", 50004)


def _server(tmp_path, name="server"):
    """Build an unstarted server whose progress file belongs to this test."""
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=0,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        is_print_log=False,
    )
    server.messages_log_file = str(tmp_path / f"{name}_messages_log.json")
    server.events_log_file = str(tmp_path / f"{name}_events_log.json")
    server.sent_messages_log_file = str(tmp_path / f"{name}_sent_messages_log.json")
    server.sent_events_log_file = str(tmp_path / f"{name}_sent_events_log.json")
    server.file_trans_progress_file = str(tmp_path / f"{name}_file_trans_progress.json")
    return server


@pytest.fixture
def server(tmp_path):
    instance = _server(tmp_path)
    try:
        yield instance
    finally:
        instance.stop()


def _install_client(server, address):
    """Install a real control owner while retaining its peer for cleanup."""
    control, peer = socket.socketpair()
    server.clients[address] = {"socket": control}
    return control, peer


def test_lookup_returns_the_same_authoritative_route_from_upstream_and_targets(server):
    route = server._register_fid_route(
        303,
        ORIGINATOR,
        {TARGET_1: 305, TARGET_2: 306, TARGET_3: 307},
    )
    assert route == {"originator": ORIGINATOR, "targets": {
        TARGET_1: 305,
        TARGET_2: 306,
        TARGET_3: 307,
    }, "completed": set()}
    assert server._lookup_fid_route(None, 303) == (303, route)
    for target, child_fid in route["targets"].items():
        upstream_fid, found = server._lookup_fid_route(target, child_fid)
        assert upstream_fid == 303
        assert found is route


def test_completion_is_identity_safe_and_removes_only_one_reverse_index(server):
    route = server._register_fid_route(
        303, ORIGINATOR, {TARGET_1: 305, TARGET_2: 306}
    )
    assert route is not None

    assert server._complete_fid_target(303, route, TARGET_3, 305) is False
    assert server._complete_fid_target(303, route, TARGET_1, 306) is False
    assert server._complete_fid_target(303, {**route}, TARGET_1, 305) is False
    assert server._complete_fid_target(303, route, TARGET_1, 305) is True
    assert server._complete_fid_target(303, route, TARGET_1, 305) is False
    assert route["completed"] == {TARGET_1}
    assert server._lookup_fid_route(TARGET_1, 305) is None
    assert server._lookup_fid_route(TARGET_2, 306) == (303, route)


def test_registration_rejects_completed_outside_targets_without_mutation(server):
    assert server._register_fid_route(
        303, ORIGINATOR, {TARGET_1: 305}, completed=(TARGET_2,)
    ) is None
    assert server._fid_routes == {}
    assert server._fid_route_index == {}


def test_equal_child_fids_are_scoped_by_receiver_even_across_routes(server):
    first = server._register_fid_route(303, ORIGINATOR, {TARGET_1: 305, TARGET_2: 305})
    second = server._register_fid_route(304, ORIGINATOR, {TARGET_3: 305})

    assert first is not None
    assert second is not None
    assert server._lookup_fid_route(TARGET_1, 305) == (303, first)
    assert server._lookup_fid_route(TARGET_2, 305) == (303, first)
    assert server._lookup_fid_route(TARGET_3, 305) == (304, second)
    assert server._lookup_fid_route(TARGET_3, 306) is None


def test_lookup_rejects_unknown_and_wrong_peers_without_falling_back_to_source(server):
    route = server._register_fid_route(303, ORIGINATOR, {TARGET_1: 305})

    assert route is not None
    assert server._lookup_fid_route(None, 305) is None
    assert server._lookup_fid_route(ORIGINATOR, 305) is None
    assert server._lookup_fid_route(("127.0.0.1", 59999), 305) is None
    assert server._lookup_fid_route(TARGET_1, 303) is None
    assert server._lookup_fid_route(None, 999) is None


def test_registration_collision_is_atomic_for_primary_and_downstream_indexes(server):
    original = server._register_fid_route(303, ORIGINATOR, {TARGET_1: 305, TARGET_2: 306})
    assert original is not None
    primary_before = dict(server._fid_routes)
    index_before = dict(server._fid_route_index)

    assert server._register_fid_route(303, TARGET_3, {TARGET_3: 307}) is None
    assert server._fid_routes == primary_before
    assert server._fid_route_index == index_before

    assert server._register_fid_route(304, TARGET_3, {TARGET_2: 306, TARGET_3: 308}) is None
    assert server._fid_routes == primary_before
    assert server._fid_route_index == index_before


@pytest.mark.parametrize(
    "owner",
    [
        "receive", "upload", "active_fid", "active_receive_fid",
        "active_socket", "active_receive_socket", "control", "relay", "route_index",
    ],
)
def test_registration_rejects_upstream_fid_that_collides_with_live_transfer_identity(
    server, owner
):
    fid = 303
    target_key = (TARGET_1, fid)
    if owner == "receive":
        server.file_recv_progress[fid] = [0, 1, None, "/tmp/live", ORIGINATOR]
    elif owner == "upload":
        server.file_upload_progress[target_key] = [0, 1, None, "/tmp/live", TARGET_1]
    elif owner == "active_fid":
        server._active_transfer_fids.add(target_key)
    elif owner == "active_receive_fid":
        server._active_transfer_fids.add(fid)
    elif owner == "active_socket":
        server._active_transfer_sockets[target_key] = object()
    elif owner == "active_receive_socket":
        server._active_transfer_sockets[fid] = object()
    elif owner == "control":
        data_socket = object()
        server._register_transfer_socket(target_key, data_socket)
        state = server._transfer_controls.register(
            (TARGET_1, fid, 1, 0), object(), data_socket,
        )
        assert state is not None
        server._transfer_controls.local(state["key"], True, lambda *_: True)
    elif owner == "relay":
        server._forward_relays[target_key] = object()
    else:
        assert server._register_fid_route(404, ORIGINATOR, {TARGET_1: fid}) is not None

    routes_before = dict(server._fid_routes)
    indexes_before = dict(server._fid_route_index)

    assert server._register_fid_route(303, ORIGINATOR, {TARGET_1: 306}) is None
    assert server._fid_routes == routes_before
    assert server._fid_route_index == indexes_before


@pytest.mark.parametrize("record", ["false", "preset", "sender", "other_type", "other_direction"])
def test_route_registration_ignores_control_records_without_live_data_ownership(server, record):
    key = (TARGET_1, 303, 0 if record == "other_type" else 1, 0)
    if record == "other_direction":
        key = (ORIGINATOR, 303, 1, 1)
    if record in ("false", "preset"):
        server._transfer_pause[key] = record == "preset"
    else:
        state = server._transfer_controls.register(key, object(), object())
        assert state is not None and not state["confirmed_state"]
    paused_before = dict(server._transfer_pause)
    controls_before = dict(server._transfer_controls._legs)

    route = server._register_fid_route(303, ORIGINATOR, {TARGET_1: 305})

    assert route is not None and server._lookup_fid_route(TARGET_1, 305) == (303, route)
    assert server._fid_route_index == {(TARGET_1, 305): 303}
    assert server._transfer_pause == paused_before
    assert server._transfer_controls._legs == controls_before


def test_route_registration_refuses_a_live_setup_owner_before_socket_indexing(server):
    with socket.socket() as owner:
        state = server._transfer_controls.register((TARGET_1, 303, 1, 0), object(), owner)
        assert state is not None and server._active_transfer_sockets == {}
        routes_before, indexes_before = dict(server._fid_routes), dict(server._fid_route_index)
        assert server._register_fid_route(303, ORIGINATOR, {TARGET_1: 305}) is None
        assert server._fid_routes == routes_before and server._fid_route_index == indexes_before
        assert server._transfer_controls._legs[state["key"]] is state


def test_registration_scopes_live_downstream_upload_conflicts_by_receiver(server):
    fid = 303
    server.file_upload_progress[(TARGET_2, fid)] = [0, 1, None, "/tmp/live", TARGET_2]

    route = server._register_fid_route(fid, ORIGINATOR, {TARGET_1: 305})

    assert route is not None
    assert server._lookup_fid_route(TARGET_1, 305) == (fid, route)


def test_route_removal_is_identity_safe_and_clears_all_owned_indexes(server):
    route = server._register_fid_route(303, ORIGINATOR, {TARGET_1: 305, TARGET_2: 306})
    assert route is not None

    forged_equal_route = {
        "originator": route["originator"],
        "targets": dict(route["targets"]),
    }
    assert server._drop_fid_route(303, forged_equal_route) is False
    assert server._lookup_fid_route(None, 303) == (303, route)
    assert server._lookup_fid_route(TARGET_1, 305) == (303, route)

    assert server._drop_fid_route(303, route) is True
    assert server._lookup_fid_route(None, 303) is None
    assert server._lookup_fid_route(TARGET_1, 305) is None
    assert server._lookup_fid_route(TARGET_2, 306) is None


def test_route_persists_and_round_trips_through_a_fresh_server(tmp_path):
    first = _server(tmp_path, "first")
    second = None
    try:
        route = first._register_fid_route(303, ORIGINATOR, {TARGET_1: 305, TARGET_2: 306})
        assert route is not None
        first._persist_transfer_progress()

        with open(first.file_trans_progress_file, "r", encoding="utf-8") as stream:
            snapshot = json.load(stream)
        assert snapshot["relays"] == {
            "303": {
                "originator": list(ORIGINATOR),
                "targets": [[list(TARGET_1), 305], [list(TARGET_2), 306]],
                "completed": [],
            }
        }

        second = _server(tmp_path, "second")
        second.file_trans_progress_file = first.file_trans_progress_file
        assert second._load_transfer_progress() == 1
        restored = second._fid_routes[303]
        assert restored["originator"] == ORIGINATOR
        assert restored["targets"] == {TARGET_1: 305, TARGET_2: 306}
        assert restored["completed"] == set()
        assert second._lookup_fid_route(TARGET_1, 305) == (303, restored)
        assert second._lookup_fid_route(TARGET_2, 306) == (303, restored)
    finally:
        first.stop()
        if second is not None:
            second.stop()


def test_reload_does_not_clobber_live_primary_or_downstream_index_owners(tmp_path):
    persisted = _server(tmp_path, "persisted")
    live = None
    try:
        assert persisted._register_fid_route(303, ORIGINATOR, {TARGET_1: 305}) is not None
        assert persisted._register_fid_route(304, ORIGINATOR, {TARGET_2: 306}) is not None
        persisted._persist_transfer_progress()

        live = _server(tmp_path, "live")
        live.file_trans_progress_file = persisted.file_trans_progress_file
        primary_owner = live._register_fid_route(303, TARGET_3, {TARGET_3: 307})
        index_owner = live._register_fid_route(305, TARGET_3, {TARGET_2: 306})
        assert primary_owner is not None
        assert index_owner is not None

        assert live._load_transfer_progress() == 0
        assert live._fid_routes == {303: primary_owner, 305: index_owner}
        assert live._fid_route_index == {
            (TARGET_3, 307): 303,
            (TARGET_2, 306): 305,
        }
    finally:
        persisted.stop()
        if live is not None:
            live.stop()


def test_unsupported_old_server_relay_snapshot_is_preserved_on_write_and_stop(tmp_path):
    server = _server(tmp_path)
    try:
        legacy = {
            "upload": {},
            "download": {},
            "relays": {
                "303": {
                    "fid": 303,
                    "originator": list(ORIGINATOR),
                    "targets": [list(TARGET_1)],
                    "tfids": [305],
                    "kind": 1,
                }
            },
        }
        with open(server.file_trans_progress_file, "w", encoding="utf-8") as stream:
            json.dump(legacy, stream, indent=2)
        before = open(server.file_trans_progress_file, "rb").read()

        assert server._load_transfer_progress() == 0
        assert server._fid_routes == {}
        assert server._register_fid_route(303, ORIGINATOR, {TARGET_1: 305}) is not None
        server._persist_transfer_progress()
        assert open(server.file_trans_progress_file, "rb").read() == before
        server.stop()
        assert open(server.file_trans_progress_file, "rb").read() == before
    finally:
        # stop() is intentionally idempotent; the explicit call above checks the
        # shutdown persistence path while this cleanup handles assertion failures.
        server.stop()


def test_completed_targets_survive_reload_without_recreating_their_indexes(tmp_path):
    first = _server(tmp_path, "first")
    second = _server(tmp_path, "second")
    try:
        route = first._register_fid_route(303, ORIGINATOR, {TARGET_1: 305, TARGET_2: 306})
        assert first._complete_fid_target(303, route, TARGET_1, 305)
        second.file_trans_progress_file = first.file_trans_progress_file
        assert second._load_transfer_progress() == 1
        restored = second._fid_routes[303]
        assert restored["targets"] == {TARGET_1: 305, TARGET_2: 306}
        assert restored["completed"] == {TARGET_1}
        assert second._lookup_fid_route(None, 303)[1] is restored
        assert second._lookup_fid_route(TARGET_1, 305) is None
        assert second._lookup_fid_route(TARGET_2, 306)[1] is restored
        assert second._fid_route_index == {(TARGET_2, 306): 303}
        assert second._drop_fid_route(303, restored)
        replacement = second._register_fid_route(303, TARGET_3, {TARGET_1: 305})
        assert not second._complete_fid_target(303, restored, TARGET_2, 306)
        assert second._lookup_fid_route(TARGET_1, 305)[1] is replacement
    finally:
        first.stop()
        second.stop()


@pytest.mark.parametrize("completed", [None, [list(TARGET_2)], [list(TARGET_1)] * 2])
def test_invalid_completed_snapshot_is_preserved_on_load_write_and_stop(tmp_path, completed):
    instance = _server(tmp_path)
    snapshot = {"relays": {"303": {
        "originator": list(ORIGINATOR), "targets": [[list(TARGET_1), 305]],
        "completed": completed,
    }}}
    with open(instance.file_trans_progress_file, "w", encoding="utf-8") as stream:
        json.dump(snapshot, stream)
    with open(instance.file_trans_progress_file, "rb") as stream:
        before = stream.read()
    try:
        assert instance._load_transfer_progress() == 0
        assert instance._fid_routes == {}
        assert instance._register_fid_route(303, ORIGINATOR, {TARGET_1: 305}) is not None
        instance._persist_transfer_progress()
        instance.stop()
        with open(instance.file_trans_progress_file, "rb") as stream:
            assert stream.read() == before
    finally:
        instance.stop()


def test_canonical_snapshot_without_completed_has_no_acknowledged_targets(tmp_path):
    instance = _server(tmp_path)
    with open(instance.file_trans_progress_file, "w", encoding="utf-8") as stream:
        json.dump({"relays": {"303": {
            "originator": list(ORIGINATOR), "targets": [[list(TARGET_1), 305]],
        }}}, stream)
    try:
        assert instance._load_transfer_progress() == 1
        route = instance._fid_routes[303]
        assert route["completed"] == set()
        assert instance._lookup_fid_route(TARGET_1, 305)[1] is route
    finally:
        instance.stop()


@pytest.mark.parametrize("downstream", [304, 305])
def test_resume_rejects_forged_or_stale_requesters_before_registration(
    server, downstream
):
    route = server._register_fid_route(302, ORIGINATOR, {TARGET_1: downstream})
    current, current_peer = _install_client(server, TARGET_1)
    stale, stale_peer = socket.socketpair()
    try:
        for sock in (stale, current):
            for origin, target, fid in (
                (TARGET_2, TARGET_1, downstream),
                (ORIGINATOR, TARGET_2, downstream),
                (ORIGINATOR, TARGET_1, downstream + 2),
                (ORIGINATOR, TARGET_1, downstream),
            ):
                if sock is current and (origin, target, fid) == (ORIGINATOR, TARGET_1, downstream):
                    continue
                assert not server._resume_request(sock, TARGET_1, fid, 7, origin, target)
                assert server._relay_recoveries == {}
                assert server._fid_routes[302] is route
        assert server._resume_request(
            current, TARGET_1, downstream, 7, ORIGINATOR, TARGET_1
        )
        state = server._relay_recoveries[302]
        assert state["route"] is route
        assert state["active"] == {}
        assert state["pending"] == {TARGET_1: (current, 7)}
    finally:
        server.clients.pop(TARGET_1, None)
        current.close()
        current_peer.close()
        stale.close()
        stale_peer.close()


def test_duplicate_registration_keeps_only_the_latest_pending_offset(server):
    route = server._register_fid_route(302, ORIGINATOR, {TARGET_1: 304})
    current, peer = _install_client(server, TARGET_1)
    try:
        assert server._resume_request(
            current, TARGET_1, 304, 7, ORIGINATOR, TARGET_1
        )
        state = server._relay_recoveries[302]
        assert server._resume_request(
            current, TARGET_1, 304, 19, ORIGINATOR, TARGET_1
        )
        assert server._relay_recoveries[302] is state
        assert state["active"] == {}
        assert state["pending"] == {TARGET_1: (current, 19)}
        assert not server._resume_request(current, TARGET_1, 304, 19, ORIGINATOR, TARGET_1)
        assert server._fid_routes[302] is route
    finally:
        server.clients.pop(TARGET_1, None)
        current.close()
        peer.close()


def test_recovery_registration_is_retained_when_source_is_offline(server):
    route = server._register_fid_route(302, ORIGINATOR, {TARGET_1: 304, TARGET_2: 306})
    controls = []
    try:
        for target, child in route["targets"].items():
            control, peer = _install_client(server, target)
            controls.extend((control, peer))
            assert server._resume_request(
                control, target, child, 13, ORIGINATOR, target
            )
        state = server._relay_recoveries[302]
        assert state["route"] is route
        assert state["active"] == {}
        assert state["pending"] == {
            target: (server.clients[target]["socket"], 13) for target in route["targets"]
        }
        assert server._fid_route_index == {
            (TARGET_1, 304): 302, (TARGET_2, 306): 302
        }
    finally:
        server.clients.clear()
        for control in controls:
            control.close()


def test_concurrent_relay_requests_reserve_one_real_source_request(server):
    route = server._register_fid_route(302, ORIGINATOR, {TARGET_1: 304})
    target, target_peer = _install_client(server, TARGET_1)
    source, source_peer = _install_client(server, ORIGINATOR)
    target_peer.settimeout(5)
    source_peer.settimeout(5)
    server.running = True
    barrier, accepted = threading.Barrier(3), []

    def reply():
        barrier.wait(timeout=5)
        accepted.append(server._resume_request(target, TARGET_1, 304, 13, ORIGINATOR, TARGET_1))

    contenders = [threading.Thread(target=reply) for _ in range(2)]
    try:
        for contender in contenders:
            contender.start()
        barrier.wait(timeout=5)
        for contender in contenders:
            contender.join(timeout=5)
            assert not contender.is_alive()
        assert sorted(accepted) == [False, True]
        with source_peer.makefile("r") as stream:
            assert parse_resume_command(stream.readline()) == (302, 13, ORIGINATOR, TARGET_1)
        record = server._receive_transfers[(TARGET_1, 302)]
        assert record["control_socket"] is source
        assert server._relay_recoveries[302]["active"][TARGET_1]["record"] is record
        assert server._fid_routes[302] is route and not route["completed"]
    finally:
        server.stop()
        with server._transfer_progress_changed:
            assert server._transfer_progress_changed.wait_for(
                lambda: not server._relay_recoveries[302]["active"], timeout=5)
        target_peer.close()
        source_peer.close()


def test_failed_source_request_allows_a_new_explicit_numeric_request(server, monkeypatch):
    route = server._register_fid_route(302, ORIGINATOR, {TARGET_1: 304})
    target, target_peer = _install_client(server, TARGET_1)
    source, source_peer = _install_client(server, ORIGINATOR)
    source_peer.settimeout(5)
    server.running = True
    attempts, real_send = [], server.send_message

    def send(sock, message, is_send_raw=False):
        attempts.append(message)
        return False if len(attempts) == 1 else real_send(sock, message, is_send_raw=is_send_raw)

    monkeypatch.setattr(server, "send_message", send)
    try:
        assert server._resume_request(target, TARGET_1, 304, 1, ORIGINATOR, TARGET_1)
        state = server._relay_recoveries[302]
        with server._transfer_progress_changed:
            assert server._transfer_progress_changed.wait_for(lambda: not state["active"], timeout=5)
        assert state["pending"] == {} and state["route"] is route
        assert server._resume_request(target, TARGET_1, 304, 17, ORIGINATOR, TARGET_1)
        with source_peer.makefile("r") as stream:
            assert parse_resume_command(stream.readline()) == (302, 17, ORIGINATOR, TARGET_1)
        assert server._receive_transfers[(TARGET_1, 302)]["control_socket"] is source
        assert state["active"][TARGET_1]["offset"] == 17
        assert server._fid_routes[302] is route and len(attempts) == 2
    finally:
        server.stop()
        with server._transfer_progress_changed:
            assert server._transfer_progress_changed.wait_for(lambda: not state["active"], timeout=5)
        target_peer.close()
        source_peer.close()


@pytest.mark.parametrize("replace_session", [False, True], ids=["same-session", "new-target-session"])
def test_failed_source_write_cannot_release_an_already_adopted_receive_owner(
    server, monkeypatch, replace_session
):
    route = server._register_fid_route(302, ORIGINATOR, {TARGET_1: 304})
    target, target_peer = _install_client(server, TARGET_1)
    source, source_peer = _install_client(server, ORIGINATOR)
    data, data_peer = socket.socketpair()
    replacement_peer = None
    source_peer.settimeout(5)
    server.running = True
    waiting, adopted = threading.Event(), []
    real_send, real_wait = server.send_message, server._transfer_progress_changed.wait

    def send(sock, message, is_send_raw=False):
        sent = real_send(sock, message, is_send_raw=is_send_raw)
        if not adopted:
            record = _claim_receive(server, (TARGET_1, 302), source, data)
            assert record is not None
            adopted.append(record)
            return False  # A peer may consume a request before local write failure is reported.
        return sent

    def wait(timeout=None):
        if adopted and adopted[0]["owner"] is data:
            waiting.set()
        return real_wait(timeout)

    monkeypatch.setattr(server, "send_message", send)
    monkeypatch.setattr(server._transfer_progress_changed, "wait", wait)
    try:
        assert server._resume_request(target, TARGET_1, 304, 7, ORIGINATOR, TARGET_1)
        assert waiting.wait(timeout=5)
        with source_peer.makefile("r") as stream:
            assert parse_resume_command(stream.readline()) == (302, 7, ORIGINATOR, TARGET_1)
            assert server._receive_transfers[(TARGET_1, 302)] is adopted[0]
            assert not server._resume_request(target, TARGET_1, 304, 19, ORIGINATOR, TARGET_1)
            if replace_session:
                replacement, replacement_peer = _install_client(server, TARGET_1)
                assert server._resume_request(replacement, TARGET_1, 304, 19, ORIGINATOR, TARGET_1)
                state = server._relay_recoveries[302]
                assert state["pending"] == {TARGET_1: (replacement, 19)}
                assert state["active"][TARGET_1]["record"] is adopted[0]
            _release_receive(server, (TARGET_1, 302), adopted[0])
            if not replace_session:
                with server._transfer_progress_changed:
                    assert server._transfer_progress_changed.wait_for(
                        lambda: not server._relay_recoveries[302]["active"], timeout=5)
                assert server._resume_request(target, TARGET_1, 304, 19, ORIGINATOR, TARGET_1)
            assert parse_resume_command(stream.readline()) == (302, 19, ORIGINATOR, TARGET_1)
        assert server._fid_routes[302] is route and not route["completed"]
    finally:
        if adopted:
            _release_receive(server, (TARGET_1, 302), adopted[0])
        server.stop()
        with server._transfer_progress_changed:
            assert server._transfer_progress_changed.wait_for(
                lambda: not server._relay_recoveries[302]["active"], timeout=5)
        if replacement_peer is not None:
            replacement_peer.close()
        for sock in (target, target_peer, source_peer, data, data_peer):
            sock.close()