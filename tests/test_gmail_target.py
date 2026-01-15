"""Tests for GmailTarget message delivery."""

import base64
import pytest
from unittest.mock import patch, MagicMock, mock_open

from korgalore import ConfigurationError, RemoteError
from korgalore.gmail_target import GmailTarget, SCOPES


class TestGmailTargetInit:
    """Tests for GmailTarget initialization."""

    @patch('korgalore.gmail_target.Credentials')
    @patch('os.path.exists')
    def test_loads_existing_valid_token(
        self, mock_exists: MagicMock, mock_credentials: MagicMock
    ) -> None:
        """Loads credentials from existing valid token file."""
        mock_exists.return_value = True
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        target = GmailTarget("test", "/path/to/creds.json", "/path/to/token.json")

        assert target.identifier == "test"
        assert target.creds is mock_creds
        mock_credentials.from_authorized_user_file.assert_called_once_with(
            "/path/to/token.json", SCOPES
        )

    @patch('korgalore.gmail_target.Credentials')
    @patch('korgalore.gmail_target.Request')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_refreshes_expired_token(
        self, mock_file: MagicMock, mock_exists: MagicMock,
        mock_request: MagicMock, mock_credentials: MagicMock
    ) -> None:
        """Refreshes expired credentials with refresh token."""
        mock_exists.return_value = True
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "refresh_token_value"
        mock_creds.to_json.return_value = '{"token": "refreshed"}'
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        GmailTarget("test", "/path/to/creds.json", "/path/to/token.json")

        mock_creds.refresh.assert_called_once()
        mock_file.assert_called_with("/path/to/token.json", 'w')

    @patch('korgalore.gmail_target.Credentials')
    @patch('korgalore.gmail_target.InstalledAppFlow')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_runs_oauth_flow_when_no_token(
        self, mock_file: MagicMock, mock_exists: MagicMock,
        mock_flow_class: MagicMock, mock_credentials: MagicMock
    ) -> None:
        """Runs OAuth flow when no token exists."""
        # First call (token file) returns False, second call (creds file) returns True
        mock_exists.side_effect = [False, True]

        mock_creds = MagicMock()
        mock_creds.to_json.return_value = '{"token": "new"}'
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_class.from_client_secrets_file.return_value = mock_flow

        target = GmailTarget("test", "/path/to/creds.json", "/path/to/token.json")

        mock_flow_class.from_client_secrets_file.assert_called_once_with(
            "/path/to/creds.json", SCOPES
        )
        mock_flow.run_local_server.assert_called_once_with(port=0)
        assert target.creds is mock_creds

    @patch('os.path.exists')
    def test_missing_credentials_file_raises(self, mock_exists: MagicMock) -> None:
        """Missing credentials file raises ConfigurationError."""
        # Both token file and credentials file don't exist
        mock_exists.return_value = False

        with pytest.raises(ConfigurationError) as exc_info:
            GmailTarget("test", "/nonexistent/creds.json", "/path/to/token.json")
        assert "not found" in str(exc_info.value)
        assert "creds.json" in str(exc_info.value)

    @patch('korgalore.gmail_target.Credentials')
    @patch('os.path.exists')
    def test_expands_user_paths(
        self, mock_exists: MagicMock, mock_credentials: MagicMock
    ) -> None:
        """Tilde and env vars in paths are expanded."""
        mock_exists.return_value = True
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        with patch.dict('os.environ', {'HOME': '/home/testuser'}):
            GmailTarget("test", "~/creds.json", "$HOME/token.json")

        # Verify expanded paths were used
        call_args = mock_credentials.from_authorized_user_file.call_args[0]
        assert '/home/testuser' in call_args[0] or '~' not in call_args[0]

    @patch('korgalore.gmail_target.Credentials')
    @patch('os.path.exists')
    def test_service_not_initialized(
        self, mock_exists: MagicMock, mock_credentials: MagicMock
    ) -> None:
        """Service is None before connect()."""
        mock_exists.return_value = True
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        target = GmailTarget("test", "/path/to/creds.json", "/path/to/token.json")

        assert target.service is None

    def test_default_labels(self) -> None:
        """Default labels includes INBOX and UNREAD."""
        assert GmailTarget.DEFAULT_LABELS == ['INBOX', 'UNREAD']


