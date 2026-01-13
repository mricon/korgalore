import os
import logging
from typing import Optional, List, Dict, Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow # type: ignore
from googleapiclient.discovery import build # type: ignore
from googleapiclient.errors import HttpError # type: ignore

from google.auth.exceptions import RefreshError

from korgalore import ConfigurationError, RemoteError, AuthenticationError

logger = logging.getLogger('korgalore')


# If modifying these scopes, delete the file token.json.
# We need scopes for reading and inserting new emails, but not
# modifying existing ones.
SCOPES = [
    'https://www.googleapis.com/auth/gmail.labels',
    'https://www.googleapis.com/auth/gmail.insert',
    ]


class GmailTarget:
    """Target class for delivering email messages to Gmail via the API."""

    DEFAULT_LABELS: List[str] = ['INBOX', 'UNREAD']

    def __init__(self, identifier: str, credentials_file: str, token_file: str,
                 interactive: bool = True) -> None:
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
        self.creds: Optional[Credentials] = None
        self.service: Optional[Any] = None
        self._label_map: Optional[Dict[str, str]] = None
        # Store expanded paths for potential re-authentication
        self._credentials_file = os.path.expandvars(os.path.expanduser(credentials_file))
        self._token_file = os.path.expandvars(os.path.expanduser(token_file))
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
        if os.path.exists(self._token_file):
            self.creds = Credentials.from_authorized_user_file(self._token_file, SCOPES) # type: ignore

        # If there are no (valid) credentials available, let the user log in
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                try:
                    self.creds.refresh(Request())  # type: ignore
                except RefreshError:
                    logger.warning('Gmail token for %s has expired or been revoked.',
                                   self.identifier)
                    invalid_token_file = self._token_file + '.invalid'
                    if os.path.exists(invalid_token_file):
                        os.remove(invalid_token_file)
                    os.rename(self._token_file, invalid_token_file)
                    self._needs_auth = True
                    if not self._interactive:
                        # In non-interactive mode, just return - caller will check needs_auth
                        return
                    raise AuthenticationError(
                        f"Gmail token for '{self.identifier}' is invalid. "
                        f"Please re-authenticate.",
                        target_id=self.identifier,
                        target_type='gmail'
                    )
            elif os.path.exists(self._credentials_file):
                if not self._interactive:
                    # In non-interactive mode (GUI), don't run OAuth flow
                    # Just mark as needing auth and return - caller will check needs_auth
                    self._needs_auth = True
                    return
                logger.critical('Log in to Gmail account for %s', self.identifier)

                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, SCOPES)
                self.creds = flow.run_local_server(port=0)
            else:
                raise ConfigurationError(
                    f"{self._credentials_file} not found. Please download it from Google Cloud Console."
                )

            # Save the credentials for the next run
            with open(self._token_file, 'w') as token:
                token.write(self.creds.to_json())

        self._needs_auth = False


    def connect(self) -> None:
        """Establish connection to the Gmail API service.

        Creates the Gmail API service object if not already connected.
        """
        if self.service is None:
            logger.debug('Connecting to Gmail service for %s', self.identifier)
            self.service = build('gmail', 'v1', credentials=self.creds, cache_discovery=False)

    def list_labels(self) -> List[Dict[str, str]]:
        """List all labels in the user's mailbox.

        Returns:
            List of label objects
        """
        try:
            results = self.service.users().labels().list(userId='me').execute()  # type: ignore
            labels = results.get('labels', [])
            return labels  # type: ignore

        except HttpError as error:
            raise RemoteError(f'An error occurred: {error}')

    def translate_labels(self, labels: List[str]) -> List[str]:
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
        translated: List[str] = []
        for label in labels:
            label_id = self._label_map.get(label, None)
            if label_id is None:
                raise ConfigurationError(f"Label '{label}' not found in Gmail '{self.identifier}'.")
            translated.append(label_id)
        return translated

    def import_message(self, raw_message: bytes, labels: List[str]) -> Any:
        """Import a raw email message into Gmail.

        Args:
            raw_message: The raw email message as bytes.
            labels: List of label names to apply to the message.

        Returns:
            The Gmail API response object for the imported message.

        Raises:
            RemoteError: If the Gmail API call fails.
        """
        try:
            import base64

            encoded_message = base64.urlsafe_b64encode(raw_message).decode()
            message_body: Dict[str, Any] = {'raw': encoded_message}

            if labels:
                label_ids = self.translate_labels(labels)
                message_body['labelIds'] = label_ids

            # Upload the message
            result = self.service.users().messages().import_(  # type: ignore
                userId='me',
                body=message_body
            ).execute()

            return result

        except HttpError as error:
            raise RemoteError(f'An error occurred: {error}')

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
        if not os.path.exists(self._credentials_file):
            raise ConfigurationError(
                f"{self._credentials_file} not found. Please download it from Google Cloud Console."
            )

        logger.info('Starting re-authentication for Gmail account %s', self.identifier)

        flow = InstalledAppFlow.from_client_secrets_file(
            self._credentials_file, SCOPES)
        self.creds = flow.run_local_server(port=0)

        # Save the credentials
        with open(self._token_file, 'w') as token:
            token.write(self.creds.to_json())

        self._needs_auth = False
        self.service = None  # Reset service to force reconnect with new creds
        logger.info('Re-authentication successful for %s', self.identifier)
