"""Tests for ImapTarget message delivery."""

import imaplib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from korgalore import ConfigurationError, RemoteError
from korgalore.imap_target import ImapTarget
from korgalore.message import RawMessage
from tests.target_helpers import DATED_MESSAGE, UNDATED_MESSAGE, valid_token_file

SERVER = 'imap.example.com'
USER = 'user@example.com'
MSG_WITH_ID = b'From: test@example.com\r\nMessage-ID: <test@example.com>\r\n\r\nBody'


def make_target(**kwargs: Any) -> ImapTarget:
    """An ImapTarget with password auth, overridable per test."""
    params: dict[str, Any] = {
        'identifier': 'test',
        'server': SERVER,
        'username': USER,
        'password': 'secret',
    }
    params.update(kwargs)
    return ImapTarget(**params)


@pytest.fixture
def mock_imap_class() -> Iterator[MagicMock]:
    imap_class = MagicMock()
    with patch('korgalore.imap_target.imaplib.IMAP4_SSL', imap_class):
        yield imap_class


@pytest.fixture
def mock_imap(mock_imap_class: MagicMock) -> MagicMock:
    """The IMAP connection every ImapTarget gets: everything succeeds."""
    imap = MagicMock()
    mock_imap_class.return_value = imap
    imap.login.return_value = ('OK', [])
    imap.authenticate.return_value = ('OK', [b'Success'])
    imap.select.return_value = ('OK', [b'1'])
    imap.search.return_value = ('OK', [b''])
    imap.append.return_value = ('OK', [b'Done'])
    return imap


@pytest.fixture
def imap(mock_imap: MagicMock) -> tuple[ImapTarget, MagicMock]:
    """A default password target (not yet connected) and its mock connection."""
    return make_target(), mock_imap


@pytest.fixture
def connected(imap: tuple[ImapTarget, MagicMock]) -> tuple[ImapTarget, MagicMock]:
    """Like `imap`, but already connected."""
    imap[0].connect()
    return imap


class TestImapTargetInit:
    """Tests for ImapTarget initialization and validation."""

    @pytest.mark.parametrize(
        ('kwargs', 'folder', 'timeout'),
        [
            ({}, 'INBOX', 60),
            ({'folder': 'Archive/2024', 'timeout': 120}, 'Archive/2024', 120),
        ],
        ids=['defaults', 'custom-folder-and-timeout'],
    )
    def test_valid_config_with_password(self, kwargs: dict[str, Any], folder: str, timeout: int) -> None:
        target = make_target(**kwargs)
        assert target.identifier == 'test'
        assert target.server == SERVER
        assert target.username == USER
        assert target.password == 'secret'
        assert target.folder == folder
        assert target.timeout == timeout
        # Nothing connects until connect() is called, and password auth never needs a login flow
        assert target.imap is None
        assert not target.needs_auth

    @pytest.mark.parametrize(
        ('content', 'expected'),
        [('file_secret\n', 'file_secret'), ('  secret_with_spaces  \n\n', 'secret_with_spaces')],
        ids=['plain', 'whitespace-stripped'],
    )
    def test_valid_config_with_password_file(self, tmp_path: Path, content: str, expected: str) -> None:
        pw_file = tmp_path / 'password.txt'
        pw_file.write_text(content)

        target = make_target(password=None, password_file=str(pw_file))
        assert target.password == expected

    def test_password_file_with_tilde(self, tmp_path: Path) -> None:
        """Password file path with tilde is expanded."""
        pw_file = tmp_path / 'password.txt'
        pw_file.write_text('secret')

        with patch.object(Path, 'expanduser', return_value=pw_file):
            target = make_target(password=None, password_file='~/password.txt')
        assert target.password == 'secret'

    def test_password_takes_precedence_over_file(self, tmp_path: Path) -> None:
        pw_file = tmp_path / 'password.txt'
        pw_file.write_text('file_password')

        target = make_target(password='direct_password', password_file=str(pw_file))
        assert target.password == 'direct_password'

    @pytest.mark.parametrize(
        ('kwargs', 'message'),
        [
            ({'server': ''}, 'No server specified'),
            ({'username': ''}, 'No username specified'),
            ({'password': None}, 'No password or password_file specified'),
            (
                {'password': None, 'password_file': '/nonexistent/path/password.txt'},
                'Password file not found',
            ),
            ({'password': None, 'auth_type': 'invalid'}, 'Invalid auth_type'),
            ({'message_date': 'sender'}, 'invalid message_date'),
        ],
        ids=['no-server', 'no-username', 'no-password', 'missing-password-file', 'bad-auth-type', 'bad-message-date'],
    )
    def test_invalid_config_raises(self, kwargs: dict[str, Any], message: str) -> None:
        with pytest.raises(ConfigurationError, match=message):
            make_target(**kwargs)


