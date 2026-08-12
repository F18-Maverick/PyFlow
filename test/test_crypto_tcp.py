"""End-to-end tests for RSA-encrypted TCP channels (connect_tcp.py).

A real server and client are connected over loopback with the crypto key
directories redirected to a temporary location. These tests need the
shared libcrypto_api and are skipped when it is not built.
"""

import contextlib
import io
import os
import socket
import sys
import threading
import time

import pytest

package_dictionary = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if package_dictionary not in sys.path:
    sys.path.insert(0, package_dictionary)

from PyFlow.network_api import rsa_crypto  # noqa: E402
from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base  # noqa: E402

try:
    rsa_crypto.load_library()
    HAVE_LIB = True
except rsa_crypto.CryptoLibraryError:
    HAVE_LIB = False

pytestmark = pytest.mark.skipif(
    not HAVE_LIB,
    reason="libcrypto_api not built (run cmake -S . -B build && cmake --build build first)",
)

_PORT_COUNTER = 65000  # per-test ports avoid cross-test interference



def _next_port():
    global _PORT_COUNTER
    _PORT_COUNTER += 1
    return _PORT_COUNTER


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
    port = _next_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(server.crypto, tmp_path, ssh_dir, "pub_key")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    time.sleep(0.4)

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


def _wait_flip(client, tries=300):
    for _ in range(tries):
        if client.client_socket in client._encrypted_sockets:
            return True
        time.sleep(0.1)
    return False


def _wait_disconnected(client, tries=400):
    """Wait until the client's connection is closed (rejected)."""
    for _ in range(tries):
        if not client.running or client.client_socket is None:
            return True
        time.sleep(0.1)
    return False


def _server_sock(server, client):
    """The server-side socket object for a connected client."""
    return server.clients[client.client_socket.getsockname()]["socket"]


def _wait_server_reexchanged(server, client, tries=400):
    """Wait until the server dropped the socket (decode failure) and
    re-flipped it (re-exchange complete), or the client died."""
    sock = _server_sock(server, client)
    for _ in range(tries):
        if not client.running:
            return False
        if sock not in server._encrypted_sockets:
            break
        time.sleep(0.1)
    for _ in range(tries):
        if not client.running:
            return False
        if sock in server._encrypted_sockets:
            return True
        time.sleep(0.1)
    return False


def _roundtrip(client, message, wait=2.5):
    """Send a message and capture both sides' stdout during the exchange."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        client.send_message(client.client_socket, message)
        time.sleep(wait)
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
    time.sleep(0.5)
    assert client.client_socket in client._encrypted_sockets
    assert len(server._encrypted_sockets) == 1
    for role in ("server", "client"):  # local keypairs generated under pvt_key

        assert os.path.exists(os.path.join(str(tmp_path / "pvt_key"), f"{role}_priv.pem"))
        assert os.path.exists(os.path.join(str(tmp_path / "pvt_key"), f"{role}_pub.pem"))
    server_reg = _registry_entries(server.crypto)  # the TOFU registry was written on both sides

    client_reg = _registry_entries(client.crypto)
    assert len(server_reg) == 1  # this client, keyed by (ip, client port)
    assert len(client_reg) == 1  # the server, keyed by (ip, server port)
    names = os.listdir(str(tmp_path / "pub_key"))  # exchanged peer keys cached as pem files under each side's pub_key

    assert "pub_key.json" in names
    assert any(name.startswith("client_") and name.endswith(".pem") for name in names)
    client_names = os.listdir(str(tmp_path / "pub_key_client"))
    assert "pub_key.json" in client_names
    assert any(name.startswith("server_") and name.endswith(".pem") for name in client_names)


def test_encrypted_roundtrip(tcp_pair):
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    time.sleep(0.5)
    out = _roundtrip(client, "hello encrypted")
    assert "[server] msg send: hello encrypted" in out


def test_second_connection_passes_tofu(tcp_pair, tmp_path):
    """A second connection presenting the same key is accepted (known
    key, endpoint updated), and the registry gains no duplicate."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    time.sleep(0.5)
    client.close()
    time.sleep(0.8)
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
        time.sleep(0.5)
        assert len(_registry_entries(server.crypto)) == 1  # same key from a (possibly new) source port: accepted, still one entry per peer

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
    time.sleep(0.5)
    _new_keypair((client.crypto.pub_path, client.crypto.priv_path))  # rotate the client's keypair (as if ~/.ssh was replaced) and reload it

    client.crypto.reload_own_key()
    _roundtrip(client, "trigger rotation", wait=7.0)
    assert _wait_disconnected(client)  # the server rejects the changed key and drops the connection
    assert len(_registry_entries(server.crypto)) == 1  # the rejected key must not have been recorded



