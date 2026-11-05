"""Contract tests for transient transfer-handshake correlation."""

import socket

import pytest

from PyFlow.network_api.connect_tcp import (
    _TransferHandshakeRegistry,
    _parse_transfer_port_announcement,
)



class _Control:
    pass



def test_port_replies_match_socket_and_fid_independently_of_order():
    registry = _TransferHandshakeRegistry()
    control, other_control = _Control(), _Control()
    first, second = registry.register(control, 17), registry.register(control, 18)
    assert first["key"] == (control, 17)
    assert second["key"] == (control, 18)
    assert registry.mark_sent(first)
    assert not registry.mark_sent(first)
    assert registry.mark_sent(second)
    assert registry.resolve_port(control, 18, 41002, "enc:second")
    assert not registry.resolve_port(other_control, 17, 41001, "enc:wrong-socket")
    assert not registry.resolve_port(control, 19, 41001, "enc:wrong-fid")
    assert registry.resolve_port(control, 17, 41001, "enc:first")
    assert not registry.resolve_port(control, 17, 41003, "enc:duplicate")
    assert registry.wait_many((first, second), 0) == {
        (control, 17): ("port", 17, 41001, "enc:first"),
        (control, 18): ("port", 18, 41002, "enc:second"),
    }
    assert not registry.resolve_port(control, 17, 41003, "enc:late")
    assert registry._pending == {}


def test_pre_send_cancellation_does_not_make_fid_unresolved():
    registry = _TransferHandshakeRegistry()
    control = object()
    cancelled = registry.register(control, 17)
    registry.cancel(cancelled)
    assert not registry.mark_sent(cancelled)
    reusable = registry.register(control, 17)
    assert reusable is not None
    assert registry.mark_sent(reusable)
    assert registry.resolve_error(control, 17, "listener refused")
    assert registry.wait(reusable, 0) == ("error", "listener refused")


def test_sent_cancellation_and_timeout_leave_fid_unresolved_until_disconnect():
    registry = _TransferHandshakeRegistry()
    control = _Control()
    cancelled = registry.register(control, 17)
    assert registry.mark_sent(cancelled)
    registry.cancel(cancelled)
    assert registry.register(control, 17) is None
    assert not registry.resolve_port(control, 17, 42001, "plain")

    timed_out = registry.register(control, 18)
    assert registry.mark_sent(timed_out)
    assert registry.wait(timed_out, 0) is None
    assert registry.register(control, 18) is None
    assert not registry.resolve_error(control, 18, "late error")
    registry.cancel_socket(control)
    assert (control, 17) not in registry._unresolved
    assert (control, 18) not in registry._unresolved
    assert registry.register(control, 17) is None


def test_terminal_reply_wins_atomically_over_timeout_or_cancel():
    registry = _TransferHandshakeRegistry()
    control = _Control()
    record = registry.register(control, 17)
    assert registry.mark_sent(record)
    assert registry.resolve_port(control, 17, 43001, "enc:pair")
    assert registry.wait(record, 0) == ("port", 17, 43001, "enc:pair")
    registry.cancel(record)
    assert registry.register(control, 17) is not None


def test_old_record_cannot_cancel_a_later_same_key_registration():
    registry = _TransferHandshakeRegistry()
    control = _Control()
    old = registry.register(control, 17)
    registry.cancel(old)
    new = registry.register(control, 17)
    assert new is not None and new is not old
    registry.cancel(old)
    assert registry.mark_sent(new)
    assert registry.resolve_error(control, 17, "new attempt")
    assert registry.wait(new, 0) == ("error", "new attempt")


def test_wait_many_classifies_each_target_and_keeps_completed_result_reusable():
    registry = _TransferHandshakeRegistry()
    control = _Control()
    done = registry.register(control, 17)
    pending = registry.register(control, 18)
    assert registry.mark_sent(done)
    assert registry.mark_sent(pending)
    assert registry.resolve_error(control, 17, "one target refused")
    assert registry.wait_many((done, pending), 0) is None
    assert registry.register(control, 17) is not None
    assert registry.register(control, 18) is None


def test_disconnect_cancels_only_its_socket_requests_and_resources():
    registry = _TransferHandshakeRegistry()
    old_control, new_control = _Control(), _Control()
    first = registry.register(old_control, 17)
    second = registry.register(new_control, 17)
    assert registry.mark_sent(first)
    assert registry.mark_sent(second)
    left, right = socket.socketpair()
    assert registry.track_socket(old_control, left)
    registry.cancel_socket(old_control)
    assert left.fileno() == -1
    right.close()
    assert not registry.resolve_port(old_control, 17, 43001, "plain")
    assert registry.resolve_port(new_control, 17, 43002, "plain")
    assert registry.wait(first, 0) is None
    assert registry.wait(second, 0) == ("port", 17, 43002, "plain")


def test_reconnect_socket_allows_same_fid_after_old_session_is_retired():
    registry = _TransferHandshakeRegistry()
    old_control, new_control = _Control(), _Control()
    old = registry.register(old_control, 17)
    assert registry.mark_sent(old)
    assert registry.wait(old, 0) is None
    registry.cancel_socket(old_control)
    new = registry.register(new_control, 17)
    assert new is not None
    assert registry.mark_sent(new)
    assert not registry.resolve_port(old_control, 17, 44001, "old")
    assert registry.resolve_port(new_control, 17, 44002, "new")
    assert registry.wait(new, 0) == ("port", 17, 44002, "new")


def test_port_announcement_keeps_port_and_stamp_pair():
    assert _parse_transfer_port_announcement(
        "/server_file_transfer_port 40000 17 enc:stamp"
    ) == (40000, 17, "enc:stamp")

@pytest.mark.parametrize("line", [
    "/server_file_transfer_port 40000 17",
    "/server_file_transfer_port 0 17 plain",
    "/forward_upload 17 40000 plain",
    "/server_file_transfer_port 40000 17 plain " + "a" * 32,
    "/server_file_transfer_port 40000 -1 plain",
    "/server_file_transfer_port 40000 17 plain invalid",
])
def test_old_or_malformed_listener_replies_are_rejected(line):
    assert _parse_transfer_port_announcement(line) is None
