=============
Configuration
=============

Korgalore uses a TOML file for its configuration. It defines the
following main sections:

* targets: can be Gmail accounts, local maildirs, JMAP servers, IMAP servers, pipe commands, or a no-op dummy target
* feeds: public-inbox locations or lei searches
* deliveries: mapping of which feed goes to which target

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
tracking configurations separately from your main configuration file. Each
generated file includes a ``[subsystem]`` section with the human-readable
subsystem name, used by ``track-subsystem --list`` for display and by
``--forget`` for identification.

The ``subscribe`` command also uses conf.d, storing each subscription as
``sub-{feed_key}.toml``. Paused subscriptions are renamed to
``sub-{feed_key}.toml.paused`` so they are excluded from the ``*.toml`` glob.

Gmail Setup
===========

Gmail went out of their way to make it super difficult to access your
inbox via an API, so please be prepared to suffer a bit. You will need
to download an OAuth 2.0 Client ID file from Google that will authorize
your access.

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

You can use the ``subfolder`` parameter in deliveries to route messages into
subdirectories under the target's base path. Maildir subfolders additionally
support strftime format codes (e.g., ``%Y/%m``) for date-based directory
organisation. This is particularly useful for high-traffic lists like
linux-kernel, where a single maildir can accumulate tens of thousands of
files and cause filesystem performance penalties:

.. code-block:: toml

   [targets.archive]
   type = 'maildir'
   path = '~/Mail'

   [deliveries.lkml]
   feed = 'lkml'
   target = 'archive'
   subfolder = 'Lists/LKML'  # Results in ~/Mail/Lists/LKML

   [deliveries.lkml-archive]
   feed = 'lkml'
   target = 'archive'
   subfolder = '%Y/%m'  # Results in ~/Mail/2026/01

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
can include arguments. It is split like a shell command line, and ``$VARS`` and a
leading ``~`` are expanded, but no shell runs it. For redirects or pipes, run a
shell yourself:

.. code-block:: toml

   [targets.archive]
   type = 'pipe'
   command = "sh -c 'gzip >> ~/mail-archive.gz'"

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

Dummy Setup
===========

Dummy targets discard every message delivered to them. They exist because
korgalore refuses to run any command until at least one target is configured,
even for setups that don't need a delivery copy of messages at all -- for
example, when the lei v2 archive underlying your feeds is already the only
mail storage you need.

.. code-block:: toml

   [targets.discard]
   type = 'dummy'

No other configuration is required or accepted for a dummy target.

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
* ``credentials``: Path to the OAuth credentials JSON file (required unless
  ``token_command`` is specified)
* ``token``: (Optional) Path to store the OAuth token. Defaults to
  ``~/.config/korgalore/gmail-{identifier}-token.json``
