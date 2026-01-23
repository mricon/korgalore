=====
Usage
=====

Command Line Interface
======================

Korgalore provides the ``kgl`` command-line tool for managing mailing list imports.

Global Options
--------------

These options can be used with any command:

.. code-block:: bash

   kgl [OPTIONS] COMMAND [ARGS]...

Options:

* ``--version``: Show the version and exit
* ``-v, --verbosity``: Set logging verbosity (use multiple times for more detail)
* ``-c, --cfgfile PATH``: Path to configuration file (default: ``~/.config/korgalore/korgalore.toml``)
* ``-l, --logfile PATH``: Path to log file (optional)

Commands
========

auth
----

Authenticate with targets that require authentication (Gmail, JMAP, IMAP).

.. code-block:: bash

   kgl auth [TARGET]

Arguments:

* ``TARGET``: (Optional) Name of a specific target to authenticate. If not provided, all targets requiring authentication will be authenticated.

Examples:

.. code-block:: bash

   # Authenticate all targets that require authentication
   kgl auth

   # Authenticate only the 'fastmail' target
   kgl auth fastmail

   # Authenticate only the 'personal' Gmail target
   kgl auth personal

This command will:

1. Read your configuration file
2. If TARGET is specified, authenticate only that target
3. If TARGET is omitted, authenticate all targets that require authentication
4. For Gmail: open a browser for OAuth authentication if needed
5. For JMAP: verify the API token is valid
6. Skip targets that don't require authentication (e.g., maildir)

.. note::
   Maildir targets don't require authentication. JMAP and IMAP targets authenticate
   automatically using the configured token or password.

   For Gmail targets, this will open a browser for OAuth authentication.
   For JMAP targets, this verifies the API token is valid.
   For IMAP targets, this verifies the password and folder exist.

bozofilter
----------

Manage the bozofilter for blocking messages from unwanted senders.

The bozofilter is a simple text file containing email addresses to block.
Messages from these addresses are silently skipped during delivery (they
are marked as delivered but not actually imported).

.. code-block:: bash

   kgl bozofilter [OPTIONS]

Options:

* ``-a, --add TEXT``: Add address(es) to the bozofilter (comma-separated)
* ``-r, --reason TEXT``: Reason for adding (included as comment in the file)
* ``-e, --edit``: Open the bozofilter file in ``$EDITOR``
* ``-l, --list``: List all addresses in the bozofilter

Examples:

.. code-block:: bash

   # Add a single address
   kgl bozofilter --add spammer@example.com

   # Add multiple addresses with a reason
   kgl bozofilter --add 'bot@example.com,noise@example.org' --reason 'automated noise'

   # Edit the bozofilter in your editor
   kgl bozofilter --edit

   # List all blocked addresses
   kgl bozofilter --list

File Format
~~~~~~~~~~~

The bozofilter is stored at ``~/.config/korgalore/bozofilter.txt``. The format
is simple:

* One email address per line
* Lines starting with ``#`` are comments
* Trailing comments after ``#`` are supported
* Addresses are case-insensitive

Example file:

.. code-block:: text

   # Korgalore bozofilter - one email address per line
   spammer@example.com # added on 2026-01-15, sends junk
   bot@example.org # added on 2026-01-15, automated noise

.. tip::
   When using the GUI, you can edit the bozofilter via the "Edit Bozofilter..."
   menu option, which opens it in your system's default text editor.

edit-config
-----------

Open the configuration file in your default editor.

.. code-block:: bash

   kgl edit-config

This command will:

1. Locate your configuration file (default: ``~/.config/korgalore/korgalore.toml``)
2. If the file doesn't exist, create it with example configuration
3. Open it in your default editor (as specified by ``$EDITOR`` or ``$VISUAL``)
4. Validate the TOML syntax after you close the editor

You can also specify a custom config file path:

.. code-block:: bash

   kgl -c /path/to/config.toml edit-config

This is a convenient way to edit your configuration without having to remember
the file path or manually create the directory structure.

.. tip::
   The command uses your system's default editor. You can set it by exporting
   the ``EDITOR`` environment variable in your shell configuration:

   .. code-block:: bash

      export EDITOR=vim
      # or
      export EDITOR=nano
      # or
      export EDITOR=code  # for VS Code

