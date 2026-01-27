"""Tests for ImapTarget message delivery."""

import imaplib
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from korgalore import ConfigurationError, RemoteError
from korgalore.imap_target import ImapTarget


class TestImapTargetInit:
    """Tests for ImapTarget initialization and validation."""

    def test_valid_config_with_password(self) -> None:
        """Valid configuration with direct password."""
        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret123"
        )
        assert target.identifier == "test"
        assert target.server == "imap.example.com"
        assert target.username == "user@example.com"
        assert target.password == "secret123"
        assert target.folder == "INBOX"
        assert target.timeout == 60

    def test_valid_config_with_password_file(self, tmp_path: Path) -> None:
        """Valid configuration with password file."""
        pw_file = tmp_path / "password.txt"
        pw_file.write_text("file_secret\n")

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password_file=str(pw_file)
        )
        assert target.password == "file_secret"

    def test_password_file_strips_whitespace(self, tmp_path: Path) -> None:
        """Password file content is stripped of whitespace."""
        pw_file = tmp_path / "password.txt"
        pw_file.write_text("  secret_with_spaces  \n\n")

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password_file=str(pw_file)
        )
        assert target.password == "secret_with_spaces"

    def test_password_file_with_tilde(self, tmp_path: Path) -> None:
        """Password file path with tilde is expanded."""
        pw_file = tmp_path / "password.txt"
        pw_file.write_text("secret")

        with patch.object(Path, "expanduser", return_value=pw_file):
            target = ImapTarget(
                identifier="test",
                server="imap.example.com",
                username="user@example.com",
                password_file="~/password.txt"
            )
        assert target.password == "secret"

    def test_custom_folder(self) -> None:
        """Custom folder can be specified."""
        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret",
            folder="Archive/2024"
        )
        assert target.folder == "Archive/2024"

    def test_custom_timeout(self) -> None:
        """Custom timeout can be specified."""
        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret",
            timeout=120
        )
        assert target.timeout == 120

    def test_missing_server_raises(self) -> None:
        """Empty server raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            ImapTarget(
                identifier="test",
                server="",
                username="user@example.com",
                password="secret"
            )
        assert "No server specified" in str(exc_info.value)

    def test_missing_username_raises(self) -> None:
        """Empty username raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            ImapTarget(
                identifier="test",
                server="imap.example.com",
                username="",
                password="secret"
            )
        assert "No username specified" in str(exc_info.value)

    def test_missing_password_raises(self) -> None:
        """Missing both password and password_file raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            ImapTarget(
                identifier="test",
                server="imap.example.com",
                username="user@example.com"
            )
        assert "No password or password_file specified" in str(exc_info.value)

    def test_nonexistent_password_file_raises(self) -> None:
        """Non-existent password file raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            ImapTarget(
                identifier="test",
                server="imap.example.com",
                username="user@example.com",
                password_file="/nonexistent/path/password.txt"
            )
        assert "Password file not found" in str(exc_info.value)

    def test_imap_not_connected_initially(self) -> None:
        """IMAP connection is None before connect() is called."""
        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        assert target.imap is None


