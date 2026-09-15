# Documentation Map

> This file is a skeleton; content is still being filled in.

## Documentation layout

| Path | Purpose | Status |
|------|---------|--------|
| `docs/` | Sphinx reST documentation root | ✅ present |
| `docs/conf.py` | Sphinx configuration | ✅ present |
| `docs/templates/` | Change-note, design-note and how-to templates | ✅ present |
| `docs/changes/` | Auto-generated per-PR change notes | ❌ not created yet |
| `docs/design/` | Design notes (ADR) archive | ❌ not created yet |
| `docs/MAP.md` | This file: the documentation map | ✅ this file |

## Normative documents

| File | Purpose |
|------|---------|
| `docs/DOCSTRING_GUIDE.md` | Python docstring rules (Google style) plus C comment rules (Doxygen) |
| `docs/templates/adr.md` | Design-note template → `docs/design/` |
| `docs/templates/changelog-entry.md` | Changelog entry template → CHANGELOG |
| `docs/templates/change-note.md` | Per-PR change note template → `docs/changes/` |
| `docs/templates/how-to.md` | How-to guide template |
| `.github/PULL_REQUEST_TEMPLATE.md` | PR template: original checklist plus documentation impact / design decisions |

## Existing .rst documents

| File | Purpose |
|------|---------|
| `docs/index.rst` | Documentation entry point |
| `docs/Instance_Setup/Instance_Setup.rst` | Instance setup |
| `docs/Crypto/Crypto.rst` | Crypto module |
| `docs/File_Transfer/File_Transfer.rst` | File transfer |
| `docs/Network_APIs/TCP_Server_APIs.rst` | TCP server APIs |
| `docs/Network_APIs/TCP_Client_APIs.rst` | TCP client APIs |
| `docs/Network_APIs/UDP_APIs.rst` | UDP APIs |
| `docs/Port_Allocation/Port_Allocation.rst` | Port allocation |

## Translations

`locale/` holds the gettext catalogues (`.po`): zh_TW, zh_CN, ru, ko, ja.

## Known gaps

- `docs/changes/` and `docs/design/` do not exist yet (their templates are ready).
- No `.. automodule::` / `.. autoclass::` page exists yet: `sphinx.ext.napoleon` and `sphinx.ext.autodoc` are enabled in `docs/conf.py`, but nothing consumes them, so docstrings do not appear in the built docs.
- Docstring backlog: 254 open ruff `D` findings (70 of them undocumented public API); public-API coverage measured by interrogate is 57.6%, gated by `fail-under` in `pyproject.toml`.
- No Doxygen configuration (no `Doxyfile`); `PyFlow/crypto_api/include/` uses plain block comments.
- Pre-existing docs warnings: `docs/_static` missing, malformed table at `Instance_Setup.rst:72`, duplicate label `public-api-summary` at `Port_Allocation.rst:287`, `UDP_APIs.rst` in no toctree.

## To be filled in

- [ ] API reference pages (`.. automodule::`) so napoleon output reaches the docs
- [ ] Raise the interrogate ratchet as the remaining public-API docstrings get written
- [ ] Install / launch / extension-development guides based on `docs/templates/how-to.md`
- [ ] Move parameter detail out of the existing `.rst` files into docstrings (see `DOCSTRING_GUIDE.md` section 11)
