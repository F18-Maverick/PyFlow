"""Sphinx configuration for the PyFlow documentation.

Docstrings are read as Google style by ``sphinx.ext.napoleon`` and pulled into the pages by
``sphinx.ext.autodoc``; the API pages under ``docs/source/api/`` are generated with
``sphinx-apidoc``. The rules they follow live in ``docs/source/DOCSTRING_GUIDE.md``.
"""

import os
import sys

# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = "PyFlow"
copyright = "2026, RayXu"
author = "RayXu"
release = "0.0.1-alpha"

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

# autodoc imports the package, so the repository root (two levels up: docs/source/conf.py ->
# docs/ -> repository root) must be on sys.path. Without this the docs only build from an
# environment where PyFlow is installed; Sphinx puts only docs/source on sys.path itself.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
]

# Docstrings follow the Google style (docs/source/DOCSTRING_GUIDE.md); the NumPy style is
# rejected there, so leave its parser off instead of silently accepting both.
napoleon_google_docstring = True
napoleon_numpy_docstring = False

# The API pages under docs/source/api are generated with sphinx-apidoc; these defaults give
# them the shape docs/source/DOCSTRING_GUIDE.md requires (class __init__ documented, source
# order).
autodoc_default_options = {
    "members": True,
    "show-inheritance": True,
    "member-order": "bysource",
    "special-members": "__init__",
}

locale_dirs = ["locale/"]
templates_path = ["_templates"]
# The build directory (docs/_build) lives outside this source directory, so it needs no
# exclusion here; only generated catalogues and editor droppings do.
exclude_patterns = ["Thumbs.db", ".DS_Store", "locale/**/*.po", "locale/**/*.bak"]
language = "en"


# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "alabaster"
html_static_path = ["_static"]
gettext_uuid = True
gettext_compact = False