class TestImapTargetConnect:
    """Tests for ImapTarget connect method."""

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_connect_success(self, mock_imap_class: MagicMock) -> None:
        """Successful connection and authentication."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [b'Logged in'])
        mock_imap.select.return_value = ('OK', [b'1'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        mock_imap_class.assert_called_once_with("imap.example.com", timeout=60)
        mock_imap.login.assert_called_once_with("user@example.com", "secret")
        mock_imap.select.assert_called_once_with("INBOX", readonly=True)
        assert target.imap is mock_imap

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_connect_custom_folder(self, mock_imap_class: MagicMock) -> None:
        """Connection verifies custom folder."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'5'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret",
            folder="Archive/Important"
        )
        target.connect()

        mock_imap.select.assert_called_once_with("Archive/Important", readonly=True)

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_connect_custom_timeout(self, mock_imap_class: MagicMock) -> None:
        """Connection uses custom timeout."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret",
            timeout=300
        )
        target.connect()

        mock_imap_class.assert_called_once_with("imap.example.com", timeout=300)

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_connect_auth_failure(self, mock_imap_class: MagicMock) -> None:
        """Authentication failure raises RemoteError."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.side_effect = imaplib.IMAP4.error("Invalid credentials")

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="wrong_password"
        )

        with pytest.raises(RemoteError) as exc_info:
            target.connect()
        assert "authentication failed" in str(exc_info.value)
        assert "imap.example.com" in str(exc_info.value)

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_connect_folder_not_found_status(self, mock_imap_class: MagicMock) -> None:
        """Folder not found (bad status) raises ConfigurationError."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('NO', [b'Folder not found'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret",
            folder="NonExistent"
        )

        with pytest.raises(ConfigurationError) as exc_info:
            target.connect()
        assert "does not exist" in str(exc_info.value)
        assert "NonExistent" in str(exc_info.value)

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_connect_folder_not_found_exception(self, mock_imap_class: MagicMock) -> None:
        """Folder not found (exception) raises ConfigurationError."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.side_effect = imaplib.IMAP4.error("Folder does not exist")

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret",
            folder="BadFolder"
        )

        with pytest.raises(ConfigurationError) as exc_info:
            target.connect()
        assert "does not exist" in str(exc_info.value)

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_connect_idempotent(self, mock_imap_class: MagicMock) -> None:
        """Multiple connect() calls don't reconnect."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()
        target.connect()
        target.connect()

        # Should only connect once
        assert mock_imap_class.call_count == 1


class TestImapTargetImportMessage:
    """Tests for ImapTarget import_message method."""

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_success(self, mock_imap_class: MagicMock) -> None:
        """Successful message import."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'[APPENDUID 1234 5678]'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        result = target.import_message(b"From: test@example.com\r\nSubject: Test\r\n\r\nBody", [])

        assert result == [b'[APPENDUID 1234 5678]']
        mock_imap.append.assert_called_once()

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_to_correct_folder(self, mock_imap_class: MagicMock) -> None:
        """Message is appended to correct folder."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret",
            folder="Archive"
        )
        target.connect()
        target.import_message(b"Test message", [])

        call_args = mock_imap.append.call_args[0]
        assert call_args[0] == "Archive"

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_crlf_normalization_unix(self, mock_imap_class: MagicMock) -> None:
        """Unix line endings (LF) are converted to CRLF."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        # Message with Unix LF endings
        target.import_message(b"From: a@b.com\nTo: c@d.com\n\nBody\nLine2", [])

        call_args = mock_imap.append.call_args[0]
        normalized = call_args[3]
        assert normalized == b"From: a@b.com\r\nTo: c@d.com\r\n\r\nBody\r\nLine2"

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_crlf_normalization_mixed(self, mock_imap_class: MagicMock) -> None:
        """Mixed line endings are normalized to CRLF."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        # Message with mixed endings
        target.import_message(b"Line1\r\nLine2\nLine3\r\nLine4\n", [])

        call_args = mock_imap.append.call_args[0]
        normalized = call_args[3]
        assert normalized == b"Line1\r\nLine2\r\nLine3\r\nLine4\r\n"

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_crlf_already_normalized(self, mock_imap_class: MagicMock) -> None:
        """Already-normalized CRLF messages are not double-converted."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        # Already has CRLF
        target.import_message(b"From: a@b.com\r\nTo: c@d.com\r\n\r\nBody", [])

        call_args = mock_imap.append.call_args[0]
        normalized = call_args[3]
        # Should remain the same, not become \r\r\n
        assert normalized == b"From: a@b.com\r\nTo: c@d.com\r\n\r\nBody"

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_labels_ignored(self, mock_imap_class: MagicMock) -> None:
        """Labels parameter is accepted but ignored."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        # Should not raise with labels
        result = target.import_message(b"Test", ["INBOX", "Important", "Custom"])
        assert result is not None

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_auto_connects(self, mock_imap_class: MagicMock) -> None:
        """import_message auto-connects if not connected."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        # Don't call connect() explicitly

        target.import_message(b"Test message", [])

        # Should have connected
        mock_imap.login.assert_called_once()
        mock_imap.append.assert_called_once()

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_append_failure_status(self, mock_imap_class: MagicMock) -> None:
        """APPEND returning non-OK status raises RemoteError."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('NO', [b'Quota exceeded'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        with pytest.raises(RemoteError) as exc_info:
            target.import_message(b"Test", [])
        assert "APPEND failed" in str(exc_info.value)

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_append_exception(self, mock_imap_class: MagicMock) -> None:
        """APPEND raising exception raises RemoteError."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.side_effect = imaplib.IMAP4.error("Server error")

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        with pytest.raises(RemoteError) as exc_info:
            target.import_message(b"Test", [])
        assert "Failed to append" in str(exc_info.value)

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_connection_error(self, mock_imap_class: MagicMock) -> None:
        """Connection error during import raises RemoteError."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.side_effect = OSError("Connection reset")

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        with pytest.raises(RemoteError) as exc_info:
            target.import_message(b"Test", [])
        assert "delivery failed" in str(exc_info.value)

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_multiple_messages(self, mock_imap_class: MagicMock) -> None:
        """Multiple messages can be imported."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        for i in range(5):
            target.import_message(f"Message {i}".encode(), [])

        assert mock_imap.append.call_count == 5


