"""Tests for keeping enough lore history for digests.

Lore clones are shallow, and a normal fetch moves the cut to one week
back. A digest that was last sent longer ago than that asks the fetch to
keep its history. The real-git tests use shallow mirror clones over
file://, with absolute dates, because the test commits have made-up
dates.
"""

import json
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from korgalore import RemoteError, StateError
from korgalore.cli import digest_history_needs, send_digest, update_all_feeds
from korgalore.lei_feed import LeiFeed
from korgalore.lore_feed import LoreFeed
from korgalore.maildir_target import MaildirTarget
from korgalore.pi_feed import PIFeed
from tests.digest_helpers import DAILY, DNAME, NOW, InboxRepo, ShallowCopy, digest_text, make_ctx


def digest_body(msg: EmailMessage) -> str:
    """The plain text of a digest, with the line wrapping undone."""
    return ' '.join(digest_text(msg).split())


def write_info(feed_dir: Path, info: dict[str, Any]) -> LoreFeed:
    feed_dir.mkdir(parents=True, exist_ok=True)
    (feed_dir / f'korgalore.{DNAME}.info').write_text(json.dumps(info))
    return LoreFeed('lkml', feed_dir, 'https://lore.kernel.org/lkml')


class TestShallowSince:
    @pytest.mark.parametrize(
        ('days', 'expected'),
        [
            pytest.param(None, None, id='no-digest-uses-the-default'),
            pytest.param(3, None, id='recent-digest-uses-the-default'),
            pytest.param(10, '2026-09-21 09:00:00 +0000', id='old-digest-keeps-its-history'),
            pytest.param(90, '2026-09-01 09:00:00 +0000', id='very-old-digest-is-capped'),
        ],
    )
    def test_shallow_since(self, days: int | None, expected: str | None) -> None:
        last_sent = None if days is None else NOW - timedelta(days=days)
        assert LoreFeed.shallow_since(last_sent, NOW) == expected


class TestFetchEpoch:
    """The git commands a fetch runs, with and without a digest's needs."""

    @staticmethod
    def fetch(tmp_path: Path, results: list[Any], keep: datetime | None = None) -> list[list[str]]:
        feed = LoreFeed('lkml', tmp_path / 'lkml', 'https://lore.kernel.org/lkml', lore_node=MagicMock(origins=[]))
        with patch('korgalore.lore_feed.run_git_command', side_effect=results) as git:
            try:
                feed.fetch_epoch(0, keep, now=NOW)
            finally:
                calls: list[list[str]] = [c.args[1] for c in git.call_args_list]
        return calls

    def test_default_falls_back_to_depth_one(self, tmp_path: Path) -> None:
        calls = self.fetch(tmp_path, [(128, b'', b'no commits'), (0, b'', b'')])
        assert calls[1] == ['fetch', 'origin', '--depth=1', '--update-shallow']

    @pytest.mark.parametrize(
        ('days', 'since'),
        [
            pytest.param(None, '1.week.ago', id='default'),
            pytest.param(10, '2026-09-21 09:00:00 +0000', id='digest-keeps-history'),
        ],
    )
    def test_fetch_command(self, tmp_path: Path, days: int | None, since: str) -> None:
        keep = None if days is None else NOW - timedelta(days=days)
        calls = self.fetch(tmp_path, [(0, b'', b'')], keep=keep)
        assert calls == [['fetch', 'origin', f'--shallow-since={since}', '--update-shallow']]

    def test_digest_fallback_never_cuts_history(self, tmp_path: Path) -> None:
        """--depth=1 would throw away what the digest needs, so do a plain fetch."""
        calls = self.fetch(tmp_path, [(128, b'', b'no commits'), (0, b'', b'')], keep=NOW - timedelta(days=10))
        assert calls[1] == ['fetch', 'origin']

    def test_fetch_failure(self, tmp_path: Path) -> None:
        with pytest.raises(RemoteError):
            self.fetch(tmp_path, [(128, b'', b'no'), (128, b'', b'still no')], keep=NOW - timedelta(days=10))

    def test_update_feed_passes_it_on(self, tmp_path: Path) -> None:
        feed = LoreFeed('lkml', tmp_path / 'lkml', 'https://lore.kernel.org/lkml', lore_node=MagicMock())
        keep = NOW - timedelta(days=10)
        with (
            patch.object(feed, 'load_feed_state', return_value={'epochs': {'0': {}}}),
            patch.object(feed, 'load_epochs_info', return_value=[(0, '/lkml/git/0.git', 'old')]),
            patch.object(feed, 'get_manifest_epochs', return_value=[(0, '/lkml/git/0.git', 'new')]),
            patch.object(feed, 'feed_updated', return_value=True),
            patch.object(feed, 'save_feed_state'),
            patch.object(feed, 'store_epochs_info'),
            patch.object(feed, 'fetch_epoch') as fetch_epoch,
        ):
            assert feed.update_feed(keep_history_since=keep) == PIFeed.STATUS_UPDATED
        fetch_epoch.assert_called_once_with(0, keep)


