"""Resume matching and atomic ownership contracts for ordinary file recovery.

An interrupted download matches its fid, sender and file name. An ordinary
upload can reserve recovery only after its previous per-object owner exits.
"""

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from PyFlow.network_api.connect_tcp import (
    TCP_Client_Base,
    TCP_Server_Base,
    _sending_transfer,
    _TransferControlRegistry,
    _TransferHandshakeRegistry,
    _start_resume_send,
    _resumable_entry,
    _retire_transfer_session,
    _wait_transfer_session,
    format_resume_line,
    parse_resume_command,
)

PEER = ("127.0.0.1", 6000)


def _progress(peer=PEER, path="/recv/download.bin"):
    return {7: [1024, 4096, None, path, peer]}


def test_matching_fid_peer_and_name_resumes():
    progress = _progress()
    entry = progress[7]
    assert _resumable_entry(progress, 7, PEER, ["/file", "download.bin", "7"]) is entry


def test_name_is_compared_by_basename_of_the_partial_path():
    progress = _progress(path="/recv/nested/dir/download.bin")
    assert _resumable_entry(progress, 7, PEER, ["/file", "download.bin", "7"]) is progress[7]


def test_persisted_peer_list_matches_a_peer_tuple():
    # the progress file stores the peer as a JSON list; the live line carries a tuple
    progress = _progress(peer=["127.0.0.1", 6000])
    assert _resumable_entry(progress, 7, PEER, ["/file", "download.bin", "7"]) is progress[7]


@pytest.mark.parametrize(
    ("progress", "fid", "peer", "command_part"),
    [
        pytest.param(_progress(), 8, PEER, ["/file", "download.bin", "8"], id="unknown-fid"),
        pytest.param(
            _progress(), 7, ("10.0.0.2", 6000), ["/file", "download.bin", "7"], id="other-peer"
        ),
        pytest.param(_progress(), 7, PEER, ["/file", "other.bin", "7"], id="other-file-name"),
        pytest.param(
            _progress(peer=None), 7, PEER, ["/file", "download.bin", "7"], id="no-recorded-peer"
        ),
        pytest.param(_progress(), None, PEER, ["/file", "download.bin"], id="no-fid"),
        pytest.param(_progress(), 7, None, ["/file", "download.bin", "7"], id="no-peer"),
        pytest.param(
            _progress(), 7, PEER, ["/file_folder", "download.bin", "7"], id="folder-command"
        ),
        pytest.param(_progress(), 7, PEER, ["/file", "7"], id="too-few-tokens"),
    ],
)
def test_anything_else_is_a_new_transfer(progress, fid, peer, command_part):
    assert _resumable_entry(progress, fid, peer, command_part) is None


def test_resume_command_preserves_shell_quoted_endpoint_identity():
    origin = ("sender's host", 50001)
    target = ("target host", 50002)
    line = format_resume_line(304, 12345, origin, target)
    assert parse_resume_command(line) == (304, 12345, origin, target)


@pytest.mark.parametrize("line", [
    "/resume_trans 304 1",
    "/forward_resume '(\"host\", 5)' 304 1",
    "/resume_trans 304 -1 '(\"host\", 5)' '(\"target\", 6)'",
    "/resume_trans -1 1 '(\"host\", 5)' '(\"target\", 6)'",
    "/resume_trans 304 1 '[\"host\", 5]' '(\"target\", 6)'",
    "/resume_trans 304 1 '(\"host\", \"5\")' '(\"target\", 6)'",
    "/resume_trans 304 1 '(\"host\", 5)' '(\"target\",)'",
    "/resume_trans 304 1 '(\"host\", 5)' '(\"target\", 6)' extra",
    "/resume_trans 304 1 'unterminated",
])
def test_resume_command_rejects_ambiguous_or_malformed_identity(line):
    assert parse_resume_command(line) is None


