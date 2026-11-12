"""Tests for the parts of PIFeed that read real git repositories.

The repositories are built with InboxRepo. The ones that tests only read
are built once per module.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from korgalore import GitError, run_git_command
from korgalore.pi_feed import PIFeed
from tests.conftest import make_pi_feed
from tests.digest_helpers import InboxRepo

WHEN = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 2, 20, 8, 30, tzinfo=UTC)
BAD_COMMIT = 'deadbeef' * 5


def real_feed(feed_dir: Path) -> PIFeed:
    """A PIFeed that finds its epochs, commits and subjects on disk."""
    return make_pi_feed(feed_dir, highest_epoch=None, top_commit=None, subject=None)


def read_epochs(feed: PIFeed, delivery: str = 'test-delivery') -> dict[str, Any]:
    epochs: dict[str, Any] = json.loads((feed.feed_dir / f'korgalore.{delivery}.info').read_text())['epochs']
    return epochs


@pytest.fixture(scope='module')
def empty_repo(tmp_path_factory: pytest.TempPathFactory) -> InboxRepo:
    return InboxRepo(tmp_path_factory.mktemp('empty'))


@pytest.fixture(scope='module')
def one_commit_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[InboxRepo, str]:
    """A repository with one message commit; returns it with that commit."""
    repo = InboxRepo(tmp_path_factory.mktemp('one'))
    return repo, repo.add(b'Subject: real commit\nMessage-ID: <real@example.com>\n\nbody\n', WHEN)


class TestFirstAndTopCommit:
    """get_first_commit and get_top_commit on empty and non-empty repositories."""

    @pytest.mark.parametrize('method', ['get_first_commit', 'get_top_commit'])
    @pytest.mark.parametrize('empty', [True, False], ids=['empty-repo-gives-empty-string', 'commit-hash'])
    def test_commit_lookup(
        self, method: str, empty: bool, empty_repo: InboxRepo, one_commit_repo: tuple[InboxRepo, str]
    ) -> None:
        repo, expected = (empty_repo, '') if empty else one_commit_repo

        assert getattr(real_feed(repo.feed_dir), method)(0) == expected


class TestIsEmptyRepoCache:
    """Tests for is_empty_repo caching and cache invalidation."""

    def test_cached_until_unlock(self, tmp_path: Path) -> None:
        """git is asked once; the cache survives lock and is cleared by unlock."""
        feed = real_feed(InboxRepo(tmp_path / 'test-feed').feed_dir)

        with patch('korgalore.pi_feed.run_git_command', wraps=run_git_command) as git:
            assert feed.is_empty_repo(0) is True
            assert feed.is_empty_repo(0) is True
            assert git.call_count == 1
        assert feed._empty_repo_cache == {0: True}

        feed.feed_lock()
        try:
            assert 0 in feed._empty_repo_cache
        finally:
            feed.feed_unlock()
        assert 0 not in feed._empty_repo_cache

    def test_cache_reflects_repo_state_after_unlock(self, tmp_path: Path) -> None:
        """After unlock and adding a commit, is_empty_repo returns False, and caches it."""
        repo = InboxRepo(tmp_path / 'test-feed')
        feed = real_feed(repo.feed_dir)
        assert feed.is_empty_repo(0) is True

        feed.feed_lock()
        repo.add_msg('new@example.com', WHEN)
        feed.feed_unlock()

        # Cache was cleared by unlock, so this re-checks the repo
        assert feed.is_empty_repo(0) is False
        assert feed._empty_repo_cache[0] is False


@pytest.fixture(scope='module')
def commits(tmp_path_factory: pytest.TempPathFactory) -> tuple[PIFeed, dict[str, str]]:
    repo = InboxRepo(tmp_path_factory.mktemp('noop'))
    message, removal = repo.add_many(
        [(b'Subject: Re: some thread\n\nbody\n', WHEN, 'm'), (b'blob content\n', WHEN, 'd')]
    )
    return real_feed(repo.feed_dir), {'m': message, 'd': removal}


class TestIsNoopCommit:
    """Tests for is_noop_commit detection of public-inbox commits without 'm' files.

    Detection is based on the absence of an 'm' object in the commit
    tree, mirroring the real public-inbox v2 layout where normal
    commits carry an 'm' (message) file and removal commits carry a 'd'
    (deleted) file.
    """

    @pytest.mark.parametrize(
        ('filename', 'noop'),
        [pytest.param('m', False, id='message-commit'), pytest.param('d', True, id='rm-commit')],
    )
    def test_noop_detection(self, commits: tuple[PIFeed, dict[str, str]], filename: str, noop: bool) -> None:
        feed, by_file = commits
        assert feed.is_noop_commit(0, by_file[filename]) is noop

    def test_bad_object_commit_raises_git_error(self, commits: tuple[PIFeed, dict[str, str]]) -> None:
        """A non-existent commit (bad object) must raise GitError.

        Regression: is_noop_commit returned True for bad-object commits,
        causing save_delivery_info to crash during failed delivery retry
        when it tried to ``git show`` the missing commit.

        See: c4de9f25-0c60-49e4-925f-7749eba57264@app.fastmail.com
        """
        with pytest.raises(GitError):
            commits[0].is_noop_commit(0, BAD_COMMIT)


class TestSaveDeliveryInfoMessages:
    """What save_delivery_info() writes for different 'm' blobs.

    A commit whose tree contains an 'm' file is not a no-op, so
    save_delivery_info() reads the message. If the blob is empty, the
    subject and msgid used to stay unbound and raise UnboundLocalError.

    Subjects flow through liblore's msg_get_subject(). ``emlpolicy``
    already decodes RFC 2047 and unfolds continuation lines, so what the
    helper adds is whitespace-run collapsing, which matters because these
    subjects are written into single-line state files and log messages.
    """

    @pytest.mark.parametrize(
        ('raw', 'subject', 'msgid'),
        [
            pytest.param(b'', '(no subject)', '(no message-id)', id='empty-blob'),
            pytest.param(
                b'Subject:\nMessage-ID: <empty@example.com>\n\nbody\n',
                '(no subject)',
                '<empty@example.com>',
                id='empty-subject-header',
            ),
            pytest.param(
                b'Subject: [PATCH]\tcrypto:\t  fix   the   thing\nMessage-ID: <ws@example.com>\n\nbody\n',
                '[PATCH] crypto: fix the thing',
                '<ws@example.com>',
                id='whitespace-runs-collapse',
            ),
        ],
    )
    def test_state_file_entry(self, tmp_path: Path, raw: bytes, subject: str, msgid: str) -> None:
        repo = InboxRepo(tmp_path / 'test-feed')
        commit = repo.add(raw, WHEN)
        feed = real_feed(repo.feed_dir)
        # The commit carries an 'm', so it must not be treated as a no-op.
        assert feed.is_noop_commit(0, commit) is False

        feed.save_delivery_info('test-delivery', 0, latest_commit=commit)

        entry = read_epochs(feed)['0']
        assert entry['last'] == commit
        assert entry['subject'] == subject
        assert entry['msgid'] == msgid

    def test_encoded_and_folded_subject_is_readable(self, tmp_path: Path) -> None:
        """A folded, RFC 2047 encoded subject lands decoded and on one line."""
        raw = b'Subject: =?utf-8?q?R=C3=A9paration_du?=\n =?utf-8?q?_pilote?=\nMessage-ID: <enc@example.com>\n\nbody\n'
        repo = InboxRepo(tmp_path / 'test-feed')
        commit = repo.add(raw, WHEN)

        assert real_feed(repo.feed_dir).get_subject_at_commit(0, commit) == 'Réparation du pilote'


class TestRebaseRecovery:
    def test_matches_legacy_unnormalized_subject(self, tmp_path: Path) -> None:
        """State written before normalization still matches during recovery.

        Pre-upgrade state files hold the un-collapsed subject. Recovery
        cleans the stored side too, so the newer commit is still identified
        exactly instead of falling back to the first candidate commit.

        Both commits share one date: recover_after_rebase() filters
        candidates with ``--since-as-filter``, which has one-second
        granularity, so both are candidates and the *second* one is not
        the fallback. That is what makes an exact-match failure observable.
        """
        repo = InboxRepo(tmp_path / 'test-feed')
        older, newer = repo.add_many(
            [
                (b'Subject: an earlier message\nMessage-ID: <older@example.com>\n\nbody\n', WHEN, 'm'),
                (b'Subject: crypto:\tfix   the   thing\nMessage-ID: <legacy@example.com>\n\nbody\n', WHEN, 'm'),
            ]
        )
        feed = real_feed(repo.feed_dir)

        # Write state the way an older korgalore would have: subject straight
        # off the parsed header, with its whitespace runs intact.
        feed.save_delivery_info('test-delivery', 0, latest_commit=newer)
        state_file = repo.feed_dir / 'korgalore.test-delivery.info'
        state = json.loads(state_file.read_text())
        state['epochs']['0']['subject'] = 'crypto:\tfix   the   thing'
        state_file.write_text(json.dumps(state))

        recovered = feed.recover_after_rebase('test-delivery', 0)

        # Without cleaning the stored subject this returns *older*, the
        # first candidate after the recorded date.
        assert recovered == newer
        assert recovered != older


class TestSaveDeliveryInfoEpochZero:
    """save_delivery_info() must honour an explicit epoch 0.

    Epoch 0 is falsy, so a truthiness check mistakes it for "no epoch
    given" and swaps in the highest epoch. After a rollover from 0.git to
    1.git, saving a commit that lives in 0.git then looks it up in 1.git
    and fails. These tests use two real epoch repositories so that the
    lookup actually happens.
    """

    @pytest.fixture
    def rolled_over(self, tmp_path: Path) -> tuple[PIFeed, str, str]:
        """A feed that has rolled over from epoch 0 to epoch 1, with each top commit."""
        feed_dir = tmp_path / 'test-feed'
        old = InboxRepo(feed_dir, epoch=0).add(b'Subject: old epoch\nMessage-ID: <old@example.com>\n\nbody\n', WHEN)
        new = InboxRepo(feed_dir, epoch=1).add(b'Subject: new epoch\nMessage-ID: <new@example.com>\n\nbody\n', LATER)
        return real_feed(feed_dir), old, new

    def test_explicit_epoch_zero_is_kept(self, rolled_over: tuple[PIFeed, str, str]) -> None:
        """A commit from 0.git is saved under epoch 0, not the highest epoch."""
        feed, old, _new = rolled_over
        assert feed.get_highest_epoch() == 1

        feed.save_delivery_info('test-delivery', 0, latest_commit=old)

        epochs = read_epochs(feed)
        assert list(epochs) == ['0']
        assert epochs['0']['last'] == old
        assert epochs['0']['msgid'] == '<old@example.com>'
        assert epochs['0']['commit_date'].startswith('2026-01-15 12:00:00')

    def test_epoch_zero_without_commit_uses_epoch_zero_top(self, rolled_over: tuple[PIFeed, str, str]) -> None:
        """Epoch 0 with no commit picks the top of 0.git, not of 1.git."""
        feed, old, _new = rolled_over

        feed.save_delivery_info('test-delivery', 0)

        assert read_epochs(feed)['0']['last'] == old

    def test_no_epoch_defaults_to_highest(self, rolled_over: tuple[PIFeed, str, str]) -> None:
        """Leaving the epoch out still means the highest epoch."""
        feed, _old, new = rolled_over

        feed.save_delivery_info('test-delivery')

        epochs = read_epochs(feed)
        assert list(epochs) == ['1']
        assert epochs['1']['last'] == new
        assert epochs['1']['msgid'] == '<new@example.com>'


class TestCorruptDeliveryState:
    """A delivery state file that cannot be read has lost its pointer for good.

    The pull must not crash on it. The delivery starts again as a new one,
    at the top of the feed, and the user is told once that mail may be
    missing.
    """

    @pytest.mark.parametrize(
        'content',
        [
            pytest.param(b'', id='empty-file'),
            pytest.param(b'{"epochs": {"0": {"la', id='truncated'),
            pytest.param(b'{"epochs": "\xff\xfe"}', id='not-utf8'),
            pytest.param(b'[]', id='not-an-object'),
            pytest.param(b'{"epochs": []}', id='epochs-not-an-object'),
        ],
    )
    def test_delivery_restarts_at_top(self, tmp_path: Path, content: bytes, caplog: pytest.LogCaptureFixture) -> None:
        repo = InboxRepo(tmp_path / 'feed')
        repo.add(b'Subject: first\nMessage-ID: <first@example.com>\n\nbody\n', WHEN)
        feed = real_feed(repo.feed_dir)
        feed.save_delivery_info('test-delivery')
        top = repo.add(b'Subject: second\nMessage-ID: <second@example.com>\n\nbody\n', LATER)
        state_file = repo.feed_dir / 'korgalore.test-delivery.info'
        state_file.write_bytes(content)

        with caplog.at_level(logging.WARNING, logger='korgalore'):
            assert feed.get_latest_commits_for_delivery('test-delivery') == []

        assert read_epochs(feed)['0']['last'] == top
        assert caplog.text.count('is corrupt') == 1

    def test_removed_by_another_reader(self, tmp_path: Path) -> None:
        """A digest worker reads without the feed lock, so a pull may remove the file first."""
        feed = real_feed(tmp_path)
        state_file = tmp_path / 'korgalore.test-delivery.info'
        state_file.write_bytes(b'')
        real_read_text = Path.read_text

        def read_then_lose_race(path: Path) -> str:
            text = real_read_text(path)
            path.unlink()
            return text

        with patch.object(Path, 'read_text', read_then_lose_race):
            assert not feed.has_delivery_pointer('test-delivery')

    def test_digest_state_reads_as_missing(self, tmp_path: Path) -> None:
        """The digest code reads the file on every pull, before any delivery runs."""
        feed = real_feed(tmp_path)
        (tmp_path / 'korgalore.test-delivery.info').write_bytes(b'')

        assert not feed.has_delivery_pointer('test-delivery')
        assert feed.load_digest_sent('test-delivery') is None
        assert feed.get_digest_history_start('test-delivery') is None
