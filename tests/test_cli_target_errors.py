"""Tests for how CLI commands report a target that cannot connect.

A target can fail to connect for ordinary reasons: a token helper exits
with an error, or the program is not installed. The user must get an
error message and a non-zero exit, not a Python traceback. A Gmail
target with a token_command is used here because it lets us make the
connection fail by mocking a single subprocess call.
"""

import subprocess
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from korgalore import RemoteError
from korgalore.cli import auth, perform_yank, yank
from tests.digest_helpers import make_ctx

HELPER_FAILS = subprocess.CompletedProcess(args=['get-token'], returncode=1, stdout='', stderr='command failed')


def ctx_obj() -> dict[str, Any]:
    return {
        'config': {'targets': {'test': {'type': 'gmail', 'token_command': 'get-token'}}},
        'targets': {},
        'hide_bar': True,
    }


@pytest.mark.parametrize(
    ('command', 'args', 'helper_result'),
    [
        (auth, ['test'], HELPER_FAILS),
        (yank, ['--target', 'test', '--thread', 'some-id'], HELPER_FAILS),
        (yank, ['--target', 'test', 'some-msgid'], FileNotFoundError('No such file')),
    ],
    ids=['auth', 'yank-thread', 'yank-message-missing-helper'],
)
def test_connect_failure_aborts_cleanly(command: Any, args: list[str], helper_result: object) -> None:
    """A failing or missing token helper ends the command with click.Abort."""
    with (
        patch('korgalore.google_creds.subprocess.run') as mock_run,
        patch('korgalore.cli.get_lore_node') as mock_node,
    ):
        if isinstance(helper_result, Exception):
            mock_run.side_effect = helper_result
        else:
            mock_run.return_value = helper_result
        mock_node.return_value.get_mbox_by_msgid.return_value = b'From mboxrd@z Thu Jan 1 00:00:00 1970\n\n'
        mock_node.return_value.get_message_by_msgid.return_value = b'From: a@b\nSubject: x\n\nBody\n'

        res = CliRunner().invoke(command, args, obj=ctx_obj())

    # click.Abort turns into SystemExit(1); any other exception is a traceback.
    assert isinstance(res.exception, SystemExit)
    assert res.exit_code == 1
    mock_run.assert_called_once()


def test_auth_proves_the_token_works() -> None:
    """'kgl auth' lists the labels, so a token Gmail would reject fails here, not at the next pull."""
    with (
        patch('korgalore.google_creds.subprocess.run') as mock_run,
        patch('korgalore.gmail_target.GmailTarget.list_labels') as mock_list_labels,
        patch('korgalore.gmail_target.build'),
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=['get-token'], returncode=0, stdout='valid-token\n', stderr=''
        )
        res = CliRunner().invoke(auth, ['test'], obj=ctx_obj())

    assert res.exit_code == 0
    mock_list_labels.assert_called_once()


def test_perform_yank_connect_failure_raises_remote_error() -> None:
    """The GUI calls perform_yank() directly and needs an exception it can show, not click.Abort."""
    with patch('korgalore.google_creds.subprocess.run', return_value=HELPER_FAILS), pytest.raises(RemoteError):
        perform_yank(make_ctx(ctx_obj()), 'test', 'some-msgid')
