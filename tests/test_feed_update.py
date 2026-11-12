"""Tests for update_feed(), from the feed's own state files down to git.

The epochs are real repositories built with InboxRepo. Only the network
side is mocked: the manifest request, and the git clone and fetch.
"""

import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from korgalore import RemoteError
from korgalore.lei_feed import LeiFeed
from korgalore.lore_feed import LoreFeed
from korgalore.pi_feed import PIFeed
from tests.digest_helpers import InboxRepo
from tests.feed_helpers import manifest_response

WHEN = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
URL = 'https://lore.kernel.org/lkml'


def serve_manifest(feed: LoreFeed, **fingerprints: str) -> None:
    """Make the remote manifest list the given epochs, as e0='fpr', e1=..."""
    manifest = {f'/lkml/git/{key[1:]}.git': {'fingerprint': fpr} for key, fpr in fingerprints.items()}
    cast('MagicMock', feed._node).request.return_value = manifest_response(manifest)


def new_lore_feed(tmp_path: Path) -> LoreFeed:
    """A LoreFeed that has just been initialized over a one-message epoch."""
    repo = InboxRepo(tmp_path / 'lkml')
    repo.add(b'Subject: first\nMessage-ID: <first@example.com>\n\nbody\n', WHEN)
    feed = LoreFeed('lkml', repo.feed_dir, URL, lore_node=MagicMock(origins=[]))
    serve_manifest(feed, e0='a')
    feed.init_feed()
    return feed


@pytest.fixture
def lore_feed(tmp_path: Path) -> LoreFeed:
    """A LoreFeed that has been initialized and pulled once."""
    feed = new_lore_feed(tmp_path)
    with remote(feed):
        feed.update_feed()
    return feed


@pytest.fixture
def lei_feed(tmp_path: Path) -> LeiFeed:
    """A LeiFeed that has been initialized over a one-message search."""
    repo = InboxRepo(tmp_path / 'search')
    repo.add(b'Subject: first\nMessage-ID: <first@example.com>\n\nbody\n', WHEN)
    searches = json.dumps([{'output': f'v2:{repo.feed_dir}'}]).encode()
    with patch('korgalore.lei_feed.run_lei_command', return_value=(0, searches)):
        feed = LeiFeed('search', f'lei:{repo.feed_dir}')
    feed.init_feed()
    return feed


@contextmanager
def remote(feed: LoreFeed, failing: str | None = None) -> Generator[tuple[MagicMock, MagicMock], None, None]:
    """Mock the git clone and fetch; the one named by failing raises RemoteError.

    A clone creates the epoch as an empty repository, so the feed finds it
    on disk afterwards just like after a real clone.
    """

    def clone(epoch: int, shallow: bool = True) -> None:
        if not feed.get_gitdir(epoch).exists():
            InboxRepo(feed.feed_dir, epoch)

    mocks = {'clone_epoch': MagicMock(side_effect=clone), 'fetch_epoch': MagicMock()}
    if failing is not None:
        mocks[failing].side_effect = RemoteError('down')
    with patch.multiple(feed, **mocks):
        yield mocks['clone_epoch'], mocks['fetch_epoch']


class TestLoreUpdate:
    """The cached manifest in epochs.json decides whether a pull fetches."""

    def test_unchanged_manifest_skips_fetch(self, lore_feed: LoreFeed) -> None:
        with remote(lore_feed) as (_clone, fetch):
            assert lore_feed.update_feed() == PIFeed.STATUS_NOCHANGE
        fetch.assert_not_called()

    def test_first_update_after_init_fetches(self, tmp_path: Path) -> None:
        """init_feed() does not clone over an existing epoch, so it may be behind."""
        feed = new_lore_feed(tmp_path)

        with remote(feed) as (_clone, fetch):
            feed.update_feed()
        fetch.assert_called_once()

    @pytest.mark.parametrize('failing', ['fetch_epoch', 'clone_epoch'])
    def test_failed_update_is_retried(self, lore_feed: LoreFeed, failing: str) -> None:
        """A pull that fails must not mark the new manifest as fetched."""
        serve_manifest(lore_feed, e0='b', e1='c')
        with remote(lore_feed, failing=failing), pytest.raises(RemoteError):
            lore_feed.update_feed()

        with remote(lore_feed) as (clone, fetch):
            assert lore_feed.update_feed() == PIFeed.STATUS_UPDATED
        fetch.assert_called_once()
        clone.assert_called_once_with(epoch=1, shallow=False)


class TestCorruptFeedState:
    """A crash can leave korgalore.feed empty; the pull rebuilds it and carries on.

    The status is never STATUS_INITIALIZED, because the deliveries of an
    existing feed keep their own state. It is always STATUS_UPDATED, because
    the crash may have come before the deliveries of the last pull ran.
    """

    @pytest.mark.parametrize('new_mail', [False, True], ids=['no-new-mail', 'new-mail'])
    def test_lore_feed(self, lore_feed: LoreFeed, new_mail: bool) -> None:
        (lore_feed.feed_dir / 'korgalore.feed').write_bytes(b'')
        if new_mail:
            serve_manifest(lore_feed, e0='b')

        with remote(lore_feed) as (_clone, fetch):
            assert lore_feed.update_feed() == PIFeed.STATUS_UPDATED

        assert fetch.called == new_mail
        assert lore_feed.load_feed_state()['epochs'].keys() == {'0'}

    def test_lei_feed(self, lei_feed: LeiFeed) -> None:
        (lei_feed.feed_dir / 'korgalore.feed').write_bytes(b'')

        with patch('korgalore.lei_feed.run_lei_command', return_value=(0, b'')):
            assert lei_feed.update_feed() == PIFeed.STATUS_UPDATED

        assert lei_feed.load_feed_state()['epochs'].keys() == {'0'}


class TestCorruptEpochsFile:
    """epochs.json only records the last fetch, so a corrupt one means: fetch again."""

    @pytest.mark.parametrize(
        'content',
        [
            pytest.param(b'', id='empty-file'),
            pytest.param(b'[{"epoch": 0, "pa', id='truncated'),
            pytest.param(b'\xff\xfe', id='not-utf8'),
            pytest.param(b'{}', id='not-a-list'),
            pytest.param(b'[0]', id='entry-not-an-object'),
            pytest.param(b'[{"epoch": 0}]', id='entry-missing-keys'),
        ],
    )
    def test_fetches_and_rewrites_it(self, lore_feed: LoreFeed, content: bytes) -> None:
        (lore_feed.feed_dir / 'epochs.json').write_bytes(content)

        with remote(lore_feed) as (_clone, fetch):
            lore_feed.update_feed()
        fetch.assert_called_once()

        with remote(lore_feed) as (_clone, fetch):
            assert lore_feed.update_feed() == PIFeed.STATUS_NOCHANGE
        fetch.assert_not_called()
