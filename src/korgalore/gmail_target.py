import logging
import os
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.external_account_authorized_user import Credentials as ExternalAccountCredentials
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
from googleapiclient.discovery import build  # type: ignore
from googleapiclient.errors import HttpError  # type: ignore

from korgalore import AuthenticationError, ConfigurationError, RemoteError, atomic_write
from korgalore.message import RawMessage

logger = logging.getLogger('korgalore')


# If modifying these scopes, delete the file token.json.
# We need scopes for reading and inserting new emails, but not
# modifying existing ones.
SCOPES = [
    'https://www.googleapis.com/auth/gmail.labels',
    'https://www.googleapis.com/auth/gmail.insert',
]

# InstalledAppFlow.run_local_server() returns either flavour depending on what
# the client_secrets file describes. Both expose the valid/expired/refresh_token
# attributes and the to_json() and refresh() methods used below, so the code
# does not care which one it gets -- but the declared type has to allow both.
GmailCredentials = Credentials | ExternalAccountCredentials


class GmailTarget:
    """Target class for delivering email messages to Gmail via the API."""

    DEFAULT_LABELS: list[str] = ['INBOX', 'UNREAD']

    def __init__(self, identifier: str, credentials_file: str, token_file: str, interactive: bool = True) -> None:
        """Initialize a GmailTarget instance.

        Args:
            identifier: Unique identifier for this Gmail target.
            credentials_file: Path to the Google OAuth credentials JSON file.
            token_file: Path to store/load the OAuth token.
            interactive: If True, run OAuth flow interactively when needed.
                        If False, raise AuthenticationError instead (for GUI mode).

        Raises:
            ConfigurationError: If credentials file is not found.
            AuthenticationError: If token is expired/revoked and re-auth is needed,
                               or if interactive=False and no token exists.
        """
        self.identifier = identifier
        self.creds: GmailCredentials | None = None
        self.service: Any | None = None
        self._label_map: dict[str, str] | None = None
        # Store expanded paths for potential re-authentication
        self._credentials_file = Path(os.path.expandvars(credentials_file)).expanduser()
        self._token_file = Path(os.path.expandvars(token_file)).expanduser()
        self._needs_auth = False
        self._interactive = interactive
        self._load_credentials()

    def _load_credentials(self) -> None:
        """Load or refresh OAuth credentials for Gmail API access.

        Attempts to load existing credentials from token_file. If not present
        or expired, initiates OAuth flow using credentials_file.

        Raises:
            ConfigurationError: If credentials file is not found.
            AuthenticationError: If token is expired/revoked and re-auth is needed.
        """
        # The file token.json stores the user's access and refresh tokens
        if self._token_file.exists():
            self.creds = Credentials.from_authorized_user_file(str(self._token_file), SCOPES)  # type: ignore

        # If there are no (valid) credentials available, let the user log in
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                try:
                    self.creds.refresh(Request())  # type: ignore
                except RefreshError as e:
                    logger.warning('Gmail token for %s has expired or been revoked.', self.identifier)
                    invalid_token_file = self._token_file.with_name(f'{self._token_file.name}.invalid')
                    invalid_token_file.unlink(missing_ok=True)
                    self._token_file.rename(invalid_token_file)
                    self._needs_auth = True
                    if not self._interactive:
                        # In non-interactive mode, just return - caller will check needs_auth
                        return
                    raise AuthenticationError(
                        f"Gmail token for '{self.identifier}' is invalid. Please re-authenticate.",
                        target_id=self.identifier,
                        target_type='gmail',
                    ) from e
            elif self._credentials_file.exists():
                if not self._interactive:
                    # In non-interactive mode (GUI), don't run OAuth flow
                    # Just mark as needing auth and return - caller will check needs_auth
                    self._needs_auth = True
                    return
                logger.critical('Log in to Gmail account for %s', self.identifier)

                flow = InstalledAppFlow.from_client_secrets_file(str(self._credentials_file), SCOPES)
                self.creds = flow.run_local_server(port=0)
            else:
                raise ConfigurationError(
                    f'{self._credentials_file} not found. Please download it from Google Cloud Console.'
                )

            atomic_write(self._token_file, self.creds.to_json())

        self._needs_auth = False

    def connect(self) -> None:
        """Establish connection to the Gmail API service.

        Creates the Gmail API service object if not already connected.
        """
        if self.service is None:
            logger.debug('Connecting to Gmail service for %s', self.identifier)
            self.service = build('gmail', 'v1', credentials=self.creds, cache_discovery=False)

    def _api(self) -> Any:
        """Return the connected Gmail API service, connecting first if needed.

        googleapiclient is untyped, so self.service can only be Any | None.
        Going through here keeps the None out of the call chains below without
        a suppression at every use site.
        """
        self.connect()
        return self.service

    def list_labels(self) -> list[dict[str, str]]:
        """List all labels in the user's mailbox.

        Returns:
            List of label objects
        """
        try:
            results = self._api().users().labels().list(userId='me').execute()
            labels: list[dict[str, str]] = results.get('labels', [])
            return labels

        except HttpError as error:
            raise RemoteError(f'An error occurred: {error}') from error

    def translate_labels(self, labels: list[str]) -> list[str]:
        """Translate label names to Gmail label IDs.

        Args:
            labels: List of label names to translate.

        Returns:
            List of corresponding Gmail label IDs.

        Raises:
            ConfigurationError: If any label is not found in Gmail.
        """
        # Translate label names to their corresponding IDs
        if self._label_map is None:
            # Get all labels from Gmail
            self._label_map = {label['name']: label['id'] for label in self.list_labels()}
        translated: list[str] = []
        for label in labels:
            label_id = self._label_map.get(label, None)
            if label_id is None:
                raise ConfigurationError(f"Label '{label}' not found in Gmail '{self.identifier}'.")
            translated.append(label_id)
        return translated

    def import_message(
        self,
        raw_message: bytes,
        labels: list[str],
        feed_name: str | None = None,
        delivery_name: str | None = None,
        subfolder: str | None = None,
    ) -> Any:
        """Import a raw email message into Gmail.

        Args:
            raw_message: The raw email message as bytes.
            labels: List of label names to apply to the message.
            feed_name: Optional feed name for trace header.
            delivery_name: Optional delivery name for trace header.
            subfolder: Ignored for Gmail (use labels for folder assignment).

        Returns:
            The Gmail API response object for the imported message.

        Raises:
            RemoteError: If the Gmail API call fails.
        """
        try:
            import base64

            msg = RawMessage(raw_message)
            encoded_message = base64.urlsafe_b64encode(msg.as_bytes(feed_name, delivery_name)).decode()
            message_body: dict[str, Any] = {'raw': encoded_message}

            if labels:
                label_ids = self.translate_labels(labels)
                message_body['labelIds'] = label_ids

            # Upload the message
            result = self._api().users().messages().import_(userId='me', body=message_body).execute()

            return result

        except HttpError as error:
            raise RemoteError(f'An error occurred: {error}') from error

    @property
    def needs_auth(self) -> bool:
        """Check if this target needs re-authentication."""
        return self._needs_auth

    def reauthenticate(self) -> None:
        """Perform OAuth re-authentication flow.

        Opens a browser window for the user to log in and authorize the app.
        Updates credentials upon successful authentication.

        Raises:
            ConfigurationError: If credentials file is not found.
        """
        if not self._credentials_file.exists():
            raise ConfigurationError(
                f'{self._credentials_file} not found. Please download it from Google Cloud Console.'
            )

        logger.info('Starting re-authentication for Gmail account %s', self.identifier)

        flow = InstalledAppFlow.from_client_secrets_file(str(self._credentials_file), SCOPES)
        self.creds = flow.run_local_server(port=0)

        atomic_write(self._token_file, self.creds.to_json())

        self._needs_auth = False
        self.service = None  # Reset service to force reconnect with new creds
        logger.info('Re-authentication successful for %s', self.identifier)