labels
------

List labels/folders for a Gmail, JMAP, or IMAP target.

.. code-block:: bash

   kgl labels TARGET [OPTIONS]

Arguments:

* ``TARGET``: Name of the target (as defined in your configuration file)

Options:

* ``-i, --ids``: Include label/folder IDs in the output (developer use, mostly)

Examples:

.. code-block:: bash

   # List Gmail labels
   kgl labels personal

   # List JMAP mailboxes
   kgl labels fastmail

.. note::
   This command only works with targets that support folders/labels.
   Maildir targets don't support labels, and IMAP targets deliver to a single folder only.

This is useful for:

* Checking which labels/folders exist before configuring deliveries
* Finding the exact label/folder names to use in your configuration
* Verifying that your target authentication is working

pull
----

Pull messages from configured deliveries and import them into configured targets
(Gmail, maildir, etc.).

.. code-block:: bash

   kgl pull [OPTIONS] [DELIVERY_NAME]

Arguments:

* ``DELIVERY_NAME``: (Optional) Name of a specific delivery to pull. If not provided, all configured deliveries will be processed.

Options:

* ``-m, --max-mail INTEGER``: Maximum number of messages to pull (0 for all, default: 0)
* ``-n, --no-update``: Skip feed updates (useful with ``--force`` to reprocess existing commits)
* ``-f, --force``: Run deliveries even if feeds have no apparent updates

Examples:

.. code-block:: bash

   # Pull all messages from all configured deliveries
   kgl pull

   # Pull messages from a specific delivery
   kgl pull lkml

   # Pull only the last 50 messages from each delivery
   kgl pull -m 50

   # Pull only the last 10 messages from a specific delivery
   kgl pull -m 10 lkml

   # Force re-run deliveries without updating feeds first
   kgl pull --no-update --force

   # Force re-run a specific delivery
   kgl pull -n -f lkml

How Pull Works
~~~~~~~~~~~~~~

For lore.kernel.org deliveries:

1. Check for new epochs (git repositories)
2. Pull latest commits from the highest epoch
3. Extract email messages from commits
4. Import messages into configured targets (Gmail with labels, JMAP folders, IMAP folder, maildir, etc.)
5. Update tracking information

For lei deliveries:

1. Run ``lei up`` to update the search
2. Check for new commits in the lei repository
3. Extract and import new messages into configured targets

For tracked threads (see ``track`` command):

1. Update all active tracked threads via ``lei up``
2. Deliver any new messages to their configured targets
3. Update tracking activity timestamps

yank
----

Fetch and upload a single message or entire thread from lore.kernel.org.

.. code-block:: bash

   kgl yank [OPTIONS] MSGID_OR_URL

Arguments:

* ``MSGID_OR_URL``: Either a message-id (e.g., ``some@msgid.com``) or a lore.kernel.org URL

Options:

* ``-t, --target TEXT``: Target to upload the message to (required if multiple targets configured)
* ``-l, --labels TEXT``: Labels to apply (repeatable or comma-separated)
* ``-T, --thread``: Fetch and upload the entire thread instead of just a single message

.. note::
   If only one target is configured, the ``-t`` option is not required and that
   target will be used automatically. If no labels are specified, target-specific
   defaults are used (e.g., ``INBOX, UNREAD`` for Gmail, ``INBOX`` for JMAP).

Examples:

.. code-block:: bash

   # Upload a single message by message-id
   kgl yank --target personal some@msgid.com

   # Upload a single message by URL
   kgl yank --target work https://lore.kernel.org/lkml/msgid@example.com/

   # Upload with specific labels (comma-separated or repeated)
   kgl yank --target personal --labels INBOX,UNREAD some@msgid.com

   # Upload an entire thread
   kgl yank --target personal --thread some@msgid.com

   # Upload an entire thread with labels (short form)
   kgl yank -t work -T -l Lists/LKML https://lore.kernel.org/lkml/msgid@example.com/

track
-----

Track email threads for ongoing updates without subscribing to entire mailing lists.

This command allows you to follow specific threads of interest from lore.kernel.org.
Unlike ``yank`` (which is a one-time fetch), tracked threads are automatically
updated during regular ``pull`` operations. This is useful when you want to follow
a discussion or patch series without subscribing to the full mailing list traffic.

