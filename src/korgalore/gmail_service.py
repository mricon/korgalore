import os
import logging
from typing import Optional, List, Dict, Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow # type: ignore
from googleapiclient.discovery import build # type: ignore
from googleapiclient.errors import HttpError # type: ignore

from korgalore import ConfigurationError, RemoteError

logger = logging.getLogger('korgalore')

# If modifying these scopes, delete the file token.json.
# We need scopes for reading and inserting new emails, but not
# modifying existing ones.
SCOPES = [
    'https://www.googleapis.com/auth/gmail.labels',
    'https://www.googleapis.com/auth/gmail.insert',
    ]


class GmailService:
    def __init__(self, identifier: str, credentials_file: str, token_file: str) -> None:
        self.identifier = identifier
        self.creds: Optional[Credentials] = None
        self.service: Optional[Any] = None
        self._load_credentials(credentials_file, token_file)
        self._label_map: Optional[Dict[str, str]] = None

    def _load_credentials(self, credentials_file: str, token_file: str) -> None:
        # Expand vars and tildes on file paths
        credentials_file = os.path.expandvars(os.path.expanduser(credentials_file))
        token_file = os.path.expandvars(os.path.expanduser(token_file))
        # The file token.json stores the user's access and refresh tokens
        if os.path.exists(token_file):
            self.creds = Credentials.from_authorized_user_file(token_file, SCOPES) # type: ignore

        # If there are no (valid) credentials available, let the user log in
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())  # type: ignore
            elif os.path.exists(credentials_file):
                logger.critical('Log in to Gmail account for %s', self.identifier)

                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_file, SCOPES)
                self.creds = flow.run_local_server(port=0)
            else:
                raise ConfigurationError(
                    f"{credentials_file} not found. Please download it from Google Cloud Console."
                )

            # Save the credentials for the next run
            with open(token_file, 'w') as token:
                token.write(self.creds.to_json())

        self.service = build('gmail', 'v1', credentials=self.creds)

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
