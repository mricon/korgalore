import json
import logging

from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import EmailPolicy
from email import charset
from pathlib import Path
from korgalore import PublicInboxError, GitError, StateError

from typing import Any, Dict, List, Optional, Tuple, Union

from datetime import datetime

charset.add_charset('utf-8', None)
logger = logging.getLogger('korgalore')

class PIService:
    GITCMD: str = "git"
    emlpolicy: EmailPolicy = EmailPolicy(utf8=True, cte_type='8bit', max_line_length=None,
                                         message_factory=EmailMessage)

    def __init__(self) -> None:
        pass

    def run_git_command(self, topdir: Optional[str], args: List[str]) -> Tuple[int, bytes]:
        """Run a git command in the specified topdir and return (returncode, output)."""
        import subprocess

        cmd = [self.GITCMD]
        if topdir:
            cmd += ['-C', topdir]
        cmd += args
        logger.debug('Running git command: %s', ' '.join(cmd))

        try:
            result = subprocess.run(cmd, capture_output=True)
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
        gitargs = ['rev-list', '--reverse', 'master']
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f"Git rev-list failed: {output.decode()}")
        if len(output):
            commits = output.decode().splitlines()
        else:
            commits = []
        return commits

    def recover_after_rebase(self, tgt_dir: Path) -> str:
        # Load korgalore.info to find last processed commit
        info = self.load_korgalore_info(tgt_dir)
        # Get the commit's date and parse it into datetime
        # The string is ISO with tzinfo: "2025-11-04 20:47:21 +0000"
        commit_date_str = info.get('commit_date')
        if not commit_date_str:
            raise StateError(f"No commit_date found in korgalore.info in {tgt_dir}.")
        commit_date = datetime.strptime(commit_date_str, '%Y-%m-%d %H:%M:%S %z')
        logger.debug(f"Last processed commit date: {commit_date.isoformat()}")
        # Try to find the new hash of this commit in the log by matching the subject and
        # message-id.
        gitargs = ['rev-list', '--reverse', '--since-as-filter', commit_date_str, 'master']
        retcode, output = self.run_git_command(str(tgt_dir), gitargs)
        if retcode != 0:
            # Not sure what happened here, just give up and return the latest commit
            logger.warning("Could not run rev-list to recover after rebase, returning latest commit.")
            latest_commit = self.get_top_commit(tgt_dir)
            return latest_commit

        possible_commits = output.decode().splitlines()
        if not possible_commits:
            # Just record the latest info, then
            self.update_korgalore_info(gitdir=tgt_dir)
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

        self.update_korgalore_info(gitdir=tgt_dir, latest_commit=last_commit, message=msg)
        return last_commit

    def get_latest_commits_in_epoch(self, gitdir: Path,
                                    since_commit: Optional[str] = None) -> List[str]:
        # How many new commits since our latest_commit
        if not since_commit:
            try:
                info = self.load_korgalore_info(gitdir)
            except StateError:
                raise StateError(f"korgalore.info not found in {gitdir}. Run init_list() first.")
            since_commit = info.get('last')
        # is this still a valid commit?
        gitargs = ['cat-file', '-e', f'{since_commit}^']
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            # The commit is not valid anymore, so try to find the latest commit by other
            # means.
            logger.debug(f"Since commit {since_commit} not found, trying to recover after rebase.")
            since_commit = self.recover_after_rebase(gitdir)
        gitargs = ['rev-list', '--reverse', '--ancestry-path', f'{since_commit}..master']
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
        gitargs = ['rev-list', '-n', '1', 'master']
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f"Git rev-list failed: {output.decode()}")
        top_commit = output.decode()
        return top_commit

    def update_korgalore_info(self, gitdir: Path,
                              latest_commit: Optional[str] = None,
                              message: Optional[Union[bytes, EmailMessage]] = None) -> None:
        if not latest_commit:
            latest_commit = self.get_top_commit(gitdir)

        # Get the commit date
        gitargs = ['show', '-s', '--format=%ci', latest_commit]
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise GitError(f"Git show failed: {output.decode()}")
        commit_date = output.decode()
        # TODO: latest_commit may not have a "m" file in it if it's a deletion
        korgalore_file = Path(gitdir) / 'korgalore.info'
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

    def load_korgalore_info(self, gitdir: Path) -> Dict[str, Any]:
        korgalore_file = Path(gitdir) / 'korgalore.info'
        if not korgalore_file.exists():
            raise StateError(
                f"korgalore.info not found in {gitdir}. Run init_list() first."
            )

        with open(korgalore_file, 'r') as gf:
            info = json.load(gf)  # type: Dict[str, Any]

        return info

