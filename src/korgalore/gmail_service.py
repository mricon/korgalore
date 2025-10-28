"""Gmail API service wrapper."""

import os
from typing import Optional, List, Dict, Any
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow # type: ignore
from googleapiclient.discovery import build # type: ignore
from googleapiclient.errors import HttpError # type: ignore


# If modifying these scopes, delete the file token.json.
# We need scopes for reading and inserting new emails, but not
# modifying existing ones.
SCOPES = [
    'https://www.googleapis.com/auth/gmail.labels',
    'https://www.googleapis.com/auth/gmail.insert',
    ]


class GmailService:
    """Wrapper for Gmail API operations."""

    def __init__(self, cfgdir: Path) -> None:
        """Initialize the Gmail service."""
        self.cfgdir: Path = cfgdir
        self.creds: Optional[Credentials] = None
        self.service: Optional[Any] = None
        self._load_credentials()

    def _load_credentials(self) -> None:
        """Load or refresh credentials."""
        token_path = self.cfgdir / 'token.json'
        credentials_path = self.cfgdir / 'credentials.json'

        # The file token.json stores the user's access and refresh tokens
        if os.path.exists(token_path):
            self.creds = Credentials.from_authorized_user_file(token_path, SCOPES) # type: ignore

        # If there are no (valid) credentials available, let the user log in
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())  # type: ignore
            elif os.path.exists(credentials_path):
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_path, SCOPES)
                self.creds = flow.run_local_server(port=0)
            else:
                raise FileNotFoundError(
                    "credentials.json not found. Please download it from Google Cloud Console."
                )

            # Save the credentials for the next run
            with open(token_path, 'w') as token:
                token.write(self.creds.to_json())

        self.service = build('gmail', 'v1', credentials=self.creds)

    def authenticate(self) -> None:
        """Force re-authentication."""
        if (self.cfgdir / 'token.json').exists():
            os.remove(self.cfgdir / 'token.json')
        self._load_credentials()

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
            raise Exception(f'An error occurred: {error}')

    def import_message(self, raw_message: bytes,
                       label_ids: Optional[List[str]] = None) -> Any:
        try:
            import base64

            encoded_message = base64.urlsafe_b64encode(raw_message).decode()
            message_body: Dict[str, Any] = {'raw': encoded_message}

            if label_ids:
                message_body['labelIds'] = label_ids

            # Upload the message
            result = self.service.users().messages().import_(  # type: ignore
                userId='me',
                body=message_body
            ).execute()

            return result

        except HttpError as error:
            raise RuntimeError(f'An error occurred: {error}')
