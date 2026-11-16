"""Contracts for typed transfer control and connection-scoped endpoint identity."""

import socket

import pytest

from PyFlow.network_api.connect_tcp import (
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
