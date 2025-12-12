=============
Configuration
=============

Korgalore uses a TOML configuration file to define targets (Gmail accounts, local
maildirs, etc.), feeds (mailing lists or lei searches), and deliveries (which feed
goes to which target).

Configuration File Location
===========================

The default configuration file location is:

.. code-block:: bash

   ~/.config/korgalore/korgalore.toml

You can override this with the ``-c`` or ``--cfgfile`` option:

.. code-block:: bash

   kgl -c /path/to/config.toml pull

Gmail Setup
===========

Gmail went out of their way to make it super difficult to access your inbox via an API,
so please be prepared to suffer a bit. You will need to download an OAuth 2.0 Client ID
file from Google that will authorize your access.

Creating OAuth Credentials
---------------------------

1. Go to the `Google Cloud Console <https://console.cloud.google.com/>`_
2. Create a new project (or select an existing one)
3. Enable the Gmail API for your project
4. Create OAuth 2.0 credentials:

   * Go to "Credentials" in the left sidebar
   * Click "Create Credentials" > "OAuth client ID"
   * Choose "Desktop app" as the application type
   * Download the JSON file

5. Save the downloaded file and add a target that will be using it in
   the korgalore.toml configuration file (see below).

For detailed instructions on getting your project set up and obtaining
credentials.json, follow Google's quickstart guide:
https://developers.google.com/workspace/gmail/api/quickstart/python#set-up-environment

Authenticating
--------------

You should first define your gmail account target in the config file.

After that, the first time you run ``kgl pull``, you will be prompted to
log in to your Google account and authorize the application. You can
also run ``kgl auth`` to the same effect.

Once you obtain the token file (usually located at
``~/.config/korgalore/gmail-personal-token.json``), you can copy it to a
headless node and modify your configuration file to point to it:

.. code-block:: toml

   [targets.personal]
   type = 'gmail'
   credentials = '~/.config/korgalore/credentials.json'
   token = '~/.config/korgalore/gmail-personal-token.json'

This will let you run korgalore on a headless node as opposed to your
workstation.

Maildir Setup
=============

Maildir targets provide a simpler alternative to Gmail for local message storage.
They deliver messages to a local maildir directory on your filesystem, which can
be read by mail clients like mutt, thunderbird, or any other maildir-compatible
application.

Benefits of Maildir Targets
----------------------------

* **No authentication required**: Messages are written directly to your local filesystem
* **Privacy**: All messages stay on your local machine
* **Offline access**: No internet connection needed to read messages
* **Standard format**: Compatible with most Unix mail clients
* **Easy backup**: Just copy the maildir directory

Configuring Maildir Targets
----------------------------

To configure a maildir target, simply specify the path where you want messages stored:

.. code-block:: toml

   [targets.local]
   type = 'maildir'
   path = '~/Mail/lkml'

The maildir will be created automatically if it doesn't exist. The standard maildir
structure (cur/, new/, tmp/ subdirectories) is handled automatically.

.. note::
   Labels are ignored for maildir targets. Maildir doesn't support Gmail-style
   labels, so any labels specified in deliveries using maildir targets will be
   silently ignored.

Configuration File Format
=========================

The configuration file uses TOML format and consists of three main sections:
``targets``, ``feeds``, and ``deliveries``.

Targets
-------

Targets define where messages will be delivered (Gmail accounts, local maildirs, etc.).

.. code-block:: toml

   [targets.personal]
   type = 'gmail'
   credentials = '/path/to/personal-credentials.json'

   [targets.work]
   type = 'gmail'
   credentials = '/path/to/work-credentials.json'

   [targets.local]
   type = 'maildir'
   path = '~/Mail/lkml'

Gmail Target Parameters
~~~~~~~~~~~~~~~~~~~~~~~~

* ``type``: Must be ``'gmail'``
* ``credentials``: Path to the OAuth credentials JSON file
* ``token``: (Optional) Path to store the OAuth token. Defaults to
  ``~/.config/korgalore/gmail-{identifier}-token.json``

Maildir Target Parameters
~~~~~~~~~~~~~~~~~~~~~~~~~~

