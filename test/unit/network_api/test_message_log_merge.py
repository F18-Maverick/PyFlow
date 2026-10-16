"""Unit tests for the message/event JSON log merge (connect_tcp.py).

Several instances — a server and a client on one machine, or two of either —
default to the same ``messages_log.json`` / ``events_log.json`` under the
project directory, so their flushes of one file overlap. A flush must then
neither corrupt the log (the write used to share one ``<path>.tmp``, so one
writer could publish a half-written file, after which every later flush failed
with "Extra data") nor be dropped (a writer whose merge loses the race to
another *process* redoes it instead of clobbering that process's entries).

No sockets and no crypto library are needed: the merge works on a temp path
and the snapshots are plain dicts. ``cross_process`` swaps the in-process lock
for a fresh one per call, which lets a rival publish in the middle of a merge
on one thread — the interleaving that only another process can produce in
production.
"""

import json
import os
import threading

import pytest

from PyFlow.network_api import connect_tcp
from PyFlow.network_api.connect_tcp import (
    LOG_MERGE_ATTEMPTS,
    TCP_Client_Base,
    TCP_Server_Base,
)


@pytest.fixture
def cross_process(monkeypatch):
    """Make every publish look like it comes from its own process."""

    def no_exclusion(path):  # noqa: ARG001
        return threading.RLock()

    monkeypatch.setattr(connect_tcp, "_log_merge_lock", no_exclusion)


def _instance(cls):
    """One real instance (nothing started) recording its merge failures.

    ``_record_key`` is reduced to the snapshot key itself, so a snapshot of
    ``{"peer": [...]}`` lands under the key ``"peer"``.
    """
    instance = cls(host="127.0.0.1", port=65341, is_extend_command=True)
    instance._record_key = str
    instance.failures = []
    instance._log = lambda *parts: instance.failures.append(" ".join(map(str, parts)))
    instance._log_exc = lambda: None  # failures are asserted through ``failures``
    return instance


def _raced_instance(cls, rival, path, rival_snapshot, every_attempt=False):
    """Return an instance whose merge is interrupted by ``rival`` publishing.

    ``_record_key`` runs between a merge's read and its publish, so a rival
    publishing there is exactly the interleaving that used to lose a flush:
    the rival's write lands after this writer read the log and before this
    writer replaced it. With ``every_attempt`` the rival publishes on every
    attempt, under a fresh key each time so the result stays readable.
    """
    instance = _instance(cls)
    published = []

    def key(sock):
        if every_attempt or not published:
            published.append(sock)
            snapshot = rival_snapshot
            if every_attempt:
                snapshot = {f"rival-peer-{len(published)}": [["from the rival", "t"]]}
            rival._merge_json_log(path, snapshot)
        return str(sock)

    instance._record_key = key
    return instance


def _log_of(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_stale_merge_is_redone_instead_of_dropped(tmp_path, cross_process):
    """A writer that merged a replaced version redoes the merge.

    Both directions are covered (each class delegates to the same publish):
    whichever instance is the stale one — the one another process published
    over — both snapshots end up in the log.
    """
    mine = {"stale-peer": [["from the stale writer", "t"]]}
    theirs = {"rival-peer": [["from the rival", "t"]]}

    for stale_cls, rival_cls in (
        (TCP_Server_Base, TCP_Client_Base),
        (TCP_Client_Base, TCP_Server_Base),
    ):
        path = str(tmp_path / f"{stale_cls.__name__}.json")
        stale = _raced_instance(stale_cls, _instance(rival_cls), path, theirs)

        stale._merge_json_log(path, mine)

        assert not stale.failures
        assert _log_of(path) == {**theirs, **mine}


def test_concurrent_flushes_of_two_instances_keep_every_entry(tmp_path):
    """Two instances flushing one log at the same time lose nothing.

    The merges of one process are serialised, so every entry of both
    instances must be in the log and no failure may be reported, whatever the
    thread schedule.
    """
    path = str(tmp_path / "messages_log.json")
    server, client = _instance(TCP_Server_Base), _instance(TCP_Client_Base)
    per_instance = 25

    def flush(instance, name):
        for index in range(per_instance):
            instance._merge_json_log(path, {f"{name}-{index}": [[f"{name} {index}", "t"]]})

    threads = [
        threading.Thread(target=flush, args=(server, "server")),
        threading.Thread(target=flush, args=(client, "client")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not server.failures and not client.failures
    log = _log_of(path)
    assert len(log) == 2 * per_instance
    for name in ("server", "client"):
        for index in range(per_instance):
            assert log[f"{name}-{index}"] == [[f"{name} {index}", "t"]]


def test_log_with_a_trailing_fragment_is_merged_and_repaired(tmp_path):
    """A log left with a trailing fragment is merged into, not abandoned.

    That fragment is what the shared temp path produced: the file no longer
    parsed, so every flush was dropped. The first document is kept and the
    file published by the merge parses again.
    """
    path = tmp_path / "messages_log.json"
    path.write_text(
        json.dumps({"old": [["kept", "t"]]}) + '\n{"half written',
        encoding="utf-8",
    )
    instance = _instance(TCP_Server_Base)

    instance._merge_json_log(str(path), {"new": [["added", "t"]]})

    assert not instance.failures
    assert _log_of(str(path)) == {"old": [["kept", "t"]], "new": [["added", "t"]]}


def test_empty_log_is_merged_and_repaired(tmp_path):
    """A 0-byte log — what a publish cut short left behind — is merged into.

    Every flush used to fail on it ("Expecting value"), so the log stayed
    empty for good.
    """
    path = tmp_path / "messages_log.json"
    path.write_text("", encoding="utf-8")
    instance = _instance(TCP_Server_Base)

    instance._merge_json_log(str(path), {"new": [["added", "t"]]})

    assert not instance.failures
    assert _log_of(str(path)) == {"new": [["added", "t"]]}


def test_no_temp_file_is_left_behind(tmp_path, cross_process):
    """Every publish cleans its temp file up, the retry path included."""
    path = str(tmp_path / "messages_log.json")
    stale = _raced_instance(
        TCP_Server_Base,
        _instance(TCP_Client_Base),
        path,
        {"rival-peer": [["from the rival", "t"]]},
    )

    stale._merge_json_log(path, {"stale-peer": [["from the stale writer", "t"]]})

    assert not stale.failures
    assert sorted(os.listdir(tmp_path)) == ["messages_log.json"]


def test_a_log_that_keeps_changing_is_reported_not_raised(tmp_path, cross_process):
    """Losing every attempt reports a failure instead of raising.

    The flush is dropped (visibly, in the log) and the other writer's data is
    left untouched, so the flushing thread and the log survive either way.
    """
    path = str(tmp_path / "messages_log.json")
    rival = _instance(TCP_Client_Base)
    stale = _raced_instance(
        TCP_Server_Base,
        rival,
        path,
        rival_snapshot=None,
        every_attempt=True,
    )

    stale._merge_json_log(path, {"stale-peer": [["from the stale writer", "t"]]})

    assert any("kept changing" in failure for failure in stale.failures)
    assert not rival.failures
    assert sorted(_log_of(path)) == [f"rival-peer-{n}" for n in range(1, LOG_MERGE_ATTEMPTS + 1)]
    assert sorted(os.listdir(tmp_path)) == ["messages_log.json"]
