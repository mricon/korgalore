"""Tests for PipeTarget message delivery."""

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest

from korgalore import ConfigurationError, DeliveryError
from korgalore.cli import get_target
from korgalore.pipe_target import PipeTarget
from tests.digest_helpers import make_ctx


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
        ('command', 'options', 'message'),
        [
            ('', {}, 'requires a command'),
            ('   ', {}, 'requires a non-empty command'),
            ("echo 'unclosed quote", {}, 'Invalid command'),
            ('cat', {'line_endings': 'dos'}, r"'test'.*'dos'.*'crlf', 'lf'"),
            ('cat', {'line_endings': 'LF'}, r"'test'.*'LF'.*'crlf', 'lf'"),
        ],
        ids=[
            'empty',
            'whitespace-only',
            'bad-quoting',
            'unknown-line-endings',
            'uppercase-line-endings',
        ],
    )
    def test_invalid_config_raises(self, command: str, options: dict[str, str], message: str) -> None:
        """Bad settings fail when the target is built, naming the target and what is allowed.

        A typo in line_endings must not quietly fall back to the default,
        or a pipe would get line endings its owner did not ask for.
        """
        with pytest.raises(ConfigurationError, match=message):
            PipeTarget('test', command, **options)


class TestPipeTargetFromConfig:
    """Tests for building a PipeTarget from the configuration file."""

    @staticmethod
    def build(settings: dict[str, Any]) -> Any:
        config = {'targets': {'test': {'type': 'pipe', 'command': 'cat', **settings}}}
        return get_target(make_ctx({'config': config, 'targets': {}}), 'test')

    @pytest.mark.parametrize(
        ('settings', 'crlf'),
        [({}, True), ({'line_endings': 'crlf'}, True), ({'line_endings': 'lf'}, False)],
        ids=['default', 'explicit-crlf', 'lf'],
    )
    def test_line_endings_setting_reaches_the_target(self, settings: dict[str, Any], crlf: bool) -> None:
        """The line_endings key in the config decides what the target sends.

        Without this, the option could be documented and validated but
        never read, and every pipe would quietly keep getting CRLF.
        """
        assert self.build(settings).crlf is crlf

    def test_invalid_line_endings_aborts(self) -> None:
        """A wrong value, such as a TOML boolean, stops the command instead of delivering with a guess."""
        with pytest.raises(click.Abort):
            self.build({'line_endings': True})


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

    @pytest.mark.parametrize(
        ('options', 'expected_output'),
        [
            pytest.param({}, b'From: test@example.com\r\nSubject: Test\r\n\r\nBody', id='default-crlf'),
            pytest.param({'line_endings': 'lf'}, b'From: test@example.com\nSubject: Test\n\nBody', id='lf'),
        ],
    )
    def test_successful_delivery(self, options: dict[str, str], expected_output: bytes) -> None:
        """Message is piped to the command with the configured line endings.

        CRLF stays the default so existing pipe configs see the same bytes.
        """
        target = PipeTarget('test', 'cat', **options)
        raw_message = b'From: test@example.com\nSubject: Test\n\nBody'

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
