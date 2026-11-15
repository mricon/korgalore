"""Tests for MaildirTarget message delivery."""

import logging
import mailbox
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from korgalore import ConfigurationError
from korgalore.maildir_target import MaildirTarget

SIMPLE_MESSAGE = b'From: test@example.com\nSubject: Test\n\nBody'


def count_new(maildir_path: Path) -> int:
    """Number of messages waiting in the new/ directory of a maildir."""
    return len(list((maildir_path / 'new').iterdir()))


def assert_is_maildir(path: Path) -> None:
    for sub in ('new', 'cur', 'tmp'):
        assert (path / sub).is_dir()


class TestMaildirTargetInit:
    """Tests for MaildirTarget initialization."""

    @pytest.mark.parametrize(
        'relative',
        ['test_maildir', 'nonexistent/parent/maildir'],
        ids=['plain', 'missing-parents'],
    )
    def test_creates_maildir_structure(self, tmp_path: Path, relative: str) -> None:
        maildir_path = tmp_path / relative
        target = MaildirTarget('my-maildir', str(maildir_path))

        assert target.identifier == 'my-maildir'
        assert target.maildir_path == maildir_path
        assert isinstance(target.maildir_path, Path)
        assert_is_maildir(maildir_path)

    def test_uses_existing_maildir(self, tmp_path: Path) -> None:
        """Initializing with existing maildir keeps what is in it."""
        maildir_path = tmp_path / 'existing_maildir'
        mailbox.Maildir(str(maildir_path), create=True)
        marker_file = maildir_path / 'cur' / 'marker'
        marker_file.touch()

        MaildirTarget('test', str(maildir_path))

        assert marker_file.exists()

    def test_path_expanded(self, tmp_path: Path) -> None:
        """Tilde in path is expanded."""
        with patch.object(Path, 'expanduser') as mock_expand:
            mock_expand.return_value = tmp_path / 'expanded'
            MaildirTarget('test', '~/mail')
            mock_expand.assert_called()

    @pytest.mark.parametrize(
        'error',
        [OSError('Cannot create maildir'), PermissionError('Access denied')],
        ids=['oserror', 'permission-error'],
    )
    def test_maildir_init_error_raises(self, tmp_path: Path, error: OSError) -> None:
        with patch('mailbox.Maildir') as mock_maildir:
            mock_maildir.side_effect = error
            with pytest.raises(ConfigurationError, match='Failed to initialize maildir'):
                MaildirTarget('test', str(tmp_path / 'mail'))


class TestMaildirTargetConnect:
    """Tests for MaildirTarget connect method."""

    def test_connect_logs_path(self, maildir: MaildirTarget, caplog: pytest.LogCaptureFixture) -> None:
        """Connect is a no-op that logs the maildir path."""
        caplog.set_level(logging.DEBUG, logger='korgalore')
        maildir.connect()
        assert str(maildir.maildir_path) in caplog.text


