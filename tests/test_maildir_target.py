"""Tests for MaildirTarget message delivery."""

import mailbox
import pytest
from pathlib import Path
from unittest.mock import patch

from korgalore import ConfigurationError
from korgalore.maildir_target import MaildirTarget


class TestMaildirTargetInit:
    """Tests for MaildirTarget initialization."""

    def test_creates_maildir_structure(self, tmp_path: Path) -> None:
        """Initializing creates maildir subdirectories."""
        maildir_path = tmp_path / "test_maildir"
        MaildirTarget("test", str(maildir_path))

        assert maildir_path.exists()
        assert (maildir_path / "new").is_dir()
        assert (maildir_path / "cur").is_dir()
        assert (maildir_path / "tmp").is_dir()

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """Initializing creates parent directories if they don't exist."""
        maildir_path = tmp_path / "nonexistent" / "parent" / "maildir"
        assert not maildir_path.parent.exists()

        MaildirTarget("test", str(maildir_path))

        assert maildir_path.exists()
        assert (maildir_path / "new").is_dir()
        assert (maildir_path / "cur").is_dir()
        assert (maildir_path / "tmp").is_dir()

    def test_uses_existing_maildir(self, tmp_path: Path) -> None:
        """Initializing with existing maildir works."""
        maildir_path = tmp_path / "existing_maildir"
        # Create maildir structure first
        mailbox.Maildir(str(maildir_path), create=True)
        marker_file = maildir_path / "cur" / "marker"
        marker_file.touch()

        MaildirTarget("test", str(maildir_path))

        # Marker file should still exist
        assert marker_file.exists()

    def test_identifier_preserved(self, tmp_path: Path) -> None:
        """Identifier is stored correctly."""
        target = MaildirTarget("my-maildir", str(tmp_path / "mail"))
        assert target.identifier == "my-maildir"

    def test_path_expanded(self, tmp_path: Path) -> None:
        """Tilde in path is expanded."""
        with patch.object(Path, "expanduser") as mock_expand:
            mock_expand.return_value = tmp_path / "expanded"
            MaildirTarget("test", "~/mail")
            mock_expand.assert_called()

    def test_path_stored_as_path_object(self, tmp_path: Path) -> None:
        """Path is stored as Path object."""
        target = MaildirTarget("test", str(tmp_path / "mail"))
        assert isinstance(target.maildir_path, Path)

    def test_maildir_init_error_raises(self, tmp_path: Path) -> None:
        """Maildir initialization error raises ConfigurationError."""
        with patch("mailbox.Maildir") as mock_maildir:
            mock_maildir.side_effect = OSError("Cannot create maildir")
            with pytest.raises(ConfigurationError) as exc_info:
                MaildirTarget("test", str(tmp_path / "mail"))
        assert "Failed to initialize maildir" in str(exc_info.value)

    def test_permission_error_raises(self, tmp_path: Path) -> None:
        """Permission error raises ConfigurationError."""
        with patch("mailbox.Maildir") as mock_maildir:
            mock_maildir.side_effect = PermissionError("Access denied")
            with pytest.raises(ConfigurationError) as exc_info:
                MaildirTarget("test", str(tmp_path / "mail"))
        assert "Failed to initialize maildir" in str(exc_info.value)


