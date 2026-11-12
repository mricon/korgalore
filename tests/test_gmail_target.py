"""Tests for GmailTarget message delivery."""

import base64
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

from korgalore import ConfigurationError, RemoteError
from korgalore.gmail_target import SCOPES, GmailTarget


def make_target(tmp_path: Path) -> tuple[GmailTarget, MagicMock]:
    """A GmailTarget with valid stored credentials; returns it with those credentials."""
    token = tmp_path / 'token.json'
    token.write_text('{}')
    with patch('korgalore.gmail_target.Credentials') as mock_creds_class:
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds_class.from_authorized_user_file.return_value = mock_creds
        return GmailTarget('test', str(tmp_path / 'creds.json'), str(token)), mock_creds


@pytest.fixture
def gmail(tmp_path: Path) -> tuple[GmailTarget, MagicMock]:
    """A target with a mocked API service; the label map is not loaded yet."""
    target, _ = make_target(tmp_path)
    service = MagicMock()
    target.service = service
    return target, service


@pytest.fixture
def gmail_labels(gmail: tuple[GmailTarget, MagicMock]) -> tuple[GmailTarget, MagicMock]:
    """Like `gmail`, with the label map pre-populated to avoid a list_labels call."""
    gmail[0]._label_map = {'INBOX': 'INBOX', 'UNREAD': 'UNREAD', 'MyLabel': 'Label_123'}
    return gmail


def http_error(status: int, content: bytes) -> HttpError:
    response = MagicMock()
    response.status = status
    return HttpError(response, content)


def import_body(service: MagicMock) -> dict[str, object]:
    """The body of the last messages().import_() call."""
    body: dict[str, object] = service.users().messages().import_.call_args[1]['body']
    return body


class TestGmailTargetInit:
    """Tests for GmailTarget initialization."""

    @patch('korgalore.gmail_target.Credentials')
    def test_loads_existing_valid_token(self, mock_credentials: MagicMock, tmp_path: Path) -> None:
        """Loads credentials from the stored token; the service waits for connect()."""
        token = tmp_path / 'token.json'
        token.write_text('{}')
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_credentials.from_authorized_user_file.return_value = mock_creds

        target = GmailTarget('test', str(tmp_path / 'creds.json'), str(token))

        assert target.identifier == 'test'
        assert target.creds is mock_creds
        assert target.service is None
        mock_credentials.from_authorized_user_file.assert_called_once_with(str(token), SCOPES)

    @patch('korgalore.gmail_target.Credentials')
    @patch('korgalore.gmail_target.Request')
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

        GmailTarget('test', str(tmp_path / 'creds.json'), str(token))

        mock_creds.refresh.assert_called_once()
        assert token.read_text() == '{"token": "refreshed"}'
        assert (token.stat().st_mode & 0o777) == 0o600

    @patch('korgalore.gmail_target.Credentials')
    @patch('korgalore.gmail_target.Request')
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
            GmailTarget('test', str(tmp_path / 'creds.json'), str(token))

        assert token.read_text() == '{"token": "stale"}'
        assert [p.name for p in tmp_path.iterdir()] == ['token.json']

    @patch('korgalore.gmail_target.InstalledAppFlow')
    def test_new_token_is_private_whatever_the_umask(self, mock_flow_class: MagicMock, tmp_path: Path) -> None:
        """The token file is created 0600; it is never readable by others, even briefly."""
        creds = tmp_path / 'creds.json'
        creds.write_text('{}')
        token = tmp_path / 'token.json'
        mock_flow_class.from_client_secrets_file.return_value.run_local_server.return_value.to_json.return_value = '{}'

        old_umask = os.umask(0)
        try:
            GmailTarget('test', str(creds), str(token))
        finally:
            os.umask(old_umask)

        assert (token.stat().st_mode & 0o777) == 0o600

    @patch('korgalore.gmail_target.Credentials')
    @patch('korgalore.gmail_target.Request')
    def test_revoked_token_is_set_aside(
        self, mock_request: MagicMock, mock_credentials: MagicMock, tmp_path: Path
    ) -> None:
        """A token that fails to refresh is renamed out of the way and auth is flagged."""
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

        target = GmailTarget('test', str(tmp_path / 'creds.json'), str(token), interactive=False)

        assert target.needs_auth
        assert not token.exists()
        assert invalid.read_text() == '{"token": "revoked"}'

    @patch('korgalore.gmail_target.InstalledAppFlow')
    def test_runs_oauth_flow_and_saves_token_when_no_token(self, mock_flow_class: MagicMock, tmp_path: Path) -> None:
        """Runs the OAuth flow when no token exists, then saves the new token."""
        creds = tmp_path / 'creds.json'
        creds.write_text('{}')
        token = tmp_path / 'token.json'
        mock_creds = MagicMock()
        mock_creds.to_json.return_value = '{"access_token": "new_token"}'
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_class.from_client_secrets_file.return_value = mock_flow

        target = GmailTarget('test', str(creds), str(token))

        mock_flow_class.from_client_secrets_file.assert_called_once_with(str(creds), SCOPES)
        mock_flow.run_local_server.assert_called_once_with(port=0)
        assert target.creds is mock_creds
        assert token.read_text() == '{"access_token": "new_token"}'

    def test_missing_credentials_file_raises(self, tmp_path: Path) -> None:
        """Missing credentials file raises ConfigurationError."""
        # Neither the token file nor the credentials file exists
        with pytest.raises(ConfigurationError) as exc_info:
            GmailTarget('test', str(tmp_path / 'creds.json'), str(tmp_path / 'token.json'))
        assert 'not found' in str(exc_info.value)
        assert 'creds.json' in str(exc_info.value)

    @patch('korgalore.gmail_target.Credentials')
    def test_expands_user_paths(self, mock_credentials: MagicMock, tmp_path: Path) -> None:
        """Tilde and env vars in paths are expanded."""
        token = tmp_path / 'token.json'
        token.write_text('{}')
        mock_credentials.from_authorized_user_file.return_value = MagicMock(valid=True)

        with patch.dict('os.environ', {'HOME': str(tmp_path)}):
            GmailTarget('test', '~/creds.json', '$HOME/token.json')

        mock_credentials.from_authorized_user_file.assert_called_once_with(str(token), SCOPES)


