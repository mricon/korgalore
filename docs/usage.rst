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

Authenticate with Gmail targets defined in your configuration.

.. code-block:: bash

   kgl auth

This command will:

1. Read your configuration file
2. For each Gmail target, check for valid credentials
3. If credentials are missing or expired, open a browser for OAuth authentication
4. Save the authentication token for future use

.. note::
   You only need to run this once per target, unless you revoke access or the token expires.

edit-config
-----------

Open the configuration file in your default editor.

.. code-block:: bash

   kgl edit-config

This command will:

1. Locate your configuration file (default: ``~/.config/korgalore/korgalore.toml``)
2. If the file doesn't exist, create it with example configuration
3. Open it in your default editor (as specified by ``$EDITOR`` or ``$VISUAL``)

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

List Gmail labels for a specific target.

.. code-block:: bash

   kgl labels TARGET [OPTIONS]

Arguments:

* ``TARGET``: Name of the target (as defined in your configuration file)

Options:

* ``-i, --ids``: Include label IDs in the output (developer use, mostly)

Example:

.. code-block:: bash

   # List labels for the "personal" target
   kgl labels personal

This is useful for:

* Checking which labels exist before configuring deliveries
* Finding the exact label names to use in your configuration
* Verifying that your target authentication is working

pull
----

Pull messages from configured deliveries and import them into Gmail.

.. code-block:: bash

   kgl pull [OPTIONS] [LISTNAME]

Arguments:

* ``LISTNAME``: (Optional) Name of a specific list to pull. If not provided, all configured deliveries will be processed.

Options:

* ``-m, --max-mail INTEGER``: Maximum number of messages to pull (0 for all, default: 0)

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

How Pull Works
~~~~~~~~~~~~~~

For lore.kernel.org deliveries:

1. Check for new epochs (git repositories)
2. Pull latest commits from the highest epoch
3. Extract email messages from commits
4. Import messages into Gmail with configured labels
5. Update tracking information

For lei deliveries:

1. Run ``lei up`` to update the search
2. Check for new commits in the lei repository
3. Extract and import new messages

yank
----

Fetch and upload a single message or entire thread from lore.kernel.org.

.. code-block:: bash

   kgl yank [OPTIONS] MSGID_OR_URL

Arguments:

* ``MSGID_OR_URL``: Either a message-id (e.g., ``some@msgid.com``) or a lore.kernel.org URL

Options:

* ``-t, --target TEXT``: Target to upload the message to (required)
* ``-l, --labels TEXT``: Labels to apply to the message (can be used multiple times)
* ``-T, --thread``: Fetch and upload the entire thread instead of just a single message

Examples:

.. code-block:: bash

   # Upload a single message by message-id
   kgl yank --target personal some@msgid.com

   # Upload a single message by URL
   kgl yank --target work https://lore.kernel.org/lkml/msgid@example.com/

   # Upload with specific labels
   kgl yank --target personal --labels INBOX --labels UNREAD some@msgid.com

   # Upload an entire thread
   kgl yank --target personal --thread some@msgid.com

   # Upload an entire thread with labels (short form)
   kgl yank -t work -T -l Lists/LKML https://lore.kernel.org/lkml/msgid@example.com/


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

   # Pull all new messages
   kgl pull

   # Pull with logging for troubleshooting
   kgl -v DEBUG pull

   # Pull a specific list
   kgl pull lkml

   # Yank a specific message you're interested in
   kgl yank --target personal --labels INBOX some@msgid.com

   # Yank an entire thread
   kgl yank --target personal --thread --labels INBOX some@msgid.com

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

