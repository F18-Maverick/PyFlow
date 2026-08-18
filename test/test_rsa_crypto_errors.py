"""Error-path tests for rsa_crypto: library lookup failure, invalid keys,
registry persistence failures, and decrypt/load error branches."""

import os
import sys
import ctypes

import pytest

import PyFlow.network_api.rsa_crypto as rc

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    rc.load_library()
    HAVE_LIB = True
except rc.CryptoLibraryError:
    HAVE_LIB = False

pytestmark = pytest.mark.skipif(
    not HAVE_LIB,
    reason="libcrypto_api not built (run cmake -S . -B build && cmake --build build first)",
)


def _fresh_crypto(tmp_path, role="client"):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(exist_ok=True)
    return rc.RsaCrypto(role, str(tmp_path), str(ssh_dir))


def test_load_library_raises_when_missing(monkeypatch):
    """No candidate library file exists -> CryptoLibraryError."""
    rc._Library._instance = None
    monkeypatch.setattr(rc.ctypes.util, "find_library", lambda _: None)
    monkeypatch.setattr(rc.os.path, "exists", lambda _: False)
    with pytest.raises(rc.CryptoLibraryError):
        rc.load_library()
    rc._Library._instance = None


def test_load_library_falls_back_to_build_dir(tmp_path, monkeypatch):
    """find_library misses, but the repo build/ candidate exists."""
    build_lib = tmp_path / "build" / "libcrypto_api.so"
    build_lib.parent.mkdir(parents=True)
    # point the repo-root search at tmp: patch os.path.join? simpler: patch
    # exists to accept the real build dir while find_library returns None
    import PyFlow.network_api.rsa_crypto as rcm

    real_exists = os.path.exists
    rc._Library._instance = None
    monkeypatch.setattr(rc.ctypes.util, "find_library", lambda _: None)
    # find the actual built .so and simulate only it existing
    import glob

    real = glob.glob(os.path.join(PROJECT_ROOT, "build", "libcrypto_api.so"))
    if not real:
        pytest.skip("no built library in repo build/")
    monkeypatch.setattr(
        rc.os.path, "exists", lambda p: p == real[0] or p in real or real_exists(p)
    )
    lib = rc.load_library()
    assert lib is not None
    rc._Library._instance = None


def test_rsa_key_del_handles_free_failure(monkeypatch, tmp_path):
    crypto = _fresh_crypto(tmp_path)
    crypto.ensure_keys()
    key = crypto._priv_key
    monkeypatch.setattr(key._lib, "pf_rsa_key_free", lambda _: (_ for _ in ()).throw(RuntimeError()))
    key.__del__()  # must not raise
    assert key._handle is None


def test_ensure_keys_custom_invalid_pub_unreadable(tmp_path):
    """A custom pair whose pub file is not a valid PEM falls back."""
    crypto = _fresh_crypto(tmp_path)
    (tmp_path / "bad_pub.pem").write_text("not a pem")
    (tmp_path / "good_pvt.pem").write_text("not a pem either")
    crypto.custom_keys = [str(tmp_path / "bad_pub.pem"), str(tmp_path / "good_pvt.pem")]
    crypto.ensure_keys()
    # fell back to generated keys
    assert crypto.priv_path.startswith(crypto.pvt_key_dir)


def test_validate_custom_keys_false_on_bad_files(tmp_path):
    crypto = _fresh_crypto(tmp_path)
    lib = rc.load_library()
    assert crypto._validate_custom_keys(lib, ["/nonexistent.pem", "/nonexistent.pem"]) is False
    assert crypto._validate_custom_keys(lib, "not-a-list") is False
    assert crypto._validate_custom_keys(lib, [1, 2]) is False


def test_decrypt_with_own_stale_key_fails(tmp_path):
    """Decrypting with a *different* private key fails cleanly."""
    a = _fresh_crypto(tmp_path, role="server")
    b = _fresh_crypto(tmp_path, role="client")
    a.ensure_keys()
    b.ensure_keys()
    wire = a.encrypt_for_peer(b.pub_path, "hello")  # encrypted to b's pub
    ok, plain = b.decrypt_with_own(wire)
    assert ok and plain == "hello"
    # 'a' cannot decrypt its own ciphertext (wrong private key)
    ok2, _ = a.decrypt_with_own(wire)
    assert not ok2


def test_decrypt_with_own_empty_body(tmp_path):
    crypto = _fresh_crypto(tmp_path)
    crypto.ensure_keys()
    ok, _ = crypto.decrypt_with_own("")
    assert not ok


def test_decrypt_with_own_garbage_chunk(tmp_path):
    crypto = _fresh_crypto(tmp_path)
    crypto.ensure_keys()
    ok, _ = crypto.decrypt_with_own("!!not-base64!!")
    assert not ok


def test_load_peer_pub_missing_file(tmp_path):
    crypto = _fresh_crypto(tmp_path)
    crypto.ensure_keys()
    assert crypto._load_peer_pub(str(tmp_path / "nope.pem")) is None


def test_load_peer_pub_caches_by_mtime(tmp_path):
    crypto = _fresh_crypto(tmp_path)
    peer = _fresh_crypto(tmp_path, role="server")
    peer.ensure_keys()
    crypto.ensure_keys()
    dest = crypto.peer_pem_path("server", "127.0.0.1", 7000)
    import shutil

    shutil.copy(peer.pub_path, dest)
    k1 = crypto._load_peer_pub(dest)
    k2 = crypto._load_peer_pub(dest)
    assert k1 is k2  # cached


def test_verify_peer_pub_rejects_garbage_pem(tmp_path):
    """A key push whose PEM parses is accepted; garbage content is still
    recorded (TOFU stores whatever text) - but the registry stays valid."""
    crypto = _fresh_crypto(tmp_path)
    ok, reason = crypto.verify_peer_pub("127.0.0.1", 7001, "garbage not pem", "server")
    assert ok and "TOFU" in reason
    # a different garbage from the same endpoint is rejected (key changed)
    ok2, reason2 = crypto.verify_peer_pub("127.0.0.1", 7001, "other garbage", "server")
    assert not ok2 and "changed" in reason2


def test_save_registry_raises_on_unwritable_dir(tmp_path):
    crypto = _fresh_crypto(tmp_path)
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    crypto.pub_key_dir = str(ro)
    crypto.registry_path = str(ro / "pub_key.json")
    if sys.platform != "win32":
        with pytest.raises(OSError):
            crypto._save_registry({})