class TestImapTargetConnect:
    """Tests for ImapTarget connect method."""

    @pytest.mark.parametrize(
        ('kwargs', 'folder', 'timeout'),
        [
            ({}, 'INBOX', 60),
            ({'folder': 'Archive/Important', 'timeout': 300}, 'Archive/Important', 300),
        ],
        ids=['defaults', 'custom-folder-and-timeout'],
    )
    def test_connect_success(
        self,
        mock_imap_class: MagicMock,
        mock_imap: MagicMock,
        kwargs: dict[str, Any],
        folder: str,
        timeout: int,
    ) -> None:
        target = make_target(**kwargs)
        target.connect()

        mock_imap_class.assert_called_once_with(SERVER, timeout=timeout)
        mock_imap.login.assert_called_once_with(USER, 'secret')
        mock_imap.select.assert_called_once_with(folder, readonly=True)
        assert target.imap is mock_imap

    def test_connect_auth_failure(self, imap: tuple[ImapTarget, MagicMock]) -> None:
        target, mock_imap = imap
        mock_imap.login.side_effect = imaplib.IMAP4.error('Invalid credentials')

        with pytest.raises(RemoteError) as exc_info:
            target.connect()
        assert 'authentication failed' in str(exc_info.value)
        assert SERVER in str(exc_info.value)

    @pytest.mark.parametrize(
        ('select_kwargs'),
        [
            {'return_value': ('NO', [b'Folder not found'])},
            {'side_effect': imaplib.IMAP4.error('Folder does not exist')},
        ],
        ids=['bad-status', 'exception'],
    )
    def test_connect_folder_not_found(self, mock_imap: MagicMock, select_kwargs: dict[str, Any]) -> None:
        mock_imap.select.configure_mock(**select_kwargs)
        target = make_target(folder='NonExistent')

        with pytest.raises(ConfigurationError) as exc_info:
            target.connect()
        assert 'does not exist' in str(exc_info.value)
        assert 'NonExistent' in str(exc_info.value)

    def test_connect_idempotent(self, imap: tuple[ImapTarget, MagicMock], mock_imap_class: MagicMock) -> None:
        """Multiple connect() calls don't reconnect."""
        target, _ = imap
        target.connect()
        target.connect()
        target.connect()

        assert mock_imap_class.call_count == 1


