C/OpenSSL Crypto Module
========================

The ``PyFlow/crypto_api`` directory contains PyFlow's C/OpenSSL
cryptography library. The library is independent from the Python runtime
and is built from the repository root with CMake.

The module provides:

- RSA-OAEP encryption and decryption with SHA-256.
- ECDH key agreement on P-256, P-384, and P-521.
- HKDF-SHA256 session-key derivation.
- AES-256-GCM authenticated encryption for ECDH sessions.
- PEM key persistence and structured error reporting.

The library version is 1.0.0 (``PF_CRYPTO_VERSION_MAJOR`` /
``PF_CRYPTO_VERSION_MINOR`` / ``PF_CRYPTO_VERSION_PATCH`` and
``PF_CRYPTO_VERSION_STRING`` in ``pf_crypto.h``).

The public headers are located in ``PyFlow/crypto_api/include``:

- ``pf_crypto.h`` - common errors, OpenSSL diagnostics, and HKDF.
- ``pf_rsa.h`` - RSA key and encryption APIs.
- ``pf_ecdh.h`` - ECDH key agreement and seal/open APIs.

Build
=====

The module requires OpenSSL 1.1.1 or newer and CMake 3.16 or newer.
On Debian or Ubuntu, install the build dependencies with:

.. code-block:: bash

    sudo apt-get update
    sudo apt-get install -y build-essential cmake libssl-dev

Installing the Python package builds it automatically: ``setup.py`` compiles
the three sources and installs the result inside the package
(``PyFlow/_crypto_api.cpython-3XX-<platform>.so``), so ``pip install
pyflow-net`` needs only a C compiler and the OpenSSL development headers.

Build it from the repository root with CMake when the C test suite or the
C API itself is wanted:

.. code-block:: bash

    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
    cmake --build build --parallel
    ctest --test-dir build --output-on-failure

``ctest --test-dir`` was added in CMake 3.20; with the 3.16 minimum the
equivalent is ``ctest --output-on-failure`` run from inside ``build/``.

The C test suite lives in ``test/crypto_api/`` (``test_hkdf``, ``test_rsa``,
``test_ecdh``). It is built only when ``CRYPTO_API_BUILD_TESTS`` (default
``ON``) and ``BUILD_TESTING`` are both enabled, and then runs together with
the library.

The library is built as a shared object (``libcrypto_api.so``) so it can
be loaded from Python: ``PyFlow/network_api/rsa_crypto.py`` is a ctypes
binding used by the TCP layer to encrypt messages with RSA-OAEP (see the
``TCP_Server_APIs`` / ``TCP_Client_APIs`` encrypted channel sections). The
binding also implements the TCP layer's anti-MITM identity check (TOFU):
peer public keys are exchanged on every connection, recorded under the
peer's ``(ip, port)`` in ``network_api/.Flow/pub_key/pub_key.json`` with
the SHA-256 of the received PEM text, and a changed key for a recorded
endpoint rejects the connection; a key already known under another
endpoint is accepted and re-registered under the new one. The encryption
state machine is bounded: three consecutive decode failures trip a
circuit breaker (``MAX_DECODE_FAILURES`` in ``connect_tcp.py``) that stops
re-keying, and a key that is stale or rotated makes the TCP layer reload
its own key and re-exchange. The binding looks for the library via
``ctypes.util.find_library`` and then next to the repository root in
``build/`` (``libcrypto_api.so``, ``libcrypto_api.dylib``,
``libcrypto_api.dll`` and ``build/Release/crypto_api.dll``).

The build produces the SOVERSIONed names ``libcrypto_api.so.1`` /
``libcrypto_api.so.1.0.0`` plus the ``libcrypto_api.so`` loader symlink.

CMake install rules export the ``crypto_api`` library, its public headers,
and a CMake package configuration. All CMake content sits at the
repository root: the CMake package template and the pkg-config template
are ``cmake/crypto_apiConfig.cmake.in`` and ``crypto_api.pc.in``. Consumers
use the CMake target ``crypto_api::crypto_api`` (or ``find_package(crypto_api)``,
``SameMajorVersion`` compatible), or pkg-config module ``crypto_api``
(``-lcrypto_api`` for linking, ``-lcrypto`` as a private dependency).

Common API
==========

Most public functions return ``pf_err_t``; ``PF_OK`` indicates success.
Functions that report a size or a string instead have their own return
type (``pf_err_string`` returns ``const char *``,
``pf_crypto_openssl_errors`` returns ``void``,
``pf_rsa_ciphertext_len`` and ``pf_rsa_max_plaintext_len`` return
``size_t``, and the ``*_free`` functions return ``void``).
Use ``pf_err_string`` to convert an error code to readable text and
``pf_crypto_openssl_errors`` to retrieve the pending OpenSSL error queue.

.. code-block:: c

    #include "pf_crypto.h"

    pf_err_t error = pf_crypto_hkdf_sha256(
        ikm, ikm_len,
        salt, salt_len,
        info, info_len,
        session_key, 32);

    if (error != PF_OK) {
        fprintf(stderr, "crypto error: %s\n", pf_err_string(error));
    }

The library uses caller-provided output buffers for fixed-size results.
Functions that return allocated buffers document that the caller must
release them with ``free``. Opaque key handles must be released with their
corresponding ``*_free`` function. HKDF output is capped at
``PF_CRYPTO_HKDF_SHA256_MAX_OUT`` (8160 bytes) by both
``pf_crypto_hkdf_sha256`` and ``pf_ecdh_derive_key``.

RSA API
=======

RSA keys are generated with ``pf_rsa_keygen``. The implementation uses
RSA-OAEP with SHA-256 for encryption and decryption. Key sizes from 2048 to
16384 bits are accepted; 2048, 3072, or 4096 bits are recommended.