class TestHistoryStart:
    """How far back one digest delivery needs the feed history."""

    def test_no_state(self, tmp_path: Path) -> None:
        feed = LoreFeed('lkml', tmp_path / 'lkml', 'https://lore.kernel.org/lkml')
        assert feed.get_digest_history_start(DNAME) is None

    def test_older_of_pointer_and_last_sent(self, tmp_path: Path) -> None:
        # The pointer is a message from before the digest was sent
        feed = write_info(
            tmp_path / 'lkml',
            {
                'epochs': {'0': {'last': 'abc', 'commit_date': '2026-09-01 06:00:00 +0000'}},
                'digest': {'last_sent': '2026-09-01T07:00:00+00:00'},
            },
        )
        assert feed.get_digest_history_start(DNAME) == datetime(2026, 8, 31, 6, 0, tzinfo=UTC)

    def test_highest_epoch_pointer(self, tmp_path: Path) -> None:
        feed = write_info(
            tmp_path / 'lkml',
            {
                'epochs': {
                    '0': {'last': 'a', 'commit_date': '2026-01-01 00:00:00 +0000'},
                    '1': {'last': 'b', 'commit_date': '2026-09-20 00:00:00 +0000'},
                },
                'digest': {'last_sent': '2026-09-21T07:00:00+00:00'},
            },
        )
        assert feed.get_digest_history_start(DNAME) == datetime(2026, 9, 19, 0, 0, tzinfo=UTC)

    def test_empty_feed_has_only_last_sent(self, tmp_path: Path) -> None:
        feed = write_info(tmp_path / 'lkml', {'epochs': {}, 'digest': {'last_sent': '2026-09-21T07:00:00+00:00'}})
        assert feed.get_digest_history_start(DNAME) == datetime(2026, 9, 20, 7, 0, tzinfo=UTC)

    def test_bad_commit_date(self, tmp_path: Path) -> None:
        feed = write_info(tmp_path / 'lkml', {'epochs': {'0': {'last': 'a', 'commit_date': 'yesterday'}}})
        with pytest.raises(StateError):
            feed.get_digest_history_start(DNAME)


def lore_mock(feed_key: str) -> MagicMock:
    feed = MagicMock(spec=LoreFeed)
    feed.feed_key = feed_key
    feed.STATUS_UPDATED = PIFeed.STATUS_UPDATED
    feed.STATUS_INITIALIZED = PIFeed.STATUS_INITIALIZED
    feed.update_feed.return_value = PIFeed.STATUS_NOCHANGE
    return feed


