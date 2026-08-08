"""Unit tests for the crypto_api RSA wrapper (rsa_crypto.py).

These tests need the shared libcrypto_api; they are skipped when the C
library has not been built (see CMakeLists.txt at the repository root).
"""

import os
import sys

import pytest

package_dictionary = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if package_dictionary not in sys.path:
    sys.path.insert(0, package_dictionary)

from src.network_api import rsa_crypto  # noqa: E402

try:
    rsa_crypto.load_library()
    HAVE_LIB = True
except rsa_crypto.CryptoLibraryError:
    HAVE_LIB = False

pytestmark = pytest.mark.skipif(
    not HAVE_LIB,
    reason="libcrypto_api not built (run cmake -S . -B build && cmake --build build first)",
)

# fixed endpoint used by the registry tests: (peer_ip, peer_port)
PEER_IP = "127.0.0.1"
PEER_PORT = 50001


def _peer_pem_path(crypto, role="server"):
    return crypto.peer_pem_path(role, PEER_IP, PEER_PORT)


@pytest.fixture
def crypto(tmp_path):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    return rsa_crypto.RsaCrypto("client", str(tmp_path), str(ssh_dir))


@pytest.fixture
def peer(crypto):
    """A second role with its own keypair, as the registered peer."""
    other = rsa_crypto.RsaCrypto(
        "server", os.path.dirname(crypto.flow_dir) or ".", crypto.ssh_dir
    )
    other.ensure_keys()
    return other


def _register(crypto, peer_obj, ip=PEER_IP, port=PEER_PORT):
    crypto.store_peer_pem("server", ip, port, peer_obj.pub_path)
    return crypto.peer_pem_path("server", ip, port)


def test_mac_format():
    mac = rsa_crypto.get_local_mac()
    parts = mac.split(":")
    assert len(parts) == 6
    for part in parts:
        assert len(part) == 2
        int(part, 16)


def test_generated_keys_land_in_pvt_key(crypto):
    crypto.ensure_keys()
    assert os.path.exists(crypto.priv_path)
    assert os.path.exists(crypto.pub_path)
    assert crypto.priv_path.startswith(crypto.pvt_key_dir)
    assert os.path.dirname(crypto.priv_path) == crypto.pvt_key_dir
    # generated private keys must not be world-readable
    assert os.stat(crypto.priv_path).st_mode & 0o077 == 0


def test_ssh_key_is_reused_when_present(tmp_path):
    """A parseable PEM private key in ~/.ssh is used instead of a new one."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    lib = rsa_crypto.load_library()
    import ctypes

    handle = ctypes.c_void_p()
    assert lib.ss_rsa_keygen(rsa_crypto.DEFAULT_KEY_BITS, ctypes.byref(handle)) == rsa_crypto.SS_OK
    key = rsa_crypto.RsaKey(handle.value, lib)
    id_rsa = ssh_dir / "id_rsa"
    assert lib.ss_rsa_write_priv(key.handle, str(id_rsa).encode(), None) == 0
    crypto = rsa_crypto.RsaCrypto("server", str(tmp_path), str(ssh_dir))
    crypto.ensure_keys()
    assert crypto.priv_path == str(id_rsa)


def test_ssh_open_ssh_format_falls_back_to_generation(tmp_path):
    """An OpenSSH-format (unparseable) ~/.ssh key is ignored."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-pem\n-----END OPENSSH PRIVATE KEY-----\n"
    )
    crypto = rsa_crypto.RsaCrypto("client", str(tmp_path), str(ssh_dir))
    crypto.ensure_keys()
    assert crypto.priv_path != str(ssh_dir / "id_rsa")
    assert crypto.priv_path.startswith(crypto.pvt_key_dir)


