"""End-to-end tests for RSA-encrypted TCP channels (connect_tcp.py).

A real server and client are connected over loopback with the crypto key
directories redirected to a temporary location. These tests need the
shared libcrypto_api and are skipped when it is not built.
"""

import io
import os
import sys
import threading
import time

import pytest

package_dictionary = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if package_dictionary not in sys.path:
    sys.path.insert(0, package_dictionary)

from src.network_api import rsa_crypto  # noqa: E402
from src.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base  # noqa: E402

try:
    rsa_crypto.load_library()
    HAVE_LIB = True
except rsa_crypto.CryptoLibraryError:
    HAVE_LIB = False

pytestmark = pytest.mark.skipif(
    not HAVE_LIB, reason="libcrypto_api not built (run cmake -S . -B build "
                        "&& cmake --build build first)")

# Ports are per-test to avoid cross-test interference.
_PORT_COUNTER = 65000


def _next_port():
    global _PORT_COUNTER
    _PORT_COUNTER += 1
    return _PORT_COUNTER


@pytest.fixture
def tcp_pair(tmp_path):
    """A running server plus a connected, handshaken client."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    port = _next_port()
    server = TCP_Server_Base(host="127.0.0.1", port=port,
                             is_extend_command=True,
                             is_input_command_in_console=False,
                             is_enable_encrypto=True)
    _redirect_crypto(server.crypto, tmp_path, ssh_dir)
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    time.sleep(0.4)

    client = TCP_Client_Base(host="127.0.0.1", port=port,
                             client_host="127.0.0.1",
                             is_extend_command=True,
                             is_input_command_in_console=False,
                             is_enable_encrypto=True)
    _redirect_crypto(client.crypto, tmp_path, ssh_dir)
    assert client.connect()
    yield server, client, tmp_path

    client.close()
    server.stop()


def _redirect_crypto(crypto, tmp_path, ssh_dir):
    crypto.pvt_key_dir = str(tmp_path / "pvt_key")
    crypto.pub_key_dir = str(tmp_path / "pub_key")
    crypto.ssh_dir = str(ssh_dir)
    os.makedirs(crypto.pvt_key_dir, exist_ok=True)
    os.makedirs(crypto.pub_key_dir, exist_ok=True)


def _wait_flip(client, tries=200):
    for _ in range(tries):
        if client.client_socket in client._encrypted_sockets:
            return True
        time.sleep(0.1)
    return False


def _roundtrip(client, message, wait=2.5):
    """Send a message and capture both sides' stdout during the exchange."""
    buf = io.StringIO()
    with __import__("contextlib").redirect_stdout(buf):
        client.send_message(client.client_socket, message)
        time.sleep(wait)
    return buf.getvalue()


def test_handshake_flips_both_sides_and_stores_keys(tcp_pair):
    server, client, tmp_path = tcp_pair
    assert _wait_flip(client)
    time.sleep(0.5)
    assert client.client_socket in client._encrypted_sockets
    assert len(server._encrypted_sockets) == 1
    # local keypairs generated under pvt_key
    for role in ("server", "client"):
        assert os.path.exists(
            os.path.join(str(tmp_path / "pvt_key"), f"{role}_priv.pem"))
        assert os.path.exists(
            os.path.join(str(tmp_path / "pvt_key"), f"{role}_pub.pem"))
    # exchanged peer keys stored under pub_key, labelled with the peer MAC
    mac = client.crypto.mac
    assert os.path.exists(server.crypto.peer_pub_path("client", mac))
    assert os.path.exists(client.crypto.peer_pub_path("server", mac))
    # the exchanged key files carry the peer MAC (filesystem-safe "_"
    # separators) in their name
    mac_file = mac.replace(":", "_")
    names = os.listdir(str(tmp_path / "pub_key"))
    assert any(name.startswith("client_") and mac_file in name
               for name in names)
    assert any(name.startswith("server_") and mac_file in name
               for name in names)


def test_encrypted_roundtrip(tcp_pair):
    server, client, _ = tcp_pair
    assert _wait_flip(client)
    time.sleep(0.5)
    out = _roundtrip(client, "hello encrypted")
    assert "[server] msg send: hello encrypted" in out


def test_second_connection_reuses_exchanged_keys(tcp_pair):
    server, client, tmp_path = tcp_pair
    assert _wait_flip(client)
    time.sleep(0.5)
    pub_before = sorted(os.listdir(str(tmp_path / "pub_key")))
    client.close()
    time.sleep(0.8)
    port = server.port
    client2 = TCP_Client_Base(host="127.0.0.1", port=port,
                              client_host="127.0.0.1",
                              is_extend_command=True,
                              is_input_command_in_console=False,
                              is_enable_encrypto=True)
    _redirect_crypto(client2.crypto, tmp_path, tmp_path / "ssh")
    assert client2.connect()
    try:
        assert _wait_flip(client2)
        time.sleep(0.5)
        # no re-exchange: the registry is untouched
        assert sorted(os.listdir(str(tmp_path / "pub_key"))) == pub_before
        out = _roundtrip(client2, "reused keys")
        assert "[server] msg send: reused keys" in out
    finally:
        client2.close()


def test_rotated_client_key_triggers_re_exchange(tcp_pair):
    server, client, tmp_path = tcp_pair
    assert _wait_flip(client)
    time.sleep(0.5)
    # simulate a user rotating their ~/.ssh keypair: replace the client's
    # private key with a brand-new one
    lib = rsa_crypto.load_library()
    import ctypes

    handle = ctypes.c_void_p()
    assert lib.ss_rsa_keygen(rsa_crypto.DEFAULT_KEY_BITS,
                             ctypes.byref(handle)) == rsa_crypto.SS_OK
    new_key = rsa_crypto.RsaKey(handle.value, lib)
    assert lib.ss_rsa_write_priv(
        new_key.handle, client.crypto.priv_path.encode(), None) == 0
    assert lib.ss_rsa_write_pub(
        new_key.handle, client.crypto.pub_path.encode()) == 0
    del new_key
    server_pub_path = server.crypto.peer_pub_path("client", client.crypto.mac)
    old_stored_pub = open(server_pub_path, "rb").read()
    new_pub = open(client.crypto.pub_path, "rb").read()
    assert old_stored_pub != new_pub

    client.crypto.reload_own_key()  # pick up the rotated keypair
    # the first encrypted reply uses the stale stored pub: the client
    # detects the decode failure and forces a re-exchange (that first
    # reply is lost, which is the designed detection path), after which
    # the server stores the *new* public key and a fresh message
    # round-trips
    _roundtrip(client, "after rotation", wait=7.0)
    assert open(server_pub_path, "rb").read() == new_pub
    out2 = _roundtrip(client, "second after rotation")
    assert "[server] msg send: second after rotation" in out2


def test_encryption_disabled_is_plaintext(tmp_path):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    port = _next_port()
    server = TCP_Server_Base(host="127.0.0.1", port=port,
                             is_extend_command=True,
                             is_input_command_in_console=False,
                             is_enable_encrypto=False)
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    time.sleep(0.4)
    client = TCP_Client_Base(host="127.0.0.1", port=port,
                             client_host="127.0.0.1",
                             is_extend_command=True,
                             is_input_command_in_console=False,
                             is_enable_encrypto=False)
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
