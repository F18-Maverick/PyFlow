"""Unit tests for the crypto_api ECDH/HKDF wrapper (rsa_crypto.py).

These tests need the shared libcrypto_api; they are skipped when the C
library has not been built (see CMakeLists.txt at the repository root).
"""

import pytest

from PyFlow.network_api import rsa_crypto

try:
    rsa_crypto.load_library()
    HAVE_LIB = True
except rsa_crypto.CryptoLibraryError:
    HAVE_LIB = False

pytestmark = pytest.mark.skipif(
    not HAVE_LIB,
    reason="libcrypto_api not built (run cmake -S . -B build && cmake --build build first)",
)


@pytest.fixture
def alice():
    return rsa_crypto.EcdhKeyPair()


@pytest.fixture
def bob():
    return rsa_crypto.EcdhKeyPair()


def _pub(keypair):
    """Parse ``keypair``'s exported public PEM as a peer key object."""
    return rsa_crypto.EcdhPubKey.from_pem(keypair.public_pem())


def test_public_pem_roundtrip(alice):
    pem = alice.public_pem()

    assert pem.startswith("-----BEGIN PUBLIC KEY-----")
    peer = rsa_crypto.EcdhPubKey.from_pem(pem)
    assert peer.handle  # a live parsed handle


def test_bad_pem_raises_parse_error():
    with pytest.raises(rsa_crypto.CryptoOperationError) as exc:
        rsa_crypto.EcdhPubKey.from_pem("not a pem")

    assert exc.value.code == rsa_crypto.PF_ERR_PARSE


def test_seal_open_roundtrip_both_directions(alice, bob):
    message = b"hello ecdh"

    sealed = alice.seal(_pub(bob), message)
    assert bob.open(_pub(alice), sealed) == message

    reply = b"hello back"
    sealed_reply = bob.seal(_pub(alice), reply)
    assert alice.open(_pub(bob), sealed_reply) == reply


def test_seal_overhead_is_44_bytes(alice, bob):
    for size in (0, 1, 1000):
        sealed = alice.seal(_pub(bob), b"x" * size)
        assert len(sealed) == size + rsa_crypto.PF_ECDH_SEAL_OVERHEAD


def test_empty_plaintext_roundtrips(alice, bob):
    sealed = alice.seal(_pub(bob), b"")

    assert len(sealed) == rsa_crypto.PF_ECDH_SEAL_OVERHEAD
    assert bob.open(_pub(alice), sealed) == b""


def test_wrong_peer_key_open_fails(alice, bob):
    stranger = rsa_crypto.EcdhKeyPair()
    sealed = alice.seal(_pub(bob), b"secret")

    with pytest.raises(rsa_crypto.CryptoOperationError) as exc:
        bob.open(_pub(stranger), sealed)

    assert exc.value.code == rsa_crypto.PF_ERR_AUTH_FAILED


def test_tampered_ciphertext_fails(alice, bob):
    sealed = bytearray(alice.seal(_pub(bob), b"secret"))
    sealed[-1] ^= 0x01  # flip a bit inside the GCM tag

    with pytest.raises(rsa_crypto.CryptoOperationError) as exc:
        bob.open(_pub(alice), bytes(sealed))

    assert exc.value.code == rsa_crypto.PF_ERR_AUTH_FAILED


def test_aad_mismatch_fails(alice, bob):
    sealed = alice.seal(_pub(bob), b"secret", aad=b"header-a")

    assert bob.open(_pub(alice), sealed, aad=b"header-a") == b"secret"
    with pytest.raises(rsa_crypto.CryptoOperationError) as exc:
        bob.open(_pub(alice), sealed, aad=b"header-b")
    assert exc.value.code == rsa_crypto.PF_ERR_AUTH_FAILED

    with pytest.raises(rsa_crypto.CryptoOperationError) as exc:
        bob.open(_pub(alice), sealed)  # missing AAD
    assert exc.value.code == rsa_crypto.PF_ERR_AUTH_FAILED


def test_hkdf_is_deterministic():
    first = rsa_crypto.hkdf_sha256(b"ikm", b"salt", b"info", 32)
    second = rsa_crypto.hkdf_sha256(b"ikm", b"salt", b"info", 32)

    assert first == second
    assert len(first) == 32
    assert first != rsa_crypto.hkdf_sha256(b"ikm", b"salt", b"other-info", 32)


def test_hkdf_output_length_prefix_stable():
    long_out = rsa_crypto.hkdf_sha256(b"ikm", b"salt", b"info", 64)
    short_out = rsa_crypto.hkdf_sha256(b"ikm", b"salt", b"info", 16)

    assert len(long_out) == 64
    assert len(short_out) == 16
    assert long_out[:16] == short_out  # RFC 5869 expand output is prefix-stable


def test_hkdf_empty_salt_and_info_supported():
    out = rsa_crypto.hkdf_sha256(b"ikm", b"", b"", 32)

    assert len(out) == 32


def test_hkdf_invalid_output_length_raises():
    for bad_len in (0, -1, 8161):
        with pytest.raises(rsa_crypto.CryptoOperationError) as exc:
            rsa_crypto.hkdf_sha256(b"ikm", b"salt", b"info", bad_len)
        assert exc.value.code == rsa_crypto.PF_ERR_INVALID_ARG


def test_unsupported_curve_raises():
    with pytest.raises(rsa_crypto.CryptoOperationError) as exc:
        rsa_crypto.EcdhKeyPair(curve="P-1")

    assert exc.value.code == rsa_crypto.PF_ERR_UNSUPPORTED
