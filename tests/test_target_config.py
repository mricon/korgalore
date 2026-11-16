"""Tests for options that every mail target reads from the configuration file."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
from google.auth.credentials import Credentials as BaseCredentials

from korgalore.cli import get_target
from tests.digest_helpers import make_ctx

TARGET_SETTINGS: dict[str, dict[str, Any]] = {
    'gmail': {},
    'maildir': {'path': 'mail'},
    'jmap': {'server': 'https://api.example.com', 'username': 'user@example.com', 'token': 'token'},
    'imap': {'server': 'imap.example.com', 'username': 'user@example.com', 'password': 'secret'},
}


@pytest.fixture(params=sorted(TARGET_SETTINGS))
def build(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    """Build a target of each type from config settings, without touching the network or the home directory."""
    target_type: str = request.param
    settings = dict(TARGET_SETTINGS[target_type])
    if 'path' in settings:
        settings['path'] = str(tmp_path / settings['path'])

    def _build(extra: dict[str, Any]) -> Any:
        config = {'targets': {'test': {'type': target_type, **settings, **extra}}}
        creds = MagicMock(spec=BaseCredentials)
        creds.valid = True
        with (
            patch('korgalore.cli.make_gmail_credentials', return_value=creds),
            patch('korgalore.cli.get_xdg_config_dir', return_value=tmp_path),
        ):
            return get_target(make_ctx({'config': config, 'targets': {}}), 'test')

    return _build


class TestMessageDateFromConfig:
    """Tests for the message_date key of gmail, maildir, jmap and imap targets."""

    @pytest.mark.parametrize(
        ('settings', 'by_header'),
        [({}, True), ({'message_date': 'header'}, True), ({'message_date': 'import'}, False)],
        ids=['default', 'explicit-header', 'import'],
    )
    def test_message_date_setting_reaches_the_target(
        self, build: Any, settings: dict[str, Any], by_header: bool
    ) -> None:
        """The message_date key in the config decides how the target dates messages.

        Without this, the option could be documented and validated but
        never read, and 'import' would quietly keep dating by the headers.
        """
        assert build(settings).use_header_date is by_header

    def test_invalid_message_date_aborts(self, build: Any) -> None:
        """A wrong value stops the command instead of delivering with a guess."""
        with pytest.raises(click.Abort):
            build({'message_date': 'sender'})
