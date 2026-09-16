sphinx-apidoc -o api -T -e --separate --module-first --force ../PyFlow
sphinx-build -b gettext . _build/gettext
sphinx-intl update -p _build/gettext
python3.14t batch_translate_po.py --proxy http://127.0.0.1:7897 ${TRANSLATE_ARGS:-}
sphinx-intl build
sphinx-build -b html . _build/html/ja -D language=ja
sphinx-build -b html . _build/html/ru -D language=ru
sphinx-build -b html . _build/html/zh_TW -D language=zh_TW
sphinx-build -b html . _build/html/zh_CN -D language=zh_CN
sphinx-build -b html . _build/html/ko -D language=ko