def test_custom_keys_valid_pair_used(tmp_path):
    """A valid [pub, pvt] pair is used instead of the default lookup."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    lib = rsa_crypto.load_library()
    import ctypes

    handle = ctypes.c_void_p()
    assert lib.ss_rsa_keygen(rsa_crypto.DEFAULT_KEY_BITS, ctypes.byref(handle)) == rsa_crypto.SS_OK
    key = rsa_crypto.RsaKey(handle.value, lib)
    pub_path = str(tmp_path / "custom_pub.pem")
    pvt_path = str(tmp_path / "custom_pvt.pem")
    assert lib.ss_rsa_write_priv(key.handle, pvt_path.encode(), None) == 0
    assert lib.ss_rsa_write_pub(key.handle, pub_path.encode()) == 0
    del key
    crypto = rsa_crypto.RsaCrypto(
        "server", str(tmp_path), str(ssh_dir), custom_keys=[pub_path, pvt_path]
    )
    crypto.ensure_keys()
    assert crypto.priv_path == pvt_path
    assert crypto.pub_path == pub_path


def test_custom_keys_invalid_pair_ignored(tmp_path):
    """An invalid [pub, pvt] pair falls back to the default lookup."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    lib = rsa_crypto.load_library()
    import ctypes

    handle = ctypes.c_void_p()
    assert lib.ss_rsa_keygen(rsa_crypto.DEFAULT_KEY_BITS, ctypes.byref(handle)) == rsa_crypto.SS_OK
    key = rsa_crypto.RsaKey(handle.value, lib)
    pvt_path = str(tmp_path / "only_pvt.pem")
    assert lib.ss_rsa_write_priv(key.handle, pvt_path.encode(), None) == 0
    del key
    # pub file missing -> pair invalid -> default lookup used
    crypto = rsa_crypto.RsaCrypto(
        "client",
        str(tmp_path),
        str(ssh_dir),
        custom_keys=[str(tmp_path / "missing.pem"), pvt_path],
    )
    crypto.ensure_keys()
    assert crypto.priv_path != pvt_path
    assert crypto.priv_path.startswith(crypto.pvt_key_dir)


