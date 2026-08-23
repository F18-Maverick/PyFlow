"""crypto_api (C/OpenSSL) RSA integration for PyFlow's TCP layer.

A thin ctypes binding to the shared ``libcrypto_api`` plus the key
lifecycle required by the encrypted TCP channel:

- Reuse an existing RSA keypair from ``~/.ssh`` (PEM private key) when
  one is present and parseable, otherwise generate a fresh keypair into
  ``.Flow/pvt_key``. A caller-supplied keypair (``custom_keys``) is
  honoured when both files parse and the pair matches.
- Anti-MITM identity check (TOFU): every connection exchanges public
  keys in plaintext. Each side records the peer in
  ``.Flow/pub_key/pub_key.json`` under the peer's ``(ip, port)`` with
  the SHA-256 of its public key; a later connection from the same
  endpoint presenting a different key is rejected, and a known key seen
  from a new endpoint is re-registered under the new ``(ip, port)``.
- RSA-OAEP encrypt/decrypt with the ``_VALID`` plaintext signature so a
  stale key (for example a rotated ``~/.ssh`` pair) is detected and the
  peers re-exchange their public keys.

The C library must be built first (``cmake -S . -B build &&
cmake --build build``); see ``load_library`` for the search paths.
"""

import ctypes
import ctypes.util
import contextlib
import hashlib
import json
import os
import tempfile
import threading
import uuid

VALID_SIGN = "_VALID"  # plaintext suffix; a decrypt missing it signals a stale/wrong key

PF_OK = 0  # pf_err_t values, must match pf_crypto.h
PF_ERR_INVALID_ARG = 1
PF_ERR_NOMEM = 2
PF_ERR_OPENSSL = 3
PF_ERR_IO = 4
PF_ERR_PARSE = 5
PF_ERR_BUFFER_TOO_SMALL = 6
PF_ERR_AUTH_FAILED = 7
PF_ERR_UNSUPPORTED = 8
PF_ERR_DECRYPT = 9

DEFAULT_KEY_BITS = 2048  # generated keys (bits); pf_rsa_keygen accepts 2048..16384


class CryptoLibraryError(RuntimeError):
    """Raised when the shared libcrypto_api cannot be loaded."""


class RsaKey:
    """Owns an ``pf_rsa_key_t*`` handle; frees it on GC."""

    def __init__(self, handle, lib):
        self._handle = handle
        self._lib = lib

    @property
    def handle(self):
        return self._handle

    def __del__(self):
        if self._handle:
            try:
                self._lib.pf_rsa_key_free(self._handle)
            except Exception:
                pass
            self._handle = None


class _Library:
    """Lazy singleton wrapper around the ctypes binding."""

    _instance = None

    def __init__(self, path):
        self.lib = ctypes.CDLL(path)
        _configure(self.lib)

    def __getattr__(self, name):  # expose the CDLL functions directly on the wrapper
        return getattr(self.lib, name)


