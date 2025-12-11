==========
Quickstart
==========

This guide will help you get started with Korgalore quickly.

Step 1: Install Korgalore
==========================

See :doc:`installation` for detailed installation instructions.

Eventually, this will work, too:

.. code-block:: bash

   pip install korgalore

Step 2: Set Up Gmail API Credentials
=====================================

Gmail went out of their way to make it super difficult to access your
inbox via an API, so please be prepared to suffer a bit. You will need
to download an OAuth 2.0 Client ID file from Google that will authorize
your access.

The best approach is to follow the "quickstart app" instructions from
Google itself:
https://developers.google.com/workspace/gmail/api/quickstart/python#set-up-environment

When setting up:

1. Choose **"Internal Use"** for the OAuth consent screen
2. Choose **"Desktop Application"** for the application type

Eventually, you should have a "Download JSON" link. Use that to download
``client_secret_mumble-long-string.json``.

Rename it to ``credentials.json`` and place it in your config directory:

.. code-block:: bash

   mkdir -p ~/.config/korgalore
   mv ~/Downloads/client_secret_*.json ~/.config/korgalore/credentials.json

Step 3: Create a Configuration File
====================================

You can create a configuration file manually or use the built-in command:

.. code-block:: bash

   kgl edit-config

This will create a configuration file at ``~/.config/korgalore/korgalore.toml`` with
an example configuration and open it in your default editor.

Alternatively, create it manually with this content:

.. code-block:: toml

   [targets.personal]
   type = 'gmail'
   credentials = '~/.config/korgalore/credentials.json'

   [deliveries.lkml]
   feed = 'https://lore.kernel.org/lkml'
   target = 'personal'
   labels = ['LKML']

This minimal configuration:

* Sets up a Gmail target called "personal"
* Configures the Linux Kernel Mailing List as a delivery
* Applies the label "LKML" to imported messages

.. note::
   Make sure to create the Gmail label "LKML" in your Gmail account
   before running ``korgalore pull``.

Step 4: Authenticate with Gmail
================================

This must be done on a system with a running web browser, because Gmail
basically hates you. If you are setting up a headless node, you therefore
must run ``kgl auth`` on your local machine first, and then copy the
generated token file to the headless node.

.. code-block:: bash

   kgl auth personal

Follow the link that appears, authorize the application in your browser,
and allow access to your Gmail account.

Step 4.5: Move to a headless node
---------------------------------

Once you obtain the token file (usually located at
``~/.config/korgalore/gmail-personal-token.json``), copy it to your headless
node and modify your configuration file to point to it:

.. code-block:: toml

   [targets.personal]
   type = 'gmail'
   credentials = '~/.config/korgalore/credentials.json'
   token = '~/.config/korgalore/gmail-personal-token.json'

This will let you run korgalore on a headless node.

Step 5: Pull Messages
======================

Now you can pull messages from your configured deliveries:

.. code-block:: bash

   kgl pull

The first run is similar to "subscribing" to a mailing list -- it will
not yet import any email into your Gmail inbox. However, you only have
to wait a few minutes and then rerun ``kgl pull`` again:

.. code-block::

   $ kgl pull
   Uploading lkml  [####################################]  3/3
   Pull complete with updates:
     lkml: 3

Step 6: Check Your Gmail
=========================

Open Gmail and look for the label you configured (e.g., "LKML"). You
should see the imported messages there.

One-Off Message Import (Yank)
==============================

If you want to import a specific message or thread, you can use the ``yank``
command. This is useful if, for example, you want to respond to a message or
just want a copy of the thread for reading.

.. code-block:: bash

   # Import a single message by URL into the first defined target
   kgl yank https://lore.kernel.org/lkml/some-message-id@example.com/

   # Import an entire thread
   kgl yank --thread https://lore.kernel.org/lkml/some-message-id@example.com/

Unsubscribing
=============
Comment out the relevant ``[deliveries]`` section to stop pulling that
mailing list.

Next Steps
==========

* Read :doc:`configuration` to learn about advanced configuration options
* Read :doc:`usage` to learn about all available commands
* Set up additional mailing list deliveries
* Configure automatic pulls using cron or systemd timers

Troubleshooting
===============

For more help, see :doc:`usage` or contact tools@kernel.org.
