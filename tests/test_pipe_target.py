"""Tests for PipeTarget message delivery."""

import pytest
from unittest.mock import patch, MagicMock

from korgalore import ConfigurationError, DeliveryError
from korgalore.pipe_target import PipeTarget


class TestPipeTargetInit:
    """Tests for PipeTarget initialization and validation."""

    def test_valid_simple_command(self) -> None:
        """Simple command without arguments."""
        target = PipeTarget("test", "cat")
        assert target.identifier == "test"
        assert target.command == "cat"
        assert target.command_args == ["cat"]

    def test_valid_command_with_args(self) -> None:
        """Command with multiple arguments."""
        target = PipeTarget("test", "mail -s 'Test Subject' user@example.com")
        assert target.command_args == ["mail", "-s", "Test Subject", "user@example.com"]

    def test_valid_command_with_path(self) -> None:
        """Command with full path."""
        target = PipeTarget("test", "/usr/bin/sendmail -t")
        assert target.command_args == ["/usr/bin/sendmail", "-t"]

    def test_empty_command_raises(self) -> None:
        """Empty command string raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            PipeTarget("test", "")
        assert "requires a command" in str(exc_info.value)

    def test_whitespace_only_command_raises(self) -> None:
        """Whitespace-only command raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            PipeTarget("test", "   ")
        assert "requires a non-empty command" in str(exc_info.value)

    def test_invalid_shlex_raises(self) -> None:
        """Invalid shell quoting raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            PipeTarget("test", "echo 'unclosed quote")
        assert "Invalid command" in str(exc_info.value)

    def test_identifier_preserved(self) -> None:
        """Identifier is stored correctly."""
        target = PipeTarget("my-pipe-target", "cat")
        assert target.identifier == "my-pipe-target"


class TestPipeTargetConnect:
    """Tests for PipeTarget connect method."""

    def test_connect_succeeds(self) -> None:
        """Connect is a no-op that doesn't raise."""
        target = PipeTarget("test", "cat")
        target.connect()  # Should not raise

    def test_connect_logs_command(self, caplog: pytest.LogCaptureFixture) -> None:
        """Connect logs the configured command."""
        import logging
        # Clear any handlers added by click-log from cli.py imports
        # and ensure propagation for caplog to capture
        korg_logger = logging.getLogger('korgalore')
        korg_logger.handlers.clear()
        korg_logger.propagate = True
        caplog.set_level(logging.DEBUG, logger='korgalore')
        target = PipeTarget("test", "/usr/bin/mycommand --flag")
        target.connect()
        assert "/usr/bin/mycommand --flag" in caplog.text


