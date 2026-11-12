"""Tests for OAuth2 IMAP authenticator."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from korgalore import AuthenticationError, ConfigurationError
from korgalore.oauth2_imap import (
    MS_TOKEN_URL,
    ImapOAuth2Authenticator,
    OAuth2Token,
    xoauth2_callback,
)
from tests.target_helpers import valid_token_file


def make_auth(
    token_file: Path | str,
    username: str = 'user@example.com',
    client_id: str = 'client-id',
    tenant: str = 'common',
    interactive: bool = True,
) -> ImapOAuth2Authenticator:
    """An authenticator for the given token file, overridable per test."""
    return ImapOAuth2Authenticator(
        identifier='test',
        username=username,
        client_id=client_id,
        token_file=str(token_file),
        tenant=tenant,
        interactive=interactive,
    )


class TestOAuth2Token:
    """Tests for OAuth2Token dataclass."""

    @pytest.mark.parametrize(
        ('offset', 'buffer_seconds', 'expected'),
        [
            (3600, None, False),
            (-3600, None, True),
            (120, 300, True),
        ],
        ids=['future', 'past', 'within-buffer'],
    )
    def test_is_expired(self, offset: float, buffer_seconds: int | None, expected: bool) -> None:
        token = OAuth2Token(
            access_token='test',
            refresh_token='test',
            expires_at=datetime.now(UTC).timestamp() + offset,
        )
        if buffer_seconds is None:
            assert token.is_expired() is expected
        else:
            assert token.is_expired(buffer_seconds=buffer_seconds) is expected

    def test_dict_round_trip(self) -> None:
        token = OAuth2Token(
            access_token='access',
            refresh_token='refresh',
            expires_at=1234567890.0,
            token_type='Bearer',
            scope='test scope',
        )
        data = token.to_dict()
        assert data == {
            'access_token': 'access',
            'refresh_token': 'refresh',
            'expires_at': 1234567890.0,
            'token_type': 'Bearer',
            'scope': 'test scope',
        }
        assert OAuth2Token.from_dict(data) == token

    def test_optional_fields_default(self) -> None:
        """Both the dataclass and from_dict default token_type and scope."""
        data = {'access_token': 'access', 'refresh_token': 'refresh', 'expires_at': 1234567890.0}
        for token in (
            OAuth2Token(access_token='access', refresh_token='refresh', expires_at=1234567890.0),
            OAuth2Token.from_dict(data),
        ):
            assert token.access_token == 'access'
            assert token.refresh_token == 'refresh'
            assert token.expires_at == 1234567890.0
            assert token.token_type == 'Bearer'
            assert token.scope == ''


class TestImapOAuth2Authenticator:
    """Tests for ImapOAuth2Authenticator."""

    @pytest.mark.parametrize('content', [None, 'invalid json {{{'], ids=['no-file', 'invalid-json'])
    def test_init_without_usable_token_file(self, tmp_path: Path, content: str | None) -> None:
        token_file = tmp_path / 'token.json'
        if content is not None:
            token_file.write_text(content)

        auth = make_auth(token_file)
        assert auth.needs_auth
        assert auth._token is None

    def test_init_with_valid_token_file(self, tmp_path: Path) -> None:
        auth = make_auth(valid_token_file(tmp_path, access_token='valid_access'))
        assert not auth.needs_auth
        assert auth._token is not None
        assert auth._token.access_token == 'valid_access'

    def test_init_expands_tilde(self, tmp_path: Path) -> None:
        valid_token_file(tmp_path)

        with patch.dict(os.environ, {'HOME': str(tmp_path)}):
            auth = make_auth('~/token.json')
        assert not auth.needs_auth

    def test_save_token(self, tmp_path: Path) -> None:
        """The token file is created 0600; it is never readable by others, even briefly."""
        token_file = tmp_path / 'subdir' / 'token.json'
        auth = make_auth(token_file)
        auth._token = OAuth2Token(
            access_token='saved_access',
            refresh_token='saved_refresh',
            expires_at=1234567890.0,
        )
        old_umask = os.umask(0)
        try:
            auth._save_token()
        finally:
            os.umask(old_umask)

        assert (token_file.stat().st_mode & 0o777) == 0o600
        assert json.loads(token_file.read_text())['access_token'] == 'saved_access'

    def test_failed_save_keeps_old_token(self, tmp_path: Path) -> None:
        """A save that dies half-way must not leave a truncated token behind.

        The file holds the refresh token. Losing it means a new browser
        login, which a headless pull cannot do.
        """
        token_file = valid_token_file(tmp_path, access_token='old_access')
        auth = make_auth(token_file)
        # json.dump() has already written the first keys when it reaches
        # the value it cannot serialise, like a crash in the middle of writing.
        with (
            patch.object(OAuth2Token, 'to_dict', return_value={'access_token': 'new', 'oops': object()}),
            pytest.raises(TypeError),
        ):
            auth._save_token()

        assert json.loads(token_file.read_text())['access_token'] == 'old_access'
        assert [p.name for p in token_file.parent.iterdir()] == [token_file.name]

    def test_get_access_token_no_token_non_interactive(self, tmp_path: Path) -> None:
        """Non-interactive mode raises AuthenticationError when no token."""
        auth = make_auth(tmp_path / 'token.json', interactive=False)

        with pytest.raises(AuthenticationError) as exc_info:
            auth.get_access_token()
        assert 'requires authentication' in str(exc_info.value)
        assert exc_info.value.target_id == 'test'
        assert exc_info.value.target_type == 'imap'

    def test_get_access_token_valid(self, tmp_path: Path) -> None:
        auth = make_auth(valid_token_file(tmp_path, access_token='valid_token'))
        assert auth.get_access_token() == 'valid_token'

    @patch('korgalore.oauth2_imap.requests.post')
    def test_refresh_token_success(self, mock_post: MagicMock, tmp_path: Path) -> None:
        token_file = valid_token_file(
            tmp_path, access_token='old_token', refresh_token='valid_refresh', expires_in=-3600
        )
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'access_token': 'new_access_token',
            'refresh_token': 'new_refresh_token',
            'expires_in': 3600,
            'token_type': 'Bearer',
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        auth = make_auth(token_file, client_id='test-client-id', tenant='test-tenant')

        assert auth.get_access_token() == 'new_access_token'
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == MS_TOKEN_URL.format(tenant='test-tenant')
        assert call_args[1]['data']['client_id'] == 'test-client-id'
        assert call_args[1]['data']['grant_type'] == 'refresh_token'

    @patch('korgalore.oauth2_imap.requests.post')
    def test_refresh_token_failure(self, mock_post: MagicMock, tmp_path: Path) -> None:
        """Refresh failure in non-interactive mode raises and sets the token file aside."""
        token_file = valid_token_file(
            tmp_path, access_token='old_token', refresh_token='invalid_refresh', expires_in=-3600
        )
        mock_post.side_effect = requests.RequestException('Refresh failed')

        auth = make_auth(token_file, interactive=False)

        with pytest.raises(AuthenticationError, match='Token refresh failed'):
            auth.get_access_token()
        assert (tmp_path / 'token.json.invalid').exists()

    def test_build_xoauth2_string(self, tmp_path: Path) -> None:
        auth = make_auth(valid_token_file(tmp_path, access_token='test_access_token'))
        expected = 'user=user@example.com\x01auth=Bearer test_access_token\x01\x01'
        assert auth.build_xoauth2_string() == expected

    def test_reauthenticate_no_client_id(self, tmp_path: Path) -> None:
        auth = make_auth(tmp_path / 'token.json', client_id='')

        with pytest.raises(ConfigurationError):
            auth.reauthenticate()


class TestXOAuth2Callback:
    """Tests for xoauth2_callback function."""

    def test_callback_returns_encoded_string(self, tmp_path: Path) -> None:
        auth = make_auth(valid_token_file(tmp_path, access_token='test_token'), username='test@example.com')

        result = xoauth2_callback(auth)(b'')

        assert result == b'user=test@example.com\x01auth=Bearer test_token\x01\x01'