* ``token_command``: (Optional) External command that prints a valid OAuth2
  bearer token to stdout. Mutually exclusive with both ``credentials`` and
  ``token``. It is split like a shell command line, and ``$VARS`` and a
  leading ``~`` are expanded, but no shell runs it. The bearer token returned by the command must carry the OAuth
  scopes required by korgalore (``https://www.googleapis.com/auth/gmail.labels``
  and ``https://www.googleapis.com/auth/gmail.insert``).

Maildir Target Parameters
~~~~~~~~~~~~~~~~~~~~~~~~~~

* ``type``: Must be ``'maildir'``
* ``path``: Path to the maildir directory (will be created if it doesn't exist)

Maildir deliveries also support the ``subfolder`` delivery parameter, which
creates subdirectories under the base path. Subfolders can use strftime format
codes for date-based directories (e.g., ``%Y/%m`` becomes ``2026/01``). See
`Delivery Parameters`_ for details.

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
* ``token``: Bearer token provided inline (less secure)
* ``token_file``: Path to file containing bearer token
* ``timeout``: (Optional) Request timeout in seconds (default: ``60``)

.. important::
   Either ``token`` or ``token_file`` must be provided.

IMAP Target Parameters
~~~~~~~~~~~~~~~~~~~~~~~

* ``type``: Must be ``'imap'``
* ``server``: IMAP server hostname (e.g., ``'imap.example.com'``)
* ``username``: Your account username or email address
* ``folder``: Target folder for delivery (default: ``'INBOX'``)
* ``auth_type``: (Optional) Authentication type - ``'password'`` (default) or ``'oauth2'``
* ``timeout``: (Optional) Connection timeout in seconds (default: ``60``)

**For password authentication** (``auth_type = 'password'`` or omitted):

* ``password``: Password provided inline (less secure)
* ``password_file``: Path to file containing password (recommended)

.. important::
   Either ``password`` or ``password_file`` must be provided for password auth.

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
* ``command``: Command to pipe messages to (can include arguments). ``$VARS``
  and a leading ``~`` are expanded, but no shell runs it

Dummy Target Parameters
~~~~~~~~~~~~~~~~~~~~~~~~

* ``type``: Must be ``'dummy'``

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
* ``labels``: List of labels to apply to imported messages (Gmail/JMAP only; ignored for maildir and IMAP targets)
* ``mode``: (Optional) ``'message'`` (the default) delivers every message.
  ``'digest'`` sends one summary email per period instead; see
  `Digest Deliveries`_ for the other keys it uses
* ``subfolder``: (Optional) Subfolder path for IMAP and Maildir targets

  - For IMAP: Combined with the target's base folder (e.g., ``INBOX`` + ``Lists/LKML`` = ``INBOX/Lists/LKML``)
  - For Maildir: Creates a subdirectory under the target's base path
  - Ignored for Gmail, JMAP, and pipe targets (use ``labels`` for Gmail/JMAP)
  - Must be a string, not a list
  - **Maildir only**: Supports strftime format codes for date-based directories
    (e.g., ``Archive/%Y/%m`` becomes ``Archive/2026/01``). The template is
    validated at startup. For CLI commands, the template is expanded once per
    invocation. For the GUI, the template is refreshed before each periodic
    sync to ensure messages are delivered to the correct date-based folder.

Example with subfolder (IMAP):

.. code-block:: toml

   [targets.imap-server]
   type = 'imap'
   server = 'imap.example.com'
   username = 'user@example.com'
   folder = 'INBOX'  # Base folder
   password_file = '~/.config/korgalore/imap-password.txt'

   [deliveries.lkml]
   feed = 'lkml'
   target = 'imap-server'
   subfolder = 'Lists/LKML'  # Results in INBOX/Lists/LKML

Example with subfolder (Maildir):

.. code-block:: toml

   [targets.local]
   type = 'maildir'
   path = '~/Mail'  # Base path

   [deliveries.lkml]
   feed = 'lkml'
   target = 'local'
   subfolder = 'Lists/LKML'  # Results in ~/Mail/Lists/LKML

   [deliveries.git]
   feed = 'git'
   target = 'local'
   subfolder = 'Lists/Git'  # Results in ~/Mail/Lists/Git

Example with date-based archiving (Maildir only):

.. code-block:: toml

   [targets.archive]
   type = 'maildir'
   path = '~/Mail/Archive'

   [deliveries.lkml-archive]
   feed = 'lkml'
   target = 'archive'
   subfolder = '%Y/%m'  # Results in ~/Mail/Archive/2026/01

Digest Deliveries
~~~~~~~~~~~~~~~~~

.. warning::
   Digests are in beta, and these keys may change. See
   :doc:`digests` for a walkthrough and for troubleshooting.

A digest delivery doesn't put every message in your inbox. Instead, it
sends one email per period (a day or a week) that lists the threads in
the feed. The threads are in sections: new patches and pull requests,
updates to earlier patches, bug reports, and discussions. For each
thread you get:

* who started it, how many messages and patches it has, and any
  ``Reviewed-by`` or ``Acked-by`` trailers
* the patches posted, in series order, and the replies
* a link to the thread on lore.kernel.org. To get the whole thread into
  your mailbox, give that link to ``kgl yank -T``. To follow the thread
  from now on, give it to ``kgl track add``.

The email has a plain text part and an HTML part. The HTML part has no
scripts and loads nothing from the network.

.. code-block:: toml

   [deliveries.lkml-digest]
   feed = 'lkml'
   target = 'personal'
   labels = ['INBOX']
   mode = 'digest'
   schedule = 'daily'    # or 'weekly'
   send_at = '07:00'     # local time

Digest parameters (only allowed with ``mode = 'digest'``):

* ``schedule``: ``'daily'`` (the default) or ``'weekly'``
* ``send_at``: Local time to send at, as ``'HH:MM'`` (default ``'07:00'``).
  This is wall-clock time, so a 07:00 digest stays at 07:00 when daylight
  saving time starts or ends
* ``send_day``: Weekly digests only: the day to send on, such as
  ``'mon'`` or ``'friday'`` (default ``'mon'``)
* ``send_empty``: Send a digest even when nothing happened in the period
  (default ``false``, so quiet days send nothing)
* ``digest_from``: The ``From:`` address of the digest email (default
  ``'korgalore <korgalore@localhost>'``). Set this to an address your mail
  filters will recognize
* ``digest_format``: Which parts the digest email has. ``'both'`` (the
  default) sends plain text and HTML together, and your mail client
  shows the one it prefers. ``'plain'`` sends only plain text, for
  people who never read HTML mail. ``'html'`` sends only HTML, for
  people who always read digests in a browser or a webmail client

How digests are sent:

* ``kgl pull`` (and the GUI) sends any digest that is due, after it
  delivers the other messages. ``kgl digest`` sends due digests without
  delivering anything else. Neither one waits until ``send_at``: a digest
  goes out on the first run at or after that time, so run ``kgl pull``
  often enough (for example, from a timer every 15 minutes)
* If korgalore didn't run for a few days, you get one digest that covers
  all of that time, not one digest per day
* The first digest of a new delivery is sent on the next run and covers
  one period back: the last 24 hours, or the last 7 days for a weekly
  digest
* A big digest is split into several emails, so that mail clients
  don't cut it off. The subjects are numbered like a patch series
  (``[DIGEST 1/3]``, ``[DIGEST 2/3]``, ...), parts 2 and up are replies
  to part 1, and every part shows the totals for the whole digest. A
  thread is never split between two parts
* If the target doesn't accept the digest (or one of its parts), nothing
  is marked as sent. The next run sends the parts that were not
  delivered yet, before anything else, so you never get a part twice
* Messages from addresses in your bozofilter are left out
* Lore feeds normally keep about one week of history on your disk. When
  a feed has a digest that was last sent longer ago than that (for
  example, a weekly digest, or after a vacation), korgalore keeps the
  history back to that digest, up to 30 days. If messages are missing
  anyway (after more than 30 days away), the digest starts with a note
  that says which messages are missing and links to the archive where
  you can find them
* Lei feeds link to ``https://lore.kernel.org/all/``, because lei results
  can come from any list

Summarized Digests
~~~~~~~~~~~~~~~~~~

To set up summaries step by step, see :doc:`digests`.

A digest can also have a short summary of each thread, written by a
language model. You choose the model: a local one (with Ollama,
llama.cpp, vLLM or LM Studio) or a hosted one. To try it, add a
``[summarizers]`` entry and point the digest at it:

.. code-block:: toml

   [deliveries.lkml-digest]
   feed = 'lkml'
   target = 'personal'
   mode = 'digest'
   summarizer = 'local'
   max_summaries = 50    # optional
   summary_instructions = 'Tell me if anyone sounds confused or upset.'    # optional

   [summarizers.local]
   type = 'openai'
   url = 'http://localhost:11434/v1'
   model = 'qwen3:32b'

Before the first real digest, run ``kgl digest --estimate`` to see how
many threads would be summarized and how much text would be sent. It
doesn't call the model and doesn't send anything.

Digest parameters for summaries:

* ``summarizer``: The name of a ``[summarizers]`` entry. Without it, the
  digest has no summaries
* ``max_summaries``: (Optional) The most new summaries in one digest.
  Summaries that korgalore made before don't count. The busiest threads
  (the most new messages) get their summaries first. The other threads
  are still in the digest, with a note that the limit was reached
* ``summary_instructions``: (Optional) More instructions for the model,
  added at the end of korgalore's own. Use it to ask for something you
  want to know about each thread, for example "Tell me if anyone sounds
  confused or upset." korgalore's own rules still come first, and the
  facts in the digest never come from the model. Saved summaries are
  only used again with the same text, so after a change, the next
  digest writes a new summary for each thread that needs one

Summarizer parameters, for every type:

* ``type``: ``'openai'`` or ``'command'`` (see below)
* ``max_input_chars``: (Optional) The most characters sent for one
  thread (default 24000, at least 1000). See "Long threads" below
* ``timeout``: (Optional) Seconds to wait for one summary (default 120)
* ``allow_private_feeds``: (Optional) Allow this summarizer for lei feeds
  (default ``false``). See "Privacy" below

``type = 'openai'`` works with any server that has an OpenAI-compatible
chat API. This includes Ollama, llama.cpp, vLLM, LM Studio and most
hosted services:

* ``url``: The API base URL, such as ``'http://localhost:11434/v1'``
* ``model``: The model name, as the server knows it
* ``api_key_file``: (Optional) A file with the API key, for servers that
  need one

``type = 'command'`` runs a program for each thread. The program gets
the instructions and the thread on stdin, and writes the summary to
stdout. This works with tools like ``llm`` or ``claude -p``:

* ``command``: The command to run, such as ``'llm -m mistral'``. It is
  split like a shell command line, and ``$VARS`` and a leading ``~`` are
  expanded, but no shell runs it
* ``model``: (Optional) The model name to record in the digest headers
  and the summary cache. Without it, the program name is used, such as
  ``llm``. The full command line is never recorded, since it may hold
  an API key

.. code-block:: toml

   [summarizers.claude]
   type = 'command'
   command = 'claude -p --model sonnet'

What goes into a summary:

* Summaries are marked "machine-generated", and the model name is in the
  ``X-Korgalore-Digest-Model`` header of the digest email
* The facts in a digest (message and patch counts, versions, review
  trailers) never come from the model, so a wrong summary cannot fake a
  ``Reviewed-by``
* The model does not see the review trailers in patches and cover
  letters. A patch often carries them from earlier versions of the
  series, and the model would report those old reviews as new
* A new thread that nobody answered gets no summary: its facts already
  say everything. It is still listed. A new patch series is different:
  it gets a summary of what it changes and why, even before anybody
  answers
* Before a thread is sent, quoted text is removed and each patch is
  replaced with a short note, such as
  ``[diff: 42 lines; 2 files: mm/a.c, mm/b.c]``. Models with a reasoning
  step work too: only the answer goes into the digest
* **Long threads:** a thread longer than ``max_input_chars`` keeps its
  first message and its newest ones, and the model is told how many
  messages in the middle were left out
* Each summary is saved in ``summaries/`` in the data directory for 30
  days. A thread that gets new replies sends only the new messages plus
  its earlier summary, so long threads stay cheap. A different model
  starts again from the messages
* If the model fails (it's down, it times out, or it gives an error),
  the digest is still sent. The thread says "Summary unavailable." and
  keeps all its facts. After 3 failures in a row, korgalore stops trying
  until the next run

.. tip::
   Ollama has a small context window by default, and it cuts longer
   prompts without an error. The model then sees only part of the
   thread. korgalore warns you when it notices this. Set
   ``OLLAMA_CONTEXT_LENGTH`` to at least 8192 for the Ollama server, or
   lower ``max_input_chars``.

The Digest Worker
^^^^^^^^^^^^^^^^^

A local model can take a long time to summarize a busy day. So
``kgl pull`` doesn't wait for it: it only collects the messages of a
summarized digest, and then starts the **digest worker** in the
background. The worker summarizes the threads and sends the digest.
Meanwhile, ``kgl pull`` keeps running as usual. Only one worker runs at
a time. A worker that korgalore starts writes its log to
``digest-worker.log`` in the data directory. A worker that you start
yourself with ``kgl digest --work`` logs like any other ``kgl`` command.

You choose how the worker starts in the ``[digests]`` section:

.. code-block:: toml

   [digests]
   worker = 'spawn'    # or 'external'

* ``worker``: ``'spawn'`` (the default) starts the worker from
  ``kgl pull`` and the GUI. ``'external'`` never starts it: you run
  ``kgl digest --work`` yourself, for example from its own timer

.. warning::
   If you run ``kgl pull`` from a systemd service with
   ``Type=oneshot``, systemd stops the worker as soon as ``kgl pull``
   exits. This is because of the default ``KillMode=control-group``.
   Use ``worker = 'external'`` and run ``kgl digest --work`` from its
   own service (see "Automated Pulls" in the usage docs). A stopped
   worker doesn't lose its work: the next one starts from the saved
   summaries.

Privacy
^^^^^^^

Lore lists are public, so their messages can go to any model. A lei
feed is different: it can include your **private mail**. So korgalore
refuses to use a lei feed with a summarizer that is not on your
machine, unless that summarizer sets ``allow_private_feeds = true``.
An ``openai`` summarizer is on your machine when its URL points to
``localhost`` (or ``127.0.0.1``). A ``command`` summarizer could send
the text anywhere, so korgalore never treats it as local.

For any feed, korgalore also logs a warning when it sends threads to a
summarizer that is not on your machine.

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
* Digests on their way out, saved summaries (``summaries/``) and the
  log of a digest worker that korgalore started (``digest-worker.log``)

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