class TestHistoryNeeds:
    def test_oldest_digest_per_feed_wins(self) -> None:
        lkml, netdev = lore_mock('lkml'), lore_mock('netdev')
        lkml.get_digest_history_start.side_effect = lambda d: {
            'a': NOW - timedelta(days=9),
            'b': NOW - timedelta(days=20),
        }[d]
        netdev.get_digest_history_start.return_value = None
        obj = {
            'deliveries': {
                'a': (lkml, None, [], None),
                'b': (lkml, None, [], None),
                'c': (netdev, None, [], None),
                'msgs': (lkml, None, [], None),
            },
            'digest_schedules': {'a': DAILY, 'b': DAILY, 'c': DAILY},
        }
        assert digest_history_needs(make_ctx(obj)) == {'lkml': NOW - timedelta(days=20)}
        # Message deliveries never ask for history
        assert [c.args[0] for c in lkml.get_digest_history_start.call_args_list] == ['a', 'b']

    def test_lei_feeds_are_skipped(self) -> None:
        lei = MagicMock(spec=LeiFeed)
        obj = {'deliveries': {'a': (lei, None, [], None)}, 'digest_schedules': {'a': DAILY}}
        assert digest_history_needs(make_ctx(obj)) == {}
        lei.get_digest_history_start.assert_not_called()

    def test_bad_state_is_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        lkml = lore_mock('lkml')
        lkml.get_digest_history_start.side_effect = StateError('Bad commit_date')
        obj = {'deliveries': {'a': (lkml, None, [], None)}, 'digest_schedules': {'a': DAILY}}
        assert digest_history_needs(make_ctx(obj)) == {}
        assert 'Bad commit_date' in caplog.text

    def test_update_all_feeds_passes_needs(self) -> None:
        lkml, netdev = lore_mock('lkml'), lore_mock('netdev')
        lkml.get_digest_history_start.return_value = NOW - timedelta(days=20)
        obj = {
            'feeds': {'lkml': lkml, 'netdev': netdev},
            'deliveries': {'a': (lkml, None, [], None), 'b': (netdev, None, [], None)},
            'digest_schedules': {'a': DAILY},
            'hide_bar': True,
        }
        update_all_feeds(make_ctx(obj))
        lkml.update_feed.assert_called_once_with(keep_history_since=NOW - timedelta(days=20))
        netdev.update_feed.assert_called_once_with()


class TestRealHistory:
    """The four-week vacation, with real shallow clones."""

    @pytest.fixture
    def upstream(self, tmp_path: Path) -> InboxRepo:
        return InboxRepo(tmp_path / 'upstream')

    @staticmethod
    def first_digest(upstream: InboxRepo, tmp_path: Path, days_ago: int) -> ShallowCopy:
        """A feed whose last digest went out days_ago days before NOW."""
        sent = NOW - timedelta(days=days_ago)
        upstream.add_msg('older@x', sent - timedelta(hours=3))
        upstream.add_msg('before@x', sent - timedelta(hours=1))
        local = ShallowCopy(upstream, tmp_path / 'lkml', since=sent - timedelta(days=5))
        maildir = MaildirTarget('local', str(tmp_path / 'first'))
        assert send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=sent)
        return local

    @staticmethod
    def fetch_for_digest(local: ShallowCopy) -> None:
        feed = local.feed()
        feed.fetch_epoch(0, feed.get_digest_history_start(DNAME), now=NOW)

    @staticmethod
    def next_digest(local: ShallowCopy, tmp_path: Path) -> str:
        maildir = MaildirTarget('local', str(tmp_path / 'next'))
        parts = send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=NOW)
        assert len(parts) == 1
        return digest_body(parts[0])

    @pytest.mark.parametrize(
        'shrink_first', [False, True], ids=['vacation-loses-nothing', 'lost-history-is-fetched-back']
    )
    def test_history_is_kept(self, tmp_path: Path, upstream: InboxRepo, shrink_first: bool) -> None:
        """A feed that already lost history (older korgalore) gets it back."""
        local = self.first_digest(upstream, tmp_path, days_ago=28)
        upstream.add_msg('vacation@x', NOW - timedelta(days=20))
        upstream.add_msg('recent@x', NOW - timedelta(days=3))
        if shrink_first:
            local.fetch(since=NOW - timedelta(days=7))
            feed = local.feed()
            assert feed.find_history_gap(DNAME, feed.get_latest_commits_for_delivery(DNAME)) is not None

        self.fetch_for_digest(local)

        text = self.next_digest(local, tmp_path)
        assert 'vacation@x' in text
        assert 'recent@x' in text
        assert 'Some messages are missing' not in text

    def test_very_long_vacation_is_capped(self, tmp_path: Path, upstream: InboxRepo) -> None:
        """Past HISTORY_MAX, older messages stay out and the digest says so."""
        local = self.first_digest(upstream, tmp_path, days_ago=45)
        upstream.add_msg('too-old@x', NOW - timedelta(days=40))
        upstream.add_msg('vacation@x', NOW - timedelta(days=20))
        upstream.add_msg('recent@x', NOW - timedelta(days=3))
        local.fetch(since=NOW - timedelta(days=7))

        self.fetch_for_digest(local)

        text = self.next_digest(local, tmp_path)
        assert 'too-old@x' not in text
        assert 'vacation@x' in text
        assert 'Some messages are missing. Korgalore only has messages from 2026-09-11' in text