The track command uses ``lei`` (local email interface from public-inbox) to create
persistent searches that monitor threads for new messages.

Subcommands
~~~~~~~~~~~

**track add** - Start tracking a thread:

.. code-block:: bash

   kgl track add [OPTIONS] MSGID_OR_URL

Options:

* ``-t, --target TEXT``: Target for deliveries (required if multiple targets configured)
* ``-l, --labels TEXT``: Labels to apply (repeatable or comma-separated)

**track list** - List tracked threads:

.. code-block:: bash

   kgl track list [OPTIONS]

Options:

* ``-i, --inactive``: Show only inactive or paused threads

**track stop** - Stop tracking a thread:

.. code-block:: bash

   kgl track stop [OPTIONS] TRACK_ID

Options:

* ``--delete``: Also delete the lei search data (default: keep data)

**track pause** - Temporarily pause tracking:

.. code-block:: bash

   kgl track pause TRACK_ID

**track resume** - Resume a paused or expired thread:

.. code-block:: bash

   kgl track resume TRACK_ID

Examples
~~~~~~~~

.. code-block:: bash

   # Start tracking a thread by message-id
   kgl track add '<20251217-feature-v3-0-abc123@kernel.org>'

   # Start tracking a thread by lore URL
   kgl track add https://lore.kernel.org/lkml/20251217-feature-v3-0-abc123@kernel.org/

   # Track with specific labels (comma-separated)
   kgl track add -l tracked,patches '<msgid@example.org>'

   # List all tracked threads
   kgl track list

   # List only inactive/paused threads
   kgl track list --inactive

   # Pause tracking temporarily
   kgl track pause track-a1b2c3

   # Resume a paused thread
   kgl track resume track-a1b2c3

   # Stop tracking (keeps data for reference)
   kgl track stop track-a1b2c3

   # Stop tracking and delete all data
   kgl track stop --delete track-a1b2c3

How Thread Tracking Works
~~~~~~~~~~~~~~~~~~~~~~~~~

1. When you run ``track add``, korgalore:

   * Creates a lei search for the thread using ``lei q "mid:<msgid>" --threads``
   * Populates the search with current thread messages
   * Delivers all existing messages to your target
   * Saves tracking metadata in ``~/.local/share/korgalore/tracking.json``

2. During ``pull``, tracked threads are:

   * Updated via ``lei up`` to fetch new messages
   * Processed alongside regular deliveries
   * Subject to the same retry mechanism for failed deliveries

3. Threads are automatically marked inactive after 30 days of no new messages.
   Inactive threads are skipped during ``pull`` but can be resumed if needed.

4. When you ``stop`` tracking:

   * By default, the lei search data is preserved (you can clean it up manually)
   * Use ``--delete`` to remove all data
   * The command shows how to clean up with ``lei forget-search`` if data is kept

.. note::
   Thread tracking requires ``lei`` from the public-inbox project to be installed
   and configured. See https://public-inbox.org/lei for installation instructions.


track-subsystem
---------------

Track a Linux kernel subsystem by parsing the MAINTAINERS file and creating
lei queries for mailing list traffic and patches.

This is useful for kernel developers who want to follow a subsystem's mailing
list and patches without manually configuring lei queries.

.. code-block:: bash

   kgl track-subsystem [OPTIONS] SUBSYSTEM_NAME

Arguments:

* ``SUBSYSTEM_NAME``: Name of the subsystem (or substring) from the MAINTAINERS file

Options:

