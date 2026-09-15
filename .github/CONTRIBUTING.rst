Contributing to PyFlow
==============================

Thanks!
--------
First, thank you for your interest in contributing to our project! 
We welcome contributions from the community, and we appreciate your 
efforts to help improve PyFlow.

Before contributing:
--------------------
1. Before you start to contribute, especially before opening a pull request,
   please make sure that you have already opened an issue to discuss the
   changes you want to make. This will help us understand your intentions and
   provide feedback before you start working on the code, tests or docs etc.,
   or your PR may be closed without review. In addition, please add the issue
   number or the URL in the description of your pull request, so we can easily
   track the related issue and PR.

2. Please make sure that you have already forked the PyFlow
repository.

3. After you have forked the repository, please clone it to your local machine:

.. code-block:: bash

   git clone https://github.com/<your-username>/PyFlow.git
   cd ./PyFlow

4. If there are new commits in the upstream repository that your fork
does not have, please pull the latest changes to avoid merge conflicts:

.. code-block:: bash

   git pull origin main --rebase

5. Create a new branch for your changes
   (Make sure the branch name is descriptive of the changes you are making):

.. code-block:: bash

   git checkout -b <your-branch-name>

6. When you have made your changes, commit and push them with a clear
   and descriptive commit message:

.. code-block:: bash

   git add <changed-files>
   git commit -m "Describe your changes here"
   git push origin <your-branch-name>

7. Finally, open a pull request to the original repository.

Documentation and style
-----------------------

Public Python interfaces need a docstring that follows ``docs/DOCSTRING_GUIDE.md``
(Google style: summary line, ``Args``, ``Returns``, ``Raises``); C comments follow
the Doxygen rules in that same file. The guide is the single source of truth for
structure and wording, and ``docs/MAP.md`` maps where each kind of document
belongs.

Run the checks that CI runs before you push:

.. code-block:: bash

   uvx ruff check     # style, including the docstring rules configured in pyproject.toml
   uvx interrogate    # public-API docstring coverage ratchet

Docstrings carry the API detail (arguments, return values, exceptions); files
under ``docs/`` carry module purpose, tutorials and architecture only. Design
decisions belong in ``docs/design/`` (``docs/templates/adr.md``), per-PR change
notes in ``docs/changes/`` (``docs/templates/change-note.md``), and
``.github/PULL_REQUEST_TEMPLATE.md`` asks about both when you open the pull
request.
