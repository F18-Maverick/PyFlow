sphinx-apidoc -o api -T -e --separate --module-first --force ../PyFlow
sphinx-build -b gettext . _build/gettext
sphinx-intl update -p _build/gettext
# 引擎可用环境变量切换，例如: TRANSLATE_ARGS="--engine baidu" ./reBuild.sh（需 BAIDU_APPID/BAIDU_KEY）
python3.14t batch_translate_po.py --proxy http://127.0.0.1:7897 ${TRANSLATE_ARGS:-}
sphinx-intl build
sphinx-build -b html . _build/html/ja -D language=ja
sphinx-build -b html . _build/html/ru -D language=ru
sphinx-build -b html . _build/html/zh_TW -D language=zh_TW
sphinx-build -b html . _build/html/zh_CN -D language=zh_CN
sphinx-build -b html . _build/html/ko -D language=ko

