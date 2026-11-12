"""Tests for update_feed(), from the feed's own state files down to git.

The epochs are real repositories built with InboxRepo. Only the network
side is mocked: the manifest request, and the git clone and fetch.
"""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from korgalore import RemoteError
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
