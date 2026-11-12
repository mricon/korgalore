"""Tests for PipeTarget message delivery."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from korgalore import ConfigurationError, DeliveryError
from korgalore.pipe_target import PipeTarget


class TestPipeTargetInit:
    """Tests for PipeTarget initialization and validation."""

    @pytest.mark.parametrize(
        ('command', 'expected_args'),
        [
            ('cat', ['cat']),
            (
                "mail -s 'Test Subject' user@example.com",
                ['mail', '-s', 'Test Subject', 'user@example.com'],
            ),
            ('/usr/bin/sendmail -t', ['/usr/bin/sendmail', '-t']),
            ('~/bin/filter', [f'{Path.home()}/bin/filter']),
        ],
        ids=['simple', 'quoted-args', 'full-path', 'home-dir'],
    )
    def test_valid_command(self, command: str, expected_args: list[str]) -> None:
        target = PipeTarget('my-pipe-target', command)
        assert target.identifier == 'my-pipe-target'
        assert target.command == command
        assert target.command_args == expected_args

    @pytest.mark.parametrize(
        ('command', 'message'),
        [
            ('', 'requires a command'),
            ('   ', 'requires a non-empty command'),
            ("echo 'unclosed quote", 'Invalid command'),
        ],
        ids=['empty', 'whitespace-only', 'bad-quoting'],
    )
    def test_invalid_command_raises(self, command: str, message: str) -> None:
        with pytest.raises(ConfigurationError, match=message):
            PipeTarget('test', command)


class TestPipeTargetConnect:
    """Tests for PipeTarget connect method."""

    def test_connect_logs_command(self, caplog: pytest.LogCaptureFixture) -> None:
        """Connect is a no-op that logs the configured command."""
        caplog.set_level(logging.DEBUG, logger='korgalore')
        target = PipeTarget('test', '/usr/bin/mycommand --flag')
        target.connect()
        assert '/usr/bin/mycommand --flag' in caplog.text


class TestPipeTargetImportMessage:
    """Tests for PipeTarget import_message method."""

    def test_successful_delivery(self) -> None:
        """Message is piped to the command, normalized to CRLF."""
        target = PipeTarget('test', 'cat')
        raw_message = b'From: test@example.com\nSubject: Test\n\nBody'
        expected_output = b'From: test@example.com\r\nSubject: Test\r\n\r\nBody'

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b'', stderr=b'')
            result = target.import_message(raw_message, [])

        assert result == 0
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ['cat']
        assert call_args[1]['input'] == expected_output
        assert call_args[1]['capture_output'] is True

    @pytest.mark.parametrize(
        ('command', 'labels', 'argv'),
        [
            (
                'deliver --maildir',
                ['inbox', 'important'],
                ['deliver', '--maildir', 'inbox', 'important'],
            ),
            ('cat -v', [], ['cat', '-v']),
        ],
        ids=['labels-appended', 'no-labels'],
    )
    def test_labels_become_arguments(self, command: str, labels: list[str], argv: list[str]) -> None:
        target = PipeTarget('test', command)
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b'', stderr=b'')
            target.import_message(b'test', labels)

        assert mock_run.call_args[0][0] == argv

    @pytest.mark.parametrize(
        ('returncode', 'stderr', 'expected'),
        [
            (1, b'Something went wrong', 'Something went wrong'),
            (2, b'Error with \xff\xfe invalid bytes', 'exit code 2'),
        ],
        ids=['ascii-stderr', 'non-utf8-stderr'],
    )
    def test_nonzero_exit_raises_delivery_error(self, returncode: int, stderr: bytes, expected: str) -> None:
        """Non-zero exit raises DeliveryError, tolerating undecodable stderr."""
        target = PipeTarget('test', 'failing-command')

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=returncode, stdout=b'', stderr=stderr)
            with pytest.raises(DeliveryError) as exc_info:
                target.import_message(b'test', [])

        assert f'exit code {returncode}' in str(exc_info.value)
        assert expected in str(exc_info.value)

    def test_other_exception_raises_delivery_error(self) -> None:
        """Other exceptions are wrapped in DeliveryError."""
        target = PipeTarget('test', 'some-command')

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = OSError('Permission denied')
            with pytest.raises(DeliveryError, match='Permission denied'):
                target.import_message(b'test', [])


class TestPipeTargetIntegration:
    """Integration tests using real commands."""

    def test_cat_returns_success(self) -> None:
        target = PipeTarget('test', 'cat')
        assert target.import_message(b'Hello, World!', []) == 0

    def test_false_command_fails(self) -> None:
        target = PipeTarget('test', 'false')
        with pytest.raises(DeliveryError, match='exit code 1'):
            target.import_message(b'test', [])

    def test_nonexistent_command_fails(self) -> None:
        target = PipeTarget('test', '/nonexistent/path/to/command')
        with pytest.raises(DeliveryError) as exc_info:
            target.import_message(b'test', [])
        assert 'not found' in str(exc_info.value)
        assert '/nonexistent/path/to/command' in str(exc_info.value)