def _configure(lib):
    c_void_p = ctypes.c_void_p
    c_size_t = ctypes.c_size_t
    c_uint8_p = ctypes.POINTER(ctypes.c_uint8)

    lib.pf_rsa_keygen.argtypes = [ctypes.c_int, ctypes.POINTER(c_void_p)]
    lib.pf_rsa_keygen.restype = ctypes.c_int
    lib.pf_rsa_read_pub.argtypes = [ctypes.c_char_p, ctypes.POINTER(c_void_p)]
    lib.pf_rsa_read_pub.restype = ctypes.c_int
    lib.pf_rsa_read_priv.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(c_void_p)]
    lib.pf_rsa_read_priv.restype = ctypes.c_int
    lib.pf_rsa_write_pub.argtypes = [c_void_p, ctypes.c_char_p]
    lib.pf_rsa_write_pub.restype = ctypes.c_int
    lib.pf_rsa_write_priv.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.pf_rsa_write_priv.restype = ctypes.c_int
    lib.pf_rsa_encrypt.argtypes = [
        c_void_p,
        c_uint8_p,
        c_size_t,
        c_uint8_p,
        ctypes.POINTER(c_size_t),
    ]
    lib.pf_rsa_encrypt.restype = ctypes.c_int
    lib.pf_rsa_decrypt.argtypes = [
        c_void_p,
        c_uint8_p,
        c_size_t,
        c_uint8_p,
        ctypes.POINTER(c_size_t),
    ]
    lib.pf_rsa_decrypt.restype = ctypes.c_int
    lib.pf_rsa_max_plaintext_len.argtypes = [c_void_p]
    lib.pf_rsa_max_plaintext_len.restype = c_size_t
    lib.pf_rsa_ciphertext_len.argtypes = [c_void_p]
    lib.pf_rsa_ciphertext_len.restype = c_size_t
    lib.pf_rsa_key_free.argtypes = [c_void_p]
    lib.pf_rsa_key_free.restype = None
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
    peer key cache (``pub_key/<peer_role>_<ip>_<port>.pem``). Peer
    identity is tracked in ``pub_key/pub_key.json`` (TOFU, see
    ``verify_peer_pub``).
    """

    def __init__(self, role, project_dir, ssh_dir=None, custom_keys=None):
        """Create the crypto wrapper for ``role`` ("server" or "client").

        ``custom_keys`` may be a ``[pub_key_path, pvt_key_path]`` pair to
        use a user-supplied RSA keypair instead of the default lookup
        (``~/.ssh`` / generated). The pair is validated on first use
        (paths exist, files parse, the keys match); an invalid pair is
        ignored and the default lookup is used instead.
        """
        self.role = role
        self.project_dir = project_dir
        self.flow_dir = os.path.join(project_dir, ".Flow")
        self.pvt_key_dir = os.path.join(self.flow_dir, "pvt_key")
        self.pub_key_dir = os.path.join(self.flow_dir, "pub_key")
        self.ssh_dir = (
            ssh_dir if ssh_dir is not None else os.path.join(os.path.expanduser("~"), ".ssh")
        )
        self.custom_keys = custom_keys
        self.mac = get_local_mac()
        self.registry_path = os.path.join(self.pub_key_dir, "pub_key.json")
        self._registry_lock = threading.Lock()
        self._key_lock = threading.Lock()  # serialises _priv_key use/reload so a handle is never freed mid-decrypt (no-GIL safe)
        self._peer_pub_cache_lock = threading.Lock()  # peer-key cache; the encrypt loop holds it so a cached handle is never freed mid-encrypt
        for directory in (self.pvt_key_dir, self.pub_key_dir):
            os.makedirs(directory, exist_ok=True)
        self.priv_path = None
        self.pub_path = None
        self._priv_key = None
        self._peer_pub_cache = {}  # path -> (mtime, RsaKey)


    def ensure_keys(self, force_reload=False):
        """Load the RSA keypair (see module docstring) and cache handles.

        Runs under ``_key_lock``: the private-key handle must never be
        replaced (or freed on GC) while another thread is decrypting.
        """
        with self._key_lock:
            if not force_reload and self._priv_key is not None and self.priv_path is not None:
                return
            lib = load_library()
            if self.custom_keys is not None and self._validate_custom_keys(
                lib, self.custom_keys
            ):
                self.priv_path = self.custom_keys[1]
                self.pub_path = self.custom_keys[0]
                self._priv_key = self._read_priv_handle(lib, self.priv_path)
                return
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
            priv_key = self._read_priv_handle(lib, self.priv_path)  # pub re-derived from priv: ~/.ssh pub file is OpenSSH-format, unparseable here
            if os.path.exists(self.pub_path):
                try:
                    os.remove(self.pub_path)
                except OSError:
                    pass
            err = lib.pf_rsa_write_pub(priv_key.handle, self.pub_path.encode("utf-8"))
            if err != PF_OK:
                raise RuntimeError("pf_rsa_write_pub failed: {}".format(err))
            self._priv_key = priv_key

    def reload_own_key(self):
        """Re-read the private key (e.g. after a ~/.ssh rotation)."""
        with self._key_lock:
            self._priv_key = None
        self.ensure_keys(force_reload=True)

    def _read_priv_handle(self, lib, path):
        handle = ctypes.c_void_p()
        err = lib.pf_rsa_read_priv(path.encode("utf-8"), None, ctypes.byref(handle))
        if err != PF_OK or not handle.value:
            raise ValueError("cannot parse private key {} (err {})".format(path, err))
        return RsaKey(handle.value, lib)

    def _validate_custom_keys(self, lib, custom_keys):
        """Check a user-supplied ``[pub_path, pvt_path]`` pair.

        Valid means: both paths exist, both files parse as PEM keys, and
        the public key pairs with the private key (a probe encrypted with
        the public key decrypts with the private key).
        """
        if not isinstance(custom_keys, (list, tuple)) or len(custom_keys) != 2:
            return False
        pub_path, pvt_path = custom_keys
        if not (isinstance(pub_path, str) and isinstance(pvt_path, str)):
            return False
        if not (os.path.isfile(pub_path) and os.path.isfile(pvt_path)):
            return False
        try:
            pub_handle = ctypes.c_void_p()
            if lib.pf_rsa_read_pub(
                pub_path.encode("utf-8"), ctypes.byref(pub_handle)
            ) != PF_OK or not pub_handle.value:
                return False
            pub_key = RsaKey(pub_handle.value, lib)
            priv_handle = ctypes.c_void_p()
            if lib.pf_rsa_read_priv(
                pvt_path.encode("utf-8"), None, ctypes.byref(priv_handle)
            ) != PF_OK or not priv_handle.value:
                return False
            priv_key = RsaKey(priv_handle.value, lib)
            probe = b"custom-key-pair-check"  # pairing probe: pub-encrypt -> priv-decrypt must round-trip
            in_buf = (ctypes.c_uint8 * len(probe)).from_buffer_copy(probe)
            out_len = ctypes.c_size_t(0)
            if lib.pf_rsa_encrypt(
                pub_key.handle, in_buf, len(probe), None, ctypes.byref(out_len)
            ) != PF_OK:
                return False
            out_buf = (ctypes.c_uint8 * out_len.value)()
            if lib.pf_rsa_encrypt(
                pub_key.handle, in_buf, len(probe), out_buf, ctypes.byref(out_len)
            ) != PF_OK:
                return False
            dec_len = ctypes.c_size_t(0)
            if lib.pf_rsa_decrypt(
                priv_key.handle, out_buf, out_len.value, None, ctypes.byref(dec_len)
            ) != PF_OK:
                return False
            dec_buf = (ctypes.c_uint8 * dec_len.value)()
            if lib.pf_rsa_decrypt(
                priv_key.handle, out_buf, out_len.value, dec_buf, ctypes.byref(dec_len)
            ) != PF_OK:
                return False
            return bytes(dec_buf[: dec_len.value]) == probe
        except Exception:
            return False

    def _generate_keypair(self, lib, priv_path, pub_path):
        handle = ctypes.c_void_p()
        err = lib.pf_rsa_keygen(DEFAULT_KEY_BITS, ctypes.byref(handle))
        if err != PF_OK or not handle.value:
            raise RuntimeError("pf_rsa_keygen failed: {}".format(err))
        key = RsaKey(handle.value, lib)
        err = lib.pf_rsa_write_priv(key.handle, priv_path.encode("utf-8"), None)
        if err != PF_OK:
            raise RuntimeError("pf_rsa_write_priv failed: {}".format(err))
        try:
            os.chmod(priv_path, 0o600)  # private key: owner-only
        except OSError:
            pass
        err = lib.pf_rsa_write_pub(key.handle, pub_path.encode("utf-8"))
        if err != PF_OK:
            raise RuntimeError("pf_rsa_write_pub failed: {}".format(err))


    def peer_pem_path(self, peer_role, peer_ip, peer_port):
        """Path of the exchanged public key file for ``peer_role`` at
        ``(peer_ip, peer_port)``.

        The IP is sanitized for the filesystem (``:`` -> ``_`` so IPv6
        literals are safe on every platform, including Windows).
        """
        safe_ip = peer_ip.replace(":", "_")
        return os.path.join(
            self.pub_key_dir, "{}_{}_{}.pem".format(peer_role, safe_ip, peer_port)
        )

    def _load_registry(self):
        """Read ``pub_key.json``: ``{(ip, port)} -> [sha256, pem]``."""
        try:
            with open(self.registry_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_registry(self, registry):
        """Atomically persist the registry (unique tmp file + rename)."""
        fd, tmp = tempfile.mkstemp(dir=self.pub_key_dir, prefix=".pub_key.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(registry, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.registry_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @contextlib.contextmanager
    def _registry_file_lock(self):
        """Cross-process exclusive lock around the registry file.

        The in-process ``_registry_lock`` serialises threads of one
        instance; this lock serialises separate processes (e.g. several
        server instances sharing one ``.Flow`` directory) so the
        read-modify-write of ``pub_key.json`` never loses updates.
        """
        lock_path = self.registry_path + ".lock"
        lock_file = open(lock_path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                if lock_file.tell() == 0:
                    lock_file.write("\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def verify_peer_pub(self, peer_ip, peer_port, peer_pem, peer_role):
        """TOFU check-and-record for a peer public key.

        ``peer_pem`` is the PEM text received on this connection,
        ``(peer_ip, peer_port)`` the endpoint it came from. Returns
        ``(ok, reason)``:

        - key already registered under any endpoint -> accept, and
          re-register it under the current endpoint when it moved
          (IPs are dynamic and ports are user-changeable);
        - key unknown but the endpoint already holds a *different* key
          -> reject (a trusted endpoint suddenly presenting a new key);
        - key and endpoint both unknown -> accept and record (first
          connection is trusted, TOFU).
        """
        sha = hashlib.sha256(peer_pem.encode("utf-8")).hexdigest()
        key_str = str((peer_ip, peer_port))
        with self._registry_lock:
            with self._registry_file_lock():
                registry = self._load_registry()
                for k, v in registry.items():
                    if v[0] == sha:
                        if k == key_str:
                            return True, "known key"
                        if key_str in registry:
                            return False, "endpoint already claimed by another key"
                        del registry[k]
                        registry[key_str] = [sha, peer_pem]
                        self._save_registry(registry)
                        return True, "known key, endpoint updated"
                if key_str in registry:
                    return False, "public key changed for same endpoint"
                registry[key_str] = [sha, peer_pem]
                self._save_registry(registry)
                return True, "first connection (TOFU)"

    def store_peer_pem(self, peer_role, peer_ip, peer_port, src_path):
        """Move a freshly received public key file into the key cache."""
        dest = self.peer_pem_path(peer_role, peer_ip, peer_port)
        import shutil

        shutil.move(src_path, dest)
        with self._peer_pub_cache_lock:
            self._peer_pub_cache.pop(dest, None)


    def _load_peer_pub(self, path):
        """Return the cached RsaKey for ``path``, (re)parsing on change.

        The caller must hold ``_peer_pub_cache_lock``: the returned
        handle is only safe to use while that lock is held (a concurrent
        ``store_peer_pem`` may otherwise free it).
        """
        cached = self._peer_pub_cache.get(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        if cached is not None and cached[0] == mtime:
            return cached[1]
        lib = load_library()
        handle = ctypes.c_void_p()
        err = lib.pf_rsa_read_pub(path.encode("utf-8"), ctypes.byref(handle))
        if err != PF_OK or not handle.value:
            return None
        key = RsaKey(handle.value, lib)
        self._peer_pub_cache[path] = (mtime, key)
        return key

    def encrypt_for_peer(self, peer_pem_path, plaintext):
        """Encrypt ``plaintext`` with the peer's public key file.

        Returns the ASCII wire body (no trailing newline): each chunk is
        RSA-OAEP encrypted and base64 encoded, chunks joined with ``|``.
        Raises if no peer key is stored at ``peer_pem_path`` yet. The
        whole encryption runs under ``_peer_pub_cache_lock`` so the peer
        handle cannot be freed mid-encrypt (no-GIL safe).
        """
        path = peer_pem_path
        with self._peer_pub_cache_lock:
            key = self._load_peer_pub(path)
            if key is None:
                raise ValueError("no public key for peer stored at {}".format(path))
            payload = plaintext + VALID_SIGN
            data = payload.encode("utf-8")
            lib = load_library()
            max_len = lib.pf_rsa_max_plaintext_len(key.handle)
            if max_len <= 0:
                raise RuntimeError("pf_rsa_max_plaintext_len returned {}".format(max_len))
            parts = []
            for offset in range(0, len(data), max_len):
                chunk = data[offset : offset + max_len]
                parts.append(self._rsa_encrypt_chunk(lib, key, chunk))
            return "|".join(parts)

    def _rsa_encrypt_chunk(self, lib, key, chunk):
        in_buf = (ctypes.c_uint8 * len(chunk)).from_buffer_copy(chunk)
        out_len = ctypes.c_size_t(0)
        err = lib.pf_rsa_encrypt(key.handle, in_buf, len(chunk), None, ctypes.byref(out_len))
        if err != PF_OK:
            raise RuntimeError("pf_rsa_encrypt (query) failed: {}".format(err))
        out_buf = (ctypes.c_uint8 * out_len.value)()
        err = lib.pf_rsa_encrypt(key.handle, in_buf, len(chunk), out_buf, ctypes.byref(out_len))
        if err != PF_OK:
            raise RuntimeError("pf_rsa_encrypt failed: {}".format(err))
        import base64

        return base64.b64encode(bytes(out_buf[: out_len.value])).decode("ascii")

    def decrypt_with_own(self, wire_body):
        """Decrypt a wire body with our private key.

        Returns ``(True, plaintext)`` on success, or ``(False, None)``
        when the key is stale/wrong or the ``_VALID`` signature is
        missing. Runs under ``_key_lock`` so the handle cannot be freed
        by a concurrent ``reload_own_key`` (no-GIL safe).
        """
        with self._key_lock:
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
        err = lib.pf_rsa_decrypt(
            self._priv_key.handle, in_buf, len(chunk), None, ctypes.byref(out_len)
        )
        if err != PF_OK:
            return False, b""
        out_buf = (ctypes.c_uint8 * out_len.value)()
        err = lib.pf_rsa_decrypt(
            self._priv_key.handle, in_buf, len(chunk), out_buf, ctypes.byref(out_len)
        )
        if err != PF_OK:
            return False, b""
        return True, bytes(out_buf[: out_len.value])
