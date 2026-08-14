# PyFlow

[![CI](https://github.com/F18-Maverick/PyFlow/workflows/CI/badge.svg)](https://github.com/F18-Maverick/PyFlow/actions)

PyFlow is a high-level network protocol with APIs for transferring messages, files, and folders, plus extensible interfaces etc..

## Features

- **TCP server / client** — message exchange, custom commands, file transfer, and port allocation over a single control channel (`PyFlow/network_api/connect_tcp.py`).
- **UDP communication** — connectionless messaging (`PyFlow/network_api/connect_udp.py`).
- **Encrypted TCP channel** — RSA-OAEP message encryption with a TOFU (trust-on-first-use) peer-key registry, session nonces and sequence numbers against replay, and a circuit breaker against re-exchange storms. See [docs/Crypto](docs/Crypto/Crypto.rst) and the encrypted-channel sections of the TCP API docs.
- **C/OpenSSL cryptography library** — `libcrypto_api` provides RSA-OAEP, ECDH (P-256/384/521), HKDF-SHA256 and AES-256-GCM with a stable C API (`pf_*` prefix) usable from C, CMake or pkg-config.
- **Multi-instance launcher** — `python -m PyFlow` (package entry point backed by `PyFlow/flow_setup.py`) starts one or more server/client instances from a CLI, an interactive prompt, or a `setup.json` configuration file.

## Architecture

```
PyFlow/
├── crypto_api/              C/OpenSSL library (pf_crypto, pf_rsa, pf_ecdh)
│   └── include/             public headers: pf_crypto.h, pf_rsa.h, pf_ecdh.h
├── network_api/
│   ├── connect_tcp.py       TCP_Server_Base / TCP_Client_Base
│   ├── connect_udp.py       UDP communication
│   ├── rsa_crypto.py        ctypes binding to libcrypto_api + TOFU key registry
│   └── decode_command_table.json   wire-format table for the file-transfer protocol
├── command_control_extension_tcp.py  command-control extension over TCP
├── __init__.py / __main__.py         package launcher entry (`python -m PyFlow`)
├── flow_setup.py                     launcher implementation
└── setup.json                        default launcher configuration (generated)
test/                        C tests (test_hkdf/test_rsa/test_ecdh) + Python tests
docs/                        Sphinx documentation (multi-language)
CMakeLists.txt               top-level build for the C library and C tests
```

The Python layer runs on the standard library only; the C library is loaded at runtime via `ctypes`.

## Requirements

- Python 3.10 or newer
- CMake 3.16 or newer
- OpenSSL 1.1.1 or newer (development headers, e.g. `libssl-dev` on Debian/Ubuntu)
- A C compiler (gcc/clang on Linux/macOS, MSVC on Windows)

## Build

### 1. Build the C library (required for the encrypted channel)

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure   # optional: run the C test suite
```

This produces `build/libcrypto_api.so` (or `.dylib` / `.dll`), which `rsa_crypto.py` locates automatically.

### 2. Set up the Python environment

With [uv](https://docs.astral.sh/uv/) (the project uses `pyproject.toml` + `uv.lock`):

```bash
uv sync --group dev
```

Or with pip:

```bash
pip install -r requirements.txt          # runtime (pure stdlib) + docs/translation tooling
pip install -r requirements-dev.txt      # development: pytest (plus the above)
```

## Quick start

### Interactive launcher

```bash
python -m PyFlow
```

Prompts for server/client configuration, writes `setup.json`, and launches the instances.

### Command-line launcher

Start a server listening on `127.0.0.1:12345`:

```bash
python -m PyFlow --type 0 --setup_addr_port 127.0.0.1:12345
```

Start a client that connects to that server (and binds its own local address/port):

```bash
python -m PyFlow --type 1 --setup_addr_port 127.0.0.1:23456 --connect_addr_port 127.0.0.1:12345
```

### `setup.json`

A pre-written `setup.json` is honoured by the launcher:

```json
{
  "servers": [
    { "host": "127.0.0.1", "port": 12345, "max_clients": 10,
      "is_extend_command": false, "is_input_command_in_console": true }
  ],
  "clients": []
}
```

### Programmatic use

```python
from PyFlow.network_api.connect_tcp import TCP_Server_Base, TCP_Client_Base

server = TCP_Server_Base(host="127.0.0.1", port=12345, is_extend_command=True)
client = TCP_Client_Base(host="127.0.0.1", port=12345, is_extend_command=True)
```

By default both ends enable the encrypted channel (`is_enable_encrypto=True`): keys come from `~/.ssh/id_rsa` when parseable, otherwise an RSA-2048 pair is generated into `PyFlow/network_api/.Flow/pvt_key/`, and peer keys are exchanged and TOFU-checked on every connection (`PyFlow/network_api/.Flow/pub_key/pub_key.json`). See the TCP API docs for `is_custom_keys` and the full handshake.

## Testing

```bash
pytest                       # full Python suite
ctest --test-dir build       # C library tests
```

The encrypted-channel tests (`test/test_crypto_rsa.py`, `test/test_crypto_tcp.py`) are skipped automatically when `libcrypto_api` has not been built; everything else runs regardless. The suite passes on Python 3.10–3.14, including the free-threaded (no-GIL) 3.14 build.

## Documentation

Sphinx sources live in `docs/` (English source with `ja`/`ko`/`ru`/`zh_CN`/`zh_TW` translations). Build the HTML docs with:

```bash
python -m sphinx -b html docs build/sphinx_doc
```

Rebuild the translations (extract gettext, machine-translate new strings, compile `.mo`) with `docs/reBuild.sh`; it needs the documentation/translation dependencies from `requirements.txt` (`sphinx`, `sphinx-intl`, `polib`, `deep-translator`).

## License

[GPL-3.0](LICENSE)
