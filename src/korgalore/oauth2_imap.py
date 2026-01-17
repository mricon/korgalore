"""OAuth2 IMAP authenticator for Microsoft 365 using PKCE flow."""

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import requests

from korgalore import AuthenticationError, ConfigurationError

logger = logging.getLogger('korgalore')

# Microsoft 365 OAuth2 endpoints
MS_AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MS_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# Scope for IMAP access
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

# Default Azure AD Application (client) ID for korgalore
# Users can override this with their own app registration if their tenant
# blocks third-party applications.
DEFAULT_CLIENT_ID = "96202974-99c3-4d7d-b2a5-1f57fe7f114c"


@dataclass
class OAuth2Token:
    """OAuth2 token with metadata for persistence and expiry checking."""

    access_token: str
    refresh_token: str
    expires_at: float  # Unix timestamp
    token_type: str = "Bearer"
    scope: str = ""

    def is_expired(self, buffer_seconds: int = 300) -> bool:
        """Check if token is expired or will expire within buffer_seconds."""
        return datetime.now(timezone.utc).timestamp() >= (self.expires_at - buffer_seconds)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OAuth2Token":
        """Create from dictionary (JSON deserialization)."""
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=data["expires_at"],
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope", ""),
        )


@dataclass
class ImapOAuth2Authenticator:
    """OAuth2 authenticator for IMAP servers using PKCE flow."""

    identifier: str
    username: str
    client_id: str
    token_file: str
    tenant: str = "common"
    interactive: bool = True

    _token: Optional[OAuth2Token] = field(default=None, init=False, repr=False)
    _needs_auth: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Load existing token if present."""
        self.token_file = os.path.expandvars(os.path.expanduser(self.token_file))
        self._load_token()

    def _load_token(self) -> None:
        """Load token from file if it exists."""
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, 'r') as f:
                    data = json.load(f)
                self._token = OAuth2Token.from_dict(data)
                logger.debug("Loaded OAuth2 token for %s from %s",
                            self.identifier, self.token_file)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("Failed to load OAuth2 token from %s: %s",
                              self.token_file, e)
                self._token = None
                self._needs_auth = True
        else:
            self._needs_auth = True

    def _save_token(self) -> None:
        """Save token to file."""
        if self._token is None:
            return

        # Ensure directory exists
        token_path = Path(self.token_file)
        token_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.token_file, 'w') as f:
            json.dump(self._token.to_dict(), f, indent=2)

        # Set restrictive permissions
        os.chmod(self.token_file, 0o600)
        logger.debug("Saved OAuth2 token to %s", self.token_file)

    @property
    def needs_auth(self) -> bool:
        """Check if authentication is required."""
        if self._token is None:
            return True
        # Also mark as needing auth if refresh fails
        return self._needs_auth

    def get_access_token(self) -> str:
        """Get a valid access token, refreshing if needed.

        Returns:
            Valid access token string.

        Raises:
            AuthenticationError: If no valid token and interactive mode is disabled,
                or if authentication flow fails.
        """
        if self._token is None:
            if not self.interactive:
                self._needs_auth = True
                raise AuthenticationError(
                    f"IMAP OAuth2 target '{self.identifier}' requires authentication.",
                    target_id=self.identifier,
                    target_type='imap'
                )
            self._run_auth_flow()
            if self._token is None:
                raise AuthenticationError(
                    f"Authentication failed for IMAP target '{self.identifier}'.",
                    target_id=self.identifier,
                    target_type='imap'
                )

        if self._token.is_expired():
            try:
                self._refresh_token()
            except AuthenticationError:
                if not self.interactive:
                    self._needs_auth = True
                    raise
                # Try full re-auth in interactive mode
                self._run_auth_flow()

        if self._token is None:
            raise AuthenticationError(
                f"No valid token for IMAP target '{self.identifier}'.",
                target_id=self.identifier,
                target_type='imap'
            )

        return self._token.access_token

    def _refresh_token(self) -> None:
        """Use refresh token to get new access token.

        Raises:
            AuthenticationError: If refresh fails.
        """
        if self._token is None or not self._token.refresh_token:
            raise AuthenticationError(
                f"No refresh token available for IMAP target '{self.identifier}'.",
                target_id=self.identifier,
                target_type='imap'
            )

        token_url = MS_TOKEN_URL.format(tenant=self.tenant)
        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self._token.refresh_token,
            "scope": IMAP_SCOPE,
        }

        logger.debug("Refreshing OAuth2 token for %s", self.identifier)

        try:
            response = requests.post(token_url, data=data, timeout=30)
            response.raise_for_status()
            token_data = response.json()
        except requests.RequestException as e:
            logger.warning("Token refresh failed for %s: %s", self.identifier, e)
            # Invalidate token file
            invalid_file = self.token_file + '.invalid'
            if os.path.exists(self.token_file):
                if os.path.exists(invalid_file):
                    os.remove(invalid_file)
                os.rename(self.token_file, invalid_file)
            self._token = None
            self._needs_auth = True
            raise AuthenticationError(
                f"Token refresh failed for IMAP target '{self.identifier}'. "
                "Please re-authenticate.",
                target_id=self.identifier,
                target_type='imap'
            ) from e

        # Calculate expiry time
        expires_in = token_data.get("expires_in", 3600)
        expires_at = datetime.now(timezone.utc).timestamp() + expires_in

        self._token = OAuth2Token(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", self._token.refresh_token),
            expires_at=expires_at,
            token_type=token_data.get("token_type", "Bearer"),
            scope=token_data.get("scope", IMAP_SCOPE),
        )
        self._save_token()
        self._needs_auth = False
        logger.debug("OAuth2 token refreshed for %s", self.identifier)

    def _run_auth_flow(self) -> None:
        """Run interactive PKCE authentication flow.

        Opens a browser window for the user to authenticate.

        Raises:
            AuthenticationError: If authentication fails.
        """
        logger.info("Starting OAuth2 authentication for IMAP target %s", self.identifier)

        # Generate PKCE challenge
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).decode().rstrip('=')

        # Generate state for CSRF protection
        state = secrets.token_urlsafe(32)

        # Result container for callback
        auth_result: Dict[str, Any] = {"code": None, "error": None}
        server_ready = threading.Event()

        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            """HTTP handler for OAuth2 callback."""

            def log_message(self, format: str, *args: Any) -> None:
                """Suppress default logging."""
                pass

            def do_GET(self) -> None:
                """Handle GET request with auth callback."""
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)

                if 'code' in params:
                    auth_result["code"] = params['code'][0]
                    # Verify state
                    returned_state = params.get('state', [None])[0]
                    if returned_state != state:
                        auth_result["error"] = "State mismatch - possible CSRF attack"
                        auth_result["code"] = None
                elif 'error' in params:
                    auth_result["error"] = params.get('error_description',
                                                       params.get('error', ['Unknown error']))[0]

                # Send response page
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()

                if auth_result["code"]:
                    html = """<!DOCTYPE html>
