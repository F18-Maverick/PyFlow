# PyFlow

[![CI](https://github.com/F18-Maverick/PyFlow/workflows/CI/badge.svg)](https://github.com/F18-Maverick/PyFlow/actions)  [![readthedocs](https://img.shields.io/readthedocs/pyflow-net)](https://pyflow-net.readthedocs.io/en/latest/)  [![coverage](https://img.shields.io/codecov/c/github/F18-Maverick/PyFlow)](https://app.codecov.io/gh/F18-Maverick/PyFlow)  [![Pypi](https://img.shields.io/pypi/v/pyflow-net.svg)](https://pypi.org/project/pyflow-net/)  [![supported_version](https://img.shields.io/pypi/pyversions/pyflow-net)](https://img.shields.io/pypi/pyversions/pyflow-net)  [![lisence](https://img.shields.io/github/license/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/blob/main/LICENSE)  [![commit](https://img.shields.io/github/last-commit/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/commits/main/)

PyFlow is a high-level network protocol offering APIs and web apps, both of which transfer messages, files, and folders, along with extensible interfaces and other features.

## Features

- **TCP server / client** — message exchange, custom commands, file transfer, and port allocation over a single control channel (`PyFlow/network_api/connect_tcp.py`).
- **UDP communication** — connectionless messaging (`PyFlow/network_api/connect_udp.py`).
- **Encrypted TCP channel** — RSA-OAEP message encryption with a TOFU (trust-on-first-use) peer-key registry, session nonces and sequence numbers against replay, and a circuit breaker against re-exchange storms. See [docs/Crypto](docs/source/Crypto/Crypto.rst) and the encrypted-channel sections of the TCP API docs.
- **C/OpenSSL cryptography library** — `libcrypto_api` provides RSA-OAEP, ECDH (P-256/384/521), HKDF-SHA256 and AES-256-GCM with a stable C API (`pf_*` prefix) usable from C, CMake or pkg-config.
- **Multi-instance launcher** — `python -m PyFlow` (package entry point backed by `PyFlow/flow_setup.py`) starts one or more server/client instances from a CLI, an interactive prompt, or a `setup.json` configuration file.
- **Extension protocols** — `command_control_extension_tcp.py` (remote command execution with log collection) and `forward_extension_tcp.py` (forwarding messages/files/folders to multiple destinations) plug into any instance via `setup_*_commands()`; `flow_setup.py` loads them automatically for every instance whose `setup.json` config sets `is_extend_command=True`, and starts instances in a background thread when `is_input_command_in_console=False`.
- **Web tool** — `PyFlow/transfer_web/` wraps the TCP protocol in a browser UI for non-library use: `setup_server.py` opens a startup-configuration page (saved to `.Flow_Web/setup_server.json`, same shape as `setup.json`) and then serves a status page plus a client-facing API; `setup_client.py` connects to a server by address, and both pages offer a sidebar of connected instances, message/file/folder sending (with forwarding to other clients), and extension loading. Backed by Flask.

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
├── forward_extension_tcp.py          forward extension over TCP (messages/files/folders to multiple destinations)
├── transfer_web/                     web tool: setup_server.py / setup_client.py launchers,
│   │                                 web_backend/ (Flask + TCP server wrapper),
│   │                                 web_front/ (Flask + TCP client wrapper), static/ (shared UI)
├── __init__.py / __main__.py         package launcher entry (`python -m PyFlow`)
├── flow_setup.py                     launcher implementation
└── setup.json                        default launcher configuration (generated)
test/                        Python tests (unit/ + integration/), C tests under test/crypto_api/
docs/                        documentation build root: Makefile / make.bat / reBuild.sh + _build output
docs/source/                 Sphinx sources (English) with locale/ (ja, ko, ru, zh_CN, zh_TW)
CMakeLists.txt               top-level build for the C library and C tests
```

The Python layer runs on the standard library plus Flask (used only by the `transfer_web` web tool); the C library is loaded at runtime via `ctypes`.

## Requirements

- Python 3.10 or newer
- Pip 25.1 or newer
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
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install --group dev -e .
```

## Quick start

The examples below use `uv run` for uv users; if you installed with pip instead, drop the `uv run` prefix and use `python -m` directly.

### Interactive launcher

```bash
uv run python -m PyFlow
```

Prompts for server/client configuration, writes `setup.json`, and launches the instances.

### Command-line launcher

Start a server listening on `127.0.0.1:12345`:

```bash
uv run python -m PyFlow --type 0 --setup_addr_port 127.0.0.1:12345
```

Start a client that connects to that server (and binds its own local address/port):

```bash
uv run python -m PyFlow --type 1 --setup_addr_port 127.0.0.1:23456 --connect_addr_port 127.0.0.1:12345
```

### Web tool (browser UI)

The web tool wraps the TCP protocol in a browser UI for non-library use
(requires Flask, installed by `uv sync`). Launch it through the package
launcher or directly:

```bash
uv run python -m PyFlow --web_server   # server: config UI -> status page + API
uv run python -m PyFlow --web_client   # client: connect UI -> main UI
```

or directly:

```bash
uv run python PyFlow/transfer_web/setup_server.py
uv run python PyFlow/transfer_web/setup_client.py
```

Visitors of the server's web address get a white landing page (the
addresses clients should connect to) with **Login**, **Register** and
**Change password** buttons; the startup-configuration page, the status
page and the web APIs behind them need a session. Accounts live in the
SQLite database `PyFlow/transfer_web/.Flow_Web/flow_web.db`: username,
email, a PBKDF2-SHA256 password record and a unique 8-character user ID
that other users search by. The first run seeds the administrator
`admin` / `admin` (a legacy `users.json` is imported once and renamed);
while that exact pair is still in use, a login pops up a prominent
warning to change the username and password **before** the server is
exposed to a public network, or anyone who can reach it can administer
it. Administrators manage users (`Users` in the sidebar, which lists the
user IDs) and are the only ones who can change the startup configuration
or load/extend extension protocols; regular users get the status page
with message/file/folder sending. `/api/server_info` stays public, as web
clients query it before they connect.

Registration, password change and code-based client login are verified by
email: an administrator fills in the outgoing mailbox (host, port,
account, authorization code, sender and encryption) in the
startup-configuration page, the settings are checked against the real
SMTP server before they are stored in
`PyFlow/transfer_web/.Flow_Web/email_config.json`, and only then is the
verification mail service started. Codes are valid for 5 minutes, one
code may be requested per minute, and the send button counts the minute
down. Without a working mailbox nobody can register or reset a password;
the seeded administrator can still log in and configure one.

On first run the server launcher opens the startup-configuration page
showing every `TCP_Server_Base` parameter with its default; the saved
config lives in `PyFlow/transfer_web/.Flow_Web/setup_server.json` (same
shape as `setup.json`). Once the TCP server is up, the server's web
backend serves a status page and a client-facing API
(`/api/server_info` returns the TCP address/port). The client launcher
asks for the server address (an `http`/`https` domain or a bare IP) and
connects through the server's web backend; it then asks the account to log
in (username/email, the account password and a verification code mailed to
the account address — both factors are required) and stores the password and
the session token in `PyFlow/transfer_web/.Flow_Web/client_login.json`, so
every reload logs the client in again until **Log out** deletes that file.
Both pages show a sidebar of connected instances and message/file/folder
sending (client-to-client sends are forwarded through the server); a web
client sees only the accounts it is a contact of. Contacts are added with
the **Contacts** button (search by user ID, username or email); the other
side answers the request in its **Requests** list, and only after both
accounts accepted each other does the contact appear in the sidebar.
Extension protocols are loaded by the client page and by administrators on
the server page.

The server page also offers a `"ftp"` share (administrator-only). It is not
the FTP protocol: it browses one folder of the server host and hands the
ticked entries to clients over the protocol's own `/file` and `/file_folder`
transfers. The shared folder is kept in the startup configuration
(`web.ftp_root` of `setup_server.json`), so a restart keeps serving it. The
client's browse dialog takes an optional **Download to** folder: the entries
land there on the client host, and in the default transfer folder
(`PyFlow/network_api/received_files`) when the field is left empty.

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
uv run pytest                       # full Python suite
ctest --test-dir build       # C library tests
```

The encrypted-channel tests (`test/unit/network_api/test_crypto_rsa.py`, `test/integration/network_api/test_crypto_tcp.py`) are skipped automatically when `libcrypto_api` has not been built; everything else runs regardless. The suite passes on Python 3.10–3.14, including the free-threaded (no-GIL) 3.14 build.

## Documentation

Sphinx sources live in `docs/source/` (English source with `ja`/`ko`/`ru`/`zh_CN`/`zh_TW` catalogues under `docs/source/locale/`); `docs/` holds the build wrappers. Build the HTML docs with:

```bash
make -C docs html          # docs/Makefile; docs/make.bat html does the same on Windows
uv run python -m sphinx -b html docs/source build/sphinx_doc   # equivalent, explicit paths
```

Rebuild the translations (extract gettext, machine-translate new strings, compile `.mo`) with `docs/reBuild.sh`; it needs the documentation/translation dependencies from `pyproject.toml` (`sphinx`, `sphinx-intl`, `polib`, `deep-translator`).

## License

[GPL-3.0](LICENSE)
