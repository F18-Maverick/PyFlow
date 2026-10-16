/*
 * pf_crypto.h - PyFlow crypto module, common public interface.
 *
 * A self-contained C/OpenSSL cryptography library providing:
 *   - RSA-OAEP key generation, PEM persistence, encrypt/decrypt
 *   - ECDH key agreement with HKDF-SHA256 session key derivation
 *   - AES-256-GCM authenticated encryption (seal/open) bound to an
 *     ECDH key pair, with implicit key confirmation of the peer
 *
 * Design rules:
 *   - Every function returns an pf_err_t; PF_OK (0) means success.
 *   - No global mutable state; the library is thread-safe as long as
 *     each handle is used by one thread at a time.
 *   - Output buffers supplied by the caller are never overrun; the
 *     "query length" pattern (out == NULL) returns the required size
 *     in *out_len.
 *   - Buffers returned via **out (PEM strings, seal/open results) are
 *     allocated with malloc(3) inside the library and MUST be released
 *     with pf_crypto_free(): a caller running on another C runtime
 *     (CPython's pyd/ctypes host on Windows, for example) must not free
 *     them with its own free(3), which would corrupt the heap.
 *
 * Requires OpenSSL >= 1.1.1. Uses only the EVP high-level API.
 */
#ifndef PF_CRYPTO_H
#define PF_CRYPTO_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PF_CRYPTO_VERSION_MAJOR 1
#define PF_CRYPTO_VERSION_MINOR 0
#define PF_CRYPTO_VERSION_PATCH 0
#define PF_CRYPTO_VERSION_STRING "1.0.0"

/* DLL export/import for Windows shared builds; no-op elsewhere. */
#if defined(_WIN32)
#  if defined(PF_CRYPTO_SHARED)
#    define PF_CRYPTO_API __declspec(dllexport)
#  elif defined(PF_CRYPTO_USE_SHARED)
#    define PF_CRYPTO_API __declspec(dllimport)
#  else
#    define PF_CRYPTO_API
#  endif
#else
#  define PF_CRYPTO_API __attribute__((visibility("default")))
#endif

/* Maximum HKDF-SHA256 output: 255 * 32 bytes (RFC 5869). */
#define PF_CRYPTO_HKDF_SHA256_MAX_OUT 8160u

typedef enum {
    PF_OK = 0,               /* success */
    PF_ERR_INVALID_ARG,      /* NULL argument, illegal length, bad combination */
    PF_ERR_NOMEM,            /* memory allocation failed */
    PF_ERR_OPENSSL,          /* underlying OpenSSL operation failed; see
                                pf_crypto_openssl_errors() for detail */
    PF_ERR_IO,               /* file could not be opened/read/written */
    PF_ERR_PARSE,            /* PEM/DER input could not be parsed */
    PF_ERR_BUFFER_TOO_SMALL, /* caller-supplied output buffer too small */
    PF_ERR_AUTH_FAILED,      /* AEAD tag verification failed */
    PF_ERR_UNSUPPORTED,      /* unsupported algorithm or parameter */
    PF_ERR_DECRYPT,          /* decryption failed for a non-authentication
                                reason (e.g. malformed RSA padding) */
} pf_err_t;

/* Human-readable description of an error code. Never returns NULL. */
PF_CRYPTO_API const char *pf_err_string(pf_err_t err);

/*
 * Releases a buffer the library returned through an **out parameter
 * (PEM strings, seal/open results). This is the library's own free(3),
 * so the allocation and the deallocation always run on the same C
 * runtime; passing NULL is a no-op.
 */
PF_CRYPTO_API void pf_crypto_free(void *ptr);

/*
 * Formats the pending OpenSSL error queue into buf (always NUL-terminated,
 * truncated to buf_len). Each call consumes the queue; "no error" is written
 * when the queue is empty. buf may be NULL with buf_len 0 to no-op.
 */
PF_CRYPTO_API void pf_crypto_openssl_errors(char *buf, size_t buf_len);

/*
 * HKDF (RFC 5869) with SHA-256: Extract-and-Expand.
 *   ikm      - input key material (for ECDH: the raw shared secret)
 *   salt     - optional salt; may be NULL/0 (HKDF zero-pads per RFC)
 *   info     - optional context binding; may be NULL/0
 *   out      - output buffer of out_len bytes; out_len <= PF_CRYPTO_HKDF_SHA256_MAX_OUT
 */
PF_CRYPTO_API pf_err_t pf_crypto_hkdf_sha256(
    const uint8_t *ikm, size_t ikm_len,
    const uint8_t *salt, size_t salt_len,
    const uint8_t *info, size_t info_len,
    uint8_t *out, size_t out_len);

#ifdef __cplusplus
}
#endif

#endif /* PF_CRYPTO_H */
