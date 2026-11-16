"""Service for delivering messages to local maildir."""

import logging
import mailbox
import os
from pathlib import Path
from typing import Any

from korgalore import ConfigurationError
from korgalore.message import RawMessage, use_header_date

logger = logging.getLogger('korgalore')


class MaildirTarget:
    """Service for delivering messages to a local maildir."""

    DEFAULT_LABELS: list[str] = []

    def __init__(self, identifier: str, maildir_path: str, message_date: str = 'header') -> None:
        """Initialize maildir service.

        Args:
            identifier: Target identifier for logging
            maildir_path: Path to maildir directory
            message_date: 'header' to set each file's mtime to the date the
                message arrived, or 'import' to leave it at delivery time

        Raises:
            ConfigurationError: If maildir cannot be accessed
        """
        self.identifier = identifier
        self.maildir_path = Path(maildir_path).expanduser()
        self.use_header_date = use_header_date(identifier, message_date)

        # Cache for subfolder maildirs
        self._subfolder_maildirs: dict[str, mailbox.Maildir] = {}

        try:
            # Ensure parent directories exist (mailbox.Maildir only creates
            # the maildir structure itself, not parent directories)
            self.maildir_path.parent.mkdir(parents=True, exist_ok=True)
            # Use Python's mailbox.Maildir - creates cur/new/tmp structure
            self.maildir = mailbox.Maildir(str(self.maildir_path), create=True)
        except Exception as e:
            raise ConfigurationError(f'Failed to initialize maildir at {self.maildir_path}: {e}') from e

    def _get_maildir(self, subfolder: str | None) -> mailbox.Maildir:
        """Get or create a maildir for the given subfolder.

        Args:
            subfolder: Optional subfolder path relative to base maildir

        Returns:
            The mailbox.Maildir instance for the target folder
        """
        if subfolder is None:
            return self.maildir

        # Check cache first
        if subfolder in self._subfolder_maildirs:
            return self._subfolder_maildirs[subfolder]

        # Compute path and create maildir
        subfolder_path = self.maildir_path / subfolder
        try:
            # Ensure parent directories exist
            subfolder_path.parent.mkdir(parents=True, exist_ok=True)
            subfolder_maildir = mailbox.Maildir(str(subfolder_path), create=True)
            self._subfolder_maildirs[subfolder] = subfolder_maildir
            logger.debug('Created subfolder maildir at %s', subfolder_path)
            return subfolder_maildir
        except Exception as e:
            raise ConfigurationError(f'Failed to create maildir at {subfolder_path}: {e}') from e

    def connect(self) -> None:
        """Connect to maildir (no-op for local maildir)."""
        logger.debug('Maildir target ready at %s', self.maildir_path)

    def import_message(
        self,
        raw_message: bytes,
        labels: list[str],
        feed_name: str | None = None,
        delivery_name: str | None = None,
        subfolder: str | None = None,
    ) -> Any:
        """Import message to maildir.

        Args:
            raw_message: Raw email bytes
            labels: Ignored for maildir (Gmail-specific)
            feed_name: Optional feed name for trace header
            delivery_name: Optional delivery name for trace header
            subfolder: Optional subfolder path relative to base maildir

        Returns:
            Message key from maildir

        Raises:
            ConfigurationError: On delivery errors
        """
        try:
            msg = RawMessage(raw_message)
            target_maildir = self._get_maildir(subfolder)
            # mailbox.Maildir.add() handles atomic delivery automatically
            # It writes to tmp/ and moves to new/
            # Maildir files use local Unix line endings, which mail readers
            # expect; CRLF is only for the wire
            key = target_maildir.add(msg.as_bytes(feed_name, delivery_name, crlf=False))
            when = msg.date if self.use_header_date else None
            if when is not None:
                # Mail readers such as mutt take a maildir message's received
                # time from the file's mtime
                folder = self.maildir_path if subfolder is None else self.maildir_path / subfolder
                path = folder / 'new' / key
                try:
                    os.utime(path, (path.stat().st_atime, when.timestamp()))
                except OSError as e:
                    # The message is delivered; failing now would make a
                    # retry deliver it again, which is worse than a wrong date
                    logger.warning('Could not set the date of maildir message %s: %s', key, e)
            logger.debug('Delivered message to maildir with key: %s', key)
            return key
        except Exception as e:
            raise ConfigurationError(f'Failed to deliver to maildir: {e}') from e
