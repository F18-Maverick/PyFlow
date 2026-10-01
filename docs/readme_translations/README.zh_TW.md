# PyFlow

[![CI](https://github.com/F18-Maverick/PyFlow/workflows/CI/badge.svg)](https://github.com/F18-Maverick/PyFlow/actions)  [![readthedocs](https://img.shields.io/readthedocs/pyflow-net)](https://pyflow-net.readthedocs.io/en/latest/)  [![coverage](https://img.shields.io/codecov/c/github/F18-Maverick/PyFlow)](https://app.codecov.io/gh/F18-Maverick/PyFlow)  [![Pypi](https://img.shields.io/pypi/v/pyflow-net.svg)](https://pypi.org/project/pyflow-net/)  [![supported_version](https://img.shields.io/pypi/pyversions/pyflow-net)](https://img.shields.io/pypi/pyversions/pyflow-net)  [![lisence](https://img.shields.io/github/license/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/blob/main/LICENSE)  [![commit](https://img.shields.io/github/last-commit/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/commits/main/)

<!-- readme-translations:start -->
[English](../../README.md) | [日本語](README.ja.md) | [简体中文](README.zh_CN.md) | [繁體中文](README.zh_TW.md) | [한국어](README.ko.md) | [Русский](README.ru.md)
<!-- readme-translations:end -->

PyFlow是一種高階網路通訊協定，提供API和Web應用程式，兩者都能傳輸訊息、檔案和資料夾，以及可擴充的介面和其他功能。

## 特色Features

- **TCP伺服器/用戶端** —透過單一控制通道（`PyFlow/network_api/connect_tcp.py`）進行訊息交換、自訂命令、檔案傳輸和連接埠分配。
- **UDP通訊** —無連線訊息(`PyFlow/network_api/connect_udp.py`)。
- **加密的TCP通道** — RSA-OAEP訊息加密，使用TOFU （首次使用信任）對等金鑰登錄、工作階段隨機數和重播序列號，以及防止重新交換風暴的斷路器。請參閱[docs/Crypto](docs/source/Crypto/Crypto.rst)和TCP API文件的加密通道部分。
- **C/OpenSSL加密庫** — `libcrypto_api`提供RSA-OAEP、ECDH （ P-256/384/521 ）、HKDF-SHA256和AES-256-GCM ，以及可從C、CMake或pkg-config使用的穩定C API （`pf_*`前綴）。
- **多實例啟動器** — `python -m PyFlow` （由`PyFlow/flow_setup.py`支持的包入口點）從CLI、交互式提示或`setup.json`配置文件啟動一個或多個伺服器/用戶端實例。
- **擴充功能通訊協定** — `command_control_extension_tcp.py` （使用日誌收集執行遠端命令）和`forward_extension_tcp.py` （將檔案/資料夾上傳到多個目的地；純訊息轉發`forward_send_msg`是TCP層的原生，而不是此擴充功能的一部分）透過`setup_*_commands()`插入任何執行個體； `flow_setup.py`為每個`setup.json`設定`is_extend_command=True`的執行個體自動載入它們，並在`is_input_command_in_console=False`時在後臺執行緒中啟動執行個體。副檔名檔案也可以 持續註冊為`flow_setup.py --add`/`--delete` （保存在`PyFlow/added_extensions.json`中，並在`add_extension.py`之前在每次發射時加載）。
- **Web工具** — `PyFlow/transfer_web/`將TCP通訊協定包裝在瀏覽器UI中以供非圖書館使用： `setup_server.py`開啟啟動配置頁面（儲存為`.Flow_Web/setup_server.json`，形狀與`setup.json`相同） ，然後提供狀態頁面和面向用戶端的API ； `setup_client.py`按地址連接到伺服器，兩個頁面都提供連接執行個體、郵件/檔案/資料夾發送（轉發給其他用戶端）和擴展加載的側邊欄。由Flask支持。

## 系統架構

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

Python圖層在標準庫和Flask上運行（僅由`transfer_web` Web工具使用） ； C庫在運行時通過`ctypes`加載。

## 要求

- Python 3.10或更新版本
- PIP 25.1或更高版本
- C編譯器（ Linux/macOS上的gcc/clang ， Windows上的MSVC ）
- OpenSSL 1.1.1或更新版本（開發標題，例如Debian/Ubuntu上的`libssl-dev` ）
- CMake 3.16或更新版本（僅適用於C測試套件和C消費者）

## 構造

### 1.編譯C函式庫

安裝套件會將其編譯： `setup.py`根據OpenSSL建立`crypto_api` C程式庫，並將其安裝為`PyFlow/_crypto_api.*.so` （ Windows上的`.pyd` ） ，這是`rsa_crypto.py`在執行時加載的內容。

```bash
pip install pyflow-net
```

從結帳開始，相同的構建作為`uv sync`/`pip install -e .`的一部分運行。在Windows上，如果OpenSSL安裝不在標準位置（`C:\Program Files\OpenSSL-Win64`，... ） ，請指向`OPENSSL_ROOT_DIR`。

改為使用CMake構建C庫僅適用於C測試套件和C使用者。它會產生`build/libcrypto_api.so` （或`.dylib`/`.dll`） ， `rsa_crypto.py`也會自動定位。

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure   # optional: run the C test suite
```

### 2.設定Python環境

使用[uv](https://docs.astral.sh/uv/) （專案使用`pyproject.toml` + `uv.lock`） ：

```bash
uv sync --group dev
```

或使用pip ：

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install --group dev -e .
```

## 快速入門

以下示例對紫外線用戶使用`uv run` ；如果您改為使用pip安裝，請刪除`uv run`前綴並直接使用`python -m`。

### 互動式啟動器

```bash
uv run python -m PyFlow
```

提示伺服器/用戶端設定、寫入`setup.json`並啟動執行個體。

### 命令列啟動器

啟動伺服器監聽`127.0.0.1:12345`：

```bash
uv run python -m PyFlow --type 0 --setup_addr_port 127.0.0.1:12345
```

啟動連接到該伺服器的用戶端（並綁定自己的本地地址/端口） ：

```bash
uv run python -m PyFlow --type 1 --setup_addr_port 127.0.0.1:23456 --connect_addr_port 127.0.0.1:12345
```

註冊（或取消註冊）擴充功能通訊協定檔案，以便每次稍後啟動時載入它，無論`is_extend_command`如何：

```bash
uv run python -m PyFlow --add path/to/my_extension.py
uv run python -m PyFlow --delete path/to/my_extension.py
```

註冊由`add_extension.py`儲存在`PyFlow/added_extensions.json`中，並在每次啟動時重新閱讀。

### 網頁工具（瀏覽器使用者介面）

Web工具將TCP協議包裝在瀏覽器UI中以供非圖書館使用（需要Flask ，由`uv sync`安裝）。透過套件啟動器啟動，或直接：

```bash
uv run python -m PyFlow --web_server   # server: config UI -> status page + API
uv run python -m PyFlow --web_client   # client: connect UI -> main UI
```

或直接：

```bash
uv run python PyFlow/transfer_web/setup_server.py
uv run python PyFlow/transfer_web/setup_client.py
```

伺服器網址的訪客會透過**登入**、**註冊**和**變更密碼**按鈕獲得白色登陸頁面（客戶端應連接的地址） ；啟動配置頁面、狀態頁面及其背後的Web API需要工作階段。帳戶存放在SQLite資料庫`PyFlow/transfer_web/.Flow_Web/flow_web.db`中：使用者名稱、電子郵件、PBKDF2-SHA256密碼記錄，以及其他使用者用來搜尋的唯一8個字元的使用者ID。第一次運行播種 管理員`admin`/`admin` （舊版`users.json`匯入一次並重命名） ；當該對仍在使用中時，登入會彈出一個醒目的警告，要求在伺服器**暴露給公共網絡之前**更改用戶名和密碼，或者任何人都可以訪問它來管理它。管理員管理用戶（側邊欄中的`Users` ，其中列出用戶ID ） ，並且是唯一可以更改啟動配置或加載/擴展的用戶 擴充功能通訊協定；一般使用者會在狀態頁面上傳送訊息/檔案/資料夾。`/api/server_info`會維持公開狀態，因為Web用戶端會在連線前查詢它。

通過電子郵件驗證註冊、密碼更改和基於代碼的用戶端登錄：管理員在啟動配置頁面中填寫發送信箱（主機、端口、帳戶、授權代碼、發送者和加密） ，在將設置存儲在`PyFlow/transfer_web/.Flow_Web/email_config.json`中之前根據真實的SMTP服務器進行檢查，然後才啟動驗證郵件服務。驗證碼的有效期為5分鐘，每分鐘可索取一個驗證碼，並且 「傳送」按鈕會計算分鐘數。沒有工作信箱，任何人都無法註冊或重設密碼；種子管理員仍然可以登入並設定密碼。

首次執行時，伺服器啟動器會打開啟動配置頁面，顯示每個`TCP_Server_Base`參數及其預設值；保存的配置以`PyFlow/transfer_web/.Flow_Web/setup_server.json` （與`setup.json`相同的形狀）存在。TCP伺服器啟動後，伺服器的Web後端會提供狀態頁面和面向用戶端的API （`/api/server_info`傳回TCP位址/連接埠）。用戶端啟動器會詢問伺服器位址（ `http`/`https`網域或裸IP ） ，並透過伺服器的Web後端連線；然後詢問 帳戶登錄（用戶名/電子郵件、帳戶密碼和郵寄到帳戶地址的驗證碼—這兩個因素都是必需的） ，並將密碼和會話令牌存儲在`PyFlow/transfer_web/.Flow_Web/client_login.json`中，因此每次重新加載都會再次登錄客戶端，直到**登出**刪除該文件。兩個頁面都顯示連線執行個體和訊息/檔案/資料夾傳送的側邊欄（用戶端到用戶端的傳送會透過伺服器轉寄） ； Web用戶端只會看到 是其聯絡人的帳戶。聯絡人會透過**聯絡人**按鈕新增(依使用者ID、使用者名稱或電子郵件搜尋) ；另一方會在其**請求**清單中回覆請求，只有在兩個帳戶彼此接受後，聯絡人才會顯示在側邊欄中。擴充功能通訊協定由用戶端頁面以及伺服器頁面上的系統管理員載入。Web工具將其剩餘狀態保持在相同的`.Flow_Web/`目錄中： `setup_client.json` （最後 用戶端啟動配置）、`client_last_server.json` （連接頁面提供的最後一個伺服器位址）、`client_extensions_ui.json`/`server_extensions_ui.json` （擴展UI狀態）和`uploads/`暫存目錄。

伺服器頁面還提供`"ftp"`共享（僅限管理員）。這不是FTP通訊協定：它瀏覽伺服器主機的一個資料夾，並透過通訊協定本身的`/file`和`/file_folder`傳輸將勾選的條目交給用戶端。共享文件夾保留在啟動配置中（`web.ftp_root`/`setup_server.json`） ，因此重新啟動將繼續提供服務。客戶端的瀏覽對話框需要一個可選的**下載到**文件夾：條目落在客戶端主機上，並在 當欄位留空時，預設傳輸資料夾(`PyFlow/network_api/received_files`)。

### `setup.json`

預先寫好的`setup.json`會被發射器兌現：

```json
{
  "servers": [
    { "host": "127.0.0.1", "port": 12345, "max_clients": 10,
      "is_extend_command": false, "is_input_command_in_console": true }
  ],
  "clients": []
}
```

### 程式化使用

```python
from PyFlow.network_api.connect_tcp import TCP_Server_Base, TCP_Client_Base

server = TCP_Server_Base(host="127.0.0.1", port=12345, is_extend_command=True)
client = TCP_Client_Base(host="127.0.0.1", port=12345, is_extend_command=True)
```

默認情況下，兩端都啟用加密通道（`is_enable_encrypto=True`） ：可解析時金鑰來自`~/.ssh/id_rsa` ，否則在`PyFlow/network_api/.Flow/pvt_key/`中生成RSA-2048對，並在每個連接上交換對等金鑰並檢查TOFU （`PyFlow/network_api/.Flow/pub_key/pub_key.json`）。請參閱`is_custom_keys`和完整握手的TCP API文件。

## 測試

```bash
uv run pytest                       # full Python suite
ctest --test-dir build       # C library tests
```

未構建`libcrypto_api`時，會自動跳過加密通道測試(`test/unit/network_api/test_crypto_rsa.py`, `test/integration/network_api/test_crypto_tcp.py`) ；其他所有測試都會無論如何執行。CI在Ubuntu和Windows上的Python 3.10–3.14上運行該套件；它還傳遞了自由執行緒（無GIL ） 3.14版本。

## 文章

獅身人面像來源位於`docs/source/` （英語來源， `ja`/`ko`/`ru`/`zh_CN`/`zh_TW`目錄位於`docs/source/locale/`之下） ； `docs/`保存構建包裝。使用以下內容建立HTML文件：

```bash
make -C docs html          # docs/Makefile; docs/make.bat html does the same on Windows
uv run python -m sphinx -b html docs/source build/sphinx_doc   # equivalent, explicit paths
```

使用`docs/reBuild.sh`重建翻譯（提取gettext ，機器翻譯新字符串，編譯`.mo`） ；它需要`pyproject.toml` （`sphinx`， `sphinx-intl`， `polib`， `deep-translator`）的文檔/翻譯依賴關係以及`PATH`上的`python3.14`可執行文件，因為腳本使用該解譯器調用`source/batch_translate_po.py`。它在`docs/_build/html/<lang>`下每種語言寫一棵HTML樹。閱讀文件執行`docs/readthedocs_build.sh`作為其預構建步驟，該步驟僅編譯現有目錄並構建單個HTML樹狀結構 rTD輸出目錄。

## 許可

[GPL-3.0](LICENSE)

