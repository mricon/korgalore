.. Korgalore documentation master file

Korgalore
=========

Korgalore is a tool for feeding public-inbox git repositories directly
into mail targets (Gmail, JMAP, IMAP, or local maildir) as an alternative
to subscribing. It provides a workaround for Gmail's notorious hostility
to high-volume technical mailing list traffic.

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
into your mail system of choice, bypassing most of Gmail's hostile
anti-features when used with Gmail targets.

About the Name
--------------

It's a play on "k.org lore" and "Orgalorg," who is a primordial cosmic
entity in the Adventure Time universe -- the "breaker of worlds," which
is basically what Gmail is to mailing lists.

Features
--------

* Direct integration with public-inbox repositories
* Support for lore.kernel.org archives
* Support for lei searches
* Multiple delivery targets:

  * Gmail (via REST API)
  * JMAP servers (e.g., Fastmail)
  * IMAP servers
  * Local maildir
  * Pipe commands

* GNOME taskbar application for background syncing
* Bozofilter for blocking unwanted senders

Non-features
------------

* No filtering beyond bozofilter (use lei for that)
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
