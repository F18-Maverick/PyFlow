"""Unit tests for _SecureChannel's receive contract (connect_tcp.py).

The channel parses socket bytes while it is framed and reports them to the
line reader while it is not. Frame bytes are consumed in both modes: a peer
that has not processed a re-exchange yet keeps sending frames, and a stale
frame must neither surface as a message nor be mistaken for plaintext. These
tests need no sockets and no crypto library: the frames they build are never
decrypted (the sections they exercise run in raw mode) except where a stub
session supplies the plaintext.
"""

from types import SimpleNamespace

import pytest

from PyFlow.network_api.connect_tcp import (
    FRAME_HEADER_LEN,
    FRAME_MAGIC,
    FRAME_TYPE_ECDH,
    FRAME_TYPE_RSA,
    MAX_FRAME_BODY,
    _FrameError,
    _SecureChannel,
)


def _frame(frame_type, body):
    """Build one wire frame exactly like ``_SecureChannel._wrap`` does."""
    return FRAME_MAGIC + bytes([frame_type]) + len(body).to_bytes(4, "big") + body


def _channel(framed=False, rsa=None):
    """Build a channel over a sink socket; ``framed`` flips it first."""
    session = SimpleNamespace(rsa=rsa, ecdh=None, peer_ecdh_pub=lambda timeout=0: None)
    channel = _SecureChannel(
        SimpleNamespace(sendall=lambda data: None),
        session,
        log=lambda *args: None,
        debug=lambda *args: None,
    )
    if framed:
        channel.enable("nonce", "nonce")
    return channel


def test_raw_plaintext_is_reported():
    """Raw mode hands plaintext bytes back to the line reader."""
    channel = _channel()

    assert channel.feed(b"hello\n") == b"hello\n"
    assert channel.feed(b"again") == b"again"  # partial lines pass through too
    assert channel.take(4096) == b""


def test_raw_stale_frame_is_dropped():
    """Drop a frame a still-framed peer sends while this side is raw.

    The frame must land neither as plaintext nor as a message: it is
    consumed, and the plaintext behind it comes through.
    """
    channel = _channel()
    stale = _frame(FRAME_TYPE_ECDH, b"x" * 64)

    assert channel.feed(stale) == b""
    assert channel.feed(b"next line\n") == b"next line\n"
    assert channel.take(4096) == b""


def test_raw_stale_frame_split_across_reads():
    """A frame split over reads is skipped whole, not leaked in pieces."""
    channel = _channel()
    stale = _frame(FRAME_TYPE_RSA, b"payload")

    for cut in range(len(stale) + 1):
        channel = _channel()
        assert channel.feed(stale[:cut]) == b""
        assert channel.feed(stale[cut:]) == b""
        assert channel.take(4096) == b""


def test_raw_frame_behind_plaintext_is_not_leaked():
    """A stale frame behind plaintext in one chunk is consumed, not a line.

    A peer's frames-start line and the frame behind it regularly coalesce
    into one read (Windows loopback does it every time); serving the frame
    as a line would echo it as a message and reset the decode-failure
    streak, so the breaker would never trip.
    """
    channel = _channel()
    stale = _frame(FRAME_TYPE_RSA, b"{nonce|0|cipher}")

    assert channel.feed(b"/crypto_frames_start\n" + stale) == b"/crypto_frames_start\n"
    assert channel.take(4096) == b""


def test_raw_frame_behind_plaintext_split_across_reads():
    """Same, with the frame (magic included) straddling the read boundary."""
    stale = _frame(FRAME_TYPE_ECDH, b"x" * 32)

    for cut in range(len(stale) + 1):
        channel = _channel()
        plain = channel.feed(b"line\n" + stale[:cut]) + channel.feed(stale[cut:])
        assert plain == b"line\n", f"cut {cut}: frame bytes reached the line reader"
        assert channel.take(4096) == b""


def test_raw_partial_magic_is_held():
    """Bytes that may still become a frame are not served as plaintext."""
    channel = _channel()

    assert channel.feed(FRAME_MAGIC[:2]) == b""
    assert channel.feed(FRAME_MAGIC[2:] + b"\x01\x00\x00\x00\x04body") == b""
    assert channel.take(4096) == b""


def test_raw_malformed_header_is_reported():
    """A frame header that cannot be delimited trips the decode failure."""
    channel = _channel()

    unknown_type = FRAME_MAGIC + bytes([99]) + (4).to_bytes(4, "big") + b"body"

    with pytest.raises(_FrameError):
        channel.feed(unknown_type)


def test_framed_mode_parses_frames_and_reports_nothing():
    """A framed channel decrypts into the buffer and reports no plaintext."""
    channel = _channel(
        framed=True,
        rsa=SimpleNamespace(decrypt_with_own=lambda cipher: (True, "decrypted")),
    )

    assert channel.feed(_frame(FRAME_TYPE_RSA, b"nonce|0|cipher")) == b""
    assert channel.take(4096) == b"decrypted"


def test_framed_oversized_frame_rejected():
    """A frame larger than MAX_FRAME_BODY cannot be resynchronised."""
    channel = _channel()
    header = FRAME_MAGIC + bytes([FRAME_TYPE_ECDH]) + (MAX_FRAME_BODY + 1).to_bytes(4, "big")

    with pytest.raises(_FrameError):
        channel.feed(header)
    assert len(header) < FRAME_HEADER_LEN + MAX_FRAME_BODY  # the length alone must trip it


def test_drain_plaintext_keeps_frames_in_the_channel():
    """After a failure the line reader gets plaintext, never frame bytes."""
    channel = _channel()
    stale = _frame(FRAME_TYPE_RSA, b"stale")

    assert channel.feed(stale + b"line\n") == b"line\n"  # dropped and passed in one chunk

    channel = _channel()
    channel.recv_seq = 0
    assert channel.feed(stale[: len(stale) - 1]) == b""  # frame left incomplete
    assert channel.drain_plaintext() == b""
    assert channel.feed(stale[-1:] + b"line\n") == b"line\n"