class TestImapTargetImportMessage:
    """Tests for ImapTarget import_message method."""

    @pytest.mark.parametrize(
        ('search_result', 'appended'),
        [(b'', True), (b'42', False)],
        ids=['not-duplicate', 'duplicate'],
    )
    @pytest.mark.parametrize('subfolder', [None, 'Lists/LKML'], ids=['base-folder', 'subfolder'])
    def test_import_dedup(
        self,
        connected: tuple[ImapTarget, MagicMock],
        search_result: bytes,
        appended: bool,
        subfolder: str | None,
    ) -> None:
        """Duplicates are skipped; the check runs in the effective folder."""
        target, mock_imap = connected
        mock_imap.search.return_value = ('OK', [search_result])
        raw_message = b'From: test@example.com\r\nMessage-ID: <dup@example.com>\r\n\r\nBody'

        result = target.import_message(raw_message, [], subfolder=subfolder)

        effective = 'INBOX' if subfolder is None else f'INBOX/{subfolder}'
        assert effective in [c[0][0] for c in mock_imap.select.call_args_list]
        if appended:
            assert result == [b'Done']
            mock_imap.append.assert_called_once()
        else:
            assert result.get('skipped') is True
            mock_imap.append.assert_not_called()

    def test_import_success(self, connected: tuple[ImapTarget, MagicMock]) -> None:
        """Message is appended with no flags, current time and normalized line endings."""
        target, mock_imap = connected
        mock_imap.append.return_value = ('OK', [b'[APPENDUID 1234 5678]'])
        raw_message = b'From: test@example.com\nSubject: Test\n\nBody'

        result = target.import_message(raw_message, ['ignored', 'labels'])

        assert result == [b'[APPENDUID 1234 5678]']
        mock_imap.append.assert_called_once()
        # The arguments are folder, flags, datetime and message. Empty
        # flags mean unread, and an empty datetime means now.
        assert mock_imap.append.call_args[0] == (
            'INBOX',
            '',
            '',
            RawMessage(raw_message).as_bytes(),
        )

    @pytest.mark.parametrize(
        ('kwargs', 'raw_message', 'expected'),
        [
            ({}, DATED_MESSAGE, '"02-Nov-2026 09:58:00 +0000"'),
            ({'message_date': 'received'}, DATED_MESSAGE, '"02-Nov-2026 10:00:00 +0000"'),
            ({'message_date': 'date'}, UNDATED_MESSAGE, ''),
            ({'message_date': 'import'}, DATED_MESSAGE, ''),
        ],
        ids=['default-is-date', 'received', 'date-without-date', 'import'],
    )
    def test_import_append_date(
        self, mock_imap: MagicMock, kwargs: dict[str, Any], raw_message: bytes, expected: str
    ) -> None:
        """APPEND carries the message's own date; an empty one lets the server stamp import time."""
        target = make_target(**kwargs)

        target.import_message(raw_message, [])

        assert mock_imap.append.call_args[0][2] == expected
        # Only the stored date may change: rewriting the message would break its DKIM signature
        assert mock_imap.append.call_args[0][3] == raw_message.replace(b'\n', b'\r\n')

    @pytest.mark.parametrize(
        ('folder', 'subfolder', 'expected'),
        [
            ('Archive', None, 'Archive'),
            ('INBOX', 'Lists/LKML', 'INBOX/Lists/LKML'),
            ('Archive/2024', 'Projects/Korgalore', 'Archive/2024/Projects/Korgalore'),
        ],
        ids=['base', 'subfolder', 'nested'],
    )
    def test_import_to_correct_folder(
        self, mock_imap: MagicMock, folder: str, subfolder: str | None, expected: str
    ) -> None:
        target = make_target(folder=folder)
        target.connect()
        target.import_message(MSG_WITH_ID, [], subfolder=subfolder)

        assert mock_imap.append.call_args[0][0] == expected

    def test_import_auto_connects(self, imap: tuple[ImapTarget, MagicMock]) -> None:
        """import_message auto-connects if not connected."""
        target, mock_imap = imap
        target.import_message(b'Test message', [])

        mock_imap.login.assert_called_once()
        mock_imap.append.assert_called_once()

    @pytest.mark.parametrize(
        ('append_kwargs', 'message'),
        [
            ({'return_value': ('NO', [b'Quota exceeded'])}, 'APPEND failed'),
            ({'side_effect': imaplib.IMAP4.error('Server error')}, 'Failed to append'),
            ({'side_effect': OSError('Connection reset')}, 'delivery failed'),
        ],
        ids=['bad-status', 'imap-error', 'connection-error'],
    )
    def test_import_append_failure(
        self, connected: tuple[ImapTarget, MagicMock], append_kwargs: dict[str, Any], message: str
    ) -> None:
        target, mock_imap = connected
        mock_imap.append.configure_mock(**append_kwargs)

        with pytest.raises(RemoteError, match=message):
            target.import_message(b'Test', [])

    def test_import_multiple_messages(self, connected: tuple[ImapTarget, MagicMock]) -> None:
        target, mock_imap = connected
        for i in range(5):
            target.import_message(f'Message {i}'.encode(), [])

        assert mock_imap.append.call_count == 5

    def test_import_proceeds_without_message_id(self, connected: tuple[ImapTarget, MagicMock]) -> None:
        """No dedup check when Message-ID is missing."""
        target, mock_imap = connected
        target.import_message(b'From: test@example.com\r\n\r\nBody', [])

        mock_imap.search.assert_not_called()
        mock_imap.append.assert_called_once()

    @pytest.mark.parametrize(
        'payload',
        [b'', bytes(range(256)), b'Subject: x\n\n\xff\xfe\x00 binary\n'],
        ids=['empty', 'all-byte-values', 'binary-body'],
    )
    def test_odd_payloads_reach_append_normalized(
        self, connected: tuple[ImapTarget, MagicMock], payload: bytes
    ) -> None:
        target, mock_imap = connected
        target.import_message(payload, [])

        assert mock_imap.append.call_args[0][3] == RawMessage(payload).as_bytes()


