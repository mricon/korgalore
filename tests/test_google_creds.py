"""Tests for Google OAuth2 and token_command credential providers."""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError

from korgalore import AuthenticationError, ConfigurationError, RemoteError
from korgalore.google_creds import (
    CommandCredentials,
    OAuthFileCredentials,
    parse_token_command,
    run_token_command,
)

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
    def test_failed_save_keeps_old_token(
        self, mock_request: MagicMock, mock_credentials: MagicMock, tmp_path: Path
    ) -> None:
        """A save that dies half-way must not leave a truncated token behind.

        The token file holds the refresh token. If it is lost, the user has
        to log in again in a browser, which a headless pull cannot do.
        """
        token = tmp_path / 'token.json'
        token.write_text('{"token": "stale"}')
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = 'refresh_token_value'
        # A lone surrogate cannot be encoded, so the write fails after the
        # file has been opened, like a crash in the middle of writing.
        mock_creds.to_json.return_value = '{"token": "\udc80"}'
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        with pytest.raises(UnicodeEncodeError):
            OAuthFileCredentials('test', str(tmp_path / 'creds.json'), token, scopes=TEST_SCOPES)

        assert token.read_text() == '{"token": "stale"}'
        assert [p.name for p in tmp_path.iterdir()] == ['token.json']

    @patch('korgalore.google_creds.InstalledAppFlow')
    def test_new_token_is_private_whatever_the_umask(self, mock_flow_class: MagicMock, tmp_path: Path) -> None:
        """The token file is created 0600; it is never readable by others, even briefly."""
        creds = tmp_path / 'creds.json'
        creds.write_text('{}')
        token = tmp_path / 'token.json'
        mock_flow_class.from_client_secrets_file.return_value.run_local_server.return_value.to_json.return_value = '{}'

        old_umask = os.umask(0)
        try:
            OAuthFileCredentials('test', str(creds), token, scopes=TEST_SCOPES)
        finally:
            os.umask(old_umask)

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


class TestTokenCommandHelpers:
    """Tests for target-agnostic parse_token_command and run_token_command helpers."""

    def test_parse_expands_env_and_home(self, tmp_path: Path) -> None:
        """Splits quoted args and expands environment variables and tildes."""
        with patch.dict('os.environ', {'HOME': str(tmp_path), 'HELPER_ARG': 'work account'}):
            args = parse_token_command('~/bin/get-token --profile "$HELPER_ARG"', "target 'test'")

        assert args == [f'{tmp_path}/bin/get-token', '--profile', 'work account']

    @pytest.mark.parametrize(
        ('value', 'expected_match'),
        [
            (123, 'must be a string'),
            ('   ', 'cannot be empty'),
            ('unclosed "quote', 'syntax'),
        ],
        ids=['non-string', 'whitespace-only', 'bad-quoting'],
    )
    def test_parse_invalid_values_raise(self, value: object, expected_match: str) -> None:
        """Invalid command strings raise ConfigurationError."""
        with pytest.raises(ConfigurationError, match=expected_match):
            parse_token_command(value, "target 'test'")

    @patch('korgalore.google_creds.subprocess.run')
    def test_run_returns_stripped_token(self, mock_run: MagicMock) -> None:
        """Executes command without a shell and returns the stripped bearer token."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=['get-token'], returncode=0, stdout='  valid-bearer-token\n', stderr=''
        )

        assert run_token_command(['get-token'], "target 'test'") == 'valid-bearer-token'
        mock_run.assert_called_once_with(
            ['get-token'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
            check=False,
        )

    @patch('korgalore.google_creds.subprocess.run')
    def test_run_nonzero_exit_raises_remote_error(self, mock_run: MagicMock) -> None:
        """Non-zero exit raises RemoteError including the last non-empty stderr line."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=['get-token'], returncode=1, stdout='', stderr='ignored line\nauth failed\n'
        )

        with pytest.raises(RemoteError, match='auth failed'):
            run_token_command(['get-token'], "target 'test'")

    @pytest.mark.parametrize(
        'stdout',
        ['   \n', 'multi word token\n'],
        ids=['empty', 'embedded-whitespace'],
    )
    @patch('korgalore.google_creds.subprocess.run')
    def test_run_invalid_output_raises_remote_error(self, mock_run: MagicMock, stdout: str) -> None:
        """Empty or whitespace-containing stdout raises RemoteError."""
        mock_run.return_value = subprocess.CompletedProcess(args=['get-token'], returncode=0, stdout=stdout, stderr='')

        with pytest.raises(RemoteError, match='empty or invalid'):
            run_token_command(['get-token'], "target 'test'")

    @patch('korgalore.google_creds.subprocess.run')
    def test_run_timeout_raises_remote_error(self, mock_run: MagicMock) -> None:
        """Subprocess timeout raises RemoteError."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='get-token', timeout=30)

        with pytest.raises(RemoteError, match='timed out after 30s'):
            run_token_command(['get-token'], "target 'test'")

    @patch('korgalore.google_creds.subprocess.run')
    def test_run_oserror_raises_configuration_error(self, mock_run: MagicMock) -> None:
        """Missing executable raises ConfigurationError."""
        mock_run.side_effect = FileNotFoundError('No such file or directory')

        with pytest.raises(ConfigurationError, match='Failed to execute'):
            run_token_command(['nonexistent-cmd'], "target 'test'")


class TestCommandCredentials:
    """Tests for CommandCredentials."""

    @patch('korgalore.google_creds.subprocess.run')
    def test_refresh_sets_token_and_marks_valid(self, mock_run: MagicMock) -> None:
        """CommandCredentials.refresh() invokes run_token_command and populates token."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=['get-token'], returncode=0, stdout='valid-bearer-token\n', stderr=''
        )

        creds = CommandCredentials('test', ['get-token'])
        assert not creds.valid

        creds.refresh(None)

        assert creds.valid
        assert creds.token == 'valid-bearer-token'