class TestGmailTargetConnect:
    """Tests for GmailTarget connect method."""

    @patch('korgalore.gmail_target.build')
    @patch('korgalore.gmail_target.Credentials')
    @patch('os.path.exists')
    def test_connect_builds_service(
        self, mock_exists: MagicMock, mock_credentials: MagicMock,
        mock_build: MagicMock
    ) -> None:
        """Connect builds Gmail API service."""
        mock_exists.return_value = True
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        mock_service = MagicMock()
        mock_build.return_value = mock_service

        target = GmailTarget("test", "/path/to/creds.json", "/path/to/token.json")
        target.connect()

        mock_build.assert_called_once_with('gmail', 'v1', credentials=mock_creds, cache_discovery=False)
        assert target.service is mock_service

    @patch('korgalore.gmail_target.build')
    @patch('korgalore.gmail_target.Credentials')
    @patch('os.path.exists')
    def test_connect_idempotent(
        self, mock_exists: MagicMock, mock_credentials: MagicMock,
        mock_build: MagicMock
    ) -> None:
        """Multiple connect() calls don't rebuild service."""
        mock_exists.return_value = True
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        mock_service = MagicMock()
        mock_build.return_value = mock_service

        target = GmailTarget("test", "/path/to/creds.json", "/path/to/token.json")
        target.connect()
        target.connect()
        target.connect()

        assert mock_build.call_count == 1


