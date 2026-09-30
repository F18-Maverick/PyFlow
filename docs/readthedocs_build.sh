set -e

cd ./docs

sphinx-intl build -d source/locale

sphinx-build -b html source "$READTHEDOCS_OUTPUT/html"
