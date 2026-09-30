# PyFlow

[![CI](https://github.com/F18-Maverick/PyFlow/workflows/CI/badge.svg)](https://github.com/F18-Maverick/PyFlow/actions)  [![readthedocs](https://img.shields.io/readthedocs/pyflow-net)](https://pyflow-net.readthedocs.io/en/latest/)  [![coverage](https://img.shields.io/codecov/c/github/F18-Maverick/PyFlow)](https://app.codecov.io/gh/F18-Maverick/PyFlow)  [![Pypi](https://img.shields.io/pypi/v/pyflow-net.svg)](https://pypi.org/project/pyflow-net/)  [![supported_version](https://img.shields.io/pypi/pyversions/pyflow-net)](https://img.shields.io/pypi/pyversions/pyflow-net)  [![lisence](https://img.shields.io/github/license/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/blob/main/LICENSE)  [![commit](https://img.shields.io/github/last-commit/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/commits/main/)

<!-- readme-translations:start -->
[English](../../README.md) | [日本語](README.ja.md) | [简体中文](README.zh_CN.md) | [繁體中文](README.zh_TW.md) | [한국어](README.ko.md) | [Русский](README.ru.md)
<!-- readme-translations:end -->

PyFlow是一种高级网络协议，提供API和Web应用程序，两者都传输消息、文件和文件夹，以及可扩展的界面和其他功能。

## 特点

- **TCP服务器/客户端** —通过单个控制通道（`PyFlow/network_api/connect_tcp.py`）进行消息交换、自定义命令、文件传输和端口分配。
- **UDP通信** —无连接消息传递(`PyFlow/network_api/connect_udp.py`)。
- **加密TCP通道** — RSA-OAEP消息加密，具有TOFU （首次使用信任）对等密钥注册表、会话随机数和重播的序列号，以及防止重新交换风暴的断路器。请参阅[docs/Crypto](docs/source/Crypto/Crypto.rst)和TCP API文档的加密通道部分。
- **C/OpenSSL加密库** — `libcrypto_api`提供RSA-OAEP、ECDH （ P-256/384/521 ）、HKDF-SHA256和AES-256-GCM ，以及可从C、CMake或pkg-config使用的稳定C API （`pf_*`前缀）。
- **多实例启动器** — `python -m PyFlow` （包入口点由`PyFlow/flow_setup.py`支持）从CLI、交互式提示或`setup.json`配置文件启动一个或多个服务器/客户端实例。
- **扩展协议** — `command_control_extension_tcp.py` （使用日志收集执行远程命令）和`forward_extension_tcp.py` （将消息/文件/文件夹转发到多个目标）通过`setup_*_commands()`插入任何实例； `flow_setup.py`为`setup.json`配置设置`is_extend_command=True`的每个实例自动加载它们，并在`is_input_command_in_console=False`时在后台线程中启动实例。
- **Web工具** — `PyFlow/transfer_web/`将TCP协议包装在浏览器UI中，供非库使用： `setup_server.py`打开启动配置页面（保存到`.Flow_Web/setup_server.json`，形状与`setup.json`相同） ，然后提供状态页面和面向客户端的API ； `setup_client.py`通过地址连接到服务器，并且两个页面都提供连接的实例、消息/文件/文件夹发送（转发到其他客户端）和扩展加载的侧边栏。由Flask支持。

## 建筑

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

Python层在标准库和Flask上运行（仅由`transfer_web` web工具使用） ； C库在运行时通过`ctypes`加载。

## 需求

- Python 3.10或更高版本
- PIP 25.1或更高版本
- CMake 3.16或更高版本
- OpenSSL 1.1.1或更高版本（开发标头，例如Debian/Ubuntu上的`libssl-dev` ）
- C编译器（ Linux/macOS上的gcc/clang ， Windows上的MSVC ）

## 打造

### 1.构建C库（加密通道需要）

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure   # optional: run the C test suite
```

这会产生`build/libcrypto_api.so` （或`.dylib`/`.dll`） ， `rsa_crypto.py`会自动定位。

### 2.设置Python环境

使用[uv](https://docs.astral.sh/uv/) （项目使用`pyproject.toml` + `uv.lock`） ：

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

## 快速启动

以下示例对紫外线用户使用`uv run` ；如果您使用pip安装，请删除`uv run`前缀并直接使用`python -m`。

### 交互式启动器

```bash
uv run python -m PyFlow
```

提示服务器/客户端配置，写入`setup.json`，并启动实例。

### 命令行启动器

启动监听`127.0.0.1:12345`的服务器：

```bash
uv run python -m PyFlow --type 0 --setup_addr_port 127.0.0.1:12345
```

启动连接到该服务器的客户端（并绑定自己的本地地址/端口） ：

```bash
uv run python -m PyFlow --type 1 --setup_addr_port 127.0.0.1:23456 --connect_addr_port 127.0.0.1:12345
```

### Web工具（浏览器UI ）

Web工具将TCP协议包装在浏览器UI中以供非库使用（需要Flask ，由`uv sync`安装）。通过软件包启动器或直接启动：

```bash
uv run python -m PyFlow --web_server   # server: config UI -> status page + API
uv run python -m PyFlow --web_client   # client: connect UI -> main UI
```

或直接：

```bash
uv run python PyFlow/transfer_web/setup_server.py
uv run python PyFlow/transfer_web/setup_client.py
```

服务器网址的访问者将获得一个白色登录页面（客户端应连接到的地址） ，其中包含**登录**、**注册**和**更改密码**按钮；启动配置页面、状态页面及其背后的Web API需要一个会话。帐户位于SQLite数据库`PyFlow/transfer_web/.Flow_Web/flow_web.db`中：用户名、电子邮件、PBKDF2-SHA256密码记录和其他用户搜索的唯一8个字符的用户ID。第一次运行播种 管理员`admin`/`admin` （旧版`users.json`导入一次并重命名） ；当该对仍在使用中时，登录会弹出一个醒目的警告，要求在服务器**暴露给公共网络之前**更改用户名和密码，或者任何可以访问它的人都可以管理它。管理员管理用户（侧边栏中的`Users` ，其中列出了用户ID ） ，并且是唯一可以更改启动配置或加载/扩展的用户 扩展协议；常规用户获取包含消息/文件/文件夹发送的状态页面。`/api/server_info`保持公开，因为Web客户端在连接之前对其进行查询。

通过电子邮件验证注册、密码更改和基于代码的客户端登录：管理员在启动配置页面中填写发件邮箱（主机、端口、帐户、授权码、发件人和加密） ，在将设置存储在`PyFlow/transfer_web/.Flow_Web/email_config.json`中之前对照真实的SMTP服务器进行检查，然后才启动验证邮件服务。验证码有效期为5分钟，每分钟可能请求一个验证码， “发送”按钮将倒计时分钟。没有工作邮箱，任何人都无法注册或重置密码；种子管理员仍然可以登录并配置密码。

首次运行时，服务器启动器打开启动配置页面，显示每个`TCP_Server_Base`参数及其默认值；保存的配置以`PyFlow/transfer_web/.Flow_Web/setup_server.json` （与`setup.json`相同的形状）存在。TCP服务器启动后，服务器的Web后端将提供状态页面和面向客户端的API （`/api/server_info`返回TCP地址/端口）。客户端启动器询问服务器地址（ `http`/`https`域或裸IP ）并通过服务器的Web后端连接；然后询问 帐户登录（用户名/电子邮件，帐户密码和邮寄到帐户地址的验证码—这两个因素都是必需的） ，并将密码和会话令牌存储在`PyFlow/transfer_web/.Flow_Web/client_login.json`中，因此每次重新加载都会再次登录客户端，直到**注销**删除该文件。两个页面都显示连接实例和消息/文件/文件夹发送的侧边栏（客户端到客户端的发送通过服务器转发） ； Web客户端仅看到 其为联系人的账号。联系人使用**联系人**按钮添加（按用户ID、用户名或电子邮件搜索） ；另一方在其**请求**列表中应答请求，并且只有在两个帐户彼此接受后，联系人才会出现在侧边栏中。扩展协议由客户端页面以及服务器页面上的管理员加载。

服务器页面还提供`"ftp"`共享（仅限管理员）。它不是FTP协议：它浏览服务器主机的一个文件夹，并通过协议自己的`/file`和`/file_folder`传输将勾选的条目交给客户端。共享文件夹保留在启动配置中（`web.ftp_root`/`setup_server.json`） ，因此重新启动会继续为其提供服务。客户端的浏览对话框需要一个可选的**下载到**文件夹：条目位于客户端主机上， 当字段为空时，默认传输文件夹(`PyFlow/network_api/received_files`)。

### `setup.json`

预先编写的`setup.json`由启动器执行：

```json
{
  "servers": [
    { "host": "127.0.0.1", "port": 12345, "max_clients": 10,
      "is_extend_command": false, "is_input_command_in_console": true }
  ],
  "clients": []
}
```

### 程序化使用

```python
from PyFlow.network_api.connect_tcp import TCP_Server_Base, TCP_Client_Base

server = TCP_Server_Base(host="127.0.0.1", port=12345, is_extend_command=True)
client = TCP_Client_Base(host="127.0.0.1", port=12345, is_extend_command=True)
```

默认情况下，两端都启用加密通道（`is_enable_encrypto=True`） ：可解析时密钥来自`~/.ssh/id_rsa` ，否则将RSA-2048对生成到`PyFlow/network_api/.Flow/pvt_key/`中，并在每个连接（`PyFlow/network_api/.Flow/pub_key/pub_key.json`）上交换对等密钥并进行TOFU检查。有关`is_custom_keys`和完整握手，请参阅TCP API文档。

## 测试

```bash
uv run pytest                       # full Python suite
ctest --test-dir build       # C library tests
```

未构建`libcrypto_api`时，将自动跳过加密通道测试(`test/unit/network_api/test_crypto_rsa.py`, `test/integration/network_api/test_crypto_tcp.py`) ；其他所有测试都会自动运行。该套件采用Python 3.10-3.14 ，包括自由线程（无GIL ） 3.14版本。

## 文件材料

狮身人面像源位于`docs/source/` （英语源， `ja`/`ko`/`ru`/`zh_CN`/`zh_TW`目录位于`docs/source/locale/`下） ； `docs/`保存构建包装。使用以下内容生成HTML文档：

```bash
make -C docs html          # docs/Makefile; docs/make.bat html does the same on Windows
uv run python -m sphinx -b html docs/source build/sphinx_doc   # equivalent, explicit paths
```

使用`docs/reBuild.sh`重建翻译（提取gettext、机器翻译新字符串、编译`.mo`） ；它需要`pyproject.toml` (`sphinx`, `sphinx-intl`, `polib`, `deep-translator`)的文档/翻译依赖项。

## 许可证

[GPL-3.0](LICENSE)

