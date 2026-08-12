/* Shared test harness: check macros with failure accounting. */
#ifndef PF_CRYPTO_TEST_UTIL_H
#define PF_CRYPTO_TEST_UTIL_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_checks = 0;
static int g_failures = 0;

#define CHECK(cond)                                                             \
    do {                                                                        \
        g_checks++;                                                             \
        if (!(cond)) {                                                          \
            g_failures++;                                                       \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);    \
        }                                                                       \
    } while (0)

#define CHECK_ERR(err, expected)                                                \
    do {                                                                        \
        pf_err_t _e = (err);                                                    \
        g_checks++;                                                             \
        if (_e != (expected)) {                                                 \
            g_failures++;                                                       \
            fprintf(stderr,                                                     \
                    "FAIL %s:%d: %s returned %s (%d), expected %s (%d)\n",      \
                    __FILE__, __LINE__, #err, pf_err_string(_e), (int)_e,       \
                    pf_err_string(expected), (int)(expected));                  \
        }                                                                       \
    } while (0)

#define TEST_MAIN(name)                                                         \
    int main(void) {                                                            \
        run_tests();                                                            \
        if (g_failures == 0) {                                                  \
            printf("%s: PASS (%d checks)\n", name, g_checks);                   \
            return 0;                                                           \
        }                                                                       \
        printf("%s: FAIL (%d/%d checks failed)\n", name, g_failures, g_checks); \
        return 1;                                                               \
    }

#endif /* PF_CRYPTO_TEST_UTIL_H */