class TestHistoryGap:
    """A pointer older than the shallow cut means missing messages.

    Each test starts with an older commit before the pointer: a pointer
    with no parent looks like a rebased feed to korgalore.
    """

    @pytest.fixture
    def upstream(self, tmp_path: Path) -> InboxRepo:
        return InboxRepo(tmp_path / 'upstream')

    @pytest.fixture
    def gapped(self, tmp_path: Path, upstream: InboxRepo) -> tuple[ShallowCopy, MaildirTarget, datetime]:
        """Four weeks of vacation: the cut moves past the pointer.

        Returns the clone, the target of the first digest, and the time of the
        only message that is still visible.
        """
        upstream.add_msg('older@x', NOW - timedelta(days=29))
        upstream.add_msg('before@x', NOW - timedelta(days=28, hours=1))
        local = ShallowCopy(upstream, tmp_path / 'lkml', since=NOW - timedelta(days=35))
        maildir = MaildirTarget('local', str(tmp_path / 'mail'))
        send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=NOW - timedelta(days=28))
        upstream.add_msg('lost@x', NOW - timedelta(days=20))
        after = NOW - timedelta(days=3)
        upstream.add_msg('kept@x', after)
        local.fetch(since=NOW - timedelta(days=7))
        return local, maildir, after

    def test_gap_is_found(self, gapped: tuple[ShallowCopy, MaildirTarget, datetime]) -> None:
        local, _, after = gapped
        feed = local.feed()
        commits = feed.get_latest_commits_for_delivery(DNAME)
        assert len(commits) == 1  # lost@x is hidden by the cut
        assert feed.find_history_gap(DNAME, commits) == after

    def test_digest_says_messages_are_missing(self, gapped: tuple[ShallowCopy, MaildirTarget, datetime]) -> None:
        local, maildir, _ = gapped
        parts = send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=NOW)

        assert len(parts) == 1
        text = digest_body(parts[0])
        assert 'Some messages are missing.' in text
        assert 'kept@x' in text
        assert 'lost@x' not in text

    def test_cut_right_after_pointer_is_no_gap(self, tmp_path: Path, upstream: InboxRepo) -> None:
        """The first commit after the cut can be the one right after the pointer."""
        upstream.add_msg('older@x', NOW - timedelta(days=10, hours=1))
        pointer = upstream.add_msg('before@x', NOW - timedelta(days=10))
        local = ShallowCopy(upstream, tmp_path / 'lkml', since=NOW - timedelta(days=11))
        local.feed().save_delivery_info(DNAME, 0, pointer, digest_sent=NOW - timedelta(days=10))
        upstream.add_msg('next@x', NOW - timedelta(days=3))
        local.fetch(since=NOW - timedelta(days=7))

        feed = local.feed()
        assert (local.gitdir / 'shallow').exists()
        commits = feed.get_latest_commits_for_delivery(DNAME)
        assert len(commits) == 1
        assert feed.find_history_gap(DNAME, commits) is None

    @pytest.mark.parametrize('shallow', [True, False], ids=['pointer-inside-history', 'full-clone'])
    def test_no_gap(self, tmp_path: Path, upstream: InboxRepo, shallow: bool) -> None:
        age = timedelta(days=2 if shallow else 30)
        if shallow:
            upstream.add_msg('old@x', NOW - timedelta(days=6))
        pointer = upstream.add_msg('before@x', NOW - age)
        source = ShallowCopy(upstream, tmp_path / 'lkml', since=NOW - timedelta(days=7)) if shallow else upstream
        source.feed().save_delivery_info(DNAME, 0, pointer, digest_sent=NOW - age)
        upstream.add_msg('next@x', NOW - timedelta(days=1))
        if isinstance(source, ShallowCopy):
            source.fetch(since=NOW - timedelta(days=7))

        feed = source.feed()
        commits = feed.get_latest_commits_for_delivery(DNAME)
        assert feed.find_history_gap(DNAME, commits) is None

    def test_gap_with_no_new_threads_is_still_sent(self, tmp_path: Path, upstream: InboxRepo) -> None:
        """The only news is that something is missing, and that is news."""
        upstream.add_msg('older@x', NOW - timedelta(days=29))
        upstream.add_msg('before@x', NOW - timedelta(days=28, hours=1))
        local = ShallowCopy(upstream, tmp_path / 'lkml', since=NOW - timedelta(days=35))
        maildir = MaildirTarget('local', str(tmp_path / 'mail'))
        send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=NOW - timedelta(days=28))
        upstream.add_msg('lost@x', NOW - timedelta(days=20))
        # The only commit after the cut is a deletion, so there are no threads
        upstream.add(b'', NOW - timedelta(days=3), filename='d')
        local.fetch(since=NOW - timedelta(days=7))

        parts = send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=NOW)

        assert len(parts) == 1
        assert 'Some messages are missing.' in digest_text(parts[0])