.. code-block:: c

    pf_rsa_key_t *key = NULL;
    pf_err_t error = pf_rsa_keygen(3072, &key);
    if (error != PF_OK) {
        return error;
    }

    size_t ciphertext_len = 0;
    error = pf_rsa_encrypt(key, plaintext, plaintext_len,
                           NULL, &ciphertext_len);
    if (error == PF_OK) {
        uint8_t *ciphertext = malloc(ciphertext_len);
        error = pf_rsa_encrypt(key, plaintext, plaintext_len,
                               ciphertext, &ciphertext_len);
        free(ciphertext);
    }

    pf_rsa_key_free(key);

The first encryption call with ``out == NULL`` queries the required output
size; ``pf_rsa_decrypt`` supports the same query pattern. RSA encryption
is binary-safe (it accepts an explicit input length, embedded NUL bytes
included) and rejects inputs longer than the value returned by
``pf_rsa_max_plaintext_len`` with ``PF_ERR_INVALID_ARG``.
``pf_rsa_ciphertext_len`` returns the ciphertext size for a key (equal to
its modulus size in bytes), and ``pf_rsa_key_free`` releases a key handle.

Public and private keys can be stored as PEM files:

.. code-block:: c

    pf_rsa_write_pub(key, "server-public.pem");
    pf_rsa_write_priv(key, "server-private.pem", "passphrase");

    pf_rsa_read_pub("server-public.pem", &public_key);
    pf_rsa_read_priv("server-private.pem", "passphrase", &private_key);

ECDH API
========

ECDH key pairs are created with ``pf_ecdh_keypair_generate``. The supported
curve names are:

- ``PF_ECDH_CURVE_P256``
- ``PF_ECDH_CURVE_P384``
- ``PF_ECDH_CURVE_P521``

An unknown curve name returns ``PF_ERR_UNSUPPORTED``, and ``NULL`` selects
P-256. Public keys are exchanged as PEM SubjectPublicKeyInfo strings.
Parsed peer keys are checked to lie on the curve before use.

.. code-block:: c

    pf_ecdh_keypair_t *local = NULL;
    pf_ecdh_pubkey_t *peer = NULL;
    char *public_pem = NULL;

    pf_ecdh_keypair_generate(PF_ECDH_CURVE_P256, &local);
    pf_ecdh_pub_to_pem(local, &public_pem);
    pf_ecdh_pub_from_pem(peer_pem, &peer);

    uint8_t session_key[32];
    pf_ecdh_derive_key(local, peer,
                       salt, salt_len,
                       info, info_len,
                       session_key, sizeof(session_key));

    free(public_pem);
    pf_ecdh_pubkey_free(peer);
    pf_ecdh_keypair_free(local);

The higher-level ``pf_ecdh_seal`` and ``pf_ecdh_open`` APIs are recommended
for application payloads. They derive an AES-256-GCM key with HKDF-SHA256
from a random 16-byte salt and bind both public keys (in byte-sorted
canonical order) as the HKDF ``info``, so the same key pair always agrees
regardless of call order and a peer using a different key pair fails
authentication. They authenticate optional AAD.

The ``pf_ecdh_seal`` output format is:

.. code-block:: text

    salt(16 bytes) | iv(12 bytes) | ciphertext(N bytes) | tag(16 bytes)

The fixed overhead is ``PF_ECDH_SEAL_OVERHEAD`` (44 bytes). Empty plaintext
is allowed. A failed tag check returns ``PF_ERR_AUTH_FAILED`` and no
plaintext is returned. On success the output buffer is ``malloc``-ed and
the caller frees it.

ECDH key pairs can be persisted with ``pf_ecdh_keypair_write_priv`` and
``pf_ecdh_keypair_read_priv`` (PKCS#8 PEM, optionally passphrase-protected
with AES-256-CBC); ``pf_ecdh_keypair_free`` and ``pf_ecdh_pubkey_free``
release the handles.

Security Notes
==============

ECDH provides key agreement, but it does not authenticate public-key
ownership by itself. Public keys must be exchanged over an authenticated
channel or verified with an external signature/certificate mechanism.

Private key files may be protected with a passphrase: ``pf_rsa_write_priv``
and ``pf_ecdh_keypair_write_priv`` encrypt the PKCS#8 PEM with AES-256-CBC
when a passphrase is given, and the matching read function needs the same
passphrase. Applications should
restrict their file permissions and avoid logging passphrases, private keys,
or plaintext session keys.

Error Handling
==============

The main error codes are:

- ``PF_ERR_INVALID_ARG`` - NULL argument, illegal length or bad parameter
  combination (e.g. out-of-range RSA key size, a NULL salt/info with a
  non-zero length, or a plaintext longer than the key allows).
- ``PF_ERR_NOMEM`` - allocation failure.
- ``PF_ERR_OPENSSL`` - OpenSSL operation failure.
- ``PF_ERR_IO`` - file operation failure.
- ``PF_ERR_PARSE`` - malformed PEM/DER input or wrong private-key passphrase.
- ``PF_ERR_BUFFER_TOO_SMALL`` - caller output buffer is insufficient.
- ``PF_ERR_AUTH_FAILED`` - AES-GCM authentication failed.
- ``PF_ERR_UNSUPPORTED`` - unsupported curve or key type.
- ``PF_ERR_DECRYPT`` - RSA decryption failed.

For detailed OpenSSL diagnostics:

.. code-block:: c

    char details[4096];
    pf_crypto_openssl_errors(details, sizeof(details));
    fprintf(stderr, "%s\n", details);
