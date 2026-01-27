"""Service for delivering messages to local maildir."""

import logging
import mailbox
from pathlib import Path
from typing import Any, List
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

        try:
            # Use Python's mailbox.Maildir - creates structure if needed
            self.maildir = mailbox.Maildir(str(self.maildir_path), create=True)
        except Exception as e:
            raise ConfigurationError(
                f"Failed to initialize maildir at {self.maildir_path}: {e}"
            ) from e

    def connect(self) -> None:
        """Connect to maildir (no-op for local maildir)."""
        logger.debug('Maildir target ready at %s', self.maildir_path)

    def import_message(self, raw_message: bytes, labels: List[str]) -> Any:
        """Import message to maildir.

        Args:
            raw_message: Raw email bytes
            labels: Ignored for maildir (Gmail-specific)

        Returns:
            Message key from maildir

        Raises:
            ConfigurationError: On delivery errors
        """
        try:
            msg = RawMessage(raw_message)
            # mailbox.Maildir.add() handles atomic delivery automatically
            # It writes to tmp/ and moves to new/
            key = self.maildir.add(msg.as_bytes())
            logger.debug('Delivered message to maildir with key: %s', key)
            return key
        except Exception as e:
            raise ConfigurationError(f"Failed to deliver to maildir: {e}") from e
