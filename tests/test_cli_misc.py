"""Miscellaneous CLI helper tests: key display and progress bar suppression.

Progress bar suppression across Click versions:

korgalore hides its progress bars by handing Click a throwaway stream rather
than by passing ``hidden=True``, which only exists in Click 8.3.0 and newer.
These tests pin the behaviour we rely on, so that a Click release changing how
a non-terminal output stream is treated fails here instead of dumping bar
noise into somebody's debug log.
"""

import io
import subprocess
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from korgalore import RemoteError, format_key_for_display
from korgalore.cli import auth, perform_yank, progress_file, yank
from tests.digest_helpers import make_ctx


@pytest.mark.parametrize(
    ('key', 'expected'),
    [
        ('lei:/home/user/foo/bar/queryname', 'lei:queryname'),
        ('lei:queryname', 'lei:queryname'),
        ('lei:/', 'lei:'),
        ('lei:/path/to/query/', 'lei:query'),
        # Lore keys are already normalized to the list name
        ('lkml', 'lkml'),
        ('ksummit', 'ksummit'),
        ('my-delivery', 'my-delivery'),
        ('https://example.com/feed', 'https://example.com/feed'),
        (None, ''),
    ],
    ids=[
        'lei-path',
        'lei-short',
        'lei-empty-component',
        'lei-trailing-slash',
        'lore-lkml',
        'lore-ksummit',
        'other-name',
        'other-url',
        'none',
    ],
)
def test_format_key_for_display(key: str | None, expected: str) -> None:
    assert format_key_for_display(key) == expected


def test_progress_file_visible() -> None:
    # None means "use Click's default", i.e. stdout.
    assert progress_file(False) is None


def test_progress_file_hidden() -> None:
    stream = progress_file(True)
    assert isinstance(stream, io.StringIO)
    # A fresh stream every call: two concurrent bars must not share one.
    assert progress_file(True) is not stream


def test_hidden_bar_writes_nothing_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    items = list(range(5))
    stream = progress_file(True)
    with click.progressbar(items, label='Hiding', show_pos=True, file=stream) as bar:
        seen = list(bar)

    # The iteration must still work -- hiding the bar is not skipping the work.
    assert seen == items
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == ''
    # Click's non-terminal path echoes the label once. Directing it at our own
    # stream is what keeps it off the user's terminal, so check it landed here.
    assert isinstance(stream, io.StringIO)
    assert 'Hiding' in stream.getvalue()


def test_auth_token_command_failure_aborts_cleanly() -> None:
    runner = CliRunner()
    ctx_obj = {
        'config': {'targets': {'test': {'type': 'gmail', 'token_command': 'get-token'}}},
        'targets': {},
    }
    with patch('korgalore.google_creds.subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=['get-token'], returncode=1, stdout='', stderr='command failed'
        )
        res = runner.invoke(auth, ['test'], obj=ctx_obj)
        assert res.exit_code == 1
        assert res.exception is not None
        assert isinstance(res.exception, SystemExit)
        mock_run.assert_called_once()


def test_auth_token_command_verifies_labels() -> None:
    runner = CliRunner()
    ctx_obj = {
        'config': {'targets': {'test': {'type': 'gmail', 'token_command': 'get-token'}}},
        'targets': {},
    }
    with (
        patch('korgalore.google_creds.subprocess.run') as mock_run,
        patch('korgalore.gmail_target.GmailTarget.list_labels') as mock_list_labels,
        patch('korgalore.gmail_target.build'),
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=['get-token'], returncode=0, stdout='valid-token\n', stderr=''
        )
        res = runner.invoke(auth, ['test'], obj=ctx_obj)
        assert res.exit_code == 0
        mock_run.assert_called_once()
        mock_list_labels.assert_called_once()


def test_yank_token_command_failure_aborts_cleanly() -> None:
    runner = CliRunner()
    ctx_obj = {
        'config': {'targets': {'test': {'type': 'gmail', 'token_command': 'get-token'}}},
        'targets': {},
        'hide_bar': True,
    }
    with (
        patch('korgalore.google_creds.subprocess.run') as mock_run,
        patch('korgalore.cli.get_lore_node') as mock_node,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=['get-token'], returncode=1, stdout='', stderr='command failed'
        )
        mock_node.return_value.get_mbox_by_msgid.return_value = b'From mboxrd@z Thu Jan 1 00:00:00 1970\n\n'
        res = runner.invoke(yank, ['--target', 'test', '--thread', 'some-id'], obj=ctx_obj)
        assert res.exit_code == 1
        assert res.exception is not None
        assert isinstance(res.exception, SystemExit)
        mock_run.assert_called_once()


def test_yank_single_message_command_oserror_aborts_cleanly() -> None:
    runner = CliRunner()
    ctx_obj = {
        'config': {'targets': {'test': {'type': 'gmail', 'token_command': '/nonexistent/get-token'}}},
        'targets': {},
        'hide_bar': True,
    }
    with (
        patch('korgalore.google_creds.subprocess.run') as mock_run,
        patch('korgalore.cli.get_lore_node') as mock_node,
    ):
        mock_run.side_effect = FileNotFoundError('No such file')
        mock_node.return_value.get_message_by_msgid.return_value = b'From: a@b\nSubject: x\n\nBody\n'
        res = runner.invoke(yank, ['--target', 'test', 'some-msgid'], obj=ctx_obj)
        assert res.exit_code == 1
        assert res.exception is not None
        assert isinstance(res.exception, SystemExit)
        mock_run.assert_called_once()


def test_perform_yank_connection_failure_raises_remote_error() -> None:
    ctx = make_ctx(
        {
            'config': {'targets': {'test': {'type': 'gmail', 'token_command': 'get-token'}}},
            'targets': {},
            'hide_bar': True,
        }
    )
    with patch('korgalore.google_creds.subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=['get-token'], returncode=1, stdout='', stderr='command failed'
        )
        with pytest.raises(RemoteError):
            perform_yank(ctx, 'test', 'some-msgid')
