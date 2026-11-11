"""Tests for Google OAuth2 credential providers."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError

from korgalore import AuthenticationError, ConfigurationError
from korgalore.google_creds import OAuthFileCredentials

TEST_SCOPES = ['https://example.invalid/scope.a', 'https://example.invalid/scope.b']


class TestOAuthFileCredentials:
    """Tests for OAuthFileCredentials initialization, refresh, and reauth."""

    @patch('korgalore.google_creds.Credentials')
    def test_loads_existing_valid_token(self, mock_credentials: MagicMock, tmp_path: Path) -> None:
        """Loads credentials from the stored token when already valid."""
        token = tmp_path / 'token.json'
        token.write_text('{}')
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.token = 'access-tok'
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        provider = OAuthFileCredentials('test', str(tmp_path / 'creds.json'), token, scopes=TEST_SCOPES)

        assert provider.valid
        assert provider.token == 'access-tok'
        assert not provider.needs_auth
        mock_credentials.from_authorized_user_file.assert_called_once_with(str(token))

    @patch('korgalore.google_creds.Credentials')
    @patch('korgalore.google_creds.Request')
    def test_refreshes_expired_token(
        self, mock_request: MagicMock, mock_credentials: MagicMock, tmp_path: Path
    ) -> None:
        """Refreshes expired credentials with refresh token and saves the result."""
        token = tmp_path / 'token.json'
        token.write_text('{"token": "stale"}')
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = 'refresh_token_value'
        mock_creds.to_json.return_value = '{"token": "refreshed"}'
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        OAuthFileCredentials('test', str(tmp_path / 'creds.json'), token, scopes=TEST_SCOPES)

        mock_creds.refresh.assert_called_once()
        assert token.read_text() == '{"token": "refreshed"}'
        assert (token.stat().st_mode & 0o777) == 0o600

    @patch('korgalore.google_creds.Credentials')
    @patch('korgalore.google_creds.Request')
    def test_revoked_token_is_set_aside(
        self, mock_request: MagicMock, mock_credentials: MagicMock, tmp_path: Path
    ) -> None:
        """A token that fails to refresh is renamed aside and needs_auth is flagged."""
        token = tmp_path / 'token.json'
        token.write_text('{"token": "revoked"}')
        invalid = tmp_path / 'token.json.invalid'
        invalid.write_text('{"token": "older"}')
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = 'refresh_token_value'
        mock_creds.refresh.side_effect = RefreshError
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        provider = OAuthFileCredentials(
            'test', str(tmp_path / 'creds.json'), token, scopes=TEST_SCOPES, interactive=False
        )

        assert provider.needs_auth
        assert not token.exists()
        assert invalid.read_text() == '{"token": "revoked"}'

    @patch('korgalore.google_creds.Credentials')
    @patch('korgalore.google_creds.Request')
    def test_revoked_token_interactive_raises_authentication_error(
        self, mock_request: MagicMock, mock_credentials: MagicMock, tmp_path: Path
    ) -> None:
        """Interactive load raises AuthenticationError when token refresh is revoked."""
        token = tmp_path / 'token.json'
        token.write_text('{"token": "revoked"}')
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = 'refresh_token_value'
        mock_creds.refresh.side_effect = RefreshError
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        with pytest.raises(AuthenticationError, match='invalid'):
            OAuthFileCredentials('test', str(tmp_path / 'creds.json'), token, scopes=TEST_SCOPES, interactive=True)

        assert not token.exists()
        assert token.with_name('token.json.invalid').exists()

    @patch('korgalore.google_creds.InstalledAppFlow')
    def test_runs_oauth_flow_and_saves_token_when_no_token(self, mock_flow_class: MagicMock, tmp_path: Path) -> None:
        """Runs the OAuth flow interactively, saves the returned credentials, and marks auth done."""
        creds = tmp_path / 'creds.json'
        creds.write_text('{}')
        token = tmp_path / 'token.json'
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.to_json.return_value = '{"token": "fresh"}'
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_class.from_client_secrets_file.return_value = mock_flow

        provider = OAuthFileCredentials('test', str(creds), token, scopes=TEST_SCOPES)

        mock_flow_class.from_client_secrets_file.assert_called_once_with(str(creds), TEST_SCOPES)
        mock_flow.run_local_server.assert_called_once_with(port=0)
        assert provider.valid
        assert not provider.needs_auth
        assert token.read_text() == '{"token": "fresh"}'
        assert (token.stat().st_mode & 0o777) == 0o600

    def test_missing_credentials_file_raises(self, tmp_path: Path) -> None:
        """Missing credentials file raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            OAuthFileCredentials('test', str(tmp_path / 'creds.json'), tmp_path / 'token.json', scopes=TEST_SCOPES)
        assert 'not found' in str(exc_info.value)
        assert 'creds.json' in str(exc_info.value)

    @patch('korgalore.google_creds.Credentials')
    def test_expands_user_paths(self, mock_credentials: MagicMock, tmp_path: Path) -> None:
        """Tilde and environment variables in paths are expanded."""
        token = tmp_path / 'token.json'
        token.write_text('{}')
        creds = tmp_path / 'creds.json'
        creds.write_text('{}')
        mock_credentials.from_authorized_user_file.return_value = MagicMock(valid=True)

        with patch.dict('os.environ', {'HOME': str(tmp_path)}):
            OAuthFileCredentials('test', '~/creds.json', '$HOME/token.json', scopes=TEST_SCOPES)

        mock_credentials.from_authorized_user_file.assert_called_once_with(str(token))

    def test_corrupt_token_file_falls_back_gracefully(self, tmp_path: Path) -> None:
        """Corrupt or truncated token.json logs warning and leaves creds unloaded instead of crashing."""
        token = tmp_path / 'token.json'
        token.write_text('not valid json{')
        creds = tmp_path / 'creds.json'
        creds.write_text('{}')

        provider = OAuthFileCredentials('test', str(creds), token, scopes=TEST_SCOPES, interactive=False)
        assert provider.needs_auth
        assert not provider.valid
