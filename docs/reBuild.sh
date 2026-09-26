#!/usr/bin/env bash
# Rebuild the English Sphinx docs plus every translation under docs/source/locale/.
#
# Run it from anywhere (the Makefile/make.bat equivalents are for a plain HTML build); all
# paths below are relative to docs/, so the script moves there first.
set -euo pipefail
cd "$(dirname "$0")"

# 1. Regenerate the autodoc stubs and extract the translatable strings into gettext.
sphinx-apidoc -o source/api -T -e --separate --module-first --force ../PyFlow
sphinx-build -b gettext source _build/gettext

# 2. Update the catalogues (source/locale/**/*.po) and machine-translate the still-empty
#    ones, including the docstring-generated api/*.po pages (--include-generated). The step
#    is best-effort: --ignore-failures turns a failed run into a warning, and the trailing
#    guard covers a translation step that cannot start at all (missing interpreter or
#    dependency), so step 3 always runs on whatever is already translated.
sphinx-intl update -p _build/gettext -d source/locale
python3.14 source/batch_translate_po.py --include-generated --ignore-failures ${TRANSLATE_ARGS:-} \
  || echo "⚠️ 翻译步骤未能执行，继续构建（英文与已有译文仍然可用）。"

# 3. Compile the catalogues and build one HTML tree per language.
sphinx-intl build -d source/locale
sphinx-build -b html source _build/html/ja -D language=ja
sphinx-build -b html source _build/html/ru -D language=ru
sphinx-build -b html source _build/html/zh_TW -D language=zh_TW
sphinx-build -b html source _build/html/zh_CN -D language=zh_CN
sphinx-build -b html source _build/html/ko -D language=ko
