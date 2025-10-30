.. Korgalore documentation master file

Korgalore
=========

Korgalore is a tool for feeding public-inbox git repositories directly
into Gmail via its REST API as an alternative to subscribing. It
provides a workaround for Gmail's notorious hostility to high-volume
technical mailing list traffic.

.. warning::
   This is beta-quality software. It can explode or cause you to miss mail.

Overview
--------

Gmail is notoriously hostile to high-volume technical mailing list
traffic. It will routinely throttle incoming messages, mark them as
spam, or just drop them outright based on some unknown internal
heuristic. Gmail's throttling is responsible for hundreds of thousands
of messages sitting in the kernel.org mail queue just waiting to be
delivered.

Korgalore can feed public-inbox archives and lei search results directly
into Gmail, bypassing most of Gmail's hostile anti-features.

About the Name
--------------

It's a play on "k.org lore" and "Orgalorg," who is a primordial cosmic
entity in the Adventure Time universe -- the "breaker of worlds," which
is basically what Gmail is to mailing lists.

Features
--------

* Direct integration with public-inbox repositories
* Direct Gmail API integration
* Support for lore.kernel.org archives
* Support for lei searches

Non-features
------------

* No filtering (use lei for that)
* No querying (use lei for that)

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   installation
   quickstart
   configuration
   usage
   contributing

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
