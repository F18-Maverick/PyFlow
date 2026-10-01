# PyFlow

[![CI](https://github.com/F18-Maverick/PyFlow/workflows/CI/badge.svg)](https://github.com/F18-Maverick/PyFlow/actions)  [![readthedocs](https://img.shields.io/readthedocs/pyflow-net)](https://pyflow-net.readthedocs.io/en/latest/)  [![coverage](https://img.shields.io/codecov/c/github/F18-Maverick/PyFlow)](https://app.codecov.io/gh/F18-Maverick/PyFlow)  [![Pypi](https://img.shields.io/pypi/v/pyflow-net.svg)](https://pypi.org/project/pyflow-net/)  [![supported_version](https://img.shields.io/pypi/pyversions/pyflow-net)](https://img.shields.io/pypi/pyversions/pyflow-net)  [![lisence](https://img.shields.io/github/license/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/blob/main/LICENSE)  [![commit](https://img.shields.io/github/last-commit/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/commits/main/)

<!-- readme-translations:start -->
[English](../../README.md) | [日本語](README.ja.md) | [简体中文](README.zh_CN.md) | [繁體中文](README.zh_TW.md) | [한국어](README.ko.md) | [Русский](README.ru.md)
<!-- readme-translations:end -->

PyFlow는 확장 가능한 인터페이스 및 기타 기능과 함께 메시지, 파일 및 폴더를 전송하는 API 및 웹 앱을 제공하는 높은 수준의 네트워크 프로토콜입니다.

## 기능

- **TCP 서버/클라이언트** — 단일 제어 채널 (`PyFlow/network_api/connect_tcp.py`) 을 통한 메시지 교환, 사용자 지정 명령, 파일 전송 및 포트 할당.
- **UDP 통신** — 연결 없는 메시징 (`PyFlow/network_api/connect_udp.py`).
- **암호화된 TCP 채널** — RSA-OAEP 메시지 암호화, TOFU (trust-on-first-use) 피어 키 레지스트리, 재생에 대한 세션 넌세스 및 시퀀스 번호, 재교환 폭풍에 대한 회로 차단기. [docs/Crypto](docs/source/Crypto/Crypto.rst) 및 TCP API 문서의 암호화된 채널 섹션을 참조하십시오.
- **C/OpenSSL 암호화 라이브러리** — `libcrypto_api` 는 C, CMake 또는 pkg-config에서 사용할 수 있는 안정적인 C API (`pf_*` 접두사) 를 갖춘 RSA-OAEP, ECDH (P-256/384/521), HKDF-SHA256 및 AES-256-GCM을 제공합니다.
- **다중 인스턴스 실행기** — `python -m PyFlow` (`PyFlow/flow_setup.py`이 (가) 지원하는 패키지 진입점) 는 CLI, 대화형 프롬프트 또는 `setup.json` 구성 파일에서 하나 이상의 서버/클라이언트 인스턴스를 시작합니다.
- **확장 프로토콜** — `command_control_extension_tcp.py` (로그 수집을 통한 원격 명령 실행) 및 `forward_extension_tcp.py` (파일/폴더를 여러 대상으로 업로드-후-푸시-포워딩; 일반 메시지 포워딩 `forward_send_msg` 은 이 확장의 일부가 아닌 TCP 계층에서 기본) `setup_*_commands()`를 통해 인스턴스에 연결합니다. `flow_setup.py` `setup.json` 구성이 `is_extend_command=True`을 (를) 설정하는 모든 인스턴스에 대해 자동으로 로드하고 `is_input_command_in_console=False`때 백그라운드 스레드에서 인스턴스를 시작합니다. 확장자 파일은 `flow_setup.py --add`/`--delete` 에 지속적으로 등록됨 (`PyFlow/added_extensions.json` 에 보관되고 `add_extension.py`까지 모든 출시 시 로드됨).
- **웹 도구** — `PyFlow/transfer_web/` 는 비 라이브러리 사용을 위해 브라우저 UI에서 TCP 프로토콜을 래핑합니다. `setup_server.py` 시작 구성 페이지 (`.Flow_Web/setup_server.json`에 저장됨, `setup.json`와 동일한 모양) 를 연 다음 상태 페이지와 클라이언트 대면 API를 제공합니다. `setup_client.py` 주소로 서버에 연결하고 두 페이지 모두 연결된 인스턴스, 메시지/파일/폴더 전송 (다른 클라이언트에 전달) 및 확장 로딩의 사이드바를 제공합니다. Flask의 지원을 받습니다.

## 건축

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

Python 레이어는 표준 라이브러리와 Flask (`transfer_web` 웹 도구에서만 사용됨) 에서 실행됩니다. C 라이브러리는 `ctypes`을 통해 런타임에 로드됩니다.

## 요구사항

- 파이썬 3.10 이상
- Pip 25.1 이상
- C 컴파일러 (Linux/macOS의 경우 gcc/clang, Windows의 경우 MSVC)
- OpenSSL 1.1.1 이상 (개발 헤더, 예: 데비안/우분투의 `libssl-dev`)
- CMake 3.16 이상 (C 테스트 제품군 및 C 소비자 전용)

소스 배포판만 PyPI에 게시되므로 패키지를 설치하는 시스템에서 C 라이브러리가 컴파일됩니다. Windows에서는 `pip install`이전에 컴파일러와 OpenSSL 개발 파일이 있음을 의미합니다.

- Visual Studio **빌드 도구** (C + + 워크로드, "MSVC v143" 및 Windows SDK):
<https://visualstudio.microsoft.com/visual-cpp-build-tools/>. 컴파일러는 자동으로 조회됩니다. 컴파일러가 없으면 Microsoft의 자체 "Microsoft Visual C + + 14.0 이상이 필요합니다" 라는 메시지와 함께 핍 스톱됩니다.
- Win64 설치 관리자의 OpenSSL
<https://slproweb.com/products/Win32OpenSSL.html> — 기본 (비 "Light") 설치 관리자는 "Light" 가 포함하지 않는 개발 파일을 포함합니다. 빌드가 찾는 `C:\Program Files\OpenSSL`에 착륙합니다. `OPENSSL_ROOT_DIR` 다른 곳에 살 때 검색을 재정의합니다.

## 빌드

### 1. C 라이브러리 컴파일

패키지를 설치하면 컴파일됩니다. `setup.py` OpenSSL에 대해 `crypto_api` C 라이브러리를 빌드하고 `PyFlow/_crypto_api.*.so` (Windows에서는`.pyd`) 로 설치합니다. `rsa_crypto.py` 이 (가) 런타임에 로드됩니다.

```bash
pip install pyflow-net
```

체크 아웃에서 작업하면 동일한 빌드가 `uv sync`/`pip install -e .`의 일부로 실행됩니다.

대신 CMake로 C 라이브러리를 빌드하는 것은 C 테스트 제품군과 C 소비자에게만 필요합니다. `build/libcrypto_api.so` (또는 `.dylib`/`.dll`) 를 생성하며, `rsa_crypto.py` 또한 자동으로 위치를 찾습니다.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure   # optional: run the C test suite
```

### 2. Python 환경 설정

[uv](https://docs.astral.sh/uv/) (프로젝트는 `pyproject.toml` + `uv.lock`사용):

```bash
uv sync --group dev
```

또는 핍으로:

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install --group dev -e .
```

## 빠른 시작

아래의 예는 자외선 사용자에 대해 `uv run` 을 (를) 사용합니다. 대신 pip로 설치한 경우 `uv run` 접두사를 삭제하고 `python -m` 을 (를) 직접 사용하십시오.

### 인터랙티브 런처

```bash
uv run python -m PyFlow
```

서버/클라이언트 구성을 묻고 `setup.json`을 (를) 쓰고 인스턴스를 시작합니다.

### 명령줄 실행기

`127.0.0.1:12345`에서 수신 대기 중인 서버 시작:

```bash
uv run python -m PyFlow --type 0 --setup_addr_port 127.0.0.1:12345
```

해당 서버에 연결하고 자체 로컬 주소/포트를 바인딩하는 클라이언트를 시작하십시오:

```bash
uv run python -m PyFlow --type 1 --setup_addr_port 127.0.0.1:23456 --connect_addr_port 127.0.0.1:12345
```

`is_extend_command`에 관계없이 나중에 실행할 때마다 로드되도록 확장 프로토콜 파일을 등록 (또는 등록 취소) 합니다.

```bash
uv run python -m PyFlow --add path/to/my_extension.py
uv run python -m PyFlow --delete path/to/my_extension.py
```

등록은 `add_extension.py` 에 의해 `PyFlow/added_extensions.json` 에 저장되고 모든 실행 시 다시 읽힙니다.

### 웹 도구 (브라우저 UI)

웹 도구는 라이브러리가 아닌 사용을 위해 브라우저 UI에서 TCP 프로토콜을 래핑합니다 (`uv sync`에서 설치한 Flask 필요). 패키지 런처를 통해 또는 직접 실행:

```bash
uv run python -m PyFlow --web_server   # server: config UI -> status page + API
uv run python -m PyFlow --web_client   # client: connect UI -> main UI
```

또는 직접:

```bash
uv run python PyFlow/transfer_web/setup_server.py
uv run python PyFlow/transfer_web/setup_client.py
```

Visitors of the server's web address get a white landing page (the addresses clients should connect to) with **Login**, **Register** and **Change password** buttons; the startup-configuration page, the status page and the web APIs behind them need a session. Accounts live in the SQLite database `PyFlow/transfer_web/.Flow_Web/flow_web.db`: username, email, a PBKDF2-SHA256 password record and a unique 8-character user ID that other users search by. The first run seeds the administrator `admin` / `admin` (a legacy `users.json` is imported once and renamed); while that exact pair is still in use, a login pops up a prominent warning to change the username and password **before** the server is exposed to a public network, or anyone who can reach it can administer it. Administrators manage users (`Users` in the sidebar, which lists the user IDs) and are the only ones who can change the startup configuration or load/extend extension protocols; regular users get the status page with message/file/folder sending. `/api/server_info` stays public, as web clients query it before they connect.

등록, 암호 변경 및 코드 기반 클라이언트 로그인은 이메일로 확인됩니다. 관리자가 시작 구성 페이지의 발신 사서함 (호스트, 포트, 계정, 인증 코드, 발신자 및 암호화) 을 채우고 설정이 `PyFlow/transfer_web/.Flow_Web/email_config.json`에 저장되기 전에 실제 SMTP 서버에 대해 검사된 다음 확인 메일 서비스가 시작됩니다. 코드는 5분 동안 유효하며, 분당 하나의 코드를 요청할 수 있으며, 보내기 버튼은 분을 계산합니다. 작동하는 사서함이 없으면 아무도 비밀번호를 등록하거나 재설정할 수 없습니다. 시드 관리자는 여전히 로그인하여 비밀번호를 구성할 수 있습니다.

첫 번째 실행 시 서버 런처는 기본값으로 모든 `TCP_Server_Base` 매개 변수를 보여주는 시작 구성 페이지를 엽니다. 저장된 구성은 `PyFlow/transfer_web/.Flow_Web/setup_server.json` (`setup.json`와 동일한 모양) 에 있습니다. TCP 서버가 가동되면 서버의 웹 백엔드는 상태 페이지와 클라이언트 대면 API를 제공합니다 (`/api/server_info` 는 TCP 주소/포트를 반환합니다). 클라이언트 런처는 서버 주소 (`http`/`https` 도메인 또는 베어 IP) 를 요청하고 서버의 웹 백엔드를 통해 연결합니다. 그런 다음 로그인할 계정 (사용자 이름/이메일, 계정 암호 및 계정 주소로 발송된 확인 코드 — 두 가지 요인이 모두 필요함) 과 암호 및 세션 토큰을 `PyFlow/transfer_web/.Flow_Web/client_login.json`에 저장하므로 **로그아웃** 이 해당 파일을 삭제할 때까지 다시 클라이언트를 기록합니다. 두 페이지 모두 연결된 인스턴스와 메시지/파일/폴더 전송의 사이드바를 보여줍니다 (클라이언트 간 전송은 서버를 통해 전달됨). 웹 클라이언트는 계정은 연락처입니다. 연락처는 **연락처** 버튼 (사용자 ID, 사용자 이름 또는 이메일로 검색) 과 함께 추가됩니다. 상대방은 **요청** 목록에서 요청에 응답하며, 두 계정이 서로를 수락한 후에만 연락처가 사이드바에 나타납니다. 확장 프로토콜은 클라이언트 페이지와 관리자가 서버 페이지에 로드합니다. 웹 도구는 동일한 `.Flow_Web/` 디렉토리에 남은 상태를 유지합니다: `setup_client.json` (마지막 클라이언트 시작 구성), `client_last_server.json` (연결 페이지에서 제공하는 마지막 서버 주소), `client_extensions_ui.json`/`server_extensions_ui.json` (확장 UI 상태) 및 `uploads/` 스테이징 디렉터리.

서버 페이지는 `"ftp"` 공유 (관리자 전용) 도 제공합니다. FTP 프로토콜이 아닙니다. 서버 호스트의 한 폴더를 탐색하고 선택한 항목을 프로토콜의 자체 `/file` 및 `/file_folder` 전송을 통해 클라이언트에 전달합니다. 공유 폴더는 시작 구성 (`setup_server.json`의`web.ftp_root`) 에 보관되므로 다시 시작하면 계속 작동합니다. 클라이언트의 찾아보기 대화 상자는 선택적 **다운로드 폴더를** 폴더로 가져옵니다. 항목은 클라이언트 호스트에 있고 필드가 비어 있는 경우 기본 전송 폴더 (`PyFlow/network_api/received_files`).

### `setup.json`

사전 작성된 `setup.json` 은 런처에 의해 존중됩니다:

```json
{
  "servers": [
    { "host": "127.0.0.1", "port": 12345, "max_clients": 10,
      "is_extend_command": false, "is_input_command_in_console": true }
  ],
  "clients": []
}
```

### 프로그래밍 방식 사용

```python
from PyFlow.network_api.connect_tcp import TCP_Server_Base, TCP_Client_Base

server = TCP_Server_Base(host="127.0.0.1", port=12345, is_extend_command=True)
client = TCP_Client_Base(host="127.0.0.1", port=12345, is_extend_command=True)
```

기본적으로 양쪽 끝은 암호화된 채널 (`is_enable_encrypto=True`) 을 활성화합니다: 키는 구문 분석이 가능한 경우 `~/.ssh/id_rsa` 에서 가져옵니다. 그렇지 않으면 RSA-2048 쌍이 `PyFlow/network_api/.Flow/pvt_key/`에 생성되고 모든 연결에서 피어 키가 교환되고 TOFU 검사됩니다 (`PyFlow/network_api/.Flow/pub_key/pub_key.json`). `is_custom_keys` 에 대한 TCP API 문서와 전체 핸드셰이크를 참조하십시오.

## 테스트

```bash
uv run pytest                       # full Python suite
ctest --test-dir build       # C library tests
```

암호화된 채널 테스트 (`test/unit/network_api/test_crypto_rsa.py`, `test/integration/network_api/test_crypto_tcp.py`) 는 `libcrypto_api` 이 (가) 빌드되지 않은 경우 자동으로 건너뜁니다. 다른 모든 테스트는 상관없이 실행됩니다. CI는 Python 3.10-3.14, Ubuntu 및 Windows 모두에서 제품군을 실행합니다. 또한 자유 스레드 (GIL 없음) 3.14 빌드를 전달합니다.

## 문서화

스핑크스 소스는 `docs/source/` 에 있습니다 (`ja`/`ko`/`ru`/`zh_CN`/`zh_TW` 카탈로그가 `docs/source/locale/`아래에 있는 영어 소스). `docs/` 은 (는) 빌드 래퍼를 보관합니다. 다음을 사용하여 HTML 문서 작성:

```bash
make -C docs html          # docs/Makefile; docs/make.bat html does the same on Windows
uv run python -m sphinx -b html docs/source build/sphinx_doc   # equivalent, explicit paths
```

`docs/reBuild.sh`을 (를) 사용하여 번역 (gettext 추출, 새 문자열 기계 번역, 컴파일 `.mo`) 을 다시 빌드합니다. 스크립트가 해당 인터프리터로 `source/batch_translate_po.py` 를 호출하기 때문에 `pyproject.toml` (`sphinx`, `sphinx-intl`, `polib`, `deep-translator`) 및 `PATH`에서 실행 가능한 `python3.14` 의 문서/번역 종속성이 필요합니다. `docs/_build/html/<lang>`에서 언어당 하나의 HTML 트리를 작성합니다. 문서도구는 기존 카탈로그만 컴파일하고 단일 HTML 트리를 빌드하는 사전 빌드 단계로 `docs/readthedocs_build.sh` 실행됩니다. rTD 출력 디렉터리.

## 면허증

[GPL-3.0](LICENSE)

