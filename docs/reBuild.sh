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
#    ones, including the docstring-generated api/*.po pages (--include-generated).
sphinx-intl update -p _build/gettext -d source/locale
python3.14 source/batch_translate_po.py --include-generated ${TRANSLATE_ARGS:-}

# 3. Compile the catalogues and build one HTML tree per language.
sphinx-intl build -d source/locale
sphinx-build -b html source _build/html/ja -D language=ja
sphinx-build -b html source _build/html/ru -D language=ru
sphinx-build -b html source _build/html/zh_TW -D language=zh_TW
sphinx-build -b html source _build/html/zh_CN -D language=zh_CN
sphinx-build -b html source _build/html/ko -D language=ko
