"""crypto_api (C/OpenSSL) RSA integration for ServSpy's TCP layer.

A thin ctypes binding to the shared ``libcrypto_api`` plus the key
lifecycle required by the encrypted TCP channel:

- Reuse an existing RSA keypair from ``~/.ssh`` (PEM private key) when
  one is present and parseable, otherwise generate a fresh keypair into
  ``.Flow/pvt_key``.
- Keep exchanged peer public keys in ``.Flow/pub_key/<role>_<mac>.pem``.
- RSA-OAEP encrypt/decrypt with the ``_VALID`` plaintext signature so a
  stale key (for example a rotated ``~/.ssh`` pair) is detected and the
  peers re-exchange their public keys.

The C library must be built first (``cmake -S . -B build &&
cmake --build build``); see ``load_library`` for the search paths.
"""

import ctypes
import ctypes.util
import os
import uuid

# Suffix appended to every plaintext before RSA-OAEP encryption. The
# receiver strips it after a successful decrypt, so a message decoded
# with a wrong/stale key (decrypt failure or missing suffix) is
# unmistakably detected and triggers a public-key re-exchange.
VALID_SIGN = "_VALID"

# ss_err_t values, must match ss_crypto.h.
SS_OK = 0
SS_ERR_INVALID_ARG = 1
SS_ERR_NOMEM = 2
SS_ERR_OPENSSL = 3
SS_ERR_IO = 4
SS_ERR_PARSE = 5
SS_ERR_BUFFER_TOO_SMALL = 6
SS_ERR_AUTH_FAILED = 7
SS_ERR_UNSUPPORTED = 8
SS_ERR_DECRYPT = 9

# Default size for generated keys (bits). ss_rsa_keygen accepts 2048..16384.
DEFAULT_KEY_BITS = 2048


class CryptoLibraryError(RuntimeError):
    """Raised when the shared libcrypto_api cannot be loaded."""


class RsaKey:
    """Owns an ``ss_rsa_key_t*`` handle; frees it on GC."""

    def __init__(self, handle, lib):
        self._handle = handle
        self._lib = lib

    @property
    def handle(self):
        return self._handle

    def __del__(self):
        if self._handle:
            try:
                self._lib.ss_rsa_key_free(self._handle)
            except Exception:
                pass
            self._handle = None


class _Library:
    """Lazy singleton wrapper around the ctypes binding."""

    _instance = None

    def __init__(self, path):
        self.lib = ctypes.CDLL(path)
        _configure(self.lib)

    def __getattr__(self, name):
        # Expose the CDLL functions directly on the wrapper.
        return getattr(self.lib, name)


def _configure(lib):
    c_void_p = ctypes.c_void_p
    c_size_t = ctypes.c_size_t
    c_uint8_p = ctypes.POINTER(ctypes.c_uint8)

    lib.ss_rsa_keygen.argtypes = [ctypes.c_int, ctypes.POINTER(c_void_p)]
    lib.ss_rsa_keygen.restype = ctypes.c_int
    lib.ss_rsa_read_pub.argtypes = [ctypes.c_char_p, ctypes.POINTER(c_void_p)]
    lib.ss_rsa_read_pub.restype = ctypes.c_int
    lib.ss_rsa_read_priv.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(c_void_p)]
    lib.ss_rsa_read_priv.restype = ctypes.c_int
    lib.ss_rsa_write_pub.argtypes = [c_void_p, ctypes.c_char_p]
    lib.ss_rsa_write_pub.restype = ctypes.c_int
    lib.ss_rsa_write_priv.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.ss_rsa_write_priv.restype = ctypes.c_int
    lib.ss_rsa_encrypt.argtypes = [
        c_void_p,
        c_uint8_p,
        c_size_t,
        c_uint8_p,
        ctypes.POINTER(c_size_t),
    ]
    lib.ss_rsa_encrypt.restype = ctypes.c_int
    lib.ss_rsa_decrypt.argtypes = [
        c_void_p,
        c_uint8_p,
        c_size_t,
        c_uint8_p,
        ctypes.POINTER(c_size_t),
    ]
    lib.ss_rsa_decrypt.restype = ctypes.c_int
    lib.ss_rsa_max_plaintext_len.argtypes = [c_void_p]
    lib.ss_rsa_max_plaintext_len.restype = c_size_t
    lib.ss_rsa_ciphertext_len.argtypes = [c_void_p]
    lib.ss_rsa_ciphertext_len.restype = c_size_t
    lib.ss_rsa_key_free.argtypes = [c_void_p]
    lib.ss_rsa_key_free.restype = None
    return lib


