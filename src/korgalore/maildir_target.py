"""Service for delivering messages to local maildir."""

import logging
import mailbox
from pathlib import Path
from typing import Any, Dict, List, Optional
from korgalore import ConfigurationError
from korgalore.message import RawMessage

logger = logging.getLogger('korgalore')


class MaildirTarget:
    """Service for delivering messages to a local maildir."""

    DEFAULT_LABELS: List[str] = []

    def __init__(self, identifier: str, maildir_path: str) -> None:
        """Initialize maildir service.

        Args:
            identifier: Target identifier for logging
            maildir_path: Path to maildir directory

        Raises:
            ConfigurationError: If maildir cannot be accessed
        """
        self.identifier = identifier
        self.maildir_path = Path(maildir_path).expanduser()

        # Cache for subfolder maildirs
        self._subfolder_maildirs: Dict[str, mailbox.Maildir] = {}

        try:
            # Ensure parent directories exist (mailbox.Maildir only creates
            # the maildir structure itself, not parent directories)
            self.maildir_path.parent.mkdir(parents=True, exist_ok=True)
            # Use Python's mailbox.Maildir - creates cur/new/tmp structure
            self.maildir = mailbox.Maildir(str(self.maildir_path), create=True)
        except Exception as e:
            raise ConfigurationError(
                f"Failed to initialize maildir at {self.maildir_path}: {e}"
            ) from e

    def _get_maildir(self, subfolder: Optional[str]) -> mailbox.Maildir:
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
            raise ConfigurationError(
                f"Failed to create maildir at {subfolder_path}: {e}"
            ) from e

    def connect(self) -> None:
        """Connect to maildir (no-op for local maildir)."""
        logger.debug('Maildir target ready at %s', self.maildir_path)

    def import_message(
        self,
        raw_message: bytes,
        labels: List[str],
        feed_name: Optional[str] = None,
        delivery_name: Optional[str] = None,
        subfolder: Optional[str] = None
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
            key = target_maildir.add(msg.as_bytes(feed_name, delivery_name))
            logger.debug('Delivered message to maildir with key: %s', key)
            return key
        except Exception as e:
            raise ConfigurationError(f"Failed to deliver to maildir: {e}") from e
