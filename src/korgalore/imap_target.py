"""Service for delivering messages to IMAP mail servers."""

import logging
import imaplib
from pathlib import Path
from typing import Any, List, Optional, Tuple, cast, TYPE_CHECKING

from korgalore import ConfigurationError, RemoteError
from korgalore.message import RawMessage

if TYPE_CHECKING:
    from korgalore.oauth2_imap import ImapOAuth2Authenticator

logger = logging.getLogger('korgalore')


class ImapTarget:
    """Target for delivering messages to IMAP mail servers."""

    DEFAULT_LABELS: List[str] = []

    def __init__(self, identifier: str, server: str, username: str,
                 folder: str = 'INBOX',
                 password: Optional[str] = None,
                 password_file: Optional[str] = None,
                 timeout: int = 60,
                 auth_type: str = 'password',
                 client_id: Optional[str] = None,
                 tenant: str = 'common',
                 token: Optional[str] = None,
                 interactive: bool = True) -> None:
        """Initialize IMAP service.

        Args:
            identifier: Target identifier for logging
            server: IMAP server hostname (e.g., 'imap.example.com')
            username: Account username/email
            folder: Target folder for message delivery (default: 'INBOX')
            password: Password (if provided directly, for auth_type='password')
            password_file: Path to file containing password (for auth_type='password')
            timeout: Connection timeout in seconds (default: 60)
            auth_type: Authentication type - 'password' or 'oauth2' (default: 'password')
            client_id: Azure AD application client ID (required for auth_type='oauth2')
            tenant: Azure AD tenant ID or 'common' (default: 'common')
            token: Path to OAuth2 token file (optional, auto-generated if not specified)
            interactive: If True, run OAuth flow interactively when needed.
                        If False, raise AuthenticationError instead (for GUI mode).

        Raises:
            ConfigurationError: If configuration is invalid
            RemoteError: If server connection or authentication fails
        """
        self.identifier = identifier
        self.server = server
        self.username = username
        self.folder = folder
        self.imap: Optional[imaplib.IMAP4_SSL] = None
        self.auth_type = auth_type
        self._interactive = interactive

        # Validate required configuration
        if not server:
            raise ConfigurationError(
                f"No server specified for IMAP target: {identifier}"
            )

        if not username:
            raise ConfigurationError(
                f"No username specified for IMAP target: {identifier}"
            )

        # Initialize authentication based on auth_type
        self._oauth2_authenticator: Optional["ImapOAuth2Authenticator"] = None
        self.password: Optional[str] = None

        if auth_type == 'oauth2':
            # OAuth2 authentication
            # Import here to avoid circular imports and optional dependency issues
            from korgalore.oauth2_imap import ImapOAuth2Authenticator, DEFAULT_CLIENT_ID

            # Use default client_id if not specified
            effective_client_id = client_id if client_id else DEFAULT_CLIENT_ID

            # Generate default token file path if not specified
            if not token:
                from korgalore.cli import get_xdg_config_dir
                config_dir = get_xdg_config_dir()
                token = str(config_dir / f'imap-{identifier}-oauth2-token.json')

            self._oauth2_authenticator = ImapOAuth2Authenticator(
                identifier=identifier,
                username=username,
                client_id=effective_client_id,
                token_file=token,
                tenant=tenant,
                interactive=interactive,
            )
        elif auth_type == 'password':
            # Password authentication (original behavior)
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
        else:
            raise ConfigurationError(
                f"Invalid auth_type '{auth_type}' for IMAP target: {identifier}. "
                "Must be 'password' or 'oauth2'."
            )

        # Connection timeout
        self.timeout = timeout

    @property
    def needs_auth(self) -> bool:
        """Check if this target needs authentication (for OAuth2 targets)."""
        if self._oauth2_authenticator is not None:
            return self._oauth2_authenticator.needs_auth
        return False

    def reauthenticate(self) -> None:
        """Perform OAuth2 re-authentication flow.

        Opens a browser window for the user to log in and authorize the app.
        Only applicable for OAuth2 auth_type targets.

        Raises:
            ConfigurationError: If target is not using OAuth2 authentication.
            AuthenticationError: If authentication fails.
        """
        if self._oauth2_authenticator is None:
            raise ConfigurationError(
                f"Target '{self.identifier}' is not configured for OAuth2 authentication."
            )

        self._oauth2_authenticator.reauthenticate()
        # Reset connection to force reconnect with new credentials
        self.imap = None

    def connect(self) -> None:
        """Establish connection to the IMAP server and verify folder exists.

        Creates an SSL connection, authenticates with the server, and verifies
        the target folder exists.

        Raises:
            RemoteError: If authentication fails.
            ConfigurationError: If the target folder does not exist.
            AuthenticationError: If OAuth2 authentication is required.
        """
        if self.imap is None:
            # Connect with SSL on port 993
            self.imap = imaplib.IMAP4_SSL(self.server, timeout=self.timeout)

            # Authenticate based on auth_type
            try:
                if self.auth_type == 'oauth2':
                    self._authenticate_oauth2()
                else:
                    if self.password is None:
                        raise RemoteError(
                            f"No password available for IMAP target: {self.identifier}"
                        )
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

            logger.debug('IMAP service initialized: server=%s, folder=%s, auth_type=%s',
                        self.server, self.folder, self.auth_type)

    def _authenticate_oauth2(self) -> None:
        """Authenticate using OAuth2 XOAUTH2 mechanism.

        Raises:
            RemoteError: If XOAUTH2 authentication fails.
            AuthenticationError: If OAuth2 token is invalid/expired.
        """
        if self._oauth2_authenticator is None or self.imap is None:
            raise RemoteError(
                f"OAuth2 authenticator not configured for IMAP target: {self.identifier}"
            )

        from korgalore.oauth2_imap import xoauth2_callback

        try:
            # Get the XOAUTH2 callback and authenticate
            callback = xoauth2_callback(self._oauth2_authenticator)
            self.imap.authenticate('XOAUTH2', callback)
            logger.debug('IMAP OAuth2 authentication successful for %s', self.identifier)
        except imaplib.IMAP4.error as e:
            error_str = str(e)
            # Check for authentication failure indicators
            if 'AUTHENTICATE' in error_str or 'authentication' in error_str.lower():
                raise RemoteError(
                    f"IMAP XOAUTH2 authentication failed for {self.server}: {e}"
                ) from e
            raise

    def _check_message_exists(self, message_id: str) -> bool:
        """Check if a message with this Message-ID exists in the target folder.

        Args:
            message_id: Message-ID header value (with angle brackets)

        Returns:
            True if message exists in the folder
        """
        imap = self.imap
        if imap is None:
            return False

        try:
            # Select the folder (read-only for search)
            status, _ = imap.select(self.folder, readonly=True)
            if status != 'OK':
                logger.debug('Failed to select folder %s for search', self.folder)
                return False

            # Search for messages with this Message-ID
            # The HEADER criterion searches for the specified header field
            status, data = imap.search(None, 'HEADER', 'Message-ID', message_id)
            if status != 'OK':
                logger.debug('IMAP SEARCH failed: %s', data)
                return False

            # data[0] is a space-separated list of message numbers
            message_nums = data[0].split() if data[0] else []
            if message_nums:
                logger.debug('Message-ID %s already exists in folder %s',
                            message_id, self.folder)
                return True

            return False
        except imaplib.IMAP4.error as e:
            # On error, log and proceed with import (fail-open)
            logger.debug('Failed to check for existing message: %s', e)
            return False

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

        msg = RawMessage(raw_message)

        # Check if message already exists in target folder
        if msg.message_id and self._check_message_exists(msg.message_id):
            logger.debug('Skipping import: message %s already in folder %s',
                        msg.message_id, self.folder)
            return {'skipped': True}

        try:
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
                        msg.as_bytes()
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

    def disconnect(self) -> None:
        """Close the IMAP connection.

        Safely closes the IMAP connection if one is open. Should be called
        after delivery is complete to avoid keeping connections open
        unnecessarily between sync runs.
        """
        if self.imap is not None:
            try:
                self.imap.logout()
                logger.debug('IMAP connection closed for %s', self.identifier)
            except (OSError, imaplib.IMAP4.error) as e:
                logger.debug('Error closing IMAP connection for %s: %s',
                            self.identifier, e)
            finally:
                self.imap = None
