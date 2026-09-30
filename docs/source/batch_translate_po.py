#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch-translate the .po files of this Sphinx project into several languages.

Walks ``locale/<lang>/LC_MESSAGES/**/*.po`` recursively and translates every
entry whose ``msgstr`` is still empty. A failed entry keeps its empty ``msgstr``,
so running the script again continues where it left off.

Translations are written back as soon as ``SAVE_INTERVAL`` of them accumulate, and once more
when a file ends or the run stops (abort, Ctrl-C, crash). A file with 279 pending entries
where the last 79 keep failing therefore keeps the first 200 already persisted by the time
the run gives up, instead of discarding the whole file's work and burning the quota again.

Engines (``--engine``)
    ``mymemory``  MyMemory REST API — **default, no key at all**. Reachable without a proxy
                (verified from a mainland connection), so it needs neither an account nor a
                proxy. Anonymous quota is 5,000 characters per day per IP, which covers this
                project's ~4.8k characters of catalogues per pass but not the README: the
                README costs another ~8.3k characters per language. Set ``MYMEMORY_EMAIL``
                (any address, no registration) to raise the daily quota to 50,000, which fits
                one full README pass. Quality is translation-memory grade.
    ``baidu``   Baidu Translate open API — needs ``BAIDU_APPID`` plus ``BAIDU_KEY``
                (deep-translator's ``BAIDU_APPKEY`` is accepted too) and real-name
                registration. Domestic, so it needs no proxy. Free standard tier is 50k
                characters per day. Better quality than MyMemory once a key exists.
                deep-translator's own ``BaiduTranslator`` has the same requirement.
    ``microsoft``  Microsoft Translator v3. ``MICROSOFT_API_KEY`` plus
                ``MICROSOFT_API_REGION`` (aliases accepted: ``MICROSOFT_TRANSLATOR_*``,
                ``AZURE_TRANSLATOR_*``); the key name matches deep-translator's own
                ``MICROSOFT_API_KEY``. Needs the proxy to leave the country, but a keyed API
                is never treated as scraping. Free F0 tier is 2M characters per month.
                Implemented with a direct call rather than deep-translator's
                ``MicrosoftTranslator``: that class rejects every Chinese target code
                (``zh-Hans``/``zh-Hant``/``zh-cn``/``zh-tw``/names all raise
                ``LanguageNotSupportedException``), and Chinese is half of our targets.
    ``google``  deep-translator's Google web endpoint. It is **not** a rate-limit problem
                that a different proxy node can fix: Google answers automated clients with
                its "Sorry..." anti-abuse page (HTTP 429) regardless of User-Agent or exit
                IP, while the same IP loads google.com and translate.google.com normally.

The generated API pages (``api/*.po``) are skipped by default: they are docstrings pulled
in by autodoc, and keeping them English keeps the API text single-sourced (see
``DOCSTRING_GUIDE.md`` section 11) — they also account for ~97% of the characters.
Use ``--include-generated`` to translate them anyway.

The repository ``README.md`` is translated **before** the catalogues, into the same languages,
and written to ``docs/readme_translations/README.<lang>.md``. A language bar linking the
translations is inserted into ``README.md``, ``test/README.md`` and every translation, right
below the badges (below the title where a README has no badges). Markdown is translated block
by block: fenced code blocks, link-only lines (badges), table rows, the ``# PyFlow`` title and
the language bar itself are copied verbatim, while inline code, links, URLs and bold markers
inside a translatable block are masked with ``{tN}`` placeholders and checked after the
response — a block whose placeholders come back changed keeps its English text instead of
shipping broken markdown. Blocks are cached in
``docs/readme_translations/readme_cache.json``, keyed by the SHA-256 of their English source
(like the ``.po`` files, the cache is committed): a rerun requests only the blocks whose
English changed, and a quota-exhausted run cannot overwrite a finished translation with
English.

Rate limits and failures are handled instead of aborting the run:
requests are paced by ``REQUEST_DELAY``; a rate-limited entry is retried after
``RATE_LIMIT_BACKOFF`` seconds; after ``MAX_CONSECUTIVE_FAILURES`` failures in a row the run
stops with an explanation.

``--ignore-failures`` makes the run best-effort for build pipelines (``docs/reBuild.sh``,
Read the Docs): every failure — a missing ``locale/`` directory, an unconfigured engine, a
stopped run, or an unexpected exception — is reported and the script still exits ``0``, so
the HTML build that follows keeps running on the catalogues that are already translated.

Usage:
    python3 batch_translate_po.py                 # every language, mymemory (no key, no proxy)
    python3 batch_translate_po.py --engine baidu  # better quality once BAIDU_APPID/KEY exist
    python3 batch_translate_po.py --lang ja       # a single language
    python3 batch_translate_po.py --limit 5       # at most 5 entries per file and README language
    python3 batch_translate_po.py --proxy http://127.0.0.1:7897   # route via an explicit proxy
    python3 batch_translate_po.py --include-generated --ignore-failures   # full pipeline run
"""

import argparse
import json
import os
import re
import sys
import time
from hashlib import md5, sha256
from importlib.util import find_spec
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

import requests

# ========== 用户配置 ==========
LANGUAGES = ["ja", "zh_CN", "zh_TW", "ko", "ru"]
SOURCE_LANG = "en"
ENGINE = "mymemory"  # mymemory (默认，免 key) | baidu | microsoft | google
TRANSLATE_GENERATED_PAGES = False  # api/*.po 是 docstring 生成的，默认保持英文（单源）
REQUEST_DELAY = 0.5  # 两次请求之间的最小间隔（秒）
REQUEST_TIMEOUT = 30  # 单条请求超时（秒）
MAX_RETRIES = 3  # 单条翻译失败重试次数
RATE_LIMIT_BACKOFF = (10, 30)  # 被限流后的退避秒数，按尝试次数递增
RATE_LIMIT_MARKERS = ("too many requests", "server error", "429", "quota")
MAX_CONSECUTIVE_FAILURES = 3  # 连续失败达到该数量即停止本轮
SAVE_INTERVAL = 20  # 每译好多少条就把该文件落盘一次（>0；中断时最多丢这么多条）
ENABLE_TRANSLATION = True
TRANSLATE_README = True  # 翻译仓库根 README.md（优先于 .po），并把语言链接栏写回每个 README
README_TRANSLATION_DIR = "readme_translations"  # README 译本目录，相对 docs/

GOOGLE_LANG = {
    "zh_CN": "zh-CN",
    "zh_TW": "zh-TW",
}
BAIDU_LANG = {
    "ja": "jp",
    "ko": "kor",
    "ru": "ru",
    "zh_CN": "zh",
    "zh_TW": "cht",
}
MICROSOFT_LANG = {
    "ja": "ja",
    "ko": "ko",
    "ru": "ru",
    "zh_CN": "zh-Hans",
    "zh_TW": "zh-Hant",
}
MYMEMORY_LANG = {
    "ja": "ja",
    "ko": "ko",
    "ru": "ru",
    "zh_CN": "zh-CN",
    "zh_TW": "zh-TW",
}
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
MYMEMORY_QUERY_LIMIT = 450  # bytes per query (the API caps a query at 500)
MYMEMORY_OK_STATUS = 200  # the API reports failures through responseStatus
BAIDU_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"
MICROSOFT_URL = "https://api.cognitive.microsofttranslator.com/translate"
# ================================

try:
    import polib
    from deep_translator import GoogleTranslator
except ImportError:
    print("❌ 请安装依赖: pip install polib deep-translator")
    sys.exit(1)


class ConfigError(RuntimeError):
    """Raised when the selected engine is missing its credentials."""


class RateLimitAbort(RuntimeError):
    """Raised when consecutive failures show that the endpoint refuses the client."""


class Throttle:
    """Pace the translate requests across files and languages."""

    def __init__(self, delay: float = REQUEST_DELAY):
        """Initialize the pacer.

        Args:
            delay (float): Minimum seconds between two requests; 0 disables waiting.
        """
        self.delay = delay
        self._last = 0.0

    def wait(self):
        """Sleep until the next request is allowed to go out."""
        gap = self.delay - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()


class GoogleEngine:
    """Translate through deep-translator's Google web endpoint."""

    def __init__(self, target_lang: str, proxy: str | None):
        """Initialize the engine.

        Args:
            target_lang (str): Target locale such as "ja" or "zh_CN".
            proxy (str | None): Explicit proxy URL, or None for the environment setting.
        """
        self._translator = GoogleTranslator(
            source=SOURCE_LANG,
            target=GOOGLE_LANG.get(target_lang, target_lang),
            timeout=REQUEST_TIMEOUT,
            proxies=proxy_map(proxy),
        )

    def translate(self, text: str) -> str:
        """Translate one string.

        Args:
            text (str): Text to translate.

        Returns:
            str: The translation.
        """
        return self._translator.translate(text)


class BaiduEngine:
    """Translate through the Baidu open API (domestic, works without a proxy)."""

    def __init__(self, target_lang: str, proxy: str | None):
        """Initialize the engine.

        Args:
            target_lang (str): Target locale such as "ja" or "zh_CN".
            proxy (str | None): Explicit proxy URL, or None for the environment setting.

        Raises:
            ConfigError: When the appid or the key is not set (``BAIDU_APPID`` plus
                ``BAIDU_KEY``, or deep-translator's ``BAIDU_APPKEY``).
        """
        self.appid = os.environ.get("BAIDU_APPID", "")
        self.key = os.environ.get("BAIDU_KEY") or os.environ.get("BAIDU_APPKEY", "")
        if not (self.appid and self.key):
            raise ConfigError(
                "缺少 BAIDU_APPID / BAIDU_KEY（或 BAIDU_APPKEY）："
                "fanyi-api.baidu.com 申请，标准版免费 5 万字符/天"
            )
        if target_lang not in BAIDU_LANG:
            raise ConfigError(f"百度引擎不支持目标语言: {target_lang}")
        self.target = BAIDU_LANG[target_lang]
        self.proxies = proxy_map(proxy)

    def translate(self, text: str) -> str:
        """Translate one string.

        Args:
            text (str): Text to translate.

        Returns:
            str: The translation; translated text may come back in several parts.

        Raises:
            RuntimeError: When the API answers with an error payload.
        """
        salt = str(int(time.time() * 1000) % 100000)
        sign = md5((self.appid + text + salt + self.key).encode("utf-8")).hexdigest()
        response = requests.post(
            BAIDU_URL,
            data={
                "q": text,
                "from": SOURCE_LANG,
                "to": self.target,
                "appid": self.appid,
                "salt": salt,
                "sign": sign,
            },
            proxies=self.proxies,
            timeout=REQUEST_TIMEOUT,
        )
        payload = response.json()
        if "trans_result" not in payload:
            raise RuntimeError(f"baidu api error: {payload}")
        return "".join(part["dst"] for part in payload["trans_result"])


def env_value(*names: str) -> str:
    """Return the first environment variable among ``names`` that is set and non-empty.

    Args:
        *names (str): Candidate variable names, highest priority first.

    Returns:
        str: The value found, or "" when none of them is set.
    """
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


class MicrosoftEngine:
    """Translate through the Microsoft Translator v3 API (needs a key and the region)."""

    def __init__(self, target_lang: str, proxy: str | None):
        """Initialize the engine.

        Args:
            target_lang (str): Target locale such as "ja" or "zh_CN".
            proxy (str | None): Explicit proxy URL, or None for the environment setting.

        Raises:
            ConfigError: When the key, the region or the target locale is missing.
        """
        self.key = env_value(
            "MICROSOFT_API_KEY", "MICROSOFT_TRANSLATOR_KEY", "AZURE_TRANSLATOR_KEY"
        )
        self.region = env_value(
            "MICROSOFT_API_REGION", "MICROSOFT_TRANSLATOR_REGION", "AZURE_TRANSLATOR_REGION"
        )
        if not (self.key and self.region):
            raise ConfigError(
                "缺少 MICROSOFT_API_KEY / MICROSOFT_API_REGION"
                "（Azure 门户建 Translator 资源即可，免费 F0 层 200 万字符/月）"
            )
        if target_lang not in MICROSOFT_LANG:
            raise ConfigError(f"microsoft 引擎不支持目标语言: {target_lang}")
        self.target = MICROSOFT_LANG[target_lang]
        self.proxies = proxy_map(proxy)

    def translate(self, text: str) -> str:
        """Translate one string.

        Args:
            text (str): Text to translate.

        Returns:
            str: The translation.

        Raises:
            RuntimeError: When the API answers with an error payload.
        """
        response = requests.post(
            MICROSOFT_URL,
            params={"api-version": "3.0", "from": SOURCE_LANG, "to": self.target},
            headers={
                "Ocp-Apim-Subscription-Key": self.key,
                "Ocp-Apim-Subscription-Region": self.region,
                "Content-Type": "application/json",
            },
            json=[{"Text": text}],
            proxies=self.proxies,
            timeout=REQUEST_TIMEOUT,
        )
        payload = response.json()
        if not isinstance(payload, list) or "translations" not in payload[0]:
            raise RuntimeError(f"azure api error: {payload}")
        return payload[0]["translations"][0]["text"]


def split_query(text: str, limit: int = MYMEMORY_QUERY_LIMIT):
    """Split a long string into chunks the translation API accepts per query.

    Args:
        text (str): Text to split, usually a ``msgid``.
        limit (int): Maximum bytes per chunk.

    Returns:
        list[str]: One chunk when the text fits, otherwise word-aligned chunks.
    """
    if len(text.encode("utf-8")) <= limit:
        return [text]
    chunks = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if current and len(candidate.encode("utf-8")) > limit:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class MyMemoryEngine:
    """Translate through the keyless MyMemory API (no proxy, no credentials)."""

    def __init__(self, target_lang: str, proxy: str | None):
        """Initialize the engine.

        Args:
            target_lang (str): Target locale such as "ja" or "zh_CN".
            proxy (str | None): Explicit proxy URL, or None for the environment setting.

        Raises:
            ConfigError: When the target locale is not supported.
        """
        if target_lang not in MYMEMORY_LANG:
            raise ConfigError(f"MyMemory 不支持目标语言: {target_lang}")
        self.target = MYMEMORY_LANG[target_lang]
        self.proxies = proxy_map(proxy)
        self.email = os.environ.get("MYMEMORY_EMAIL", "")  # raises the daily quota to 50k

    def translate(self, text: str) -> str:
        """Translate one string, splitting it when it exceeds the per-query limit.

        Args:
            text (str): Text to translate.

        Returns:
            str: The translation.

        Raises:
            RuntimeError: When the API reports a failure (quota exhausted included).
        """
        return " ".join(self._translate_chunk(chunk) for chunk in split_query(text))

    def _translate_chunk(self, chunk: str) -> str:
        """Translate a single chunk that fits the per-query limit.

        Args:
            chunk (str): Text of at most ``MYMEMORY_QUERY_LIMIT`` bytes.

        Returns:
            str: The translation.

        Raises:
            RuntimeError: When the API reports a failure.
        """
        params = {"q": chunk, "langpair": f"{SOURCE_LANG}|{self.target}"}
        if self.email:
            params["de"] = self.email
        response = requests.get(
            MYMEMORY_URL, params=params, proxies=self.proxies, timeout=REQUEST_TIMEOUT
        )
        payload = response.json()
        translated = (payload.get("responseData") or {}).get("translatedText") or ""
        failed = payload.get("responseStatus") != MYMEMORY_OK_STATUS
        if failed or "MYMEMORY WARNING" in translated.upper():
            raise RuntimeError(f"mymemory api error: {payload.get('responseDetails') or payload}")
        return translated


ENGINES = {
    "mymemory": MyMemoryEngine,
    "google": GoogleEngine,
    "baidu": BaiduEngine,
    "microsoft": MicrosoftEngine,
}


class Session:
    """State shared by one run: engine choice, pacing, cache and entry limit."""

    def __init__(
        self,
        delay: float = REQUEST_DELAY,
        limit: int = 0,
        proxy: str | None = None,
        engine: str = ENGINE,
    ):
        """Initialize the run state.

        Args:
            delay (float): Minimum seconds between two requests.
            limit (int): Maximum entries translated per file; 0 means no limit.
            proxy (str | None): Explicit proxy URL overriding ``HTTP(S)_PROXY``; None keeps
                the environment setting.
            engine (str): One of ``ENGINES``.
        """
        self.throttle = Throttle(delay)
        self.limit = limit
        self.proxy = proxy
        self.engine = engine
        self.cache = {}  # (target_code, msgid) -> translation, shared by every file


def is_rate_limited(error: Exception) -> bool:
    """Report whether an exception looks like throttling rather than a bad request.

    Args:
        error (Exception): Exception raised by the translation request.

    Returns:
        bool: True when the message matches a known rate-limit marker.
    """
    text = str(error).lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


def proxy_map(proxy: str | None):
    """Build the ``requests`` proxies mapping for an explicit proxy URL.

    Args:
        proxy (str | None): Proxy URL, for example "http://127.0.0.1:7897".

    Returns:
        dict | None: ``{"http": ..., "https": ...}``, or None to let requests use the
            ``HTTP(S)_PROXY`` environment.
    """
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def translate_text(translator, text: str, session: "Session"):
    """Translate one string, retrying according to the kind of failure.

    Args:
        translator (object): Engine instance exposing ``translate(text)``.
        text (str): Text to translate (a ``msgid``).
        session (Session): Run state holding the request pacer.

    Returns:
        str | None: The translation, or None when the entry failed and must stay
            untranslated for a later run.
    """
    for attempt in range(MAX_RETRIES):
        try:
            session.throttle.wait()
            return translator.translate(text)
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                print(f"     ❌ 翻译失败: {text[:40]}... → {e}")
                return None
            if is_rate_limited(e):
                wait = RATE_LIMIT_BACKOFF[min(attempt, len(RATE_LIMIT_BACKOFF) - 1)]
                print(f"     ⏸️ 被限流，等待 {wait}s 后重试 ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
            else:
                print(f"     ⚠️ 重试 {attempt + 1}/{MAX_RETRIES}: {text[:30]}...")
                time.sleep(2)
    return None


def find_locale_dir(start_path: Path):
    """Look for the ``locale/`` directory at or above ``start_path``.

    Args:
        start_path (Path): Directory to start from, normally the script directory.

    Returns:
        Path | None: The ``locale/`` directory, or None when none is found within
            ten levels.
    """
    current = start_path.resolve()
    for _ in range(10):
        candidate = current / "locale"
        if candidate.is_dir():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    return None


def is_generated_page(po_path: Path, lang_dir: Path) -> bool:
    """Report whether a .po file belongs to a generated API page.

    Args:
        po_path (Path): The catalogue to test.
        lang_dir (Path): ``LC_MESSAGES`` directory it lives under.

    Returns:
        bool: True for ``LC_MESSAGES/api/*.po``, whose entries come from docstrings.
    """
    parts = po_path.relative_to(lang_dir).parts
    return bool(parts) and parts[0] == "api"


def write_po(po, po_path: Path, backup: Path | None):
    """Persist one catalogue atomically, keeping the pre-run file as ``.bak``.

    Args:
        po (polib.POFile): Catalogue holding the translations done so far.
        po_path (Path): Target .po file.
        backup (Path | None): Backup returned by an earlier call, if any.

    Returns:
        Path: The backup path; only the first call of a run creates it, so the later
            flushes do not overwrite the file as it was before the run.
    """
    if backup is None:
        backup = po_path.with_suffix(po_path.suffix + ".bak")
        po_path.replace(backup)
    temp = po_path.with_suffix(po_path.suffix + ".tmp")
    po.save(str(temp))
    temp.replace(po_path)  # 原子替换：落盘途中被打断也不会留下写了一半的 .po
    return backup


# ================= README 翻译 =================
# 译文目录：docs/readme_translations/README.<lang>.md；翻译记忆：同目录 readme_cache.json
README_REPO_ROOT = Path(__file__).resolve().parents[2]
README_DIR = README_REPO_ROOT / "docs" / README_TRANSLATION_DIR
README_SOURCE_NAME = "README.md"
README_BAR_FILES = ("README.md", "test/README.md")  # 每个 README 都带上语言链接栏
README_CACHE_NAME = "readme_cache.json"
README_LANGUAGES = ["en", *LANGUAGES]  # 链接栏顺序：英文源 + 文档各语言
README_LANGUAGE_NAMES = {
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "ru": "Русский",
    "zh_CN": "简体中文",
    "zh_TW": "繁體中文",
}
README_BAR_START = "<!-- readme-translations:start -->"
README_BAR_END = "<!-- readme-translations:end -->"
README_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
README_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")
README_LIST_RE = re.compile(r"^([ \t]*(?:[-*+]|\d+[.)])[ \t]+)(.*)$")
README_QUOTE_RE = re.compile(r"^([ \t]*>[ \t]*)(.*)$")
README_HR_RE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$")
# 链接/图片，允许一层嵌套（badge 是 [![alt](img)](link)）
README_LINK_PATTERN = r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\([^)]*\)"
README_LINK_RE = re.compile(README_LINK_PATTERN)
README_PROTECT_RE = re.compile(
    r"`[^`]*`"  # 行内代码
    r"|"
    + README_LINK_PATTERN  # 链接 / 图片
    + r"|<[A-Za-z/][^>\s]*>"  # 自动链接 / HTML 标签
    r"|https?://[^\s)>]+"  # 裸 URL
    r"|\*\*"  # 粗体标记
)
README_TOKEN_RE = re.compile(r"\{\s*t\s*(\d+)\s*\}")  # MyMemory 会把 {t0} 写成 {t 0}
# ==============================================


class ReadmeBlock(NamedTuple):
    """One unit of the README: a verbatim block or a run of translatable text.

    Attributes:
        translatable (bool): Whether ``text`` is meant to be translated.
        prefix (str): Leading markdown marker kept out of the translation, such as the
            ``"## "`` of a heading or the ``"- "`` of a list item; empty when verbatim.
        text (str): Block content, without ``prefix``.
    """

    translatable: bool
    prefix: str
    text: str


class ReadmeSpan(NamedTuple):
    """One run of a README block that must survive the translation untouched.

    Attributes:
        text (str): The original string, restored verbatim into the translation.
        glued_left (bool): Whether the run sits flush against the text before it, so a space
            the engine inserted in front of the token has to be dropped again.
        glued_right (bool): Whether the run sits flush against the text after it.
    """

    text: str
    glued_left: bool
    glued_right: bool


class ReadmeResult(NamedTuple):
    """Outcome of translating the README blocks of one language.

    Attributes:
        failed (int): Blocks left in English because the engine refused them.
        translated (int): Blocks translated by this call.
        reused (int): Blocks taken from the translation memory.
        aborted (bool): Whether the endpoint kept refusing and the run should stop.
    """

    failed: int
    translated: int
    reused: int
    aborted: bool


def readme_translation_path(lang: str) -> Path:
    """Return the file holding one language's README translation.

    Args:
        lang (str): Language code such as "ja" or "zh_CN".

    Returns:
        Path: ``docs/readme_translations/README.<lang>.md`` inside the repository.
    """
    return README_DIR / f"README.{lang}.md"


def readme_block_key(text: str) -> str:
    """Return the translation-memory key of one README block.

    Args:
        text (str): English source text of the block.

    Returns:
        str: Truncated SHA-256 digest of the UTF-8 text.
    """
    return sha256(text.encode("utf-8")).hexdigest()[:16]


def is_readme_verbatim_line(line: str) -> bool:
    """Report whether a README line must be copied without translation.

    Args:
        line (str): One markdown line.

    Returns:
        bool: True for blank lines, horizontal rules, table rows, HTML comments and
            link-only lines such as the badge row.
    """
    stripped = line.strip()
    if not stripped or README_HR_RE.match(line):
        return True
    if stripped.startswith("|") or stripped.startswith("<!--"):
        return True
    return bool(README_LINK_RE.search(line)) and not README_LINK_RE.sub("", line).strip()


def is_readme_paragraph_line(line: str) -> bool:
    """Report whether a README line continues the paragraph being gathered.

    Args:
        line (str): One markdown line.

    Returns:
        bool: True for a plain text line that starts no block of its own.
    """
    if is_readme_verbatim_line(line):
        return False
    return not (
        README_FENCE_RE.match(line)
        or README_HEADING_RE.match(line)
        or README_LIST_RE.match(line)
        or README_QUOTE_RE.match(line)
    )


def _take_readme_fence(lines: list[str], index: int, blocks: list) -> int:
    """Append the fenced code block at ``index`` and return the next line index.

    Args:
        lines (list[str]): README lines, split on newlines.
        index (int): Index of the opening fence.
        blocks (list): Block list to append to.

    Returns:
        int: Index of the first line after the closing fence.
    """
    fence = README_FENCE_RE.match(lines[index]).group(1)
    end = index + 1
    while end < len(lines) and not lines[end].strip().startswith(fence):
        end += 1
    if end < len(lines):
        end += 1  # 连同收尾的 ``` 一起复制
    blocks.append(ReadmeBlock(False, "", "\n".join(lines[index:end])))
    return end


def split_readme_blocks(text: str) -> list[ReadmeBlock]:
    """Split README markdown into verbatim and translatable blocks.

    Args:
        text (str): Markdown source, with the language bar already removed.

    Returns:
        list[ReadmeBlock]: Blocks in document order; joining ``prefix + text`` of every block
            with newlines yields the markdown to write. A paragraph wrapped over several
            source lines becomes one line, since markdown re-wraps it when rendering.
    """
    lines = text.split("\n")
    blocks: list[ReadmeBlock] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if README_FENCE_RE.match(line):
            index = _take_readme_fence(lines, index, blocks)
            continue
        heading = README_HEADING_RE.match(line)
        if heading and len(heading.group(1)) > 1:  # H1 是项目名，保持原文
            blocks.append(ReadmeBlock(True, heading.group(1) + " ", heading.group(2)))
            index += 1
            continue
        marker = README_LIST_RE.match(line) or README_QUOTE_RE.match(line)
        if marker:
            blocks.append(ReadmeBlock(True, marker.group(1), marker.group(2)))
            index += 1
            continue
        if heading:
            blocks.append(ReadmeBlock(False, "", line))
            index += 1
            continue
        if is_readme_verbatim_line(line):
            blocks.append(ReadmeBlock(False, "", line))
            index += 1
            continue
        end = index
        while end < len(lines) and is_readme_paragraph_line(lines[end]):
            end += 1
        joined = " ".join(one.strip() for one in lines[index:end])
        blocks.append(ReadmeBlock(True, "", joined))
        index = end
    return blocks


def protect_readme_text(text: str):
    """Mask the parts of a block that the translation engine must not touch.

    Args:
        text (str): Markdown text of one translatable block.

    Returns:
        tuple: ``(masked, spans)`` — the text with inline code, links, URLs, tags and bold
            markers replaced by ``{tN}`` tokens, and the replaced runs in token order.
    """
    spans: list[ReadmeSpan] = []

    def replace(match: re.Match) -> str:
        spans.append(
            ReadmeSpan(
                match.group(0),
                match.start() > 0 and not text[match.start() - 1].isspace(),
                match.end() < len(text) and not text[match.end()].isspace(),
            )
        )
        return f"{{t{len(spans) - 1}}}"

    return README_PROTECT_RE.sub(replace, text), spans


def restore_readme_text(text: str, spans: list[ReadmeSpan]) -> str:
    """Put the masked runs back into a translated block.

    The engine pads the tokens and the text around them with spaces (``{t0}`` comes back as
    ``{t 0}``), which would break the markdown glued to a run — ``**`` followed by a space is
    no longer bold — so whitespace the source did not have is dropped again here.

    Args:
        text (str): Translation of the masked block, carrying the ``{tN}`` tokens.
        spans (list[ReadmeSpan]): Runs returned by :func:`protect_readme_text`.

    Returns:
        str: The translation with every token replaced by its original run.

    Raises:
        ValueError: If a token is missing, duplicated or unknown, which means the engine
            rewrote the placeholders and the translation cannot be trusted.
    """
    seen = sorted(int(match.group(1)) for match in README_TOKEN_RE.finditer(text))
    if seen != list(range(len(spans))):
        raise ValueError(f"占位符 {seen} != 0..{len(spans) - 1}")

    pieces = []
    position = 0
    for match in README_TOKEN_RE.finditer(text):
        span = spans[int(match.group(1))]
        gap = match.start()
        if span.glued_left:
            while gap > position and text[gap - 1].isspace():
                gap -= 1
        pieces.append(text[position:gap])
        pieces.append(span.text)
        position = match.end()
        if span.glued_right:
            while position < len(text) and text[position].isspace():
                position += 1
    pieces.append(text[position:])
    return "".join(pieces)


def translate_readme_block(translator, text: str, session: Session):
    """Translate one README block with its spans masked.

    Args:
        translator (object): Engine instance exposing ``translate(text)``.
        text (str): Markdown text of the block, without its leading marker.
        session (Session): Run state holding the request pacer.

    Returns:
        str | None: The translated block, or None when the engine failed or mangled the
            placeholders, in which case the English text must be kept.
    """
    masked, spans = protect_readme_text(text)
    if not any(char.isalpha() for char in README_TOKEN_RE.sub("", masked)):
        return text  # 整块只有代码/链接，无可翻译文本
    translation = translate_text(translator, masked, session)
    if translation is None:
        return None
    try:
        restored = restore_readme_text(translation, spans)
    except ValueError as error:
        print(f"     ❌ 占位符被改写，保留英文: {text[:40]}... → {error}")
        return None
    return " ".join(restored.split())  # 单行块：折叠换行与多余空白


def load_readme_cache(path: Path) -> dict:
    """Load the README translation memory of earlier runs.

    Args:
        path (Path): Cache file holding ``{lang: {block key: translation}}``.

    Returns:
        dict: The parsed memory; empty when the file is missing, unreadable or not a JSON
            object.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"⚠️ README 翻译缓存不可用，本轮重新翻译: {error}")
        return {}
    return data if isinstance(data, dict) else {}


def save_readme_cache(path: Path, cache: dict) -> None:
    """Persist the README translation memory for the next run.

    Args:
        path (Path): Cache file to write.
        cache (dict): ``{lang: {block key: translation}}``, pruned to the blocks this run
            saw.
    """
    ordered = {lang: dict(sorted(cache[lang].items())) for lang in sorted(cache)}
    write_text_atomic(path, json.dumps(ordered, ensure_ascii=False, indent=2) + "\n")


def write_text_atomic(path: Path, text: str) -> bool:
    """Write a text file through a temporary sibling, skipping identical content.

    Args:
        path (Path): Target file.
        text (str): Full content to write, encoded as UTF-8.

    Returns:
        bool: True when the file changed on disk.
    """
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)  # 原子替换：中途被打断不会留下写了一半的 README
    return True


def readme_bar_lines(from_path: Path) -> list[str]:
    """Render the language bar linking every translation of the README.

    Args:
        from_path (Path): README the bar is written into, used to resolve the links.

    Returns:
        list[str]: The opening marker, the link row and the closing marker.
    """
    links = []
    for lang in README_LANGUAGES:
        target = (
            README_REPO_ROOT / README_SOURCE_NAME if lang == "en" else readme_translation_path(lang)
        )
        relative = os.path.relpath(target, from_path.parent).replace(os.sep, "/")
        links.append(f"[{README_LANGUAGE_NAMES.get(lang, lang)}]({relative})")
    return [README_BAR_START, " | ".join(links), README_BAR_END]


def readme_bar_index(lines: list[str]) -> int:
    """Return the line index where the language bar belongs.

    Args:
        lines (list[str]): README lines, split on newlines.

    Returns:
        int: Index just below the title and the badge row, in front of the body text.
    """
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines):
        index += 1  # 跳过标题行（# PyFlow / # Test layout）
    while index < len(lines) and (
        not lines[index].strip() or is_readme_verbatim_line(lines[index])
    ):
        index += 1
    return index


def strip_readme_bar(text: str) -> str:
    """Remove the language bar of a README.

    Args:
        text (str): README content, with or without a language bar.

    Returns:
        str: The content without the bar; byte-identical to the pre-bar source when the bar
            sits where :func:`readme_bar_index` puts it.
    """
    lines = text.split("\n")
    if README_BAR_START not in lines or README_BAR_END not in lines:
        return text
    start = lines.index(README_BAR_START)
    end = lines.index(README_BAR_END)
    del lines[start : end + 1]
    if (
        start > 0
        and start < len(lines)
        and not lines[start].strip()
        and not lines[start - 1].strip()
    ):
        del lines[start]  # 去掉插入时留下的多余空行
    return "\n".join(lines)


def update_readme_bar(path: Path) -> bool:
    """Insert or refresh the language bar of one README.

    Args:
        path (Path): README file to update.

    Returns:
        bool: True when the file changed on disk.
    """
    lines = path.read_text(encoding="utf-8").split("\n")
    bar = readme_bar_lines(path)
    if README_BAR_START in lines and README_BAR_END in lines:
        start = lines.index(README_BAR_START)
        end = lines.index(README_BAR_END)
        updated = lines[:start] + bar + lines[end + 1 :]
    else:
        index = readme_bar_index(lines)
        updated = lines[:index] + bar + [""] + lines[index:]
    return write_text_atomic(path, "\n".join(updated))


def translate_readme_language(lang: str, blocks: list[ReadmeBlock], cache: dict, session: Session):
    """Translate the README blocks into one language and write its translation file.

    Args:
        lang (str): Target language code such as "ja" or "zh_CN".
        blocks (list): Blocks of the stripped README source.
        cache (dict): Translation memory ``{lang: {block key: translation}}``, replaced in
            place by the blocks this call saw.
        session (Session): Run state holding the engine, pacer and limit.

    Returns:
        ReadmeResult: Blocks failed, translated and reused, and whether the endpoint kept
            refusing.
    """
    translator = ENGINES[session.engine](lang, session.proxy)
    stored = cache.get(lang) or {}
    output = [
        block.text if not block.translatable else block.prefix + block.text for block in blocks
    ]
    kept: dict = {}
    translated = reused = failed = consecutive = requests = 0
    aborted = False
    try:
        for index, block in enumerate(blocks):
            if not block.translatable:
                continue
            key = readme_block_key(block.text)
            if key in stored:  # 英文原文没变：直接复用，不再请求
                output[index] = block.prefix + stored[key]
                kept[key] = stored[key]
                reused += 1
                continue
            if session.limit and requests >= session.limit:
                continue  # 达到 --limit：保持英文，下一轮继续
            requests += 1
            if requests % 10 == 0:
                print(f"     ⏳ README {lang} 进度: 处理 {index + 1}/{len(blocks)} 块")
            result = translate_readme_block(translator, block.text, session)
            if result is None:
                failed += 1
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    raise RateLimitAbort(f"{consecutive} consecutive failures")
                continue
            consecutive = 0
            output[index] = block.prefix + result
            kept[key] = result
            translated += 1
    except RateLimitAbort:
        aborted = True
        print(f"     ⛔ README {lang}: 连续失败，落盘已完成的块后停止本轮")

    cache[lang] = kept
    write_text_atomic(readme_translation_path(lang), "\n".join(output) + "\n")
    print(f"  🌐 README {lang}: 新译 {translated} 块，复用 {reused} 块，保留英文 {failed} 块")
    return ReadmeResult(failed, translated, reused, aborted)


def translate_readme(session: Session, langs: list[str]):
    """Translate the repository README and refresh the language bar of every README.

    Blocks that fail keep their English text and are retried on the next run; blocks whose
    English source did not change come from the translation memory, so a run only requests
    what changed.

    Args:
        session (Session): Run state holding the engine, pacer and limit.
        langs (list[str]): Target language codes; the link bar always lists
            ``README_LANGUAGES``.

    Returns:
        int: Number of blocks left in English.

    Raises:
        RateLimitAbort: If the endpoint keeps refusing, after the finished blocks, the cache
            and the language bars have been written.
    """
    source_path = README_REPO_ROOT / README_SOURCE_NAME
    blocks = split_readme_blocks(strip_readme_bar(source_path.read_text(encoding="utf-8")))
    README_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = README_DIR / README_CACHE_NAME
    cache = load_readme_cache(cache_path)
    translatable = sum(1 for block in blocks if block.translatable)
    target = README_DIR.relative_to(README_REPO_ROOT)
    print(f"\n📖 翻译 README → {target}/（{translatable} 个可译块）")

    failed = 0
    aborted = False
    for lang in langs:
        result = translate_readme_language(lang, blocks, cache, session)
        failed += result.failed
        if result.aborted:
            aborted = True
            break

    save_readme_cache(cache_path, cache)
    bar_files = [README_REPO_ROOT / name for name in README_BAR_FILES]
    bar_files += [readme_translation_path(lang) for lang in README_LANGUAGES if lang != "en"]
    for path in bar_files:
        if path.is_file():
            update_readme_bar(path)
    if aborted:
        raise RateLimitAbort(f"README {lang}")
    return failed


def report_abort():
    """Explain why the run stopped on repeated failures and how to resume."""
    print(f"\n⛔ 连续 {MAX_CONSECUTIVE_FAILURES} 条翻译失败，停止本轮。")
    print("ℹ️ google 引擎遇到的是反滥用拦截，换代理节点无效（实测跨大洲换 IP 仍 429）。")
    print("   改用 --engine baidu（国内直连）或 --engine microsoft（带 key）。")
    print("ℹ️ 已完成的译文均已写入 .po 与 README 译本；重新运行本脚本即可续跑。")


def translate_po_file(po_path: Path, target_lang: str, locale_dir: Path, session: Session):
    """Translate the untranslated entries of one .po file.

    Only entries with an empty ``msgstr`` and a non-empty ``msgid`` are touched;
    failed ones stay empty, so the function can be run again until all are done.

    Every ``SAVE_INTERVAL`` translations are written to disk, and the loop flushes once
    more on the way out — the ``RateLimitAbort``, ``KeyboardInterrupt`` and crash paths
    included — so a file that stops halfway keeps the entries that already succeeded.

    Args:
        po_path (Path): The .po file to translate.
        target_lang (str): Target language code, for example "ja" or "zh_CN".
        locale_dir (Path): ``locale/`` root, used to print a relative path.
        session (Session): Run state holding the engine, pacer, cache and limit.

    Returns:
        tuple: ``(failed, translated)`` entry counts for this file.

    Raises:
        RateLimitAbort: If ``MAX_CONSECUTIVE_FAILURES`` entries fail in a row.
    """
    print(f"\n  📄 {po_path.relative_to(locale_dir)}")

    po = polib.pofile(str(po_path))
    target_code = GOOGLE_LANG.get(target_lang, target_lang)

    empty_entries = [e for e in po if e.msgstr == "" and e.msgid]
    total = len(empty_entries)
    if session.limit:
        empty_entries = empty_entries[: session.limit]

    if not empty_entries:
        print("     ✅ 无需翻译（所有条目已有译文）")
        return 0, 0

    suffix = f"，本次处理前 {len(empty_entries)} 条" if session.limit else ""
    print(f"     📝 待翻译: {total} 条{suffix}")

    translator = ENGINES[session.engine](target_lang, session.proxy)
    translated = 0
    reused = 0
    failed = 0
    consecutive = 0
    pending = 0  # 已译好但尚未落盘的条数
    backup = None

    def flush():
        """Write the entries translated so far, so an interrupted run keeps them."""
        nonlocal pending, backup
        if not pending:
            return
        backup = write_po(po, po_path, backup)
        pending = 0
        print(f"     💾 已落盘 {translated + reused} 条（增量写入，中断不丢）")

    try:
        for idx, entry in enumerate(empty_entries, 1):
            if idx % 10 == 0 or idx == 1 or idx == len(empty_entries):
                print(f"     ⏳ 进度: {idx}/{len(empty_entries)} - {entry.msgid[:40]}...")

            key = (target_code, entry.msgid)
            if key in session.cache:  # 同一字符串在多个文件/语言里重复出现，只请求一次
                entry.msgstr = session.cache[key]
                reused += 1
                pending += 1
            else:
                result = translate_text(translator, entry.msgid, session)
                if result is None:
                    failed += 1
                    consecutive += 1
                    if consecutive >= MAX_CONSECUTIVE_FAILURES:
                        raise RateLimitAbort(f"{consecutive} consecutive failures")
                    continue
                consecutive = 0
                entry.msgstr = result
                session.cache[key] = result
                translated += 1
                pending += 1

            if pending >= SAVE_INTERVAL:
                flush()
    finally:
        flush()  # 中止、Ctrl-C 或异常退出时同样保住已完成的译文

    if translated or reused:
        saved = f"，备份: {backup.name}" if backup else ""
        print(f"     ✅ 完成: 新译 {translated} 条，复用 {reused} 条{saved}")
    else:
        print("     ⚠️ 未新增任何翻译")
    if failed:
        print(f"     ℹ️ {failed} 条失败（保持未翻译），重新运行本脚本即可继续")
    return failed, translated


def check_proxy(proxy: str | None) -> bool:
    """Report whether an explicit proxy URL can be used, announcing it when it can.

    Args:
        proxy (str | None): Proxy URL given on the command line; None means "use the
            HTTP(S)_PROXY environment".

    Returns:
        bool: True when the run may continue; False when the proxy is unusable, in
            which case the reason has already been printed.
    """
    if not proxy:
        return True
    if urlparse(proxy).scheme.startswith("socks") and find_spec("socks") is None:
        print("❌ socks 代理需要 PySocks：uv add --group dev pysocks（或改用 http:// 代理）")
        return False
    print(f"🌐 使用代理: {proxy}")
    return True


def collect_po_files(lang_dir: Path, include_generated: bool):
    """List the catalogues to translate for one language.

    Args:
        lang_dir (Path): ``LC_MESSAGES`` directory of the language.
        include_generated (bool): Whether the generated ``api/*.po`` pages are included.

    Returns:
        tuple: ``(po_files, skipped)`` — the catalogues to process and how many
            generated pages were left out.
    """
    po_files = sorted(lang_dir.rglob("*.po"))
    if include_generated:
        return po_files, 0
    kept = [p for p in po_files if not is_generated_page(p, lang_dir)]
    return kept, len(po_files) - len(kept)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        argparse.ArgumentParser: Parser with the ``--lang``, ``--engine``, ``--limit``,
            ``--delay``, ``--proxy``, ``--include-generated`` and ``--ignore-failures``
            options.
    """
    parser = argparse.ArgumentParser(description="批量翻译 locale/**/LC_MESSAGES 下的 .po 文件")
    parser.add_argument(
        "--lang", action="append", choices=LANGUAGES, help="只处理指定语言（可重复）"
    )
    parser.add_argument(
        "--engine",
        choices=sorted(ENGINES),
        default=ENGINE,
        help="翻译引擎：mymemory（默认，免 key，无需代理）/ baidu（需 key）/"
        " microsoft（需 key）/ google（常被反滥用拦截）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="每个 .po 文件、以及每种语言的 README，最多翻译多少条（0=全部）",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY,
        help=f"两次请求的最小间隔秒数（默认 {REQUEST_DELAY}）",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="翻译请求走的代理，如 http://127.0.0.1:7897（默认沿用 HTTP(S)_PROXY 环境变量）",
    )
    parser.add_argument(
        "--include-generated",
        action="store_true",
        help="连带翻译生成的 api/*.po（默认跳过：docstring 属 API 单源，且字符量占绝大部分）",
    )
    parser.add_argument(
        "--ignore-failures",
        action="store_true",
        help="尽力而为：任何失败（目录缺失、引擎未配置、连续失败中止、异常）都只报警告并返回 0，"
        "供 reBuild.sh / Read the Docs 等构建流程使用",
    )
    return parser


def translate_language(lang: str, locale_dir: Path, session: Session, include_generated: bool):
    """Translate every catalogue of one language.

    Args:
        lang (str): Language code such as "ja" or "zh_CN".
        locale_dir (Path): ``locale/`` root.
        session (Session): Run state holding the engine, pacer, cache and limit.
        include_generated (bool): Whether the generated ``api/*.po`` pages are included.

    Returns:
        int: Number of entries that failed and keep their empty ``msgstr``.

    Raises:
        RateLimitAbort: If the endpoint keeps refusing, after the current file was flushed.
    """
    lang_dir = locale_dir / lang / "LC_MESSAGES"
    if not lang_dir.is_dir():
        print(f"⚠️ 跳过 {lang}：目录不存在")
        return 0

    po_files, skipped = collect_po_files(lang_dir, include_generated)
    if not po_files:
        print(f"⚠️ 跳过 {lang}：没有可翻译的 .po 文件")
        return 0

    suffix = f"，跳过 {skipped} 个生成页" if skipped else ""
    print(f"\n🌐 处理语言: {lang} ({len(po_files)} 个文件{suffix})")

    failed = 0
    for po_file in po_files:
        file_failed, _ = translate_po_file(po_file, lang, locale_dir, session)
        failed += file_failed
    return failed


def run_translation(args) -> int:
    """Translate the configured languages under ``locale/``.

    Args:
        args (argparse.Namespace): Options from :func:`build_parser`: ``lang``, ``engine``,
            ``limit``, ``delay``, ``proxy`` and ``include_generated``.

    Returns:
        int: 0 on completion (individual entries may still have failed), 1 when
            ``locale/`` is missing, the engine is unconfigured, or the run stopped on
            repeated failures.
    """
    locale_dir = find_locale_dir(Path(__file__).parent)

    if not locale_dir:
        print("❌ 未找到 locale/ 目录")
        return 1

    print(f"✅ 找到 locale 目录: {locale_dir}")
    print(f"🔧 引擎: {args.engine}")

    if not ENABLE_TRANSLATION:
        print("ℹ️ 翻译功能已关闭，仅扫描文件...")
        for lang in LANGUAGES:
            lang_path = locale_dir / lang / "LC_MESSAGES"
            if lang_path.exists():
                print(f"  {lang}: {len(list(lang_path.rglob('*.po')))} 个 .po 文件")
        return 0

    if not check_proxy(args.proxy):
        return 1

    try:  # fail fast on missing credentials instead of failing entry by entry
        ENGINES[args.engine](LANGUAGES[0], args.proxy)
    except ConfigError as e:
        print(f"❌ {e}")
        return 1

    session = Session(args.delay, args.limit, args.proxy, args.engine)
    languages = args.lang or LANGUAGES
    total_failed = 0
    try:
        if TRANSLATE_README:  # README 优先：先译 README，再译 .po
            total_failed += translate_readme(session, languages)
        for lang in languages:
            total_failed += translate_language(lang, locale_dir, session, args.include_generated)
    except RateLimitAbort:
        report_abort()
        return 1

    if total_failed:
        print(f"\n⚠️ 完成，但有 {total_failed} 条失败（保持未翻译），重新运行本脚本即可继续。")
    else:
        print("\n✅ 所有翻译任务完成！")
    print("📌 请运行: sphinx-intl build")
    return 0


def main(argv=None):
    """Run one translation pass, optionally without ever failing the caller.

    Args:
        argv (list | None): Command-line arguments; None uses ``sys.argv``.

    Returns:
        int: 0 on completion, or on any failure when ``--ignore-failures`` is given (the
            failure is printed and the caller's build continues). Otherwise 1 when
            ``locale/`` is missing, the engine is unconfigured, the run stopped on repeated
            failures, or an unexpected exception escaped :func:`run_translation`.
    """
    args = build_parser().parse_args(argv)
    try:
        status = run_translation(args)
    except Exception as e:  # best-effort mode swallows any engine/network failure
        if not args.ignore_failures:
            raise
        print(f"❌ 翻译过程异常：{type(e).__name__}: {e}")
        status = 1

    if status and args.ignore_failures:
        print("⚠️ 翻译未完成；--ignore-failures 下按成功返回，后续构建继续。")
        print("ℹ️ 已经写入 .po 的译文保持不变，修复后重新运行即可续跑。")
        return 0
    return status


if __name__ == "__main__":
    sys.exit(main())
