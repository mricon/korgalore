import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, lockf
from pathlib import Path
from typing import Any

from liblore.utils import clean_header, msg_get_subject, parse_message

from korgalore import FeedLockedError, GitError, PublicInboxError, StateCorruptError, StateError, run_git_command

logger = logging.getLogger('korgalore')

# We use this to cache commit messages to avoid reparsing them multiple times
# during delivery just to get the subject
COMMIT_SUBJECT_CACHE: dict[str, str] = {}
LOCKED_FEEDS: dict[str, Any] = {}
# We retry failed deliveries for 5 days and then give up
RETRY_FAILED_INTERVAL = 5 * 24 * 60 * 60  # 5 days in seconds


class PIFeed:
    """Base class for public-inbox feed implementations.

    Provides core functionality for interacting with public-inbox git
    repositories, including commit traversal, message extraction, state
    management, and delivery tracking. Subclassed by LoreFeed and LeiFeed.
    """

    # Status constants for update_feed() return value
    STATUS_NOCHANGE: int = 0
    STATUS_UPDATED: int = 1
    STATUS_INITIALIZED: int = 4

    def __init__(self, feed_key: str, feed_dir: Path) -> None:
        self._branch_cache: dict[str, str] = {}
        self._empty_repo_cache: dict[int, bool] = {}
        self.feed_key: str = feed_key
        self.feed_dir: Path = feed_dir
        self.feed_type: str = 'unknown'
        self.feed_url: str = ''

    def _read_jsonl_file(self, filepath: Path) -> list[tuple[int | str, ...]]:
        """Read a JSONL state file and return a list of tuples."""
        results: list[tuple[int | str, ...]] = []
        if not filepath.exists():
            return results
        with filepath.open() as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                results.append(tuple(obj))
        return results

    def _write_jsonl_file(self, filepath: Path, data: list[tuple[int | str, ...]]) -> None:
        """Write a list of tuples to a JSONL state file."""
        if not len(data):
            # Remove the file if it exists
            if filepath.exists():
                filepath.unlink()
            return
        content = ''.join(json.dumps(obj) + '\n' for obj in data)
        self._atomic_write(filepath, content)

    def _atomic_write(self, filepath: Path, content: str) -> None:
        """Write content to file atomically using temp file and rename."""
        dirpath = filepath.parent
        fd, tmp_name = tempfile.mkstemp(dir=dirpath, prefix='.tmp_')
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(content)
                # The rename alone is atomic but not durable: a crash can
                # land the rename without the data and leave an empty file.
                # Syncing the directory too is not needed, because losing
                # the rename only brings back the old, valid content.
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(filepath)
        except Exception:
            # Clean up temp file on error
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

    def get_gitdir(self, epoch: int) -> Path:
        """Return the path to the git directory for a specific epoch."""
        return self.feed_dir / 'git' / f'{epoch}.git'

    def _append_to_jsonl_file(self, filepath: Path, obj: tuple[int | str, ...]) -> None:
        """Append a tuple as a JSONL entry to a state file."""
        with filepath.open('a') as f:
            line = json.dumps(obj)
            f.write(line + '\n')

    def _perform_legacy_migration(self) -> None:
        """
        Version 0.1 stored state in a single feed_dir/git/{epoch}.git/korgalore.info file.
        Version 0.2 decouples this into multiple state files:
            - feed_dir/korgalore.feed : feed update state, which tracks the folowing things:
                - epochs: {
                    - epoch_number: {
                        - last_update: timestamp of last update
                        - update_successful: whether the last update was successful
                        - latest_commit: latest known commit hash
                        }
                - extra_data: {
                    'feed_type': 'lei' or 'lore',
                    'feed_url': public-inbox URL (for feed_type='lore'),
                    ... other data as needed ...
                    }
                }
            - feed_dir/korgalore.{delivery_name}.info : per-delivery state files, which track:
                - epochs: {
                    - epoch_number: {
                        - last: latest commit hash processed
                        - subject: subject of last processed message
                        - msgid: message-id of last processed message
                        - commit_date: date of last processed message
                    }
                }
            - feed_dir/korgalore.{delivery_name}.failed : JSONL file of per-delivery messages to retry
                - (epoch_number, commit_hash, first_failed_datetime, retry_count)
                - ...
            - feed_dir/korgalore.{delivery_name}.rejected : JSONL file of per-delivery deliveries we've given up on
                - (epoch_number, commit_hash, first_failed_datetime, retry_count, given_up_datetime)
                - ...
        This function migrates from the old single korgalore.info file to the new structure
        and leaves a backup of the old file as korgalore.info.pre-migration to indicate that the
        migration has been performed.

        Since version 0.1 only supported a single delivery per feed, we assume that the config
        file was not modified between version upgrades, so we only perform this migration once.
        """
        # Check if the git directory exists - if not, there's nothing to migrate
        epochs_dir = self.feed_dir / 'git'
        if not epochs_dir.exists():
            return  # New feed, nothing to migrate

        # Check if there is a legacy korgalore.info file
        try:
            highest_epoch = self.get_highest_epoch()
        except PublicInboxError:
            return  # git dir exists but no epoch repos yet, nothing to migrate
        legacy_info_path = self.feed_dir / 'git' / f'{highest_epoch}.git' / 'korgalore.info'
        if not legacy_info_path.exists():
            return  # No legacy file, nothing to do

        # In the 0.1 version, the directory was named the same as the source name, so we
        # assume delivery_name will be korgalore.dirname.info
        delivery_name = self.feed_dir.name

        # Read the legacy info
        with legacy_info_path.open() as f:
            lgi = json.load(f)

        latest_commit = lgi.get('last')

        self.save_delivery_info(delivery_name=delivery_name, epoch=highest_epoch, latest_commit=latest_commit)

        self.save_feed_state(
            epoch=highest_epoch,
            latest_commit=latest_commit,
            success=True,
        )

    def _get_state_file_path(self, delivery_name: str | None = None, suffix: str = 'info') -> Path:
        if not delivery_name:
            return self.feed_dir / f'korgalore.{suffix}'
        return self.feed_dir / f'korgalore.{delivery_name}.{suffix}'

    def _get_default_branch(self, gitdir: Path) -> str:
        """Detect the default branch name in the repository."""
        gitdir_str = str(gitdir)

        # Check cache first
        if gitdir_str in self._branch_cache:
            return self._branch_cache[gitdir_str]

        # Try to get the symbolic ref for HEAD
        gitargs = ['symbolic-ref', '-q', 'HEAD']
        retcode, output, _err = run_git_command(gitdir_str, gitargs)
        if retcode == 0:
            # Output is like 'refs/remotes/origin/main' - extract the branch name
            branch_name = output.decode().strip().split('/')[-1]
            self._branch_cache[gitdir_str] = branch_name
            return branch_name

        # Fallback: try to find the first branch
        gitargs = ['branch', '--format=%(refname:short)']
        retcode, output, _err = run_git_command(gitdir_str, gitargs)
        if retcode == 0 and output:
            # Return the first branch listed
            branch_name = output.decode().strip().split('\n')[0]
            self._branch_cache[gitdir_str] = branch_name
            return branch_name

        # Last fallback: assume 'master'
        logger.warning("Could not detect default branch in %s, falling back to 'master'", gitdir)
        branch_name = 'master'
        self._branch_cache[gitdir_str] = branch_name
        return branch_name

    def find_epochs(self) -> list[int]:
        """Find all epoch directories in the feed and return sorted list."""
        epochs_dir = self.feed_dir / 'git'
        if not epochs_dir.exists():
            raise PublicInboxError(f'No existing epochs found in {epochs_dir}.')
        # List this directory for existing epochs
        existing_epochs: list[int] = []
        for item in epochs_dir.iterdir():
            if item.is_dir() and item.name.endswith('.git'):
                epoch_str = item.name.replace('.git', '')
                try:
                    epoch_num = int(epoch_str)
                    existing_epochs.append(epoch_num)
                except ValueError:
                    logger.debug('Invalid epoch directory: %s', item.name)
        if not existing_epochs:
            raise PublicInboxError(f'No existing epochs found in {epochs_dir}.')
        return sorted(existing_epochs)

    def get_highest_epoch(self) -> int:
        """Return the highest (most recent) epoch number."""
        epochs = self.find_epochs()
        return max(epochs)

    def get_all_commits_in_epoch(self, epoch: int) -> list[str]:
        """Return all commits in an epoch in chronological order."""
        gitdir = self.get_gitdir(epoch)
        branch = self._get_default_branch(gitdir)
        gitargs = ['rev-list', '--reverse', branch]
        retcode, output, error = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f'Git rev-list failed (exit {retcode}): {error.decode()}')
        if len(output):
            commits = output.decode().splitlines()
        else:
            commits = []
        return commits

    def recover_after_rebase(self, delivery_name: str, epoch: int) -> str:
        """Recover delivery state after a feed rebase by matching commit metadata."""
        # Load delivery info to find last processed commit
        delivery_info = self.load_delivery_info(delivery_name)
        if str(epoch) in delivery_info.get('epochs', {}):
            info = delivery_info['epochs'][str(epoch)]
        else:
            raise StateError(f'No delivery info found for epoch {epoch} in delivery {delivery_name}.')

        # Get the commit's date and parse it into datetime
        # The string is ISO with tzinfo: "2025-11-04 20:47:21 +0000"
        commit_date_str = info.get('commit_date')
        if not commit_date_str:
            raise StateError(f'No commit_date found in the state file for {delivery_name}.')
        commit_date = datetime.strptime(commit_date_str, '%Y-%m-%d %H:%M:%S %z')
        logger.debug('Last processed commit date: %s', commit_date.isoformat())
        # Try to find the new hash of this commit in the log by matching the subject and
        # message-id.
        gitdir = self.get_gitdir(epoch)
        gitargs = ['rev-list', '--reverse', '--since-as-filter', commit_date_str, 'HEAD']
        retcode, output, _err = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            # Not sure what happened here, just give up and return the latest commit
            logger.warning('Could not run rev-list to recover after rebase, returning latest commit.')
            latest_commit = self.get_top_commit(epoch)
            return latest_commit

        possible_commits = output.decode().splitlines()
        if not possible_commits:
            # Just record the latest info, then
            self.save_delivery_info(delivery_name, epoch)
            latest_commit = self.get_top_commit(epoch)
            return latest_commit

        first_commit = possible_commits[0]
        last_commit = ''
        # Holds the parsed message for whichever commit we settle on. Tracking
        # it separately from the loop variable keeps it unambiguously bound on
        # both paths out of the loop.
        matched_msg: EmailMessage | None = None
        for commit in possible_commits:
            raw_message = self.get_message_at_commit(epoch, commit)
            msg = parse_message(raw_message)
            subject = msg_get_subject(msg) or '(no subject)'
            msgid = msg.get('Message-ID', '(no message-id)')
            # msg_get_subject() collapses internal whitespace runs, which
            # state files written before it holding a tab- or double-space
            # subject would no longer match. Clean the stored side too;
            # clean_header() is idempotent, so this is a no-op for state
            # written by the current code.
            if subject == clean_header(info.get('subject')) and msgid == info.get('msgid'):
                logger.debug('Found matching commit: %s', commit)
                last_commit = commit
                matched_msg = msg
                break
        if matched_msg is None:
            logger.error('Could not find exact commit after rebase.')
            logger.error('Returning first possible commit after date: %s', first_commit)
            last_commit = first_commit
            raw_message = self.get_message_at_commit(epoch, last_commit)
            matched_msg = parse_message(raw_message)
        else:
            logger.debug('Recovered exact matching commit after rebase: %s', last_commit)

        self.save_delivery_info(delivery_name, epoch, latest_commit=last_commit, message=matched_msg)
        return last_commit

    def get_latest_commits_for_delivery(self, delivery_name: str) -> list[tuple[int, str]]:
        """Return list of (epoch, commit) tuples for new commits since last delivery."""
        try:
            dinfo = self.load_delivery_info(delivery_name)
        except StateError:
            # Fallback for edge cases, e.g. a new delivery added to config between runs.
            # Normal first-clone initialisation is handled by perform_pull() after update_all_feeds().
            logger.info('Initializing new delivery: %s', delivery_name)
            self.save_delivery_info(delivery_name)
            return []

        # Grab the highest epoch we know about
        known_epochs = [int(e) for e in dinfo.get('epochs', {})]
        highest_known_epoch = max(known_epochs)
        logger.debug('Highest known epoch for delivery %s: %s', delivery_name, highest_known_epoch)
        since_commit = dinfo['epochs'][str(highest_known_epoch)]['last']

        # is this still a valid commit?
        gitdir = self.get_gitdir(highest_known_epoch)
        gitargs = ['cat-file', '-e', f'{since_commit}^']
        retcode, output, _err = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            # The commit is not valid anymore, so try to find the latest commit by other
            # means.
            logger.debug('Since commit %s not found, trying to recover after rebase.', since_commit)
            since_commit = self.recover_after_rebase(delivery_name, highest_known_epoch)
        gitargs = ['rev-list', '--reverse', f'{since_commit}..HEAD']
        retcode, output, error = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f'Git rev-list failed (exit {retcode}): {error.decode()}')
        if len(output):
            new_commits = [(highest_known_epoch, x) for x in output.decode().splitlines()]
        else:
            new_commits = []

        # Now check if the underlying repo has rolled over to the new epoch
        highest_found_epoch = self.get_highest_epoch()
        if highest_found_epoch > highest_known_epoch:
            logger.debug('New epoch detected: %s', highest_found_epoch)
            # Get all commits in this epoch
            commits = self.get_all_commits_in_epoch(highest_found_epoch)
            if commits:
                new_commits += [(highest_found_epoch, x) for x in commits]

        return new_commits

    def get_commits_since(self, since: datetime) -> list[tuple[int, str]]:
        """Return (epoch, commit) tuples for the commits made after since.

        Every local epoch is walked, oldest first, so a feed that rolled
        over to a new epoch inside the window loses nothing. This uses the
        commit date, which public-inbox sets when a message arrives, so it
        is not affected by wrong Date headers.
        """
        commits: list[tuple[int, str]] = []
        for epoch in self.find_epochs():
            if self.is_empty_repo(epoch):
                continue
            gitdir = self.get_gitdir(epoch)
            branch = self._get_default_branch(gitdir)
            gitargs = ['rev-list', '--reverse', f'--since-as-filter={since.isoformat()}', branch]
            retcode, output, error = run_git_command(str(gitdir), gitargs)
            if retcode != 0:
                raise GitError(f'Git rev-list failed (exit {retcode}): {error.decode()}')
            commits.extend((epoch, commit) for commit in output.decode().splitlines())
        return commits

    def is_noop_commit(self, epoch: int, commitish: str) -> bool:
        """Check if a commit has no 'm' file and should be skipped.

        Public-inbox v2 repositories can contain commits that carry no
        message blob:

        * 'rm' commits — record the removal of a message.  The tree
          contains a 'd' file (the deleted message) but no 'm' file.
        * 'purged …' commits — record content scrubbing via
          replace_oids().  The tree may have no entries at all.

        Both types are no-ops for delivery purposes.  We detect them
        by checking for the absence of the 'm' object in the commit
        tree rather than relying on the commit subject, since subjects
        are derived from email headers and could match coincidentally.

        Raises GitError if the commit object itself is missing (bad
        object), since treating a missing commit as a no-op would cause
        save_delivery_info to crash downstream.
        """
        gitdir = self.get_gitdir(epoch)
        # First verify the commit object exists locally.
        retcode, _output, _err = run_git_command(str(gitdir), ['cat-file', '-e', commitish])
        if retcode != 0:
            raise GitError(f'Bad object {commitish} in epoch {epoch}')
        # Now check whether the commit tree contains an 'm' file.
        gitargs = ['cat-file', '-e', f'{commitish}:m']
        retcode, _output, _err = run_git_command(str(gitdir), gitargs)
        return retcode != 0

    def get_message_at_commit(self, epoch: int, commitish: str) -> bytes:
        """Retrieve raw email message bytes from a specific git commit."""
        gitdir = self.get_gitdir(epoch)
        gitargs = ['show', f'{commitish}:m']
        retcode, output, error = run_git_command(str(gitdir), gitargs)
        if retcode == 128:
            raise StateError(f'Commit {commitish} does not have a message file.')
        if retcode != 0:
            raise GitError(f'Git show failed (exit {retcode}): {error.decode()}')
        return output

    def get_subject_at_commit(self, epoch: int, commitish: str) -> str:
        """Get email subject line from a commit, with caching."""
        global COMMIT_SUBJECT_CACHE
        try:
            return COMMIT_SUBJECT_CACHE[commitish]
        except KeyError:
            raw_msg = self.get_message_at_commit(epoch, commitish)
            msg = parse_message(raw_msg)
            subject: str = msg_get_subject(msg) or '(no subject)'
            COMMIT_SUBJECT_CACHE[commitish] = subject
            return subject

    def is_empty_repo(self, epoch: int) -> bool:
        """Check if a repository has no commits.

        Results are cached per epoch and cleared on feed_unlock().
        """
        if epoch in self._empty_repo_cache:
            return self._empty_repo_cache[epoch]
        gitdir = self.get_gitdir(epoch)
        retcode, output, error = run_git_command(str(gitdir), ['branch', '--list'])
        if retcode != 0:
            raise GitError(f'Git branch --list failed (exit {retcode}): {error.decode()}')
        empty = not output.strip()
        self._empty_repo_cache[epoch] = empty
        return empty

    def get_top_commit(self, epoch: int) -> str:
        """Get the most recent commit hash in an epoch.

        Returns an empty string if the repository has no commits.
        """
        if self.is_empty_repo(epoch):
            return ''
        gitdir = self.get_gitdir(epoch)
        branch = self._get_default_branch(gitdir)
        gitargs = ['rev-list', '-n', '1', branch]
        retcode, output, error = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f'Git rev-list failed (exit {retcode}): {error.decode()}')
        top_commit = output.decode().strip()
        return top_commit

    def get_first_commit(self, epoch: int) -> str:
        """Get the first (oldest) commit hash in an epoch.

        Returns an empty string if the repository has no commits.
        """
        if self.is_empty_repo(epoch):
            return ''
        gitdir = self.get_gitdir(epoch)
        branch = self._get_default_branch(gitdir)
        gitargs = ['rev-list', '--max-parents=0', branch]
        retcode, output, error = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f'Git rev-list failed (exit {retcode}): {error.decode()}')
        first_commit = output.decode().strip()
        return first_commit

    def feed_lock(self) -> None:
        """Acquire exclusive lock on feed to prevent concurrent access.

        Raises:
            FeedLockedError: Another process holds the lock.
        """
        # Grab an exclusive posix lock to make sure that we're not running the
        # same delivery in multiple processes at the same time.
        global LOCKED_FEEDS
        lock_file_path = self._get_state_file_path(delivery_name=None, suffix='lock')
        lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        # Held open until feed_unlock(): closing it would drop the lock
        lockfh = lock_file_path.open('w')
        try:
            lockf(lockfh, LOCK_EX | LOCK_NB)
        except BlockingIOError as e:
            lockfh.close()
            raise FeedLockedError(
                f"Another kgl process is using feed '{self.feed_key}' ({self.feed_dir}). Try again when it is done."
            ) from e
        except Exception:
            lockfh.close()
            raise
        logger.debug("Acquired lock for feed '%s'.", self.feed_dir)
        LOCKED_FEEDS[str(self.feed_dir)] = lockfh

    def feed_unlock(self) -> None:
        """Release lock on feed after operations complete."""
        global LOCKED_FEEDS
        key = str(self.feed_dir)
        try:
            lockfh = LOCKED_FEEDS[key]
            lockf(lockfh, LOCK_UN)
            lockfh.close()
            del LOCKED_FEEDS[key]
            self._empty_repo_cache.clear()
            logger.debug("Released lock for feed '%s'.", key)
        except KeyError as e:
            raise PublicInboxError(f"Feed '{key}' is not locked.") from e

    def get_failed_commits_for_delivery(self, delivery_name: str) -> list[tuple[int, str]]:
        """Return list of (epoch, commit) tuples that previously failed delivery."""
        state_file = self._get_state_file_path(delivery_name, 'failed')
        failed = self._read_jsonl_file(state_file)
        return [(int(entry[0]), str(entry[1])) for entry in failed]

    def mark_successful_delivery(
        self,
        delivery_name: str,
        epoch: int,
        commit_hash: str,
        message: bytes | None = None,
        was_failing: bool = False,
    ) -> None:
        """Mark a commit as successfully delivered and remove from failed list if present."""
        # We've successfully delivered a message, so remove it from the
        # korgalore.{delivery_name}.failed file if it exists there.
        if was_failing:
            state_file = self._get_state_file_path(delivery_name, 'failed')
            failed = self._read_jsonl_file(state_file)
            original_len = len(failed)
            # Use list comprehension instead of O(n) remove() in loop
            failed = [e for e in failed if not (e[0] == epoch and e[1] == commit_hash)]
            if len(failed) < original_len:
                self._write_jsonl_file(state_file, failed)
                logger.debug(
                    'Marked commit %s in epoch %d as successfully delivered for delivery %s.',
                    commit_hash,
                    epoch,
                    delivery_name,
                )
            # Don't update the delivery pointer for retried commits —
            # they are older than the current pointer and overwriting
            # it would rewind the state, causing all subsequent commits
            # to be re-delivered on the next pull.
            return

        self.save_delivery_info(delivery_name, epoch, commit_hash, message=message)

    def cleanup_failed_state(self, delivery_name: str) -> None:
        # Remove the failed state file if it's empty
        state_file = self._get_state_file_path(delivery_name, 'failed')
        if not state_file.exists():
            logger.debug('No failed state file for delivery %s, nothing to clean up.', delivery_name)
            return
        failed = self.get_failed_commits_for_delivery(delivery_name)
        if not len(failed):
            state_file.unlink()
            logger.debug('Removed empty failed state file for delivery %s.', delivery_name)

    def mark_failed_delivery(self, delivery_name: str, epoch: int, commit_hash: str) -> None:
        """Record a failed delivery attempt for later retry."""
        # We've attempted to deliver a message, but it failed. Record this in the
        # korgalore.{delivery_name}.failed file.
        state_file = self._get_state_file_path(delivery_name, 'failed')
        failed = self._read_jsonl_file(state_file)
        now_dt = datetime.now(UTC)
        # Find existing entry by index (avoids O(n) remove() in loop)
        found_idx = None
        for idx, entry in enumerate(failed):
            if entry[0] == epoch and entry[1] == commit_hash:
                found_idx = idx
                break
        if found_idx is not None:
            entry = failed[found_idx]
            # Has it been longer than RETRY_FAILED_INTERVAL?
            first_failed_dt = datetime.fromisoformat(str(entry[2]))
            delta = now_dt - first_failed_dt
            if delta.total_seconds() > RETRY_FAILED_INTERVAL:
                subject = self.get_subject_at_commit(epoch, commit_hash)
                logger.warning('Delivery for %s has exceeded retry interval, will not retry.', commit_hash)
                logger.warning(' Feed: %s', self.feed_dir)
                logger.warning(' Delivery: %s', delivery_name)
                logger.warning(' Subject: %s', subject)
                # Move to rejected file
                rejected_file = self._get_state_file_path(delivery_name, 'rejected')
                rejected_entry = list(entry) + [now_dt.isoformat()]
                self._append_to_jsonl_file(rejected_file, tuple(rejected_entry))
                # Remove from failed list using pop (O(n) shift but only once, not inside loop)
                failed.pop(found_idx)
                self._write_jsonl_file(state_file, failed)
                return
            # Increment retry count - update entry in place
            retry_count = int(entry[3]) + 1
            failed[found_idx] = (epoch, commit_hash, entry[2], retry_count)
            self._write_jsonl_file(state_file, failed)
            return
        # New entry
        new_entry = (epoch, commit_hash, now_dt.isoformat(), 1)
        self._append_to_jsonl_file(state_file, new_entry)

    def save_delivery_info(
        self,
        delivery_name: str,
        epoch: int | None = None,
        latest_commit: str | None = None,
        message: bytes | EmailMessage | None = None,
        digest_sent: datetime | None = None,
    ) -> None:
        """Save delivery progress state to disk.

        For digest deliveries, digest_sent records when the digest was
        sent. It is written together with the pointer, so the two can never
        disagree.
        """
        if epoch is None:
            epoch = self.get_highest_epoch()

        if digest_sent is not None and not latest_commit and self.is_empty_repo(epoch):
            # An empty feed has nothing to point at yet. The next digest
            # notices the missing pointer and collects by date instead.
            self.save_delivery_entry(delivery_name, None, digest_sent=digest_sent)
            return

        if not latest_commit:
            latest_commit = self.get_top_commit(epoch)

        entry = self.make_delivery_entry(epoch, latest_commit, message)
        self.save_delivery_entry(delivery_name, {'epoch': epoch, 'entry': entry}, digest_sent=digest_sent)

    def make_delivery_entry(
        self,
        epoch: int,
        commit: str,
        message: bytes | EmailMessage | None = None,
    ) -> dict[str, str]:
        """Build the state entry that points a delivery at this commit."""
        gitdir = self.get_gitdir(epoch)
        gitargs = ['show', '-s', '--format=%ci', commit]
        retcode, output, error = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f'Git show failed (exit {retcode}): {error.decode()}')
        commit_date = output.decode()
        # Seed both fields, because neither branch below is guaranteed to set
        # them: a commit whose 'm' file exists but is empty is not a no-op, so
        # get_message_at_commit() returns b'' and the parsing block is skipped
        # entirely. The state file needs both keys regardless.
        subject = '(no subject)'
        msgid = '(no message-id)'
        if not message and self.is_noop_commit(epoch, commit):
            subject = '(noop)'
            msgid = '(noop)'
        elif not message:
            message = self.get_message_at_commit(epoch, commit)

        if message:
            if isinstance(message, bytes):
                msg = parse_message(message)
            else:
                msg = message
            subject = msg_get_subject(msg) or '(no subject)'
            msgid = msg.get('Message-ID', '(no message-id)')

        return {
            'last': commit,
            'subject': subject,
            'msgid': msgid,
            'commit_date': commit_date,
        }

    def save_delivery_entry(
        self,
        delivery_name: str,
        pointer: dict[str, Any] | None,
        digest_sent: datetime | None = None,
    ) -> None:
        """Write a pointer made by make_delivery_entry() to the state file.

        pointer is {'epoch': N, 'entry': {...}}, or None to leave the
        pointer as it is. This reads no git data, so a digest job can save
        the pointer it collected long after the feed has moved on.
        """
        state_file = self._get_state_file_path(delivery_name, 'info')
        state_info = self._read_delivery_info(delivery_name) or {'epochs': {}}
        if pointer is not None:
            state_info.setdefault('epochs', {})[str(pointer['epoch'])] = pointer['entry']
        if digest_sent is not None:
            state_info['digest'] = {'last_sent': digest_sent.isoformat()}

        self._atomic_write(state_file, json.dumps(state_info, indent=2))

    def get_delivery_info_for_epoch(self, delivery_name: str, epoch: int | None = None) -> dict[str, Any]:
        """Retrieve saved delivery state for a specific epoch."""
        info = self.load_delivery_info(delivery_name)
        if epoch is None:
            # This is different than self.get_highest_epoch() because we want the highest
            # epoch known to this delivery, not the feed as a whole.
            known_epochs = [int(e) for e in info.get('epochs', {})]
            epoch = max(known_epochs)
        elif str(epoch) not in info.get('epochs', {}):
            # Is it a valid epoch?
            gitdir = self.get_gitdir(epoch)
            if not gitdir.exists():
                raise StateError(f'Epoch {epoch} does not exist in feed {self.feed_dir}.')
            raise StateError(f'No delivery info found for epoch {epoch} in delivery {delivery_name}.')
        epoch_info: dict[str, Any] = info['epochs'][str(epoch)]
        return epoch_info

    def load_delivery_info(self, delivery_name: str) -> dict[str, Any]:
        """Load delivery progress state from disk."""
        state_file = self._get_state_file_path(delivery_name, 'info')
        if not state_file.exists():
            logger.debug('Initializing new state file for delivery: %s', delivery_name)
            self.save_delivery_info(delivery_name)

        with state_file.open() as gf:
            info: dict[str, Any] = json.load(gf)

        return info

    def _read_delivery_info(self, delivery_name: str) -> dict[str, Any] | None:
        """Read the delivery state file as it is, or None if there is none."""
        state_file = self._get_state_file_path(delivery_name, 'info')
        if not state_file.exists():
            return None
        with state_file.open() as gf:
            info: dict[str, Any] = json.load(gf)
        return info

    def get_digest_job_dir(self, delivery_name: str) -> Path:
        """Where a digest delivery keeps a digest until it is sent."""
        return self._get_state_file_path(delivery_name, 'digest-job')

    def find_history_gap(self, delivery_name: str, commits: list[tuple[int, str]]) -> datetime | None:
        """Find out if commits between the delivery pointer and HEAD are missing.

        Lore clones are shallow, and every fetch moves the cut to one week
        back. When the pointer is older than that, the walk from HEAD stops
        at the cut, before it gets to the pointer. git lists the cut
        commits in the "shallow" file, and the raw commit object still
        names its real parent. If a cut commit is in the range and its
        parent is not the pointer, the messages in between are missing.

        Returns the commit date of the first commit after the gap, or None
        when nothing is missing.
        """
        info = self._read_delivery_info(delivery_name)
        if not info or not info.get('epochs'):
            return None
        epoch = max(int(e) for e in info['epochs'])
        pointer = info['epochs'][str(epoch)]['last']
        gitdir = self.get_gitdir(epoch)
        shallow_file = gitdir / 'shallow'
        if not shallow_file.exists():
            return None
        cuts = set(shallow_file.read_text().split())
        for commit_epoch, commit in commits:
            if commit_epoch != epoch or commit not in cuts:
                continue
            retcode, output, error = run_git_command(str(gitdir), ['cat-file', 'commit', commit])
            if retcode != 0:
                raise GitError(f'Git cat-file failed (exit {retcode}): {error.decode()}')
            header = output.decode(errors='replace').split('\n\n', 1)[0]
            parents = [line[7:] for line in header.splitlines() if line.startswith('parent ')]
            if pointer in parents:
                continue
            retcode, output, error = run_git_command(str(gitdir), ['show', '-s', '--format=%cI', commit])
            if retcode != 0:
                raise GitError(f'Git show failed (exit {retcode}): {error.decode()}')
            return datetime.fromisoformat(output.decode().strip())
        return None

    def has_delivery_pointer(self, delivery_name: str) -> bool:
        """True when the delivery has saved how far it got in the feed."""
        info = self._read_delivery_info(delivery_name)
        return bool(info and info.get('epochs'))

    def load_digest_sent(self, delivery_name: str) -> datetime | None:
        """Return when the last digest of a delivery was sent, or None if never."""
        info = self._read_delivery_info(delivery_name)
        if info is None:
            return None
        state_file = self._get_state_file_path(delivery_name, 'info')
        value = info.get('digest', {}).get('last_sent')
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError as e:
            raise StateError(f'Bad digest last_sent {value!r} in {state_file}') from e

    def get_digest_history_start(self, delivery_name: str) -> datetime | None:
        """How far back a digest delivery needs the feed history.

        The next digest starts at the pointer, so its commit must stay in
        the local history, with its parent. Without a pointer, the next
        digest collects by commit date from last_sent. We keep one extra
        day as a margin, so the pointer is never right at the shallow cut.

        Returns None when the delivery has no digest state yet.
        """
        info = self._read_delivery_info(delivery_name)
        if not info:
            return None
        starts: list[datetime] = []
        last_sent = self.load_digest_sent(delivery_name)
        if last_sent is not None:
            starts.append(last_sent)
        epochs = info.get('epochs', {})
        if epochs:
            pointer = epochs[str(max(int(e) for e in epochs))]
            if pointer.get('commit_date'):
                try:
                    starts.append(datetime.strptime(pointer['commit_date'], '%Y-%m-%d %H:%M:%S %z'))
                except ValueError as e:
                    raise StateError(f'Bad commit_date for delivery {delivery_name}: {e}') from e
        if not starts:
            return None
        return min(starts) - timedelta(days=1)

    def feed_updated(self, epoch: int | None = None) -> bool:
        """Check if feed has new commits since last recorded state."""
        try:
            feed_state = self.load_feed_state()
        except StateError:
            # We return True because there is no state, so we treat it as having been updated
            return True

        epochs = feed_state.get('epochs', {})
        if epoch is not None:
            if str(epoch) not in epochs:
                # No state for this epoch, so treat as updated
                return True
            known_top_commit: str | None = epochs[str(epoch)].get('latest_commit')
            current_top_commit = self.get_top_commit(epoch)

            return known_top_commit != current_top_commit

        # We go by epoch and return True whenever we find a changed epoch
        for epoch_key in epochs:
            known_top_commit = epochs[epoch_key].get('latest_commit')
            try:
                current_top_commit = self.get_top_commit(int(epoch_key))
            except GitError:
                logger.warning('Could not get top commit for epoch %s, skipping.', epoch_key)
                continue
            if known_top_commit != current_top_commit:
                return True

        return False

    def load_feed_state(self) -> dict[str, Any]:
        """Load feed-level state (epochs and metadata) from disk."""
        state_file = self._get_state_file_path(delivery_name=None, suffix='feed')

        if not state_file.exists():
            self._perform_legacy_migration()
            if not state_file.exists():
                raise StateError(f'Feed state not found: {state_file}')

        return self._read_feed_state(state_file)

    def _read_feed_state(self, state_file: Path) -> dict[str, Any]:
        """Read a feed state file that exists.

        Raises:
            StateCorruptError: If the file is not a usable feed state.
        """
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise StateCorruptError(f'Feed state {state_file} is corrupt: {e}') from e
        # save_feed_state() always records an epoch, and every reader
        # relies on having at least one
        if not isinstance(state, dict) or not isinstance(state.get('epochs'), dict) or not state['epochs']:
            raise StateCorruptError(f'Feed state {state_file} is corrupt: no epochs recorded')
        return state

    def rebuild_feed_state(self) -> dict[str, Any]:
        """Replace a corrupt feed state with one made from the local epochs.

        Deliveries keep their state in their own files, so they carry on
        from where they were.
        """
        self.save_feed_state(epoch=self.get_highest_epoch())
        return self.load_feed_state()

    def save_feed_state(self, epoch: int | None = None, latest_commit: str | None = None, success: bool = True) -> None:
        """Save feed-level state to disk."""
        state_file = self._get_state_file_path(delivery_name=None, suffix='feed')

        # Get latest commit if not provided
        if epoch is None:
            epoch = self.get_highest_epoch()
        if latest_commit is None:
            latest_commit = self.get_top_commit(epoch)

        state: dict[str, Any] | None = None
        if state_file.exists():
            # Not load_feed_state(), because its legacy migration calls
            # save_feed_state(). A corrupt file has nothing worth keeping.
            with contextlib.suppress(StateCorruptError):
                state = self._read_feed_state(state_file)
        if state is None:
            state = {
                'epochs': {},
                'extra_data': {
                    'feed_type': self.feed_type,
                    'feed_url': self.feed_url,
                },
            }

        state['epochs'][str(epoch)] = {
            'last_update': datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S %z'),
            'update_successful': success,
            'latest_commit': latest_commit,
        }

        self._atomic_write(state_file, json.dumps(state, indent=2))
