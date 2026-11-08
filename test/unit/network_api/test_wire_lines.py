"""Transfer signs survive read boundaries without retaining consumed history."""

import socket
import threading

import pytest

from PyFlow.network_api.connect_tcp import (
    TRANSFER_SIGN_BUFFER_LIMIT,
    TCP_Server_Base,
    _TransferSignBuffer,
)

START = "/FILE_SEND_SERVER_START_FILE_TRANSFER"
HEADER = "/FILE_RECEIVE_CLIENT_HEADER"
ACK = "/FILE_RECEIVE_CLIENT_DATA"
ERROR = "/FER"


def _buffer():
    return _TransferSignBuffer(START, ACK, ERROR)


def test_coalesced_signs_and_unterminated_tail_survive_every_read_boundary():
    wire = f"\r\n {START} \r\n\n{HEADER}\n{ACK}\n{ERROR}".encode()
    for boundary in range(len(wire) + 1):
        signs = _buffer()
        signs.feed(wire[:boundary])
        signs.consume()
        signs.feed(wire[boundary:])
        signs.consume()
        assert signs.start_seen and signs.ack_seen, boundary
        assert not signs.error_seen, boundary
        assert signs.pending == [] and signs.pending_bytes == 0, boundary
        assert signs.partial == ERROR.encode(), boundary
        assert signs.feed(b"\n") is True
        signs.consume()
        assert signs.error_seen, boundary


def test_fragmented_ack_is_not_delivered_before_its_newline():
    signs = _buffer()
    for byte in ACK.encode():
        signs.feed(bytes([byte]))
        signs.consume()
        assert not signs.ack_seen
    assert signs.partial == ACK.encode()
    signs.feed(b"\n")
    signs.consume()
    assert signs.ack_seen and not signs.partial
    assert signs.pending == [] and signs.pending_bytes == 0


def test_scanning_records_at_capacity_latches_results_before_deleting_them():
    signs = _buffer()
    signs.feed(f"{HEADER}\n{ACK}\n{ERROR}\n".encode())
    signs.consume()
    assert signs.ack_seen and signs.error_seen
    assert signs.pending == [] and signs.pending_bytes == 0


@pytest.mark.parametrize("suffix", [b"", b"\n", b"\n" + ACK.encode() + b"\n"])
def test_oversize_line_is_rejected_without_reinterpreting_its_tail(suffix):
    signs = _buffer()
    signs.feed(b"x" * (TRANSFER_SIGN_BUFFER_LIMIT - 1))
    with pytest.raises(ValueError, match="64 KiB"):
        signs.feed(b"x" + suffix)
    signs.consume()
    assert not signs.ack_seen
    assert len(signs.partial) == TRANSFER_SIGN_BUFFER_LIMIT - 1
    assert signs.pending_bytes == 0