def load_library():
    """Locate and load the shared crypto_api library (cached)."""
    if _Library._instance is not None:
        return _Library._instance
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    candidates = []
    found = ctypes.util.find_library("crypto_api")
    if found:
        candidates.append(found)
    candidates += [
        os.path.join(repo_root, "build", "libcrypto_api.so"),
        os.path.join(repo_root, "build", "libcrypto_api.dylib"),
        os.path.join(repo_root, "build", "libcrypto_api.dll"),
        os.path.join(repo_root, "build", "Release", "crypto_api.dll"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            _Library._instance = _Library(candidate)
            return _Library._instance
    raise CryptoLibraryError(
        "libcrypto_api not found; build it first with "
        "'cmake -S . -B build && cmake --build build' "
        "(searched: {})".format(", ".join(candidates))
    )


def get_local_mac():
    """Return a stable 48-bit machine identifier as colon-separated hex.

    Uses ``uuid.getnode()`` (the real hardware MAC when one is
    available). Server and client on the same host share this value;
    the ``<role>_`` prefix in the key file names keeps them apart.
    """
    node = uuid.getnode()
    return ":".join("{:02x}".format((node >> (8 * i)) & 0xFF) for i in range(5, -1, -1))


class RsaCrypto:
    """Key lifecycle plus RSA-OAEP encrypt/decrypt for one role.

    ``role`` is ``"server"`` or ``"client"`` and is used to name the
    locally generated keypair (``pvt_key/<role>_priv.pem``) and the
    peer registry entries (``pub_key/<peer_role>_<peer_mac>.pem``).
    """

    def __init__(self, role, project_dir, ssh_dir=None):
        self.role = role
        self.project_dir = project_dir
        self.flow_dir = os.path.join(project_dir, ".Flow")
        self.pvt_key_dir = os.path.join(self.flow_dir, "pvt_key")
        self.pub_key_dir = os.path.join(self.flow_dir, "pub_key")
        self.ssh_dir = (
            ssh_dir if ssh_dir is not None else os.path.join(os.path.expanduser("~"), ".ssh")
        )
        self.mac = get_local_mac()
        for directory in (self.pvt_key_dir, self.pub_key_dir):
            os.makedirs(directory, exist_ok=True)
        self.priv_path = None
        self.pub_path = None
        self._priv_key = None
        self._peer_pub_cache = {}  # path -> (mtime, RsaKey)

    # ---- key lifecycle ------------------------------------------------------

    def ensure_keys(self, force_reload=False):
        """Load the RSA keypair (see module docstring) and cache handles."""
        if not force_reload and self._priv_key is not None and self.priv_path is not None:
            return
        lib = load_library()
        priv_candidates = [os.path.join(self.ssh_dir, "id_rsa")]
        priv_path = None
        for candidate in priv_candidates:
            if os.path.exists(candidate):
                try:
                    self._read_priv_handle(lib, candidate)
                    priv_path = candidate
                    break
                except Exception:
                    priv_path = None
                    continue
        if priv_path is None:
            priv_path = os.path.join(self.pvt_key_dir, "{}_priv.pem".format(self.role))
            pub_path = os.path.join(self.pvt_key_dir, "{}_pub.pem".format(self.role))
            if not os.path.exists(priv_path):
                self._generate_keypair(lib, priv_path, pub_path)
        self.priv_path = priv_path
        self.pub_path = os.path.join(self.pvt_key_dir, "{}_pub.pem".format(self.role))
        # (Re)derive the PEM public key from the private key: the SSH
        # public file (~/.ssh/id_rsa.pub) is OpenSSH-format and cannot
        # be parsed by the C library.
        priv_key = self._read_priv_handle(lib, self.priv_path)
        if os.path.exists(self.pub_path):
            try:
                os.remove(self.pub_path)
            except OSError:
                pass
        err = lib.ss_rsa_write_pub(priv_key.handle, self.pub_path.encode("utf-8"))
        if err != SS_OK:
            raise RuntimeError("ss_rsa_write_pub failed: {}".format(err))
        self._priv_key = priv_key

    def reload_own_key(self):
        """Re-read the private key (e.g. after a ~/.ssh rotation)."""
        self._priv_key = None
        self.ensure_keys(force_reload=True)

    def _read_priv_handle(self, lib, path):
        handle = ctypes.c_void_p()
        err = lib.ss_rsa_read_priv(path.encode("utf-8"), None, ctypes.byref(handle))
        if err != SS_OK or not handle.value:
            raise ValueError("cannot parse private key {} (err {})".format(path, err))
        return RsaKey(handle.value, lib)

    def _generate_keypair(self, lib, priv_path, pub_path):
        handle = ctypes.c_void_p()
        err = lib.ss_rsa_keygen(DEFAULT_KEY_BITS, ctypes.byref(handle))
        if err != SS_OK or not handle.value:
            raise RuntimeError("ss_rsa_keygen failed: {}".format(err))
        key = RsaKey(handle.value, lib)
        err = lib.ss_rsa_write_priv(key.handle, priv_path.encode("utf-8"), None)
        if err != SS_OK:
            raise RuntimeError("ss_rsa_write_priv failed: {}".format(err))
        err = lib.ss_rsa_write_pub(key.handle, pub_path.encode("utf-8"))
        if err != SS_OK:
            raise RuntimeError("ss_rsa_write_pub failed: {}".format(err))

    # ---- peer public key registry -------------------------------------------

    def peer_pub_path(self, peer_role, peer_mac):
        """Path of the stored public key for ``peer_role`` on ``peer_mac``.

        The MAC is sanitized for the filesystem (``:`` -> ``_``) so the same
        registry layout works on every platform: ``:`` is an illegal
        filename character on Windows.
        """
        return os.path.join(
            self.pub_key_dir, "{}_{}.pem".format(peer_role, peer_mac.replace(":", "_"))
        )

    def has_peer_key(self, peer_role, peer_mac):
        return os.path.exists(self.peer_pub_path(peer_role, peer_mac))

    def store_peer_key(self, peer_role, peer_mac, src_path):
        """Move a freshly received public key into the registry."""
        dest = self.peer_pub_path(peer_role, peer_mac)
        import shutil

        shutil.move(src_path, dest)
        self._peer_pub_cache.pop(dest, None)

    # ---- encrypt / decrypt ---------------------------------------------------

    def _load_peer_pub(self, path):
        cached = self._peer_pub_cache.get(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        if cached is not None and cached[0] == mtime:
            return cached[1]
        lib = load_library()
        handle = ctypes.c_void_p()
        err = lib.ss_rsa_read_pub(path.encode("utf-8"), ctypes.byref(handle))
        if err != SS_OK or not handle.value:
            return None
        key = RsaKey(handle.value, lib)
        self._peer_pub_cache[path] = (mtime, key)
        return key

    def encrypt_for_peer(self, peer_role, peer_mac, plaintext):
        """Encrypt ``plaintext`` with the peer's public key.

        Returns the ASCII wire body (no trailing newline): each chunk is
        RSA-OAEP encrypted and base64 encoded, chunks joined with ``|``.
        Raises if no peer key is stored yet.
        """
        path = self.peer_pub_path(peer_role, peer_mac)
        key = self._load_peer_pub(path)
        if key is None:
            raise ValueError(
                "no public key for {} {} stored at {}".format(peer_role, peer_mac, path)
            )
        payload = plaintext + VALID_SIGN
        data = payload.encode("utf-8")
        lib = load_library()
        max_len = lib.ss_rsa_max_plaintext_len(key.handle)
        if max_len <= 0:
            raise RuntimeError("ss_rsa_max_plaintext_len returned {}".format(max_len))
        parts = []
        for offset in range(0, len(data), max_len):
            chunk = data[offset : offset + max_len]
            parts.append(self._rsa_encrypt_chunk(lib, key, chunk))
        return "|".join(parts)

    def _rsa_encrypt_chunk(self, lib, key, chunk):
        in_buf = (ctypes.c_uint8 * len(chunk)).from_buffer_copy(chunk)
        out_len = ctypes.c_size_t(0)
        err = lib.ss_rsa_encrypt(key.handle, in_buf, len(chunk), None, ctypes.byref(out_len))
        if err != SS_OK:
            raise RuntimeError("ss_rsa_encrypt (query) failed: {}".format(err))
        out_buf = (ctypes.c_uint8 * out_len.value)()
        err = lib.ss_rsa_encrypt(key.handle, in_buf, len(chunk), out_buf, ctypes.byref(out_len))
        if err != SS_OK:
            raise RuntimeError("ss_rsa_encrypt failed: {}".format(err))
        import base64

        return base64.b64encode(bytes(out_buf[: out_len.value])).decode("ascii")

    def decrypt_with_own(self, wire_body):
        """Decrypt a wire body with our private key.

        Returns ``(True, plaintext)`` on success, or ``(False, None)``
        when the key is stale/wrong or the ``_VALID`` signature is
        missing.
        """
        if self._priv_key is None:
            return False, None
        lib = load_library()
        try:
            import base64

            plain = b""
            for part in wire_body.split("|"):
                if not part:
                    return False, None
                chunk = base64.b64decode(part)
                ok, out = self._rsa_decrypt_chunk(lib, chunk)
                if not ok:
                    return False, None
                plain += out
            if not plain.endswith(VALID_SIGN.encode("utf-8")):
                return False, None
            return True, plain[: -len(VALID_SIGN)].decode("utf-8")
        except Exception:
            return False, None

    def _rsa_decrypt_chunk(self, lib, chunk):
        in_buf = (ctypes.c_uint8 * len(chunk)).from_buffer_copy(chunk)
        out_len = ctypes.c_size_t(0)
        err = lib.ss_rsa_decrypt(
            self._priv_key.handle, in_buf, len(chunk), None, ctypes.byref(out_len)
        )
        if err != SS_OK:
            return False, b""
        out_buf = (ctypes.c_uint8 * out_len.value)()
        err = lib.ss_rsa_decrypt(
            self._priv_key.handle, in_buf, len(chunk), out_buf, ctypes.byref(out_len)
        )
        if err != SS_OK:
            return False, b""
        return True, bytes(out_buf[: out_len.value])
