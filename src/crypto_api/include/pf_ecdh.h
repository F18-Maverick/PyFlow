/*
 * pf_ecdh.h - ECDH key agreement with HKDF key derivation and
 * AES-256-GCM authenticated encryption.
 *
 * Session model:
 *   1. Each side generates a key pair (pf_ecdh_keypair_generate) and
 *      exchanges public keys as PEM (pf_ecdh_pub_to_pem /
 *      pf_ecdh_pub_from_pem).
 *   2. pf_ecdh_derive_key derives a shared session key from the local
 *      private key and the peer public key. Both sides MUST pass the
 *      same salt and info.
 *   3. pf_ecdh_seal / pf_ecdh_open provide a higher-level,
 *      self-contained AEAD: they derive the key internally with a
 *      random salt (prepended to the output) and with the two public
 *      keys bound as HKDF info, so the same pair of keys always agrees
 *      regardless of call order, and a MITM with a different key pair
 *      cannot produce a matching key.
 *
 * SECURITY: ECDH alone provides no authentication. The public keys
 * must be exchanged over an authenticated channel (or signed/verified
 * out of band). The peer key is checked to lie on the curve when
 * parsed.
 *
 * Wire format of pf_ecdh_seal output (all lengths in bytes):
 *   [0:16]  salt        - random HKDF salt
 *   [16:28] iv          - random 12-byte GCM nonce
 *   [28:28+N] ciphertext - N = plain_len
 *   [28+N:44+N] tag     - 16-byte GCM authentication tag
 * Overhead is therefore PF_ECDH_SEAL_OVERHEAD (44) bytes.
 */
#ifndef PF_ECDH_H
#define PF_ECDH_H

#include "pf_crypto.h"

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct pf_ecdh_keypair pf_ecdh_keypair_t; /* local key pair (has private key) */
typedef struct pf_ecdh_pubkey pf_ecdh_pubkey_t;   /* peer public key only */

/* NIST curves supported by pf_ecdh_keypair_generate. */
#define PF_ECDH_CURVE_P256 "P-256"
#define PF_ECDH_CURVE_P384 "P-384"
#define PF_ECDH_CURVE_P521 "P-521"

/* Fixed overhead of the pf_ecdh_seal format: salt(16) + iv(12) + tag(16). */
#define PF_ECDH_SEAL_OVERHEAD 44u

/*
 * Generate an ECDH key pair. curve: one of the PF_ECDH_CURVE_* macros
 * (NULL selects P-256). Unknown curves return PF_ERR_UNSUPPORTED.
 */
PF_CRYPTO_API pf_err_t pf_ecdh_keypair_generate(const char *curve,
                                                pf_ecdh_keypair_t **out_kp);

/* Export the public key as a PEM SubjectPublicKeyInfo string
 * (caller frees with free(3)). */
PF_CRYPTO_API pf_err_t pf_ecdh_pub_to_pem(const pf_ecdh_keypair_t *kp, char **out_pem);

/* Parse a peer public key from PEM. Verifies the point is on the curve. */
PF_CRYPTO_API pf_err_t pf_ecdh_pub_from_pem(const char *pem,
                                            pf_ecdh_pubkey_t **out_pub);

/* Persist / load the private key (PKCS#8 PEM). When passphrase is
 * non-NULL the key is encrypted with AES-256-CBC; the same passphrase
 * is required to load it. */
PF_CRYPTO_API pf_err_t pf_ecdh_keypair_write_priv(const pf_ecdh_keypair_t *kp,
                                                  const char *path,
                                                  const char *passphrase);
PF_CRYPTO_API pf_err_t pf_ecdh_keypair_read_priv(const char *path,
                                                 const char *passphrase,
                                                 pf_ecdh_keypair_t **out_kp);

/*
 * Derive a session key: shared secret -> HKDF-SHA256(salt, info).
 * Both parties MUST supply identical salt and info; recommended info
 * is the concatenation of both public keys (in a canonical order).
 * out_key_len must be <= PF_CRYPTO_HKDF_SHA256_MAX_OUT.
 */
PF_CRYPTO_API pf_err_t pf_ecdh_derive_key(const pf_ecdh_keypair_t *self,
                                          const pf_ecdh_pubkey_t *peer,
                                          const uint8_t *salt, size_t salt_len,
                                          const uint8_t *info, size_t info_len,
                                          uint8_t *out_key, size_t out_key_len);

/*
 * Authenticated encryption to `peer`: derives an AES-256-GCM key via
 * HKDF (random 16-byte salt, info = both public keys in byte-sorted
 * canonical order) and produces the format described at the top of
 * this file. aad (optional, may be NULL/0) is authenticated but not
 * encrypted. On success *out_buf is malloc'd (free with free(3)).
 * Empty plaintext (plain_len == 0) is permitted.
 */
PF_CRYPTO_API pf_err_t pf_ecdh_seal(const pf_ecdh_keypair_t *self,
                                    const pf_ecdh_pubkey_t *peer,
                                    const uint8_t *aad, size_t aad_len,
                                    const uint8_t *plain, size_t plain_len,
                                    uint8_t **out_buf, size_t *out_len);

/* Inverse of pf_ecdh_seal. Returns PF_ERR_AUTH_FAILED on any
 * tampering (bad tag), including a mismatched peer key. */
PF_CRYPTO_API pf_err_t pf_ecdh_open(const pf_ecdh_keypair_t *self,
                                    const pf_ecdh_pubkey_t *peer,
                                    const uint8_t *aad, size_t aad_len,
                                    const uint8_t *in_buf, size_t in_len,
                                    uint8_t **out_plain, size_t *out_len);

PF_CRYPTO_API void pf_ecdh_keypair_free(pf_ecdh_keypair_t *kp);
PF_CRYPTO_API void pf_ecdh_pubkey_free(pf_ecdh_pubkey_t *pub);

#ifdef __cplusplus
}
#endif

#endif /* PF_ECDH_H */
