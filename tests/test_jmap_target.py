"""Tests for JmapTarget message delivery."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from korgalore import ConfigurationError, RemoteError
from korgalore.jmap_target import JmapTarget
from korgalore.message import RawMessage
from tests.target_helpers import DATED_MESSAGE, UNDATED_MESSAGE

SERVER = 'https://api.example.com'

# Sample JMAP session response
SAMPLE_SESSION = {
    'apiUrl': 'https://api.example.com/jmap/api/',
    'uploadUrl': 'https://api.example.com/jmap/upload/{accountId}/',
    'accounts': {'acc-123': {'name': 'user@example.com'}, 'acc-456': {'name': 'other@example.com'}},
}

# Sample mailbox list response
SAMPLE_MAILBOXES_RESPONSE = {
    'methodResponses': [
        ['Mailbox/query', {'ids': ['mb-1', 'mb-2', 'mb-3']}, 'call-0'],
        [
            'Mailbox/get',
            {
                'list': [
                    {'id': 'mb-1', 'name': 'Inbox', 'role': 'inbox'},
                    {'id': 'mb-2', 'name': 'Sent', 'role': 'sent'},
                    {'id': 'mb-3', 'name': 'Archive', 'role': ''},
                ]
            },
            'call-1',
        ],
    ]
}

MAILBOX_MAP = {'inbox': 'mb-1', 'sent': 'mb-2', 'archive': 'mb-3'}


def make_target(**kwargs: Any) -> JmapTarget:
    """A JmapTarget with a bearer token, overridable per test."""
    params: dict[str, Any] = {
        'identifier': 'test',
        'server': SERVER,
        'username': 'user@example.com',
        'token': 'token',
    }
    params.update(kwargs)
    return JmapTarget(**params)


def json_response(payload: dict[str, Any]) -> MagicMock:
    """A requests response whose .json() is the given payload."""
    response = MagicMock()
    response.json.return_value = payload
    return response


def upload_response(blob_id: str = 'blob-123') -> MagicMock:
    return json_response({'blobId': blob_id})


def import_response(result: dict[str, Any]) -> MagicMock:
    """An Email/import response for the single email 'msg1'."""
    return json_response({'methodResponses': [['Email/import', result, 'call-0']]})


def created(email_id: str = 'email-456') -> MagicMock:
    return import_response({'created': {'msg1': {'id': email_id}}})


def query_response(ids: list[str]) -> MagicMock:
    return json_response({'methodResponses': [['Email/query', {'ids': ids}, 'call-0']]})


@pytest.fixture
def mock_post() -> Iterator[MagicMock]:
    post = MagicMock()
    with patch('korgalore.jmap_target.requests.post', post):
        yield post


def make_connected(**kwargs: Any) -> JmapTarget:
    """A target with session state pre-populated, as after connect()."""
    target = make_target(**kwargs)
    target.session = SAMPLE_SESSION
    target.account_id = 'acc-123'
    target.api_url = 'https://api.example.com/jmap/api/'
    target.upload_url = 'https://api.example.com/jmap/upload/acc-123/'
    return target


@pytest.fixture
def jmap() -> JmapTarget:
    return make_connected()


@pytest.fixture
def jmap_mailboxes(jmap: JmapTarget) -> JmapTarget:
    """Like `jmap`, with the mailbox cache pre-populated."""
    jmap._mailbox_map = dict(MAILBOX_MAP)
    return jmap


class TestJmapTargetInit:
    """Tests for JmapTarget initialization."""

    @pytest.mark.parametrize(
        ('kwargs', 'timeout'),
        [({}, 60), ({'server': SERVER + '/', 'timeout': 120}, 120)],
        ids=['defaults', 'trailing-slash-and-custom-timeout'],
    )
    def test_valid_config_with_token(self, kwargs: dict[str, Any], timeout: int) -> None:
        target = make_target(token='secret_token', **kwargs)
        assert target.identifier == 'test'
        assert target.server == SERVER  # trailing slash stripped
        assert target.username == 'user@example.com'
        assert target.token == 'secret_token'
        assert target.timeout == timeout
        # Session state is only filled in by connect()
        assert target.session is None
        assert target.account_id is None
        assert target.api_url is None
        assert target.upload_url is None

    @pytest.mark.parametrize(
        ('content', 'expected'),
        [('file_token\n', 'file_token'), ('  token_with_spaces  \n\n', 'token_with_spaces')],
        ids=['plain', 'whitespace-stripped'],
    )
    def test_valid_config_with_token_file(self, tmp_path: Path, content: str, expected: str) -> None:
        token_file = tmp_path / 'token.txt'
        token_file.write_text(content)

        target = make_target(token=None, token_file=str(token_file))
        assert target.token == expected

    def test_token_file_with_tilde(self, tmp_path: Path) -> None:
        """Token file path with tilde is expanded."""
        token_file = tmp_path / 'token.txt'
        token_file.write_text('secret')

        with patch.object(Path, 'expanduser', return_value=token_file):
            target = make_target(token=None, token_file='~/token.txt')
        assert target.token == 'secret'

    def test_token_takes_precedence_over_file(self, tmp_path: Path) -> None:
        token_file = tmp_path / 'token.txt'
        token_file.write_text('file_token')

        target = make_target(token='direct_token', token_file=str(token_file))
        assert target.token == 'direct_token'

    @pytest.mark.parametrize(
        ('kwargs', 'message'),
        [
            ({'token': None}, 'No token or token_file specified'),
            ({'token': None, 'token_file': '/nonexistent/path/token.txt'}, 'Token file not found'),
            ({'message_date': 'sender'}, 'invalid message_date'),
        ],
        ids=['no-token', 'missing-token-file', 'bad-message-date'],
    )
    def test_invalid_config_raises(self, kwargs: dict[str, Any], message: str) -> None:
        with pytest.raises(ConfigurationError, match=message):
            make_target(**kwargs)


class TestJmapTargetConnect:
    """Tests for JmapTarget connect method."""

    @patch('korgalore.jmap_target.requests.get')
    def test_connect_success_once(self, mock_get: MagicMock) -> None:
        """Session discovery fills in the session state, and repeated calls don't reconnect."""
        mock_get.return_value = json_response(SAMPLE_SESSION)

        target = make_target(token='secret_token')
        target.connect()
        target.connect()
        target.connect()

        mock_get.assert_called_once_with(
            'https://api.example.com/jmap/session', headers={'Authorization': 'Bearer secret_token'}, timeout=60
        )
        assert target.account_id == 'acc-123'
        assert target.api_url == 'https://api.example.com/jmap/api/'
        assert target.upload_url == 'https://api.example.com/jmap/upload/acc-123/'

    @pytest.mark.parametrize(
        ('session', 'error', 'message'),
        [
            (None, RemoteError, 'Failed to discover JMAP session'),
            ({'uploadUrl': 'https://api.example.com/upload/{accountId}/'}, RemoteError, 'missing apiUrl or uploadUrl'),
            ({'apiUrl': 'https://api.example.com/api/'}, RemoteError, 'missing apiUrl or uploadUrl'),
            (
                {
                    'apiUrl': 'https://api.example.com/api/',
                    'uploadUrl': 'https://api.example.com/upload/{accountId}/',
                    'accounts': {'acc-999': {'name': 'different@example.com'}},
                },
                ConfigurationError,
                'Account not found',
            ),
        ],
        ids=['request-failure', 'missing-api-url', 'missing-upload-url', 'account-not-found'],
    )
    @patch('korgalore.jmap_target.requests.get')
    def test_connect_failures(
        self,
        mock_get: MagicMock,
        session: dict[str, Any] | None,
        error: type[Exception],
        message: str,
    ) -> None:
        if session is None:
            mock_get.side_effect = requests.RequestException('Connection refused')
        else:
            mock_get.return_value = json_response(session)

        with pytest.raises(error, match=message):
            make_target().connect()