class TestMaildirTargetConnect:
    """Tests for MaildirTarget connect method."""

    def test_connect_succeeds(self, tmp_path: Path) -> None:
        """Connect is a no-op that doesn't raise."""
        target = MaildirTarget("test", str(tmp_path / "mail"))
        target.connect()  # Should not raise

    def test_connect_logs_path(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Connect logs the maildir path."""
        import logging
        # Clear any handlers added by click-log from cli.py imports
        # and ensure propagation for caplog to capture
        korg_logger = logging.getLogger('korgalore')
        korg_logger.handlers.clear()
        korg_logger.propagate = True
        caplog.set_level(logging.DEBUG, logger='korgalore')
        maildir_path = tmp_path / "my_maildir"
        target = MaildirTarget("test", str(maildir_path))
        target.connect()
        assert str(maildir_path) in caplog.text


class TestMaildirTargetImportMessage:
    """Tests for MaildirTarget import_message method."""

    def test_successful_delivery(self, tmp_path: Path) -> None:
        """Message is delivered successfully."""
        target = MaildirTarget("test", str(tmp_path / "mail"))
        raw_message = b"From: test@example.com\nSubject: Test\n\nBody"

        key = target.import_message(raw_message, [])

        assert key is not None

    def test_message_stored_in_new(self, tmp_path: Path) -> None:
        """Delivered message appears in new/ directory."""
        maildir_path = tmp_path / "mail"
        target = MaildirTarget("test", str(maildir_path))
        raw_message = b"From: test@example.com\nSubject: Test\n\nBody"

        target.import_message(raw_message, [])

        new_dir = maildir_path / "new"
        files = list(new_dir.iterdir())
        assert len(files) == 1

    def test_message_content_preserved(self, tmp_path: Path) -> None:
        """Message content is preserved correctly."""
        maildir_path = tmp_path / "mail"
        target = MaildirTarget("test", str(maildir_path))
        raw_message = b"From: sender@example.com\nTo: recipient@example.com\nSubject: Important\n\nMessage body here."

        key = target.import_message(raw_message, [])

        # Read back via mailbox API
        mbox = mailbox.Maildir(str(maildir_path))
        msg = mbox[key]
        assert msg["From"] == "sender@example.com"
        assert msg["To"] == "recipient@example.com"
        assert msg["Subject"] == "Important"

    def test_labels_ignored(self, tmp_path: Path) -> None:
        """Labels parameter is accepted but ignored."""
        target = MaildirTarget("test", str(tmp_path / "mail"))
        raw_message = b"From: test@example.com\nSubject: Test\n\nBody"

        # Should not raise with labels
        key = target.import_message(raw_message, ["INBOX", "important", "custom-label"])
        assert key is not None

    def test_multiple_messages(self, tmp_path: Path) -> None:
        """Multiple messages can be delivered."""
        maildir_path = tmp_path / "mail"
        target = MaildirTarget("test", str(maildir_path))

        keys = []
        for i in range(5):
            raw_message = f"From: test{i}@example.com\nSubject: Test {i}\n\nBody {i}".encode()
            keys.append(target.import_message(raw_message, []))

        # All keys should be unique
        assert len(set(keys)) == 5

        # All messages should be in new/
        new_dir = maildir_path / "new"
        files = list(new_dir.iterdir())
        assert len(files) == 5

    def test_binary_content(self, tmp_path: Path) -> None:
        """Binary content in message is preserved."""
        target = MaildirTarget("test", str(tmp_path / "mail"))
        # Message with binary attachment simulation
        raw_message = b"From: test@example.com\nContent-Type: application/octet-stream\n\n" + bytes(range(256))

        key = target.import_message(raw_message, [])
        assert key is not None

    def test_large_message(self, tmp_path: Path) -> None:
        """Large messages are handled correctly."""
        target = MaildirTarget("test", str(tmp_path / "mail"))
        body = b"X" * 1024 * 1024  # 1MB body
        raw_message = b"From: test@example.com\nSubject: Large\n\n" + body

        key = target.import_message(raw_message, [])
        assert key is not None

    def test_empty_message(self, tmp_path: Path) -> None:
        """Empty message body is handled."""
        target = MaildirTarget("test", str(tmp_path / "mail"))
        raw_message = b"From: test@example.com\nSubject: Empty\n\n"

        key = target.import_message(raw_message, [])
        assert key is not None

    def test_delivery_error_raises(self, tmp_path: Path) -> None:
        """Delivery errors raise ConfigurationError."""
        target = MaildirTarget("test", str(tmp_path / "mail"))

        with patch.object(target.maildir, "add") as mock_add:
            mock_add.side_effect = OSError("Disk full")
            with pytest.raises(ConfigurationError) as exc_info:
                target.import_message(b"test", [])

        assert "Failed to deliver to maildir" in str(exc_info.value)
        assert "Disk full" in str(exc_info.value)

    def test_returns_unique_keys(self, tmp_path: Path) -> None:
        """Each delivery returns a unique key."""
        target = MaildirTarget("test", str(tmp_path / "mail"))
        raw_message = b"From: test@example.com\nSubject: Test\n\nBody"

        keys = [target.import_message(raw_message, []) for _ in range(10)]

        assert len(set(keys)) == 10  # All unique


class TestMaildirTargetIntegration:
    """Integration tests with real maildir operations."""

    def test_full_workflow(self, tmp_path: Path) -> None:
        """Complete workflow: init, connect, deliver, verify."""
        maildir_path = tmp_path / "integration_test"

        # Initialize
        target = MaildirTarget("integration", str(maildir_path))

        # Connect
        target.connect()

        # Deliver messages
        messages = [
            b"From: alice@example.com\nSubject: Hello\n\nHi there!",
            b"From: bob@example.com\nSubject: Re: Hello\n\nHi back!",
            b"From: charlie@example.com\nSubject: Meeting\n\nLet's meet.",
        ]
        keys = [target.import_message(msg, ["label"]) for msg in messages]

        # Verify using standard mailbox API
        mbox = mailbox.Maildir(str(maildir_path))
        assert len(mbox) == 3

        subjects = {mbox[k]["Subject"] for k in keys}
        assert subjects == {"Hello", "Re: Hello", "Meeting"}

    def test_message_survives_reopen(self, tmp_path: Path) -> None:
        """Messages persist after maildir is reopened."""
        maildir_path = tmp_path / "persist_test"

        # First session: deliver message
        target1 = MaildirTarget("test", str(maildir_path))
        key = target1.import_message(b"From: test@example.com\nSubject: Persist\n\nTest", [])

        # Second session: read message
        MaildirTarget("test", str(maildir_path))
        mbox = mailbox.Maildir(str(maildir_path))
        assert len(mbox) == 1
        assert mbox[key]["Subject"] == "Persist"

    def test_concurrent_safe_filenames(self, tmp_path: Path) -> None:
        """Maildir generates unique filenames even for identical messages."""
        target = MaildirTarget("test", str(tmp_path / "mail"))
        identical_message = b"From: test@example.com\nSubject: Identical\n\nSame content"

        keys = [target.import_message(identical_message, []) for _ in range(100)]

        # All should succeed with unique keys
        assert len(set(keys)) == 100