class TestImapTargetEdgeCases:
    """Edge case and integration-style tests."""

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_binary_message_content(self, mock_imap_class: MagicMock) -> None:
        """Binary content in message is preserved."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        # Binary content (no newlines to normalize)
        binary_content = bytes(range(256))
        target.import_message(binary_content, [])

        call_args = mock_imap.append.call_args[0]
        # Binary content should pass through (with \n -> \r\n conversion)
        assert call_args[3] is not None

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_empty_message(self, mock_imap_class: MagicMock) -> None:
        """Empty message is handled."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        result = target.import_message(b"", [])
        assert result is not None

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_large_message(self, mock_imap_class: MagicMock) -> None:
        """Large messages are handled."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        # 1MB message
        large_body = b"X" * 1024 * 1024
        target.import_message(b"Subject: Large\r\n\r\n" + large_body, [])

        mock_imap.append.assert_called_once()

    def test_password_takes_precedence_over_file(self, tmp_path: Path) -> None:
        """Direct password takes precedence over password_file."""
        pw_file = tmp_path / "password.txt"
        pw_file.write_text("file_password")

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="direct_password",
            password_file=str(pw_file)
        )
        assert target.password == "direct_password"

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_append_flags_and_datetime(self, mock_imap_class: MagicMock) -> None:
        """APPEND is called with empty flags and datetime."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()
        target.import_message(b"Test", [])

        call_args = mock_imap.append.call_args[0]
        # folder, flags, datetime, message
        assert call_args[0] == "INBOX"  # folder
        assert call_args[1] == ''  # flags (empty = unread)
        assert call_args[2] == ''  # datetime (empty = current time)


