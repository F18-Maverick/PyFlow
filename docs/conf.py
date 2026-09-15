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

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
]

# Docstrings follow the Google style (docs/DOCSTRING_GUIDE.md); the NumPy style is rejected
# there, so leave its parser off instead of silently accepting both.
napoleon_google_docstring = True
napoleon_numpy_docstring = False

locale_dirs = ["locale/"]
templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "locale/**/*.po", "locale/**/*.bak"]
language = "en"


# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "alabaster"
html_static_path = ["_static"]
gettext_uuid = True
gettext_compact = False