def test_custom_keys_unmatched_pair_ignored(tmp_path):
    """A pub key that does not pair with the pvt key is rejected."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    lib = rsa_crypto.load_library()
    import ctypes

    def gen(pub_path, pvt_path):
        handle = ctypes.c_void_p()
        assert (
            lib.ss_rsa_keygen(rsa_crypto.DEFAULT_KEY_BITS, ctypes.byref(handle))
            == rsa_crypto.SS_OK
        )
        key = rsa_crypto.RsaKey(handle.value, lib)
        assert lib.ss_rsa_write_priv(key.handle, pvt_path.encode(), None) == 0
        assert lib.ss_rsa_write_pub(key.handle, pub_path.encode()) == 0

    gen(str(tmp_path / "a_pub.pem"), str(tmp_path / "a_pvt.pem"))
    gen(str(tmp_path / "b_pub.pem"), str(tmp_path / "b_pvt.pem"))
    crypto = rsa_crypto.RsaCrypto(
        "client",
        str(tmp_path),
        str(ssh_dir),
        custom_keys=[str(tmp_path / "a_pub.pem"), str(tmp_path / "b_pvt.pem")],
    )
    crypto.ensure_keys()
    assert crypto.priv_path != str(tmp_path / "b_pvt.pem")
    assert crypto.priv_path.startswith(crypto.pvt_key_dir)


def test_encrypt_decrypt_roundtrip(crypto, peer):
    crypto.ensure_keys()
    pem_path = _register(crypto, peer)
    wire = crypto.encrypt_for_peer(pem_path, "hello crypto")
    ok, plain = peer.decrypt_with_own(wire)
    assert ok
    assert plain == "hello crypto"


def test_long_message_chunking(crypto, peer):
    crypto.ensure_keys()
    pem_path = _register(crypto, peer)
    long_msg = "x" * 5000
    wire = crypto.encrypt_for_peer(pem_path, long_msg)
    assert "|" in wire  # multiple RSA chunks
    ok, plain = peer.decrypt_with_own(wire)
    assert ok
    assert plain == long_msg


def test_tampered_ciphertext_fails(crypto, peer):
    crypto.ensure_keys()
    pem_path = _register(crypto, peer)
    wire = crypto.encrypt_for_peer(pem_path, "hello")
    tampered = ("A" + wire[1:]) if wire[0] != "A" else ("B" + wire[1:])
    ok, _ = peer.decrypt_with_own(tampered)
    assert not ok


def test_missing_valid_signature_fails(crypto, peer):
    """A ciphertext whose plaintext lacks the _VALID suffix is rejected."""
    crypto.ensure_keys()
    pem_path = _register(crypto, peer)
    # encrypt "hello" WITHOUT the _VALID suffix via the C library directly
    lib = rsa_crypto.load_library()
    import base64
    import ctypes
    import src.network_api.rsa_crypto as rc

    key = crypto._load_peer_pub(pem_path)
    chunk = b"hello"
    in_buf = (ctypes.c_uint8 * len(chunk)).from_buffer_copy(chunk)
    out_len = ctypes.c_size_t(0)
    assert (
        lib.ss_rsa_encrypt(key.handle, in_buf, len(chunk), None, ctypes.byref(out_len)) == rc.SS_OK
    )
    out_buf = (ctypes.c_uint8 * out_len.value)()
    assert (
        lib.ss_rsa_encrypt(key.handle, in_buf, len(chunk), out_buf, ctypes.byref(out_len))
        == rc.SS_OK
    )
    wire = base64.b64encode(bytes(out_buf[: out_len.value])).decode("ascii")
    ok, _ = peer.decrypt_with_own(wire)
    assert not ok
    # while the normal wrapper path (with suffix) still round-trips
    ok2, plain2 = peer.decrypt_with_own(crypto.encrypt_for_peer(pem_path, "hello"))
    assert ok2
    assert plain2 == "hello"


def test_encrypt_without_peer_key_raises(crypto):
    crypto.ensure_keys()
    with pytest.raises(ValueError):
        crypto.encrypt_for_peer("/nonexistent/peer.pem", "hello")


def test_peer_pem_naming(crypto):
    # the endpoint in the filename is filesystem-safe: ":" becomes "_"
    path = crypto.peer_pem_path("server", "fe80::1", 5000)
    assert path == os.path.join(crypto.pub_key_dir, "server_fe80__1_5000.pem")


# ---- TOFU registry ----------------------------------------------------------


def _pem_of(crypto):
    with open(crypto.pub_path, "r", encoding="utf-8") as f:
        return f.read()


def _registry(crypto):
    with open(crypto.registry_path, "r", encoding="utf-8") as f:
        return __import__("json").load(f)


def test_tofu_first_connection_adds_record(crypto, peer):
    ok, reason = crypto.verify_peer_pub(PEER_IP, PEER_PORT, _pem_of(peer), "server")
    assert ok
    assert "TOFU" in reason
    registry = _registry(crypto)
    assert str((PEER_IP, PEER_PORT)) in registry
    sha, pem = registry[str((PEER_IP, PEER_PORT))]
    assert pem == _pem_of(peer)
    import hashlib

    assert sha == hashlib.sha256(_pem_of(peer).encode("utf-8")).hexdigest()


def test_tofu_known_key_accepted(crypto, peer):
    crypto.verify_peer_pub(PEER_IP, PEER_PORT, _pem_of(peer), "server")
    ok, reason = crypto.verify_peer_pub(PEER_IP, PEER_PORT, _pem_of(peer), "server")
    assert ok
    assert "known key" in reason
    assert len(_registry(crypto)) == 1


def test_tofu_known_key_updates_endpoint(crypto, peer):
    """The same key seen from a new (ip, port) is re-registered there."""
    crypto.verify_peer_pub(PEER_IP, PEER_PORT, _pem_of(peer), "server")
    new_ip, new_port = "192.168.1.99", 43210
    ok, reason = crypto.verify_peer_pub(new_ip, new_port, _pem_of(peer), "server")
    assert ok
    assert "endpoint updated" in reason
    registry = _registry(crypto)
    assert len(registry) == 1  # moved, not duplicated
    assert str((new_ip, new_port)) in registry
    assert str((PEER_IP, PEER_PORT)) not in registry


def _other_role(crypto):
    """A second ``server``-role keypair in an isolated directory."""
    other = rsa_crypto.RsaCrypto(
        "server", os.path.join(os.path.dirname(crypto.flow_dir), "other"), crypto.ssh_dir
    )
    other.ensure_keys()
    return other


def test_tofu_same_endpoint_new_key_rejected(crypto, peer):
    """A trusted endpoint presenting a different key is rejected."""
    crypto.verify_peer_pub(PEER_IP, PEER_PORT, _pem_of(peer), "server")
    other = _other_role(crypto)
    ok, reason = crypto.verify_peer_pub(PEER_IP, PEER_PORT, _pem_of(other), "server")
    assert not ok
    assert "changed" in reason
    # the original record is untouched
    registry = _registry(crypto)
    assert len(registry) == 1
    assert registry[str((PEER_IP, PEER_PORT))][0] != ""


def test_tofu_endpoint_conflict_rejected(crypto, peer):
    """A known key arriving from an endpoint claimed by another key."""
    crypto.verify_peer_pub(PEER_IP, PEER_PORT, _pem_of(peer), "server")
    other = _other_role(crypto)
    crypto.verify_peer_pub("10.0.0.7", 6000, _pem_of(other), "server")
    # peer's key is registered at (PEER_IP, PEER_PORT); presenting it
    # from the endpoint already claimed by another key must be rejected
    ok, reason = crypto.verify_peer_pub("10.0.0.7", 6000, _pem_of(peer), "server")
    assert not ok
    assert "claimed" in reason
