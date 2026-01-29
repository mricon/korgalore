"""Tests for delivery state initialisation after first feed clone.

Verifies that when update_all_feeds() reports newly initialised feeds,
perform_pull() sets up delivery state immediately so the next pull cycle
can deliver new commits without wasting a run.
"""

from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch

import click

from korgalore import StateError
from korgalore.pi_feed import PIFeed


class _StubPIFeed(PIFeed):
    """PIFeed subclass for testing that doesn't require real git repos."""

    def __init__(self, feed_dir: Path, feed_key: str = "test-feed") -> None:
        super().__init__(feed_key=feed_key, feed_dir=feed_dir)
        self.feed_type = "test"

    def get_subject_at_commit(self, epoch: int, commit_hash: str) -> str:
        return f"Test subject for {commit_hash}"

    def get_highest_epoch(self) -> int:
        return 0

    def get_top_commit(self, epoch: int) -> str:
        return "abc123def456"


def _make_mock_feed(feed_key: str, status: int) -> MagicMock:
    """Create a MagicMock feed with correct STATUS_* constants."""
    feed = MagicMock()
    feed.feed_key = feed_key
    feed.feed_url = f"https://example.com/{feed_key}"
    feed.update_feed.return_value = status
    feed.STATUS_UPDATED = PIFeed.STATUS_UPDATED
    feed.STATUS_INITIALIZED = PIFeed.STATUS_INITIALIZED
    feed.STATUS_NOCHANGE = PIFeed.STATUS_NOCHANGE
    return feed


class TestUpdateAllFeedsReturnValue:
    """Verify update_all_feeds returns both updated and initialized feeds."""

    def test_returns_tuple_of_two_lists(self) -> None:
        """update_all_feeds must return (updated_feeds, initialized_feeds)."""
        from korgalore.cli import update_all_feeds

        feed = _make_mock_feed("test-feed", PIFeed.STATUS_NOCHANGE)

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['feeds'] = {"test-feed": feed}
        ctx.obj['hide_bar'] = True

        result = update_all_feeds(ctx)
        assert isinstance(result, tuple)
        assert len(result) == 2
        updated, initialized = result
        assert isinstance(updated, list)
        assert isinstance(initialized, list)
        assert updated == []
        assert initialized == []

    def test_initialized_feed_in_second_list(self) -> None:
        """A feed returning STATUS_INITIALIZED appears in initialized list only."""
        from korgalore.cli import update_all_feeds

        feed = _make_mock_feed("new-feed", PIFeed.STATUS_INITIALIZED)

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['feeds'] = {"new-feed": feed}
        ctx.obj['hide_bar'] = True

        updated, initialized = update_all_feeds(ctx)
        assert initialized == ["new-feed"]
        assert updated == []

    def test_updated_feed_in_first_list(self) -> None:
        """A feed returning STATUS_UPDATED appears in updated list only."""
        from korgalore.cli import update_all_feeds

        feed = _make_mock_feed("existing-feed", PIFeed.STATUS_UPDATED)

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['feeds'] = {"existing-feed": feed}
        ctx.obj['hide_bar'] = True

        updated, initialized = update_all_feeds(ctx)
        assert updated == ["existing-feed"]
        assert initialized == []

    def test_both_flags_set(self) -> None:
        """A feed returning both UPDATED and INITIALIZED appears in both lists."""
        from korgalore.cli import update_all_feeds

        feed = _make_mock_feed("both-feed",
                               PIFeed.STATUS_UPDATED | PIFeed.STATUS_INITIALIZED)

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['feeds'] = {"both-feed": feed}
        ctx.obj['hide_bar'] = True

        updated, initialized = update_all_feeds(ctx)
        assert updated == ["both-feed"]
        assert initialized == ["both-feed"]


