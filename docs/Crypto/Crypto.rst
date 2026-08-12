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

Build it from the repository root:

.. code-block:: bash

    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
    cmake --build build --parallel
    ctest --test-dir build --output-on-failure

The C test suite lives in ``test/`` (``test_hkdf``, ``test_rsa``,
``test_ecdh``) and is built and run together with the library.

The library is built as a shared object (``libcrypto_api.so``) so it can
be loaded from Python: ``PyFlow/network_api/rsa_crypto.py`` is a ctypes
binding used by the TCP layer to encrypt messages with RSA-OAEP (see the
``TCP_Server_APIs`` / ``TCP_Client_APIs`` encrypted channel sections). The
binding also implements the TCP layer's anti-MITM identity check (TOFU):
peer public keys are exchanged on every connection, recorded under the
peer's ``(ip, port)`` in ``network_api/.Flow/pub_key/pub_key.json`` with
their SHA-256, and a changed key for a recorded endpoint rejects the
connection. The binding looks for the library via
``ctypes.util.find_library`` and in ``build/`` next to the repository root.

CMake install rules export the ``crypto_api`` library, its public headers,
and a CMake package configuration. All CMake content sits at the
repository root: the CMake package template and the pkg-config template
are ``cmake/crypto_apiConfig.cmake.in`` and ``crypto_api.pc.in``.

Common API
==========

All public functions return ``pf_err_t``. ``PF_OK`` indicates success.
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
corresponding ``*_free`` function.

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
size. RSA encryption is binary-safe and accepts an explicit input length.
The maximum plaintext size is returned by ``pf_rsa_max_plaintext_len``.

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

Public keys are exchanged as PEM SubjectPublicKeyInfo strings. Parsed peer
keys are checked before use.

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
for application payloads. They derive an AES-256-GCM key with HKDF-SHA256,
include a random salt and IV, and authenticate optional AAD.

The ``pf_ecdh_seal`` output format is:

.. code-block:: text

    salt(16 bytes) | iv(12 bytes) | ciphertext(N bytes) | tag(16 bytes)

The fixed overhead is ``PF_ECDH_SEAL_OVERHEAD`` (44 bytes). Empty plaintext
is allowed. A failed tag check returns ``PF_ERR_AUTH_FAILED`` and no
plaintext is returned.

Security Notes
==============

ECDH provides key agreement, but it does not authenticate public-key
ownership by itself. Public keys must be exchanged over an authenticated
channel or verified with an external signature/certificate mechanism.

Private key files may be protected with a passphrase. Applications should
restrict their file permissions and avoid logging passphrases, private keys,
or plaintext session keys.

Error Handling
==============

The main error codes are:

- ``PF_ERR_INVALID_ARG`` - invalid pointer or length.
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
