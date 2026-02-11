"""Tests for tracked thread delivery tuple format.

Verifies that map_tracked_threads() produces 4-tuples consistent with
map_deliveries(), so that retry_all_failed_deliveries() and perform_pull()
can unpack them without a ValueError.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import click

from korgalore.cli import map_tracked_threads
from korgalore.tracking import TrackedThread, TrackStatus


def _make_tracked_thread(track_id: str = 'track-abc123',
                         target: str = 'local',
                         labels: list | None = None) -> TrackedThread:
    """Create a TrackedThread with sensible defaults."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return TrackedThread(
        track_id=track_id,
        msgid='<test@example.com>',
        subject='Test thread',
        target=target,
        labels=labels or ['INBOX'],
        lei_path=Path('/tmp/lei-test'),
        created=now,
        last_update=now,
        last_new_message=now,
        status=TrackStatus.ACTIVE,
        message_count=1,
    )


def _make_context() -> click.Context:
    """Create a minimal Click context for map_tracked_threads."""
    ctx = click.Context(click.Command('test'))
    ctx.ensure_object(dict)
    ctx.obj['config'] = {'targets': {}, 'feeds': {}}
    ctx.obj['targets'] = {}
    ctx.obj['feeds'] = {}
    ctx.obj['deliveries'] = {}
    ctx.obj['data_dir'] = '/tmp/kgl-test'
    return ctx


class TestTrackedThreadDeliveryTuple:
    """Tracked thread deliveries must use the same 4-tuple as regular ones."""

    @patch('korgalore.cli.get_target')
    @patch('korgalore.cli.get_tracking_manifest')
    @patch('korgalore.cli.LeiFeed')
    def test_delivery_tuple_has_four_elements(
        self, mock_lei_cls, mock_manifest, mock_target
    ) -> None:
        """map_tracked_threads must store (feed, target, labels, subfolder)."""
        tracked = _make_tracked_thread()
        manifest = MagicMock()
        manifest.check_and_expire_threads.return_value = []
        manifest.get_active_threads.return_value = [tracked]
        mock_manifest.return_value = manifest

        mock_feed = MagicMock()
        mock_lei_cls.return_value = mock_feed
        mock_tgt = MagicMock()
        mock_target.return_value = mock_tgt

        ctx = _make_context()
        map_tracked_threads(ctx)

        delivery = ctx.obj['deliveries'][tracked.track_id]
        assert len(delivery) == 4, (
            f'Expected 4-tuple (feed, target, labels, subfolder), got {len(delivery)}'
        )

    @patch('korgalore.cli.get_target')
    @patch('korgalore.cli.get_tracking_manifest')
    @patch('korgalore.cli.LeiFeed')
    def test_subfolder_is_none(
        self, mock_lei_cls, mock_manifest, mock_target
    ) -> None:
        """Tracked threads do not support subfolders; fourth element must be None."""
        tracked = _make_tracked_thread()
        manifest = MagicMock()
        manifest.check_and_expire_threads.return_value = []
        manifest.get_active_threads.return_value = [tracked]
        mock_manifest.return_value = manifest

        mock_lei_cls.return_value = MagicMock()
        mock_target.return_value = MagicMock()

        ctx = _make_context()
        map_tracked_threads(ctx)

        _, _, _, subfolder = ctx.obj['deliveries'][tracked.track_id]
        assert subfolder is None

    @patch('korgalore.cli.get_target')
    @patch('korgalore.cli.get_tracking_manifest')
    @patch('korgalore.cli.LeiFeed')
    def test_labels_preserved(
        self, mock_lei_cls, mock_manifest, mock_target
    ) -> None:
        """Labels from the tracked thread must appear in the delivery tuple."""
        tracked = _make_tracked_thread(labels=['patch-review', 'urgent'])
        manifest = MagicMock()
        manifest.check_and_expire_threads.return_value = []
        manifest.get_active_threads.return_value = [tracked]
        mock_manifest.return_value = manifest

        mock_lei_cls.return_value = MagicMock()
        mock_target.return_value = MagicMock()

        ctx = _make_context()
        map_tracked_threads(ctx)

        _, _, labels, _ = ctx.obj['deliveries'][tracked.track_id]
        assert labels == ['patch-review', 'urgent']

    @patch('korgalore.cli.get_target')
    @patch('korgalore.cli.get_tracking_manifest')
    @patch('korgalore.cli.LeiFeed')
    def test_tuple_unpacks_like_regular_delivery(
        self, mock_lei_cls, mock_manifest, mock_target
    ) -> None:
        """The tuple must unpack as (feed, target, labels, subfolder) without error."""
        tracked = _make_tracked_thread()
        manifest = MagicMock()
        manifest.check_and_expire_threads.return_value = []
        manifest.get_active_threads.return_value = [tracked]
        mock_manifest.return_value = manifest

        mock_feed = MagicMock()
        mock_lei_cls.return_value = mock_feed
        mock_tgt = MagicMock()
        mock_target.return_value = mock_tgt

        ctx = _make_context()
        map_tracked_threads(ctx)

        # This is the exact unpacking used by retry_all_failed_deliveries
        for delivery_name, (feed, target, labels, subfolder) in ctx.obj['deliveries'].items():
            assert delivery_name == tracked.track_id
            assert feed is mock_feed
            assert target is mock_tgt
            assert labels == ['INBOX']
            assert subfolder is None
