=============
Configuration
=============

Korgalore uses a TOML configuration file to define targets (Gmail accounts, local
maildirs, JMAP servers, IMAP servers, pipe commands), feeds (mailing lists or lei
searches), and deliveries (which feed goes to which target).

Configuration File Location
===========================

The default configuration file location is:

.. code-block:: bash

   ~/.config/korgalore/korgalore.toml

You can override this with the ``-c`` or ``--cfgfile`` option:

.. code-block:: bash

   kgl -c /path/to/config.toml pull

Modular Configuration (conf.d)
==============================

In addition to the main configuration file, korgalore automatically loads
additional configuration files from the ``conf.d/`` subdirectory:

.. code-block:: bash

   ~/.config/korgalore/conf.d/*.toml

This is useful for:

* Keeping auto-generated configurations separate from your main config
* Organizing configurations by purpose (e.g., one file per subsystem)
* Easily enabling/disabling configurations by adding/removing files

Files are loaded in alphabetical order and merged into the main configuration.
The ``targets``, ``feeds``, and ``deliveries`` sections are merged (new entries
added, existing entries with the same key are overwritten). The ``gui`` section
is replaced entirely if present in a conf.d file.

This feature is used by the ``track-subsystem`` command to store subsystem
tracking configurations separately from your main configuration file.

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

IMAP Setup
==========

IMAP targets provide a way to deliver messages to any IMAP-compatible mail server
(like personal email servers, Office365, generic hosting providers, etc.).

Benefits of IMAP Targets
-------------------------

* **Wide compatibility**: Works with virtually any email provider supporting IMAP
* **SSL-only security**: Always uses encrypted connections on port 993
* **Simple authentication**: Standard password-based authentication
* **Server-side storage**: Messages stored on your mail server
* **Multiple client access**: Access via any IMAP-compatible mail client

Configuring IMAP Targets
-------------------------

To configure an IMAP target, specify the server, credentials, and target folder:

.. code-block:: toml

   [targets.myserver]
   type = 'imap'
   server = 'imap.example.com'
   username = 'user@example.com'
   folder = 'INBOX'
   password_file = '~/.config/korgalore/imap-password.txt'

.. note::
   Labels are ignored for IMAP targets. Messages are delivered to the single
   configured folder only. Any labels specified in deliveries using IMAP targets
   will be silently ignored.

.. warning::
   For security, use ``password_file`` instead of inline ``password`` in your
   configuration file.

IMAP OAuth2 Setup (Microsoft 365)
=================================

Microsoft 365 and Office 365 accounts require OAuth2 authentication (Modern
Authentication) instead of passwords. Korgalore supports this via the XOAUTH2
IMAP extension using the PKCE authorization flow.

Configuring IMAP OAuth2 Targets
-------------------------------

To configure an IMAP target with OAuth2 authentication for Microsoft 365:

.. code-block:: toml

   [targets.office365]
   type = 'imap'
   auth_type = 'oauth2'
   server = 'outlook.office365.com'
   username = 'user@company.com'

That's it! Korgalore includes a built-in Azure AD application, so no additional
setup is required for most users.

OAuth2 Target Parameters
~~~~~~~~~~~~~~~~~~~~~~~~

* ``auth_type``: Must be ``'oauth2'`` to enable OAuth2 authentication
* ``client_id``: (Optional) Azure AD Application (client) ID. Korgalore uses a
  built-in default; only specify this if your organization blocks third-party
  apps and you need to use your own app registration.
* ``tenant``: (Optional) Azure AD tenant - use ``'common'`` for multi-tenant,
  ``'organizations'`` for any work/school account, or your specific tenant ID.
  Default: ``'common'``
* ``token``: (Optional) Path to store the OAuth2 token file. Defaults to
  ``~/.config/korgalore/imap-{identifier}-oauth2-token.json``

.. note::
   Korgalore includes a default Azure AD application ID. However, some
   organizations block third-party applications via conditional access policies.
   If you encounter errors like "AADSTS65001" or "application not approved",
   contact your IT department to obtain an approved client ID and specify it
   in your configuration:

   .. code-block:: toml

      [targets.office365]
      type = 'imap'
      auth_type = 'oauth2'
      server = 'outlook.office365.com'
      username = 'user@company.com'
      client_id = 'client-id-from-it-department'

Authenticating
--------------

The first time you access an OAuth2 IMAP target, korgalore will:

1. Open your default web browser to the Microsoft login page
2. Prompt you to sign in and grant permissions to the application
3. Save the refresh token locally for future use

You can trigger authentication explicitly:

.. code-block:: bash

   kgl auth office365

Once authenticated, the token is stored locally and refreshed automatically.
If the token expires or is revoked, korgalore will prompt for re-authentication.

Using on Headless Servers
-------------------------

For headless servers, authenticate on a machine with a browser first, then copy
the token file:

1. On your workstation, run ``kgl auth office365``
2. Complete the browser authentication
3. Copy the token file to your server:

   .. code-block:: bash

      scp ~/.config/korgalore/imap-office365-oauth2-token.json \
          server:~/.config/korgalore/

4. Ensure the configuration on the server matches (same ``client_id``, etc.)

The token will be refreshed automatically without requiring a browser.

Troubleshooting OAuth2
----------------------

**"AADSTS700016: Application not found"**
   The ``client_id`` is incorrect or the application was deleted. Verify the
   Application ID in Azure Portal.

**"AADSTS65001: User or administrator has not consented"**
   The user hasn't granted permissions. Run ``kgl auth`` to trigger the
   consent flow, or ask your administrator to grant admin consent.

**"AUTHENTICATE failed" after successful login**
   Verify that IMAP.AccessAsUser.All permission is granted and that IMAP is
   enabled for your mailbox in Microsoft 365 admin settings.

**Token refresh fails repeatedly**
   The refresh token may have expired (90 days of inactivity) or been revoked.
   Delete the token file and re-authenticate.

Pipe Setup
==========

Pipe targets provide a way to send raw messages to any external command via stdin.
This is useful for custom processing, filtering, or integration with other tools.

Benefits of Pipe Targets
-------------------------

* **No authentication required**: Messages are piped to a local command
* **Flexibility**: Integrate with any tool that accepts email on stdin
* **Custom processing**: Filter, transform, or archive messages your way
* **Scriptable**: Use shell scripts, Python scripts, or any executable

Configuring Pipe Targets
-------------------------

To configure a pipe target, specify the command to pipe messages to:

.. code-block:: toml

   [targets.filter]
   type = 'pipe'
   command = '/usr/local/bin/process-email.sh'

The command receives the raw email message (RFC 2822 format) on stdin. The command
can include arguments:

.. code-block:: toml

   [targets.archive]
   type = 'pipe'
   command = 'gzip >> ~/mail-archive.gz'

Labels specified in deliveries are appended as additional command line arguments:

.. code-block:: toml

   [targets.processor]
   type = 'pipe'
   command = '/usr/local/bin/process-mail.sh'

   [deliveries.lkml-process]
   feed = 'lkml'
   target = 'processor'
   labels = ['--list=lkml', '--priority=high']

This would execute: ``/usr/local/bin/process-mail.sh --list=lkml --priority=high``

.. warning::
   Ensure the command is trusted and handles email data safely. The raw message
   bytes are piped directly to the command's stdin.

Configuration File Format
=========================

The configuration file uses TOML format and consists of three main sections:
``targets``, ``feeds``, and ``deliveries``.

Targets
-------

Targets define where messages will be delivered (Gmail accounts, local maildirs,
JMAP servers, IMAP servers, pipe commands, etc.).

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

JMAP Targets
~~~~~~~~~~~~

JMAP (JSON Meta Application Protocol) is a modern email protocol that provides
efficient, JSON-based access to mail servers. Fastmail is the primary provider
supporting JMAP.

**Benefits:**

- Modern JSON API (more efficient than IMAP)
- Native folder support
- Unified authentication
- Fast synchronization

**Required Parameters:**

- ``type``: Must be ``jmap``
- ``server``: JMAP server URL (e.g., ``https://api.fastmail.com``)
- ``username``: Your account email address
- ``token`` or ``token_file``: API token for authentication

**Getting a Fastmail API Token:**

1. Log in to Fastmail
2. Go to Settings → Privacy & Security → Integrations
3. Click "New API Token"
4. Give it a name (e.g., "korgalore")
5. Select permissions: **Mail (read/write)**
6. Copy the token and save it to a file

**Example Configuration:**

.. code-block:: toml

   [targets.fastmail]
   type = 'jmap'
   server = 'https://api.fastmail.com'
   username = 'user@fastmail.com'
   token_file = '~/.config/korgalore/fastmail-token.txt'

**Notes:**

- Labels are mapped to JMAP mailboxes/folders
- Folder names are case-insensitive
- Can use folder names (e.g., "INBOX") or roles (e.g., "inbox")
- Messages are imported with original headers preserved
- Authentication uses API bearer tokens only (OAuth is not implemented)

JMAP Target Parameters
~~~~~~~~~~~~~~~~~~~~~~~

* ``type``: Must be ``'jmap'``
* ``server``: JMAP server URL (e.g., ``'https://api.fastmail.com'``)
* ``username``: Your account email address
* ``token``: (Optional*) Bearer token provided inline (less secure)
* ``token_file``: (Optional*) Path to file containing bearer token
* ``timeout``: (Optional) Request timeout in seconds (default: ``60``)

*Either ``token`` or ``token_file`` must be provided.

IMAP Target Parameters
~~~~~~~~~~~~~~~~~~~~~~~

* ``type``: Must be ``'imap'``
* ``server``: IMAP server hostname (e.g., ``'imap.example.com'``)
* ``username``: Your account username or email address
* ``folder``: Target folder for delivery (default: ``'INBOX'``)
* ``auth_type``: (Optional) Authentication type - ``'password'`` (default) or ``'oauth2'``
* ``timeout``: (Optional) Connection timeout in seconds (default: ``60``)

**For password authentication** (``auth_type = 'password'`` or omitted):

* ``password``: (Optional*) Password provided inline (less secure)
* ``password_file``: (Optional*) Path to file containing password (recommended)

*Either ``password`` or ``password_file`` must be provided for password auth.

**For OAuth2 authentication** (``auth_type = 'oauth2'``):

* ``client_id``: (Optional) Azure AD Application (client) ID. Uses built-in default
  if not specified.
* ``tenant``: (Optional) Azure AD tenant ID (default: ``'common'``)
* ``token``: (Optional) Path to OAuth2 token file

See `IMAP OAuth2 Setup (Microsoft 365)`_ for detailed OAuth2 configuration.

.. note::
   IMAP connections always use SSL on port 993 for security.

Pipe Target Parameters
~~~~~~~~~~~~~~~~~~~~~~~

* ``type``: Must be ``'pipe'``
* ``command``: Command to pipe messages to (can include arguments)

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

   [targets.myserver]
   type = 'imap'
   server = 'imap.example.com'
   username = 'user@example.com'
   folder = 'INBOX'
   password_file = '~/.config/korgalore/imap-password.txt'

   [targets.office365]
   type = 'imap'
   auth_type = 'oauth2'
   server = 'outlook.office365.com'
   username = 'user@company.com'

   [targets.processor]
   type = 'pipe'
   command = '/usr/local/bin/process-mail.sh'

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

   # Deliver to IMAP server (password auth)
   [deliveries.lkml-imap]
   feed = 'lkml'
   target = 'myserver'
   labels = []  # Ignored for IMAP targets

   # Deliver to Microsoft 365 (OAuth2 auth)
   [deliveries.lkml-office365]
   feed = 'lkml'
   target = 'office365'
   labels = []  # Ignored for IMAP targets

   # Deliver to pipe command for custom processing
   [deliveries.lkml-process]
   feed = 'lkml'
   target = 'processor'
   labels = ['--list=lkml']  # Appended as command line arguments

   # Using a direct URL without a feed definition
   # [deliveries.example-direct]
   # feed = 'https://lore.kernel.org/example'
   # target = 'work'
   # labels = ['Lists/Example']

   ### GUI ###

   [gui]
   sync_interval = 300  # Sync every 5 minutes

   # Lei source (commented out - requires lei setup)
   # [deliveries.lei-mentions]
   # feed = 'lei:/home/user/lei/mentions'
   # target = 'work'
   # labels = ['INBOX', 'UNREAD']

GUI Configuration
=================

The ``[gui]`` section configures the GNOME taskbar application (``kgl gui``).

.. code-block:: toml

   [gui]
   sync_interval = 300

Parameters
----------

* ``sync_interval``: Time in seconds between automatic syncs (default: 300, i.e., 5 minutes)

The GUI automatically reloads the configuration when you edit it via the
"Edit Config..." menu item, so changes take effect without restarting.

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

Main Section
============

The optional ``[main]`` section configures global korgalore behavior.

.. code-block:: toml

   [main]
   user_agent_plus = "abcd1234"
   catchall_lists = ["linux-kernel@vger.kernel.org", "patches@lists.linux.dev"]

Parameters
----------

* ``user_agent_plus``: (Optional) A string appended to the User-Agent header
  sent to remote servers. This helps server operators identify traffic from
  specific korgalore installations. The resulting User-Agent will be
  ``korgalore/VERSION+VALUE`` (e.g., ``korgalore/0.5+abcd1234``).

* ``catchall_lists``: (Optional) List of mailing list addresses to exclude from
  ``track-subsystem`` queries. These are high-volume lists that receive copies
  of most kernel patches and would flood subsystem-specific queries with
  irrelevant messages. Default:

  .. code-block:: toml

     catchall_lists = [
         "linux-kernel@vger.kernel.org",
         "patches@lists.linux.dev",
     ]

  Set to an empty list ``[]`` to include all mailing lists in queries.

Logging
=======

Enable verbose logging with the ``-v LEVEL`` option:

.. code-block:: bash

   kgl -v DEBUG pull

Save logs to a file with ``--logfile``:

.. code-block:: bash

   kgl --logfile /tmp/korgalore.log pull
