"""Tests for epoch rollover detection in PIFeed.

Epochs are separate git repositories in public-inbox feeds. When an epoch
fills up, a new one is created (e.g., 0.git -> 1.git). These tests verify
that the feed correctly detects rollover and retrieves commits from both
the old and new epochs.
"""

import json
import pytest
from pathlib import Path
from typing import List, Tuple, Any
from unittest.mock import patch, MagicMock

from korgalore.pi_feed import PIFeed
from korgalore import GitError, StateError, PublicInboxError


class MockPIFeed(PIFeed):
    """PIFeed subclass for testing without real git repositories."""

    def __init__(self, feed_dir: Path) -> None:
        super().__init__(feed_key="test-feed", feed_dir=feed_dir)
        self.feed_type = "test"
        self._default_branch = "master"

    def _get_default_branch(self, gitdir: Path) -> str:
        """Return mocked default branch."""
        return self._default_branch

    def get_subject_at_commit(self, epoch: int, commit_hash: str) -> str:
        """Mock implementation."""
        return f"Test subject for {commit_hash}"


def create_feed_with_epochs(tmp_path: Path, epochs: List[int]) -> MockPIFeed:
    """Create a mock feed with specified epoch directories."""
    feed_dir = tmp_path / "test-feed"
    feed_dir.mkdir()
    for epoch in epochs:
        (feed_dir / "git" / f"{epoch}.git").mkdir(parents=True)
    return MockPIFeed(feed_dir)


def write_delivery_info(feed: PIFeed, delivery_name: str, epochs_data: dict) -> None:
    """Write delivery info state file."""
    state_file = feed.feed_dir / f"korgalore.{delivery_name}.info"
    state = {"epochs": {}}
    for epoch_num, data in epochs_data.items():
        state["epochs"][str(epoch_num)] = {
            "last": data.get("last", "dummy_commit"),
            "commit_date": data.get("commit_date", "2024-01-01 00:00:00 +0000"),
            "subject": data.get("subject", "Test subject"),
            "msgid": data.get("msgid", "<test@example.com>"),
        }
    state_file.write_text(json.dumps(state, indent=2))


class TestFindEpochs:
    """Tests for epoch discovery."""

    def test_single_epoch(self, tmp_path: Path) -> None:
        """Single epoch directory is found."""
        feed = create_feed_with_epochs(tmp_path, [0])
        epochs = feed.find_epochs()
        assert epochs == [0]

    def test_multiple_epochs_sorted(self, tmp_path: Path) -> None:
        """Multiple epochs are returned sorted."""
        feed = create_feed_with_epochs(tmp_path, [2, 0, 1])
        epochs = feed.find_epochs()
        assert epochs == [0, 1, 2]

    def test_non_contiguous_epochs(self, tmp_path: Path) -> None:
        """Non-contiguous epochs are handled."""
        feed = create_feed_with_epochs(tmp_path, [0, 2, 5])
        epochs = feed.find_epochs()
        assert epochs == [0, 2, 5]

    def test_no_epochs_raises(self, tmp_path: Path) -> None:
        """No epoch directories raises PublicInboxError."""
        feed_dir = tmp_path / "test-feed"
        feed_dir.mkdir()
        (feed_dir / "git").mkdir()
        feed = MockPIFeed(feed_dir)

        with pytest.raises(PublicInboxError) as exc_info:
            feed.find_epochs()
        assert "No existing epochs" in str(exc_info.value)

    def test_missing_git_directory_raises(self, tmp_path: Path) -> None:
        """Missing git directory raises PublicInboxError."""
        feed_dir = tmp_path / "test-feed"
        feed_dir.mkdir()
        # Do not create the git subdirectory
        feed = MockPIFeed(feed_dir)

        with pytest.raises(PublicInboxError) as exc_info:
            feed.find_epochs()
        assert "No existing epochs" in str(exc_info.value)

    def test_ignores_non_epoch_directories(self, tmp_path: Path) -> None:
        """Non-epoch directories are ignored."""
        feed = create_feed_with_epochs(tmp_path, [0, 1])
        # Add non-epoch directories
        (feed.feed_dir / "git" / "not_an_epoch.git").mkdir()
        (feed.feed_dir / "git" / "random_dir").mkdir()

        epochs = feed.find_epochs()
        assert epochs == [0, 1]


