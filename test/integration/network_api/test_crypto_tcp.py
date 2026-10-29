"""Integration tests for RSA-encrypted TCP channels (connect_tcp.py).

A real server and client are connected over loopback with the crypto key
directories redirected to a temporary location. These tests need the
shared libcrypto_api and are skipped when it is not built.
"""

import contextlib
import io
import os
import socket
import threading
import time

import pytest


from helpers import free_port, server_ready, wait_until

from PyFlow.network_api import rsa_crypto
from PyFlow.network_api.connect_tcp import (
    CRYPTO_FRAMES_START,
    FRAME_MAGIC,
    FRAME_TYPE_RSA,
    MAX_DECODE_FAILURES,
    TCP_Client_Base,
    TCP_Server_Base,
)


def _frame(frame_type, body):
    """Build one wire frame exactly like _SecureChannel._wrap does."""
    return FRAME_MAGIC + bytes([frame_type]) + len(body).to_bytes(4, "big") + body


try:
    rsa_crypto.load_library()
    HAVE_LIB = True
except rsa_crypto.CryptoLibraryError:
    HAVE_LIB = False

pytestmark = pytest.mark.skipif(
    not HAVE_LIB,
    reason="libcrypto_api not built (run cmake -S . -B build && cmake --build build first)",
)


def _new_keypair(paths, lib=None, bits=None):
    """Generate a fresh RSA keypair into (pub_path, pvt_path)."""
    import ctypes

    if lib is None:
        lib = rsa_crypto.load_library()
    handle = ctypes.c_void_p()
    assert (
        lib.pf_rsa_keygen(bits or rsa_crypto.DEFAULT_KEY_BITS, ctypes.byref(handle))
        == rsa_crypto.PF_OK
    )
    key = rsa_crypto.RsaKey(handle.value, lib)
    pub_path, pvt_path = paths
    assert lib.pf_rsa_write_priv(key.handle, pvt_path.encode(), None) == 0
    assert lib.pf_rsa_write_pub(key.handle, pub_path.encode()) == 0
    del key


@pytest.fixture
def tcp_pair(tmp_path):
    """A running server plus a connected, handshaken client."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(server.crypto, tmp_path, ssh_dir, "pub_key")
    server.file_trans_progress_file = str(tmp_path / "server_file_trans_progress.json")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"

    client = TCP_Client_Base(
        host="127.0.0.1",
        port=port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(client.crypto, tmp_path, ssh_dir, "pub_key_client")
    client.file_trans_progress_file = str(tmp_path / "client_file_trans_progress.json")
    assert client.connect()
    yield server, client, tmp_path

    client.close()
    server.stop()


def _redirect_crypto(crypto, tmp_path, ssh_dir, subdir="pub_key"):
    crypto.pvt_key_dir = str(tmp_path / "pvt_key")
    crypto.pub_key_dir = str(tmp_path / subdir)
    crypto.ssh_dir = str(ssh_dir)
    crypto.registry_path = os.path.join(crypto.pub_key_dir, "pub_key.json")
    os.makedirs(crypto.pvt_key_dir, exist_ok=True)
    os.makedirs(crypto.pub_key_dir, exist_ok=True)


def _wait_flip(client, timeout=15):
    return wait_until(
        lambda: client.client_socket in client._encrypted_sockets,
        timeout=timeout,
    )


def _wait_disconnected(client, timeout=15):
    """Wait until the client's connection is closed (rejected)."""
    return wait_until(
        lambda: not client.running or client.client_socket is None,
        timeout=timeout,
    )


def _server_sock(server, client):
    """The server-side socket object for a connected client."""
    return server.clients[client.client_socket.getsockname()]["socket"]


def _wait_server_reexchanged(server, client, timeout=20):
    """Wait until the server dropped the socket (decode failure) and
    re-flipped it (re-exchange complete), or the client died."""
    sock = _server_sock(server, client)
    if not wait_until(lambda: sock not in server._encrypted_sockets, timeout=timeout):
        return False
    if not client.running:
        return False

    return wait_until(
        lambda: sock in server._encrypted_sockets and client.running,
        timeout=timeout,
    )


