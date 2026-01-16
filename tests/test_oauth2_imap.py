"""Tests for OAuth2 IMAP authenticator."""

import json
import os
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from korgalore import AuthenticationError, ConfigurationError
from korgalore.oauth2_imap import (
    OAuth2Token, ImapOAuth2Authenticator, xoauth2_callback,
    MS_AUTH_URL, MS_TOKEN_URL, IMAP_SCOPE
)


class TestOAuth2Token:
    """Tests for OAuth2Token dataclass."""

    def test_token_creation(self) -> None:
        """Token can be created with required fields."""
        token = OAuth2Token(
            access_token="test_access",
            refresh_token="test_refresh",
            expires_at=datetime.now(timezone.utc).timestamp() + 3600,
        )
        assert token.access_token == "test_access"
        assert token.refresh_token == "test_refresh"
        assert token.token_type == "Bearer"

    def test_token_not_expired(self) -> None:
        """Token is not expired when expiry is in the future."""
        future_time = datetime.now(timezone.utc).timestamp() + 3600
        token = OAuth2Token(
            access_token="test",
            refresh_token="test",
            expires_at=future_time,
        )
        assert not token.is_expired()

    def test_token_expired(self) -> None:
        """Token is expired when expiry is in the past."""
        past_time = datetime.now(timezone.utc).timestamp() - 3600
        token = OAuth2Token(
            access_token="test",
            refresh_token="test",
            expires_at=past_time,
        )
        assert token.is_expired()

    def test_token_expired_within_buffer(self) -> None:
        """Token is considered expired when within buffer time."""
        # Token expires in 2 minutes, but buffer is 5 minutes
        near_future = datetime.now(timezone.utc).timestamp() + 120
        token = OAuth2Token(
            access_token="test",
            refresh_token="test",
            expires_at=near_future,
        )
        assert token.is_expired(buffer_seconds=300)

    def test_token_to_dict(self) -> None:
        """Token can be converted to dictionary."""
        token = OAuth2Token(
            access_token="access",
            refresh_token="refresh",
            expires_at=1234567890.0,
            token_type="Bearer",
            scope="test scope",
        )
        data = token.to_dict()
        assert data["access_token"] == "access"
        assert data["refresh_token"] == "refresh"
        assert data["expires_at"] == 1234567890.0
        assert data["token_type"] == "Bearer"
        assert data["scope"] == "test scope"

    def test_token_from_dict(self) -> None:
        """Token can be created from dictionary."""
        data = {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 1234567890.0,
            "token_type": "Bearer",
            "scope": "test scope",
        }
        token = OAuth2Token.from_dict(data)
        assert token.access_token == "access"
        assert token.refresh_token == "refresh"
        assert token.expires_at == 1234567890.0

    def test_token_from_dict_defaults(self) -> None:
        """Token from_dict uses defaults for optional fields."""
        data = {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 1234567890.0,
        }
        token = OAuth2Token.from_dict(data)
        assert token.token_type == "Bearer"
        assert token.scope == ""