class TestGetHighestEpoch:
    """Tests for highest epoch detection."""

    def test_single_epoch(self, tmp_path: Path) -> None:
        """Single epoch returns that epoch."""
        feed = create_feed_with_epochs(tmp_path, [0])
        assert feed.get_highest_epoch() == 0

    def test_multiple_epochs(self, tmp_path: Path) -> None:
        """Multiple epochs returns highest."""
        feed = create_feed_with_epochs(tmp_path, [0, 1, 2])
        assert feed.get_highest_epoch() == 2

    def test_non_contiguous(self, tmp_path: Path) -> None:
        """Non-contiguous epochs returns highest."""
        feed = create_feed_with_epochs(tmp_path, [0, 5, 10])
        assert feed.get_highest_epoch() == 10


class TestGetAllCommitsInEpoch:
    """Tests for retrieving all commits in an epoch."""

    @patch('korgalore.pi_feed.run_git_command')
    def test_returns_commits_in_order(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """Commits are returned in chronological order."""
        feed = create_feed_with_epochs(tmp_path, [0])
        mock_git.return_value = (0, b"aaa111\nbbb222\nccc333")

        commits = feed.get_all_commits_in_epoch(0)

        assert commits == ["aaa111", "bbb222", "ccc333"]
        # Verify rev-list was called with --reverse
        call_args = mock_git.call_args[0]
        assert '--reverse' in call_args[1]

    @patch('korgalore.pi_feed.run_git_command')
    def test_empty_epoch(self, mock_git: MagicMock, tmp_path: Path) -> None:
        """Empty epoch returns empty list."""
        feed = create_feed_with_epochs(tmp_path, [0])
        mock_git.return_value = (0, b"")

        commits = feed.get_all_commits_in_epoch(0)

        assert commits == []

    @patch('korgalore.pi_feed.run_git_command')
    def test_git_error_raises(self, mock_git: MagicMock, tmp_path: Path) -> None:
        """Git error raises GitError."""
        feed = create_feed_with_epochs(tmp_path, [0])
        mock_git.return_value = (1, b"fatal: bad revision")

        with pytest.raises(GitError):
            feed.get_all_commits_in_epoch(0)


class TestEpochRolloverDetection:
    """Tests for epoch rollover detection in get_latest_commits_for_delivery."""

    @patch('korgalore.pi_feed.run_git_command')
    def test_no_rollover_single_epoch(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """No rollover - only returns commits from current epoch."""
        feed = create_feed_with_epochs(tmp_path, [0])
        write_delivery_info(feed, "delivery1", {0: {"last": "aaa111"}})

        mock_git.side_effect = [
            (0, b""),  # cat-file -e (commit exists)
            (0, b"bbb222\nccc333\nddd444"),  # rev-list (new commits)
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        assert result == [(0, "bbb222"), (0, "ccc333"), (0, "ddd444")]

    @patch('korgalore.pi_feed.run_git_command')
    def test_no_new_commits_no_rollover(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """No new commits and no rollover returns empty list."""
        feed = create_feed_with_epochs(tmp_path, [0])
        write_delivery_info(feed, "delivery1", {0: {"last": "aaa111"}})

        mock_git.side_effect = [
            (0, b""),  # cat-file -e
            (0, b""),  # rev-list (no new commits)
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        assert result == []

    @patch('korgalore.pi_feed.run_git_command')
    def test_rollover_detected_includes_new_epoch(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """Rollover detected - includes commits from both epochs."""
        feed = create_feed_with_epochs(tmp_path, [0, 1])  # New epoch exists
        write_delivery_info(feed, "delivery1", {0: {"last": "aaa111"}})

        mock_git.side_effect = [
            (0, b""),  # cat-file -e
            (0, b"bbb222"),  # rev-list in epoch 0 (one new commit)
            (0, b"xxx111\nyyy222\nzzz333"),  # rev-list in epoch 1 (all commits)
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        # Should have commit from epoch 0 followed by all commits from epoch 1
        assert result == [
            (0, "bbb222"),
            (1, "xxx111"),
            (1, "yyy222"),
            (1, "zzz333"),
        ]

    @patch('korgalore.pi_feed.run_git_command')
    def test_rollover_no_new_commits_in_old_epoch(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """Rollover with no new commits in old epoch."""
        feed = create_feed_with_epochs(tmp_path, [0, 1])
        write_delivery_info(feed, "delivery1", {0: {"last": "aaa111"}})

        mock_git.side_effect = [
            (0, b""),  # cat-file -e
            (0, b""),  # rev-list epoch 0 (no new commits)
            (0, b"xxx111\nyyy222"),  # rev-list epoch 1
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        # Only commits from new epoch
        assert result == [(1, "xxx111"), (1, "yyy222")]

    @patch('korgalore.pi_feed.run_git_command')
    def test_rollover_empty_new_epoch(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """New epoch exists but has no commits yet."""
        feed = create_feed_with_epochs(tmp_path, [0, 1])
        write_delivery_info(feed, "delivery1", {0: {"last": "aaa111"}})

        mock_git.side_effect = [
            (0, b""),  # cat-file -e
            (0, b"bbb222"),  # rev-list epoch 0
            (0, b""),  # rev-list epoch 1 (empty)
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        # Only commits from epoch 0
        assert result == [(0, "bbb222")]

    @patch('korgalore.pi_feed.run_git_command')
    def test_multiple_epoch_rollover(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """Multiple new epochs - only highest is fetched."""
        feed = create_feed_with_epochs(tmp_path, [0, 1, 2, 3])
        write_delivery_info(feed, "delivery1", {0: {"last": "aaa111"}})

        mock_git.side_effect = [
            (0, b""),  # cat-file -e
            (0, b"bbb222"),  # rev-list epoch 0
            (0, b"new_commit"),  # rev-list epoch 3 (highest)
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        # Note: current implementation only fetches highest epoch, not intermediates
        assert (0, "bbb222") in result
        assert (3, "new_commit") in result

    @patch('korgalore.pi_feed.run_git_command')
    def test_already_on_latest_epoch(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """Delivery already knows about latest epoch."""
        feed = create_feed_with_epochs(tmp_path, [0, 1])
        write_delivery_info(feed, "delivery1", {
            0: {"last": "old_commit"},
            1: {"last": "aaa111"}  # Already on epoch 1
        })

        mock_git.side_effect = [
            (0, b""),  # cat-file -e
            (0, b"bbb222\nccc333"),  # rev-list epoch 1
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        # Only new commits from epoch 1
        assert result == [(1, "bbb222"), (1, "ccc333")]

    @patch('korgalore.pi_feed.run_git_command')
    def test_skipped_epoch(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """Epoch numbers are non-contiguous (e.g., 0 -> 2)."""
        feed = create_feed_with_epochs(tmp_path, [0, 2])  # No epoch 1
        write_delivery_info(feed, "delivery1", {0: {"last": "aaa111"}})

        mock_git.side_effect = [
            (0, b""),  # cat-file -e
            (0, b"bbb222"),  # rev-list epoch 0
            (0, b"xxx111"),  # rev-list epoch 2
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        assert result == [(0, "bbb222"), (2, "xxx111")]


class TestEpochRolloverStateManagement:
    """Tests for state management during epoch rollover."""

    @patch('korgalore.pi_feed.run_git_command')
    def test_delivery_with_multiple_known_epochs(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """Delivery knows about multiple epochs, uses highest."""
        feed = create_feed_with_epochs(tmp_path, [0, 1, 2])
        write_delivery_info(feed, "delivery1", {
            0: {"last": "epoch0_commit"},
            1: {"last": "epoch1_commit"},
            2: {"last": "epoch2_commit"},
        })

        mock_git.side_effect = [
            (0, b""),  # cat-file -e (for epoch 2)
            (0, b"new_commit"),  # rev-list epoch 2
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        # Should query from epoch 2 (highest known)
        assert result == [(2, "new_commit")]


class TestEpochRolloverEdgeCases:
    """Edge case tests for epoch rollover."""

    @patch('korgalore.pi_feed.run_git_command')
    def test_large_number_of_commits_in_new_epoch(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """Large number of commits in new epoch."""
        feed = create_feed_with_epochs(tmp_path, [0, 1])
        write_delivery_info(feed, "delivery1", {0: {"last": "aaa111"}})

        # Generate 1000 commits
        new_epoch_commits = "\n".join([f"commit_{i:04d}" for i in range(1000)])

        mock_git.side_effect = [
            (0, b""),  # cat-file -e
            (0, b""),  # rev-list epoch 0 (no new)
            (0, new_epoch_commits.encode()),  # rev-list epoch 1
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        assert len(result) == 1000
        assert result[0] == (1, "commit_0000")
        assert result[-1] == (1, "commit_0999")

    @patch('korgalore.pi_feed.run_git_command')
    def test_high_epoch_numbers(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """High epoch numbers are handled correctly."""
        feed = create_feed_with_epochs(tmp_path, [99, 100])
        write_delivery_info(feed, "delivery1", {99: {"last": "aaa111"}})

        mock_git.side_effect = [
            (0, b""),  # cat-file -e
            (0, b"bbb222"),  # rev-list epoch 99
            (0, b"xxx111"),  # rev-list epoch 100
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        assert result == [(99, "bbb222"), (100, "xxx111")]

    def test_epoch_zero_only(self, tmp_path: Path) -> None:
        """Feed with only epoch 0."""
        feed = create_feed_with_epochs(tmp_path, [0])
        assert feed.get_highest_epoch() == 0
        assert feed.find_epochs() == [0]

    @patch('korgalore.pi_feed.run_git_command')
    def test_commit_not_found_triggers_recovery(
        self, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """Invalid commit triggers rebase recovery."""
        feed = create_feed_with_epochs(tmp_path, [0])
        write_delivery_info(feed, "delivery1", {
            0: {
                "last": "invalid_commit",
                "commit_date": "2024-01-01 00:00:00 +0000",
                "subject": "Test subject",
                "msgid": "<test@example.com>",
            }
        })

        # Simulate commit not found, then recovery process
        mock_git.side_effect = [
            (1, b""),  # cat-file -e fails (commit not found)
            (0, b"recovered_commit"),  # rev-list --since-as-filter finds commits
            # get_message_at_commit for matching
            (0, b"From: test@example.com\nSubject: Test subject\nMessage-ID: <test@example.com>\n\nBody"),
            # save_delivery_info calls
            (0, b"2024-01-01 00:00:00 +0000"),  # git show commit date
            (0, b"new_commit1\nnew_commit2"),  # rev-list from recovered commit
        ]

        result = feed.get_latest_commits_for_delivery("delivery1")

        assert len(result) == 2
        assert result[0] == (0, "new_commit1")


class TestGetGitdir:
    """Tests for get_gitdir method."""

    def test_returns_correct_path(self, tmp_path: Path) -> None:
        """Returns correct git directory path for epoch."""
        feed = create_feed_with_epochs(tmp_path, [0, 1, 2])

        assert feed.get_gitdir(0) == feed.feed_dir / "git" / "0.git"
        assert feed.get_gitdir(1) == feed.feed_dir / "git" / "1.git"
        assert feed.get_gitdir(2) == feed.feed_dir / "git" / "2.git"

    def test_high_epoch_number(self, tmp_path: Path) -> None:
        """High epoch numbers work correctly."""
        feed = create_feed_with_epochs(tmp_path, [999])
        assert feed.get_gitdir(999) == feed.feed_dir / "git" / "999.git"
