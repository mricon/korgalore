"""Tests for PIFeed state management functions.

These tests cover the delivery tracking functionality including:
- mark_successful_delivery: removing entries from failed list
- mark_failed_delivery: adding/updating failed entries, rejection after timeout
- JSONL file operations
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from korgalore.pi_feed import PIFeed, RETRY_FAILED_INTERVAL


class TestJSONLOperations:
    """Tests for JSONL file read/write operations."""

    def test_read_empty_file(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Reading non-existent file returns empty list."""
        result = mock_feed._read_jsonl_file(temp_feed_dir / "nonexistent.jsonl")
        assert result == []

    def test_write_and_read_jsonl(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Write and read back JSONL data."""
        filepath = temp_feed_dir / "test.jsonl"
        data: list[tuple[int | str, ...]] = [
            (1, "abc123", "2024-01-01T00:00:00", 1),
            (2, "def456", "2024-01-02T00:00:00", 2),
        ]
        mock_feed._write_jsonl_file(filepath, data)

        result = mock_feed._read_jsonl_file(filepath)
        assert len(result) == 2
        assert result[0] == (1, "abc123", "2024-01-01T00:00:00", 1)
        assert result[1] == (2, "def456", "2024-01-02T00:00:00", 2)

    def test_write_empty_list_removes_file(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Writing empty list removes the file."""
        filepath = temp_feed_dir / "test.jsonl"
        filepath.write_text('[1, "abc"]\n')
        assert filepath.exists()

        mock_feed._write_jsonl_file(filepath, [])
        assert not filepath.exists()

    def test_append_to_jsonl(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Append entries to JSONL file."""
        filepath = temp_feed_dir / "test.jsonl"
        mock_feed._append_to_jsonl_file(filepath, (1, "abc123"))
        mock_feed._append_to_jsonl_file(filepath, (2, "def456"))

        result = mock_feed._read_jsonl_file(filepath)
        assert len(result) == 2
        assert result[0] == (1, "abc123")
        assert result[1] == (2, "def456")


class TestMarkSuccessfulDelivery:
    """Tests for mark_successful_delivery function."""

    def test_success_without_prior_failure(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Successful delivery without was_failing flag doesn't touch failed file."""
        failed_file = temp_feed_dir / "korgalore.test-delivery.failed"
        failed_file.write_text('[0, "abc123", "2024-01-01T00:00:00", 1]\n')

        with patch.object(mock_feed, "save_delivery_info"):
            mock_feed.mark_successful_delivery("test-delivery", 0, "abc123", was_failing=False)

        # File should be unchanged
        result = mock_feed._read_jsonl_file(failed_file)
        assert len(result) == 1

    def test_success_removes_from_failed_list(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Successful delivery with was_failing=True removes entry from failed list."""
        failed_file = temp_feed_dir / "korgalore.test-delivery.failed"
        entries = [
            (0, "abc123", "2024-01-01T00:00:00", 1),
            (0, "def456", "2024-01-01T00:00:00", 2),
            (1, "ghi789", "2024-01-01T00:00:00", 1),
        ]
        content = "".join(json.dumps(e) + "\n" for e in entries)
        failed_file.write_text(content)

        with patch.object(mock_feed, "save_delivery_info"):
            mock_feed.mark_successful_delivery("test-delivery", 0, "def456", was_failing=True)

        result = mock_feed._read_jsonl_file(failed_file)
        assert len(result) == 2
        assert (0, "abc123", "2024-01-01T00:00:00", 1) in result
        assert (1, "ghi789", "2024-01-01T00:00:00", 1) in result
        assert (0, "def456", "2024-01-01T00:00:00", 2) not in result

    def test_success_entry_not_in_failed_list(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """No error if entry not in failed list."""
        failed_file = temp_feed_dir / "korgalore.test-delivery.failed"
        entries = [(0, "abc123", "2024-01-01T00:00:00", 1)]
        content = "".join(json.dumps(e) + "\n" for e in entries)
        failed_file.write_text(content)

        with patch.object(mock_feed, "save_delivery_info"):
            mock_feed.mark_successful_delivery("test-delivery", 0, "nonexistent", was_failing=True)

        result = mock_feed._read_jsonl_file(failed_file)
        assert len(result) == 1  # Unchanged

    def test_success_removes_last_entry_deletes_file(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Removing last entry from failed list deletes the file."""
        failed_file = temp_feed_dir / "korgalore.test-delivery.failed"
        entries = [(0, "abc123", "2024-01-01T00:00:00", 1)]
        content = "".join(json.dumps(e) + "\n" for e in entries)
        failed_file.write_text(content)

        with patch.object(mock_feed, "save_delivery_info"):
            mock_feed.mark_successful_delivery("test-delivery", 0, "abc123", was_failing=True)

        assert not failed_file.exists()


class TestMarkFailedDelivery:
    """Tests for mark_failed_delivery function."""

    def test_new_failure_creates_entry(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """First failure creates new entry with retry count 1."""
        failed_file = temp_feed_dir / "korgalore.test-delivery.failed"

        mock_feed.mark_failed_delivery("test-delivery", 0, "abc123")

        result = mock_feed._read_jsonl_file(failed_file)
        assert len(result) == 1
        assert result[0][0] == 0  # epoch
        assert result[0][1] == "abc123"  # commit hash
        assert result[0][3] == 1  # retry count

    def test_repeated_failure_increments_retry(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Repeated failure increments retry count."""
        failed_file = temp_feed_dir / "korgalore.test-delivery.failed"
        now = datetime.now(timezone.utc)
        entries = [(0, "abc123", now.isoformat(), 3)]
        content = "".join(json.dumps(e) + "\n" for e in entries)
        failed_file.write_text(content)

        mock_feed.mark_failed_delivery("test-delivery", 0, "abc123")

        result = mock_feed._read_jsonl_file(failed_file)
        assert len(result) == 1
        assert result[0][0] == 0
        assert result[0][1] == "abc123"
        assert result[0][3] == 4  # incremented from 3

    def test_expired_failure_moves_to_rejected(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Failure past retry interval moves to rejected file."""
        failed_file = temp_feed_dir / "korgalore.test-delivery.failed"
        rejected_file = temp_feed_dir / "korgalore.test-delivery.rejected"

        # Create failure from 6 days ago (past 5-day interval)
        old_time = datetime.now(timezone.utc) - timedelta(seconds=RETRY_FAILED_INTERVAL + 3600)
        entries = [(0, "abc123", old_time.isoformat(), 10)]
        content = "".join(json.dumps(e) + "\n" for e in entries)
        failed_file.write_text(content)

        mock_feed.mark_failed_delivery("test-delivery", 0, "abc123")

        # Should be removed from failed
        failed_result = mock_feed._read_jsonl_file(failed_file)
        assert len(failed_result) == 0

        # Should be in rejected
        rejected_result = mock_feed._read_jsonl_file(rejected_file)
        assert len(rejected_result) == 1
        assert rejected_result[0][0] == 0
        assert rejected_result[0][1] == "abc123"

    def test_multiple_failures_only_updates_matching(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Only the matching entry is updated when multiple failures exist."""
        failed_file = temp_feed_dir / "korgalore.test-delivery.failed"
        now = datetime.now(timezone.utc)
        entries = [
            (0, "abc123", now.isoformat(), 1),
            (0, "def456", now.isoformat(), 2),
            (1, "ghi789", now.isoformat(), 3),
        ]
        content = "".join(json.dumps(e) + "\n" for e in entries)
        failed_file.write_text(content)

        mock_feed.mark_failed_delivery("test-delivery", 0, "def456")

        result = mock_feed._read_jsonl_file(failed_file)
        assert len(result) == 3

        # Find each entry and verify
        by_commit = {r[1]: r for r in result}
        assert by_commit["abc123"][3] == 1  # unchanged
        assert by_commit["def456"][3] == 3  # incremented from 2
        assert by_commit["ghi789"][3] == 3  # unchanged


class TestGetFailedCommits:
    """Tests for get_failed_commits_for_delivery function."""

    def test_no_failed_file(self, mock_feed: PIFeed) -> None:
        """Returns empty list when no failed file exists."""
        result = mock_feed.get_failed_commits_for_delivery("nonexistent")
        assert result == []

    def test_returns_epoch_commit_tuples(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Returns list of (epoch, commit) tuples."""
        failed_file = temp_feed_dir / "korgalore.test-delivery.failed"
        entries = [
            (0, "abc123", "2024-01-01T00:00:00", 1),
            (1, "def456", "2024-01-02T00:00:00", 2),
        ]
        content = "".join(json.dumps(e) + "\n" for e in entries)
        failed_file.write_text(content)

        result = mock_feed.get_failed_commits_for_delivery("test-delivery")
        assert result == [(0, "abc123"), (1, "def456")]


class TestCleanupFailedState:
    """Tests for cleanup_failed_state function."""

    def test_removes_empty_failed_file(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Removes failed file if empty."""
        failed_file = temp_feed_dir / "korgalore.test-delivery.failed"
        failed_file.write_text("")

        mock_feed.cleanup_failed_state("test-delivery")

        assert not failed_file.exists()

    def test_keeps_nonempty_failed_file(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Keeps failed file if it has entries."""
        failed_file = temp_feed_dir / "korgalore.test-delivery.failed"
        entries = [(0, "abc123", "2024-01-01T00:00:00", 1)]
        content = "".join(json.dumps(e) + "\n" for e in entries)
        failed_file.write_text(content)

        mock_feed.cleanup_failed_state("test-delivery")

        assert failed_file.exists()

    def test_no_error_if_file_missing(self, mock_feed: PIFeed) -> None:
        """No error if failed file doesn't exist."""
        mock_feed.cleanup_failed_state("nonexistent")  # Should not raise


class TestFeedLocking:
    """Tests for feed_lock and feed_unlock functions."""

    def test_lock_creates_directory_if_missing(self, tmp_path: Path) -> None:
        """Locking a feed creates the parent directory if it doesn't exist."""
        from korgalore.pi_feed import PIFeed

        # Create a feed pointing to a non-existent directory
        nonexistent_dir = tmp_path / "nonexistent" / "feed" / "path"
        assert not nonexistent_dir.exists()

        class TestPIFeed(PIFeed):
            def __init__(self, feed_dir: Path) -> None:
                super().__init__(feed_key="test-feed", feed_dir=feed_dir)
                self.feed_type = "test"

            def get_subject_at_commit(self, epoch: int, commit_hash: str) -> str:
                return f"Test subject for {commit_hash}"

            def get_highest_epoch(self) -> int:
                return 0

            def get_top_commit(self, epoch: int) -> str:
                return "abc123"

        feed = TestPIFeed(nonexistent_dir)

        # This should not raise FileNotFoundError
        feed.feed_lock()

        try:
            # Directory should now exist
            assert nonexistent_dir.exists()
            # Lock file should exist
            lock_file = nonexistent_dir / "korgalore.lock"
            assert lock_file.exists()
        finally:
            # Clean up
            feed.feed_unlock()

    def test_lock_and_unlock_cycle(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Lock and unlock cycle works correctly."""
        lock_file = temp_feed_dir / "korgalore.lock"

        # Lock the feed
        mock_feed.feed_lock()
        assert lock_file.exists()

        # Unlock the feed
        mock_feed.feed_unlock()

    def test_lock_is_stored_in_global_dict(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """Lock file handle is stored in LOCKED_FEEDS for later unlock."""
        from korgalore.pi_feed import LOCKED_FEEDS

        key = str(temp_feed_dir)
        assert key not in LOCKED_FEEDS

        mock_feed.feed_lock()
        try:
            assert key in LOCKED_FEEDS
            assert LOCKED_FEEDS[key] is not None
        finally:
            mock_feed.feed_unlock()
            assert key not in LOCKED_FEEDS

    def test_unlock_without_lock_raises_error(self, tmp_path: Path) -> None:
        """Attempting to unlock a feed that isn't locked raises an error."""
        from korgalore.pi_feed import PIFeed
        from korgalore import PublicInboxError

        class TestPIFeed(PIFeed):
            def __init__(self, feed_dir: Path) -> None:
                super().__init__(feed_key="unlocked-feed", feed_dir=feed_dir)
                self.feed_type = "test"

            def get_subject_at_commit(self, epoch: int, commit_hash: str) -> str:
                return f"Test subject for {commit_hash}"

            def get_highest_epoch(self) -> int:
                return 0

            def get_top_commit(self, epoch: int) -> str:
                return "abc123"

        feed_dir = tmp_path / "unlocked-feed"
        feed_dir.mkdir()
        feed = TestPIFeed(feed_dir)

        with pytest.raises(PublicInboxError, match="is not locked"):
            feed.feed_unlock()


class TestLegacyMigration:
    """Tests for legacy state migration."""

    def test_migration_skips_when_no_git_directory(self, tmp_path: Path) -> None:
        """Legacy migration does not crash when git directory doesn't exist."""
        from korgalore.pi_feed import PIFeed

        class TestPIFeed(PIFeed):
            def __init__(self, feed_dir: Path) -> None:
                super().__init__(feed_key="new-feed", feed_dir=feed_dir)
                self.feed_type = "test"

            def get_subject_at_commit(self, epoch: int, commit_hash: str) -> str:
                return f"Test subject for {commit_hash}"

        # Create feed directory without git subdirectory
        feed_dir = tmp_path / "new-feed"
        feed_dir.mkdir()
        # Do NOT create feed_dir / "git"

        feed = TestPIFeed(feed_dir)

        # This should not raise an error - it should just return early
        feed._perform_legacy_migration()  # Should not crash

    def test_migration_skips_when_git_dir_has_no_epochs(self, tmp_path: Path) -> None:
        """Legacy migration does not crash when git/ exists but has no epoch repos."""
        from korgalore.pi_feed import PIFeed

        class TestPIFeed(PIFeed):
            def __init__(self, feed_dir: Path) -> None:
                super().__init__(feed_key="partial-feed", feed_dir=feed_dir)
                self.feed_type = "test"

            def get_subject_at_commit(self, epoch: int, commit_hash: str) -> str:
                return f"Test subject for {commit_hash}"

        # Create feed directory with empty git/ subdirectory (e.g. from a
        # failed or interrupted clone that left the parent dir behind)
        feed_dir = tmp_path / "partial-feed"
        feed_dir.mkdir()
        (feed_dir / "git").mkdir()

        feed = TestPIFeed(feed_dir)

        # This should not raise PublicInboxError - it should return early
        feed._perform_legacy_migration()


class TestGetFirstCommit:
    """Tests for get_first_commit with empty and non-empty repositories."""

    def _make_feed(self, feed_dir: Path) -> PIFeed:
        """Create a concrete PIFeed subclass for testing.

        Only overrides the abstract methods; get_top_commit and
        get_first_commit use the real implementations so they can be
        tested against actual git repositories.
        """
        class TestPIFeed(PIFeed):
            def __init__(self, fd: Path) -> None:
                super().__init__(feed_key="test-feed", feed_dir=fd)
                self.feed_type = "test"

            def get_subject_at_commit(self, epoch: int, commit_hash: str) -> str:
                return f"Test subject for {commit_hash}"

            def get_highest_epoch(self) -> int:
                return 0

        return TestPIFeed(feed_dir)

    def _init_bare_repo(self, gitdir: Path) -> None:
        """Initialise a bare git repository at gitdir."""
        import subprocess
        subprocess.run(
            ['git', 'init', '--bare', str(gitdir)],
            check=True, capture_output=True,
        )

    def _add_commit(self, gitdir: Path) -> str:
        """Add a single commit to a bare repo and return its hash."""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as work:
            subprocess.run(
                ['git', 'clone', str(gitdir), work],
                check=True, capture_output=True,
            )
            dummy = Path(work) / 'dummy'
            dummy.write_text('content')
            subprocess.run(
                ['git', '-C', work, 'add', 'dummy'],
                check=True, capture_output=True,
            )
            subprocess.run(
                ['git', '-C', work,
                 '-c', 'user.name=Test', '-c', 'user.email=test@test',
                 'commit', '-m', 'initial'],
                check=True, capture_output=True,
            )
            subprocess.run(
                ['git', '-C', work, 'push'],
                check=True, capture_output=True,
            )
            result = subprocess.run(
                ['git', '-C', work, 'rev-parse', 'HEAD'],
                check=True, capture_output=True, text=True,
            )
            return result.stdout.strip()

    def test_empty_repo_returns_empty_string(self, tmp_path: Path) -> None:
        """get_first_commit returns '' for a repository with no commits."""
        feed_dir = tmp_path / "test-feed"
        feed_dir.mkdir()
        gitdir = feed_dir / "git" / "0.git"
        gitdir.mkdir(parents=True)
        self._init_bare_repo(gitdir)

        feed = self._make_feed(feed_dir)
        result = feed.get_first_commit(0)
        assert result == ''

    def test_nonempty_repo_returns_commit_hash(self, tmp_path: Path) -> None:
        """get_first_commit returns the root commit hash."""
        feed_dir = tmp_path / "test-feed"
        feed_dir.mkdir()
        gitdir = feed_dir / "git" / "0.git"
        gitdir.mkdir(parents=True)
        self._init_bare_repo(gitdir)
        expected = self._add_commit(gitdir)

        feed = self._make_feed(feed_dir)
        result = feed.get_first_commit(0)
        assert result == expected

    def test_top_commit_empty_repo_returns_empty_string(self, tmp_path: Path) -> None:
        """get_top_commit returns '' for a repository with no commits."""
        feed_dir = tmp_path / "test-feed"
        feed_dir.mkdir()
        gitdir = feed_dir / "git" / "0.git"
        gitdir.mkdir(parents=True)
        self._init_bare_repo(gitdir)

        feed = self._make_feed(feed_dir)
        result = feed.get_top_commit(0)
        assert result == ''

    def test_top_commit_nonempty_repo_returns_commit_hash(self, tmp_path: Path) -> None:
        """get_top_commit returns the latest commit hash."""
        feed_dir = tmp_path / "test-feed"
        feed_dir.mkdir()
        gitdir = feed_dir / "git" / "0.git"
        gitdir.mkdir(parents=True)
        self._init_bare_repo(gitdir)
        expected = self._add_commit(gitdir)

        feed = self._make_feed(feed_dir)
        result = feed.get_top_commit(0)
        assert result == expected


class TestIsEmptyRepoCache:
    """Tests for is_empty_repo caching and cache invalidation."""

    def _make_feed(self, feed_dir: Path) -> PIFeed:
        """Create a concrete PIFeed subclass for testing."""
        class TestPIFeed(PIFeed):
            def __init__(self, fd: Path) -> None:
                super().__init__(feed_key="test-feed", feed_dir=fd)
                self.feed_type = "test"

            def get_subject_at_commit(self, epoch: int, commit_hash: str) -> str:
                return f"Test subject for {commit_hash}"

            def get_highest_epoch(self) -> int:
                return 0

        return TestPIFeed(feed_dir)

    def _init_bare_repo(self, gitdir: Path) -> None:
        """Initialise a bare git repository at gitdir."""
        import subprocess
        subprocess.run(
            ['git', 'init', '--bare', str(gitdir)],
            check=True, capture_output=True,
        )

    def _add_commit(self, gitdir: Path) -> str:
        """Add a single commit to a bare repo and return its hash."""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as work:
            subprocess.run(
                ['git', 'clone', str(gitdir), work],
                check=True, capture_output=True,
            )
            dummy = Path(work) / 'dummy'
            dummy.write_text('content')
            subprocess.run(
                ['git', '-C', work, 'add', 'dummy'],
                check=True, capture_output=True,
            )
            subprocess.run(
                ['git', '-C', work,
                 '-c', 'user.name=Test', '-c', 'user.email=test@test',
                 'commit', '-m', 'initial'],
                check=True, capture_output=True,
            )
            subprocess.run(
                ['git', '-C', work, 'push'],
                check=True, capture_output=True,
            )
            result = subprocess.run(
                ['git', '-C', work, 'rev-parse', 'HEAD'],
                check=True, capture_output=True, text=True,
            )
            return result.stdout.strip()

    def test_result_is_cached(self, tmp_path: Path) -> None:
        """Repeated is_empty_repo calls use the cache."""
        feed_dir = tmp_path / "test-feed"
        feed_dir.mkdir()
        gitdir = feed_dir / "git" / "0.git"
        gitdir.mkdir(parents=True)
        self._init_bare_repo(gitdir)

        feed = self._make_feed(feed_dir)
        assert feed.is_empty_repo(0) is True
        assert 0 in feed._empty_repo_cache
        assert feed._empty_repo_cache[0] is True

        # Second call should return cached value without git command
        # Verify by checking the cache is still populated
        assert feed.is_empty_repo(0) is True

    def test_cache_cleared_on_unlock(self, tmp_path: Path) -> None:
        """feed_unlock clears the is_empty_repo cache."""
        feed_dir = tmp_path / "test-feed"
        feed_dir.mkdir()
        gitdir = feed_dir / "git" / "0.git"
        gitdir.mkdir(parents=True)
        self._init_bare_repo(gitdir)

        feed = self._make_feed(feed_dir)
        assert feed.is_empty_repo(0) is True
        assert 0 in feed._empty_repo_cache

        feed.feed_lock()
        try:
            # Cache should still be present while locked
            assert 0 in feed._empty_repo_cache
        finally:
            feed.feed_unlock()

        # Cache should be cleared after unlock
        assert 0 not in feed._empty_repo_cache

    def test_cache_reflects_repo_state_after_unlock(self, tmp_path: Path) -> None:
        """After unlock and adding a commit, is_empty_repo returns False."""
        feed_dir = tmp_path / "test-feed"
        feed_dir.mkdir()
        gitdir = feed_dir / "git" / "0.git"
        gitdir.mkdir(parents=True)
        self._init_bare_repo(gitdir)

        feed = self._make_feed(feed_dir)
        assert feed.is_empty_repo(0) is True

        feed.feed_lock()
        self._add_commit(gitdir)
        feed.feed_unlock()

        # Cache was cleared by unlock, so this re-checks the repo
        assert feed.is_empty_repo(0) is False

    def test_nonempty_repo_cached_as_false(self, tmp_path: Path) -> None:
        """Non-empty repos are cached as False."""
        feed_dir = tmp_path / "test-feed"
        feed_dir.mkdir()
        gitdir = feed_dir / "git" / "0.git"
        gitdir.mkdir(parents=True)
        self._init_bare_repo(gitdir)
        self._add_commit(gitdir)

        feed = self._make_feed(feed_dir)
        assert feed.is_empty_repo(0) is False
        assert feed._empty_repo_cache[0] is False