class TestImapTargetOAuth2:
    """Tests for ImapTarget OAuth2 authentication."""

    def test_oauth2_uses_default_client_id(self, tmp_path: Path) -> None:
        """OAuth2 uses default client_id when not specified."""
        from korgalore.oauth2_imap import DEFAULT_CLIENT_ID

        target = ImapTarget(
            identifier="test",
            server="outlook.office365.com",
            username="user@company.com",
            auth_type="oauth2",
            token=str(tmp_path / "token.json")
        )
        assert target._oauth2_authenticator is not None
        assert target._oauth2_authenticator.client_id == DEFAULT_CLIENT_ID

    def test_oauth2_custom_client_id(self, tmp_path: Path) -> None:
        """OAuth2 configuration with custom client_id overrides default."""
        target = ImapTarget(
            identifier="test",
            server="outlook.office365.com",
            username="user@company.com",
            auth_type="oauth2",
            client_id="custom-client-id",
            token=str(tmp_path / "token.json")
        )
        assert target.auth_type == "oauth2"
        assert target._oauth2_authenticator is not None
        assert target._oauth2_authenticator.client_id == "custom-client-id"
        assert target.password is None

    def test_oauth2_custom_tenant(self, tmp_path: Path) -> None:
        """OAuth2 configuration with custom tenant."""
        target = ImapTarget(
            identifier="test",
            server="outlook.office365.com",
            username="user@company.com",
            auth_type="oauth2",
            client_id="test-client-id",
            tenant="my-tenant-id",
            token=str(tmp_path / "token.json")
        )
        assert target._oauth2_authenticator is not None
        assert target._oauth2_authenticator.tenant == "my-tenant-id"

    def test_oauth2_needs_auth_initially(self, tmp_path: Path) -> None:
        """OAuth2 target needs_auth is True without token file."""
        target = ImapTarget(
            identifier="test",
            server="outlook.office365.com",
            username="user@company.com",
            auth_type="oauth2",
            client_id="test-client-id",
            token=str(tmp_path / "nonexistent-token.json")
        )
        assert target.needs_auth

    def test_oauth2_needs_auth_with_valid_token(self, tmp_path: Path) -> None:
        """OAuth2 target needs_auth is False with valid token."""
        import json
        from datetime import datetime, timezone

        token_file = tmp_path / "token.json"
        token_data = {
            "access_token": "valid",
            "refresh_token": "refresh",
            "expires_at": datetime.now(timezone.utc).timestamp() + 3600,
        }
        token_file.write_text(json.dumps(token_data))

        target = ImapTarget(
            identifier="test",
            server="outlook.office365.com",
            username="user@company.com",
            auth_type="oauth2",
            client_id="test-client-id",
            token=str(token_file)
        )
        assert not target.needs_auth

    def test_password_auth_needs_auth_always_false(self) -> None:
        """Password auth target always has needs_auth False."""
        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        assert not target.needs_auth

    def test_invalid_auth_type(self) -> None:
        """Invalid auth_type raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            ImapTarget(
                identifier="test",
                server="imap.example.com",
                username="user@example.com",
                auth_type="invalid"
            )
        assert "Invalid auth_type" in str(exc_info.value)

    def test_reauthenticate_password_raises(self) -> None:
        """reauthenticate() raises for password auth type."""
        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        with pytest.raises(ConfigurationError) as exc_info:
            target.reauthenticate()
        assert "not configured for OAuth2" in str(exc_info.value)

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_oauth2_connect_calls_authenticate(self, mock_imap_class: MagicMock,
                                                tmp_path: Path) -> None:
        """OAuth2 connection uses AUTHENTICATE instead of LOGIN."""
        import json
        from datetime import datetime, timezone

        token_file = tmp_path / "token.json"
        token_data = {
            "access_token": "test_access_token",
            "refresh_token": "refresh",
            "expires_at": datetime.now(timezone.utc).timestamp() + 3600,
        }
        token_file.write_text(json.dumps(token_data))

        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.authenticate.return_value = ('OK', [b'Success'])
        mock_imap.select.return_value = ('OK', [b'1'])

        target = ImapTarget(
            identifier="test",
            server="outlook.office365.com",
            username="user@company.com",
            auth_type="oauth2",
            client_id="test-client-id",
            token=str(token_file)
        )
        target.connect()

        # Verify authenticate was called with XOAUTH2
        mock_imap.authenticate.assert_called_once()
        call_args = mock_imap.authenticate.call_args[0]
        assert call_args[0] == 'XOAUTH2'

        # Verify login was NOT called
        mock_imap.login.assert_not_called()

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_oauth2_connect_auth_failure(self, mock_imap_class: MagicMock,
                                          tmp_path: Path) -> None:
        """OAuth2 authentication failure raises RemoteError."""
        import json
        from datetime import datetime, timezone

        token_file = tmp_path / "token.json"
        token_data = {
            "access_token": "invalid_token",
            "refresh_token": "refresh",
            "expires_at": datetime.now(timezone.utc).timestamp() + 3600,
        }
        token_file.write_text(json.dumps(token_data))

        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.authenticate.side_effect = imaplib.IMAP4.error(
            "AUTHENTICATE failed"
        )

        target = ImapTarget(
            identifier="test",
            server="outlook.office365.com",
            username="user@company.com",
            auth_type="oauth2",
            client_id="test-client-id",
            token=str(token_file)
        )

        with pytest.raises(RemoteError) as exc_info:
            target.connect()
        assert "XOAUTH2 authentication failed" in str(exc_info.value)


class TestImapTargetDisconnect:
    """Tests for ImapTarget disconnect method."""

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_disconnect_closes_connection(self, mock_imap_class: MagicMock) -> None:
        """disconnect() calls logout on the IMAP connection."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()
        assert target.imap is not None

        target.disconnect()

        mock_imap.logout.assert_called_once()
        assert target.imap is None

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_disconnect_handles_logout_error(self, mock_imap_class: MagicMock) -> None:
        """disconnect() handles errors during logout gracefully."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.logout.side_effect = imaplib.IMAP4.error("Connection lost")

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        # Should not raise
        target.disconnect()
        assert target.imap is None

    def test_disconnect_when_not_connected(self) -> None:
        """disconnect() is safe when not connected."""
        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        assert target.imap is None

        # Should not raise
        target.disconnect()
        assert target.imap is None

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_disconnect_allows_reconnect(self, mock_imap_class: MagicMock) -> None:
        """After disconnect(), connect() establishes a new connection."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()
        target.disconnect()

        assert target.imap is None

        target.connect()
        assert target.imap is not None
        assert mock_imap_class.call_count == 2