class TestJmapTargetUploadBlob:
    """Tests for JmapTarget _upload_blob method."""

    def test_upload_success(self, jmap: JmapTarget, mock_post: MagicMock) -> None:
        mock_post.return_value = upload_response('blob-abc123')

        assert jmap._upload_blob(b'Test message content') == 'blob-abc123'

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs['data'] == b'Test message content'
        assert call_kwargs['headers']['Content-Type'] == 'message/rfc822'
        assert 'Bearer token' in call_kwargs['headers']['Authorization']

    @pytest.mark.parametrize(
        ('post_kwargs', 'message'),
        [
            ({'side_effect': requests.RequestException('Upload failed')}, 'Failed to upload message blob'),
            ({'return_value': json_response({'size': 100})}, 'No blobId in upload response'),
        ],
        ids=['request-failure', 'missing-blob-id'],
    )
    def test_upload_failures(
        self, jmap: JmapTarget, mock_post: MagicMock, post_kwargs: dict[str, Any], message: str
    ) -> None:
        mock_post.configure_mock(**post_kwargs)

        with pytest.raises(RemoteError, match=message):
            jmap._upload_blob(b'Test')


class TestJmapTargetMailboxes:
    """Tests for list_mailboxes, list_labels and translate_folders."""

    def test_list_mailboxes_and_labels(self, jmap: JmapTarget, mock_post: MagicMock) -> None:
        mock_post.return_value = json_response(SAMPLE_MAILBOXES_RESPONSE)

        assert jmap.list_mailboxes() == [
            {'id': 'mb-1', 'name': 'Inbox', 'role': 'inbox'},
            {'id': 'mb-2', 'name': 'Sent', 'role': 'sent'},
            {'id': 'mb-3', 'name': 'Archive', 'role': ''},
        ]
        assert jmap.list_labels() == [
            {'name': 'Inbox', 'id': 'mb-1'},
            {'name': 'Sent', 'id': 'mb-2'},
            {'name': 'Archive', 'id': 'mb-3'},
        ]

    def test_list_mailboxes_request_failure(self, jmap: JmapTarget, mock_post: MagicMock) -> None:
        mock_post.side_effect = requests.RequestException('API error')

        with pytest.raises(RemoteError, match='Failed to list mailboxes'):
            jmap.list_mailboxes()

    @pytest.mark.parametrize(
        'folders',
        [['inbox', 'sent', 'archive'], ['INBOX', 'Sent', 'ARCHIVE']],
        ids=['exact', 'case-insensitive'],
    )
    def test_translate_folders(self, jmap_mailboxes: JmapTarget, folders: list[str]) -> None:
        assert jmap_mailboxes.translate_folders(folders) == ['mb-1', 'mb-2', 'mb-3']

    def test_translate_unknown_folder_raises(self, jmap_mailboxes: JmapTarget) -> None:
        with pytest.raises(ConfigurationError) as exc_info:
            jmap_mailboxes.translate_folders(['nonexistent'])
        assert 'not found' in str(exc_info.value)
        assert 'nonexistent' in str(exc_info.value)

    def test_translate_lazy_loads_mailboxes(self, jmap: JmapTarget, mock_post: MagicMock) -> None:
        """Mailbox map is lazy-loaded on first translation, matching by role or name."""
        mock_post.return_value = json_response(SAMPLE_MAILBOXES_RESPONSE)

        assert jmap._mailbox_map is None
        assert jmap.translate_folders(['inbox', 'sent']) == ['mb-1', 'mb-2']
        # Read into a fresh local: the assert above narrowed the attribute to
        # None, and the checkers cannot see translate_folders() populate it.
        populated: Any = jmap._mailbox_map
        assert populated is not None


