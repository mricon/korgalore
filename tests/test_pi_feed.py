"""Tests for PIFeed state management that need no git repository.

These tests cover the delivery tracking functionality including:
- mark_successful_delivery: removing entries from failed list
- mark_failed_delivery: adding/updating failed entries, rejection after timeout
- JSONL file operations
- feed locking, also when another process holds the lock
- the feed state file, also when it is corrupt

The git-backed parts of PIFeed are tested in test_pi_feed_git.py.
"""

import json
import logging
import subprocess
import sys
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from korgalore import FeedLockedError, PublicInboxError, StateCorruptError, StateError
from korgalore.pi_feed import LOCKED_FEEDS, RETRY_FAILED_INTERVAL, PIFeed
from tests.conftest import make_pi_feed

# Holds the lock in another process, because a process never blocks on
# its own POSIX locks
_HOLD_LOCK = """
import sys
from fcntl import LOCK_EX, lockf
fh = open(sys.argv[1], 'w')
lockf(fh, LOCK_EX)
print('locked', flush=True)
sys.stdin.read()
"""

OLD = '2024-01-01T00:00:00'


def write_state(path: Path, entries: list[tuple[Any, ...]]) -> None:
    """Write entries to a state file, one JSON list per line."""
    path.write_text(''.join(json.dumps(e) + '\n' for e in entries))