class TestGmailTargetListLabels:
    """Tests for GmailTarget list_labels method."""

    def _create_target_with_service(self) -> GmailTarget:
        """Create a target with mocked credentials and service."""
        with patch('korgalore.gmail_target.Credentials') as mock_creds_class, \
             patch('os.path.exists', return_value=True):
            mock_creds = MagicMock()
            mock_creds.valid = True
            mock_creds_class.from_authorized_user_file.return_value = mock_creds
            target = GmailTarget("test", "/creds.json", "/token.json")

        target.service = MagicMock()
        return target

    def test_list_labels_success(self) -> None:
        """Successful label listing."""
        target = self._create_target_with_service()
        assert target.service is not None

        mock_labels = [
            {"id": "INBOX", "name": "INBOX"},
            {"id": "SENT", "name": "SENT"},
            {"id": "Label_123", "name": "MyLabel"}
        ]
        target.service.users().labels().list().execute.return_value = {
            "labels": mock_labels
        }

        result = target.list_labels()

        assert result == mock_labels
        target.service.users().labels().list.assert_called_with(userId='me')

    def test_list_labels_empty(self) -> None:
        """Empty label list returns empty list."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().labels().list().execute.return_value = {}

        result = target.list_labels()

        assert result == []

    def test_list_labels_http_error(self) -> None:
        """HTTP error raises RemoteError."""
        target = self._create_target_with_service()
        assert target.service is not None

        # Import HttpError for the mock
        from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
        mock_response = MagicMock()
        mock_response.status = 403
        target.service.users().labels().list().execute.side_effect = HttpError(
            mock_response, b"Forbidden"
        )

        with pytest.raises(RemoteError) as exc_info:
            target.list_labels()
        assert "error occurred" in str(exc_info.value)


class TestGmailTargetTranslateLabels:
    """Tests for GmailTarget translate_labels method."""

    def _create_target_with_service(self) -> GmailTarget:
        """Create a target with mocked credentials and service."""
        with patch('korgalore.gmail_target.Credentials') as mock_creds_class, \
             patch('os.path.exists', return_value=True):
            mock_creds = MagicMock()
            mock_creds.valid = True
            mock_creds_class.from_authorized_user_file.return_value = mock_creds
            target = GmailTarget("test", "/creds.json", "/token.json")

        target.service = MagicMock()
        return target

    def test_translate_single_label(self) -> None:
        """Translate single label name to ID."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().labels().list().execute.return_value = {
            "labels": [
                {"id": "INBOX", "name": "INBOX"},
                {"id": "Label_123", "name": "MyLabel"}
            ]
        }

        result = target.translate_labels(["INBOX"])

        assert result == ["INBOX"]

    def test_translate_multiple_labels(self) -> None:
        """Translate multiple label names to IDs."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().labels().list().execute.return_value = {
            "labels": [
                {"id": "INBOX", "name": "INBOX"},
                {"id": "UNREAD", "name": "UNREAD"},
                {"id": "Label_123", "name": "MyLabel"}
            ]
        }

        result = target.translate_labels(["INBOX", "MyLabel", "UNREAD"])

        assert result == ["INBOX", "Label_123", "UNREAD"]

    def test_translate_unknown_label_raises(self) -> None:
        """Unknown label raises ConfigurationError."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().labels().list().execute.return_value = {
            "labels": [{"id": "INBOX", "name": "INBOX"}]
        }

        with pytest.raises(ConfigurationError) as exc_info:
            target.translate_labels(["NonExistent"])
        assert "not found" in str(exc_info.value)
        assert "NonExistent" in str(exc_info.value)

    def test_translate_caches_label_map(self) -> None:
        """Label map is cached after first translation."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().labels().list().execute.return_value = {
            "labels": [
                {"id": "INBOX", "name": "INBOX"},
                {"id": "Label_123", "name": "MyLabel"}
            ]
        }

        target.translate_labels(["INBOX"])
        target.translate_labels(["MyLabel"])
        target.translate_labels(["INBOX", "MyLabel"])

        # list() should only be called once
        assert target.service.users().labels().list().execute.call_count == 1


class TestGmailTargetImportMessage:
    """Tests for GmailTarget import_message method."""

    def _create_target_with_service(self) -> GmailTarget:
        """Create a target with mocked credentials and service."""
        with patch('korgalore.gmail_target.Credentials') as mock_creds_class, \
             patch('os.path.exists', return_value=True):
            mock_creds = MagicMock()
            mock_creds.valid = True
            mock_creds_class.from_authorized_user_file.return_value = mock_creds
            target = GmailTarget("test", "/creds.json", "/token.json")

        target.service = MagicMock()
        # Pre-populate label map to avoid list_labels call
        target._label_map = {
            "INBOX": "INBOX",
            "UNREAD": "UNREAD",
            "MyLabel": "Label_123"
        }
        return target

    def test_import_success_with_labels(self) -> None:
        """Successful message import with labels."""
        target = self._create_target_with_service()
        assert target.service is not None

        mock_result = {"id": "msg123", "labelIds": ["INBOX", "UNREAD"]}
        target.service.users().messages().import_().execute.return_value = mock_result

        raw_message = b"From: test@example.com\r\nSubject: Test\r\n\r\nBody"
        result = target.import_message(raw_message, ["INBOX", "UNREAD"])

        assert result == mock_result

        # Verify the API call - check the call was made with correct kwargs
        import_call = target.service.users().messages().import_
        # Find the call with userId and body kwargs
        call_kwargs = None
        for call in import_call.call_args_list:
            if call[1].get('userId') == 'me':
                call_kwargs = call[1]
                break
        assert call_kwargs is not None, "import_ was not called with userId='me'"
        assert 'raw' in call_kwargs['body']
        assert call_kwargs['body']['labelIds'] == ["INBOX", "UNREAD"]

    def test_import_base64_encoding(self) -> None:
        """Message is base64 URL-safe encoded."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().messages().import_().execute.return_value = {"id": "msg123"}

        raw_message = b"Test message with special chars: +/="
        target.import_message(raw_message, ["INBOX"])

        import_call = target.service.users().messages().import_
        call_kwargs = import_call.call_args[1]
        encoded = call_kwargs['body']['raw']

        # Verify it's valid base64 and decodes back
        decoded = base64.urlsafe_b64decode(encoded)
        assert decoded == raw_message

    def test_import_without_labels(self) -> None:
        """Import without labels doesn't include labelIds."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().messages().import_().execute.return_value = {"id": "msg123"}

        raw_message = b"Test message"
        target.import_message(raw_message, [])

        import_call = target.service.users().messages().import_
        call_kwargs = import_call.call_args[1]
        assert 'labelIds' not in call_kwargs['body']

    def test_import_translates_label_names(self) -> None:
        """Label names are translated to IDs."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().messages().import_().execute.return_value = {"id": "msg123"}

        target.import_message(b"Test", ["MyLabel"])

        import_call = target.service.users().messages().import_
        call_kwargs = import_call.call_args[1]
        assert call_kwargs['body']['labelIds'] == ["Label_123"]

    def test_import_http_error(self) -> None:
        """HTTP error raises RemoteError."""
        target = self._create_target_with_service()
        assert target.service is not None

        from googleapiclient.errors import HttpError
        mock_response = MagicMock()
        mock_response.status = 500
        target.service.users().messages().import_().execute.side_effect = HttpError(
            mock_response, b"Internal Server Error"
        )

        with pytest.raises(RemoteError) as exc_info:
            target.import_message(b"Test", ["INBOX"])
        assert "error occurred" in str(exc_info.value)

    def test_import_unknown_label_raises(self) -> None:
        """Unknown label in import raises ConfigurationError."""
        target = self._create_target_with_service()
        assert target.service is not None

        with pytest.raises(ConfigurationError) as exc_info:
            target.import_message(b"Test", ["UnknownLabel"])
        assert "not found" in str(exc_info.value)


