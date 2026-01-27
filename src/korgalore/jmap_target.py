"""Service for delivering messages to JMAP-compatible mail servers."""

import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, cast

import requests

from korgalore import ConfigurationError, RemoteError
from korgalore.message import RawMessage

logger = logging.getLogger('korgalore')


class JmapTarget:
    """Service for delivering messages to JMAP mail servers (e.g., Fastmail)."""

    DEFAULT_LABELS: List[str] = ['INBOX']

    def __init__(self, identifier: str, server: str, username: str,
                 token: Optional[str] = None, token_file: Optional[str] = None,
                 timeout: int = 60,
                 reqsession: Optional[requests.Session] = None) -> None:
        """Initialize JMAP service.

        Args:
            identifier: Target identifier for logging
            server: JMAP server URL (e.g., 'https://api.fastmail.com')
            username: Account username/email
            token: Bearer token (if provided directly)
            token_file: Path to file containing bearer token
            timeout: Request timeout in seconds (default: 60)
            reqsession: Optional requests session for HTTP calls

        Raises:
            ConfigurationError: If configuration is invalid
        """
        self.identifier = identifier
        self.server = server.rstrip('/')
        self.username = username
        self._reqsession = reqsession

        # Load token from file or use provided token
        if token:
            self.token = token
        elif token_file:
            token_path = Path(token_file).expanduser()
            if not token_path.exists():
                raise ConfigurationError(
                    f"Token file not found: {token_file}"
                )
            with open(token_path, 'r') as f:
                self.token = f.read().strip()
        else:
            raise ConfigurationError(
                f"No token or token_file specified for JMAP target: {identifier}"
            )

        # Request timeout
        self.timeout = timeout

        # Session state
        self.session: Optional[Dict[str, Any]] = None
        self.account_id: Optional[str] = None
        self.api_url: Optional[str] = None
        self.upload_url: Optional[str] = None

        # Mailbox cache
        self._mailbox_map: Optional[Dict[str, str]] = None  # name -> id

    def _get_session(self) -> requests.Session:
        """Get requests session, using provided one or falling back to requests module."""
        if self._reqsession is not None:
            return self._reqsession
        # Fall back to requests module directly (creates new connection each time)
        return requests  # type: ignore[return-value]

    def connect(self) -> None:
        """Connect to JMAP server and discover session."""
        if self.session is None:
            logger.debug('Connecting to JMAP server for %s', self.identifier)
            self._discover_session()

    def _discover_session(self) -> None:
        """Discover JMAP session and API endpoints."""
        session_url = f"{self.server}/jmap/session"

        try:
            response = self._get_session().get(
                session_url,
                headers={'Authorization': f'Bearer {self.token}'},
                timeout=self.timeout
            )
            response.raise_for_status()
            self.session = response.json()
        except requests.RequestException as e:
            raise RemoteError(
                f"Failed to discover JMAP session at {session_url}: {e}"
            ) from e

        # Extract endpoints and account
        self.api_url = self.session.get('apiUrl')
        upload_url_template = self.session.get('uploadUrl')

        if not self.api_url or not upload_url_template:
            raise RemoteError(
                "Invalid JMAP session response: missing apiUrl or uploadUrl"
            )

        # Find account by username
        accounts = self.session.get('accounts', {})
        for acc_id, acc_info in accounts.items():
            if acc_info.get('name') == self.username:
                self.account_id = acc_id
                break

        if not self.account_id:
            raise ConfigurationError(
                f"Account not found for username: {self.username}"
            )

        # Set upload URL with account ID
        self.upload_url = upload_url_template.replace('{accountId}', self.account_id)

        logger.debug('JMAP session discovered: accountId=%s', self.account_id)

    def _upload_blob(self, raw_message: bytes) -> str:
        """Upload raw message bytes and get blob ID.

        Args:
            raw_message: Raw email bytes (RFC 2822)

        Returns:
            Blob ID string
        """
        assert self.upload_url is not None, "Must call connect() first"
        try:
            response = self._get_session().post(
                self.upload_url,
                data=raw_message,
                headers={
                    'Authorization': f'Bearer {self.token}',
                    'Content-Type': 'message/rfc822'
                },
                timeout=self.timeout
            )
            response.raise_for_status()
            result = response.json()
            blob_id = result.get('blobId')

            if not blob_id or not isinstance(blob_id, str):
                raise RemoteError(f"No blobId in upload response: {result}")

            # mypy needs explicit cast after isinstance check
            blob_id_str = cast(str, blob_id)
            logger.debug('Uploaded blob: %s (%d bytes)', blob_id_str, len(raw_message))
            return blob_id_str
        except requests.RequestException as e:
            raise RemoteError(f"Failed to upload message blob: {e}") from e

    def list_mailboxes(self) -> List[Dict[str, str]]:
        """List all mailboxes/folders.

        Returns:
            List of dicts with 'id', 'name', 'role' keys
        """
        assert self.api_url is not None, "Must call connect() first"
        try:
            # Query all mailboxes
            request_body = {
                "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
                "methodCalls": [
                    ["Mailbox/query", {"accountId": self.account_id}, "call-0"],
                    ["Mailbox/get", {
                        "accountId": self.account_id,
                        "#ids": {
                            "resultOf": "call-0",
                            "name": "Mailbox/query",
                            "path": "/ids"
                        }
                    }, "call-1"]
                ]
            }

            response = self._get_session().post(
                self.api_url,
                json=request_body,
                headers={'Authorization': f'Bearer {self.token}'},
                timeout=self.timeout
            )
            response.raise_for_status()
            result = response.json()

            # Extract mailboxes from Mailbox/get response
            mailboxes = []
            for method_response in result.get('methodResponses', []):
                method_name, method_result, _ = method_response
                if method_name == 'Mailbox/get':
                    for mailbox in method_result.get('list', []):
                        mailboxes.append({
                            'id': mailbox['id'],
                            'name': mailbox['name'],
                            'role': mailbox.get('role', '')
                        })

            logger.debug('Found %d mailboxes', len(mailboxes))
            return mailboxes
        except requests.RequestException as e:
            raise RemoteError(f"Failed to list mailboxes: {e}") from e

    def translate_folders(self, folder_names: List[str]) -> List[str]:
        """Translate folder names to mailbox IDs.

        Args:
            folder_names: List of folder names (e.g., ['INBOX', 'Sent'])

        Returns:
            List of mailbox IDs
        """
        # Lazy-load mailbox map
        if self._mailbox_map is None:
            mailboxes = self.list_mailboxes()
            # Build name -> id map (case-insensitive)
            self._mailbox_map = {}
            for mb in mailboxes:
                name = mb['name'].lower()
                self._mailbox_map[name] = mb['id']
                # Also map by role
                if mb['role']:
                    self._mailbox_map[mb['role'].lower()] = mb['id']

        # Translate each folder name
        mailbox_ids = []
        for folder_name in folder_names:
            folder_key = folder_name.lower()
            mailbox_id = self._mailbox_map.get(folder_key)

            if mailbox_id is None:
                raise ConfigurationError(
                    f"Folder '{folder_name}' not found in JMAP account '{self.identifier}'"
                )

            mailbox_ids.append(mailbox_id)

        return mailbox_ids

    def _check_message_exists(self, message_id: str, mailbox_ids: List[str]) -> bool:
        """Check if a message with this Message-ID exists in any of the mailboxes.

        Args:
            message_id: Message-ID header value (with angle brackets)
            mailbox_ids: List of mailbox IDs to check

        Returns:
            True if message exists in at least one of the mailboxes
        """
        assert self.api_url is not None, "Must call connect() first"

        # Build filter: check for Message-ID in any of the target mailboxes
        if len(mailbox_ids) == 1:
            # Simple case: single mailbox
            query_filter: Dict[str, Any] = {
                "header": ["Message-ID", message_id],
                "inMailbox": mailbox_ids[0]
            }
        else:
            # Multiple mailboxes: use OR filter
            query_filter = {
                "operator": "OR",
                "conditions": [
                    {"header": ["Message-ID", message_id], "inMailbox": mb_id}
                    for mb_id in mailbox_ids
                ]
            }

        try:
            request_body = {
                "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
                "methodCalls": [
                    ["Email/query", {
                        "accountId": self.account_id,
                        "filter": query_filter,
                        "limit": 1  # We only need to know if any exist
                    }, "call-0"]
                ]
            }

            response = self._get_session().post(
                self.api_url,
                json=request_body,
                headers={'Authorization': f'Bearer {self.token}'},
                timeout=self.timeout
            )
            response.raise_for_status()
            result = response.json()

            # Check if any messages were found
            for method_response in result.get('methodResponses', []):
                method_name, method_result, _ = method_response
                if method_name == 'Email/query':
                    ids = method_result.get('ids', [])
                    if ids:
                        logger.debug('Message-ID %s already exists in target mailbox', message_id)
                        return True

            return False
        except requests.RequestException as e:
            # On error, log and proceed with import (fail-open)
            logger.debug('Failed to check for existing message: %s', e)
            return False

    def import_message(self, raw_message: bytes, labels: List[str]) -> Any:
        """Import raw email message to JMAP server.

        Args:
            raw_message: Raw email bytes (may have Unix LF line endings)
            labels: List of folder names (e.g., ['INBOX', 'Lists/LKML'])

        Returns:
            JMAP import result dict
        """
        assert self.api_url is not None, "Must call connect() first"

        msg = RawMessage(raw_message)

        # Step 1: Translate folder names to IDs (needed for both dedup check and import)
        if labels:
            mailbox_ids_list = self.translate_folders(labels)
        else:
            # Default to INBOX if no folders specified
            mailbox_ids_list = self.translate_folders(['inbox'])

        # Step 2: Check if message already exists in target mailbox(es)
        if msg.message_id and self._check_message_exists(msg.message_id, mailbox_ids_list):
            logger.debug('Skipping import: message %s already in target mailbox', msg.message_id)
            return {'id': '(duplicate)', 'skipped': True}

        # Step 3: Upload blob
        blob_id = self._upload_blob(msg.as_bytes())

        # Build mailboxIds dict (id -> true)
        mailbox_ids = {mb_id: True for mb_id in mailbox_ids_list}

        # Step 5: Import email
        try:
            request_body = {
                "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
                "methodCalls": [
                    ["Email/import", {
                        "accountId": self.account_id,
                        "emails": {
                            "msg1": {
                                "blobId": blob_id,
                                "mailboxIds": mailbox_ids,
                                "keywords": {}  # Can add $seen, $flagged, etc.
                            }
                        }
                    }, "call-0"]
                ]
            }

            response = self._get_session().post(
                self.api_url,
                json=request_body,
                headers={'Authorization': f'Bearer {self.token}'},
                timeout=self.timeout
            )
            response.raise_for_status()
            result = response.json()

            # Check for errors
            for method_response in result.get('methodResponses', []):
                method_name, method_result, _ = method_response
                if method_name == 'Email/import':
                    # Check for successful creation first
                    created = method_result.get('created', {})
                    if 'msg1' in created:
                        logger.debug('Imported message: %s', created['msg1']['id'])
                        return created['msg1']

                    # If not created, check if there were errors (notCreated will be non-empty)
                    not_created = method_result.get('notCreated', {})
                    if not_created:
                        # Check if message already exists - this is OK, treat as success
                        if 'msg1' in not_created and not_created['msg1'].get('type') == 'alreadyExists':
                            existing_id = not_created['msg1'].get('existingId', '(unknown)')
                            logger.debug('Message already exists with id: %s', existing_id)
                            return {'id': existing_id}
                        # Any other error is a real failure
                        raise RemoteError(f"JMAP Email/import failed: {not_created}")

            raise RemoteError(f"Unexpected JMAP response: {result}")
        except requests.RequestException as e:
            raise RemoteError(f"Failed to import message: {e}") from e

    def list_labels(self) -> List[Dict[str, str]]:
        """List all available folders/mailboxes.

        Returns:
            List of dicts with 'name' and 'id' keys (for CLI labels command)
        """
        mailboxes = self.list_mailboxes()
        return [
            {'name': mb['name'], 'id': mb['id']}
            for mb in mailboxes
        ]
