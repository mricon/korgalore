"""Service for delivering messages to IMAP mail servers."""

import logging
import imaplib
from pathlib import Path
from typing import Any, List, Optional, Tuple, cast
from korgalore import ConfigurationError, RemoteError

logger = logging.getLogger('korgalore')


class ImapTarget:
    """Target for delivering messages to IMAP mail servers."""

    DEFAULT_LABELS: List[str] = []

    def __init__(self, identifier: str, server: str, username: str,
                 folder: str = 'INBOX',
                 password: Optional[str] = None,
                 password_file: Optional[str] = None,
                 timeout: int = 60) -> None:
        """Initialize IMAP service.

        Args:
            identifier: Target identifier for logging
            server: IMAP server hostname (e.g., 'imap.example.com')
            username: Account username/email
            folder: Target folder for message delivery (default: 'INBOX')
            password: Password (if provided directly)
            password_file: Path to file containing password
            timeout: Connection timeout in seconds (default: 60)

        Raises:
            ConfigurationError: If configuration is invalid
            RemoteError: If server connection or authentication fails
        """
        self.identifier = identifier
        self.server = server
        self.username = username
        self.folder = folder
        self.imap: Optional[imaplib.IMAP4_SSL] = None

        # Validate required configuration
        if not server:
            raise ConfigurationError(
                f"No server specified for IMAP target: {identifier}"
            )

        if not username:
            raise ConfigurationError(
                f"No username specified for IMAP target: {identifier}"
            )

        # Load password from file or use provided password
        if password:
            self.password = password
        elif password_file:
            password_path = Path(password_file).expanduser()
            if not password_path.exists():
                raise ConfigurationError(
                    f"Password file not found: {password_file}"
                )
            with open(password_path, 'r') as f:
                self.password = f.read().strip()
        else:
            raise ConfigurationError(
                f"No password or password_file specified for IMAP target: {identifier}"
            )

        # Connection timeout
        self.timeout = timeout

    def connect(self) -> None:
        """Establish connection to the IMAP server and verify folder exists.

        Creates an SSL connection, authenticates with the server, and verifies
        the target folder exists.

        Raises:
            RemoteError: If authentication fails.
            ConfigurationError: If the target folder does not exist.
        """
        if self.imap is None:
            # Connect with SSL on port 993
            self.imap = imaplib.IMAP4_SSL(self.server, timeout=self.timeout)

            # Authenticate
            try:
                self.imap.login(self.username, self.password)
            except imaplib.IMAP4.error as e:
                raise RemoteError(
                    f"IMAP authentication failed for {self.server}: {e}"
                ) from e
            # Verify folder exists (don't auto-create)
            try:
                status, _ = self.imap.select(self.folder, readonly=True)
                if status != 'OK':
                    raise ConfigurationError(
                        f"Folder '{self.folder}' does not exist on IMAP server {self.server}"
                    )
            except imaplib.IMAP4.error as e:
                raise ConfigurationError(
                    f"Folder '{self.folder}' does not exist on IMAP server {self.server}: {e}"
                ) from e

            logger.debug('IMAP service initialized: server=%s, folder=%s',
                        self.server, self.folder)

    def import_message(self, raw_message: bytes, labels: List[str]) -> Any:
        """Import raw email message to IMAP server.

        Args:
            raw_message: Raw email bytes (RFC 2822/5322 format)
            labels: Ignored for IMAP (single folder only)

        Returns:
            IMAP response from APPEND command

        Raises:
            RemoteError: On delivery errors
        """
        imap = self.imap
        if imap is None:
            self.connect()
            imap = self.imap
            if imap is None:
                raise RemoteError("IMAP connection not established.")

        try:
            # Normalize line endings to CRLF as required by RFC 2822/5322
            # Git stores messages with Unix LF endings, but IMAP requires CRLF
            normalized_message = raw_message.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')

            # Append message to folder
            # flags: empty string = no flags set (message will be unread)
            # date_time: empty string = use current time (imaplib doesn't accept None)
            try:
                # imaplib type stubs are incomplete - append returns (str, List[Any])
                typ, data = cast(
                    Tuple[str, List[Any]],
                    imap.append(
                        self.folder,
                        '',  # No flags (empty string)
                        '',  # Use current time (empty string for default)
                        normalized_message
                    )
                )

                if typ != 'OK':
                    raise RemoteError(
                        f"IMAP APPEND failed with status: {typ}, response: {data}"
                    )

                logger.debug('Delivered message to IMAP folder %s: %s',
                           self.folder, data)

            except imaplib.IMAP4.error as e:
                raise RemoteError(
                    f"Failed to append message to folder '{self.folder}': {e}"
                ) from e

            return data

        except (OSError, imaplib.IMAP4.error) as e:
            if isinstance(e, RemoteError):
                raise
            raise RemoteError(
                f"IMAP delivery failed: {e}"
            ) from e