<html><head><title>Authentication Successful</title></head>
<body><h1>Authentication Successful</h1>
<p>You can close this window and return to korgalore.</p></body></html>"""
                else:
                    html = f"""<!DOCTYPE html>
<html><head><title>Authentication Failed</title></head>
<body><h1>Authentication Failed</h1>
<p>Error: {auth_result.get('error', 'Unknown error')}</p>
<p>Please close this window and try again.</p></body></html>"""

                self.wfile.write(html.encode())

        # Start local server on random port
        server = http.server.HTTPServer(('127.0.0.1', 0), CallbackHandler)
        port = server.server_address[1]
        redirect_uri = f"http://localhost:{port}/"

        # Build authorization URL
        auth_url = MS_AUTH_URL.format(tenant=self.tenant)
        auth_params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": IMAP_SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "login_hint": self.username,
        }
        full_auth_url = f"{auth_url}?{urllib.parse.urlencode(auth_params)}"

        # Run server in background thread
        def serve_one() -> None:
            server_ready.set()
            server.handle_request()
            server.server_close()

        server_thread = threading.Thread(target=serve_one, daemon=True)
        server_thread.start()
        server_ready.wait()

        # Open browser
        logger.info("Opening browser for Microsoft 365 authentication...")
        logger.info("If browser doesn't open, visit: %s", full_auth_url)
        webbrowser.open(full_auth_url)

        # Wait for callback (with timeout)
        server_thread.join(timeout=300)

        if auth_result.get("error"):
            raise AuthenticationError(
                f"OAuth2 authentication failed: {auth_result['error']}",
                target_id=self.identifier,
                target_type='imap'
            )

        if not auth_result.get("code"):
            raise AuthenticationError(
                "OAuth2 authentication timed out or was cancelled.",
                target_id=self.identifier,
                target_type='imap'
            )

        # Exchange authorization code for tokens
        token_url = MS_TOKEN_URL.format(tenant=self.tenant)
        token_data = {
            "client_id": self.client_id,
            "grant_type": "authorization_code",
            "code": auth_result["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "scope": IMAP_SCOPE,
        }

        try:
            response = requests.post(token_url, data=token_data, timeout=30)
            response.raise_for_status()
            token_response = response.json()
        except requests.RequestException as e:
            raise AuthenticationError(
                f"Failed to exchange authorization code: {e}",
                target_id=self.identifier,
                target_type='imap'
            ) from e

        # Calculate expiry time
        expires_in = token_response.get("expires_in", 3600)
        expires_at = datetime.now(timezone.utc).timestamp() + expires_in

        self._token = OAuth2Token(
            access_token=token_response["access_token"],
            refresh_token=token_response.get("refresh_token", ""),
            expires_at=expires_at,
            token_type=token_response.get("token_type", "Bearer"),
            scope=token_response.get("scope", IMAP_SCOPE),
        )
        self._save_token()
        self._needs_auth = False
        logger.info("OAuth2 authentication successful for %s", self.identifier)

    def build_xoauth2_string(self) -> str:
        """Build XOAUTH2 authentication string for IMAP.

        Returns:
            XOAUTH2 string ready for IMAP AUTHENTICATE command.

        Raises:
            AuthenticationError: If no valid token available.
        """
        access_token = self.get_access_token()
        # XOAUTH2 format: user={email}\x01auth=Bearer {token}\x01\x01
        auth_string = f"user={self.username}\x01auth=Bearer {access_token}\x01\x01"
        return auth_string

    def reauthenticate(self) -> None:
        """Force re-authentication (for GUI use).

        Opens a browser window for the user to re-authenticate.

        Raises:
            AuthenticationError: If authentication fails.
            ConfigurationError: If client_id is not configured.
        """
        if not self.client_id:
            raise ConfigurationError(
                f"No client_id configured for IMAP OAuth2 target '{self.identifier}'."
            )

        # Clear existing token to force full re-auth
        self._token = None

        # Run the auth flow (temporarily enable interactive mode)
        old_interactive = self.interactive
        self.interactive = True
        try:
            self._run_auth_flow()
        finally:
            self.interactive = old_interactive


def xoauth2_callback(authenticator: ImapOAuth2Authenticator) -> Callable[[bytes], bytes]:
    """Create IMAP AUTHENTICATE callback function for XOAUTH2.

    Args:
        authenticator: The OAuth2 authenticator instance.

    Returns:
        Callback function for use with IMAP4.authenticate().
    """
    def callback(challenge: bytes) -> bytes:
        auth_string = authenticator.build_xoauth2_string()
        return auth_string.encode()

    return callback
