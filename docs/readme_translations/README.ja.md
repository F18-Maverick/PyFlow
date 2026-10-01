# PyFlow

[![CI](https://github.com/F18-Maverick/PyFlow/workflows/CI/badge.svg)](https://github.com/F18-Maverick/PyFlow/actions)  [![readthedocs](https://img.shields.io/readthedocs/pyflow-net)](https://pyflow-net.readthedocs.io/en/latest/)  [![coverage](https://img.shields.io/codecov/c/github/F18-Maverick/PyFlow)](https://app.codecov.io/gh/F18-Maverick/PyFlow)  [![Pypi](https://img.shields.io/pypi/v/pyflow-net.svg)](https://pypi.org/project/pyflow-net/)  [![supported_version](https://img.shields.io/pypi/pyversions/pyflow-net)](https://img.shields.io/pypi/pyversions/pyflow-net)  [![lisence](https://img.shields.io/github/license/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/blob/main/LICENSE)  [![commit](https://img.shields.io/github/last-commit/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/commits/main/)

<!-- readme-translations:start -->
[English](../../README.md) | [日本語](README.ja.md) | [简体中文](README.zh_CN.md) | [繁體中文](README.zh_TW.md) | [한국어](README.ko.md) | [Русский](README.ru.md)
<!-- readme-translations:end -->

PyFlowは、拡張可能なインターフェースやその他の機能とともに、メッセージ、ファイル、フォルダを転送するAPIとWebアプリを提供する、高度なネットワークプロトコルです。

## 特長

- **TCPサーバー/クライアント** —単一の制御チャネル（`PyFlow/network_api/connect_tcp.py`）を介したメッセージ交換、カスタムコマンド、ファイル転送、およびポート割り当て。
- **UDP通信** —コネクションレスメッセージング（`PyFlow/network_api/connect_udp.py`）。
- **暗号化されたTCPチャネル** — TOFU （ trust - on - first - use ）ピアキーレジストリ、セッションナンス、リプレイに対するシーケンス番号、再交換ストームに対するサーキットブレーカーを使用したRSA - OAEPメッセージ暗号化。[docs/Crypto](docs/source/Crypto/Crypto.rst)およびTCP APIドキュメントの暗号化されたチャネルセクションを参照してください。
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

## 構築

### 1. Cライブラリをコンパイルする

パッケージをインストールすると、次のようにパッケージがコンパイルされます。`setup.py` OpenSSLに対して`crypto_api` Cライブラリを構築し、`PyFlow/_crypto_api.*.so`（ Windows上では`.pyd`）としてインストールします。これは、`rsa_crypto.py`が実行時にロードするものです。

```bash
pip install pyflow-net
```

チェックアウトから、同じビルドが`uv sync`/`pip install -e .`の一部として実行されます。Windowsの場合、標準の場所（`C:\Program Files\OpenSSL-Win64`、... ）にない場合は、OpenSSLインストールを`OPENSSL_ROOT_DIR`ポイントします。

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

サーバーのWebアドレスの訪問者は、**ログイン**、**登録**および**パスワード変更**ボタンを使用して、白いランディングページ（クライアントが接続するアドレス）を取得します。スタートアップコンフィギュレーションページ、ステータスページ、およびその背後にあるWeb APIにはセッションが必要です。アカウントはSQLiteデータベース`PyFlow/transfer_web/.Flow_Web/flow_web.db`に存在します：ユーザー名、メールアドレス、PBKDF 2 - SHA 256パスワードレコード、および他のユーザーが検索する一意の8文字のユーザーID。最初の実行は、 administrator `admin`/`admin`（レガシー`users.json`は一度インポートされ、名前が変更されます）。その正確なペアがまだ使用されている間、ログインすると、サーバーがパブリックネットワークに公開される前に、**ユーザー名とパスワードを変更するための目立つ警告がポップアップ表示されます**、またはそれに到達できる誰でも管理できます。管理者は、ユーザー（`Users`サイドバーにあり、ユーザーIDが一覧表示されます）を管理し、スタートアップ構成を変更したり、ロード/拡張したりできるのは管理者のみです。 拡張プロトコル。通常のユーザーは、メッセージ/ファイル/フォルダを送信するステータスページを取得します。`/api/server_info`は、Webクライアントが接続する前にクエリを実行するため、公開されたままになります。

登録、パスワード変更、コードベースのクライアントログインは電子メールで検証されます。管理者はスタートアップ構成ページに送信メールボックス（ホスト、ポート、アカウント、認証コード、送信者、暗号化）を入力し、設定は`PyFlow/transfer_web/.Flow_Web/email_config.json`に保存される前に実際のSMTPサーバーと照合され、検証メールサービスが開始されます。コードは5分間有効です。1分に1つのコードがリクエストされる場合があります。 送信ボタンは分をカウントします。作業メールボックスがなければ、誰もパスワードを登録またはリセットすることはできません。シードされた管理者はログインしてパスワードを設定することができます。

最初に実行すると、サーバーランチャーは、デフォルトですべての`TCP_Server_Base`パラメーターを表示するスタートアップコンフィギュレーションページを開きます。保存された構成は`PyFlow/transfer_web/.Flow_Web/setup_server.json`（`setup.json`と同じ形状）にあります。TCPサーバーが起動すると、サーバーのWebバックエンドはステータスページを提供し、クライアント側のAPI （`/api/server_info`はTCPアドレス/ポートを返します）を提供します。クライアントランチャーは、サーバーアドレス（`http`/`https`ドメインまたはベアIP ）を要求し、サーバーのWebバックエンドを介して接続します。次に、 アカウントにログインし（ユーザー名/メールアドレス、アカウントパスワード、およびアカウントアドレスに郵送された確認コード—両方の要素が必要です）、パスワードとセッショントークンを`PyFlow/transfer_web/.Flow_Web/client_login.json`に保存するため、**ログアウト**がそのファイルを削除するまで、すべてのリロードがクライアントを再度ログインさせます。両方のページには、接続されたインスタンスとメッセージ/ファイル/フォルダ送信のサイドバーが表示されます（クライアントからクライアントへの送信はサーバーを介して転送されます）。Webクライアントは、 取引先責任者であるアカウント。連絡先は、**連絡先**ボタン（ユーザーID、ユーザー名、またはメールアドレスで検索）で追加されます。相手側は、**リクエスト**リストでリクエストに応答し、両方のアカウントが互いに承認した後にのみ、連絡先がサイドバーに表示されます。拡張プロトコルは、クライアントページとサーバーページの管理者によって読み込まれます。ウェブツールは、残りの状態を同じ`.Flow_Web/`ディレクトリに保持します：`setup_client.json`（最後に クライアント起動構成）、`client_last_server.json`（接続ページによって提供される最後のサーバーアドレス）、`client_extensions_ui.json`/`server_extensions_ui.json`（拡張UI状態）、および`uploads/`ステージングディレクトリ。

サーバーページには、`"ftp"`共有もあります（管理者のみ）。これはFTPプロトコルではありません。サーバーホストの1つのフォルダを参照し、プロトコル自身の`/file`および`/file_folder`転送を介して、チェックされたエントリをクライアントに渡します。共有フォルダーはスタートアップ構成（`web.ftp_root`/`setup_server.json`）に保持されているため、再起動すると引き続き提供されます。クライアントのブラウズダイアログには、オプションの**Download to**フォルダーがあります。エントリはクライアントホスト上にあり、 フィールドが空のままの場合、デフォルトの転送フォルダ（`PyFlow/network_api/received_files`）。

### `setup.json`

事前に書かれた`setup.json`はランチャーによって表彰されます：

```json
{
  "servers": [
    { "host": "127.0.0.1", "port": 12345, "max_clients": 10,
      "is_extend_command": false, "is_input_command_in_console": true }
  ],
  "clients": []
}
```

### プログラマティックな使用

```python
from PyFlow.network_api.connect_tcp import TCP_Server_Base, TCP_Client_Base

server = TCP_Server_Base(host="127.0.0.1", port=12345, is_extend_command=True)
client = TCP_Client_Base(host="127.0.0.1", port=12345, is_extend_command=True)
```

デフォルトでは、両端は暗号化されたチャネル（`is_enable_encrypto=True`）を有効にします。キーは解析可能な場合は`~/.ssh/id_rsa`から来ます。そうでなければ、RSA -2048ペアが`PyFlow/network_api/.Flow/pvt_key/`に生成され、ピアキーが交換され、すべての接続でTOFUチェックされます（`PyFlow/network_api/.Flow/pub_key/pub_key.json`）。`is_custom_keys`と完全なハンドシェイクについては、TCP APIドキュメントを参照してください。

## テスト

```bash
uv run pytest                       # full Python suite
ctest --test-dir build       # C library tests
```

`libcrypto_api`が構築されていない場合、暗号化されたチャネルテスト（`test/unit/network_api/test_crypto_rsa.py`、`test/integration/network_api/test_crypto_tcp.py`）は自動的にスキップされます。他のすべては関係なく実行されます。CIは、UbuntuとWindowsの両方でPython 3.10-3.14でスイートを実行します。また、フリースレッド（非GIL ） 3.14ビルドも渡します。

## ドキュメンテーション

スフィンクスのソースは`docs/source/`（`ja`/`ko`/`ru`/`zh_CN`/`zh_TW`カタログが`docs/source/locale/`の英語のソース）にあり、`docs/`はビルドラッパーを保持しています。次のものを使用してHTMLドキュメントを作成します。

```bash
make -C docs html          # docs/Makefile; docs/make.bat html does the same on Windows
uv run python -m sphinx -b html docs/source build/sphinx_doc   # equivalent, explicit paths
```

`docs/reBuild.sh`で翻訳（ gettextの抽出、新しい文字列の機械翻訳、`.mo`のコンパイル）を再構築します。スクリプトはそのインタプリタで`source/batch_translate_po.py`を呼び出すため、`pyproject.toml`（`sphinx`、`sphinx-intl`、`polib`、`deep-translator`）と`PATH`の`python3.14`実行可能ファイルからのドキュメント/翻訳依存関係が必要です。`docs/_build/html/<lang>`の下で言語ごとに1つのHTMLツリーを書き込みます。ドキュメントを読む`docs/readthedocs_build.sh`は、既存のカタログのみをコンパイルし、単一のHTMLツリーを構築するプレビルドステップとして実行されます rTD出力ディレクトリ。

## ライセンス

[GPL-3.0](LICENSE)

