"""Tests for JmapTarget message delivery."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import requests

from korgalore import ConfigurationError, RemoteError
from korgalore.jmap_target import JmapTarget


# Sample JMAP session response
SAMPLE_SESSION = {
    "apiUrl": "https://api.example.com/jmap/api/",
    "uploadUrl": "https://api.example.com/jmap/upload/{accountId}/",
    "accounts": {
        "acc-123": {"name": "user@example.com"},
        "acc-456": {"name": "other@example.com"}
    }
}

# Sample mailbox list response
SAMPLE_MAILBOXES_RESPONSE = {
    "methodResponses": [
        ["Mailbox/query", {"ids": ["mb-1", "mb-2", "mb-3"]}, "call-0"],
        ["Mailbox/get", {
            "list": [
                {"id": "mb-1", "name": "Inbox", "role": "inbox"},
                {"id": "mb-2", "name": "Sent", "role": "sent"},
                {"id": "mb-3", "name": "Archive", "role": ""}
            ]
        }, "call-1"]
    ]
}


class TestJmapTargetInit:
    """Tests for JmapTarget initialization."""

    def test_valid_config_with_token(self) -> None:
        """Valid configuration with direct token."""
        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="secret_token"
        )
        assert target.identifier == "test"
        assert target.server == "https://api.example.com"
        assert target.username == "user@example.com"
        assert target.token == "secret_token"
        assert target.timeout == 60

    def test_server_trailing_slash_stripped(self) -> None:
        """Trailing slash on server URL is stripped."""
        target = JmapTarget(
            identifier="test",
            server="https://api.example.com/",
            username="user@example.com",
            token="token"
        )
        assert target.server == "https://api.example.com"

    def test_valid_config_with_token_file(self, tmp_path: Path) -> None:
        """Valid configuration with token file."""
        token_file = tmp_path / "token.txt"
        token_file.write_text("file_token\n")

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token_file=str(token_file)
        )
        assert target.token == "file_token"

    def test_token_file_strips_whitespace(self, tmp_path: Path) -> None:
        """Token file content is stripped of whitespace."""
        token_file = tmp_path / "token.txt"
        token_file.write_text("  token_with_spaces  \n\n")

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token_file=str(token_file)
        )
        assert target.token == "token_with_spaces"

    def test_token_file_with_tilde(self, tmp_path: Path) -> None:
        """Token file path with tilde is expanded."""
        token_file = tmp_path / "token.txt"
        token_file.write_text("secret")

        with patch.object(Path, "expanduser", return_value=token_file):
            target = JmapTarget(
                identifier="test",
                server="https://api.example.com",
                username="user@example.com",
                token_file="~/token.txt"
            )
        assert target.token == "secret"

    def test_custom_timeout(self) -> None:
        """Custom timeout can be specified."""
        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token",
            timeout=120
        )
        assert target.timeout == 120

    def test_missing_token_raises(self) -> None:
        """Missing both token and token_file raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            JmapTarget(
                identifier="test",
                server="https://api.example.com",
                username="user@example.com"
            )
        assert "No token or token_file specified" in str(exc_info.value)

    def test_nonexistent_token_file_raises(self) -> None:
        """Non-existent token file raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            JmapTarget(
                identifier="test",
                server="https://api.example.com",
                username="user@example.com",
                token_file="/nonexistent/path/token.txt"
            )
        assert "Token file not found" in str(exc_info.value)

    def test_session_not_initialized(self) -> None:
        """Session state is None before connect()."""
        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )
        assert target.session is None
        assert target.account_id is None
        assert target.api_url is None
        assert target.upload_url is None

    def test_default_labels(self) -> None:
        """Default labels is INBOX."""
        assert JmapTarget.DEFAULT_LABELS == ['INBOX']


class TestJmapTargetConnect:
    """Tests for JmapTarget connect method."""

    @patch('korgalore.jmap_target.requests.get')
    def test_connect_success(self, mock_get: MagicMock) -> None:
        """Successful session discovery."""
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_SESSION
        mock_get.return_value = mock_response

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="secret_token"
        )
        target.connect()

        mock_get.assert_called_once_with(
            "https://api.example.com/jmap/session",
            headers={'Authorization': 'Bearer secret_token'},
            timeout=60
        )
        assert target.account_id == "acc-123"
        assert target.api_url == "https://api.example.com/jmap/api/"
        assert target.upload_url == "https://api.example.com/jmap/upload/acc-123/"

    @patch('korgalore.jmap_target.requests.get')
    def test_connect_idempotent(self, mock_get: MagicMock) -> None:
        """Multiple connect() calls don't reconnect."""
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_SESSION
        mock_get.return_value = mock_response

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )
        target.connect()
        target.connect()
        target.connect()

        assert mock_get.call_count == 1

    @patch('korgalore.jmap_target.requests.get')
    def test_connect_request_failure(self, mock_get: MagicMock) -> None:
        """Request failure raises RemoteError."""
        mock_get.side_effect = requests.RequestException("Connection refused")

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )

        with pytest.raises(RemoteError) as exc_info:
            target.connect()
        assert "Failed to discover JMAP session" in str(exc_info.value)

    @patch('korgalore.jmap_target.requests.get')
    def test_connect_missing_api_url(self, mock_get: MagicMock) -> None:
        """Missing apiUrl in session raises RemoteError."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "uploadUrl": "https://api.example.com/upload/{accountId}/"
        }
        mock_get.return_value = mock_response

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )

        with pytest.raises(RemoteError) as exc_info:
            target.connect()
        assert "missing apiUrl or uploadUrl" in str(exc_info.value)

    @patch('korgalore.jmap_target.requests.get')
    def test_connect_missing_upload_url(self, mock_get: MagicMock) -> None:
        """Missing uploadUrl in session raises RemoteError."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "apiUrl": "https://api.example.com/api/"
        }
        mock_get.return_value = mock_response

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )

        with pytest.raises(RemoteError) as exc_info:
            target.connect()
        assert "missing apiUrl or uploadUrl" in str(exc_info.value)

    @patch('korgalore.jmap_target.requests.get')
    def test_connect_account_not_found(self, mock_get: MagicMock) -> None:
        """Account not found raises ConfigurationError."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "apiUrl": "https://api.example.com/api/",
            "uploadUrl": "https://api.example.com/upload/{accountId}/",
            "accounts": {
                "acc-999": {"name": "different@example.com"}
            }
        }
        mock_get.return_value = mock_response

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )

        with pytest.raises(ConfigurationError) as exc_info:
            target.connect()
        assert "Account not found" in str(exc_info.value)


class TestJmapTargetUploadBlob:
    """Tests for JmapTarget _upload_blob method."""

    def _create_connected_target(self) -> JmapTarget:
        """Create a target with session state pre-populated."""
        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )
        target.session = SAMPLE_SESSION
        target.account_id = "acc-123"
        target.api_url = "https://api.example.com/jmap/api/"
        target.upload_url = "https://api.example.com/jmap/upload/acc-123/"
        return target

    @patch('korgalore.jmap_target.requests.post')
    def test_upload_success(self, mock_post: MagicMock) -> None:
        """Successful blob upload."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"blobId": "blob-abc123"}
        mock_post.return_value = mock_response

        target = self._create_connected_target()
        blob_id = target._upload_blob(b"Test message content")

        assert blob_id == "blob-abc123"
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs['data'] == b"Test message content"
        assert call_kwargs['headers']['Content-Type'] == 'message/rfc822'
        assert 'Bearer token' in call_kwargs['headers']['Authorization']

    @patch('korgalore.jmap_target.requests.post')
    def test_upload_request_failure(self, mock_post: MagicMock) -> None:
        """Upload request failure raises RemoteError."""
        mock_post.side_effect = requests.RequestException("Upload failed")

        target = self._create_connected_target()

        with pytest.raises(RemoteError) as exc_info:
            target._upload_blob(b"Test")
        assert "Failed to upload message blob" in str(exc_info.value)

    @patch('korgalore.jmap_target.requests.post')
    def test_upload_missing_blob_id(self, mock_post: MagicMock) -> None:
        """Missing blobId in response raises RemoteError."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"size": 100}  # No blobId
        mock_post.return_value = mock_response

        target = self._create_connected_target()

        with pytest.raises(RemoteError) as exc_info:
            target._upload_blob(b"Test")
        assert "No blobId in upload response" in str(exc_info.value)


class TestJmapTargetListMailboxes:
    """Tests for JmapTarget list_mailboxes method."""

    def _create_connected_target(self) -> JmapTarget:
        """Create a target with session state pre-populated."""
        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )
        target.session = SAMPLE_SESSION
        target.account_id = "acc-123"
        target.api_url = "https://api.example.com/jmap/api/"
        target.upload_url = "https://api.example.com/jmap/upload/acc-123/"
        return target

    @patch('korgalore.jmap_target.requests.post')
    def test_list_mailboxes_success(self, mock_post: MagicMock) -> None:
        """Successful mailbox listing."""
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_MAILBOXES_RESPONSE
        mock_post.return_value = mock_response

        target = self._create_connected_target()
        mailboxes = target.list_mailboxes()

        assert len(mailboxes) == 3
        assert mailboxes[0] == {"id": "mb-1", "name": "Inbox", "role": "inbox"}
        assert mailboxes[1] == {"id": "mb-2", "name": "Sent", "role": "sent"}
        assert mailboxes[2] == {"id": "mb-3", "name": "Archive", "role": ""}

    @patch('korgalore.jmap_target.requests.post')
    def test_list_mailboxes_request_failure(self, mock_post: MagicMock) -> None:
        """Request failure raises RemoteError."""
        mock_post.side_effect = requests.RequestException("API error")

        target = self._create_connected_target()

        with pytest.raises(RemoteError) as exc_info:
            target.list_mailboxes()
        assert "Failed to list mailboxes" in str(exc_info.value)


class TestJmapTargetTranslateFolders:
    """Tests for JmapTarget translate_folders method."""

    def _create_connected_target_with_mailboxes(self) -> JmapTarget:
        """Create a target with mailbox cache pre-populated."""
        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )
        target.session = SAMPLE_SESSION
        target.account_id = "acc-123"
        target.api_url = "https://api.example.com/jmap/api/"
        target.upload_url = "https://api.example.com/jmap/upload/acc-123/"
        # Pre-populate mailbox cache
        target._mailbox_map = {
            "inbox": "mb-1",
            "sent": "mb-2",
            "archive": "mb-3"
        }
        return target

    def test_translate_single_folder(self) -> None:
        """Translate single folder name."""
        target = self._create_connected_target_with_mailboxes()
        result = target.translate_folders(["inbox"])
        assert result == ["mb-1"]

    def test_translate_multiple_folders(self) -> None:
        """Translate multiple folder names."""
        target = self._create_connected_target_with_mailboxes()
        result = target.translate_folders(["inbox", "sent", "archive"])
        assert result == ["mb-1", "mb-2", "mb-3"]

    def test_translate_case_insensitive(self) -> None:
        """Folder translation is case-insensitive."""
        target = self._create_connected_target_with_mailboxes()
        result = target.translate_folders(["INBOX", "Sent", "ARCHIVE"])
        assert result == ["mb-1", "mb-2", "mb-3"]

    def test_translate_unknown_folder_raises(self) -> None:
        """Unknown folder raises ConfigurationError."""
        target = self._create_connected_target_with_mailboxes()

        with pytest.raises(ConfigurationError) as exc_info:
            target.translate_folders(["nonexistent"])
        assert "not found" in str(exc_info.value)
        assert "nonexistent" in str(exc_info.value)

    @patch('korgalore.jmap_target.requests.post')
    def test_translate_lazy_loads_mailboxes(self, mock_post: MagicMock) -> None:
        """Mailbox map is lazy-loaded on first translation."""
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_MAILBOXES_RESPONSE
        mock_post.return_value = mock_response

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )
        target.session = SAMPLE_SESSION
        target.account_id = "acc-123"
        target.api_url = "https://api.example.com/jmap/api/"
        target.upload_url = "https://api.example.com/jmap/upload/acc-123/"

        assert target._mailbox_map is None
        result = target.translate_folders(["inbox"])
        assert target._mailbox_map is not None
        assert result == ["mb-1"]


class TestJmapTargetImportMessage:
    """Tests for JmapTarget import_message method."""

    def _create_connected_target_with_mailboxes(self) -> JmapTarget:
        """Create a fully configured target."""
        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )
        target.session = SAMPLE_SESSION
        target.account_id = "acc-123"
        target.api_url = "https://api.example.com/jmap/api/"
        target.upload_url = "https://api.example.com/jmap/upload/acc-123/"
        target._mailbox_map = {
            "inbox": "mb-1",
            "sent": "mb-2",
            "archive": "mb-3"
        }
        return target

    @patch('korgalore.jmap_target.requests.post')
    def test_import_success(self, mock_post: MagicMock) -> None:
        """Successful message import."""
        # First call: upload blob, Second call: import email
        upload_response = MagicMock()
        upload_response.json.return_value = {"blobId": "blob-123"}

        import_response = MagicMock()
        import_response.json.return_value = {
            "methodResponses": [
                ["Email/import", {
                    "created": {"msg1": {"id": "email-456"}}
                }, "call-0"]
            ]
        }

        mock_post.side_effect = [upload_response, import_response]

        target = self._create_connected_target_with_mailboxes()
        result = target.import_message(b"From: test@example.com\r\n\r\nBody", ["inbox"])

        assert result == {"id": "email-456"}
        assert mock_post.call_count == 2

    @patch('korgalore.jmap_target.requests.post')
    def test_import_crlf_normalization(self, mock_post: MagicMock) -> None:
        """Unix line endings are normalized to CRLF before upload."""
        upload_response = MagicMock()
        upload_response.json.return_value = {"blobId": "blob-123"}

        import_response = MagicMock()
        import_response.json.return_value = {
            "methodResponses": [
                ["Email/import", {"created": {"msg1": {"id": "email-456"}}}, "call-0"]
            ]
        }

        mock_post.side_effect = [upload_response, import_response]

        target = self._create_connected_target_with_mailboxes()
        target.import_message(b"From: a@b.com\nTo: c@d.com\n\nBody\nLine2", ["inbox"])

        # Check the upload call
        upload_call = mock_post.call_args_list[0]
        uploaded_data = upload_call[1]['data']
        assert uploaded_data == b"From: a@b.com\r\nTo: c@d.com\r\n\r\nBody\r\nLine2"

    @patch('korgalore.jmap_target.requests.post')
    def test_import_default_to_inbox(self, mock_post: MagicMock) -> None:
        """Empty labels defaults to inbox."""
        upload_response = MagicMock()
        upload_response.json.return_value = {"blobId": "blob-123"}

        import_response = MagicMock()
        import_response.json.return_value = {
            "methodResponses": [
                ["Email/import", {"created": {"msg1": {"id": "email-456"}}}, "call-0"]
            ]
        }

        mock_post.side_effect = [upload_response, import_response]

        target = self._create_connected_target_with_mailboxes()
        target.import_message(b"Test", [])  # Empty labels

        # Check the import call mailboxIds
        import_call = mock_post.call_args_list[1]
        request_body = import_call[1]['json']
        mailbox_ids = request_body['methodCalls'][0][1]['emails']['msg1']['mailboxIds']
        assert mailbox_ids == {"mb-1": True}  # inbox

    @patch('korgalore.jmap_target.requests.post')
    def test_import_multiple_folders(self, mock_post: MagicMock) -> None:
        """Message can be imported to multiple folders."""
        upload_response = MagicMock()
        upload_response.json.return_value = {"blobId": "blob-123"}

        import_response = MagicMock()
        import_response.json.return_value = {
            "methodResponses": [
                ["Email/import", {"created": {"msg1": {"id": "email-456"}}}, "call-0"]
            ]
        }

        mock_post.side_effect = [upload_response, import_response]

        target = self._create_connected_target_with_mailboxes()
        target.import_message(b"Test", ["inbox", "archive"])

        import_call = mock_post.call_args_list[1]
        request_body = import_call[1]['json']
        mailbox_ids = request_body['methodCalls'][0][1]['emails']['msg1']['mailboxIds']
        assert mailbox_ids == {"mb-1": True, "mb-3": True}

    @patch('korgalore.jmap_target.requests.post')
    def test_import_already_exists(self, mock_post: MagicMock) -> None:
        """Already-existing message is handled gracefully."""
        upload_response = MagicMock()
        upload_response.json.return_value = {"blobId": "blob-123"}

        import_response = MagicMock()
        import_response.json.return_value = {
            "methodResponses": [
                ["Email/import", {
                    "notCreated": {
                        "msg1": {"type": "alreadyExists", "existingId": "existing-789"}
                    }
                }, "call-0"]
            ]
        }

        mock_post.side_effect = [upload_response, import_response]

        target = self._create_connected_target_with_mailboxes()
        result = target.import_message(b"Test", ["inbox"])

        assert result == {"id": "existing-789"}

    @patch('korgalore.jmap_target.requests.post')
    def test_import_failure(self, mock_post: MagicMock) -> None:
        """Import failure raises RemoteError."""
        upload_response = MagicMock()
        upload_response.json.return_value = {"blobId": "blob-123"}

        import_response = MagicMock()
        import_response.json.return_value = {
            "methodResponses": [
                ["Email/import", {
                    "notCreated": {
                        "msg1": {"type": "invalidEmail", "description": "Bad message"}
                    }
                }, "call-0"]
            ]
        }

        mock_post.side_effect = [upload_response, import_response]

        target = self._create_connected_target_with_mailboxes()

        with pytest.raises(RemoteError) as exc_info:
            target.import_message(b"Test", ["inbox"])
        assert "Email/import failed" in str(exc_info.value)

    @patch('korgalore.jmap_target.requests.post')
    def test_import_unexpected_response(self, mock_post: MagicMock) -> None:
        """Unexpected response raises RemoteError."""
        upload_response = MagicMock()
        upload_response.json.return_value = {"blobId": "blob-123"}

        import_response = MagicMock()
        import_response.json.return_value = {
            "methodResponses": []  # Empty responses
        }

        mock_post.side_effect = [upload_response, import_response]

        target = self._create_connected_target_with_mailboxes()

        with pytest.raises(RemoteError) as exc_info:
            target.import_message(b"Test", ["inbox"])
        assert "Unexpected JMAP response" in str(exc_info.value)

    @patch('korgalore.jmap_target.requests.post')
    def test_import_request_failure(self, mock_post: MagicMock) -> None:
        """Request failure during import raises RemoteError."""
        upload_response = MagicMock()
        upload_response.json.return_value = {"blobId": "blob-123"}

        mock_post.side_effect = [upload_response, requests.RequestException("Network error")]

        target = self._create_connected_target_with_mailboxes()

        with pytest.raises(RemoteError) as exc_info:
            target.import_message(b"Test", ["inbox"])
        assert "Failed to import message" in str(exc_info.value)


class TestJmapTargetListLabels:
    """Tests for JmapTarget list_labels method."""

    @patch('korgalore.jmap_target.requests.post')
    def test_list_labels(self, mock_post: MagicMock) -> None:
        """list_labels returns name and id for each mailbox."""
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_MAILBOXES_RESPONSE
        mock_post.return_value = mock_response

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )
        target.session = SAMPLE_SESSION
        target.account_id = "acc-123"
        target.api_url = "https://api.example.com/jmap/api/"
        target.upload_url = "https://api.example.com/jmap/upload/acc-123/"

        labels = target.list_labels()

        assert len(labels) == 3
        assert labels[0] == {"name": "Inbox", "id": "mb-1"}
        assert labels[1] == {"name": "Sent", "id": "mb-2"}
        assert labels[2] == {"name": "Archive", "id": "mb-3"}


class TestJmapTargetEdgeCases:
    """Edge case tests."""

    def test_token_takes_precedence_over_file(self, tmp_path: Path) -> None:
        """Direct token takes precedence over token_file."""
        token_file = tmp_path / "token.txt"
        token_file.write_text("file_token")

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="direct_token",
            token_file=str(token_file)
        )
        assert target.token == "direct_token"

    @patch('korgalore.jmap_target.requests.post')
    def test_mailbox_role_mapping(self, mock_post: MagicMock) -> None:
        """Mailboxes can be found by role as well as name."""
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_MAILBOXES_RESPONSE
        mock_post.return_value = mock_response

        target = JmapTarget(
            identifier="test",
            server="https://api.example.com",
            username="user@example.com",
            token="token"
        )
        target.session = SAMPLE_SESSION
        target.account_id = "acc-123"
        target.api_url = "https://api.example.com/jmap/api/"
        target.upload_url = "https://api.example.com/jmap/upload/acc-123/"

        # Use role names instead of folder names
        result = target.translate_folders(["inbox", "sent"])
        assert result == ["mb-1", "mb-2"]