class TestGmailTargetEdgeCases:
    """Edge case tests."""

    def _create_target_with_service(self) -> GmailTarget:
        """Create a target with mocked credentials and service."""
        with patch('korgalore.gmail_target.Credentials') as mock_creds_class, \
             patch('os.path.exists', return_value=True):
            mock_creds = MagicMock()
            mock_creds.valid = True
            mock_creds_class.from_authorized_user_file.return_value = mock_creds
            target = GmailTarget("test", "/creds.json", "/token.json")

        target.service = MagicMock()
        target._label_map = {"INBOX": "INBOX"}
        return target

    def test_large_message(self) -> None:
        """Large messages are handled correctly."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().messages().import_().execute.return_value = {"id": "msg123"}

        # 1MB message
        large_message = b"X" * 1024 * 1024
        result = target.import_message(large_message, ["INBOX"])

        assert result == {"id": "msg123"}

    def test_binary_message_content(self) -> None:
        """Binary content is properly base64 encoded."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().messages().import_().execute.return_value = {"id": "msg123"}

        # Message with all byte values
        binary_message = bytes(range(256))
        target.import_message(binary_message, ["INBOX"])

        import_call = target.service.users().messages().import_
        call_kwargs = import_call.call_args[1]
        encoded = call_kwargs['body']['raw']

        # Should decode back correctly
        decoded = base64.urlsafe_b64decode(encoded)
        assert decoded == binary_message

    def test_empty_message(self) -> None:
        """Empty message is handled."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().messages().import_().execute.return_value = {"id": "msg123"}

        result = target.import_message(b"", ["INBOX"])

        assert result == {"id": "msg123"}

    def test_multiple_imports(self) -> None:
        """Multiple messages can be imported."""
        target = self._create_target_with_service()
        assert target.service is not None
        target.service.users().messages().import_().execute.side_effect = [
            {"id": f"msg{i}"} for i in range(5)
        ]

        results = []
        for i in range(5):
            results.append(target.import_message(f"Message {i}".encode(), ["INBOX"]))

        assert [r["id"] for r in results] == ["msg0", "msg1", "msg2", "msg3", "msg4"]

    @patch('korgalore.gmail_target.Credentials')
    @patch('korgalore.gmail_target.InstalledAppFlow')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_token_saved_after_oauth_flow(
        self, mock_file: MagicMock, mock_exists: MagicMock,
        mock_flow_class: MagicMock, mock_credentials: MagicMock
    ) -> None:
        """Token is saved after OAuth flow completes."""
        mock_exists.side_effect = [False, True]  # No token, creds exist

        mock_creds = MagicMock()
        mock_creds.to_json.return_value = '{"access_token": "new_token"}'
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_class.from_client_secrets_file.return_value = mock_flow

        GmailTarget("test", "/path/to/creds.json", "/path/to/token.json")

        # Verify token was written
        mock_file.assert_called_with("/path/to/token.json", 'w')
        handle = mock_file()
        handle.write.assert_called_once_with('{"access_token": "new_token"}')

    def test_label_names_are_case_sensitive(self) -> None:
        """Label name matching is case-sensitive."""
        target = self._create_target_with_service()
        assert target.service is not None
        target._label_map = {"INBOX": "INBOX", "inbox": "inbox_lower"}

        result = target.translate_labels(["INBOX"])
        assert result == ["INBOX"]

        result = target.translate_labels(["inbox"])
        assert result == ["inbox_lower"]