class TestDigestState:
    def test_round_trip_keeps_pointer(self, repo: InboxRepo) -> None:
        commit = repo.add_msg('a@x', NOW)
        feed = repo.feed()
        feed.save_delivery_info(DNAME, 0, commit, digest_sent=NOW)

        info = feed.load_delivery_info(DNAME)
        assert info['epochs']['0']['last'] == commit
        assert info['digest'] == {'last_sent': NOW.isoformat()}
        assert feed.load_digest_sent(DNAME) == NOW

    def test_message_save_keeps_digest_state(self, repo: InboxRepo) -> None:
        commit = repo.add_msg('a@x', NOW)
        feed = repo.feed()
        feed.save_delivery_info(DNAME, 0, commit, digest_sent=NOW)
        feed.save_delivery_info(DNAME, 0, commit)
        assert feed.load_digest_sent(DNAME) == NOW

    def test_missing(self, repo: InboxRepo) -> None:
        assert repo.feed().load_digest_sent('nope') is None

    def test_corrupt(self, repo: InboxRepo) -> None:
        state = repo.feed_dir / f'korgalore.{DNAME}.info'
        state.write_text(json.dumps({'epochs': {}, 'digest': {'last_sent': 'yesterday'}}))
        with pytest.raises(StateError):
            repo.feed().load_digest_sent(DNAME)

    def test_commits_since(self, repo: InboxRepo) -> None:
        repo.add_msg('a@x', NOW - timedelta(days=2))
        second = repo.add_msg('b@x', NOW - timedelta(hours=2))
        third = repo.add_msg('c@x', NOW - timedelta(hours=1))
        assert repo.feed().get_commits_since(NOW - timedelta(days=1)) == [(0, second), (0, third)]
        assert repo.feed().get_commits_since(NOW) == []

    def test_commits_since_empty_repo(self, repo: InboxRepo) -> None:
        assert repo.feed().get_commits_since(NOW) == []

    def test_commits_since_spans_epochs(self, repo: InboxRepo) -> None:
        """A list that rolled over to a new epoch inside the window loses nothing."""
        repo.add_msg('old@x', NOW - timedelta(days=2))
        last_in_old = repo.add_msg('a@x', NOW - timedelta(hours=3))
        new_epoch = InboxRepo(repo.feed_dir, epoch=1)
        first_in_new = new_epoch.add_msg('b@x', NOW - timedelta(hours=2))
        assert repo.feed().get_commits_since(NOW - timedelta(days=1)) == [(0, last_in_old), (1, first_in_new)]
        assert repo.feed().get_commits_since(NOW - timedelta(hours=2, minutes=30)) == [(1, first_in_new)]
