# PyFlow

[![CI](https://github.com/F18-Maverick/PyFlow/workflows/CI/badge.svg)](https://github.com/F18-Maverick/PyFlow/actions)  [![readthedocs](https://img.shields.io/readthedocs/pyflow-net)](https://pyflow-net.readthedocs.io/en/latest/)  [![coverage](https://img.shields.io/codecov/c/github/F18-Maverick/PyFlow)](https://app.codecov.io/gh/F18-Maverick/PyFlow)  [![Pypi](https://img.shields.io/pypi/v/pyflow-net.svg)](https://pypi.org/project/pyflow-net/)  [![supported_version](https://img.shields.io/pypi/pyversions/pyflow-net)](https://img.shields.io/pypi/pyversions/pyflow-net)  [![lisence](https://img.shields.io/github/license/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/blob/main/LICENSE)  [![commit](https://img.shields.io/github/last-commit/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/commits/main/)

<!-- readme-translations:start -->
[English](../../README.md) | [日本語](README.ja.md) | [简体中文](README.zh_CN.md) | [繁體中文](README.zh_TW.md) | [한국어](README.ko.md) | [Русский](README.ru.md)
<!-- readme-translations:end -->

PyFlowは、拡張可能なインターフェースやその他の機能とともに、メッセージ、ファイル、フォルダを転送するAPIとWebアプリを提供する、高度なネットワークプロトコルです。

## 特長

- **TCPサーバー/クライアント** —単一の制御チャネル（`PyFlow/network_api/connect_tcp.py`）を介したメッセージ交換、カスタムコマンド、ファイル転送、コマンド拡張API、およびポート割り当て。
- **UDP通信** —コネクションレスメッセージング（`PyFlow/network_api/connect_udp.py`）。
- **Encrypted TCP channel** — after a plaintext key handshake every message and every file/folder/forward transfer connection is framed and encrypted (RSA-OAEP for small text payloads, ECDH-keyed AES-256-GCM for everything else, file data included), with a TOFU (trust-on-first-use) peer-key registry, session nonces and sequence numbers against replay, and a circuit breaker against re-exchange storms. See [docs/Crypto](docs/source/Crypto/Crypto.rst) and the encrypted-channel sections of the TCP API docs.
- **C/OpenSSL暗号ライブラリ** — `libcrypto_api`は、RSA - OAEP、ECDH （ P -256/384/521 ）、HKDF - SHA 256、およびAES -256 - GCMに、C、CMake、またはpkg - configから使用できる安定したC API （`pf_*`プレフィックス）を提供します。
- **マルチインスタンスランチャー** — `python -m PyFlow`（`PyFlow/flow_setup.py`がサポートするパッケージエントリポイント）は、CLI、インタラクティブプロンプト、または`setup.json`構成ファイルから1つ以上のサーバー/クライアントインスタンスを開始します。
- **Extension protocols** — `command_control_extension_tcp.py` (remote command execution with log collection) and `forward_extension_tcp.py` (upload-then-push forwarding of files/folders to multiple destinations; plain-message forwarding `forward_send_msg` is native to the TCP layer, not part of this extension) plug into any instance via `setup_*_commands()`; `flow_setup.py` loads them automatically for every instance whose `setup.json` config sets `is_extend_command=True`, and starts instances in a background thread when `is_input_command_in_console=False`. Extension files can also be registered persistently with `flow_setup.py --add` / `--delete` (kept in `PyFlow/added_extensions.json` and loaded on every launch by `add_extension.py`).
- **Webツール** — `PyFlow/transfer_web/`は、図書館以外で使用するためにブラウザUIでTCPプロトコルをラップします。`setup_server.py`スタートアップコンフィギュレーションページ（`.Flow_Web/setup_server.json`に保存され、`setup.json`と同じ形状）を開き、ステータスページとクライアント対応APIを提供します。`setup_client.py`アドレスでサーバーに接続し、両方のページで接続されたインスタンス、メッセージ/ファイル/フォルダ送信（他のクライアントへの転送付き）、および拡張機能の読み込みのサイドバーを提供します。Flaskに裏打ちされています。

## アーキテクチャー

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

Pythonレイヤーは、標準ライブラリとFlask （`transfer_web` Webツールでのみ使用）で実行されます。Cライブラリは、`ctypes`を介して実行時にロードされます。

## 要件

- Python 3.10以降
- PIP 25.1以降
- Cコンパイラ（ Linux/macOSではgcc/clang、WindowsではMSVC ）
- OpenSSL 1.1.1以降（ Debian/Ubuntuの`libssl-dev`などの開発ヘッダー）
- CMake 3.16以降（ CテストスイートとCコンシューマーのみ）

ソース配布のみがPyPIに公開されるため、Cライブラリはパッケージをインストールするマシン上でコンパイルされます。Windowsでは、`pip install`の前にコンパイラとOpenSSL開発ファイルがあることを意味します。

- Visual Studio **ビルドツール** (C ++ワークロード、"MSVC v 143"およびWindows SDK):
<https://visualstudio.microsoft.com/visual-cpp-build-tools/>.コンパイラは自動的に検索されます。それがなければ、Microsoft独自の「Microsoft Visual C ++ 14.0以降が必要です」というメッセージでpipが停止します。
- Win 64インストーラーからのOpenSSL
<https://slproweb.com/products/Win32OpenSSL.html> —デフォルト（非「Light」）インストーラーには開発ファイルが含まれていますが、「Light」には含まれていません。ビルドが探している`C:\Program Files\OpenSSL`に着陸します。他の場所にある場合、`OPENSSL_ROOT_DIR`は検索を上書きします。

## 構築

### 1. Cライブラリをコンパイルする

パッケージをインストールすると、次のようにパッケージがコンパイルされます。`setup.py` OpenSSLに対して`crypto_api` Cライブラリを構築し、`PyFlow/_crypto_api.*.so`（ Windows上では`.pyd`）としてインストールします。これは、`rsa_crypto.py`が実行時にロードするものです。

```bash
pip install pyflow-net
```

チェックアウトから、同じビルドが`uv sync`/`pip install -e .`の一部として実行されます。

代わりにCMakeを使用してCライブラリを構築することは、CテストスイートとCコンシューマーにのみ必要です。`build/libcrypto_api.so`（または`.dylib`/`.dll`）を生成し、`rsa_crypto.py`も自動的に位置を特定します。

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure   # optional: run the C test suite
```

### 2. Python環境を設定する

[uv](https://docs.astral.sh/uv/)の場合（プロジェクトは`pyproject.toml`+`uv.lock`を使用） ：

```bash
uv sync --group dev
```

またはpipを使用する場合：

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install --group dev -e .
```

## クイックスタート

以下の例では、UVユーザーに`uv run`を使用しています。代わりにpipを使用してインストールした場合は、`uv run`プレフィックスをドロップして`python -m`を直接使用します。

### インタラクティブランチャー

```bash
uv run python -m PyFlow
```

サーバー/クライアント設定のプロンプトを表示し、`setup.json`を書き込み、インスタンスを起動します。

### コマンドラインランチャー

`127.0.0.1:12345`でサーバーのリッスンを開始します：

```bash
uv run python -m PyFlow --type 0 --setup_addr_port 127.0.0.1:12345
```

そのサーバーに接続するクライアントを起動します（および独自のローカルアドレス/ポートをバインドします）。

```bash
uv run python -m PyFlow --type 1 --setup_addr_port 127.0.0.1:23456 --connect_addr_port 127.0.0.1:12345
```

`is_extend_command`に関係なく、後で起動するたびにロードされるように、拡張プロトコルファイルを登録（または登録解除）します。

```bash
uv run python -m PyFlow --add path/to/my_extension.py
uv run python -m PyFlow --delete path/to/my_extension.py
```

登録は`add_extension.py`までに`PyFlow/added_extensions.json`に保存され、起動ごとに再読み込みされます。

### Webツール（ブラウザUI ）

Webツールは、非ライブラリ使用のためにブラウザUIにTCPプロトコルをラップします（`uv sync`によってインストールされたFlaskが必要です）。パッケージランチャーから起動するか、次の手順に従います。

```bash
uv run python -m PyFlow --web_server   # server: config UI -> status page + API
uv run python -m PyFlow --web_client   # client: connect UI -> main UI
```

または直接：

```bash
uv run python PyFlow/transfer_web/setup_server.py
uv run python PyFlow/transfer_web/setup_client.py
```

Visitors of the server's web address get a white landing page (the addresses clients should connect to) with a **Login** button, plus **Register** and **Change password** while a verification mailbox is configured; the startup-configuration page, the status page and the web APIs behind them need a session. Accounts live in the SQLite database `PyFlow/transfer_web/.Flow_Web/flow_web.db`: username, email, a PBKDF2-SHA256 password record and a unique 8-character user ID that other users search by. The first run seeds the administrator `admin` / `admin` (a legacy `users.json` is imported once and renamed); while that exact pair is still in use, a login pops up a prominent warning to change the username and password **before** the server is exposed to a public network, or anyone who can reach it can administer it. Administrators manage users (`Users` in the sidebar, which lists the user IDs) and are the only ones who can change the startup configuration or load/extend extension protocols; regular users get the status page with message/file/folder sending. `/api/server_info` stays public, as web clients query it before they connect.

The verification mailbox decides how clients connect: with it the server is meant for public networks, without it the server serves an internal network. An administrator fills in the outgoing mailbox (host, port, account, authorization code, sender and encryption) in the startup-configuration page, the settings are checked against the real SMTP server before they are stored in `PyFlow/transfer_web/.Flow_Web/email_config.json`, and only then is the verification mail service started: registration, password reset and every client login are then verified by email. Codes are valid for 5 minutes, one code may be requested per minute, and the send button counts the minute down. The same card offers **Disable Mail Service**, which removes the stored mailbox and switches the server back to internal mode. While no mailbox is configured, no account is needed on the client side at all: no registration, no password reset, no client login — clients connect and use the tool directly, and only the console administrator logs in to administer the server.

On first run the server launcher opens the startup-configuration page showing every `TCP_Server_Base` parameter with its default; the saved config lives in `PyFlow/transfer_web/.Flow_Web/setup_server.json` (same shape as `setup.json`). Once the TCP server is up, the server's web backend serves a status page and a client-facing API (`/api/server_info` returns the TCP address/port and whether clients have to log in). The client launcher asks for the server address (an `http`/`https` domain or a bare IP) and connects through the server's web backend. A server that asks for accounts makes the client log in (username/email, the account password and a verification code mailed to the account address — both factors are required) and stores the password and the session token in `PyFlow/transfer_web/.Flow_Web/client_login.json`, so every reload logs the client in again until **Log out** deletes that file; an internal server skips the login window and the client's account buttons (**Contacts**, **Requests**, **User info**, **Log out**) do not appear. Both pages show a sidebar of connected instances and message/file/folder sending (client-to-client sends are forwarded through the server); on a public server a web client sees only its contacts, while internal clients see every connected instance. Contacts are added with the **Contacts** button (search by user ID, username or email); the other side answers the request in its **Requests** list, and only after both accounts accepted each other does the contact appear in the sidebar. Every file or folder message links the transferred name: clicking it asks the backend that recorded the transfer to open that location in the file manager of its host — a received file is revealed inside its arrival folder, a sent one opens the staged copy in `uploads/`. The browser only ever carries the opaque token of the record, never a filesystem path. Extension protocols are loaded by the client page and by administrators on the server page. The web tool keeps its remaining state in the same `.Flow_Web/` directory: `setup_client.json` (last client startup configuration), `client_last_server.json` (last server address offered by the connect page), `client_extensions_ui.json` / `server_extensions_ui.json` (extension UI state) and an `uploads/` staging directory.

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