class TestGmailTargetConnect:
    """Tests for GmailTarget connect method."""

    @patch('korgalore.gmail_target.build')
    def test_connect_builds_service_once(self, mock_build: MagicMock, tmp_path: Path) -> None:
        """Connect builds the Gmail API service, and repeated calls don't rebuild it."""
        target, mock_creds = make_target(tmp_path)
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        target.connect()
        target.connect()
        target.connect()

        mock_build.assert_called_once_with('gmail', 'v1', credentials=mock_creds, cache_discovery=False)
        assert target.service is mock_service


class TestGmailTargetListLabels:
    """Tests for GmailTarget list_labels method."""

    @pytest.mark.parametrize(
        ('response', 'expected'),
        [
            (
                {
                    'labels': [
                        {'id': 'INBOX', 'name': 'INBOX'},
                        {'id': 'SENT', 'name': 'SENT'},
                        {'id': 'Label_123', 'name': 'MyLabel'},
                    ]
                },
                [
                    {'id': 'INBOX', 'name': 'INBOX'},
                    {'id': 'SENT', 'name': 'SENT'},
                    {'id': 'Label_123', 'name': 'MyLabel'},
                ],
            ),
            ({}, []),
        ],
        ids=['labels', 'empty'],
    )
    def test_list_labels(
        self, gmail: tuple[GmailTarget, MagicMock], response: dict[str, object], expected: list[object]
    ) -> None:
        target, service = gmail
        service.users().labels().list().execute.return_value = response

        assert target.list_labels() == expected
        service.users().labels().list.assert_called_with(userId='me')

    def test_list_labels_http_error(self, gmail: tuple[GmailTarget, MagicMock]) -> None:
        target, service = gmail
        service.users().labels().list().execute.side_effect = http_error(403, b'Forbidden')

        with pytest.raises(RemoteError, match='error occurred'):
            target.list_labels()


