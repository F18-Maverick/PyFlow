#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch-translate the .po files of this Sphinx project into several languages.

Walks ``locale/<lang>/LC_MESSAGES/**/*.po`` recursively and translates every
entry whose ``msgstr`` is still empty. A failed entry keeps its empty ``msgstr``,
so running the script again continues where it left off.

Engines (``--engine``)
    ``mymemory``  MyMemory REST API — **default, no key at all**. Reachable without a proxy
                (verified from a mainland connection), so it needs neither an account nor a
                proxy. Anonymous quota is 5,000 characters per day per IP, which covers this
                project's ~4.8k characters per pass; set ``MYMEMORY_EMAIL`` (any address, no
                registration) to raise it to 50,000. Quality is translation-memory grade.
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
    python3 batch_translate_po.py --limit 5       # at most 5 entries per file
    python3 batch_translate_po.py --proxy http://127.0.0.1:7897   # route via an explicit proxy
    python3 batch_translate_po.py --include-generated --ignore-failures   # full pipeline run
"""

import argparse
import os
import sys
import time
from hashlib import md5
from importlib.util import find_spec
from pathlib import Path
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
ENABLE_TRANSLATION = True

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


def translate_po_file(po_path: Path, target_lang: str, locale_dir: Path, session: Session):
    """Translate the untranslated entries of one .po file.

    Only entries with an empty ``msgstr`` and a non-empty ``msgid`` are touched;
    failed ones stay empty, so the function can be run again until all are done.

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
    for idx, entry in enumerate(empty_entries, 1):
        if idx % 10 == 0 or idx == 1 or idx == len(empty_entries):
            print(f"     ⏳ 进度: {idx}/{len(empty_entries)} - {entry.msgid[:40]}...")

        key = (target_code, entry.msgid)
        if key in session.cache:  # 同一字符串在多个文件/语言里重复出现，只请求一次
            entry.msgstr = session.cache[key]
            reused += 1
            continue

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

    if translated or reused:
        backup = po_path.with_suffix(po_path.suffix + ".bak")
        po_path.rename(backup)
        po.save(str(po_path))
        print(f"     ✅ 完成: 新译 {translated} 条，复用 {reused} 条，备份: {backup.name}")
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
    parser.add_argument("--limit", type=int, default=0, help="每个文件最多翻译多少条（0=全部）")
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
        tuple: ``(failed, aborted)`` — the number of entries that failed and whether the
            endpoint kept refusing, in which case the caller stops the run.
    """
    lang_dir = locale_dir / lang / "LC_MESSAGES"
    if not lang_dir.is_dir():
        print(f"⚠️ 跳过 {lang}：目录不存在")
        return 0, False

    po_files, skipped = collect_po_files(lang_dir, include_generated)
    if not po_files:
        print(f"⚠️ 跳过 {lang}：没有可翻译的 .po 文件")
        return 0, False

    suffix = f"，跳过 {skipped} 个生成页" if skipped else ""
    print(f"\n🌐 处理语言: {lang} ({len(po_files)} 个文件{suffix})")

    failed = 0
    for po_file in po_files:
        try:
            file_failed, _ = translate_po_file(po_file, lang, locale_dir, session)
        except RateLimitAbort:
            print(f"\n⛔ 连续 {MAX_CONSECUTIVE_FAILURES} 条翻译失败，停止本轮。")
            print("ℹ️ google 引擎遇到的是反滥用拦截，换代理节点无效（实测跨大洲换 IP 仍 429）。")
            print("   改用 --engine baidu（国内直连）或 --engine azure（带 key）。")
            print("ℹ️ 已完成的译文均已写入 .po；重新运行本脚本即可续跑。")
            return failed, True
        failed += file_failed
    return failed, False


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
    total_failed = 0
    for lang in args.lang or LANGUAGES:
        failed, aborted = translate_language(lang, locale_dir, session, args.include_generated)
        total_failed += failed
        if aborted:
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
