import json
import logging

from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import EmailPolicy
from email import charset
from pathlib import Path
from korgalore import PublicInboxError, GitError, StateError

from typing import Any, Dict, List, Optional, Tuple, Union

from datetime import datetime, timezone

charset.add_charset('utf-8', None)
logger = logging.getLogger('korgalore')

class PIService:
    GITCMD: str = "git"
    emlpolicy: EmailPolicy = EmailPolicy(utf8=True, cte_type='8bit', max_line_length=None,
                                         message_factory=EmailMessage)

    def __init__(self) -> None:
        self._branch_cache: Dict[str, str] = {}

    def _get_state_file_path(self, gitdir: Path, delivery_name: Optional[str], suffix: str) -> Path:
        """Get the path to a state file, supporting per-delivery naming.

        Args:
            gitdir: Git directory path
            delivery_name: Name of the delivery (optional for backward compatibility)
            suffix: File suffix (e.g., 'info')

        Returns:
            Path to the state file
        """
        if delivery_name:
            return gitdir / f'korgalore.{delivery_name}.{suffix}'
        else:
            return gitdir / f'korgalore.{suffix}'

    def _migrate_legacy_info_file(self, gitdir: Path, delivery_name: str) -> bool:
        """Migrate a legacy korgalore.info file to the new per-delivery format.

        Args:
            gitdir: Git directory path
            delivery_name: Name of the delivery

        Returns:
            True if migration occurred, False otherwise
        """
        legacy_path = gitdir / 'korgalore.info'
        new_path = gitdir / f'korgalore.{delivery_name}.info'

        # If new file exists, no migration needed
        if new_path.exists():
            return False

        # If legacy file exists, migrate it
        if legacy_path.exists():
            logger.debug('Migrating legacy %s file to %s', legacy_path.name, new_path.name)
            try:
                import shutil
                shutil.copy2(legacy_path, new_path)

                # Rename legacy file to prevent duplicate migrations
                archived_path = gitdir / 'korgalore.info.pre-migration'
                if not archived_path.exists():
                    legacy_path.rename(archived_path)
                    logger.info('Migrated %s to per-delivery format: %s (legacy file renamed to %s)',
                               legacy_path.name, new_path.name, archived_path.name)
                else:
                    logger.info('Migrated %s to per-delivery format: %s (archive already exists, legacy file kept)',
                               legacy_path.name, new_path.name)

                return True
            except Exception as e:
                logger.warning('Failed to migrate %s: %s', legacy_path.name, str(e))
                return False

        return False

    def get_default_branch(self, gitdir: Path) -> str:
        """Detect the default branch name in the repository."""
        gitdir_str = str(gitdir)

        # Check cache first
        if gitdir_str in self._branch_cache:
            return self._branch_cache[gitdir_str]

        # Try to get the symbolic ref for HEAD
        gitargs = ['symbolic-ref', '-q', 'HEAD']
        retcode, output = self.run_git_command(gitdir_str, gitargs)
        if retcode == 0:
            # Output is like 'refs/remotes/origin/main' - extract the branch name
            branch_name = output.decode().strip().split('/')[-1]
            self._branch_cache[gitdir_str] = branch_name
            return branch_name

        # Fallback: try to find the first branch
        gitargs = ['branch', '--format=%(refname:short)']
        retcode, output = self.run_git_command(gitdir_str, gitargs)
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

    def run_git_command(self, topdir: Optional[str], args: List[str],
                        stdin: Optional[bytes] = None) -> Tuple[int, bytes]:
        """Run a git command in the specified topdir and return (returncode, output)."""
        import subprocess

        cmd = [self.GITCMD]
        if topdir:
            cmd += ['-C', topdir]
        cmd += args
        logger.debug('Running git command: %s', ' '.join(cmd))

        try:
            result = subprocess.run(cmd, capture_output=True, input=stdin)
        except FileNotFoundError:
            raise GitError(f"Git command '{self.GITCMD}' not found. Is it installed?")
        return result.returncode, result.stdout.strip()

    def find_epochs(self, topdir: Path) -> List[int]:
        epochs_dir = topdir / 'git'
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

    def get_all_commits_in_epoch(self, gitdir: Path) -> List[str]:
        branch = self.get_default_branch(gitdir)
        gitargs = ['rev-list', '--reverse', branch]
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f"Git rev-list failed: {output.decode()}")
        if len(output):
            commits = output.decode().splitlines()
        else:
            commits = []
        return commits

    def recover_after_rebase(self, tgt_dir: Path, delivery_name: Optional[str] = None) -> str:
        """Recover after a rebase by finding the matching commit.

        Args:
            tgt_dir: Git directory path
            delivery_name: Name of the delivery (for per-delivery state files)

        Returns:
            The recovered commit hash
        """
        # Load korgalore.info to find last processed commit
        info = self.load_korgalore_info(tgt_dir, delivery_name)
        # Get the commit's date and parse it into datetime
        # The string is ISO with tzinfo: "2025-11-04 20:47:21 +0000"
        commit_date_str = info.get('commit_date')
        if not commit_date_str:
            raise StateError(f"No commit_date found in korgalore.info in {tgt_dir}.")
        commit_date = datetime.strptime(commit_date_str, '%Y-%m-%d %H:%M:%S %z')
        logger.debug(f"Last processed commit date: {commit_date.isoformat()}")
        # Try to find the new hash of this commit in the log by matching the subject and
        # message-id.
        branch = self.get_default_branch(tgt_dir)
        gitargs = ['rev-list', '--reverse', '--since-as-filter', commit_date_str, branch]
        retcode, output = self.run_git_command(str(tgt_dir), gitargs)
        if retcode != 0:
            # Not sure what happened here, just give up and return the latest commit
            logger.warning("Could not run rev-list to recover after rebase, returning latest commit.")
            latest_commit = self.get_top_commit(tgt_dir)
            return latest_commit

        possible_commits = output.decode().splitlines()
        if not possible_commits:
            # Just record the latest info, then
            self.update_korgalore_info(gitdir=tgt_dir, delivery_name=delivery_name)
            latest_commit = self.get_top_commit(tgt_dir)
            return latest_commit

        last_commit = ''
        first_commit = possible_commits[0]
        for commit in possible_commits:
            raw_message = self.get_message_at_commit(tgt_dir, commit)
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
            raw_message = self.get_message_at_commit(tgt_dir, last_commit)
            msg = self.parse_message(raw_message)
        else:
            logger.debug("Recovered exact matching commit after rebase: %s", last_commit)

        self.update_korgalore_info(gitdir=tgt_dir, latest_commit=last_commit, message=msg, delivery_name=delivery_name)
        return last_commit

    def get_latest_commits_in_epoch(self, gitdir: Path,
                                    since_commit: Optional[str] = None,
                                    delivery_name: Optional[str] = None) -> List[str]:
        """Get the latest commits in an epoch.

        Args:
            gitdir: Git directory path
            since_commit: Commit to start from (optional, will load from info if not provided)
            delivery_name: Name of the delivery (for per-delivery state files)

        Returns:
            List of new commit hashes
        """
        # How many new commits since our latest_commit
        if not since_commit:
            try:
                info = self.load_korgalore_info(gitdir, delivery_name)
            except StateError:
                raise StateError(f"korgalore.info not found in {gitdir}. Run init_feed() first.")
            since_commit = info.get('last')
        # is this still a valid commit?
        gitargs = ['cat-file', '-e', f'{since_commit}^']
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            # The commit is not valid anymore, so try to find the latest commit by other
            # means.
            logger.debug(f"Since commit {since_commit} not found, trying to recover after rebase.")
            since_commit = self.recover_after_rebase(gitdir, delivery_name)
        branch = self.get_default_branch(gitdir)
        gitargs = ['rev-list', '--reverse', '--ancestry-path', f'{since_commit}..{branch}']
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f"Git rev-list failed: {output.decode()}")
        if len(output):
            new_commits = output.decode().splitlines()
        else:
            new_commits = []
        return new_commits

    def get_message_at_commit(self, pi_dir: Path, commitish: str) -> bytes:
        gitargs = ['show', f'{commitish}:m']
        retcode, output = self.run_git_command(str(pi_dir), gitargs)
        if retcode == 128:
            raise StateError(f"Commit {commitish} does not have a message file.")
        if retcode != 0:
            raise GitError(f"Git show failed: {output.decode()}")
        return output

    def parse_message(self, raw_message: bytes) -> EmailMessage:
        """Parse a raw email message into an EmailMessage object."""
        msg: EmailMessage = BytesParser(_class=EmailMessage,
                                        policy=self.emlpolicy).parsebytes(raw_message)  # type: ignore
        return msg

    def get_top_commit(self, gitdir: Path) -> str:
        branch = self.get_default_branch(gitdir)
        gitargs = ['rev-list', '-n', '1', branch]
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f"Git rev-list failed: {output.decode()}")
        top_commit = output.decode()
        return top_commit

    def update_korgalore_info(self, gitdir: Path,
                              latest_commit: Optional[str] = None,
                              message: Optional[Union[bytes, EmailMessage]] = None,
                              delivery_name: Optional[str] = None) -> None:
        """Update korgalore.info file with latest commit information.

        Args:
            gitdir: Git directory path
            latest_commit: Latest commit hash (optional, will get top commit if not provided)
            message: Email message (optional, will fetch from commit if not provided)
            delivery_name: Name of the delivery (for per-delivery state files)
        """
        if not latest_commit:
            latest_commit = self.get_top_commit(gitdir)

        # Get the commit date
        gitargs = ['show', '-s', '--format=%ci', latest_commit]
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f"Git show failed: {output.decode()}")
        commit_date = output.decode()
        # TODO: latest_commit may not have a "m" file in it if it's a deletion
        korgalore_file = self._get_state_file_path(gitdir, delivery_name, 'info')
        if not message:
            message = self.get_message_at_commit(gitdir, latest_commit)

        if isinstance(message, bytes):
            msg = self.parse_message(message)
        else:
            msg = message
        subject = msg.get('Subject', '(no subject)')
        msgid = msg.get('Message-ID', '(no message-id)')
        with open(korgalore_file, 'w') as gf:
            json.dump({
                'last': latest_commit,
                'subject': subject,
                'msgid': msgid,
                'commit_date': commit_date,
            }, gf, indent=2)

    def load_korgalore_info(self, gitdir: Path, delivery_name: Optional[str] = None) -> Dict[str, Any]:
        """Load korgalore.info file.

        Args:
            gitdir: Git directory path
            delivery_name: Name of the delivery (for per-delivery state files)

        Returns:
            Dict containing state information
        """
        # Try to migrate legacy file if delivery_name is provided
        if delivery_name:
            self._migrate_legacy_info_file(gitdir, delivery_name)

        korgalore_file = self._get_state_file_path(gitdir, delivery_name, 'info')
        if not korgalore_file.exists():
            # For new deliveries on existing feeds, initialize at current HEAD
            # instead of failing. This allows adding new deliveries without errors.
            if delivery_name:
                logger.info('Initializing new delivery state file: %s', korgalore_file.name)
                self.update_korgalore_info(gitdir, delivery_name=delivery_name)
            else:
                raise StateError(
                    f"korgalore.info not found in {gitdir}. Run init_feed() first."
                )

        with open(korgalore_file, 'r') as gf:
            info = json.load(gf)  # type: Dict[str, Any]

        return info

    def load_feed_state(self, feed_dir: Path) -> Dict[str, Any]:
        """Load korgalore.feed state file.

        Args:
            feed_dir: Feed directory path

        Returns:
            Dict containing feed state information

        Raises:
            StateError: If feed state file doesn't exist
        """
        feed_state_file = feed_dir / 'korgalore.feed'

        if not feed_state_file.exists():
            raise StateError(f"Feed state not found: {feed_state_file}")

        with open(feed_state_file, 'r') as f:
            result = json.load(f)
            assert isinstance(result, dict)
            return result

    def save_feed_state(self, feed_dir: Path, highest_epoch: int,
                        latest_commit: Optional[str] = None,
                        success: bool = True) -> None:
        """Save korgalore.feed state file.

        Args:
            feed_dir: Feed directory path
            highest_epoch: Current highest epoch number
            latest_commit: Latest commit hash (optional, will be fetched if not provided)
            success: Whether the last update was successful
        """
        feed_state_file = feed_dir / 'korgalore.feed'

        # Get latest commit if not provided
        if latest_commit is None:
            gitdir = feed_dir / 'git' / f'{highest_epoch}.git'
            latest_commit = self.get_top_commit(gitdir)

        state = {
            'last_update': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %z'),
            'update_successful': success,
            'latest_commit': latest_commit,
            'highest_epoch': highest_epoch,
        }

        with open(feed_state_file, 'w') as f:
            json.dump(state, f, indent=2)

    def migrate_to_feed_state(self, feed_dir: Path) -> None:
        """Migrate from per-delivery info files to feed-level state.

        Looks for existing per-delivery info files and uses the earliest
        (by commit date) to initialize the feed state. This ensures no
        messages are missed during migration.

        Args:
            feed_dir: Feed directory path
        """
        feed_state_file = feed_dir / 'korgalore.feed'

        if feed_state_file.exists():
            return  # Already migrated

        # Find all delivery info files across all epochs
        info_files: List[Path] = []
        git_dir = feed_dir / 'git'
        if git_dir.exists():
            info_files = list(git_dir.glob('*/korgalore.*.info'))

        if not info_files:
            return  # No legacy files, fresh install

        # Load all info files and find earliest commit by date
        earliest_info: Optional[Dict[str, Any]] = None
        earliest_date: Optional[datetime] = None

        for info_file in info_files:
            try:
                with open(info_file, 'r') as f:
                    info = json.load(f)
                    commit_date_str = info.get('commit_date')
                    if commit_date_str:
                        commit_date = datetime.strptime(commit_date_str, '%Y-%m-%d %H:%M:%S %z')
                        if earliest_date is None or commit_date < earliest_date:
                            earliest_date = commit_date
                            earliest_info = info
            except Exception as e:
                logger.warning('Failed to read legacy info file %s: %s', info_file, e)
                continue

        if earliest_info:
            # Detect highest epoch from directory structure
            epochs = []
            if git_dir.exists():
                for item in git_dir.iterdir():
                    if item.is_dir() and item.name.endswith('.git'):
                        try:
                            epoch_num = int(item.name.replace('.git', ''))
                            epochs.append(epoch_num)
                        except ValueError:
                            pass

            highest_epoch = max(epochs) if epochs else 0

            # Initialize feed state from earliest delivery
            state = {
                'last_update': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %z'),
                'update_successful': True,
                'latest_commit': earliest_info['last'],
                'highest_epoch': highest_epoch,
            }

            with open(feed_state_file, 'w') as f:
                json.dump(state, f, indent=2)

            logger.info('Migrated feed state from legacy per-delivery info files: %s', feed_dir.name)

