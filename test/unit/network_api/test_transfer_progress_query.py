"""Behavioral contracts for detached, local-only transfer inspection."""

import json
from pathlib import Path

import pytest

from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base

PEER_A = ("127.0.0.1", 50001)
PEER_B = ("127.0.0.1", 50002)
ORIGIN = ("127.0.0.1", 50003)


@pytest.fixture(params=[TCP_Server_Base, TCP_Client_Base], ids=["server", "client"])
def instance(request, tmp_path):
    owner = request.param(
        host="127.0.0.1", port=0, is_extend_command=True,
        is_input_command_in_console=False, is_enable_encrypto=False, is_print_log=False,
    )
    for attr in ("messages_log_file", "events_log_file", "sent_messages_log_file",
                 "sent_events_log_file", "file_trans_progress_file"):
        setattr(owner, attr, str(tmp_path / f"{attr}.json"))
    try:
        yield owner
    finally:
        owner.file_trans_progress_file = str(tmp_path / "teardown_progress.json")
        owner.stop() if isinstance(owner, TCP_Server_Base) else owner.close()


def test_scoped_rows_keep_all_local_states_separate(instance):
    instance.file_upload_progress.update({
        (PEER_A, 8): [4, 10, 2.0, "/send/a.bin", ORIGIN],
        (PEER_B, 8): [10, 10, None, "/send/b.bin", PEER_B],
    })
    instance.file_recv_progress[8] = [3, 20, None, "/recv/a.bin", PEER_A]
    instance._active_transfer_fids.add((PEER_A, 8))
    instance._active_transfer_sockets[8] = object()
    instance._transfer_pause[(PEER_A, 8)] = True
    view = instance.get_transfer_progress(include_persisted=False)
    rows = {tuple(row["receiver"]): row for row in view["memory"]["uploading"]}
    assert set(rows) == {PEER_A, PEER_B}
    assert rows[PEER_A]["peer"] == list(ORIGIN)
    assert rows[PEER_A]["active"] is True and rows[PEER_A]["paused"] is True
    assert rows[PEER_A]["socket_registered"] is False
    assert rows[PEER_B]["transferred"] == rows[PEER_B]["total"] == 10
    assert rows[PEER_B]["active"] is False and rows[PEER_B]["paused"] is False
    download, = view["memory"]["downloading"]
    assert download["fid"] == 8 and download["transferred"] == 3
    assert download["receiver"] is None and download["paused"] is None
    assert download["active"] is False and download["socket_registered"] is True
    assert instance.running is False


def test_memory_disk_and_returned_mutations_do_not_change_recovery_state(instance):
    instance.file_recv_progress[9] = [100, 100, None, "/recv/current.bin", PEER_A]
    if isinstance(instance, TCP_Server_Base):
        instance._register_fid_route(11, ORIGIN, {PEER_A: 12, PEER_B: 12}, (PEER_A,))
    else:
        instance.file_upload_progress[(PEER_A, 11)] = [
            100, 100, None, "/send/source.bin", PEER_B,
        ]
    original = instance._transfer_progress_snapshot()
    disk = {"upload": {}, "download": {"9": [20, 100, None, "/recv/old.bin", list(PEER_A)],
                                       "10": [5, 50, None, "/recv/disk_only.bin", list(PEER_B)]},
            "relays": original["relays"]}
    path = Path(instance.file_trans_progress_file)
    path.write_text(json.dumps(disk), encoding="utf-8")
    before = path.read_bytes()
    view = instance.get_transfer_progress()
    current, = view["memory"]["downloading"]
    assert current["path"] == "/recv/current.bin" and current["transferred"] == 100
    saved = {row["fid"]: row for row in view["persisted"]["snapshot"]["downloading"]}
    assert set(saved) == {9, 10} and saved[9]["transferred"] == 20
    assert saved[10]["path"] == "/recv/disk_only.bin"
    assert all(saved[9][field] is None for field in ("active", "socket_registered", "paused"))
    if isinstance(instance, TCP_Server_Base):
        relay, = view["memory"]["relays"]
        assert relay["fid"] == 11
        assert relay["completed"] == [list(PEER_A)]
        assert relay["targets"] == [[list(PEER_A), 12], [list(PEER_B), 12]]
        assert "path" not in relay
        relay["targets"].clear()
    else:
        assert view["memory"]["relays"] == []
        assert view["persisted"]["snapshot"]["relays"] == []
        assert original["relays"] == {}
        upload, = view["memory"]["uploading"]
        assert upload["fid"] == 11
        assert upload["receiver"] == list(PEER_A)
        assert upload["peer"] == list(PEER_B)
        assert upload["path"] == "/send/source.bin"
        upload["path"] = "changed"
    json.dumps(view)
    current["peer"][0] = "changed"
    saved[10]["path"] = "changed"
    assert instance._transfer_progress_snapshot() == original
    assert path.read_bytes() == before
    assert 10 not in instance.file_recv_progress
    assert not instance._active_transfer_fids and not instance._active_transfer_sockets


