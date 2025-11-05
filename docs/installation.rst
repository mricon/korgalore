============
Installation
============

Prerequisites
=============

Before installing Korgalore, ensure you have the following:

* Python 3.11 or above
* Git
* Gmail API credentials (see :doc:`configuration` for setup instructions)

Installing from PyPI
====================

.. code-block:: bash

   pip install korgalore

Installing from Source
======================

Clone the repository and install:

.. code-block:: bash

   git clone https://git.kernel.org/pub/scm/utils/korgalore/korgalore.git
   cd korgalore
   pip install .

Development Installation
========================

For development, install with dev dependencies:

.. code-block:: bash

   git clone https://git.kernel.org/pub/scm/utils/korgalore/korgalore.git
   cd korgalore
   pip install -e ".[dev]"

This will install the package in editable mode with additional development tools.

Verifying Installation
======================

After installation, verify that the ``kgl`` command is available:

.. code-block:: bash

   kgl --version

This should display the version number of Korgalore.
