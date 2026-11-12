"""Contracts for named local state deletion and discarded-progress lifetime."""

import json
import threading
from pathlib import Path

import pytest

from PyFlow.network_api import connect_tcp
from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base

FILES = ("file_trans_progress_file", "messages_log_file", "events_log_file",
         "sent_messages_log_file", "sent_events_log_file")
PEER = ("127.0.0.1", 50001)


@pytest.fixture(params=[TCP_Server_Base, TCP_Client_Base], ids=["server", "client"])
def instance(request, tmp_path):
    owner = request.param(
        host="127.0.0.1", port=0, is_extend_command=True,
        is_input_command_in_console=False, is_enable_encrypto=False, is_print_log=False,
    )
    for attr in FILES:
        setattr(owner, attr, str(tmp_path / Path(getattr(owner, attr)).name))
    yield owner
    owner._active_transfer_fids.clear()
    owner._active_transfer_sockets.clear()
    close(owner)


def close(owner):
    owner.stop() if isinstance(owner, TCP_Server_Base) else owner.close()


def populate(owner):
    for attr in FILES:
        Path(getattr(owner, attr)).write_text("{}", encoding="utf-8")


def test_list_is_read_only_and_takes_precedence_over_name(instance, tmp_path):
    populate(instance)
    extra = tmp_path / "setup.json"
    extra.write_text("do not delete", encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    instance.running = True
    try:
        names = instance.delete_config([], is_list_config=True)
        assert names == sorted(Path(getattr(instance, attr)).name for attr in FILES)
        assert instance.running is True
        assert {p: p.read_bytes() for p in before} == before
        with pytest.raises(RuntimeError):
            instance.delete_config(names[0])
    finally:
        instance.running = False


@pytest.mark.parametrize("attr", FILES)
def test_named_delete_leaves_every_other_file_and_repeated_close_intact(instance, attr):
    populate(instance)
    close(instance)
    paths = [Path(getattr(instance, field)) for field in FILES]
    selected = Path(getattr(instance, attr))
    untouched = {p: p.read_bytes() for p in paths if p != selected}
    assert instance.delete_config(selected.name) is True
    assert instance.delete_config(selected.name) is False
    close(instance)
    assert not selected.exists()
    assert {p: p.read_bytes() for p in untouched} == untouched


@pytest.mark.parametrize("name", [None, "", ".", "..", "../messages_log.json",
                                    "/messages_log.json", "C:\\messages_log.json",
                                    "*.json", "setup.json", "decode_command_table.json"])
def test_invalid_names_never_resolve_to_files(instance, name):
    populate(instance)
    before = {attr: Path(getattr(instance, attr)).read_bytes() for attr in FILES}
    with pytest.raises(ValueError):
        instance.delete_config(name)
    assert {attr: Path(getattr(instance, attr)).read_bytes() for attr in FILES} == before


def test_non_string_delete_name_is_rejected(instance):
    with pytest.raises(TypeError):
        instance.delete_config(["messages_log.json"])


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_nonregular_targets_are_not_listed_or_followed(instance, tmp_path, kind):
    path = Path(instance.messages_log_file)
    other = tmp_path / "protected.txt"
    other.write_bytes(b"protected content")
    if kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(other)
    assert path.name not in instance.delete_config(is_list_config=True)
    with pytest.raises(ValueError):
        instance.delete_config(path.name)
    assert other.read_bytes() == b"protected content"
    assert path.is_dir() if kind == "directory" else path.is_symlink()


def test_redirected_names_are_supported_but_ambiguous_names_are_refused(instance, tmp_path):
    path = tmp_path / "custom" / "progress.json"
    path.parent.mkdir()
    path.write_bytes(b"unreadable old progress is still removable")
    instance.file_trans_progress_file = str(path)
    assert instance.delete_config(is_list_config=True) == [path.name]
    assert instance.delete_config(path.name) is True
    first = tmp_path / "left" / "same.json"
    second = tmp_path / "right" / "same.json"
    for candidate in (first, second):
        candidate.parent.mkdir()
        candidate.write_bytes(b"keep")
    instance.messages_log_file, instance.events_log_file = str(first), str(second)
    with pytest.raises(ValueError):
        instance.delete_config("same.json")
    with pytest.raises(ValueError):
        instance.delete_config(is_list_config=True)
    assert first.read_bytes() == second.read_bytes() == b"keep"


def test_failed_unlink_does_not_disable_normal_progress_persistence(instance, monkeypatch):
    path = Path(instance.file_trans_progress_file)
    path.write_text("{}", encoding="utf-8")

    def denied(candidate):
        raise PermissionError("deletion denied")

    with monkeypatch.context() as patch:
        patch.setattr(connect_tcp.os, "unlink", denied)
        with pytest.raises(PermissionError, match="deletion denied"):
            instance.delete_config(path.name)
    assert path.read_text(encoding="utf-8") == "{}"
    instance.file_recv_progress[6] = [3, 10, None, "/recv/partial.bin", PEER]
    close(instance)
    assert json.loads(path.read_text())["download"]["6"] == [
        3, 10, None, "/recv/partial.bin", list(PEER)
    ]


def test_discard_cannot_be_undone_by_late_finish_reload_or_start(instance, monkeypatch):
    row = [3, 10, None, "/recv/partial.bin", PEER]
    instance.file_recv_progress[6] = row
    close(instance)
    path = Path(instance.file_trans_progress_file)
    assert instance.delete_config(path.name) is True
    instance._file_progress_finish(instance.file_recv_progress, 6, row, keep=True)
    close(instance)
    assert not path.exists()
    # A different writer's new snapshot must not be loaded or overwritten by this old object.
    other = '{"download":{"8":[1,2,null,"/other.bin",["127.0.0.1",50001]]}}'
    path.write_text(other, encoding="utf-8")
    assert instance._load_transfer_progress() == 0
    instance._persist_transfer_progress()
    assert 8 not in instance.file_recv_progress and path.read_text() == other

    def no_network(*args, **kwargs):
        raise AssertionError("discarded object attempted network I/O")

    monkeypatch.setattr(connect_tcp.socket, "socket", no_network)
    entries = [instance.start_TCP_Server] if isinstance(instance, TCP_Server_Base) else [
        instance.connect, instance.start_TCP_client
    ]
    for start in entries:
        with pytest.raises(RuntimeError, match="new instance"):
            start()


def test_missing_progress_is_an_explicit_discard_too(instance):
    path = Path(instance.file_trans_progress_file)
    assert instance.delete_config(path.name) is False
    instance.file_recv_progress[6] = [3, 10, None, "/recv/partial.bin", PEER]
    close(instance)
    assert not path.exists()


def test_closing_in_progress_rejects_delete_until_successful_completion(instance, monkeypatch):
    populate(instance)
    entered, release = threading.Event(), threading.Event()
    real_free = instance.free_port

    def held_free():
        entered.set()
        assert release.wait(timeout=5)
        return real_free()

    monkeypatch.setattr(instance, "free_port", held_free)
    worker = threading.Thread(target=close, args=(instance,))
    worker.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(RuntimeError):
            instance.delete_config(Path(instance.file_trans_progress_file).name)
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert instance.delete_config(Path(instance.file_trans_progress_file).name) is True


def test_active_transfers_relays_and_pending_logs_prevent_deletion(instance):
    populate(instance)
    close(instance)
    name = Path(instance.file_trans_progress_file).name
    instance._active_transfer_fids.add(6)
    with pytest.raises(RuntimeError):
        instance.delete_config(name)
    instance._active_transfer_fids.clear()
    instance._active_transfer_sockets[6] = object()
    with pytest.raises(RuntimeError):
        instance.delete_config(name)
    instance._active_transfer_sockets.clear()
    if isinstance(instance, TCP_Server_Base):
        instance._forward_relays[6] = {}
    else:
        instance._resume_sends[(PEER, 6)] = {
            "lock": threading.RLock(), "users": 1, "targets": {PEER},
        }
    try:
        with pytest.raises(RuntimeError):
            instance.delete_config(name)
    finally:
        if isinstance(instance, TCP_Server_Base):
            instance._forward_relays.clear()
        else:
            instance._resume_sends.clear()
    instance.messages_dict["peer"] = [["late message", "now"]]
    with pytest.raises(RuntimeError):
        instance.delete_config(Path(instance.messages_log_file).name)
    close(instance)
    assert instance.delete_config(Path(instance.messages_log_file).name) is True


@pytest.mark.parametrize("users,targets", [(0, set()), (1, set()), (0, {PEER})])
def test_transient_resume_state_prevents_deletion_until_released(instance, users, targets):
    populate(instance)
    close(instance)
    path = Path(instance.file_trans_progress_file)
    before = path.read_bytes()
    key = (PEER, 6)
    state = {"lock": threading.RLock(), "users": users, "targets": targets}
    instance._resume_sends[key] = state
    try:
        with pytest.raises(RuntimeError):
            instance.delete_config(path.name)
        assert path.read_bytes() == before
        assert instance._resume_sends[key] is state
    finally:
        instance._resume_sends.clear()
    assert instance.delete_config(path.name) is True


def test_delete_waits_for_inflight_progress_publication(instance, monkeypatch):
    instance.file_recv_progress[6] = [3, 10, None, "/recv/partial.bin", PEER]
    close(instance)
    path = Path(instance.file_trans_progress_file)
    entered, release = threading.Event(), threading.Event()
    real_replace = connect_tcp.os.replace
    outcomes = []

    def held_replace(source, destination):
        if destination == str(path):
            entered.set()
            assert release.wait(timeout=5)
        return real_replace(source, destination)

    monkeypatch.setattr(connect_tcp.os, "replace", held_replace)
    writer = threading.Thread(target=instance._persist_transfer_progress)
    deleter = threading.Thread(target=lambda: outcomes.append(instance.delete_config(path.name)))
    writer.start()
    try:
        assert entered.wait(timeout=5)
        deleter.start()
    finally:
        release.set()
        writer.join(timeout=5)
        if deleter.ident is not None:
            deleter.join(timeout=5)
    assert not writer.is_alive() and not deleter.is_alive()
    assert outcomes == [True]
    close(instance)
    assert not path.exists()
