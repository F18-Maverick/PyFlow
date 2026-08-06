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
    not HAVE_LIB, reason="libcrypto_api not built (run cmake -S . -B build "
                        "&& cmake --build build first)")


@pytest.fixture
def crypto(tmp_path):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    return rsa_crypto.RsaCrypto("client", str(tmp_path), str(ssh_dir))


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


def test_ssh_key_is_reused_when_present(tmp_path):
    """A parseable PEM private key in ~/.ssh is used instead of a new one."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    lib = rsa_crypto.load_library()
    import ctypes

    handle = ctypes.c_void_p()
    assert lib.ss_rsa_keygen(rsa_crypto.DEFAULT_KEY_BITS,
                             ctypes.byref(handle)) == rsa_crypto.SS_OK
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
        "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-pem\n"
        "-----END OPENSSH PRIVATE KEY-----\n")
    crypto = rsa_crypto.RsaCrypto("client", str(tmp_path), str(ssh_dir))
    crypto.ensure_keys()
    assert crypto.priv_path != str(ssh_dir / "id_rsa")
    assert crypto.priv_path.startswith(crypto.pvt_key_dir)


def test_encrypt_decrypt_roundtrip(crypto):
    crypto.ensure_keys()
    peer = rsa_crypto.RsaCrypto("server", os.path.dirname(crypto.flow_dir) or ".",
                                crypto.ssh_dir)
    peer.ensure_keys()
    # register the peer's public key in our registry
    crypto.store_peer_key("server", peer.mac, peer.pub_path)
    wire = crypto.encrypt_for_peer("server", peer.mac, "hello crypto")
    ok, plain = peer.decrypt_with_own(wire)
    assert ok
    assert plain == "hello crypto"


def test_long_message_chunking(crypto):
    crypto.ensure_keys()
    peer = rsa_crypto.RsaCrypto("server", os.path.dirname(crypto.flow_dir) or ".",
                                crypto.ssh_dir)
    peer.ensure_keys()
    crypto.store_peer_key("server", peer.mac, peer.pub_path)
    long_msg = "x" * 5000
    wire = crypto.encrypt_for_peer("server", peer.mac, long_msg)
    assert "|" in wire  # multiple RSA chunks
    ok, plain = peer.decrypt_with_own(wire)
    assert ok
    assert plain == long_msg


def test_tampered_ciphertext_fails(crypto):
    crypto.ensure_keys()
    peer = rsa_crypto.RsaCrypto("server", os.path.dirname(crypto.flow_dir) or ".",
                                crypto.ssh_dir)
    peer.ensure_keys()
    crypto.store_peer_key("server", peer.mac, peer.pub_path)
    wire = crypto.encrypt_for_peer("server", peer.mac, "hello")
    tampered = ("A" + wire[1:]) if wire[0] != "A" else ("B" + wire[1:])
    ok, _ = peer.decrypt_with_own(tampered)
    assert not ok


def test_missing_valid_signature_fails(crypto):
    """A ciphertext whose plaintext lacks the _VALID suffix is rejected."""
    crypto.ensure_keys()
    peer = rsa_crypto.RsaCrypto("server", os.path.dirname(crypto.flow_dir) or ".",
                                crypto.ssh_dir)
    peer.ensure_keys()
    crypto.store_peer_key("server", peer.mac, peer.pub_path)
    # encrypt "hello" WITHOUT the _VALID suffix via the C library directly
    lib = rsa_crypto.load_library()
    import base64
    import ctypes
    import src.network_api.rsa_crypto as rc
    from src.network_api.rsa_crypto import _Library
    key = crypto._load_peer_pub(crypto.peer_pub_path("server", peer.mac))
    chunk = b"hello"
    in_buf = (ctypes.c_uint8 * len(chunk)).from_buffer_copy(chunk)
    out_len = ctypes.c_size_t(0)
    assert lib.ss_rsa_encrypt(key.handle, in_buf, len(chunk), None,
                              ctypes.byref(out_len)) == rc.SS_OK
    out_buf = (ctypes.c_uint8 * out_len.value)()
    assert lib.ss_rsa_encrypt(key.handle, in_buf, len(chunk), out_buf,
                              ctypes.byref(out_len)) == rc.SS_OK
    wire = base64.b64encode(bytes(out_buf[:out_len.value])).decode("ascii")
    ok, _ = peer.decrypt_with_own(wire)
    assert not ok
    # while the normal wrapper path (with suffix) still round-trips
    ok2, plain2 = peer.decrypt_with_own(
        crypto.encrypt_for_peer("server", peer.mac, "hello"))
    assert ok2
    assert plain2 == "hello"


def test_encrypt_without_peer_key_raises(crypto):
    crypto.ensure_keys()
    with pytest.raises(ValueError):
        crypto.encrypt_for_peer("server", "00:00:00:00:00:00", "hello")


def test_peer_registry_naming(crypto):
    # the MAC in the filename is filesystem-safe: ":" becomes "_"
    path = crypto.peer_pub_path("server", "aa:bb:cc:dd:ee:ff")
    assert path == os.path.join(
        crypto.pub_key_dir, "server_aa_bb_cc_dd_ee_ff.pem")
