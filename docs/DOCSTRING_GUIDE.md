# PyFlow Docstring Guide

## 1. Scope

- **Python**: this guide applies to every `.py` file in the repository.
- **C**: C comments follow the **Doxygen** convention (section 10).

---

## 2. Style: Google docstrings

PyFlow uses **Google style**: `sphinx.ext.napoleon` converts the Args / Returns / Raises sections into reST fields, and `docs/conf.py` enables `sphinx.ext.napoleon` + `sphinx.ext.autodoc` with `napoleon_numpy_docstring = False` (a NumPy-style docstring is rejected rather than silently accepted). No `.. automodule::` page exists yet, so docstrings only reach the built docs once API pages are added (section 11).

> ✅ Correct: Google style (explicit Args / Returns / Raises sections)  
> ❌ Forbidden: NumPy style, reST field lists (`:param:`), unstructured prose

---

## 3. Required fields

| Field | Rule |
|-------|------|
| **Summary line** | One sentence, verb first, ≤ 80 characters, on its own line |
| **Args** | One entry per parameter: `name (type): what it is and its constraints` |
| **Returns** | `type: meaning of the value and its possible values` |
| **Raises** | One entry per exception: `ExceptionType: condition that triggers it` |

> ⚠️ Public API must carry these fields. `Returns` may be omitted when the callable returns nothing, `Raises` when it raises nothing; `Args` and `Raises` must never be dropped.

---

## 4. Prohibited

| Prohibited | Why |
|------------|-----|
| Implementation detail | No algorithm steps, data layout, caching strategy, lock granularity, third-party calls |
| Subjective wording | No "this is efficient", "elegantly designed", "carefully written" |
| Multi-line summary | The summary is a single line; it never wraps |
| Tutorials | Usage examples, best practices, architecture walkthroughs belong in `.rst` |
| Extra blank lines | One blank line between sections, none inside a section |
| Type / annotation conflict | Args must state types when the signature has none, and must match it when it has them; never restate what the signature already says |

---

## 5. Visibility rules

| Visibility | How to recognise it | Docstring requirement |
|------------|---------------------|-----------------------|
| **Public API** | No leading underscore, listed in `__all__`, imported by another module | **Full** docstring (summary + Args + Returns + Raises) |
| **Internal** | Single leading underscore, `_helper` | Summary line only, or nothing at all |
| **Private** | Double leading underscore, `__method` | Same as internal |

> ⚠️ An internal function that is complex or easy to misuse **should** still document Args / Returns.

---

## 6. Writing the summary line

- **Verb first**: `Calculate...`, `Return...`, `Validate...`, `Load...`
- **One sentence**: no semicolons, no clauses chained with conjunctions
- **≤ 80 characters**: keep the core intent, move detail into Args / Returns

| ❌ Counter-example | ✅ Correct |
|--------------------|-----------|
| `This function loads the RSA key from a file and returns it.` | `Load RSA private key from PEM file.` |
| `Encrypt data using OAEP padding with SHA-256.` | `Encrypt plaintext with RSA-OAEP (SHA-256).` |

---

## 7. Writing parameter descriptions

State **what the value is** and **its constraints** — never **how it is used internally**.

| ❌ Counter-example | ✅ Correct |
|--------------------|-----------|
| `key_path: Path to the key file. The function opens it with open() and reads bytes.` | `key_path (str | Path): Path to PEM-encoded private key file. Must exist and be readable.` |
| `timeout: How long to wait. Uses socket.settimeout internally.` | `timeout (float): Maximum seconds to wait for connection. Must be > 0.` |

---

## 8. Complete examples

### 8.1 Simple function (parameters and return value only)

```python
def calculate_checksum(data: bytes, algorithm: str = "crc32") -> int:
    """Calculate checksum of binary data.

    Args:
        data (bytes): Input data to checksum. Must not be empty.
        algorithm (str): Checksum algorithm. One of "crc32", "adler32".

    Returns:
        int: Unsigned 32-bit checksum value.

    Raises:
        ValueError: If data is empty or algorithm is unknown.
    """
```

> PyFlow's own code carries no type annotations today (`PyFlow/` has none), so Args must state the types itself; when a signature does have annotations, Args keeps the same types (see section 4).

### 8.2 Function that raises

```python
def load_library():
    """Locate and load the shared crypto_api library, cached process-wide.

    Returns:
        _Library: Handle exposing the ``pf_*`` ctypes bindings; the same
            instance is returned on every call.

    Raises:
        CryptoLibraryError: If libcrypto_api cannot be located (searched via
            ``ctypes.util.find_library`` and the project's ``build/`` directory).
    """
```

### 8.3 Class `__init__` and public methods