* ``type``: Must be ``'maildir'``
* ``path``: Path to the maildir directory (will be created if it doesn't exist)

Feeds
-----

Feeds define reusable feed URLs that can be referenced by multiple deliveries.
This is useful when you want to import the same mailing list feed into
different Gmail accounts with different labels.

.. code-block:: toml

   [feeds.lkml]
   url = 'https://lore.kernel.org/lkml'

   [feeds.git]
   url = 'https://lore.kernel.org/git'

Feed Parameters
~~~~~~~~~~~~~~~

* ``url``: The URL of the feed (must start with ``https:`` for lore feeds or ``lei:`` for lei searches)

Deliveries
----------

Deliveries define mailing lists or lei searches to import from. A delivery can
reference a feed by name (defined in the ``feeds`` section) or use a direct URL.

Lore.kernel.org Deliveries
~~~~~~~~~~~~~~~~~~~~~~~~~~

Deliveries can reference feeds by name or use direct URLs:

.. code-block:: toml

   # Using a named feed (defined in the feeds section)
   [deliveries.lkml]
   feed = 'lkml'
   target = 'personal'
   labels = ['Lists/LKML']

   # Using a direct URL
   [deliveries.linux-doc]
   feed = 'https://lore.kernel.org/linux-doc'
   target = 'work'
   labels = ['Lists/Docs', 'UNREAD']

Lei Deliveries
~~~~~~~~~~~~~~

.. note::
   This requires ``lei`` to be installed and configured separately.

You will need to first set up a lei query to write into a "v2" output.
For example, this queries all messages where someone says "wtf":

.. code-block:: bash

    $ lei q --only=https://lore.kernel.org/all \
        --output=v2:/home/user/lei/wtf \
        --dedupe=mid \
        'nq:wtf AND rt:1.week.ago..'

This will create a /home/user/lei/wtf hierarchy with a "git/0.git"
subdirectory that korgalore will look at for updates.

You can add this feed into ``korgalore.conf`` now:

.. code-block:: toml

   [deliveries.lei-wtf]
   feed = 'lei:/home/user/lei/wtf'
   target = 'work'
   labels = ['INBOX', 'UNREAD']

Korgalore will run ``lei up`` automatically for you.

Delivery Parameters
~~~~~~~~~~~~~~~~~~~

* ``feed``: Name of a feed defined in the ``feeds`` section, or a direct URL of a lore.kernel.org archive or lei search path (prefixed with ``lei:``)
* ``target``: Identifier of the target (must match a target name defined in the ``targets`` section)
* ``labels``: List of labels to apply to imported messages (Gmail only; ignored for maildir targets)

Gmail Labels
------------

.. note::
   This section only applies to Gmail targets. Maildir targets ignore labels.

Labels must exist in your Gmail account before importing messages,
korgalore will not create them for you. You can list existing labels in
your Gmail target by using::

    kgl labels [targetname]

Complete Example
================

Here's a complete configuration file example showing both Gmail and maildir targets:

.. code-block:: toml

   ### Targets ###

   [targets.personal]
   type = 'gmail'
   credentials = '~/.config/korgalore/personal-credentials.json'

   [targets.work]
   type = 'gmail'
   credentials = '~/.config/korgalore/work-credentials.json'

   [targets.archive]
   type = 'maildir'
   path = '~/Mail/archive'

   ### Feeds ###

   [feeds.lkml]
   url = 'https://lore.kernel.org/lkml'

   [feeds.linux-doc]
   url = 'https://lore.kernel.org/linux-doc'

   [feeds.git]
   url = 'https://lore.kernel.org/git'

   ### Deliveries ###

   [deliveries.lkml]
   feed = 'lkml'  # References the feed defined above
   target = 'work'
   labels = ['Lists/LKML']

   [deliveries.linux-doc]
   feed = 'linux-doc'  # References the feed defined above
   target = 'work'
   labels = ['Lists/Docs']

   [deliveries.git]
   feed = 'git'  # References the feed defined above
   target = 'personal'
   labels = ['INBOX', 'UNREAD']

   # Deliver the same feed to both Gmail and local maildir
   [deliveries.lkml-archive]
   feed = 'lkml'  # Same feed as above!
   target = 'archive'  # Maildir target
   labels = []  # Ignored for maildir targets

   # Using a direct URL without a feed definition
   # [deliveries.example-direct]
   # feed = 'https://lore.kernel.org/example'
   # target = 'work'
   # labels = ['Lists/Example']

   # Lei source (commented out - requires lei setup)
   # [deliveries.lei-mentions]
   # feed = 'lei:/home/user/lei/mentions'
   # target = 'work'
   # labels = ['INBOX', 'UNREAD']

Data Directory
==============

Korgalore stores data in the XDG data directory:

.. code-block:: bash

   ~/.local/share/korgalore/

This directory contains:

* Cloned git repositories for each lore mailing list
* Epoch tracking information
* Metadata about imported messages

You can override this by setting the ``XDG_DATA_HOME`` environment
variable, but then korgalore will lose your existing clones, so this is
not advised.

Logging
=======

Enable verbose logging with the ``-v LEVEL`` option:

.. code-block:: bash

   kgl -v DEBUG pull

Save logs to a file with ``--logfile``:

.. code-block:: bash

   kgl --logfile /tmp/korgalore.log pull