class TestPipeTargetImportMessage:
    """Tests for PipeTarget import_message method."""

    def test_successful_delivery(self) -> None:
        """Message piped successfully to command."""
        target = PipeTarget("test", "cat")
        raw_message = b"From: test@example.com\nSubject: Test\n\nBody"
        # RawMessage.as_binary() normalizes to CRLF
        expected_output = b"From: test@example.com\r\nSubject: Test\r\n\r\nBody"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            result = target.import_message(raw_message, [])

        assert result == 0
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["cat"]
        assert call_args[1]["input"] == expected_output
        assert call_args[1]["capture_output"] is True

    def test_labels_appended_as_args(self) -> None:
        """Labels are appended as command line arguments."""
        target = PipeTarget("test", "deliver --maildir")
        raw_message = b"test message"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            target.import_message(raw_message, ["inbox", "important"])

        call_args = mock_run.call_args[0][0]
        assert call_args == ["deliver", "--maildir", "inbox", "important"]

    def test_empty_labels(self) -> None:
        """Empty labels list works correctly."""
        target = PipeTarget("test", "cat -v")
        raw_message = b"test"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            target.import_message(raw_message, [])

        call_args = mock_run.call_args[0][0]
        assert call_args == ["cat", "-v"]

    def test_nonzero_exit_raises_delivery_error(self) -> None:
        """Non-zero exit code raises DeliveryError with stderr."""
        target = PipeTarget("test", "failing-command")
        raw_message = b"test"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout=b"",
                stderr=b"Something went wrong"
            )
            with pytest.raises(DeliveryError) as exc_info:
                target.import_message(raw_message, [])

        assert "exit code 1" in str(exc_info.value)
        assert "Something went wrong" in str(exc_info.value)

    def test_nonzero_exit_with_unicode_stderr(self) -> None:
        """Non-zero exit handles non-UTF8 stderr gracefully."""
        target = PipeTarget("test", "failing-command")
        raw_message = b"test"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=2,
                stdout=b"",
                stderr=b"Error with \xff\xfe invalid bytes"
            )
            with pytest.raises(DeliveryError) as exc_info:
                target.import_message(raw_message, [])

        assert "exit code 2" in str(exc_info.value)

    def test_command_not_found_raises_delivery_error(self) -> None:
        """FileNotFoundError raises DeliveryError."""
        target = PipeTarget("test", "nonexistent-command")
        raw_message = b"test"

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            with pytest.raises(DeliveryError) as exc_info:
                target.import_message(raw_message, [])

        assert "not found" in str(exc_info.value)
        assert "nonexistent-command" in str(exc_info.value)

    def test_other_exception_raises_delivery_error(self) -> None:
        """Other exceptions are wrapped in DeliveryError."""
        target = PipeTarget("test", "some-command")
        raw_message = b"test"

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = OSError("Permission denied")
            with pytest.raises(DeliveryError) as exc_info:
                target.import_message(raw_message, [])

        assert "Permission denied" in str(exc_info.value)

    def test_delivery_error_not_wrapped(self) -> None:
        """DeliveryError from non-zero exit is not double-wrapped."""
        target = PipeTarget("test", "cmd")
        raw_message = b"test"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=42,
                stdout=b"",
                stderr=b"original error"
            )
            with pytest.raises(DeliveryError) as exc_info:
                target.import_message(raw_message, [])

        # Should contain the original message, not wrapped
        assert "exit code 42" in str(exc_info.value)
        assert "original error" in str(exc_info.value)

    def test_large_message(self) -> None:
        """Large messages are handled correctly."""
        target = PipeTarget("test", "cat")
        raw_message = b"X" * 1024 * 1024  # 1MB message

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            result = target.import_message(raw_message, [])

        assert result == 0
        assert mock_run.call_args[1]["input"] == raw_message

    def test_binary_message_content(self) -> None:
        """Binary content in message is processed through as_binary()."""
        target = PipeTarget("test", "cat")
        # Use message without newlines to avoid CRLF transformation
        raw_message = bytes([b for b in range(256) if b != 0x0a])

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            target.import_message(raw_message, [])

        assert mock_run.call_args[1]["input"] == raw_message


class TestPipeTargetIntegration:
    """Integration tests using real commands."""

    def test_cat_returns_success(self) -> None:
        """Real cat command succeeds."""
        target = PipeTarget("test", "cat")
        raw_message = b"Hello, World!"
        result = target.import_message(raw_message, [])
        assert result == 0

    def test_false_command_fails(self) -> None:
        """Real false command returns non-zero."""
        target = PipeTarget("test", "false")
        with pytest.raises(DeliveryError) as exc_info:
            target.import_message(b"test", [])
        assert "exit code 1" in str(exc_info.value)

    def test_true_command_succeeds(self) -> None:
        """Real true command returns zero."""
        target = PipeTarget("test", "true")
        result = target.import_message(b"test", [])
        assert result == 0

    def test_nonexistent_command_fails(self) -> None:
        """Non-existent command raises DeliveryError."""
        target = PipeTarget("test", "/nonexistent/path/to/command")
        with pytest.raises(DeliveryError) as exc_info:
            target.import_message(b"test", [])
        assert "not found" in str(exc_info.value)
