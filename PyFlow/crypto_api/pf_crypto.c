/*
 * pf_crypto.c - common utilities: error strings, OpenSSL error dump,
 * HKDF-SHA256.
 */
#include "pf_crypto.h"

#include <stdio.h>
#include <string.h>

#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/kdf.h>

#define COLLECT_ERRORS_MAX 4096

static const char *const kErrStrings[] = {
    "success",                            /* PF_OK */
    "invalid argument",                   /* PF_ERR_INVALID_ARG */
    "out of memory",                      /* PF_ERR_NOMEM */
    "OpenSSL operation failed",           /* PF_ERR_OPENSSL */
    "I/O error",                          /* PF_ERR_IO */
    "parse error",                        /* PF_ERR_PARSE */
    "output buffer too small",            /* PF_ERR_BUFFER_TOO_SMALL */
    "authentication failed",              /* PF_ERR_AUTH_FAILED */
    "unsupported algorithm or parameter", /* PF_ERR_UNSUPPORTED */
    "decryption failed",                  /* PF_ERR_DECRYPT */
};

_Static_assert(sizeof(kErrStrings) / sizeof(kErrStrings[0]) == PF_ERR_DECRYPT + 1,
               "error string table out of sync with pf_err_t");

const char *pf_err_string(pf_err_t err) {
    if (err < 0 || (size_t)err >= sizeof(kErrStrings) / sizeof(kErrStrings[0])) {
        return "unknown error";
    }
    return kErrStrings[err];
}

typedef struct {
    char buf[COLLECT_ERRORS_MAX];
    size_t pos;
} err_sink_t;

static int collect_errors(const char *str, size_t len, void *u) {
    err_sink_t *sink = (err_sink_t *)u;
    size_t copy = sink->pos + len < sizeof(sink->buf) ? len : sizeof(sink->buf) - sink->pos;
    if (copy > 0) {
        memcpy(sink->buf + sink->pos, str, copy);
        sink->pos += copy;
    }
    return 1; /* keep iterating */
}

void pf_crypto_openssl_errors(char *out_buf, size_t buf_len) {
    if (out_buf == NULL || buf_len == 0) {
        return;
    }
    err_sink_t sink = {0};
    ERR_print_errors_cb(collect_errors, &sink);
    if (sink.pos == 0) {
        snprintf(out_buf, buf_len, "no error");
    } else {
        size_t copy = sink.pos < buf_len - 1 ? sink.pos : buf_len - 1;
        memcpy(out_buf, sink.buf, copy);
        out_buf[copy] = '\0';
    }
}

/* OpenSSL 3.0+ uses EVP_PKEY_CTX_add1_hkdf_info, not set1. */
#if OPENSSL_VERSION_NUMBER < 0x30000000L
#  define EV_PKEY_CTX_HKDF_INFO_SET EVP_PKEY_CTX_set1_hkdf_info
#else
#  define EV_PKEY_CTX_HKDF_INFO_SET EVP_PKEY_CTX_add1_hkdf_info
#endif

pf_err_t pf_crypto_hkdf_sha256(const uint8_t *ikm, size_t ikm_len,
                               const uint8_t *salt, size_t salt_len,
                               const uint8_t *info, size_t info_len,
                               uint8_t *out, size_t out_len) {
    if (ikm == NULL || ikm_len == 0) {
        return PF_ERR_INVALID_ARG;
    }
    if (out == NULL || out_len == 0 || out_len > PF_CRYPTO_HKDF_SHA256_MAX_OUT) {
        return PF_ERR_INVALID_ARG;
    }
    if ((salt_len > 0 && salt == NULL) || (info_len > 0 && info == NULL)) {
        return PF_ERR_INVALID_ARG;
    }

    EVP_PKEY_CTX *ctx = EVP_PKEY_CTX_new_id(EVP_PKEY_HKDF, NULL);
    if (ctx == NULL) {
        return PF_ERR_OPENSSL;
    }
    if (EVP_PKEY_derive_init(ctx) <= 0 ||
        EVP_PKEY_CTX_hkdf_mode(ctx, EVP_PKEY_HKDEF_MODE_EXTRACT_AND_EXPAND) <= 0 ||
        EVP_PKEY_CTX_set_hkdf_md(ctx, EVP_sha256()) <= 0 ||
        EVP_PKEY_CTX_set1_hkdf_key(ctx, ikm, ikm_len) <= 0) {
        EVP_PKEY_CTX_free(ctx);
        return PF_ERR_OPENSSL;
    }
    if (salt_len > 0 && EVP_PKEY_CTX_set1_hkdf_salt(ctx, salt, salt_len) <= 0) {
        EVP_PKEY_CTX_free(ctx);
        return PF_ERR_OPENSSL;
    }
    if (info_len > 0 && EV_PKEY_CTX_HKDF_INFO_SET(ctx, info, info_len) <= 0) {
        EVP_PKEY_CTX_free(ctx);
        return PF_ERR_OPENSSL;
    }
    size_t actual = out_len;
    if (EVP_PKEY_derive(ctx, out, &actual) <= 0) {
        EVP_PKEY_CTX_free(ctx);
        return PF_ERR_OPENSSL;
    }
    EVP_PKEY_CTX_free(ctx);
    return PF_OK;
}