class TestDeliveryStateInitOnClone:
    """Verify perform_pull initialises delivery state for newly cloned feeds."""

    def _make_context(self, feeds: Dict[str, Any],
                      deliveries: Dict[str, Tuple[Any, Any, List[str], Any]]) -> click.Context:
        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['config'] = {'deliveries': {d: {} for d in deliveries}}
        ctx.obj['feeds'] = feeds
        ctx.obj['deliveries'] = deliveries
        ctx.obj['targets'] = {}
        ctx.obj['bozofilter'] = set()
        ctx.obj['hide_bar'] = True
        return ctx

    @patch('korgalore.cli.map_deliveries')
    @patch('korgalore.cli.map_tracked_threads')
    @patch('korgalore.cli.lock_all_feeds')
    @patch('korgalore.cli.unlock_all_feeds')
    @patch('korgalore.cli.retry_all_failed_deliveries')
    @patch('korgalore.cli.update_all_feeds')
    def test_initialized_feed_gets_delivery_state(
        self, mock_update, mock_retry, mock_unlock, mock_lock,
        mock_tracked, mock_map, tmp_path: Path
    ) -> None:
        """save_delivery_info is called for a newly initialised feed."""
        from korgalore.cli import perform_pull

        feed_dir = tmp_path / "new-feed"
        feed_dir.mkdir()
        (feed_dir / "git" / "0.git").mkdir(parents=True)
        feed = _StubPIFeed(feed_dir, feed_key="new-feed")

        target = MagicMock()
        target.identifier = "test-target"

        mock_update.return_value = ([], ["new-feed"])

        feeds = {"new-feed": feed}
        deliveries = {"my-delivery": (feed, target, ["label"], None)}
        ctx = self._make_context(feeds, deliveries)

        with patch.object(feed, 'load_delivery_info', side_effect=StateError("no state")), \
             patch.object(feed, 'save_delivery_info') as mock_save:
            perform_pull(ctx, no_update=False, force=False, delivery_name=None)
            mock_save.assert_called_once_with("my-delivery")

    @patch('korgalore.cli.map_deliveries')
    @patch('korgalore.cli.map_tracked_threads')
    @patch('korgalore.cli.lock_all_feeds')
    @patch('korgalore.cli.unlock_all_feeds')
    @patch('korgalore.cli.retry_all_failed_deliveries')
    @patch('korgalore.cli.update_all_feeds')
    def test_existing_state_not_reinitialised(
        self, mock_update, mock_retry, mock_unlock, mock_lock,
        mock_tracked, mock_map, tmp_path: Path
    ) -> None:
        """If delivery state already exists, save_delivery_info is not called."""
        from korgalore.cli import perform_pull

        feed_dir = tmp_path / "new-feed"
        feed_dir.mkdir()
        (feed_dir / "git" / "0.git").mkdir(parents=True)
        feed = _StubPIFeed(feed_dir, feed_key="new-feed")

        target = MagicMock()
        target.identifier = "test-target"

        mock_update.return_value = ([], ["new-feed"])

        feeds = {"new-feed": feed}
        deliveries = {"my-delivery": (feed, target, ["label"], None)}
        ctx = self._make_context(feeds, deliveries)

        # Pre-create a state file so load_delivery_info succeeds
        import json
        state_file = feed_dir / "korgalore.my-delivery.info"
        state_file.write_text(json.dumps({
            "epochs": {"0": {"last": "abc123", "commit_date": "2025-01-01",
                             "subject": "test", "msgid": "<test@test>"}}
        }))

        with patch.object(feed, 'save_delivery_info') as mock_save:
            perform_pull(ctx, no_update=False, force=False, delivery_name=None)
            mock_save.assert_not_called()

    @patch('korgalore.cli.map_deliveries')
    @patch('korgalore.cli.map_tracked_threads')
    @patch('korgalore.cli.lock_all_feeds')
    @patch('korgalore.cli.unlock_all_feeds')
    @patch('korgalore.cli.retry_all_failed_deliveries')
    @patch('korgalore.cli.update_all_feeds')
    def test_no_init_when_no_update(
        self, mock_update, mock_retry, mock_unlock, mock_lock,
        mock_tracked, mock_map, tmp_path: Path
    ) -> None:
        """With no_update=True, no initialisation is attempted."""
        from korgalore.cli import perform_pull

        feed_dir = tmp_path / "new-feed"
        feed_dir.mkdir()
        (feed_dir / "git" / "0.git").mkdir(parents=True)
        feed = _StubPIFeed(feed_dir, feed_key="new-feed")

        target = MagicMock()
        target.identifier = "test-target"

        feeds = {"new-feed": feed}
        deliveries = {"my-delivery": (feed, target, ["label"], None)}
        ctx = self._make_context(feeds, deliveries)

        with patch.object(feed, 'load_delivery_info') as mock_load, \
             patch.object(feed, 'save_delivery_info') as mock_save:
            perform_pull(ctx, no_update=True, force=False, delivery_name=None)
            mock_update.assert_not_called()
            mock_load.assert_not_called()
            mock_save.assert_not_called()

    @patch('korgalore.cli.map_deliveries')
    @patch('korgalore.cli.map_tracked_threads')
    @patch('korgalore.cli.lock_all_feeds')
    @patch('korgalore.cli.unlock_all_feeds')
    @patch('korgalore.cli.retry_all_failed_deliveries')
    @patch('korgalore.cli.update_all_feeds')
    def test_multiple_deliveries_for_initialized_feed(
        self, mock_update, mock_retry, mock_unlock, mock_lock,
        mock_tracked, mock_map, tmp_path: Path
    ) -> None:
        """All deliveries for an initialized feed get state initialised."""
        from korgalore.cli import perform_pull

        feed_dir = tmp_path / "new-feed"
        feed_dir.mkdir()
        (feed_dir / "git" / "0.git").mkdir(parents=True)
        feed = _StubPIFeed(feed_dir, feed_key="new-feed")

        target = MagicMock()
        target.identifier = "test-target"

        mock_update.return_value = ([], ["new-feed"])

        feeds = {"new-feed": feed}
        deliveries = {
            "delivery-a": (feed, target, ["label-a"], None),
            "delivery-b": (feed, target, ["label-b"], None),
        }
        ctx = self._make_context(feeds, deliveries)

        with patch.object(feed, 'load_delivery_info', side_effect=StateError("no state")), \
             patch.object(feed, 'save_delivery_info') as mock_save:
            perform_pull(ctx, no_update=False, force=False, delivery_name=None)
            assert mock_save.call_count == 2
            called_names = sorted(c.args[0] for c in mock_save.call_args_list)
            assert called_names == ["delivery-a", "delivery-b"]

    @patch('korgalore.cli.map_deliveries')
    @patch('korgalore.cli.map_tracked_threads')
    @patch('korgalore.cli.lock_all_feeds')
    @patch('korgalore.cli.unlock_all_feeds')
    @patch('korgalore.cli.retry_all_failed_deliveries')
    @patch('korgalore.cli.update_all_feeds')
    def test_unrelated_feed_not_initialized(
        self, mock_update, mock_retry, mock_unlock, mock_lock,
        mock_tracked, mock_map, tmp_path: Path
    ) -> None:
        """Deliveries for non-initialized feeds are not touched."""
        from korgalore.cli import perform_pull

        # Create two feeds: one initialized, one not
        init_dir = tmp_path / "init-feed"
        init_dir.mkdir()
        (init_dir / "git" / "0.git").mkdir(parents=True)
        init_feed = _StubPIFeed(init_dir, feed_key="init-feed")

        other_dir = tmp_path / "other-feed"
        other_dir.mkdir()
        (other_dir / "git" / "0.git").mkdir(parents=True)
        other_feed = _StubPIFeed(other_dir, feed_key="other-feed")

        target = MagicMock()
        target.identifier = "test-target"

        # Only init-feed is initialized; neither is updated
        mock_update.return_value = ([], ["init-feed"])

        feeds = {"init-feed": init_feed, "other-feed": other_feed}
        deliveries = {
            "d-init": (init_feed, target, ["l"], None),
            "d-other": (other_feed, target, ["l"], None),
        }
        ctx = self._make_context(feeds, deliveries)

        with patch.object(init_feed, 'load_delivery_info', side_effect=StateError("no state")), \
             patch.object(init_feed, 'save_delivery_info') as mock_save_init, \
             patch.object(other_feed, 'load_delivery_info') as mock_load_other, \
             patch.object(other_feed, 'save_delivery_info') as mock_save_other:
            perform_pull(ctx, no_update=False, force=False, delivery_name=None)
            mock_save_init.assert_called_once_with("d-init")
            mock_load_other.assert_not_called()
            mock_save_other.assert_not_called()
