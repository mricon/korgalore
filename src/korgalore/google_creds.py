"""Google credential providers for korgalore."""

import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from google.auth.credentials import Credentials as BaseCredentials
from google.auth.exceptions import RefreshError
from google.auth.external_account_authorized_user import Credentials as ExternalAccountCredentials
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

from korgalore import AuthenticationError, ConfigurationError

logger = logging.getLogger('korgalore')

# InstalledAppFlow.run_local_server() returns either flavour depending on what
# the client_secrets file describes. Both expose the valid/expired/refresh_token
# attributes and the to_json() and refresh() methods used below, so the code
# does not care which one it gets -- but the declared type has to allow both.
OAuthCredentials = Credentials | ExternalAccountCredentials


class OAuthFileCredentials(BaseCredentials):
    """Credentials backed by Google OAuth client secrets and a token file."""

    def __init__(
        self,
        identifier: str,
        credentials_file: str,
        token_file: str | Path,
        scopes: Sequence[str],
        interactive: bool = True,
        target_type: str = 'gmail',
    ) -> None:
        """Initialize file-backed OAuth credentials for a Google target.

        Args:
            identifier: Unique identifier for this target.
            credentials_file: Path to the Google OAuth credentials JSON file.
            token_file: Path to store/load the OAuth token.
            scopes: OAuth scopes to request when running interactive auth.
            interactive: If True, run OAuth flow interactively when needed.
                         If False, record ``needs_auth`` instead (for GUI mode).
            target_type: Target type string reported in AuthenticationError.

        Raises:
            ConfigurationError: If credentials file path is empty or missing.
            AuthenticationError: If token is expired/revoked and re-auth is needed,
                                 or if an interactive OAuth flow fails.
        """
        self._creds: OAuthCredentials | None = None
        super().__init__()  # type: ignore[no-untyped-call]
        self.identifier = identifier
        self._interactive = interactive
        self._scopes = list(scopes)
        self._target_type = target_type
        self.needs_auth = False

        if not credentials_file:
            raise ConfigurationError(f"No credentials file specified for target '{identifier}'.")
        # Store expanded paths for potential re-authentication
        self._credentials_file = Path(os.path.expandvars(credentials_file)).expanduser()
        self._token_file = Path(os.path.expandvars(os.fspath(token_file))).expanduser()

        self._load_credentials()

    def _load_credentials(self) -> None:
        """Load or refresh OAuth credentials for API access.

        Attempts to load existing credentials from ``self._token_file``. If
        not present or expired, refreshes with the stored refresh token or
        initiates the OAuth flow using ``self._credentials_file``.

        Raises:
            ConfigurationError: If credentials file is not found.
            AuthenticationError: If token is expired/revoked and re-auth is needed.
        """
        # The file token.json stores the user's access and refresh tokens
        if self._token_file.exists():
            try:
                self._creds = Credentials.from_authorized_user_file(str(self._token_file))  # type: ignore[no-untyped-call]
            except Exception as e:
                logger.warning('Failed to load OAuth token for %s from %s: %s', self.identifier, self._token_file, e)
                self._creds = None

        # If there are no (valid) credentials available, let the user log in
        if not self._creds or not self._creds.valid:
            if self._creds and self._creds.expired and self._creds.refresh_token:
                try:
                    self._creds.refresh(Request())  # type: ignore[no-untyped-call]
                    self._save_token()
                except RefreshError as e:
                    logger.warning('OAuth token for target %s has expired or been revoked.', self.identifier)
                    self._quarantine_token()
                    self.needs_auth = True
                    if not self._interactive:
                        # In non-interactive mode, just return - caller will check needs_auth
                        return
                    raise AuthenticationError(
                        f"OAuth token for '{self.identifier}' is invalid. Please re-authenticate.",
                        target_id=self.identifier,
                        target_type=self._target_type,
                    ) from e
            elif not self._credentials_file.exists():
                raise ConfigurationError(
                    f"Credentials file not found for target '{self.identifier}': {self._credentials_file}"
                )
            elif not self._interactive:
                # In non-interactive mode (GUI), don't run OAuth flow
                # Just mark as needing auth and return - caller will check needs_auth
                self.needs_auth = True
                return
            else:
                self.reauthenticate()
                return

        self.needs_auth = False

    @property
    def token(self) -> str | None:
        """Current OAuth2 access token, or None if not loaded."""
        if self._creds and self._creds.token is not None:
            return str(self._creds.token)
        return None

    @token.setter
    def token(self, value: str | None) -> None:
        if self._creds:
            self._creds.token = value

    @property
    def expiry(self) -> Any:
        """Expiration timestamp of the current access token, or None."""
        return self._creds.expiry if self._creds else None

    @expiry.setter
    def expiry(self, value: Any) -> None:
        if self._creds:
            self._creds.expiry = value

    @property
    def valid(self) -> bool:
        """Whether loaded OAuth2 credentials are present and currently valid."""
        return bool(self._creds and self._creds.valid)

    def _save_token(self) -> None:
        """Persist current OAuth credentials to the configured token file."""
        if self._creds is None:
            return
        # Save the credentials for the next run
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(self._creds.to_json())  # type: ignore[no-untyped-call]
        self._token_file.chmod(0o600)

    def _quarantine_token(self) -> None:
        """Rename a revoked or invalid token file aside to ``.invalid``."""
        if self._token_file.exists():
            invalid_token_file = self._token_file.with_name(f'{self._token_file.name}.invalid')
            invalid_token_file.unlink(missing_ok=True)
            self._token_file.rename(invalid_token_file)

    def refresh(self, request: Any = None) -> None:
        """Refresh the access token using the stored refresh token.

        Args:
            request: Optional Google auth transport request object.

        Raises:
            ConfigurationError: If credentials file is missing during
                                interactive re-authentication.
            AuthenticationError: If refresh fails or no token is loaded in
                                 non-interactive mode.
        """
        if self._creds is None:
            if self._interactive:
                self.reauthenticate()
                return
            self.needs_auth = True
            raise AuthenticationError(
                f"Target '{self.identifier}' requires authentication.",
                target_id=self.identifier,
                target_type=self._target_type,
            )
        req = request or Request()
        try:
            self._creds.refresh(req)  # type: ignore[no-untyped-call]
        except RefreshError as e:
            logger.warning('OAuth token for target %s has expired or been revoked.', self.identifier)
            self._quarantine_token()
            self.needs_auth = True
            raise AuthenticationError(
                f"Failed to refresh credentials for target '{self.identifier}': {e}",
                target_id=self.identifier,
                target_type=self._target_type,
            ) from e
        self._save_token()
        self.needs_auth = False

    def reauthenticate(self) -> None:
        """Perform OAuth re-authentication flow.

        Opens a browser window for the user to log in and authorize the app.
        Updates credentials upon successful authentication.

        Raises:
            ConfigurationError: If credentials file is not found.
            AuthenticationError: If the local OAuth authorization flow fails.
        """
        if not self._credentials_file.exists():
            raise ConfigurationError(
                f"Credentials file not found for target '{self.identifier}': {self._credentials_file}"
            )
        logger.critical('Log in to account for %s', self.identifier)
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(self._credentials_file), self._scopes)
            self._creds = flow.run_local_server(port=0)
        except Exception as e:
            self.needs_auth = True
            raise AuthenticationError(
                f"OAuth flow failed for target '{self.identifier}': {e}",
                target_id=self.identifier,
                target_type=self._target_type,
            ) from e
        self._save_token()
        self.needs_auth = False