def test_pending_quota_batch_scans_complete_lines_and_preserves_flags():
    signs = _buffer()
    start_line = START.encode() + b"\n"
    filler_size = TRANSFER_SIGN_BUFFER_LIMIT - len(start_line)
    assert filler_size % 2 == 0
    signs.feed(start_line + b"x\n" * (filler_size // 2))
    assert signs.pending == [] and signs.pending_bytes == 0 and not signs.partial
    assert signs.start_seen
    signs.feed(ACK.encode() + b"\n")
    _, ack_seen, _ = signs.consume()
    assert ack_seen


def test_partial_tail_at_total_quota_scans_all_complete_records():
    signs = _buffer()
    queued_size = TRANSFER_SIGN_BUFFER_LIMIT - 5
    queued_line = START.encode() + b" " * (queued_size - len(START) - 1) + b"\n"
    signs.feed(queued_line + b"tail!")
    assert signs.start_seen
    assert signs.pending == [] and signs.pending_bytes == 0
    assert signs.partial == b"tail!"

def test_pending_quota_compacts_before_appending_to_an_incomplete_tail():
    signs = _buffer()
    first_line = b" x " + b" " * (TRANSFER_SIGN_BUFFER_LIMIT - 8) + b"\n"
    signs.feed(first_line + b"abc")
    assert signs.pending_bytes + len(signs.partial) == TRANSFER_SIGN_BUFFER_LIMIT - 1
    signs.feed(b"de")
    assert signs.pending_bytes == 0 and signs.partial == b"abcde"
    signs.feed(b"\n" + ACK.encode() + b"\n")
    _, ack_seen, _ = signs.consume()
    assert ack_seen


def test_complete_short_line_batches_compact_without_failing_or_waiting():
    signs = _buffer()
    signs.feed(b"x\n" * TRANSFER_SIGN_BUFFER_LIMIT)
    assert signs.pending_bytes == 0 and signs.pending == []
    assert not signs.ack_seen
    signs.feed(ACK.encode() + b"\n")
    _, ack_seen, _ = signs.consume()
    assert ack_seen and signs.pending_bytes == 0


def test_exact_limit_complete_line_is_accepted_and_scanned():
    signs = _buffer()
    full_line = START.encode() + b" " * (TRANSFER_SIGN_BUFFER_LIMIT - len(START) - 1) + b"\n"
    signs.feed(full_line)
    assert signs.pending_bytes == 0 and signs.pending == []
    assert signs.start_seen

def test_many_consumed_batches_do_not_exhaust_the_pending_quota():
    signs = _buffer()
    for _ in range(4):
        signs.feed(b"x\n" * (TRANSFER_SIGN_BUFFER_LIMIT // 2))
        signs.consume()
        assert signs.pending_bytes == 0 and signs.pending == []
    signs.feed(f"{START}\n{ACK}\n".encode())
    signs.consume()
    assert signs.start_seen and signs.ack_seen


def test_large_read_of_empty_lines_does_not_become_a_single_oversize_line():
    signs = _buffer()
    signs.feed(b"\n" * (2 * TRANSFER_SIGN_BUFFER_LIMIT) + ACK.encode() + b"\n")
    signs.consume()
    assert signs.ack_seen
    assert not signs.partial and signs.pending_bytes == 0


def test_producer_and_consumer_do_not_lose_ack_across_prefix_deletion():
    signs = _buffer()
    batches = [HEADER, START, ACK, ERROR]
    handoff = threading.Barrier(2, timeout=5)
    failures = []

    def produce():
        try:
            for line in batches:
                signs.feed((line + "\n").encode())
                handoff.wait()
                handoff.wait()
        except Exception as exc:
            failures.append(exc)
            handoff.abort()

    worker = threading.Thread(target=produce)
    worker.start()
    try:
        for _ in batches:
            handoff.wait()
            signs.consume()
            assert signs.pending == [] and signs.pending_bytes == 0
            handoff.wait()
    except BaseException:
        handoff.abort()
        raise
    finally:
        worker.join(timeout=5)
    assert not worker.is_alive() and not failures
    assert signs.start_seen and signs.ack_seen and signs.error_seen


@pytest.fixture
def relay_server(tmp_path):
    server = TCP_Server_Base(
        host="127.0.0.1", port=0, is_extend_command=True,
        is_input_command_in_console=False, is_enable_encrypto=False, is_print_log=False,
    )
    server.file_trans_progress_file = str(tmp_path / "relay_progress.json")
    try:
        yield server
    finally:
        server.stop()


def _relay_exchange(server, monkeypatch, wire, marker, read_size):
    reader, writer = socket.socketpair()
    previous_timeout = 1.25
    reader.settimeout(previous_timeout)
    real_receive = server.receive_message

    def write_peer():
        try:
            writer.sendall(wire)
        except OSError:
            pass  # an error marker may make the relay close before the tail
        finally:
            writer.close()

    def coalesce(sock, length):
        data = bytearray()
        while len(data) < length:
            chunk = real_receive(sock, length - len(data))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    worker = threading.Thread(target=write_peer)
    worker.start()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(server, "recv_messages_chunk_len", read_size)
            patch.setattr(server, "receive_message", coalesce)
            result = server._forward_read_sign(reader, marker, timeout=2)
        assert reader.gettimeout() == previous_timeout
        return result
    finally:
        reader.close()
        worker.join(timeout=5)
        assert not worker.is_alive()


@pytest.mark.parametrize("marker", [START, ACK])
def test_relay_markers_survive_every_read_split(relay_server, monkeypatch, marker):
    wire = marker.encode()
    for split in range(1, len(wire)):
        assert _relay_exchange(relay_server, monkeypatch, wire, marker, split) == wire


@pytest.mark.parametrize("marker", [START, ACK])
def test_relay_markers_survive_noise_and_scan_window_boundaries(
    relay_server, monkeypatch, marker
):
    wire = (
        b"x" * (3 * TRANSFER_SIGN_BUFFER_LIMIT - len(marker) // 2)
        + marker.encode() + b"\n"
    )
    assert _relay_exchange(relay_server, monkeypatch, wire, marker, len(wire)) == marker.encode()


def test_relay_error_later_in_the_same_large_read_still_rejects_ack(
    relay_server, monkeypatch
):
    wire = ACK.encode() + b"x" * (2 * TRANSFER_SIGN_BUFFER_LIMIT) + ERROR.encode()
    assert _relay_exchange(relay_server, monkeypatch, wire, ACK, len(wire)) is None


def test_relay_error_split_across_reads_rejects_ack(relay_server, monkeypatch):
    wire = ERROR.encode() + ACK.encode()
    for split in range(1, len(ERROR)):
        assert _relay_exchange(relay_server, monkeypatch, wire, ACK, split) is None
