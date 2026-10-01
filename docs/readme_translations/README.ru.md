# PyFlow

[![CI](https://github.com/F18-Maverick/PyFlow/workflows/CI/badge.svg)](https://github.com/F18-Maverick/PyFlow/actions)  [![readthedocs](https://img.shields.io/readthedocs/pyflow-net)](https://pyflow-net.readthedocs.io/en/latest/)  [![coverage](https://img.shields.io/codecov/c/github/F18-Maverick/PyFlow)](https://app.codecov.io/gh/F18-Maverick/PyFlow)  [![Pypi](https://img.shields.io/pypi/v/pyflow-net.svg)](https://pypi.org/project/pyflow-net/)  [![supported_version](https://img.shields.io/pypi/pyversions/pyflow-net)](https://img.shields.io/pypi/pyversions/pyflow-net)  [![lisence](https://img.shields.io/github/license/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/blob/main/LICENSE)  [![commit](https://img.shields.io/github/last-commit/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/commits/main/)

<!-- readme-translations:start -->
[English](../../README.md) | [日本語](README.ja.md) | [简体中文](README.zh_CN.md) | [繁體中文](README.zh_TW.md) | [한국어](README.ko.md) | [Русский](README.ru.md)
<!-- readme-translations:end -->

PyFlow - это высокоуровневый сетевой протокол, предлагающий API и веб-приложения, оба из которых передают сообщения, файлы и папки, а также расширяемые интерфейсы и другие функции.

## Характеристики

- **TCP-сервер / клиент** — обмен сообщениями, пользовательские команды, передача файлов и распределение портов по одному каналу управления (`PyFlow/network_api/connect_tcp.py`).
- **UDP-связь** — обмен сообщениями без установления соединения (`PyFlow/network_api/connect_udp.py`).
- **Зашифрованный канал TCP** — шифрование сообщения RSA-OAEP с реестром одноранговых ключей TOFU (trust-on-first-use), одноразовыми номерами сеансов и порядковыми номерами для повторного воспроизведения и автоматическим выключателем для повторного обмена штормами. См. [docs/Crypto](docs/source/Crypto/Crypto.rst) и разделы зашифрованных каналов в документах TCP API.
- **Криптографическая библиотека C/OpenSSL** — `libcrypto_api` предоставляет RSA-OAEP, ECDH (P-256/384/521), HKDF-SHA256 и AES-256-GCM со стабильным C API (префиксом`pf_*`), используемым из C, CMake или pkg-config.
- **Средство запуска с несколькими экземплярами** — `python -m PyFlow` (точка входа пакета, поддерживаемая `PyFlow/flow_setup.py`) запускает один или несколько экземпляров сервера/клиента из интерфейса командной строки, интерактивной подсказки или файла конфигурации `setup.json`.
- **Extension protocols** — `command_control_extension_tcp.py` (remote command execution with log collection) and `forward_extension_tcp.py` (upload-then-push forwarding of files/folders to multiple destinations; plain-message forwarding `forward_send_msg` is native to the TCP layer, not part of this extension) plug into any instance via `setup_*_commands()`; `flow_setup.py` loads them automatically for every instance whose `setup.json` config sets `is_extend_command=True`, and starts instances in a background thread when `is_input_command_in_console=False`. Extension files can also be registered persistently with `flow_setup.py --add` / `--delete` (kept in `PyFlow/added_extensions.json` and loaded on every launch by `add_extension.py`).
- **Веб-инструмент** — `PyFlow/transfer_web/` переносит протокол TCP в пользовательский интерфейс браузера для использования не в библиотеке: `setup_server.py` открывает страницу конфигурации запуска (сохраненную в `.Flow_Web/setup_server.json`, ту же форму, что и `setup.json`), а затем обслуживает страницу состояния плюс клиентский API; `setup_client.py` подключается к серверу по адресу, и обе страницы предлагают боковую панель подключенных экземпляров, отправку сообщений/файлов/папок (с пересылкой другим клиентам) и загрузку расширений. При поддержке колбы.

## Архитектура

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
├── forward_extension_tcp.py          forward extension over TCP (files/folders to multiple destinations)
├── add_extension.py                  persistent extension registry (added_extensions.json) used by the launcher
├── transfer_web/                     web tool: setup_server.py / setup_client.py launchers,
│   │                                 web_backend/ (Flask + TCP server wrapper),
│   │                                 web_front/ (Flask + TCP client wrapper), static/ (shared UI)
├── __init__.py / __main__.py         package launcher entry (`python -m PyFlow`)
├── flow_setup.py                     launcher implementation
└── setup.json                        default launcher configuration (generated)
test/                        Python tests (unit/ + integration/), C tests under test/crypto_api/
docs/                        documentation build root: Makefile / make.bat / reBuild.sh /
                             readthedocs_build.sh (Read-the-Docs pre-build) + docs/readme_translations/
docs/source/                 Sphinx sources (English) with locale/ (ja, ko, ru, zh_CN, zh_TW), the
                             generated api/ pages, batch_translate_po.py and DOCSTRING_GUIDE.md
CMakeLists.txt               top-level build for the C library and C tests
```

Слой Python работает на стандартной библиотеке плюс Flask (используется только веб-инструментом `transfer_web`); библиотека C загружается во время выполнения через `ctypes`.

## Требования

- Python 3.10 или новее
- Pip 25.1 или новее
- Компилятор C (gcc/clang на Linux/macOS, MSVC на Windows)
- OpenSSL 1.1.1 или новее (заголовки разработки, например `libssl-dev` в Debian/Ubuntu)
- CMake 3.16 or newer (only for the C test suite and for C consumers)

## Сборка

### 1. Compile the C library

Installing the package compiles it: `setup.py` builds the `crypto_api` C library against OpenSSL and installs it as `PyFlow/_crypto_api.*.so` (`.pyd` on Windows), which is what `rsa_crypto.py` loads at runtime.

```bash
pip install pyflow-net
```

Working from a checkout, the same build runs as part of `uv sync` / `pip install -e .`. On Windows, point `OPENSSL_ROOT_DIR` at your OpenSSL installation if it is not in a standard location (`C:\Program Files\OpenSSL-Win64`, ...).

Building the C library with CMake instead is only needed for the C test suite and for C consumers. It produces `build/libcrypto_api.so` (or `.dylib` / `.dll`), which `rsa_crypto.py` also locates automatically.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure   # optional: run the C test suite
```

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

Register (or unregister) an extension protocol file so that every later launch loads it, regardless of `is_extend_command`:

```bash
uv run python -m PyFlow --add path/to/my_extension.py
uv run python -m PyFlow --delete path/to/my_extension.py
```

The registration is stored in `PyFlow/added_extensions.json` by `add_extension.py` and re-read on every launch.

### Web tool (browser UI)

The web tool wraps the TCP protocol in a browser UI for non-library use (requires Flask, installed by `uv sync`). Launch it through the package launcher or directly:

```bash
uv run python -m PyFlow --web_server   # server: config UI -> status page + API
uv run python -m PyFlow --web_client   # client: connect UI -> main UI
```

or directly:

```bash
uv run python PyFlow/transfer_web/setup_server.py
uv run python PyFlow/transfer_web/setup_client.py
```

Visitors of the server's web address get a white landing page (the addresses clients should connect to) with **Login**, **Register** and **Change password** buttons; the startup-configuration page, the status page and the web APIs behind them need a session. Accounts live in the SQLite database `PyFlow/transfer_web/.Flow_Web/flow_web.db`: username, email, a PBKDF2-SHA256 password record and a unique 8-character user ID that other users search by. The first run seeds the administrator `admin` / `admin` (a legacy `users.json` is imported once and renamed); while that exact pair is still in use, a login pops up a prominent warning to change the username and password **before** the server is exposed to a public network, or anyone who can reach it can administer it. Administrators manage users (`Users` in the sidebar, which lists the user IDs) and are the only ones who can change the startup configuration or load/extend extension protocols; regular users get the status page with message/file/folder sending. `/api/server_info` stays public, as web clients query it before they connect.

Registration, password change and code-based client login are verified by email: an administrator fills in the outgoing mailbox (host, port, account, authorization code, sender and encryption) in the startup-configuration page, the settings are checked against the real SMTP server before they are stored in `PyFlow/transfer_web/.Flow_Web/email_config.json`, and only then is the verification mail service started. Codes are valid for 5 minutes, one code may be requested per minute, and the send button counts the minute down. Without a working mailbox nobody can register or reset a password; the seeded administrator can still log in and configure one.

On first run the server launcher opens the startup-configuration page showing every `TCP_Server_Base` parameter with its default; the saved config lives in `PyFlow/transfer_web/.Flow_Web/setup_server.json` (same shape as `setup.json`). Once the TCP server is up, the server's web backend serves a status page and a client-facing API (`/api/server_info` returns the TCP address/port). The client launcher asks for the server address (an `http`/`https` domain or a bare IP) and connects through the server's web backend; it then asks the account to log in (username/email, the account password and a verification code mailed to the account address — both factors are required) and stores the password and the session token in `PyFlow/transfer_web/.Flow_Web/client_login.json`, so every reload logs the client in again until **Log out** deletes that file. Both pages show a sidebar of connected instances and message/file/folder sending (client-to-client sends are forwarded through the server); a web client sees only the accounts it is a contact of. Contacts are added with the **Contacts** button (search by user ID, username or email); the other side answers the request in its **Requests** list, and only after both accounts accepted each other does the contact appear in the sidebar. Extension protocols are loaded by the client page and by administrators on the server page. The web tool keeps its remaining state in the same `.Flow_Web/` directory: `setup_client.json` (last client startup configuration), `client_last_server.json` (last server address offered by the connect page), `client_extensions_ui.json` / `server_extensions_ui.json` (extension UI state) and an `uploads/` staging directory.

The server page also offers a `"ftp"` share (administrator-only). It is not the FTP protocol: it browses one folder of the server host and hands the ticked entries to clients over the protocol's own `/file` and `/file_folder` transfers. The shared folder is kept in the startup configuration (`web.ftp_root` of `setup_server.json`), so a restart keeps serving it. The client's browse dialog takes an optional **Download to** folder: the entries land there on the client host, and in the default transfer folder (`PyFlow/network_api/received_files`) when the field is left empty.

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

The encrypted-channel tests (`test/unit/network_api/test_crypto_rsa.py`, `test/integration/network_api/test_crypto_tcp.py`) are skipped automatically when `libcrypto_api` has not been built; everything else runs regardless. CI runs the suite on Python 3.10–3.14, on both Ubuntu and Windows; it also passes on the free-threaded (no-GIL) 3.14 build.

## Documentation

Sphinx sources live in `docs/source/` (English source with `ja`/`ko`/`ru`/`zh_CN`/`zh_TW` catalogues under `docs/source/locale/`); `docs/` holds the build wrappers. Build the HTML docs with:

```bash
make -C docs html          # docs/Makefile; docs/make.bat html does the same on Windows
uv run python -m sphinx -b html docs/source build/sphinx_doc   # equivalent, explicit paths
```

Rebuild the translations (extract gettext, machine-translate new strings, compile `.mo`) with `docs/reBuild.sh`; it needs the documentation/translation dependencies from `pyproject.toml` (`sphinx`, `sphinx-intl`, `polib`, `deep-translator`) and a `python3.14` executable on `PATH`, because the script calls `source/batch_translate_po.py` with that interpreter. It writes one HTML tree per language under `docs/_build/html/<lang>`. Read the Docs runs `docs/readthedocs_build.sh` as its pre-build step, which only compiles the existing catalogues and builds the single HTML tree for the RTD output directory.

## License

[GPL-3.0](LICENSE)