def _roundtrip(client, message, timeout=2.0):
    """Send a message and capture stdout until the server echoes it."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        client.send_message(client.client_socket, message)
        wait_until(lambda: f"msg send: {message}" in buf.getvalue(), timeout=timeout)
    return buf.getvalue()


def _registry_entries(crypto):
    import json

    if not os.path.exists(crypto.registry_path):
        return {}
    with open(crypto.registry_path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_handshake_flips_both_sides_and_stores_keys(tcp_pair):
    server, client, tmp_path = tcp_pair
    assert _wait_flip(client)
    assert client.client_socket in client._encrypted_sockets
    assert wait_until(lambda: len(server._encrypted_sockets) == 1, timeout=5)
    assert len(server._encrypted_sockets) == 1
    for role in ("server", "client"):  # local keypairs generated under pvt_key
        assert os.path.exists(os.path.join(str(tmp_path / "pvt_key"), f"{role}_priv.pem"))
        assert os.path.exists(os.path.join(str(tmp_path / "pvt_key"), f"{role}_pub.pem"))
    server_reg = _registry_entries(server.crypto)  # the TOFU registry was written on both sides

    client_reg = _registry_entries(client.crypto)
    assert len(server_reg) == 1  # this client, keyed by (ip, client port)
    assert len(client_reg) == 1  # the server, keyed by (ip, server port)
    names = os.listdir(
        str(tmp_path / "pub_key")
    )  # exchanged peer keys cached as pem files under each side's pub_key

    assert "pub_key.json" in names
    assert any(name.startswith("client_") and name.endswith(".pem") for name in names)
    client_names = os.listdir(str(tmp_path / "pub_key_client"))
    assert "pub_key.json" in client_names
    assert any(name.startswith("server_") and name.endswith(".pem") for name in client_names)


def test_encrypted_roundtrip(tcp_pair):
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    out = _roundtrip(client, "hello encrypted")
    assert "[server] msg send: hello encrypted" in out


def test_concurrent_encrypted_sends_not_dropped(tcp_pair):
    """Concurrent encrypted sends must not overtake each other on the
    wire: every message must be echoed back exactly once (seq allocation
    and the send are serialised per connection)."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    n_threads, per_thread = 8, 25
    total = n_threads * per_thread
    errors = []

    def sender(t):
        for i in range(per_thread):
            try:
                client.send_message(client.client_socket, f"msg-{t}-{i}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=sender, args=(t,)) for t in range(n_threads)]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        echoed = wait_until(lambda: buf.getvalue().count("msg send:") >= total, timeout=30)
    assert not errors
    assert echoed, f"dropped {total - buf.getvalue().count('msg send:')} of {total} messages"


def test_slow_sender_does_not_reorder_frames(tcp_pair):
    """A thread preempted between seq allocation and the wire write must
    not let a later sender overtake it (the peer would drop the earlier
    frame as out-of-order). Deterministic: the first sender's framed write
    is held under the per-connection send lock. Only that thread's framed
    write is stalled; the handshake's control lines and the ECDH publish
    are written by other threads and pass straight through."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    channel = client._channels[client.client_socket]
    assert wait_until(lambda: channel.frames_allowed.is_set(), timeout=5)
    assert wait_until(
        lambda: channel.frames_allowed.is_set()
        and server._channels[_server_sock(server, client)].frames_allowed.is_set(),
        timeout=5,
    )
    gate = threading.Event()
    stalled = threading.Event()
    first_sender = []  # the thread that issues "first"; only its framed write stalls
    real_sendall = channel._sendall

    def stalling_sendall(data):
        if threading.current_thread() is first_sender[0] and not stalled.is_set():
            stalled.set()  # the first sender holds seq 0 and is inside the send lock
            gate.wait()
        real_sendall(data)

    channel._sendall = stalling_sendall
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            t1 = threading.Thread(target=client.send_message, args=(client.client_socket, "first"))
            first_sender.append(t1)
            t1.start()
            assert stalled.wait(timeout=5), "first sender never reached the framed write"
            t2 = threading.Thread(target=client.send_message, args=(client.client_socket, "second"))
            t2.start()
            time.sleep(0.1)  # t2 attempts its send while t1 is still stalled
            gate.set()
            t1.join()
            t2.join()
            echoed = wait_until(
                lambda: "msg send: first" in buf.getvalue() and "msg send: second" in buf.getvalue(),
                timeout=10,
            )
    finally:
        gate.set()
        channel._sendall = real_sendall
    assert echoed, f"frame reordering dropped a message; echoes: {buf.getvalue()!r}"


def test_second_connection_passes_tofu(tcp_pair, tmp_path):
    """A second connection presenting the same key is accepted (known
    key, endpoint updated), and the registry gains no duplicate."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    client.close()
    assert wait_until(lambda: client.client_socket not in server._encrypted_sockets, timeout=5)
    client2 = TCP_Client_Base(
        host="127.0.0.1",
        port=server.port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(client2.crypto, tmp_path, tmp_path / "ssh", "pub_key_client")
    assert client2.connect()
    try:
        assert _wait_flip(client2)
        assert (
            len(_registry_entries(server.crypto)) == 1
        )  # same key from a (possibly new) source port: accepted, still one entry per peer

        assert len(_registry_entries(client2.crypto)) == 1
        out = _roundtrip(client2, "second connection")
        assert "[server] msg send: second connection" in out
    finally:
        client2.close()


def test_rotated_client_key_rejected(tcp_pair, tmp_path):
    """A client that rotates its keypair is rejected: on the next
    re-exchange the same endpoint presents a different public key."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    _new_keypair(
        (client.crypto.pub_path, client.crypto.priv_path)
    )  # rotate the client's keypair (as if ~/.ssh was replaced) and reload it

    client.crypto.reload_own_key()
    client.send_message(client.client_socket, "trigger rotation")
    assert _wait_disconnected(client)  # the server rejects the changed key and drops the connection
    assert (
        len(_registry_entries(server.crypto)) == 1
    )  # the rejected key must not have been recorded


def test_rotated_server_key_rejected(tcp_pair, tmp_path):
    """A server that rotates its keypair is rejected by the client: the
    known server endpoint now presents a different public key."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    _new_keypair(
        (server.crypto.pub_path, server.crypto.priv_path)
    )  # rotate the server's keypair and reload it in the running server

    server.crypto.reload_own_key()
    client.send_message(client.client_socket, "trigger rotation")
    assert _wait_disconnected(
        client
    )  # the client rejects the changed key and closes the connection

    assert len(_registry_entries(client.crypto)) == 1


def test_custom_keys_pair_used(tmp_path):
    """The server honours a valid user-supplied keypair."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    pub_path = str(tmp_path / "custom_pub.pem")
    pvt_path = str(tmp_path / "custom_pvt.pem")
    _new_keypair((pub_path, pvt_path))
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
        is_custom_keys=[pub_path, pvt_path],
    )
    _redirect_crypto(server.crypto, tmp_path, ssh_dir, "pub_key")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(client.crypto, tmp_path, ssh_dir, "pub_key_client")
    assert client.connect()
    try:
        assert _wait_flip(client)
        assert server.crypto.priv_path == pvt_path
        assert server.crypto.pub_path == pub_path
        out = _roundtrip(client, "custom key channel")
        assert "[server] msg send: custom key channel" in out
    finally:
        client.close()
        server.stop()


def test_encryption_disabled_is_plaintext(tmp_path):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    assert client.connect()
    try:
        assert not client._encrypted_sockets
        assert not server._encrypted_sockets
        out = _roundtrip(client, "plain hello")
        assert "[server] msg send: plain hello" in out
    finally:
        client.close()
        server.stop()


def test_replay_of_old_ciphertext_rejected(tcp_pair):
    """Replaying an old frame (correct session nonce, stale seq) is
    dropped by the receiver and does not disturb the connection."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    out = _roundtrip(client, "first message")  # consume seq 0 with a normal message

    assert "[server] msg send: first message" in out
    nonce = (
        client._crypto_my_nonce
    )  # replay a frame carrying seq 0 (already consumed) under the current session nonce
    body = "{}|0|{}".format(
        nonce,
        client.crypto.encrypt_for_peer(client._crypto_server_pem_path, "REPLAY_MARKER"),
    ).encode("ascii")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        client.client_socket.sendall(_frame(FRAME_TYPE_RSA, body))
        client.send_message(client.client_socket, "after replay")
        wait_until(lambda: "msg send: after replay" in buf.getvalue(), timeout=2.0)
    assert "REPLAY_MARKER" not in buf.getvalue()


def test_replay_across_sessions_rejected(tcp_pair):
    """A frame from a previous session (stale nonce) is rejected even at
    seq 0: the session nonce changes every handshake."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    old_nonce = client._crypto_my_nonce
    old_frame = _frame(
        FRAME_TYPE_RSA,
        "{}|0|{}".format(
            old_nonce,
            client.crypto.encrypt_for_peer(client._crypto_server_pem_path, "OLD_SESSION"),
        ).encode("ascii"),
    )
    # force a re-exchange (new nonce): a malformed frame trips a decode failure
    client.client_socket.sendall(_frame(FRAME_TYPE_RSA, b"garbage"))
    assert _wait_server_reexchanged(server, client)
    assert client.running
    assert client._crypto_my_nonce != old_nonce
    buf = io.StringIO()  # replay the old-session frame: nonce mismatch -> dropped

    with contextlib.redirect_stdout(buf):
        client.client_socket.sendall(old_frame)
        client.send_message(client.client_socket, "after old session")
        wait_until(lambda: "msg send: after old session" in buf.getvalue(), timeout=2.0)
    assert "OLD_SESSION" not in buf.getvalue()


def test_unauthenticated_pub_push_ignored(tmp_path, capsys):
    """A /crypto_pub_key push from a connection that never started the
    handshake is ignored: it cannot poison the TOFU registry."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(server.crypto, tmp_path, ssh_dir, "pub_key")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(3)
    raw.connect(("127.0.0.1", port))
    raw.sendall(b"/crypto_pub_key 0\n")

    output = ""

    def _saw_ignore_log():
        nonlocal output
        out, _ = capsys.readouterr()
        output += out
        return "ignoring /crypto_pub_key from non-handshaking peer" in output

    assert wait_until(_saw_ignore_log, timeout=5.0), "server did not report ignoring the push"
    raw.close()
    try:
        assert (
            len(_registry_entries(server.crypto)) == 0
        )  # no registry entry and no cached key file were created

        names = os.listdir(str(tmp_path / "pub_key"))
        assert not any(name.endswith(".pem") for name in names)
    finally:
        server.stop()


def test_decode_failure_burst_closes_connection(tcp_pair):
    """After MAX_DECODE_FAILURES consecutive decode failures the
    circuit breaker closes the connection instead of re-exchanging
    forever.

    Only a frame the server still parses as framed is counted: one that
    lands after it dropped framing again (mid re-exchange) is stale and is
    deliberately ignored — and the framed state is the channel's ``secure``
    flag, which is set after the socket rejoins ``_encrypted_sockets``.
    Waiting on that flag keeps every injection counted.
    """
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    sock = _server_sock(server, client)

    def server_framed():
        channel = server._channels.get(sock)
        return bool(channel is not None and channel.secure)

    for _ in range(MAX_DECODE_FAILURES):
        assert wait_until(server_framed, timeout=30), "the server never framed the channel again"
        client.client_socket.sendall(_frame(FRAME_TYPE_RSA, b"garbage"))
        # the frame was parsed, counted, and framing dropped again
        assert wait_until(
            lambda: not server_framed() or not client.running, timeout=30
        ), "the malformed frame was not counted by the breaker"
    assert _wait_disconnected(client)


def test_stale_frame_coalesced_behind_a_plaintext_line_is_consumed(tcp_pair):
    """A stale frame that arrives behind a plaintext line in one read is
    still consumed as a frame, never served as a line.

    Windows loopback regularly coalesces the peer's frames-start line and the
    frame behind it into one read; serving those frame bytes as a line echoes
    them as a message and resets the decode-failure streak, so the breaker
    never trips (that leak made the burst test above fail on Windows CI).
    """
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    sock = _server_sock(server, client)

    def server_framed():
        channel = server._channels.get(sock)
        return bool(channel is not None and channel.secure)

    # one counted failure drops the server's framing and re-exchanges
    assert wait_until(server_framed, timeout=30)
    client.client_socket.sendall(_frame(FRAME_TYPE_RSA, b"garbage"))
    assert wait_until(lambda: not server_framed(), timeout=30)

    # hold the server in the re-exchange window: what lands next is stale
    real_try_flip = server._crypto_try_server_flip
    server._crypto_try_server_flip = lambda *args, **kwargs: None

    def streak():
        with server._crypto_lock:
            state = server._crypto_state.get(server._crypto_sock_addr.get(sock))
            return None if state is None else state.get("decode_failures")

    dropped = []  # the channel's own stale-frame notice
    server._channels[sock]._debug = dropped.append
    client.client_socket.sendall(
        CRYPTO_FRAMES_START.encode("utf-8") + b"\n" + _frame(FRAME_TYPE_RSA, b"garbage")
    )

    assert wait_until(
        lambda: any("dropped a stale frame" in entry for entry in dropped), timeout=5
    ), "the stale frame behind the line was not consumed as a frame"
    assert streak() == 1, "frame bytes echoed as a line reset the decode-failure streak"

    # let the re-exchange settle before the fixture tears down: an in-flight
    # key push would race the stop (its thread reports an unhandled exception)
    server._crypto_try_server_flip = real_try_flip
    assert wait_until(server_framed, timeout=30), "the re-exchange never settled"


def test_mode_negotiation_survives_server_announcement_race(tmp_path):
    """The server's /crypto_mode line may be processed by the receive
    thread before connect() starts waiting for it. The event reset must
    happen before the receive thread starts; a reset after that point
    wipes the announcement and connect() times out (flaky CI failure).

    The negotiation call is delayed to force the receive thread to
    process the server's announcement before the wait begins."""
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    client._crypto_mode_timeout = 2  # fail fast if the reset race is present
    real_negotiate = client._crypto_negotiate_mode

    def slow_negotiate():
        time.sleep(0.5)  # the receive thread processes the server's mode line in this window
        return real_negotiate()

    client._crypto_negotiate_mode = slow_negotiate
    try:
        assert client.connect()
    finally:
        client.close()
        server.stop()


def test_flip_completes_even_if_push_ack_is_lost(tcp_pair, monkeypatch):
    """The server can lose the client-pub push ack (file socket closed
    right after the key file arrives): readiness must not be gated on the
    push thread finishing, or the handshake hangs forever."""
    server, client, _ = tcp_pair
    orig_send = server.send_message

    def flaky_ack(client_socket, message, is_send_raw=False):
        if message == server.server_received_file_data_sign:
            raise OSError(9, "Bad file descriptor")  # the flaky ack send
        return orig_send(client_socket, message, is_send_raw=is_send_raw)

    monkeypatch.setattr(server, "send_message", flaky_ack)
    assert _wait_flip(client)
    # the server flipped too: the key file did arrive, only its ack was lost
    assert client.client_socket in client._encrypted_sockets


def test_concurrent_clients_share_key_exchange(tmp_path):
    """Several clients handshaking at the same time share the
    received_files/ directory: public-key pushes must be stored under
    unique names, or concurrent moves corrupt the exchange (flaky
    FileNotFoundError under multi-client load)."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(server.crypto, tmp_path, ssh_dir, "pub_key")
    recv_dir = server.file_transfer_dir
    os.makedirs(recv_dir, exist_ok=True)
    # a failed earlier run may have left stale key files behind; the
    # assertion below checks this run leaves nothing behind, so start clean
    for leftover_name in os.listdir(recv_dir):
        if leftover_name.endswith(".pem"):
            try:
                os.remove(os.path.join(recv_dir, leftover_name))
            except OSError:
                pass
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"

    clients = []
    errors = []

    def connect_one(i):
        c = TCP_Client_Base(
            host="127.0.0.1",
            port=port,
            client_host="127.0.0.1",
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=True,
        )
        _redirect_crypto(c.crypto, tmp_path, ssh_dir, f"pub_key_client_{i}")
        try:
            if c.connect() and wait_until(
                lambda: c.client_socket in c._encrypted_sockets, timeout=20
            ):
                clients.append(c)
            else:
                errors.append(f"client {i} failed to connect/flip")
                c.close()
        except Exception as e:  # noqa: BLE001
            errors.append(f"client {i}: {e!r}")
            c.close()

    threads = [threading.Thread(target=connect_one, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert not errors, errors
        assert len(clients) == 4
        for c in clients:
            assert c.client_socket in c._encrypted_sockets
        # all four clients share one keypair (pvt_key dir), so the TOFU
        # registry keeps a single entry for that key
        assert len(_registry_entries(server.crypto)) == 1
        leftover = [n for n in os.listdir(recv_dir) if n.endswith(".pem")]
        assert not leftover, f"stale key files left in received_files/: {leftover}"
    finally:
        for c in clients:
            c.close()
        server.stop()


def test_crypto_mode_mismatch_disconnects(tmp_path):
    """A client whose ``is_enable_encrypto`` differs from the server's
    must be refused at connect time (both directions)."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()

    port = free_port()
    server_on = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(server_on.crypto, tmp_path, ssh_dir, "pub_key")
    threading.Thread(target=server_on.start_TCP_Server, daemon=True).start()
    assert server_ready(server_on), "server did not start"

    client_off = TCP_Client_Base(
        host="127.0.0.1",
        port=port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    try:
        assert not client_off.connect()  # server on / client off: refused
        assert not client_off.running
    finally:
        client_off.close()
        server_on.stop()

    port = free_port()
    server_off = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    threading.Thread(target=server_off.start_TCP_Server, daemon=True).start()
    assert server_ready(server_off), "server did not start"

    client_on = TCP_Client_Base(
        host="127.0.0.1",
        port=port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(client_on.crypto, tmp_path, ssh_dir, "pub_key_client")
    try:
        assert not client_on.connect()  # server off, client on: rejected and disconnected
        assert not client_on.running
    finally:
        client_on.close()
        server_off.stop()


def test_encrypted_quit_closes_the_session(tcp_pair):
    """Answer ``/quit`` and end the session on the encrypted channel too.

    The goodbye is encrypted like every other line, and the server then shuts
    the connection down, so the client must observe the disconnect.
    """
    server, client, _tmp = tcp_pair
    assert _wait_flip(client), "client never flipped to encrypted mode"
    assert wait_until(lambda: len(server._encrypted_sockets) == 1, timeout=5)
    assert client.send_message(client.client_socket, "/quit") is True
    assert wait_until(lambda: not server.clients, timeout=5), "server kept the encrypted session"
    assert _wait_disconnected(client), "client did not see the server close the connection"


def _capture_transfer_raw(instance, monkeypatch):
    """Record the raw wire bytes and stamps of the transfer legs a peer accepts."""
    raw = bytearray()
    stamps = []
    orig_attach = instance._crypto_attach_transfer_in

    def attach(sock, control_socket, stamp):
        stamps.append(stamp)
        channel = orig_attach(sock, control_socket, stamp)
        if channel is not None:
            real_feed = channel.feed

            def feed(data, _raw=raw, _feed=real_feed):
                _raw.extend(data)
                return _feed(data)

            channel.feed = feed  # capture exactly the bytes handed to the decoder
        return channel

    monkeypatch.setattr(instance, "_crypto_attach_transfer_in", attach)
    return raw, stamps


def test_encrypted_file_transfer_client_to_server(tcp_pair, tmp_path, monkeypatch):
    """A file pushed to the server crosses a framed sub-connection: the wire
    carries ciphertext and the file arrives byte-identical."""
    server, client, _ = tcp_pair
    assert _wait_flip(client), "client never flipped to encrypted mode"
    recv_dir = tmp_path / "server_recv"
    recv_dir.mkdir()
    server.file_transfer_dir = str(recv_dir)

    raw, stamps = _capture_transfer_raw(server, monkeypatch)
    payload = os.urandom(300 * 1024)
    src = tmp_path / "payload.bin"
    src.write_bytes(payload)

    server_sock = _server_sock(server, client)
    server.file_transfer_server_recv_server_start_thread("cid", server_sock, f"/file {src} 0")
    assert wait_until(lambda: list(client.file_server_port_list), timeout=10)
    with client.file_transfer_server_port_lock:
        port, _cid, stamp = client.file_server_port_list[0]
    assert stamp.startswith("enc:"), f"listener stamped {stamp!r}"
    assert client.file_transfer_mode(
        str(src), "127.0.0.1", port, 0, control_socket=client.client_socket, crypto=stamp
    )

    received = recv_dir / "payload.bin"
    assert wait_until(
        lambda: received.exists() and received.stat().st_size == len(payload), timeout=15
    ), "file was not received"
    assert received.read_bytes() == payload
    assert stamps and stamps[0].startswith("enc:"), "the leg was not encrypted"
    assert raw, "no transfer bytes crossed the wire"
    assert payload not in raw, "the payload appeared in the clear on the wire"


def test_encrypted_file_transfer_server_to_client(tcp_pair, tmp_path, monkeypatch):
    """The reverse direction (server pushes a file) is framed too."""
    server, client, _ = tcp_pair
    assert _wait_flip(client), "client never flipped to encrypted mode"
    recv_dir = tmp_path / "client_recv"
    recv_dir.mkdir()
    client.file_transfer_dir = str(recv_dir)

    raw, stamps = _capture_transfer_raw(client, monkeypatch)
    payload = os.urandom(300 * 1024)
    src = tmp_path / "big.bin"
    src.write_bytes(payload)

    client.file_transfer_client_recv_server_start_thread(
        "cid", client.client_socket, f"/file {src} 0"
    )
    assert wait_until(lambda: list(server.file_server_port_list), timeout=10)
    with server.file_transfer_server_port_lock:
        port, _cid, stamp = server.file_server_port_list[0]
    assert stamp.startswith("enc:"), f"listener stamped {stamp!r}"
    assert server.file_transfer_mode(
        str(src),
        "127.0.0.1",
        port,
        0,
        control_socket=_server_sock(server, client),
        crypto=stamp,
    )

    received = recv_dir / "big.bin"
    assert wait_until(
        lambda: received.exists() and received.stat().st_size == len(payload), timeout=15
    ), "file was not received"
    assert received.read_bytes() == payload
    assert stamps and stamps[0].startswith("enc:"), "the leg was not encrypted"
    assert raw and payload not in raw, "the payload appeared in the clear on the wire"


def test_encrypted_forward_between_clients(tmp_path, monkeypatch):
    """A forwarded file crosses the server's framed relay legs and arrives
    byte-identical; the payload never appears in the clear."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(server.crypto, tmp_path, ssh_dir, "pub_key")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    clients = []
    try:
        for i in range(2):
            c = TCP_Client_Base(
                host="127.0.0.1",
                port=port,
                client_host="127.0.0.1",
                is_extend_command=True,
                is_input_command_in_console=False,
                is_enable_encrypto=True,
            )
            _redirect_crypto(c.crypto, tmp_path, ssh_dir, f"pub_key_client_{i}")
            assert c.connect()
            clients.append(c)
        sender, receiver = clients
        recv_dir = tmp_path / "receiver_recv"
        recv_dir.mkdir()
        receiver.file_transfer_dir = str(recv_dir)
        for c in clients:
            assert wait_until(lambda c=c: c.client_socket in c._encrypted_sockets, timeout=20)

        raw, stamps = _capture_transfer_raw(receiver, monkeypatch)
        payload = os.urandom(200 * 1024)
        src = tmp_path / "fwd.bin"
        src.write_bytes(payload)
        dest = receiver.client_socket.getsockname()
        sender._forward_one("file", "", "fwd.bin", str(src), [dest])

        received = recv_dir / "fwd.bin"
        assert wait_until(
            lambda: received.exists() and received.stat().st_size == len(payload), timeout=30
        ), "forwarded file was not received"
        assert received.read_bytes() == payload
        assert stamps and stamps[0].startswith("enc:"), "the target leg was not encrypted"
        assert raw and payload not in raw, "the payload appeared in the clear on the wire"
    finally:
        for c in clients:
            c.close()
        server.stop()