* ``-m, --maintainers PATH``: Path to MAINTAINERS file (optional, see below)
* ``-t, --target TEXT``: Target for deliveries (auto-selected if only one target configured)
* ``-l, --labels TEXT``: Labels to apply (repeatable or comma-separated; defaults to target's default labels)
* ``--since TEXT``: Start date for query (default: ``7.days.ago``)
* ``--threads / --no-threads``: Include entire threads when any message matches (default: off)
* ``--forget``: Remove tracking for the subsystem (deletes config and lei queries)

MAINTAINERS File Location
~~~~~~~~~~~~~~~~~~~~~~~~~

The command looks for the MAINTAINERS file in this order:

1. Path specified with ``-m/--maintainers``
2. ``./MAINTAINERS`` in the current directory
3. Fetched from kernel.org (cached for 24 hours in ``~/.local/share/korgalore/``)

This means you can run ``kgl track-subsystem`` from a kernel source tree without
any extra arguments, or from anywhere and let it fetch the file automatically.

Examples
~~~~~~~~

.. code-block:: bash

   # Track from a kernel source tree (uses ./MAINTAINERS)
   cd ~/linux && kgl track-subsystem 'DRM'

   # Track from anywhere (fetches MAINTAINERS from kernel.org)
   kgl track-subsystem 'BTRFS'

   # Track with explicit MAINTAINERS path
   kgl track-subsystem -m ~/linux/MAINTAINERS '9P FILE SYSTEM'

   # Track using a substring match (case-insensitive)
   kgl track-subsystem '9p file'

   # Track with specific target and labels (comma-separated)
   kgl track-subsystem -t work -l INBOX,patches 'DRM'

   # Track with --threads to get full discussions (can produce many results)
   kgl track-subsystem --threads 'RUST'

   # Track patches from the last 30 days (default is 7)
   kgl track-subsystem --since 30.days.ago 'BTRFS'

   # Stop tracking a subsystem (removes config and lei queries)
   kgl track-subsystem --forget '9P FILE SYSTEM'

How It Works
~~~~~~~~~~~~

When you run ``track-subsystem``, korgalore:

1. Parses the MAINTAINERS file to find the matching subsystem entry
2. Creates two lei queries based on the subsystem's metadata:

   * **{name}-mailinglist**: Messages to the subsystem's mailing list(s) (from ``L:`` entries)
   * **{name}-patches**: Patches touching subsystem files (from ``F:``, ``X:``, ``N:``, ``K:`` entries)

3. Initializes the lei searches and feed state
4. Writes a configuration file to ``~/.config/korgalore/conf.d/{subsystem_key}.toml``

The configuration is stored in the ``conf.d/`` directory, which is automatically
loaded by korgalore alongside the main configuration file. This keeps subsystem
tracking separate from your main configuration.

MAINTAINERS File Fields Used
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The command uses these fields from the MAINTAINERS file:

* ``L:`` - Mailing list addresses (for the mailinglist query)
* ``F:`` - File patterns (for the patches query, using ``dfn:`` prefix)
* ``X:`` - Excluded file patterns (for the patches query, using ``NOT dfn:``)
* ``N:`` - File regex patterns (simple patterns only, converted to ``dfn:``)
* ``K:`` - Content regex patterns (simple patterns only, converted to ``dfb:``)

.. note::
   Complex regex patterns in ``N:`` and ``K:`` fields are skipped with a warning,
   as Xapian (used by lei) doesn't support regex queries. Only simple whole-word
   patterns can be converted.

Catch-all Mailing Lists
~~~~~~~~~~~~~~~~~~~~~~~

Many MAINTAINERS entries include high-volume catch-all lists like
``linux-kernel@vger.kernel.org`` that receive copies of most kernel patches.
Including these in subsystem queries would flood results with irrelevant messages.

By default, korgalore excludes these lists from mailinglist queries:

* ``linux-kernel@vger.kernel.org``
* ``patches@lists.linux.dev``

You can customize this via the ``main.catchall_lists`` configuration option.
See :doc:`configuration` for details.

Forgetting a Subsystem
~~~~~~~~~~~~~~~~~~~~~~

To stop tracking a subsystem and clean up all related data:

.. code-block:: bash

   kgl track-subsystem --forget 'SUBSYSTEM NAME'

This will:

1. Remove the configuration file from ``conf.d/``
2. Run ``lei forget-search`` on each lei query to remove the search data

.. note::
   Subsystem tracking requires ``lei`` from the public-inbox project to be installed
   and configured. See https://public-inbox.org/lei for installation instructions.


gui
---

Launch a GNOME taskbar status indicator application for background syncing.

.. code-block:: bash

   kgl gui

The GUI provides:

* **System tray icon** with status indication (idle/syncing/error)
* **Automatic background sync** at configurable intervals (default: 5 minutes)
* **Menu options**:

  * Sync Now - trigger an immediate sync
  * Yank - fetch a message or thread by message-id or lore.kernel.org URL
  * Authenticate - re-authenticate Gmail targets when tokens expire (appears only when needed)
  * Edit Config - open the configuration file in your preferred editor
  * Edit Bozofilter - open the bozofilter file to block unwanted senders
  * Quit - exit the application

Features
~~~~~~~~

**Gmail Re-authentication**

When a Gmail token expires or is revoked, the GUI detects this and shows an
"Authenticate..." menu item. Clicking it opens a browser for OAuth re-authentication.
After successful authentication, sync runs automatically.

**Configuration Editing**

The "Edit Config..." menu item opens your configuration file using ``xdg-open``.
After you close the editor, the file is validated for TOML syntax errors. If valid,
the new configuration is loaded immediately without restarting the GUI.

**Yank Dialog**

The "Yank..." menu item opens a dialog for fetching messages without using the
terminal. Enter a message-id (e.g., ``<msgid@example.com>``) or a lore.kernel.org
URL, optionally select a target (if multiple are configured), and check
"Yank entire thread" to fetch all messages in the thread. The status indicator
shows progress and results.

**Status Display**

The tray icon and menu show current status:

* **Idle** - waiting for next sync
* **Idle (N new)** - last sync delivered N unique messages
* **Syncing...** - sync in progress with current feed/delivery shown
* **Auth required: target** - Gmail authentication needed
* **Error: See logs** - sync failed, check logs for details

**Desktop Integration**

To launch the GUI from your application menu instead of the terminal, install
the desktop file (see :doc:`installation`).

Running as a Background Service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The GUI can be started automatically at login. On GNOME, add it to your startup
applications, or create a systemd user service:

Create ``~/.config/systemd/user/korgalore-gui.service``:

.. code-block:: ini

   [Unit]
   Description=Korgalore GUI
   After=graphical-session.target

   [Service]
   Type=simple
   ExecStart=%h/.local/bin/kgl gui

   [Install]
   WantedBy=default.target

Enable and start:

.. code-block:: bash

   systemctl --user enable korgalore-gui.service
   systemctl --user start korgalore-gui.service


Common Usage Patterns
=====================

Initial Setup
-------------

.. code-block:: bash

   # 1. Create configuration file
   vim ~/.config/korgalore/korgalore.toml

   # 2. Authenticate with Gmail
   kgl -v DEBUG auth personal

   # 3. Verify labels exist
   kgl labels personal

   # 4. Test with limited messages
   kgl -v DEBUG pull

   # 5. Check Gmail to verify import worked

Regular Use
-----------

.. code-block:: bash

   # Pull all new messages (includes tracked threads)
   kgl pull

   # Pull with logging for troubleshooting
   kgl -v DEBUG pull

   # Pull a specific list
   kgl pull lkml

   # Yank a specific message you're interested in
   kgl yank --labels INBOX some@msgid.com

   # Yank an entire thread
   kgl yank --thread --labels INBOX some@msgid.com

   # Start tracking a thread you want to follow
   kgl track add https://lore.kernel.org/lkml/msgid@example.com/

   # List threads you're tracking
   kgl track list

   # Stop tracking when you're done following
   kgl track stop track-abc123

Automated Pulls
---------------

You can set up automated pulls using a screen session or a systemd
timer. For a basic screen session:

.. code-block:: bash

   $ while true; do kgl pull; echo '---sleeping---'; sleep 600; done


Systemd Timer Example
~~~~~~~~~~~~~~~~~~~~~~

Create ``~/.config/systemd/user/korgalore.service``:

.. code-block:: ini

   [Unit]
   Description=Korgalore mailing list import
   After=network-online.target

   [Service]
   Type=oneshot
   ExecStart=%h/.local/bin/kgl -l %h/.share/korgalore/kgl.log -v CRITICAL pull

Create ``~/.config/systemd/user/korgalore.timer``:

.. code-block:: ini

   [Unit]
   Description=Run Korgalore every 10 minutes

   [Timer]
   OnBootSec=5min
   OnUnitActiveSec=10min

   [Install]
   WantedBy=timers.target

Enable and start the timer:

.. code-block:: bash

   systemctl --user enable korgalore.timer
   systemctl --user start korgalore.timer