```python
class RsaCrypto:
    """Key lifecycle plus RSA-OAEP encrypt/decrypt for one role.

    Attributes:
        role (str): "server" or "client"; decides which key files are used.
    """

    def __init__(self, role, project_dir, ssh_dir=None, custom_keys=None):
        """Create the crypto wrapper for one role.

        Args:
            role (str): "server" or "client"; must match the peer's expectation.
            project_dir (str): Project root holding the ``.Flow`` key directory.
            ssh_dir (str | None): Extra directory searched for an existing keypair.
            custom_keys (list | None): ``[pub_key_path, pvt_key_path]`` pair used
                instead of the default lookup; ignored when it fails validation.

        Raises:
            OSError: If the key directories cannot be created.
        """
        ...

    def encrypt_for_peer(self, peer_pem_path, plaintext):
        """Encrypt plaintext with the peer's stored public key.

        Args:
            peer_pem_path (str): Cached PEM file holding the peer public key.
            plaintext (str): Text to encrypt; chunked at no more than
                key size - 2 * hash length - 2 bytes per chunk.

        Returns:
            str: ASCII wire body, base64 chunks joined with ``|``, no newline.

        Raises:
            ValueError: If no peer key is stored at ``peer_pem_path``.
            CryptoLibraryError: If libcrypto_api or the peer key cannot be loaded.
        """
        ...

    def decrypt_with_own(self, wire_body):
        """Decrypt a wire body with this instance's private key.

        Args:
            wire_body (str): Wire body produced by a peer's ``encrypt_for_peer``.

        Returns:
            tuple: ``(True, plaintext)`` on success; ``(False, None)`` when the
                key is stale or wrong, or the signature is missing.
        """
        ...
```

---

## 9. Counter-example → correct example

> The counter-example below imitates the prose style of the existing AI-written docstrings in this project.

### ❌ Counter-example (prose, implementation detail, no structured fields)

```python
def _exclusive_file_lock(path):
    """This function provides a cross-process advisory lock using a sidecar
    lock file (path + ".lock"). It uses fcntl on POSIX and msvcrt on Windows
    to achieve advisory locking. The lock is implemented by opening the lock
    file and acquiring an exclusive lock on the file descriptor. On Windows
    we use LockFileEx with exclusive flag. The function yields control to
    the caller while the lock is held, and automatically releases the lock
    when the context exits. It handles the case where the lock file doesn't
    exist by creating it. This is useful for preventing multiple processes
    from writing to the same key file simultaneously which could cause
    corruption. The implementation carefully closes the file descriptor
    in a finally block to ensure no resource leaks.
    """
```

### ✅ Correct (Google style, structured, contract only — no implementation)

```python
@contextlib.contextmanager
def _exclusive_file_lock(path: str | Path) -> Iterator[None]:
    """Cross-process advisory lock via a sidecar file (``path + ".lock"``).

    Args:
        path (str | Path): Target file to protect. Lock file is created
            alongside it with ".lock" suffix.

    Yields:
        None: Lock is held for the duration of the context.

    Raises:
        OSError: If lock file cannot be created or lock acquisition fails.
    """
```

---

## 10. C comments: Doxygen style

The project ships C code (`PyFlow/crypto_api/`, public headers in `include/`), and its comments follow the **Doxygen** convention. Existing headers use plain `/* */` block comments; every new or edited comment is written in Doxygen form (`@brief` mandatory, parameter order matching the declaration):

```c
/**
 * @brief Generate an RSA key pair of @p bits bits.
 *
 * @param[in]  bits     Key size in bits, 2048..16384 (2048/3072/4096 recommended).
 * @param[out] out_key  Receives a new handle; free it with pf_rsa_key_free().
 * @return PF_OK on success, otherwise a pf_err_t code (e.g. PF_ERR_INVALID_ARG).
 *
 * @note Thread-safe; a single handle must be used by one thread at a time.
 */
PF_CRYPTO_API pf_err_t pf_rsa_keygen(int bits, pf_rsa_key_t **out_key);
```

| Tag | Purpose |
|-----|---------|
| `@brief` | One-line summary (same rule as the Python summary line) |
| `@param[in/out]` | Parameter description, marked as input / output |
| `@return` | Meaning of the return value |
| `@note` / `@warning` | Extra contract: thread safety, side effects |
| `@see` | Related function / type |

---

## 11. Working with autodoc

- **The docstring is the single source of API detail**: every parameter, return value, exception and type constraint lives in the docstring.
- **`.rst` documents carry only**: module purpose, usage scenarios, architecture overview, tutorials, best practices, example code.
- **Never repeat in `.rst`** what the docstring already states about parameters / return values / exceptions.
- Pull API pages in with `.. autoclass::` / `.. autofunction::` so the single source stays authoritative.
- **Migration**: existing `.rst` files under `docs/` are left untouched for now; new or rewritten API descriptions go into the docstring per this guide, and pre-existing parameter detail is moved out of `.rst` over time.

---

## 12. Continuous checks

CI job `Docstrings` (`.github/workflows/ci.yml`) runs the same configuration locally available:

| Check | Command | What it reads | Blocking |
|-------|---------|---------------|----------|
| Style | `uvx ruff check` | `D` rules configured in `pyproject.toml`: summary line, blank lines, imperative mood, terminal punctuation, incomplete `Args`, missing docstrings on public API | No (`continue-on-error`, like the other lint steps) |
| Coverage | `uvx interrogate` | Public API of `PyFlow/` (`fail-under` in `pyproject.toml`; raise it with every batch of docstrings) | Yes |

Known limits — check these by hand in review:

- `D417` fires only when an `Args` section exists and skips a parameter; a function whose parameters have **no** `Args` section at all still passes.
- Missing-docstring rules (`D1`) are off under `test/**`: tests are not public API.
- Nothing verifies that `.rst` pages and docstrings do not duplicate each other (section 11).
- Reaching 100% is not required: `D1` is off for private and internal callables by design (section 5), so a pass only means the *public* surface is documented.
- The coverage ratchet ignores `_private` / `__private` names and reads a class plus its `__init__` as one unit (`style = "google"`), matching section 5.

Raise `fail-under` to the newly measured value in the same change that adds the docstrings; never lower it.

---

> Maintainers: when this guide changes, update the documentation checklist in the PR template in the same change.