@pytest.fixture
def held_lock(temp_feed_dir: Path) -> Generator[None, None, None]:
    """Another process holds the lock of temp_feed_dir until the test ends."""
    holder = subprocess.Popen(
        [sys.executable, '-c', _HOLD_LOCK, str(temp_feed_dir / 'korgalore.lock')],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == 'locked\n'
        yield
    finally:
        holder.communicate('')


class TestJSONLOperations:
    """Tests for JSONL file read/write operations."""

    def test_read_empty_file(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Reading non-existent file returns empty list."""
        assert mock_feed._read_jsonl_file(temp_feed_dir / 'nonexistent.jsonl') == []

    def test_write_and_read_jsonl(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Write and read back JSONL data."""
        filepath = temp_feed_dir / 'test.jsonl'
        data: list[tuple[int | str, ...]] = [
            (1, 'abc123', '2024-01-01T00:00:00', 1),
            (2, 'def456', '2024-01-02T00:00:00', 2),
        ]
        mock_feed._write_jsonl_file(filepath, data)

        assert mock_feed._read_jsonl_file(filepath) == data

    def test_write_empty_list_removes_file(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Writing empty list removes the file."""
        filepath = temp_feed_dir / 'test.jsonl'
        filepath.write_text('[1, "abc"]\n')

        mock_feed._write_jsonl_file(filepath, [])
        assert not filepath.exists()

    def test_append_to_jsonl(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Append entries to JSONL file."""
        filepath = temp_feed_dir / 'test.jsonl'
        mock_feed._append_to_jsonl_file(filepath, (1, 'abc123'))
        mock_feed._append_to_jsonl_file(filepath, (2, 'def456'))

        assert mock_feed._read_jsonl_file(filepath) == [(1, 'abc123'), (2, 'def456')]

    @pytest.mark.parametrize(
        'content',
        [
            pytest.param(b'[1, "abc123"]\n[2, "def4', id='torn-last-line'),
            pytest.param(b'[1, "abc123"]\n\x00\x00\x00', id='nul-bytes'),
            pytest.param(b'\xff\xfe\n[1, "abc123"]\n', id='not-utf8'),
            pytest.param(b'{"epoch": 2}\n[1, "abc123"]\n', id='not-a-list'),
        ],
    )
    def test_read_drops_damaged_lines(
        self, mock_feed: PIFeed, temp_feed_dir: Path, content: bytes, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A crash damages one line; the other failed deliveries must still be retried.

        The damaged line is also removed from the file. Otherwise the next
        append lands on the end of a torn line and is lost with it.
        """
        filepath = temp_feed_dir / 'test.jsonl'
        filepath.write_bytes(content)

        with caplog.at_level(logging.WARNING, logger='korgalore'):
            assert mock_feed._read_jsonl_file(filepath) == [(1, 'abc123')]
        assert 'Dropped 1 unreadable' in caplog.text

        mock_feed._append_to_jsonl_file(filepath, (3, 'fed789'))
        assert mock_feed._read_jsonl_file(filepath) == [(1, 'abc123'), (3, 'fed789')]

    def test_read_ends_unterminated_last_line(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """An append torn just before its newline leaves a last line that still reads.

        The next append must not land on the end of it, or both entries are lost.
        """
        filepath = temp_feed_dir / 'test.jsonl'
        filepath.write_bytes(b'[1, "abc123"]\n[2, "def456"]')

        assert mock_feed._read_jsonl_file(filepath) == [(1, 'abc123'), (2, 'def456')]

        mock_feed._append_to_jsonl_file(filepath, (3, 'fed789'))
        assert mock_feed._read_jsonl_file(filepath) == [(1, 'abc123'), (2, 'def456'), (3, 'fed789')]

    def test_read_removes_file_with_only_damaged_lines(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        filepath = temp_feed_dir / 'test.jsonl'
        filepath.write_bytes(b'[1, "abc')

        assert mock_feed._read_jsonl_file(filepath) == []
        assert not filepath.exists()

    def test_write_reaches_disk_before_rename(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Renaming unsynced data can leave an empty file behind after a crash."""
        filepath = temp_feed_dir / 'test.jsonl'
        events: list[str] = []
        real_replace = Path.replace

        def replace(src: Path, dst: Path) -> Path:
            events.append('rename')
            return real_replace(src, dst)

        with (
            patch('korgalore.os.fsync', side_effect=lambda _fd: events.append('fsync')),
            patch.object(Path, 'replace', replace),
        ):
            mock_feed._write_jsonl_file(filepath, [(1, 'abc123')])

        assert events == ['fsync', 'rename']
        assert mock_feed._read_jsonl_file(filepath) == [(1, 'abc123')]


class TestMarkSuccessfulDelivery:
    """Tests for mark_successful_delivery function.

    Regression: a retried commit is older than the delivery pointer, so
    saving it with save_delivery_info would rewind the pointer and make
    every later commit get delivered again on each pull. Only a fresh
    success (was_failing=False) saves the pointer.

    See: e77298ce-1e3e-449f-9864-b4fcf77a00b4@app.fastmail.com
    """

    def test_success_without_prior_failure(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """A fresh success saves the pointer and doesn't touch the failed file."""
        failed_file = temp_feed_dir / 'korgalore.test-delivery.failed'
        write_state(failed_file, [(0, 'abc123', OLD, 1)])

        with patch.object(mock_feed, 'save_delivery_info') as mock_save:
            mock_feed.mark_successful_delivery('test-delivery', 0, 'abc123', was_failing=False)

        mock_save.assert_called_once()
        assert mock_feed._read_jsonl_file(failed_file) == [(0, 'abc123', OLD, 1)]

    def test_success_removes_from_failed_list(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Successful delivery with was_failing=True removes entry from failed list."""
        failed_file = temp_feed_dir / 'korgalore.test-delivery.failed'
        write_state(failed_file, [(0, 'abc123', OLD, 1), (0, 'def456', OLD, 2), (1, 'ghi789', OLD, 1)])

        with patch.object(mock_feed, 'save_delivery_info') as mock_save:
            mock_feed.mark_successful_delivery('test-delivery', 0, 'def456', was_failing=True)

        mock_save.assert_not_called()
        assert mock_feed._read_jsonl_file(failed_file) == [(0, 'abc123', OLD, 1), (1, 'ghi789', OLD, 1)]

    def test_success_entry_not_in_failed_list(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """No error if entry not in failed list."""
        failed_file = temp_feed_dir / 'korgalore.test-delivery.failed'
        write_state(failed_file, [(0, 'abc123', OLD, 1)])

        with patch.object(mock_feed, 'save_delivery_info') as mock_save:
            mock_feed.mark_successful_delivery('test-delivery', 0, 'nonexistent', was_failing=True)

        mock_save.assert_not_called()
        assert mock_feed._read_jsonl_file(failed_file) == [(0, 'abc123', OLD, 1)]

    def test_success_removes_last_entry_deletes_file(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Removing last entry from failed list deletes the file."""
        failed_file = temp_feed_dir / 'korgalore.test-delivery.failed'
        write_state(failed_file, [(0, 'abc123', OLD, 1)])

        with patch.object(mock_feed, 'save_delivery_info') as mock_save:
            mock_feed.mark_successful_delivery('test-delivery', 0, 'abc123', was_failing=True)

        mock_save.assert_not_called()
        assert not failed_file.exists()


class TestMarkFailedDelivery:
    """Tests for mark_failed_delivery function."""

    def test_new_failure_creates_entry(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """First failure creates new entry with retry count 1."""
        mock_feed.mark_failed_delivery('test-delivery', 0, 'abc123')

        result = mock_feed._read_jsonl_file(temp_feed_dir / 'korgalore.test-delivery.failed')
        assert len(result) == 1
        assert result[0][0] == 0  # epoch
        assert result[0][1] == 'abc123'  # commit hash
        assert result[0][3] == 1  # retry count

    def test_expired_failure_moves_to_rejected(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Failure past retry interval moves to rejected file."""
        failed_file = temp_feed_dir / 'korgalore.test-delivery.failed'
        rejected_file = temp_feed_dir / 'korgalore.test-delivery.rejected'

        # Create failure from 6 days ago (past 5-day interval)
        old_time = datetime.now(UTC) - timedelta(seconds=RETRY_FAILED_INTERVAL + 3600)
        write_state(failed_file, [(0, 'abc123', old_time.isoformat(), 10)])

        mock_feed.mark_failed_delivery('test-delivery', 0, 'abc123')

        # Should be removed from failed
        assert mock_feed._read_jsonl_file(failed_file) == []

        # Should be in rejected
        rejected_result = mock_feed._read_jsonl_file(rejected_file)
        assert len(rejected_result) == 1
        assert rejected_result[0][0] == 0
        assert rejected_result[0][1] == 'abc123'

    def test_multiple_failures_only_updates_matching(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Only the matching entry's retry count goes up when several failures exist."""
        failed_file = temp_feed_dir / 'korgalore.test-delivery.failed'
        now = datetime.now(UTC).isoformat()
        write_state(failed_file, [(0, 'abc123', now, 1), (0, 'def456', now, 2), (1, 'ghi789', now, 3)])

        mock_feed.mark_failed_delivery('test-delivery', 0, 'def456')

        result = mock_feed._read_jsonl_file(failed_file)
        assert len(result) == 3
        by_commit = {r[1]: r for r in result}
        assert by_commit['abc123'][3] == 1  # unchanged
        assert by_commit['def456'][3] == 3  # incremented from 2
        assert by_commit['ghi789'][3] == 3  # unchanged


class TestGetFailedCommits:
    """Tests for get_failed_commits_for_delivery function."""

    def test_returns_epoch_commit_tuples(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Returns list of (epoch, commit) tuples, and [] without a failed file."""
        assert mock_feed.get_failed_commits_for_delivery('test-delivery') == []

        write_state(
            temp_feed_dir / 'korgalore.test-delivery.failed',
            [(0, 'abc123', OLD, 1), (1, 'def456', '2024-01-02T00:00:00', 2)],
        )

        assert mock_feed.get_failed_commits_for_delivery('test-delivery') == [(0, 'abc123'), (1, 'def456')]


class TestCleanupFailedState:
    """Tests for cleanup_failed_state function."""

    @pytest.mark.parametrize(
        ('content', 'kept'),
        [
            pytest.param(None, False, id='missing-file-is-no-error'),
            pytest.param('', False, id='empty-file-removed'),
            pytest.param(json.dumps([0, 'abc123', OLD, 1]) + '\n', True, id='nonempty-file-kept'),
        ],
    )
    def test_cleanup(self, mock_feed: PIFeed, temp_feed_dir: Path, content: str | None, kept: bool) -> None:
        failed_file = temp_feed_dir / 'korgalore.test-delivery.failed'
        if content is not None:
            failed_file.write_text(content)

        mock_feed.cleanup_failed_state('test-delivery')

        assert failed_file.exists() is kept


class TestFeedLocking:
    """Tests for feed_lock and feed_unlock functions."""

    def test_lock_creates_directory_if_missing(self, tmp_path: Path) -> None:
        """Locking a feed creates the parent directory if it doesn't exist."""
        nonexistent_dir = tmp_path / 'nonexistent' / 'feed' / 'path'
        feed = make_pi_feed(nonexistent_dir)

        # This should not raise FileNotFoundError
        feed.feed_lock()
        try:
            assert (nonexistent_dir / 'korgalore.lock').exists()
        finally:
            feed.feed_unlock()

    def test_lock_is_stored_in_global_dict(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Lock file handle is stored in LOCKED_FEEDS for later unlock."""
        key = str(temp_feed_dir)
        assert key not in LOCKED_FEEDS

        mock_feed.feed_lock()
        try:
            assert (temp_feed_dir / 'korgalore.lock').exists()
            assert LOCKED_FEEDS[key] is not None
        finally:
            mock_feed.feed_unlock()
        assert key not in LOCKED_FEEDS

    def test_unlock_without_lock_raises_error(self, tmp_path: Path) -> None:
        """Attempting to unlock a feed that isn't locked raises an error."""
        feed_dir = tmp_path / 'unlocked-feed'
        feed_dir.mkdir()

        with pytest.raises(PublicInboxError, match='is not locked'):
            make_pi_feed(feed_dir, key='unlocked-feed').feed_unlock()

    @pytest.mark.usefixtures('held_lock')
    def test_busy_feed(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """The error names the feed and says what to do."""
        with pytest.raises(FeedLockedError) as info:
            mock_feed.feed_lock()
        assert str(info.value) == (
            f"Another kgl process is using feed 'test-feed' ({temp_feed_dir}). Try again when it is done."
        )
        assert str(temp_feed_dir) not in LOCKED_FEEDS


class TestLegacyMigration:
    """Legacy state migration returns early when there is nothing to migrate."""

    @pytest.mark.parametrize('git_dir', [False, True], ids=['no-git-dir', 'git-dir-without-epochs'])
    def test_migration_skips(self, tmp_path: Path, git_dir: bool) -> None:
        """No crash without git/, or with an empty git/ left by an interrupted clone."""
        feed_dir = tmp_path / 'new-feed'
        feed_dir.mkdir()
        if git_dir:
            (feed_dir / 'git').mkdir()

        make_pi_feed(feed_dir, key='new-feed', highest_epoch=None, top_commit=None)._perform_legacy_migration()


class TestFeedState:
    """update_feed() initializes a feed without state, but rebuilds a corrupt one."""

    def test_missing_state_is_not_corrupt(self, mock_feed: PIFeed) -> None:
        with pytest.raises(StateError) as info:
            mock_feed.load_feed_state()
        assert not isinstance(info.value, StateCorruptError)

    @pytest.mark.parametrize(
        'content',
        [
            pytest.param(b'', id='empty-file'),
            pytest.param(b'{"epochs": {"0": {"lat', id='truncated'),
            pytest.param(b'{"epochs": "\xff\xfe"}', id='not-utf8'),
            pytest.param(b'[]', id='not-an-object'),
            pytest.param(b'{}', id='no-epochs'),
            pytest.param(b'{"epochs": []}', id='epochs-not-an-object'),
            pytest.param(b'{"epochs": {}}', id='no-epoch-recorded'),
        ],
    )
    def test_corrupt_state_is_rebuilt(self, mock_feed: PIFeed, temp_feed_dir: Path, content: bytes) -> None:
        (temp_feed_dir / 'korgalore.feed').write_bytes(content)
        with pytest.raises(StateCorruptError, match='is corrupt'):
            mock_feed.load_feed_state()

        state = mock_feed.rebuild_feed_state()

        assert state['epochs']['0']['latest_commit'] == 'abc123'
        assert mock_feed.load_feed_state() == state