class TestImapTargetOAuth2:
    """Tests for ImapTarget OAuth2 authentication."""

    @pytest.mark.parametrize(
        ('kwargs', 'client_id', 'tenant'),
        [
            ({}, 'DEFAULT', 'common'),
            ({'client_id': 'custom-client-id'}, 'custom-client-id', 'common'),
            ({'client_id': 'test-client-id', 'tenant': 'my-tenant-id'}, 'test-client-id', 'my-tenant-id'),
        ],
        ids=['default-client-id', 'custom-client-id', 'custom-tenant'],
    )
    def test_oauth2_configuration(self, tmp_path: Path, kwargs: dict[str, Any], client_id: str, tenant: str) -> None:
        from korgalore.oauth2_imap import DEFAULT_CLIENT_ID

        target = make_target(
            server='outlook.office365.com',
            username='user@company.com',
            password=None,
            auth_type='oauth2',
            token=str(tmp_path / 'token.json'),
            **kwargs,
        )
        assert target.auth_type == 'oauth2'
        assert target.password is None
        assert target._oauth2_authenticator is not None
        assert target._oauth2_authenticator.client_id == (DEFAULT_CLIENT_ID if client_id == 'DEFAULT' else client_id)
        assert target._oauth2_authenticator.tenant == tenant

    @pytest.mark.parametrize('has_token', [False, True], ids=['no-token-file', 'valid-token'])
    def test_oauth2_needs_auth(self, tmp_path: Path, has_token: bool) -> None:
        """needs_auth passes through to the authenticator."""
        token = valid_token_file(tmp_path) if has_token else tmp_path / 'nonexistent-token.json'
        target = make_target(auth_type='oauth2', password=None, client_id='test-client-id', token=str(token))
        assert target.needs_auth is not has_token

    def test_reauthenticate_password_raises(self) -> None:
        with pytest.raises(ConfigurationError, match='not configured for OAuth2'):
            make_target().reauthenticate()

    def test_oauth2_connect_calls_authenticate(self, mock_imap: MagicMock, tmp_path: Path) -> None:
        """OAuth2 connection uses AUTHENTICATE instead of LOGIN."""
        target = make_target(
            auth_type='oauth2',
            password=None,
            client_id='test-client-id',
            token=str(valid_token_file(tmp_path, access_token='test_access_token')),
        )
        target.connect()

        mock_imap.authenticate.assert_called_once()
        assert mock_imap.authenticate.call_args[0][0] == 'XOAUTH2'
        mock_imap.login.assert_not_called()

    def test_oauth2_connect_auth_failure(self, mock_imap: MagicMock, tmp_path: Path) -> None:
        mock_imap.authenticate.side_effect = imaplib.IMAP4.error('AUTHENTICATE failed')
        target = make_target(
            auth_type='oauth2',
            password=None,
            client_id='test-client-id',
            token=str(valid_token_file(tmp_path, access_token='invalid_token')),
        )

        with pytest.raises(RemoteError, match='XOAUTH2 authentication failed'):
            target.connect()


class TestImapTargetDisconnect:
    """Tests for ImapTarget disconnect method."""

    def test_disconnect_closes_connection(self, connected: tuple[ImapTarget, MagicMock]) -> None:
        target, mock_imap = connected
        assert target.imap is not None

        target.disconnect()

        mock_imap.logout.assert_called_once()
        assert target.imap is None

    def test_disconnect_handles_logout_error(self, connected: tuple[ImapTarget, MagicMock]) -> None:
        target, mock_imap = connected
        mock_imap.logout.side_effect = imaplib.IMAP4.error('Connection lost')

        target.disconnect()  # Should not raise
        assert target.imap is None

    def test_disconnect_when_not_connected(self, imap: tuple[ImapTarget, MagicMock]) -> None:
        target, _ = imap
        assert target.imap is None

        target.disconnect()  # Should not raise
        assert target.imap is None

    def test_disconnect_allows_reconnect(
        self, connected: tuple[ImapTarget, MagicMock], mock_imap_class: MagicMock
    ) -> None:
        """After disconnect(), connect() establishes a new connection."""
        target, _ = connected
        target.disconnect()
        assert target.imap is None

        target.connect()
        # Read into a fresh local: the assert above narrowed the attribute to
        # None, and the checkers cannot see connect() repopulate it.
        reconnected: Any = target.imap
        assert reconnected is not None
        assert mock_imap_class.call_count == 2


class TestImapTargetDeduplication:
    """Tests for the Message-ID existence check."""

    @pytest.mark.parametrize(
        ('search_result', 'expected'),
        [(b'42', True), (b'', False)],
        ids=['found', 'not-found'],
    )
    def test_check_message_exists(
        self, connected: tuple[ImapTarget, MagicMock], search_result: bytes, expected: bool
    ) -> None:
        target, mock_imap = connected
        mock_imap.search.return_value = ('OK', [search_result])

        assert target._check_message_exists('<test@example.com>', 'INBOX') is expected
        mock_imap.search.assert_called_once_with(None, 'HEADER', 'Message-ID', '<test@example.com>')

    def test_check_message_exists_error_returns_false(self, connected: tuple[ImapTarget, MagicMock]) -> None:
        """Fail-open on IMAP error."""
        target, mock_imap = connected
        mock_imap.search.side_effect = imaplib.IMAP4.error('Search failed')

        assert target._check_message_exists('<test@example.com>', 'INBOX') is False

    def test_check_message_exists_no_connection(self, imap: tuple[ImapTarget, MagicMock]) -> None:
        target, _ = imap
        assert target._check_message_exists('<test@example.com>', 'INBOX') is False
