============
Installation
============

Prerequisites
=============

Before installing Korgalore, ensure you have the following:

* Python 3.11 or above
* pipx (https://pipx.pypa.io/)
* Git
* Gmail API credentials (see :doc:`configuration` for setup instructions)

Installing from PyPI
====================

.. code-block:: bash

   pipx install korgalore

Installing from Source
======================

Clone the repository and install:

.. code-block:: bash

   git clone https://git.kernel.org/pub/scm/utils/korgalore/korgalore.git
   cd korgalore
   pipx install .

GUI Installation
================

The GNOME taskbar application requires GTK and AppIndicator3 bindings, which
should be installed via system packages:

.. code-block:: bash

   # Debian/Ubuntu
   sudo apt install python3-gi gir1.2-appindicator3-0.1

   # Fedora
   sudo dnf install python3-gobject libappindicator-gtk3

To add Korgalore to your application menu, install the desktop file:

.. code-block:: bash

   # User-level installation
   cp korgalore.desktop ~/.local/share/applications/

   # System-wide installation
   sudo cp korgalore.desktop /usr/share/applications/

Development Installation
========================

For development, install with dev dependencies:

.. code-block:: bash

   git clone https://git.kernel.org/pub/scm/utils/korgalore/korgalore.git
   cd korgalore
   pip install -e ".[dev]"

This will install the package in editable mode with additional development tools.

Shell Completion
================

Korgalore supports tab completion for bash, zsh, and fish. If you installed
via a system package, completions may already be set up. For pip/pipx installs,
you can enable completion manually.

**Bash** (add to ``~/.bashrc``):

.. code-block:: bash

   eval "$(_KGL_COMPLETE=bash_source kgl)"

**Zsh** (add to ``~/.zshrc``):

.. code-block:: bash

   eval "$(_KGL_COMPLETE=zsh_source kgl)"

**Fish** (run once):

.. code-block:: bash

   _KGL_COMPLETE=fish_source kgl > ~/.config/fish/completions/kgl.fish

Alternatively, you can generate static completion files and source them, which
is faster for shell startup:

.. code-block:: bash

   # Bash
   _KGL_COMPLETE=bash_source kgl > ~/.local/share/bash-completion/completions/kgl

   # Zsh (ensure ~/.zfunc is in your fpath)
   _KGL_COMPLETE=zsh_source kgl > ~/.zfunc/_kgl

Verifying Installation
======================

After installation, verify that the ``kgl`` command is available:

.. code-block:: bash

   kgl --version

This should display the version number of Korgalore.