class TestImapOAuth2Authenticator:
    """Tests for ImapOAuth2Authenticator."""

    def test_init_without_token_file(self, tmp_path: Path) -> None:
        """Authenticator initializes without existing token file."""
        token_file = tmp_path / "token.json"
        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="user@example.com",
            client_id="client-id",
            token_file=str(token_file),
        )
        assert auth.needs_auth
        assert auth._token is None

    def test_init_with_valid_token_file(self, tmp_path: Path) -> None:
        """Authenticator loads existing valid token file."""
        token_file = tmp_path / "token.json"
        token_data = {
            "access_token": "valid_access",
            "refresh_token": "valid_refresh",
            "expires_at": datetime.now(timezone.utc).timestamp() + 3600,
        }
        token_file.write_text(json.dumps(token_data))

        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="user@example.com",
            client_id="client-id",
            token_file=str(token_file),
        )
        assert not auth.needs_auth
        assert auth._token is not None
        assert auth._token.access_token == "valid_access"

    def test_init_with_invalid_token_file(self, tmp_path: Path) -> None:
        """Authenticator handles invalid token file gracefully."""
        token_file = tmp_path / "token.json"
        token_file.write_text("invalid json {{{")

        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="user@example.com",
            client_id="client-id",
            token_file=str(token_file),
        )
        assert auth.needs_auth
        assert auth._token is None

    def test_init_expands_tilde(self, tmp_path: Path) -> None:
        """Token file path with tilde is expanded."""
        token_file = tmp_path / "token.json"
        token_file.write_text(json.dumps({
            "access_token": "test",
            "refresh_token": "test",
            "expires_at": datetime.now(timezone.utc).timestamp() + 3600,
        }))

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            auth = ImapOAuth2Authenticator(
                identifier="test",
                username="user@example.com",
                client_id="client-id",
                token_file="~/token.json",
            )
        assert not auth.needs_auth

    def test_save_token(self, tmp_path: Path) -> None:
        """Token is saved to file with correct permissions."""
        token_file = tmp_path / "subdir" / "token.json"
        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="user@example.com",
            client_id="client-id",
            token_file=str(token_file),
        )
        auth._token = OAuth2Token(
            access_token="saved_access",
            refresh_token="saved_refresh",
            expires_at=1234567890.0,
        )
        auth._save_token()

        assert token_file.exists()
        # Check permissions (0o600 = owner read/write only)
        assert (token_file.stat().st_mode & 0o777) == 0o600

        saved_data = json.loads(token_file.read_text())
        assert saved_data["access_token"] == "saved_access"

    def test_get_access_token_no_token_non_interactive(self, tmp_path: Path) -> None:
        """Non-interactive mode raises AuthenticationError when no token."""
        token_file = tmp_path / "token.json"
        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="user@example.com",
            client_id="client-id",
            token_file=str(token_file),
            interactive=False,
        )

        with pytest.raises(AuthenticationError) as exc_info:
            auth.get_access_token()
        assert "requires authentication" in str(exc_info.value)
        assert exc_info.value.target_id == "test"
        assert exc_info.value.target_type == "imap"

    def test_get_access_token_valid(self, tmp_path: Path) -> None:
        """Valid token is returned directly."""
        token_file = tmp_path / "token.json"
        token_data = {
            "access_token": "valid_token",
            "refresh_token": "refresh",
            "expires_at": datetime.now(timezone.utc).timestamp() + 3600,
        }
        token_file.write_text(json.dumps(token_data))

        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="user@example.com",
            client_id="client-id",
            token_file=str(token_file),
        )

        assert auth.get_access_token() == "valid_token"

    @patch('korgalore.oauth2_imap.requests.post')
    def test_refresh_token_success(self, mock_post: MagicMock, tmp_path: Path) -> None:
        """Token refresh works correctly."""
        token_file = tmp_path / "token.json"
        # Create expired token
        token_data = {
            "access_token": "old_token",
            "refresh_token": "valid_refresh",
            "expires_at": datetime.now(timezone.utc).timestamp() - 3600,
        }
        token_file.write_text(json.dumps(token_data))

        # Mock refresh response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="user@example.com",
            client_id="test-client-id",
            token_file=str(token_file),
            tenant="test-tenant",
        )

        token = auth.get_access_token()

        assert token == "new_access_token"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == MS_TOKEN_URL.format(tenant="test-tenant")
        assert call_args[1]["data"]["client_id"] == "test-client-id"
        assert call_args[1]["data"]["grant_type"] == "refresh_token"

    @patch('korgalore.oauth2_imap.requests.post')
    def test_refresh_token_failure(self, mock_post: MagicMock, tmp_path: Path) -> None:
        """Token refresh failure in non-interactive mode raises error."""
        import requests

        token_file = tmp_path / "token.json"
        token_data = {
            "access_token": "old_token",
            "refresh_token": "invalid_refresh",
            "expires_at": datetime.now(timezone.utc).timestamp() - 3600,
        }
        token_file.write_text(json.dumps(token_data))

        mock_post.side_effect = requests.RequestException("Refresh failed")

        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="user@example.com",
            client_id="client-id",
            token_file=str(token_file),
            interactive=False,
        )

        with pytest.raises(AuthenticationError) as exc_info:
            auth.get_access_token()
        assert "Token refresh failed" in str(exc_info.value)

        # Check that invalid token file was renamed
        assert (tmp_path / "token.json.invalid").exists()

    def test_build_xoauth2_string(self, tmp_path: Path) -> None:
        """XOAUTH2 string is built correctly."""
        token_file = tmp_path / "token.json"
        token_data = {
            "access_token": "test_access_token",
            "refresh_token": "refresh",
            "expires_at": datetime.now(timezone.utc).timestamp() + 3600,
        }
        token_file.write_text(json.dumps(token_data))

        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="user@example.com",
            client_id="client-id",
            token_file=str(token_file),
        )

        xoauth2_str = auth.build_xoauth2_string()
        expected = "user=user@example.com\x01auth=Bearer test_access_token\x01\x01"
        assert xoauth2_str == expected

    def test_reauthenticate_no_client_id(self, tmp_path: Path) -> None:
        """Reauthenticate raises ConfigurationError without client_id."""
        token_file = tmp_path / "token.json"
        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="user@example.com",
            client_id="",  # Empty client_id
            token_file=str(token_file),
        )

        with pytest.raises(ConfigurationError):
            auth.reauthenticate()

    @patch('korgalore.oauth2_imap.webbrowser.open')
    @patch('korgalore.oauth2_imap.requests.post')
    def test_auth_flow_timeout(self, mock_post: MagicMock,
                                mock_browser: MagicMock, tmp_path: Path) -> None:
        """Auth flow timeout raises AuthenticationError."""
        token_file = tmp_path / "token.json"
        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="user@example.com",
            client_id="client-id",
            token_file=str(token_file),
            interactive=True,
        )

        # Mock browser to do nothing
        mock_browser.return_value = True

        # Use a very short timeout by patching the join
        with patch.object(auth, '_run_auth_flow') as mock_auth:
            mock_auth.side_effect = AuthenticationError(
                "OAuth2 authentication timed out or was cancelled.",
                target_id="test",
                target_type="imap"
            )
            with pytest.raises(AuthenticationError) as exc_info:
                auth._run_auth_flow()
            assert "timed out" in str(exc_info.value)


