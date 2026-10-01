# PyFlow

[![CI](https://github.com/F18-Maverick/PyFlow/workflows/CI/badge.svg)](https://github.com/F18-Maverick/PyFlow/actions)  [![readthedocs](https://img.shields.io/readthedocs/pyflow-net)](https://pyflow-net.readthedocs.io/en/latest/)  [![coverage](https://img.shields.io/codecov/c/github/F18-Maverick/PyFlow)](https://app.codecov.io/gh/F18-Maverick/PyFlow)  [![Pypi](https://img.shields.io/pypi/v/pyflow-net.svg)](https://pypi.org/project/pyflow-net/)  [![supported_version](https://img.shields.io/pypi/pyversions/pyflow-net)](https://img.shields.io/pypi/pyversions/pyflow-net)  [![lisence](https://img.shields.io/github/license/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/blob/main/LICENSE)  [![commit](https://img.shields.io/github/last-commit/F18-Maverick/PyFlow)](https://github.com/F18-Maverick/PyFlow/commits/main/)

<!-- readme-translations:start -->
[English](../../README.md) | [日本語](README.ja.md) | [简体中文](README.zh_CN.md) | [繁體中文](README.zh_TW.md) | [한국어](README.ko.md) | [Русский](README.ru.md)
<!-- readme-translations:end -->

PyFlow - это высокоуровневый сетевой протокол, предлагающий API и веб-приложения, оба из которых передают сообщения, файлы и папки, а также расширяемые интерфейсы и другие функции.

## Характеристики

- **TCP server / client** — обмен сообщениями, пользовательские команды, передача файлов, API расширения команд и распределение портов по одному каналу управления (`PyFlow/network_api/connect_tcp.py`).
- **UDP-связь** — обмен сообщениями без установления соединения (`PyFlow/network_api/connect_udp.py`).
- **Зашифрованный канал TCP** — шифрование сообщения RSA-OAEP с реестром одноранговых ключей TOFU (trust-on-first-use), одноразовыми номерами сеансов и порядковыми номерами для повторного воспроизведения и автоматическим выключателем для повторного обмена штормами. См. [docs/Crypto](docs/source/Crypto/Crypto.rst) и разделы зашифрованных каналов в документах TCP API.
- **Криптографическая библиотека C/OpenSSL** — `libcrypto_api` предоставляет RSA-OAEP, ECDH (P-256/384/521), HKDF-SHA256 и AES-256-GCM со стабильным C API (префиксом`pf_*`), используемым из C, CMake или pkg-config.
- **Средство запуска с несколькими экземплярами** — `python -m PyFlow` (точка входа пакета, поддерживаемая `PyFlow/flow_setup.py`) запускает один или несколько экземпляров сервера/клиента из интерфейса командной строки, интерактивной подсказки или файла конфигурации `setup.json`.
- **Протоколы расширения** — `command_control_extension_tcp.py` (удаленное выполнение команд с коллекцией журналов) и `forward_extension_tcp.py` (переадресация файлов/папок в несколько мест назначения; переадресация простых сообщений `forward_send_msg` является родной для уровня TCP, а не частью этого расширения) подключаются к любому экземпляру через `setup_*_commands()`; `flow_setup.py` автоматически загружает их для каждого экземпляра, чьи `setup.json` наборы конфигурации `is_extend_command=True`, и запускает экземпляры в фоновом потоке, когда `is_input_command_in_console=False`. Файлы расширений также могут быть постоянно регистрируется в `flow_setup.py --add` / `--delete` (хранится в `PyFlow/added_extensions.json` и загружается при каждом запуске к `add_extension.py`).
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
- CMake 3.16 или новее (только для набора тестов C и для потребителей C)

В PyPI публикуются только исходные дистрибутивы, поэтому библиотека C компилируется на машине, которая устанавливает пакет. В Windows это означает наличие компилятора и файлов разработки OpenSSL до `pip install`:

- Visual Studio **Инструменты сборки** (рабочая нагрузка C++, "MSVC v143" и Windows SDK):
<https://visualstudio.microsoft.com/visual-cpp-build-tools/>. Компилятор просматривается автоматически; без него пип останавливается с собственным сообщением Microsoft "Microsoft Visual C++ 14.0 или выше требуется".
- OpenSSL из установщика Win64 по адресу
<https://slproweb.com/products/Win32OpenSSL.html> — инсталлятор по умолчанию (не«Light») включает файлы разработки, которые «Light» не включает. Он приземляется в `C:\Program Files\OpenSSL`, где сборка ищет его; `OPENSSL_ROOT_DIR` переопределяет поиск, когда он живет где-то еще.

## Сборка

### 1. Скомпилируйте библиотеку C

Установка пакета компилирует его: `setup.py` строит библиотеку `crypto_api` C против OpenSSL и устанавливает ее как `PyFlow/_crypto_api.*.so` (`.pyd` в Windows), что является тем, что `rsa_crypto.py` загружается во время выполнения.

```bash
pip install pyflow-net
```

Работая из кассы, та же сборка выполняется как часть `uv sync` / `pip install -e .`.

Вместо этого создание библиотеки C с помощью CMake необходимо только для набора тестов C и для потребителей C. Он производит `build/libcrypto_api.so` (или `.dylib` / `.dll`), который `rsa_crypto.py` также находит автоматически.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure   # optional: run the C test suite
```

### 2. Настройте среду Python

С [uv](https://docs.astral.sh/uv/) (проект использует `pyproject.toml` + `uv.lock`):

```bash
uv sync --group dev
```

Или с пипсом:

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install --group dev -e .
```

## Быстрый запуск

В приведенных ниже примерах используется `uv run` для пользователей uv; если вы установили вместо этого pip, отбросьте префикс `uv run` и используйте `python -m` напрямую.

### Интерактивная пусковая установка

```bash
uv run python -m PyFlow
```

Запрашивает конфигурацию сервера/клиента, записывает `setup.json`и запускает экземпляры.

### Пусковая установка командной строки

Запустите сервер, прослушивающий `127.0.0.1:12345`:

```bash
uv run python -m PyFlow --type 0 --setup_addr_port 127.0.0.1:12345
```

Запустите клиент, который подключается к этому серверу (и привязывает свой собственный локальный адрес/порт):

```bash
uv run python -m PyFlow --type 1 --setup_addr_port 127.0.0.1:23456 --connect_addr_port 127.0.0.1:12345
```

Зарегистрируйте (или отмените регистрацию) файл протокола расширения, чтобы каждый последующий запуск загружал его независимо от `is_extend_command`:

```bash
uv run python -m PyFlow --add path/to/my_extension.py
uv run python -m PyFlow --delete path/to/my_extension.py
```

Регистрация хранится в `PyFlow/added_extensions.json` до `add_extension.py` и перечитывается при каждом запуске.

### Веб-инструмент (пользовательский интерфейс браузера)

Веб-инструмент переносит протокол TCP в пользовательский интерфейс браузера для использования не в библиотеке (требуется Flask, установленный `uv sync`). Запустите его через лаунчер пакетов или напрямую:

```bash
uv run python -m PyFlow --web_server   # server: config UI -> status page + API
uv run python -m PyFlow --web_client   # client: connect UI -> main UI
```

Или напрямую:

```bash
uv run python PyFlow/transfer_web/setup_server.py
uv run python PyFlow/transfer_web/setup_client.py
```

Посетители веб-адреса сервера получают белую целевую страницу (адреса, к которым должны подключаться клиенты) с кнопками **Login**, **Register** и **Change password**; страница настройки запуска, страница состояния и веб-API, стоящие за ними, нуждаются в сеансе. Учетные записи находятся в базе данных SQLite `PyFlow/transfer_web/.Flow_Web/flow_web.db`: имя пользователя, адрес электронной почты, запись пароля PBKDF2-SHA256 и уникальный 8-символьный идентификатор пользователя, по которому ищут другие пользователи. Первый запуск семян администратор `admin` / `admin` (унаследованная `users.json` импортируется один раз и переименовывается); пока эта точная пара все еще используется, всплывает заметное предупреждение об изменении имени пользователя и пароля **до того, как** сервер будет открыт для общедоступной сети, или любой, кто может получить к нему доступ, может его администрировать. Администраторы управляют пользователями (`Users` на боковой панели, в которой перечислены идентификаторы пользователей) и являются единственными, кто может изменить конфигурацию запуска или загрузить/расширить протоколы расширений; обычные пользователи получают страницу состояния с отправкой сообщений/файлов/папок. `/api/server_info` остается общедоступным, поскольку веб-клиенты запрашивают его перед подключением.

Регистрация, смена пароля и вход в клиент на основе кода проверяются по электронной почте: администратор заполняет исходящий почтовый ящик (хост, порт, учетная запись, код авторизации, отправитель и шифрование) на странице настройки запуска, настройки проверяются по реальному SMTP-серверу перед их сохранением в `PyFlow/transfer_web/.Flow_Web/email_config.json`, и только после этого запускается почтовая служба проверки. Коды действительны в течение 5 минут, один код может быть запрошен в минуту, а кнопка отправки отсчитывает минуту. Без работающего почтового ящика никто не может зарегистрировать или сбросить пароль; посеянный администратор все еще может войти в систему и настроить его.

При первом запуске средство запуска сервера открывает страницу конфигурации запуска, показывающую каждый параметр `TCP_Server_Base` с его параметром по умолчанию; сохраненная конфигурация живет в `PyFlow/transfer_web/.Flow_Web/setup_server.json` (такая же форма, как `setup.json`). После запуска TCP-сервера веб-сервер сервера обслуживает страницу состояния и API, обращенный к клиенту (`/api/server_info` возвращает адрес/порт TCP). Средство запуска клиентов запрашивает адрес сервера (домен `http`/`https` или простой IP-адрес) и подключается через веб-сервер; затем он запрашивает учетная запись для входа в систему (имя пользователя/адрес электронной почты, пароль учетной записи и код подтверждения, отправленные по почте на адрес учетной записи — оба фактора обязательны) и сохраняет пароль и маркер сеанса в `PyFlow/transfer_web/.Flow_Web/client_login.json`, поэтому при каждой перезагрузке клиент снова входит в систему, пока **Выход** не удалит этот файл. На обеих страницах отображается боковая панель подключенных экземпляров и отправки сообщений/файлов/папок (отправки от клиента к клиенту пересылаются через сервер); веб-клиент видит только учетных записей, контактным лицом которых он является. Контакты добавляются с помощью кнопки **Контакты** (поиск по идентификатору пользователя, имени пользователя или электронной почте); другая сторона отвечает на запрос в своем списке **Запросы**, и только после того, как обе учетные записи приняли друг друга, контакт появляется на боковой панели. Протоколы расширения загружаются клиентской страницей и администраторами на странице сервера. Веб-инструмент сохраняет свое оставшееся состояние в том же каталоге `.Flow_Web/`: `setup_client.json` (последний конфигурация запуска клиента), `client_last_server.json` (последний адрес сервера, предлагаемый страницей подключения), `client_extensions_ui.json` / `server_extensions_ui.json` (состояние пользовательского интерфейса расширения) и промежуточный каталог `uploads/`.

Страница сервера также предлагает общий ресурс `"ftp"` (только для администратора). Это не протокол FTP: он просматривает одну папку хоста сервера и передает отмеченные записи клиентам по собственным передачам протокола `/file` и `/file_folder`. Общая папка хранится в конфигурации запуска (`web.ftp_root` из `setup_server.json`), поэтому перезапуск продолжает обслуживать ее. Диалоговое окно просмотра клиента принимает необязательную папку **Загрузить в**: записи попадают туда на хост клиента и в папка передачи по умолчанию (`PyFlow/network_api/received_files`), когда поле оставлено пустым.

### `setup.json`

Предварительно написанный `setup.json` почитается пусковой установкой:

```json
{
  "servers": [
    { "host": "127.0.0.1", "port": 12345, "max_clients": 10,
      "is_extend_command": false, "is_input_command_in_console": true }
  ],
  "clients": []
}
```

### Программное использование

```python
from PyFlow.network_api.connect_tcp import TCP_Server_Base, TCP_Client_Base

server = TCP_Server_Base(host="127.0.0.1", port=12345, is_extend_command=True)
client = TCP_Client_Base(host="127.0.0.1", port=12345, is_extend_command=True)
```

По умолчанию оба конца включают зашифрованный канал (`is_enable_encrypto=True`): ключи поступают из `~/.ssh/id_rsa` при синтаксическом анализе, в противном случае пара RSA-2048 генерируется в `PyFlow/network_api/.Flow/pvt_key/`, а одноранговые ключи обмениваются и проверяются TOFU на каждом соединении (`PyFlow/network_api/.Flow/pub_key/pub_key.json`). См. документы TCP API для `is_custom_keys` и полное рукопожатие.

## Испытания

```bash
uv run pytest                       # full Python suite
ctest --test-dir build       # C library tests
```

Тесты зашифрованного канала (`test/unit/network_api/test_crypto_rsa.py`, `test/integration/network_api/test_crypto_tcp.py`) пропускаются автоматически, когда `libcrypto_api` не был построен; все остальное выполняется независимо. CI запускает пакет на Python 3.10–3.14, как на Ubuntu, так и на Windows; он также передает сборку free-threaded (no-GIL) 3.14.

## Документация

Источники Sphinx находятся в `docs/source/` (английский источник с каталогами `ja`/`ko`/`ru`/`zh_CN`/`zh_TW` в `docs/source/locale/`); `docs/` содержит обертку сборки. Создавайте HTML-документы с помощью:

```bash
make -C docs html          # docs/Makefile; docs/make.bat html does the same on Windows
uv run python -m sphinx -b html docs/source build/sphinx_doc   # equivalent, explicit paths
```

Перестройте переводы (извлеките gettext, переведите на компьютер новые строки, скомпилируйте `.mo`) с помощью `docs/reBuild.sh`; ему нужны зависимости документации/перевода от `pyproject.toml` (`sphinx`, `sphinx-intl`, `polib`, `deep-translator`) и исполняемый файл `python3.14` на `PATH`, потому что скрипт вызывает `source/batch_translate_po.py` с помощью этого интерпретатора. Он записывает одно HTML-дерево на каждый язык под `docs/_build/html/<lang>`. Чтение документов выполняется `docs/readthedocs_build.sh` как предварительный шаг сборки, который компилирует только существующие каталоги и строит единое HTML-дерево для выходном каталоге RTD.

## Лицензия

[GPL-3.0](LICENSE)

