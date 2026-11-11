import base64
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from google.auth.credentials import Credentials as BaseCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import Resource, build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

from korgalore import AuthenticationError, ConfigurationError, RemoteError
from korgalore.google_creds import OAuthFileCredentials
from korgalore.message import RawMessage

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

    DEFAULT_LABELS: list[str] = ['INBOX', 'UNREAD']

    def __init__(self, identifier: str, creds: BaseCredentials) -> None:
        """Initialize a GmailTarget instance.

        Args:
            identifier: Unique identifier for this Gmail target.
            creds: Google API credentials instance.
        """
        self.identifier = identifier
        self.creds = creds
        self.service: Resource | None = None
        self._label_map: dict[str, str] | None = None

    @property
    def needs_auth(self) -> bool:
        """Check if this target needs re-authentication."""
        return bool(getattr(self.creds, 'needs_auth', False))

    def connect(self) -> None:
        """Establish connection to the Gmail API service.

        Creates the Gmail API service object if not already connected.

        Raises:
            ConfigurationError: If credentials file is not found during refresh.
            AuthenticationError: If OAuth token refresh or authentication fails.
        """
        if self.service is not None:
            return

        if not self.creds.valid:
            self.creds.refresh(Request())  # type: ignore[no-untyped-call]

        logger.debug('Connecting to Gmail service for %s', self.identifier)
        self.service = build('gmail', 'v1', credentials=self.creds, cache_discovery=False)
        try:
            self._label_map = {label['name']: label['id'] for label in self.list_labels()}
        except Exception:
            self.service = None
            self._label_map = None
            raise

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
            self.connect()
        assert self._label_map is not None
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

    def reauthenticate(self) -> None:
        """Perform OAuth re-authentication flow.

        Delegates interactive re-authentication to the underlying credentials
        provider and clears cached API state so the next operation reconnects
        with the updated credentials.

        Raises:
            ConfigurationError: If credentials file is not found.
            AuthenticationError: If interactive re-authentication is unsupported
                                 or the OAuth flow fails.
        """
        reauth = getattr(self.creds, 'reauthenticate', None)
        if not callable(reauth):
            raise AuthenticationError(
                f"Target '{self.identifier}' does not support interactive reauthentication.",
                target_id=self.identifier,
                target_type='gmail',
            )
        reauth()
        self.service = None
        self._label_map = None


def make_gmail_credentials(
    identifier: str,
    details: Mapping[str, Any],
    interactive: bool = True,
    config_dir: Path | str | None = None,
) -> BaseCredentials:
    """Create credentials for a Gmail target from configuration.

    Args:
        identifier: Unique identifier for the Gmail target.
        details: Target configuration mapping from ``korgalore.toml``.
        interactive: Whether interactive OAuth browser flows are permitted.
        config_dir: Optional base configuration directory for default token paths.

    Returns:
        Configured credentials instance compatible with ``GmailTarget``.

    Raises:
        ConfigurationError: If required configuration keys are missing or have
                            invalid types.
    """
    if 'credentials' not in details:
        raise ConfigurationError(f"No credentials file specified for Gmail target '{identifier}'.")

    creds_raw = details['credentials']
    if not isinstance(creds_raw, str):
        raise ConfigurationError(f"Gmail target '{identifier}': 'credentials' must be a string.")

    token_raw = details.get('token')
    if token_raw is not None and not isinstance(token_raw, str):
        raise ConfigurationError(f"Gmail target '{identifier}': 'token' must be a string.")

    if not token_raw:
        base_dir = (
            Path(config_dir)
            if config_dir is not None
            else Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'korgalore'
        )
        token_file: str | Path = base_dir / f'gmail-{identifier}-token.json'
    else:
        token_file = token_raw

    return OAuthFileCredentials(
        identifier=identifier,
        credentials_file=creds_raw,
        token_file=token_file,
        scopes=SCOPES,
        interactive=interactive,
        target_type='gmail',
    )