class TestXOAuth2Callback:
    """Tests for xoauth2_callback function."""

    def test_callback_returns_encoded_string(self, tmp_path: Path) -> None:
        """Callback returns properly encoded XOAUTH2 string."""
        token_file = tmp_path / "token.json"
        token_data = {
            "access_token": "test_token",
            "refresh_token": "refresh",
            "expires_at": datetime.now(timezone.utc).timestamp() + 3600,
        }
        token_file.write_text(json.dumps(token_data))

        auth = ImapOAuth2Authenticator(
            identifier="test",
            username="test@example.com",
            client_id="client-id",
            token_file=str(token_file),
        )

        callback = xoauth2_callback(auth)
        result = callback(b"")

        expected = b"user=test@example.com\x01auth=Bearer test_token\x01\x01"
        assert result == expected


class TestMicrosoftEndpoints:
    """Tests for Microsoft OAuth2 endpoint constants."""

    def test_auth_url_format(self) -> None:
        """Auth URL accepts tenant parameter."""
        url = MS_AUTH_URL.format(tenant="common")
        assert "common" in url
        assert "oauth2/v2.0/authorize" in url

    def test_token_url_format(self) -> None:
        """Token URL accepts tenant parameter."""
        url = MS_TOKEN_URL.format(tenant="my-tenant-id")
        assert "my-tenant-id" in url
        assert "oauth2/v2.0/token" in url

    def test_imap_scope(self) -> None:
        """IMAP scope includes required permissions."""
        assert "IMAP.AccessAsUser.All" in IMAP_SCOPE
        assert "offline_access" in IMAP_SCOPE