class TestMaildirTargetImportMessage:
    """Tests for MaildirTarget import_message method."""

    def test_message_delivered_to_new_and_preserved(self, maildir: MaildirTarget) -> None:
        raw_message = b'From: sender@example.com\nTo: recipient@example.com\nSubject: Important\n\nMessage body here.'

        # Labels are accepted but have no meaning for a maildir
        key = maildir.import_message(raw_message, ['INBOX', 'important'])

        assert key is not None
        assert count_new(maildir.maildir_path) == 1
        msg = mailbox.Maildir(str(maildir.maildir_path))[key]
        assert msg['From'] == 'sender@example.com'
        assert msg['To'] == 'recipient@example.com'
        assert msg['Subject'] == 'Important'

    @pytest.mark.parametrize(
        'raw_message',
        [
            SIMPLE_MESSAGE,
            b'From: test@example.com\nSubject: Empty\n\n',
            b'From: test@example.com\nContent-Type: application/octet-stream\n\n' + bytes(range(256)),
        ],
        ids=['simple', 'empty-body', 'binary-body'],
    )
    def test_stored_with_lf_line_endings(self, maildir: MaildirTarget, raw_message: bytes) -> None:
        """An LF message is stored byte for byte, not converted to CRLF.

        Maildir files use the local Unix convention, and readers such as
        mutt, notmuch and Dovecot expect LF, and can show a CR left in as
        a stray character.
        """
        key = maildir.import_message(raw_message, [])

        assert mailbox.Maildir(str(maildir.maildir_path)).get_bytes(key) == raw_message

    def test_trace_header_stored_with_lf(self, maildir: MaildirTarget) -> None:
        """The folded trace header uses the same LF line endings as the rest of the file."""
        key = maildir.import_message(SIMPLE_MESSAGE, [], feed_name='feed', delivery_name='delivery')

        stored = mailbox.Maildir(str(maildir.maildir_path)).get_bytes(key)
        assert b'X-Korgalore-Trace: from feed=feed for delivery=delivery' in stored
        assert re.search(rb'X-Korgalore-Trace:[^\n]*\n ', stored)
        assert b'\r' not in stored

    def test_identical_messages_get_unique_keys(self, maildir: MaildirTarget) -> None:
        """Maildir generates unique filenames even for identical messages."""
        keys = [maildir.import_message(SIMPLE_MESSAGE, []) for _ in range(20)]

        assert len(set(keys)) == 20
        assert count_new(maildir.maildir_path) == 20

    def test_delivery_error_raises(self, maildir: MaildirTarget) -> None:
        with patch.object(maildir.maildir, 'add') as mock_add:
            mock_add.side_effect = OSError('Disk full')
            with pytest.raises(ConfigurationError) as exc_info:
                maildir.import_message(b'test', [])

        assert 'Failed to deliver to maildir' in str(exc_info.value)
        assert 'Disk full' in str(exc_info.value)


class TestMaildirTargetSubfolder:
    """Tests for Maildir subfolder support."""

    @pytest.mark.parametrize(
        'subfolder',
        ['Lists/LKML', 'Archive/2024', 'Level1/Level2/Level3'],
        ids=['lists-lkml', 'archive-year', 'three-levels'],
    )
    def test_subfolder_created_and_used(self, maildir: MaildirTarget, subfolder: str) -> None:
        """The subfolder maildir (with parents) is created and gets the message."""
        key = maildir.import_message(SIMPLE_MESSAGE, [], subfolder=subfolder)

        subfolder_path = maildir.maildir_path / subfolder
        assert_is_maildir(subfolder_path)
        # The message is in the subfolder, not the base maildir
        assert count_new(maildir.maildir_path) == 0
        assert count_new(subfolder_path) == 1
        assert mailbox.Maildir(str(subfolder_path))[key]['Subject'] == 'Test'

    def test_subfolders_are_independent_and_cached(self, maildir: MaildirTarget) -> None:
        for i in range(3):
            maildir.import_message(f'Subject: Test {i}\n\nBody'.encode(), [], subfolder='Lists/Test')
        maildir.import_message(SIMPLE_MESSAGE, [], subfolder='Archive/2024')

        assert set(maildir._subfolder_maildirs) == {'Lists/Test', 'Archive/2024'}
        assert count_new(maildir.maildir_path / 'Lists' / 'Test') == 3
        assert count_new(maildir.maildir_path / 'Archive' / '2024') == 1
        assert count_new(maildir.maildir_path) == 0


class TestMaildirTargetIntegration:
    """Integration tests with real maildir operations."""

    def test_full_workflow(self, tmp_path: Path) -> None:
        """Init, connect, deliver, then read back after reopening the maildir."""
        maildir_path = tmp_path / 'integration_test'
        target = MaildirTarget('integration', str(maildir_path))
        target.connect()

        messages: list[bytes] = [
            b'From: alice@example.com\nSubject: Hello\n\nHi there!',
            b'From: bob@example.com\nSubject: Re: Hello\n\nHi back!',
            b"From: charlie@example.com\nSubject: Meeting\n\nLet's meet.",
        ]
        keys = [target.import_message(msg, ['label']) for msg in messages]

        # A fresh target on the same path sees everything that was delivered
        MaildirTarget('integration', str(maildir_path))
        mbox = mailbox.Maildir(str(maildir_path))
        assert len(mbox) == 3
        assert {mbox[k]['Subject'] for k in keys} == {'Hello', 'Re: Hello', 'Meeting'}
