import json
import logging

from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import EmailPolicy
from email import charset
from pathlib import Path

from typing import Any, Dict, List, Optional, Tuple

charset.add_charset('utf-8', None)
logger = logging.getLogger(__name__)

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

        result = subprocess.run(cmd, capture_output=True)
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
            raise FileNotFoundError(f"No existing epochs found in {epochs_dir}.")
        return sorted(existing_epochs)

    def get_all_commits_in_epoch(self, gitdir: Path) -> List[str]:
        gitargs = ['rev-list', '--reverse', 'master']
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise RuntimeError(f"Git rev-list failed: {output.decode()}")
        if len(output):
            commits = output.decode().splitlines()
        else:
            commits = []
        return commits

    def get_latest_commits_in_epoch(self, gitdir: Path) -> List[str]:
        # How many new commits since our latest_commit
        try:
            info = self.load_korgalore_info(gitdir)
        except FileNotFoundError:
            raise RuntimeError(f"korgalore.info not found in {gitdir}. Run init_list() first.")
        last_commit = info.get('last')
        gitargs = ['rev-list', '--reverse', '--ancestry-path', f'{last_commit}..master']
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise RuntimeError(f"Git rev-list failed: {output.decode()}")
        if len(output):
            new_commits = output.decode().splitlines()
        else:
            new_commits = []
        return new_commits

    def get_message_at_commit(self, pi_dir: Path, commitish: str) -> bytes:
        gitargs = ['show', f'{commitish}:m']
        retcode, output = self.run_git_command(str(pi_dir), gitargs)
        if retcode == 128:
            raise FileNotFoundError(f"Commit {commitish} does not have a message file.")
        if retcode != 0:
            raise RuntimeError(f"Git show failed: {output.decode()}")
        return output

    def parse_message(self, raw_message: bytes) -> EmailMessage:
        """Parse a raw email message into an EmailMessage object."""
        msg: EmailMessage = BytesParser(_class=EmailMessage,
                                        policy=self.emlpolicy).parsebytes(raw_message)  # type: ignore
        return msg

    def update_korgalore_info(self, gitdir: Path,
                              latest_commit: Optional[str] = None,
                              message: Optional[bytes] = None) -> None:
        if not latest_commit:
            gitargs = ['rev-list', '-n', '1', 'master']
            retcode, output = self.run_git_command(str(gitdir), gitargs)
            if retcode != 0:
                raise RuntimeError(f"Git rev-list failed: {output.decode()}")
            latest_commit = output.decode()
            if not latest_commit:
                raise RuntimeError("No commits found in the repository.")

        # Get the commit date
        gitargs = ['show', '-s', '--format=%ci', latest_commit]
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise RuntimeError(f"Git show failed: {output.decode()}")
        commit_date = output.decode()
        # TODO: latest_commit may not have a "m" file in it if it's a deletion
        korgalore_file = Path(gitdir) / 'korgalore.info'
        if not message:
            message = self.get_message_at_commit(gitdir, latest_commit)

        msg = self.parse_message(message)
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
            raise FileNotFoundError(
                f"korgalore.info not found in {gitdir}. Run init_list() first."
            )

        with open(korgalore_file, 'r') as gf:
            info = json.load(gf)  # type: Dict[str, Any]

        return info

