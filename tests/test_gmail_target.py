"""Tests for GmailTarget message delivery."""

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.credentials import Credentials as BaseCredentials
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

from korgalore import AuthenticationError, ConfigurationError, RemoteError
from korgalore.gmail_target import SCOPES, GmailTarget, make_gmail_credentials
from korgalore.google_creds import CommandCredentials, OAuthFileCredentials


def make_target() -> tuple[GmailTarget, MagicMock]:
    """A GmailTarget with valid mock credentials."""
    mock_creds = MagicMock(spec=BaseCredentials)
    mock_creds.valid = True
    return GmailTarget('test', creds=mock_creds), mock_creds


@pytest.fixture
def gmail() -> tuple[GmailTarget, MagicMock]:
    """A target with a mocked API service; the label map is not loaded yet."""
    target, _ = make_target()
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
    """Tests for GmailTarget initialization and credential delegation."""

    def test_init_with_credentials(self) -> None:
        """Stores identifier and credentials without building API service yet."""
        target, mock_creds = make_target()

        assert target.identifier == 'test'
        assert target.creds is mock_creds
        assert target.service is None

    def test_reauthenticate_delegates_and_resets_service(self, gmail_labels: tuple[GmailTarget, MagicMock]) -> None:
        """Delegates reauthenticate() to creds and clears cached service state."""
        target, _ = gmail_labels
        reauth_mock = MagicMock()
        setattr(target.creds, 'reauthenticate', reauth_mock)

        target.reauthenticate()

        reauth_mock.assert_called_once_with()
        assert target.service is None
        assert target._label_map is None

    def test_reauthenticate_unsupported_raises(self) -> None:
        """Raises AuthenticationError when credentials do not support reauth."""
        target, _ = make_target()

        with pytest.raises(AuthenticationError, match='does not support interactive reauthentication'):
            target.reauthenticate()


class TestGmailTargetConnect:
    """Tests for GmailTarget connect method."""

    @patch('korgalore.gmail_target.build')
    def test_connect_builds_service_once(self, mock_build: MagicMock) -> None:
        """Connect builds the Gmail API service, and repeated calls don't rebuild it."""
        target, mock_creds = make_target()
        mock_service = MagicMock()
        mock_service.users().labels().list().execute.return_value = {'labels': [{'id': 'INBOX', 'name': 'INBOX'}]}
        mock_build.return_value = mock_service

        target.connect()
        mock_build.assert_called_once_with('gmail', 'v1', credentials=mock_creds, cache_discovery=False)
        assert target.service is mock_service
        assert target._label_map == {'INBOX': 'INBOX'}

        target.connect()
        target.connect()
        mock_build.assert_called_once()

    @patch('korgalore.gmail_target.build')
    def test_connect_failure_resets_state(self, mock_build: MagicMock) -> None:
        """Label probe failure resets service and label map to None."""
        target, _ = make_target()
        mock_service = MagicMock()
        mock_service.users().labels().list().execute.side_effect = http_error(403, b'Forbidden')
        mock_build.return_value = mock_service

        with pytest.raises(RemoteError):
            target.connect()

        assert target.service is None
        assert target._label_map is None

    @patch('korgalore.gmail_target.build')
    @patch('korgalore.gmail_target.Request')
    def test_connect_refreshes_invalid_credentials(self, mock_request: MagicMock, mock_build: MagicMock) -> None:
        """Refreshes invalid credentials before building the API service."""
        target, mock_creds = make_target()
        mock_creds.valid = False
        mock_service = MagicMock()
        mock_service.users().labels().list().execute.return_value = {'labels': []}
        mock_build.return_value = mock_service

        target.connect()

        mock_creds.refresh.assert_called_once_with(mock_request.return_value)
        mock_build.assert_called_once()
        assert target._label_map == {}


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

    @patch('korgalore.gmail_target.build')
    def test_translate_labels(self, mock_build: MagicMock) -> None:
        """Label names become IDs, in order; the label map is fetched only once."""
        target, _ = make_target()
        service = MagicMock()
        mock_build.return_value = service
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
        assert mock_build.call_count == 1

    @patch('korgalore.gmail_target.build')
    def test_translate_unknown_label_raises(self, mock_build: MagicMock) -> None:
        target, _ = make_target()
        service = MagicMock()
        mock_build.return_value = service
        service.users().labels().list().execute.return_value = {'labels': [{'id': 'INBOX', 'name': 'INBOX'}]}

        with pytest.raises(ConfigurationError) as exc_info:
            target.translate_labels(['NonExistent'])
        assert 'not found' in str(exc_info.value)
        assert 'NonExistent' in str(exc_info.value)

    def test_label_names_are_case_sensitive(self) -> None:
        target, _ = make_target()
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


class TestMakeGmailCredentials:
    """Tests for make_gmail_credentials configuration factory."""

    def test_no_credentials_or_token_command_raises(self) -> None:
        """Must specify either credentials or token_command."""
        with pytest.raises(ConfigurationError, match='must specify either'):
            make_gmail_credentials('test', {})

    @pytest.mark.parametrize(
        'details',
        [
            {'credentials': '/tmp/creds.json', 'token_command': 'echo token'},
            {'token': '/tmp/token.json', 'token_command': 'echo token'},
        ],
        ids=['credentials-and-command', 'token-and-command'],
    )
    def test_mutually_exclusive_token_command_options_raise(self, details: dict[str, object]) -> None:
        """Specifying token_command alongside credentials or token raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match='cannot specify both'):
            make_gmail_credentials('test', details)

    def test_token_command_invalid_syntax_raises(self) -> None:
        """Unclosed quotes in token_command raise ConfigurationError."""
        with pytest.raises(ConfigurationError, match='syntax'):
            make_gmail_credentials('test', {'token_command': 'unclosed "quote'})

    def test_token_command_returns_command_credentials(self) -> None:
        """Valid token_command builds CommandCredentials without running interactive OAuth."""
        creds = make_gmail_credentials('test', {'token_command': 'get-token --profile work'})
        assert isinstance(creds, CommandCredentials)
        assert creds.command == ['get-token', '--profile', 'work']

    @pytest.mark.parametrize(
        ('details', 'expected_match'),
        [
            ({'credentials': 123}, "'credentials' must be a string"),
            ({'credentials': '/tmp/creds.json', 'token': 456}, "'token' must be a string"),
            ({'token_command': 789}, "'token_command' must be a string"),
        ],
        ids=['non-str-credentials', 'non-str-token', 'non-str-token-command'],
    )
    def test_invalid_option_types_raise(self, details: dict[str, object], expected_match: str) -> None:
        """Non-string configuration values raise ConfigurationError."""
        with pytest.raises(ConfigurationError, match=expected_match):
            make_gmail_credentials('test', details)

    @patch('korgalore.google_creds.Credentials')
    def test_default_token_file_uses_xdg_config_dir(self, mock_credentials: MagicMock, tmp_path: Path) -> None:
        """When token is omitted, defaults to gmail-{identifier}-token.json in XDG config dir."""
        default_token = tmp_path / 'korgalore' / 'gmail-personal-token.json'
        default_token.parent.mkdir(parents=True)
        default_token.write_text('{}')
        mock_credentials.from_authorized_user_file.return_value = MagicMock(valid=True)

        with patch.dict('os.environ', {'XDG_CONFIG_HOME': str(tmp_path)}):
            creds = make_gmail_credentials('personal', {'credentials': str(tmp_path / 'creds.json')})

        mock_credentials.from_authorized_user_file.assert_called_once_with(str(default_token))
        assert isinstance(creds, OAuthFileCredentials)
        assert creds._scopes == SCOPES
