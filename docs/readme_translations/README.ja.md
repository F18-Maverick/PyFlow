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
- **拡張プロトコル** — `command_control_extension_tcp.py`（ログ収集によるリモートコマンド実行）および`forward_extension_tcp.py`（メッセージ/ファイル/フォルダを複数の宛先に転送）は、`setup_*_commands()`を介して任意のインスタンスにプラグインされます。`flow_setup.py`は、`setup.json`設定`is_extend_command=True`が設定されているすべてのインスタンスに対して自動的に読み込み、`is_input_command_in_console=False`時にバックグラウンドスレッドでインスタンスを開始します。
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

Pythonレイヤーは、標準ライブラリとFlask （`transfer_web` Webツールでのみ使用）で実行されます。Cライブラリは、`ctypes`を介して実行時にロードされます。

## 要件

- Python 3.10以降
- PIP 25.1以降
- CMake 3.16以降
- OpenSSL 1.1.1以降（ Debian/Ubuntuの`libssl-dev`などの開発ヘッダー）
- Cコンパイラ（ Linux/macOSではgcc/clang、WindowsではMSVC ）

## 構築

### 1. Cライブラリを構築する（暗号化されたチャネルに必要）

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure   # optional: run the C test suite
```

これにより`build/libcrypto_api.so`（または`.dylib`/`.dll`）が生成され、`rsa_crypto.py`は自動的に位置を特定します。

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

最初に実行すると、サーバーランチャーは、デフォルトですべての`TCP_Server_Base`パラメーターを表示するスタートアップコンフィギュレーションページを開きます。保存された構成は`PyFlow/transfer_web/.Flow_Web/setup_server.json`（`setup.json`と同じ形状）にあります。TCPサーバーが起動すると、サーバーのWebバックエンドはステータスページを提供し、クライアント側のAPI （`/api/server_info`はTCPアドレス/ポートを返します）を提供します。クライアントランチャーは、サーバーアドレス（`http`/`https`ドメインまたはベアIP ）を要求し、サーバーのWebバックエンドを介して接続します。次に、 アカウントにログインし（ユーザー名/メールアドレス、アカウントパスワード、およびアカウントアドレスに郵送された確認コード—両方の要素が必要です）、パスワードとセッショントークンを`PyFlow/transfer_web/.Flow_Web/client_login.json`に保存するため、**ログアウト**がそのファイルを削除するまで、すべてのリロードがクライアントを再度ログインさせます。両方のページには、接続されたインスタンスとメッセージ/ファイル/フォルダ送信のサイドバーが表示されます（クライアントからクライアントへの送信はサーバーを介して転送されます）。Webクライアントは、 取引先責任者であるアカウント。連絡先は、**連絡先**ボタン（ユーザーID、ユーザー名、またはメールアドレスで検索）で追加されます。相手側は、**リクエスト**リストでリクエストに応答し、両方のアカウントが互いに承認した後にのみ、連絡先がサイドバーに表示されます。拡張プロトコルは、クライアントページとサーバーページの管理者によって読み込まれます。

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

`libcrypto_api`が構築されていない場合、暗号化されたチャネルテスト（`test/unit/network_api/test_crypto_rsa.py`、`test/integration/network_api/test_crypto_tcp.py`）は自動的にスキップされます。他のすべては関係なく実行されます。このスイートは、フリースレッド（非GIL ） 3.14ビルドを含むPython 3.10-3.14を渡します。

## ドキュメンテーション

スフィンクスのソースは`docs/source/`（`ja`/`ko`/`ru`/`zh_CN`/`zh_TW`カタログが`docs/source/locale/`の英語のソース）にあり、`docs/`はビルドラッパーを保持しています。次のものを使用してHTMLドキュメントを作成します。

```bash
make -C docs html          # docs/Makefile; docs/make.bat html does the same on Windows
uv run python -m sphinx -b html docs/source build/sphinx_doc   # equivalent, explicit paths
```

`docs/reBuild.sh`で翻訳（ gettextの抽出、新しい文字列の機械翻訳、`.mo`のコンパイル）を再構築します。`pyproject.toml`（`sphinx`、`sphinx-intl`、`polib`、`deep-translator`）からのドキュメント/翻訳依存関係が必要です。

## ライセンス

[GPL-3.0](LICENSE)

