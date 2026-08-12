/* RSA-OAEP test suite. */
#include "pf_rsa.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "test_util.h"

#define PUB_FILE "test_rsa_pub.pem"
#define PRIV_FILE "test_rsa_priv.pem"
#define PRIV_FILE_PW "test_rsa_priv_enc.pem"
#define PASSPHRASE "correct horse battery staple"

static void run_tests(void) {
    pf_rsa_key_t *key = NULL;
    CHECK_ERR(pf_rsa_keygen(2048, &key), PF_OK);
    CHECK(key != NULL);

    /* Key size invariants: 2048-bit RSA -> 256-byte ciphertext. */
    CHECK(pf_rsa_ciphertext_len(key) == 256);
    CHECK(pf_rsa_max_plaintext_len(key) == 256 - 2 * 32 - 2);

    /* Binary roundtrip, including embedded NULs and high bytes. */
    uint8_t msg[100];
    for (size_t i = 0; i < sizeof(msg); i++) {
        msg[i] = (uint8_t)(i * 7 + 1); /* includes 0x00 at i=73 */
    }
    size_t ct_len = 0;
    CHECK_ERR(pf_rsa_encrypt(key, msg, sizeof(msg), NULL, &ct_len), PF_OK);
    CHECK(ct_len == 256);

    uint8_t *ct = (uint8_t *)malloc(ct_len);
    CHECK(ct != NULL);
    CHECK_ERR(pf_rsa_encrypt(key, msg, sizeof(msg), ct, &ct_len), PF_OK);

    size_t pt_len = 0;
    CHECK_ERR(pf_rsa_decrypt(key, ct, ct_len, NULL, &pt_len), PF_OK);
    CHECK(pt_len >= sizeof(msg));
    uint8_t *pt = (uint8_t *)malloc(pt_len);
    CHECK(pt != NULL);
    CHECK_ERR(pf_rsa_decrypt(key, ct, ct_len, pt, &pt_len), PF_OK);
    CHECK(pt_len == sizeof(msg));
    CHECK(memcmp(pt, msg, sizeof(msg)) == 0);
    free(pt);
    free(ct);

    /* Oversized plaintext is rejected up front. */
    size_t dummy = 0;
    CHECK_ERR(pf_rsa_encrypt(key, msg, pf_rsa_max_plaintext_len(key) + 1, NULL, &dummy),
              PF_ERR_INVALID_ARG);

    /* PEM persistence, plaintext private key. */
    CHECK_ERR(pf_rsa_write_pub(key, PUB_FILE), PF_OK);
    CHECK_ERR(pf_rsa_write_priv(key, PRIV_FILE, NULL), PF_OK);

    pf_rsa_key_t *pub_only = NULL;
    CHECK_ERR(pf_rsa_read_pub(PUB_FILE, &pub_only), PF_OK);
    CHECK(pub_only != NULL);

    pf_rsa_key_t *priv_loaded = NULL;
    CHECK_ERR(pf_rsa_read_priv(PRIV_FILE, NULL, &priv_loaded), PF_OK);
    CHECK(priv_loaded != NULL);

    /* Public-key-only handle encrypts; decryption without the private
     * half fails. */
    size_t ct2_len = 0;
    CHECK_ERR(pf_rsa_encrypt(pub_only, msg, 16, NULL, &ct2_len), PF_OK);
    uint8_t *ct2 = (uint8_t *)malloc(ct2_len);
    CHECK(ct2 != NULL);
    CHECK_ERR(pf_rsa_encrypt(pub_only, msg, 16, ct2, &ct2_len), PF_OK);
    uint8_t *pt2 = (uint8_t *)malloc(ct2_len);
    CHECK(pt2 != NULL);
    size_t pt2_len = ct2_len;
    CHECK_ERR(pf_rsa_decrypt(pub_only, ct2, ct2_len, pt2, &pt2_len), PF_ERR_DECRYPT);

    /* The loaded key can decrypt what the public key encrypted. */
    pt2_len = ct2_len;
    CHECK_ERR(pf_rsa_decrypt(priv_loaded, ct2, ct2_len, pt2, &pt2_len), PF_OK);
    CHECK(pt2_len == 16 && memcmp(pt2, msg, 16) == 0);
    free(pt2);
    free(ct2);

    /* Passphrase-protected private key. */
    CHECK_ERR(pf_rsa_write_priv(key, PRIV_FILE_PW, PASSPHRASE), PF_OK);
    pf_rsa_key_t *pw_bad = NULL;
    CHECK_ERR(pf_rsa_read_priv(PRIV_FILE_PW, "wrong", &pw_bad), PF_ERR_PARSE);
    CHECK(pw_bad == NULL);
    pf_rsa_key_t *pw_ok = NULL;
    CHECK_ERR(pf_rsa_read_priv(PRIV_FILE_PW, PASSPHRASE, &pw_ok), PF_OK);
    CHECK(pw_ok != NULL);

    /* Missing file -> I/O error; bad content -> parse error. */
    pf_rsa_key_t *missing = NULL;
    CHECK_ERR(pf_rsa_read_pub("/nonexistent/pub.pem", &missing), PF_ERR_IO);
    CHECK(missing == NULL);

    /* Buffer-too-small path. */
    size_t small = 10;
    uint8_t small_buf[10];
    CHECK_ERR(pf_rsa_encrypt(key, msg, 16, small_buf, &small), PF_ERR_BUFFER_TOO_SMALL);
    CHECK(small == 256);
    pf_rsa_key_free(pw_ok);
    pf_rsa_key_free(pw_bad);
    pf_rsa_key_free(priv_loaded);
    pf_rsa_key_free(pub_only);
    pf_rsa_key_free(key);

    remove(PUB_FILE);
    remove(PRIV_FILE);
    remove(PRIV_FILE_PW);
}

TEST_MAIN("test_rsa")