class TestJmapTargetImportMessage:
    """Tests for JmapTarget import_message method."""

    def test_import_success(self, jmap_mailboxes: JmapTarget, mock_post: MagicMock) -> None:
        """Message is uploaded with CRLF line endings, then imported."""
        mock_post.side_effect = [upload_response(), created()]
        raw_message = b'From: a@b.com\nTo: c@d.com\n\nBody\nLine2'

        result = jmap_mailboxes.import_message(raw_message, ['inbox'])

        assert result == {'id': 'email-456'}
        assert mock_post.call_count == 2
        uploaded = mock_post.call_args_list[0][1]['data']
        assert uploaded == b'From: a@b.com\r\nTo: c@d.com\r\n\r\nBody\r\nLine2'
        assert uploaded == RawMessage(raw_message).as_bytes()

    @pytest.mark.parametrize(
        ('labels', 'expected_ids'),
        [
            ([], {'mb-1': True}),
            (['inbox', 'archive'], {'mb-1': True, 'mb-3': True}),
        ],
        ids=['default-to-inbox', 'multiple-folders'],
    )
    def test_import_mailbox_ids(
        self,
        jmap_mailboxes: JmapTarget,
        mock_post: MagicMock,
        labels: list[str],
        expected_ids: dict[str, bool],
    ) -> None:
        mock_post.side_effect = [upload_response(), created()]

        jmap_mailboxes.import_message(b'Test', labels)

        request_body = mock_post.call_args_list[1][1]['json']
        assert request_body['methodCalls'][0][1]['emails']['msg1']['mailboxIds'] == expected_ids

    @pytest.mark.parametrize(
        ('kwargs', 'raw_message', 'expected'),
        [
            ({}, DATED_MESSAGE, '2026-11-02T09:58:00Z'),
            ({'message_date': 'received'}, DATED_MESSAGE, '2026-11-02T10:00:00Z'),
            ({'message_date': 'date'}, UNDATED_MESSAGE, None),
        ],
        ids=['default-is-date', 'received', 'date-without-date'],
    )
    def test_import_received_at(
        self, mock_post: MagicMock, kwargs: dict[str, Any], raw_message: bytes, expected: str | None
    ) -> None:
        """receivedAt is the message's own date, or left to the server when the message has none."""
        target = make_connected(**kwargs)
        target._mailbox_map = dict(MAILBOX_MAP)
        mock_post.side_effect = [query_response([]), upload_response(), created()]

        target.import_message(raw_message, ['inbox'])

        email = mock_post.call_args[1]['json']['methodCalls'][0][1]['emails']['msg1']
        assert email.get('receivedAt') == expected
        # Only the stored date may change: rewriting the message would break its DKIM signature
        assert mock_post.call_args_list[1][1]['data'] == raw_message.replace(b'\n', b'\r\n')

    def test_import_received_at_import_time(self, mock_post: MagicMock) -> None:
        """Import mode must send the time explicitly: a missing receivedAt means the latest Received."""
        target = make_connected(message_date='import')
        target._mailbox_map = dict(MAILBOX_MAP)
        mock_post.side_effect = [query_response([]), upload_response(), created()]

        # receivedAt has whole seconds, so compare against whole seconds
        before = datetime.now(UTC).replace(microsecond=0)
        target.import_message(DATED_MESSAGE, ['inbox'])
        after = datetime.now(UTC)

        email = mock_post.call_args[1]['json']['methodCalls'][0][1]['emails']['msg1']
        received_at = datetime.strptime(email['receivedAt'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)
        assert before <= received_at <= after

    def test_import_already_exists(self, jmap_mailboxes: JmapTarget, mock_post: MagicMock) -> None:
        mock_post.side_effect = [
            upload_response(),
            import_response({'notCreated': {'msg1': {'type': 'alreadyExists', 'existingId': 'existing-789'}}}),
        ]

        assert jmap_mailboxes.import_message(b'Test', ['inbox']) == {'id': 'existing-789'}

    @pytest.mark.parametrize(
        ('import_result', 'message'),
        [
            (
                import_response({'notCreated': {'msg1': {'type': 'invalidEmail', 'description': 'Bad message'}}}),
                'Email/import failed',
            ),
            (json_response({'methodResponses': []}), 'Unexpected JMAP response'),
            (requests.RequestException('Network error'), 'Failed to import message'),
        ],
        ids=['not-created', 'unexpected-response', 'request-failure'],
    )
    def test_import_failures(
        self, jmap_mailboxes: JmapTarget, mock_post: MagicMock, import_result: Any, message: str
    ) -> None:
        mock_post.side_effect = [upload_response(), import_result]

        with pytest.raises(RemoteError, match=message):
            jmap_mailboxes.import_message(b'Test', ['inbox'])


class TestJmapTargetDeduplication:
    """Tests for JMAP message deduplication by Message-ID."""

    @pytest.mark.parametrize(
        ('ids', 'mailboxes', 'expected', 'expected_filter'),
        [
            (
                ['existing-email-id'],
                ['mb-1'],
                True,
                {'header': ['Message-ID', '<test@example.com>'], 'inMailbox': 'mb-1'},
            ),
            ([], ['mb-1'], False, {'header': ['Message-ID', '<test@example.com>'], 'inMailbox': 'mb-1'}),
            (
                [],
                ['mb-1', 'mb-2'],
                False,
                {
                    'operator': 'OR',
                    'conditions': [
                        {'header': ['Message-ID', '<test@example.com>'], 'inMailbox': 'mb-1'},
                        {'header': ['Message-ID', '<test@example.com>'], 'inMailbox': 'mb-2'},
                    ],
                },
            ),
        ],
        ids=['found', 'not-found', 'multiple-mailboxes-use-or'],
    )
    def test_check_message_exists(
        self,
        jmap_mailboxes: JmapTarget,
        mock_post: MagicMock,
        ids: list[str],
        mailboxes: list[str],
        expected: bool,
        expected_filter: dict[str, Any],
    ) -> None:
        mock_post.return_value = query_response(ids)

        assert jmap_mailboxes._check_message_exists('<test@example.com>', mailboxes) is expected

        assert mock_post.call_args[1]['json']['methodCalls'][0][1]['filter'] == expected_filter

    def test_check_message_exists_error_returns_false(self, jmap_mailboxes: JmapTarget, mock_post: MagicMock) -> None:
        """Fail-open on network error."""
        mock_post.side_effect = requests.RequestException('Network error')

        assert jmap_mailboxes._check_message_exists('<test@example.com>', ['mb-1']) is False

    @pytest.mark.parametrize(
        ('existing_ids', 'skipped'),
        [(['existing-id'], True), ([], False)],
        ids=['duplicate-skipped', 'not-duplicate-imported'],
    )
    def test_import_dedup(
        self, jmap_mailboxes: JmapTarget, mock_post: MagicMock, existing_ids: list[str], skipped: bool
    ) -> None:
        mock_post.side_effect = [query_response(existing_ids), upload_response(), created('new-email-id')]
        raw_message = b'From: test@example.com\r\nMessage-ID: <dup@example.com>\r\n\r\nBody'

        result = jmap_mailboxes.import_message(raw_message, ['inbox'])

        if skipped:
            assert result.get('skipped') is True
            # Only the query: no upload or import
            assert mock_post.call_count == 1
        else:
            assert result == {'id': 'new-email-id'}
            assert mock_post.call_count == 3  # query + upload + import

    def test_import_proceeds_without_message_id(self, jmap_mailboxes: JmapTarget, mock_post: MagicMock) -> None:
        """No dedup query when Message-ID is missing."""
        mock_post.side_effect = [upload_response(), created('new-email-id')]

        result = jmap_mailboxes.import_message(b'From: test@example.com\r\n\r\nBody', ['inbox'])

        assert result == {'id': 'new-email-id'}
        assert mock_post.call_count == 2  # upload + import only
