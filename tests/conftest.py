"""Shared pytest fixtures for korgalore tests."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock


@pytest.fixture
def temp_feed_dir(tmp_path: Path) -> Path:
    """Create a temporary feed directory structure."""
    feed_dir = tmp_path / "test-feed"
    feed_dir.mkdir()
    git_dir = feed_dir / "git" / "0.git"
    git_dir.mkdir(parents=True)
    return feed_dir


@pytest.fixture
def mock_feed(temp_feed_dir: Path) -> "PIFeed":
    """Create a PIFeed instance with mocked git operations."""
    from korgalore.pi_feed import PIFeed

    class TestPIFeed(PIFeed):
        """PIFeed subclass for testing that doesn't require real git repos."""

        def __init__(self, feed_dir: Path) -> None:
            super().__init__(feed_key="test-feed", feed_dir=feed_dir)
            self.feed_type = "test"

        def get_subject_at_commit(self, epoch: int, commit_hash: str) -> str:
            """Mock implementation that returns a test subject."""
            return f"Test subject for {commit_hash}"

        def get_highest_epoch(self) -> int:
            """Mock implementation."""
            return 0

        def get_top_commit(self, epoch: int) -> str:
            """Mock implementation."""
            return "abc123"

    return TestPIFeed(temp_feed_dir)


@pytest.fixture
def sample_deliveries() -> dict:
    """Create sample delivery data structure matching cli.py format.

    Returns dict mapping delivery_name -> (feed, target, labels)
    """
    feeds = {}
    for i in range(5):
        feed = MagicMock()
        feed.feed_key = f"feed-{i % 3}"  # 3 unique feeds
        target = MagicMock()
        target.identifier = f"target-{i % 2}"
        feeds[f"delivery-{i}"] = (feed, target, [f"label-{i}"])
    return feeds
