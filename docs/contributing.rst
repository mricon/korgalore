============
Contributing
============

We welcome contributions to Korgalore! This document outlines how to contribute
to the project.

Getting Started
===============

Development Setup
-----------------

1. Clone the repository:

   .. code-block:: bash

      git clone https://git.kernel.org/pub/scm/utils/korgalore/korgalore.git
      cd korgalore

2. Install in development mode:

   .. code-block:: bash

      pip install -e ".[dev]"

3. Verify the installation:

   .. code-block:: bash

      kgl --version
      pytest

Code Style
==========

Python Code
-----------

* Follow PEP 8 style guide
* Use type hints for all function signatures
* Maximum line length: 100 characters
* Use meaningful variable and function names

Type Checking
-------------

Korgalore uses mypy for static type checking:

.. code-block:: bash

   mypy src/korgalore/

All code should pass mypy strict mode checks.

Testing
=======

Running Tests
-------------

Tests are lacking, but the scaffolding is there to be run with pytest:

.. code-block:: bash

   pytest

Run with coverage:

.. code-block:: bash

   pytest --cov=korgalore

Submitting Changes
==================

Email Workflow
--------------

Korgalore uses an email-based workflow. Send patches to:

   tools@kernel.org

Preparing Patches
-----------------

Save yourself a lot of trouble and use b4_.

.. _b4: https://b4.docs.kernel.org/


Developer Certificate of Origin
================================

Korgalore uses the Developer Certificate of Origin (DCO) instead of a
Contributor License Agreement. By adding a Signed-off-by line to your
commits, you certify that you have the right to submit the code and
agree to the DCO.

See the DCO file in the repository for full text.

Communication
=============

Mailing List
------------

All development discussion happens on the mailing list:

   tools@kernel.org