@pytest.fixture
def ordinary_resume():
    control, wire = socket.socketpair()
    paused, condition = {}, threading.Condition()
    key = (PEER, 7)
    owner = SimpleNamespace(
        _transfer_progress_lock=threading.RLock(), _progress_discarded=False,
        file_upload_progress={key: [100, 100, None, "/send/eof.bin", PEER]},
        _resume_sends={}, _active_transfer_fids=set(), _active_transfer_sockets={},
        _receive_transfers={},
        _transfer_pause=paused, _transfer_pause_cond=condition,
        _transfer_controls=_TransferControlRegistry(paused, condition),
        _transfer_handshakes=_TransferHandshakeRegistry(),
    )
    owner._transfer_progress_changed = threading.Condition(owner._transfer_progress_lock)
    try:
        yield owner, key, control, wire
    finally:
        control.close()
        wire.close()


@pytest.mark.parametrize("busy", ["active", "paused", "bound", "closed", "pending", "unresolved", "owner", "dead-control"])
def test_ordinary_resume_refuses_an_owned_object_without_queuing(ordinary_resume, busy):
    owner, key, control, wire = ordinary_resume
    if busy == "active":
        owner._active_transfer_fids.add(key)
    elif busy == "paused":
        owner._transfer_pause[(*key, 0, 0)] = True
        owner._transfer_controls.register((*key, 0, 0), control, wire)
    elif busy in ("bound", "closed"):
        owner._active_transfer_sockets[key] = wire
        if busy == "closed":
            wire.close()
    elif busy in ("pending", "unresolved"):
        record = owner._transfer_handshakes.register(control, key[1])
        if busy == "unresolved":
            assert owner._transfer_handshakes.mark_sent(record)
            assert owner._transfer_handshakes.wait(record, 0) is None
    elif busy == "dead-control":
        control.close()
    else:
        owner._resume_sends[key] = {"lock": threading.RLock(), "users": 1, "targets": set()}
    assert _start_resume_send(owner, key, PEER, lambda row: control.sendall(b"unexpected"),
                              control_socket=control) is False
    assert not owner._resume_sends.get(key, {}).get("targets")
    if busy != "dead-control":
        assert control.fileno() >= 0


def test_ordinary_resume_atomically_reserves_eof_without_blocking_other_roles(ordinary_resume):
    owner, key, control, wire = ordinary_resume
    owner._transfer_pause[(*key, 0, 0)] = True
    owner._active_transfer_fids.add((PEER, 8))
    owner._active_transfer_sockets[7] = wire
    owner._transfer_controls.register((*key, 0, 1), control, wire)
    barrier, entered, release = threading.Barrier(2), threading.Event(), threading.Event()
    workers, rows = [], []

    def send(row):
        workers.append(threading.current_thread())
        rows.append(row)
        control.sendall(b"resume")
        entered.set()
        assert release.wait(5)

    def request():
        barrier.wait(timeout=5)
        return _start_resume_send(owner, key, PEER, send, control_socket=control)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(request) for _ in range(2)]
            outcomes = [result.result(timeout=5) for result in results]
        assert entered.wait(5)
        assert sorted(outcomes) == [False, True]
        wire.settimeout(5)
        with wire.makefile("rb") as stream:
            assert stream.read(6) == b"resume"
        assert rows == [owner.file_upload_progress[key]]
        assert _start_resume_send(owner, key, PEER, send, control_socket=control) is False
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive()
    assert owner._resume_sends == {}
    assert len(rows) == 1


@pytest.mark.parametrize("owner_type", [TCP_Server_Base, TCP_Client_Base])
def test_reconnect_waits_for_final_send_owner_after_its_socket_is_released(
    ordinary_resume, owner_type, monkeypatch
):
    owner, key, control, wire = ordinary_resume
    owner.file_recv_progress = {}
    waiting = threading.Event()
    real_wait = owner._transfer_progress_changed.wait

    def observe_wait(timeout=None):
        waiting.set()
        return real_wait(timeout)

    monkeypatch.setattr(owner._transfer_progress_changed, "wait", observe_wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with _sending_transfer(owner, key, control_socket=control):
            retired = _retire_transfer_session(owner, control)
            reconnect = pool.submit(_wait_transfer_session, owner, retired)
            assert waiting.wait(5)
            assert not reconnect.done()
            owner._active_transfer_sockets[key] = wire
            owner_type._release_transfer_socket(owner, key, control)
            assert owner._active_transfer_sockets[key] is wire
        assert reconnect.result(timeout=5) is None
    assert not owner._resume_sends
