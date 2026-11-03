"""Unit tests for the server's authoritative, receiver-scoped FID routes."""

import json
import socket

import pytest

from PyFlow.network_api.connect_tcp import TCP_Server_Base, parse_resume_command


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
def test_resume_uses_scoped_route_not_id_lowbit_and_rejects_forged_endpoints(server, downstream):
    route = server._register_fid_route(302, ORIGINATOR, {TARGET_1: downstream})
    for peer, origin, target, fid in (
        (TARGET_1, TARGET_2, TARGET_1, downstream),
        (TARGET_1, ORIGINATOR, TARGET_2, downstream),
        (TARGET_2, ORIGINATOR, TARGET_2, downstream),
        (TARGET_1, ORIGINATOR, TARGET_1, downstream + 2),
    ):
        assert not server._resume_request(peer, fid, 7, origin, target)
        assert server._pending_originator_resumes == {}
        assert server._fid_routes[302] is route
    # Offline is not success, but a verified receiver's request is retained.
    assert not server._resume_request(TARGET_1, downstream, 7, ORIGINATOR, TARGET_1)
    assert not server._resume_request(TARGET_1, downstream, 19, ORIGINATOR, TARGET_1)
    assert server._pending_originator_resumes == {ORIGINATOR: {(302, TARGET_1): 19}}
    assert server._fid_route_index == {(TARGET_1, downstream): 302}


def test_pending_resume_survives_failed_send_and_prunes_completed_or_missing_routes(server):
    route = server._register_fid_route(302, ORIGINATOR, {TARGET_1: 304, TARGET_2: 306})
    for target, child in route["targets"].items():
        assert not server._resume_request(target, child, 13, ORIGINATOR, target)
    broken, peer = socket.socketpair()
    peer.close()
    server.running = True
    server.clients[ORIGINATOR] = {"socket": broken}
    try:
        assert server._flush_pending_originator_resumes(ORIGINATOR) == 0
        assert server._pending_originator_resumes == {
            ORIGINATOR: {(302, TARGET_1): 13, (302, TARGET_2): 13}
        }
    finally:
        broken.close()
    assert server._complete_fid_target(302, route, TARGET_1, 304)
    writer, reader = socket.socketpair()
    reader.settimeout(2)
    server.clients[ORIGINATOR] = {"socket": writer}
    try:
        assert server._flush_pending_originator_resumes(ORIGINATOR) == 1
        assert parse_resume_command(reader.recv(4096).decode()) == (
            302, 13, ORIGINATOR, TARGET_2
        )
        assert server._pending_originator_resumes == {}
        assert not server._resume_request(TARGET_1, 304, 0, ORIGINATOR, TARGET_1)
        # An accepted request must not outlive its owning route.
        assert server._queue_resume_to_originator(302, route, TARGET_2, 29)
        reader.recv(4096)
        server.clients.pop(ORIGINATOR)
        assert not server._queue_resume_to_originator(302, route, TARGET_2, 31)
        assert server._drop_fid_route(302, route)
        server.clients[ORIGINATOR] = {"socket": writer}
        assert server._flush_pending_originator_resumes(ORIGINATOR) == 0
        assert server._pending_originator_resumes == {}
    finally:
        server.running = False
        server.clients.clear()
        writer.close()
        reader.close()