class TestGmailTargetTranslateLabels:
    """Tests for GmailTarget translate_labels method."""

    def test_translate_labels(self, gmail: tuple[GmailTarget, MagicMock]) -> None:
        """Label names become IDs, in order; the label map is fetched only once."""
        target, service = gmail
        service.users().labels().list().execute.return_value = {
            'labels': [
                {'id': 'INBOX', 'name': 'INBOX'},
                {'id': 'UNREAD', 'name': 'UNREAD'},
                {'id': 'Label_123', 'name': 'MyLabel'},
            ]
        }

        assert target.translate_labels(['INBOX', 'MyLabel', 'UNREAD']) == ['INBOX', 'Label_123', 'UNREAD']
        assert target.translate_labels(['INBOX']) == ['INBOX']
        assert target.translate_labels(['MyLabel']) == ['Label_123']

        assert service.users().labels().list().execute.call_count == 1

    def test_translate_unknown_label_raises(self, gmail: tuple[GmailTarget, MagicMock]) -> None:
        target, service = gmail
        service.users().labels().list().execute.return_value = {'labels': [{'id': 'INBOX', 'name': 'INBOX'}]}

        with pytest.raises(ConfigurationError) as exc_info:
            target.translate_labels(['NonExistent'])
        assert 'not found' in str(exc_info.value)
        assert 'NonExistent' in str(exc_info.value)

    def test_label_names_are_case_sensitive(self, gmail: tuple[GmailTarget, MagicMock]) -> None:
        target, _ = gmail
        target._label_map = {'INBOX': 'INBOX', 'inbox': 'inbox_lower'}

        assert target.translate_labels(['INBOX']) == ['INBOX']
        assert target.translate_labels(['inbox']) == ['inbox_lower']


class TestGmailTargetImportMessage:
    """Tests for GmailTarget import_message method."""

    def test_import_success_with_labels(self, gmail_labels: tuple[GmailTarget, MagicMock]) -> None:
        target, service = gmail_labels
        mock_result = {'id': 'msg123', 'labelIds': ['INBOX', 'UNREAD']}
        service.users().messages().import_().execute.return_value = mock_result

        result = target.import_message(b'From: test@example.com\r\nSubject: Test\r\n\r\nBody', ['INBOX', 'UNREAD'])

        assert result == mock_result
        assert service.users().messages().import_.call_args[1]['userId'] == 'me'
        body = import_body(service)
        assert 'raw' in body
        assert body['labelIds'] == ['INBOX', 'UNREAD']

    @pytest.mark.parametrize(
        ('labels', 'expected_ids'),
        [(['MyLabel'], ['Label_123']), ([], None)],
        ids=['names-translated-to-ids', 'no-labels'],
    )
    def test_import_label_ids(
        self,
        gmail_labels: tuple[GmailTarget, MagicMock],
        labels: list[str],
        expected_ids: list[str] | None,
    ) -> None:
        """Label names are translated; without labels there is no labelIds."""
        target, service = gmail_labels
        service.users().messages().import_().execute.return_value = {'id': 'msg123'}

        target.import_message(b'Test', labels)

        body = import_body(service)
        if expected_ids is None:
            assert 'labelIds' not in body
        else:
            assert body['labelIds'] == expected_ids

    @pytest.mark.parametrize(
        'payload',
        [
            b'Test message with special chars: +/=',
            bytes(b for b in range(256) if b != 0x0A),
            b'',
        ],
        ids=['ascii-punctuation', 'all-bytes-no-newline', 'empty'],
    )
    def test_import_base64_encoding(self, gmail_labels: tuple[GmailTarget, MagicMock], payload: bytes) -> None:
        """Message is base64 URL-safe encoded and decodes back to the payload."""
        target, service = gmail_labels
        service.users().messages().import_().execute.return_value = {'id': 'msg123'}

        result = target.import_message(payload, ['INBOX'])

        assert result == {'id': 'msg123'}
        encoded = import_body(service)['raw']
        assert isinstance(encoded, str)
        assert base64.urlsafe_b64decode(encoded) == payload

    def test_import_http_error(self, gmail_labels: tuple[GmailTarget, MagicMock]) -> None:
        target, service = gmail_labels
        service.users().messages().import_().execute.side_effect = http_error(500, b'Internal Server Error')

        with pytest.raises(RemoteError, match='error occurred'):
            target.import_message(b'Test', ['INBOX'])

    def test_import_unknown_label_raises(self, gmail_labels: tuple[GmailTarget, MagicMock]) -> None:
        target, _ = gmail_labels

        with pytest.raises(ConfigurationError, match='not found'):
            target.import_message(b'Test', ['UnknownLabel'])

    def test_multiple_imports(self, gmail_labels: tuple[GmailTarget, MagicMock]) -> None:
        target, service = gmail_labels
        service.users().messages().import_().execute.side_effect = [{'id': f'msg{i}'} for i in range(5)]

        results = [target.import_message(f'Message {i}'.encode(), ['INBOX']) for i in range(5)]

        assert [r['id'] for r in results] == ['msg0', 'msg1', 'msg2', 'msg3', 'msg4']