def test_rotated_server_key_rejected(tcp_pair, tmp_path):
    """A server that rotates its keypair is rejected by the client: the
    known server endpoint now presents a different public key."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    time.sleep(0.5)
    _new_keypair((server.crypto.pub_path, server.crypto.priv_path))  # rotate the server's keypair and reload it in the running server

    server.crypto.reload_own_key()
    _roundtrip(client, "trigger rotation", wait=7.0)
    assert _wait_disconnected(client)  # the client rejects the changed key and closes the connection

    assert len(_registry_entries(client.crypto)) == 1


def test_custom_keys_pair_used(tmp_path):
    """The server honours a valid user-supplied keypair."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    pub_path = str(tmp_path / "custom_pub.pem")
    pvt_path = str(tmp_path / "custom_pvt.pem")
    _new_keypair((pub_path, pvt_path))
    port = _next_port()
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
    time.sleep(0.4)
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
        time.sleep(0.5)
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
    port = _next_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    time.sleep(0.4)
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
        time.sleep(0.5)
        assert not client._encrypted_sockets
        assert not server._encrypted_sockets
        out = _roundtrip(client, "plain hello")
        assert "[server] msg send: plain hello" in out
    finally:
        client.close()
        server.stop()


def test_replay_of_old_ciphertext_rejected(tcp_pair):
    """Replaying an old ciphertext (correct session nonce, stale seq) is
    dropped by the receiver and does not disturb the connection."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    time.sleep(0.5)
    out = _roundtrip(client, "first message")  # consume seq 0 with a normal message

    assert "[server] msg send: first message" in out
    nonce = client._crypto_my_nonce  # replay a ciphertext carrying seq 0 (already consumed) under the current session nonce

    body = client.crypto.encrypt_for_peer(client._crypto_server_pem_path, "REPLAY_MARKER")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        client._send_raw(client.client_socket, f"{nonce}|0|{body}")
        time.sleep(1.2)
    assert "REPLAY_MARKER" not in buf.getvalue()
    out2 = _roundtrip(client, "after replay")  # the connection is still healthy

    assert "[server] msg send: after replay" in out2


def test_replay_across_sessions_rejected(tcp_pair):
    """A ciphertext from a previous session (stale nonce) is rejected
    even at seq 0: the session nonce changes every handshake."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    time.sleep(0.5)
    old_nonce = client._crypto_my_nonce
    body = client.crypto.encrypt_for_peer(client._crypto_server_pem_path, "OLD_SESSION")
    client._send_raw(client.client_socket, f"{'a'*32}|0|AAAA")  # force a re-exchange (new nonce): trigger a decode failure on the server with garbage
    assert _wait_server_reexchanged(server, client)
    assert client.running
    assert client._crypto_my_nonce != old_nonce
    buf = io.StringIO()  # replay the old-session ciphertext: nonce mismatch -> dropped

    with contextlib.redirect_stdout(buf):
        client._send_raw(client.client_socket, f"{old_nonce}|0|{body}")
        time.sleep(1.2)
    assert "OLD_SESSION" not in buf.getvalue()


def test_unauthenticated_pub_push_ignored(tmp_path):
    """A /crypto_pub_key push from a connection that never started the
    handshake is ignored: it cannot poison the TOFU registry."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    port = _next_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _redirect_crypto(server.crypto, tmp_path, ssh_dir, "pub_key")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    time.sleep(0.4)
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(3)
    raw.connect(("127.0.0.1", port))
    raw.sendall(b"/crypto_pub_key 0\n")
    time.sleep(1.0)
    raw.close()
    try:
        assert len(_registry_entries(server.crypto)) == 0  # no registry entry and no cached key file were created

        names = os.listdir(str(tmp_path / "pub_key"))
        assert not any(name.endswith(".pem") for name in names)
    finally:
        server.stop()


def test_decode_failure_burst_closes_connection(tcp_pair):
    """After MAX_DECODE_FAILURES consecutive decode failures the
    circuit breaker closes the connection instead of re-exchanging
    forever."""
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    time.sleep(0.5)
    for i in range(3):
        client._send_raw(client.client_socket, f"{'a'*32}|{i}|AAAA")
        if not _wait_server_reexchanged(server, client):
            break  # breaker fired, connection closed
    assert _wait_disconnected(client)