def test_absent_empty_and_unrequested_disk_are_distinct(instance, monkeypatch):
    path = Path(instance.file_trans_progress_file)
    assert instance.get_transfer_progress()["persisted"]["status"] == "missing"
    path.write_text('{"upload":{},"download":{},"relays":{}}', encoding="utf-8")
    empty = instance.get_transfer_progress()["persisted"]
    assert empty["status"] == "ok" and empty["snapshot"] == {
        "uploading": [], "downloading": [], "relays": []
    }

    def reject_io(*args, **kwargs):
        raise AssertionError("include_persisted=False performed file I/O")

    monkeypatch.setattr("builtins.open", reject_io)
    skipped = instance.get_transfer_progress(include_persisted=False)["persisted"]
    assert skipped["status"] == "not_requested"
    assert skipped["snapshot"] is None and skipped["error"] is None
    assert skipped["path"] == str(path)


@pytest.mark.parametrize("text", [
    "{", "[]", '{"upload":{"3":[]}}', '{"download":{"3":[1]}}',
    '{"download":[1]}', '{"relays":{"3":[]}}',
])
def test_bad_snapshots_remain_intact_and_do_not_hide_memory(instance, text):
    path = Path(instance.file_trans_progress_file)
    path.write_text(text, encoding="utf-8")
    instance.file_recv_progress[2] = [7, 9, None, "/recv/kept.bin", PEER_A]
    view = instance.get_transfer_progress()
    assert view["persisted"]["status"] == "error"
    assert view["persisted"]["snapshot"] is None and view["persisted"]["error"]
    assert view["memory"]["downloading"][0]["path"] == "/recv/kept.bin"
    assert path.read_text(encoding="utf-8") == text


def test_read_failure_is_visible_without_touching_live_records(instance, monkeypatch):
    instance.file_recv_progress[2] = [7, 9, None, "/recv/kept.bin", PEER_A]

    def denied(*args, **kwargs):
        raise PermissionError("inspection access denied")

    monkeypatch.setattr("builtins.open", denied)
    view = instance.get_transfer_progress()
    assert view["persisted"]["status"] == "error"
    assert "inspection access denied" in view["persisted"]["error"]
    assert view["memory"]["downloading"][0]["transferred"] == 7


def test_disabled_tracking_reports_existing_records_but_no_live_speed(instance):
    instance.file_recv_progress[2] = [9, 9, 100.0, "/recv/retained.bin", PEER_A]
    instance.is_enable_file_transfer_progress_bar = False
    instance.is_enable_real_time_network_speed = False
    view = instance.get_transfer_progress(include_persisted=False)
    assert view["progress_enabled"] is False and view["speed_enabled"] is False
    row, = view["memory"]["downloading"]
    assert row["path"] == "/recv/retained.bin" and row["total"] == row["transferred"] == 9
    assert row["speed"] is None and row["active"] is False
    assert instance.file_recv_progress[2][2] == 100.0


def test_client_legacy_relay_file_is_rejected_without_losing_original_bytes(tmp_path):
    client = TCP_Client_Base(
        host="127.0.0.1", port=0, is_extend_command=True,
        is_input_command_in_console=False, is_enable_encrypto=False, is_print_log=False,
    )
    for attr in ("messages_log_file", "events_log_file", "sent_messages_log_file",
                 "sent_events_log_file", "file_trans_progress_file"):
        setattr(client, attr, str(tmp_path / f"legacy_{attr}.json"))
    path = Path(client.file_trans_progress_file)
    original = json.dumps({"upload": {}, "download": {}, "relays": {
        "302": {"path": "/old/source.bin", "targets": [list(PEER_A)]},
    }}).encode()
    path.write_bytes(original)
    try:
        assert client._load_transfer_progress() == 0
        assert client._transfer_progress_snapshot() == {
            "upload": {}, "download": {}, "relays": {},
        }
        assert client.get_transfer_progress()["persisted"]["status"] == "error"
        client.file_recv_progress[304] = [1, 2, None, "/recv/current.bin", PEER_B]
        client._persist_transfer_progress()
        assert path.read_bytes() == original
        client.close()
        assert path.read_bytes() == original
    finally:
        client.close()
