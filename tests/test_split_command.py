"""Tests for split_command(), the parser behind every configured command.

Pipe targets, command summarizers and Gmail's token_command all run their
program without a shell. These tests pin down the small part of shell
behaviour that they promise anyway, so that the three options keep
behaving the same way.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from korgalore import split_command


@pytest.mark.parametrize(
    ('command', 'expected'),
    [
        ('cat', ['cat']),
        ("mail -s 'Test Subject' x@example.com", ['mail', '-s', 'Test Subject', 'x@example.com']),
        ('get-token --profile "$KGL_TEST_PROFILE"', ['get-token', '--profile', 'work account']),
        # A shell leaves unknown variables alone too, so a literal '$5' in a
        # prompt or a password survives.
        ('llm -s "costs $5 or $KGL_TEST_UNSET"', ['llm', '-s', 'costs $5 or $KGL_TEST_UNSET']),
        ('~/bin/get-token', ['{home}/bin/get-token']),
        ('~', ['{home}']),
        # Only the ~ part is replaced: the rest of the argument goes to the
        # program exactly as written, not normalised by pathlib.
        ('rsync ~/a//b/ host:', ['rsync', '{home}/a//b/', 'host:']),
        # A shell only expands ~ at the start of a word.
        ('cmd --file=~/x a~b', ['cmd', '--file=~/x', 'a~b']),
        # A shell leaves ~user alone when there is no such user.
        ('~kgl-no-such-user/x', ['~kgl-no-such-user/x']),
    ],
    ids=['simple', 'quoted', 'env-var', 'unset-var', 'tilde', 'bare-tilde', 'tail-untouched', 'mid-word', 'no-user'],
)
def test_split_command(command: str, expected: list[str], tmp_path: Path) -> None:
    env = {'HOME': str(tmp_path), 'KGL_TEST_PROFILE': 'work account'}
    with patch.dict('os.environ', env):
        assert split_command(command) == [arg.format(home=tmp_path) for arg in expected]


def test_split_command_bad_quoting_raises() -> None:
    """Callers turn ValueError into a ConfigurationError that names the option."""
    with pytest.raises(ValueError, match='quotation'):
        split_command("echo 'unclosed")
