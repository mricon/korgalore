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
