import json
import logging
import os
import tempfile

from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import EmailPolicy
from email import charset
from pathlib import Path
from korgalore import run_git_command, PublicInboxError, GitError, StateError
from fcntl import lockf, LOCK_EX, LOCK_UN, LOCK_NB

from typing import Any, Dict, List, Optional, Tuple, Union

from datetime import datetime, timezone
from dateutil import tz

charset.add_charset('utf-8', None)
logger = logging.getLogger('korgalore')

# We use this to cache commit messages to avoid reparsing them multiple times
# during delivery just to get the subject
COMMIT_SUBJECT_CACHE: Dict[str, str] = dict()
LOCKED_FEEDS: Dict[str, Any] = dict()
# We retry failed deliveries for 5 days and then give up
RETRY_FAILED_INTERVAL = 5 * 24 * 60 * 60  # 5 days in seconds

class PIFeed:
    emlpolicy: EmailPolicy = EmailPolicy(utf8=True, cte_type='8bit', max_line_length=None,
                                         message_factory=EmailMessage)

    def __init__(self, feed_key: str, feed_dir: Path) -> None:
        self._branch_cache: Dict[str, str] = dict()
        self.feed_key: str = feed_key
        self.feed_dir: Path = feed_dir
        self.feed_type: str = 'unknown'
        self.feed_url: str = ''

    def _read_jsonl_file(self, filepath: Path) -> List[Tuple[Union[int, str], ...]]:
        """Read a JSONL state file and return a list of tuples."""
        results: List[Tuple[Union[int, str], ...]] = list()
        if not filepath.exists():
            return results
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                results.append(tuple(obj))
        return results

    def _write_jsonl_file(self, filepath: Path, data: List[Tuple[Union[int, str], ...]]) -> None:
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
        fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix='.tmp_')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(content)
            os.replace(tmp_path, filepath)
        except Exception:
            # Clean up temp file on error
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get_gitdir(self, epoch: int) -> Path:
        return self.feed_dir / 'git' / f'{epoch}.git'

    def _append_to_jsonl_file(self, filepath: Path, obj: Tuple[Union[int, str], ...]) -> None:
        """Append a tuple as a JSONL entry to a state file."""
        with open(filepath, 'a') as f:
            line = json.dumps(obj)
            f.write(line + '\n' )

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
        # Check if there is a legacy korgalore.info file
        highest_epoch = self.get_highest_epoch()
        legacy_info_path = self.feed_dir / 'git' / f'{highest_epoch}.git' / 'korgalore.info'
        if not legacy_info_path.exists():
            return  # No legacy file, nothing to do

        # In the 0.1 version, the directory was named the same as the source name, so we
        # assume delivery_name will be korgalore.dirname.info
        delivery_name = self.feed_dir.name

        # Read the legacy info
        with open(legacy_info_path, 'r') as f:
            lgi = json.load(f)

        latest_commit = lgi.get('last')

        self.save_delivery_info(
            delivery_name=delivery_name,
            epoch=highest_epoch,
            latest_commit=latest_commit
        )

        self.save_feed_state(
            epoch=highest_epoch,
            latest_commit=latest_commit,
            success=True,
        )


    def _get_state_file_path(self, delivery_name: Optional[str] = None, suffix: str = 'info') -> Path:
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
        retcode, output = run_git_command(gitdir_str, gitargs)
        if retcode == 0:
            # Output is like 'refs/remotes/origin/main' - extract the branch name
            branch_name = output.decode().strip().split('/')[-1]
            self._branch_cache[gitdir_str] = branch_name
            return branch_name

        # Fallback: try to find the first branch
        gitargs = ['branch', '--format=%(refname:short)']
        retcode, output = run_git_command(gitdir_str, gitargs)
        if retcode == 0 and output:
            # Return the first branch listed
            branch_name = output.decode().strip().split('\n')[0]
            self._branch_cache[gitdir_str] = branch_name
            return branch_name

        # Last fallback: assume 'master'
        logger.warning(f"Could not detect default branch in {gitdir}, falling back to 'master'")
        branch_name = 'master'
        self._branch_cache[gitdir_str] = branch_name
        return branch_name

    def find_epochs(self) -> List[int]:
        epochs_dir = self.feed_dir / 'git'
        # List this directory for existing epochs
        existing_epochs: List[int] = list()
        for item in epochs_dir.iterdir():
            if item.is_dir() and item.name.endswith('.git'):
                epoch_str = item.name.replace('.git', '')
                try:
                    epoch_num = int(epoch_str)
                    existing_epochs.append(epoch_num)
                except ValueError:
                    logger.debug(f"Invalid epoch directory: {item.name}")
        if not existing_epochs:
            raise PublicInboxError(f"No existing epochs found in {epochs_dir}.")
        return sorted(existing_epochs)

    def get_highest_epoch(self) -> int:
        epochs = self.find_epochs()
        return max(epochs)

    def get_all_commits_in_epoch(self, epoch: int) -> List[str]:
        gitdir = self.get_gitdir(epoch)
        branch = self._get_default_branch(gitdir)
        gitargs = ['rev-list', '--reverse', branch]
        retcode, output = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f"Git rev-list failed: {output.decode()}")
        if len(output):
            commits = output.decode().splitlines()
        else:
            commits = []
        return commits

    def recover_after_rebase(self, delivery_name: str, epoch: int) -> str:
        # Load delivery info to find last processed commit
        delivery_info = self.load_delivery_info(delivery_name)
        if str(epoch) in delivery_info.get('epochs', {}):
            info = delivery_info['epochs'][str(epoch)]
        else:
            raise StateError(f"No delivery info found for epoch {epoch} in delivery {delivery_name}.")

        # Get the commit's date and parse it into datetime
        # The string is ISO with tzinfo: "2025-11-04 20:47:21 +0000"
        commit_date_str = info.get('commit_date')
        if not commit_date_str:
            raise StateError(f"No commit_date found in the state file for {delivery_name}.")
        commit_date = datetime.strptime(commit_date_str, '%Y-%m-%d %H:%M:%S %z')
        logger.debug(f"Last processed commit date: {commit_date.isoformat()}")
        # Try to find the new hash of this commit in the log by matching the subject and
        # message-id.
        gitdir = self.get_gitdir(epoch)
        gitargs = ['rev-list', '--reverse', '--since-as-filter', commit_date_str, 'HEAD']
        retcode, output = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            # Not sure what happened here, just give up and return the latest commit
            logger.warning("Could not run rev-list to recover after rebase, returning latest commit.")
            latest_commit = self.get_top_commit(epoch)
            return latest_commit

        possible_commits = output.decode().splitlines()
        if not possible_commits:
            # Just record the latest info, then
            self.save_delivery_info(delivery_name, epoch)
            latest_commit = self.get_top_commit(epoch)
            return latest_commit

        last_commit = ''
        first_commit = possible_commits[0]
        for commit in possible_commits:
            raw_message = self.get_message_at_commit(epoch, commit)
            msg = self.parse_message(raw_message)
            subject = msg.get('Subject', '(no subject)')
            msgid = msg.get('Message-ID', '(no message-id)')
            if subject == info.get('subject') and msgid == info.get('msgid'):
                logger.debug(f"Found matching commit: {commit}")
                last_commit = commit
                break
        if not last_commit:
            logger.error("Could not find exact commit after rebase.")
            logger.error("Returning first possible commit after date: %s", first_commit)
            last_commit = first_commit
            raw_message = self.get_message_at_commit(epoch, last_commit)
            msg = self.parse_message(raw_message)
        else:
            logger.debug("Recovered exact matching commit after rebase: %s", last_commit)

        self.save_delivery_info(delivery_name, epoch, latest_commit=last_commit, message=msg)
        return last_commit

    def get_latest_commits_for_delivery(self, delivery_name: str) -> List[Tuple[int, str]]:
        try:
            dinfo = self.load_delivery_info(delivery_name)
        except StateError:
            # XXX: currently, assuming a brand new delivery and not some other kind of error
            logger.debug(f"No delivery info found for delivery {delivery_name}, assuming new delivery.")
            self.save_delivery_info(delivery_name)
            return list()

        # Grab the highest epoch we know about
        known_epochs = [int(e) for e in dinfo.get('epochs', {}).keys()]
        highest_known_epoch = max(known_epochs)
        logger.debug(f"Highest known epoch for delivery {delivery_name}: {highest_known_epoch}")
        since_commit = dinfo['epochs'][str(highest_known_epoch)]['last']

        # is this still a valid commit?
        gitdir = self.get_gitdir(highest_known_epoch)
        gitargs = ['cat-file', '-e', f'{since_commit}^']
        retcode, output = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            # The commit is not valid anymore, so try to find the latest commit by other
            # means.
            logger.debug(f"Since commit {since_commit} not found, trying to recover after rebase.")
            since_commit = self.recover_after_rebase(delivery_name, highest_known_epoch)
        gitargs = ['rev-list', '--reverse', '--ancestry-path', f'{since_commit}..HEAD']
        retcode, output = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f"Git rev-list failed: {output.decode()}")
        if len(output):
            new_commits = [(highest_known_epoch, x) for x in output.decode().splitlines()]
        else:
            new_commits = []

        # Now check if the underlying repo has rolled over to the new epoch
        highest_found_epoch = self.get_highest_epoch()
        if highest_found_epoch > highest_known_epoch:
            logger.debug(f"New epoch detected: {highest_found_epoch}")
            # Get all commits in this epoch
            commits = self.get_all_commits_in_epoch(highest_found_epoch)
            if commits:
                new_commits += [(highest_found_epoch, x) for x in commits]

        return new_commits

    def get_message_at_commit(self, epoch: int, commitish: str) -> bytes:
        gitdir = self.get_gitdir(epoch)
        gitargs = ['show', f'{commitish}:m']
        retcode, output = run_git_command(str(gitdir), gitargs)
        if retcode == 128:
            raise StateError(f"Commit {commitish} does not have a message file.")
        if retcode != 0:
            raise GitError(f"Git show failed: {output.decode()}")
        return output

    @classmethod
    def parse_message(cls, raw_message: bytes) -> EmailMessage:
        msg: EmailMessage = BytesParser(_class=EmailMessage,
                                        policy=cls.emlpolicy).parsebytes(raw_message)  # type: ignore
        return msg

    def get_subject_at_commit(self, epoch: int, commitish: str) -> str:
        global COMMIT_SUBJECT_CACHE
        try:
            return COMMIT_SUBJECT_CACHE[commitish]
        except KeyError:
            raw_msg = self.get_message_at_commit(epoch, commitish)
            msg = self.parse_message(raw_msg)
            subject = msg.get('Subject', '(no subject)')
            COMMIT_SUBJECT_CACHE[commitish] = subject
            return subject

    def get_top_commit(self, epoch: int) -> str:
        gitdir = self.get_gitdir(epoch)
        branch = self._get_default_branch(gitdir)
        gitargs = ['rev-list', '-n', '1', branch]
        retcode, output = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f"Git rev-list failed: {output.decode()}")
        top_commit = output.decode()
        return top_commit

    def feed_lock(self) -> None:
        # Grab an exclusive posix lock to make sure that we're not running the
        # same delivery in multiple processes at the same time.
        global LOCKED_FEEDS
        lock_file_path = self._get_state_file_path(delivery_name=None, suffix='lock')
        lockfh = open(lock_file_path, 'w')
        try:
            lockf(lockfh, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            lockfh.close()
            raise PublicInboxError(f"Feed '{self.feed_dir}' is already locked by another process.")
        except Exception:
            lockfh.close()
            raise
        logger.debug("Acquired lock for feed '%s'.", self.feed_dir)
        LOCKED_FEEDS[str(self.feed_dir)] = lockfh

    def feed_unlock(self) -> None:
        global LOCKED_FEEDS
        key = str(self.feed_dir)
        try:
            lockfh = LOCKED_FEEDS[key]
            lockf(lockfh, LOCK_UN)
            lockfh.close()
            del LOCKED_FEEDS[key]
            logger.debug("Released lock for feed '%s'.", key)
        except KeyError:
            raise PublicInboxError(f"Feed '{key}' is not locked.")

    def get_failed_commits_for_delivery(self, delivery_name: str) -> List[Tuple[int, str]]:
        # Return a list of (epoch, commit_hash)
        state_file = self._get_state_file_path(delivery_name, 'failed')
        failed = self._read_jsonl_file(state_file)
        results: List[Tuple[int, str]] = list()
        for entry in failed:
            results.append((int(entry[0]), str(entry[1])))
        return results

    def mark_successful_delivery(self, delivery_name: str, epoch: int, commit_hash: str, was_failing: bool = False) -> None:
        # We've successfully delivered a message, so remove it from the
        # korgalore.{delivery_name}.failed file if it exists there.
        if was_failing:
            state_file = self._get_state_file_path(delivery_name, 'failed')
            failed = self._read_jsonl_file(state_file)
            for entry in list(failed):
                if entry[0] == epoch and entry[1] == commit_hash:
                    failed.remove(entry)
                    self._write_jsonl_file(state_file, failed)
                    logger.debug("Marked commit %s in epoch %d as successfully delivered for delivery %s.",
                                commit_hash, epoch, delivery_name)
                    break

        self.save_delivery_info(delivery_name, epoch, commit_hash)

    def cleanup_failed_state(self, delivery_name: str) -> None:
        # Remove the failed state file if it's empty
        state_file = self._get_state_file_path(delivery_name, 'failed')
        if not state_file.exists():
            logger.debug("No failed state file for delivery %s, nothing to clean up.", delivery_name)
            return
        failed = self.get_failed_commits_for_delivery(delivery_name)
        if not len(failed):
            state_file.unlink()
            logger.debug("Removed empty failed state file for delivery %s.", delivery_name)

    def mark_failed_delivery(self, delivery_name: str, epoch: int, commit_hash: str) -> None:
        # We've attempted to deliver a message, but it failed. Record this in the
        # korgalore.{delivery_name}.failed file.
        state_file = self._get_state_file_path(delivery_name, 'failed')
        failed = self._read_jsonl_file(state_file)
        now_dt = datetime.now(tz=tz.UTC)
        for entry in list(failed):
            if entry[0] == epoch and entry[1] == commit_hash:
                # Has it been longer than RETRY_FAILED_INTERVAL?
                first_failed_dt = datetime.fromisoformat(str(entry[2]))
                delta = now_dt - first_failed_dt
                if delta.total_seconds() > RETRY_FAILED_INTERVAL:
                    subject = self.get_subject_at_commit(epoch, commit_hash)
                    logger.warning("Delivery for %s has exceeded retry interval, will not retry.", commit_hash)
                    logger.warning(" Feed: %s", self.feed_dir)
                    logger.warning(" Delivery: %s", delivery_name)
                    logger.warning(" Subject: %s", subject)
                    # Move to rejected file
                    rejected_file = self._get_state_file_path(delivery_name, 'rejected')
                    rejected_entry = list(entry) + [now_dt.isoformat()]
                    self._append_to_jsonl_file(rejected_file, tuple(rejected_entry))
                    # Remove from failed list
                    failed.remove(entry)
                    self._write_jsonl_file(state_file, failed)
                    return
                # Increment retry count
                retry_count = int(entry[3]) + 1
                failed.remove(entry)
                new_entry = (epoch, commit_hash, entry[2], retry_count)
                failed.append(new_entry)
                self._write_jsonl_file(state_file, failed)
                return
        # New entry
        new_entry = (epoch, commit_hash, now_dt.isoformat(), 1)
        self._append_to_jsonl_file(state_file, new_entry)

    def save_delivery_info(self, delivery_name: str, epoch: Optional[int] = None,
                             latest_commit: Optional[str] = None,
                             message: Optional[Union[bytes, EmailMessage]] = None) -> None:
        if not epoch:
            epoch = self.get_highest_epoch()

        if not latest_commit:
            latest_commit = self.get_top_commit(epoch)

        # Get the commit date
        gitdir = self.get_gitdir(epoch)
        gitargs = ['show', '-s', '--format=%ci', latest_commit]
        retcode, output = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f"Git show failed: {output.decode()}")
        commit_date = output.decode()
        # TODO: latest_commit may not have a "m" file in it if it's a deletion
        if not message:
            message = self.get_message_at_commit(epoch, latest_commit)

        if isinstance(message, bytes):
            msg = self.parse_message(message)
        else:
            msg = message
        subject = msg.get('Subject', '(no subject)')
        msgid = msg.get('Message-ID', '(no message-id)')

        state_file = self._get_state_file_path(delivery_name, 'info')
        if state_file.exists():
            state_info = self.load_delivery_info(delivery_name)
        else:
            state_info = {
                'epochs': {}
            }
        state_info['epochs'][str(epoch)] = {
            'last': latest_commit,
            'subject': subject,
            'msgid': msgid,
            'commit_date': commit_date,
        }

        self._atomic_write(state_file, json.dumps(state_info, indent=2))

    def get_delivery_info_for_epoch(self, delivery_name: str, epoch: Optional[int] = None) -> Dict[str, Any]:
        info = self.load_delivery_info(delivery_name)
        if epoch is None:
            # This is different than self.get_highest_epoch() because we want the highest
            # epoch known to this delivery, not the feed as a whole.
            known_epochs = [int(e) for e in info.get('epochs', {}).keys()]
            epoch = max(known_epochs)
        elif str(epoch) not in info.get('epochs', {}):
            # Is it a valid epoch?
            gitdir = self.get_gitdir(epoch)
            if not gitdir.exists():
                raise StateError(f"Epoch {epoch} does not exist in feed {self.feed_dir}.")
            raise StateError(f"No delivery info found for epoch {epoch} in delivery {delivery_name}.")
        epoch_info = info['epochs'][str(epoch)] # type: Dict[str, Any]
        return epoch_info

    def load_delivery_info(self, delivery_name: str) -> Dict[str, Any]:
        state_file = self._get_state_file_path(delivery_name, 'info')
        if not state_file.exists():
            logger.debug('Initializing new state file for delivery: %s', delivery_name)
            self.save_delivery_info(delivery_name)

        with open(state_file, 'r') as gf:
            info = json.load(gf)  # type: Dict[str, Any]

        return info

    def feed_updated(self, epoch: Optional[int] = None) -> bool:
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
            known_top_commit = epochs[str(epoch)].get('latest_commit')
            current_top_commit = self.get_top_commit(epoch)

            if known_top_commit != current_top_commit:
                return True
            return False

        # We go by epoch and return True whenever we find a changed epoch
        for epoch in epochs.keys():
            known_top_commit = epochs[epoch].get('latest_commit')
            try:
                current_top_commit = self.get_top_commit(epoch)
            except GitError:
                logger.warning('Could not get top commit for epoch %s, skipping.', epoch)
                continue
            if known_top_commit != current_top_commit:
                return True

        return False

    def load_feed_state(self) -> Dict[str, Any]:
        state_file = self._get_state_file_path(delivery_name=None, suffix='feed')

        if not state_file.exists():
            self._perform_legacy_migration()
            if not state_file.exists():
                raise StateError(f"Feed state not found: {state_file}")

        with open(state_file, 'r') as f:
            result = json.load(f)
            assert isinstance(result, dict)
            return result

    def save_feed_state(self, epoch: Optional[int] = None, latest_commit: Optional[str] = None, success: bool = True) -> None:
        state_file = self._get_state_file_path(delivery_name=None, suffix='feed')

        # Get latest commit if not provided
        if epoch is None:
            epoch = self.get_highest_epoch()
        if latest_commit is None:
            latest_commit = self.get_top_commit(epoch)

        if state_file.exists():
            with open(state_file, 'r') as f:
                state = json.load(f)
        else:
            state = {
                'epochs': {},
                'extra_data': {
                    'feed_type': self.feed_type,
                    'feed_url': self.feed_url,
                }
            }

        state['epochs'][str(epoch)] = {
            'last_update': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %z'),
            'update_successful': success,
            'latest_commit': latest_commit,
        }

        self._atomic_write(state_file, json.dumps(state, indent=2))

    @staticmethod
    def mailsplit_bytes(bmbox: bytes) -> List[bytes]:
        import os
        import tempfile
        msgs: List[bytes] = list()
        # Use a safe temporary directory for mailsplit output
        with tempfile.TemporaryDirectory(suffix='-mailsplit') as tfd:
            logger.debug('Mailsplitting the mbox into %s', tfd)
            args = ['mailsplit', '--mboxrd', '-o%s' % tfd]
            ecode, out = run_git_command(None, args, stdin=bmbox)
            if ecode > 0:
                logger.critical('Unable to parse mbox received from the server')
                return msgs
            # Read in the files
            for msg in os.listdir(tfd):
                with open(os.path.join(tfd, msg), 'rb') as fh:
                    msgs.append(fh.read())
            return msgs