class TestImapTargetDeduplication:
    """Tests for IMAP message deduplication by Message-ID."""

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_check_message_exists_found(self, mock_imap_class: MagicMock) -> None:
        """Returns True when message exists in folder."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.search.return_value = ('OK', [b'42'])  # Found message 42

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        exists = target._check_message_exists("<test@example.com>")
        assert exists is True

        # Verify search was called correctly
        mock_imap.search.assert_called_once_with(
            None, 'HEADER', 'Message-ID', '<test@example.com>'
        )

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_check_message_exists_not_found(self, mock_imap_class: MagicMock) -> None:
        """Returns False when message does not exist."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.search.return_value = ('OK', [b''])  # No matches

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        exists = target._check_message_exists("<test@example.com>")
        assert exists is False

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_check_message_exists_error_returns_false(self, mock_imap_class: MagicMock) -> None:
        """Returns False on IMAP error (fail-open)."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.search.side_effect = imaplib.IMAP4.error("Search failed")

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        exists = target._check_message_exists("<test@example.com>")
        assert exists is False

    def test_check_message_exists_no_connection(self) -> None:
        """Returns False when not connected."""
        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        # Not connected
        exists = target._check_message_exists("<test@example.com>")
        assert exists is False

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_skips_duplicate(self, mock_imap_class: MagicMock) -> None:
        """Import is skipped when message already exists in folder."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.search.return_value = ('OK', [b'42'])  # Found existing message

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        raw_message = b"From: test@example.com\r\nMessage-ID: <dup@example.com>\r\n\r\nBody"
        result = target.import_message(raw_message, [])

        # Should return skipped result without appending
        assert result.get('skipped') is True
        mock_imap.append.assert_not_called()

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_proceeds_when_not_duplicate(self, mock_imap_class: MagicMock) -> None:
        """Import proceeds normally when message does not exist."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.search.return_value = ('OK', [b''])  # No matches
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        raw_message = b"From: test@example.com\r\nMessage-ID: <new@example.com>\r\n\r\nBody"
        result = target.import_message(raw_message, [])

        # Should proceed with append
        assert result == [b'Done']
        mock_imap.append.assert_called_once()

    @patch('korgalore.imap_target.imaplib.IMAP4_SSL')
    def test_import_proceeds_without_message_id(self, mock_imap_class: MagicMock) -> None:
        """Import proceeds without dedup check when Message-ID is missing."""
        mock_imap = MagicMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.login.return_value = ('OK', [])
        mock_imap.select.return_value = ('OK', [b'1'])
        mock_imap.append.return_value = ('OK', [b'Done'])

        target = ImapTarget(
            identifier="test",
            server="imap.example.com",
            username="user@example.com",
            password="secret"
        )
        target.connect()

        # Message without Message-ID header
        raw_message = b"From: test@example.com\r\n\r\nBody"
        target.import_message(raw_message, [])

        # Should proceed without search
        mock_imap.search.assert_not_called()
        mock_imap.append.assert_called_once()
