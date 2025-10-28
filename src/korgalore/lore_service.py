import requests
from typing import List, Dict, Optional, Tuple, Any
from gzip import GzipFile
from pathlib import Path
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import EmailPolicy
from email import charset
import io
import json

import logging

from korgalore import __version__

charset.add_charset('utf-8', None)

"""
Lore service for interacting with lore.kernel.org public-inbox archives.

This module provides functionality to fetch and parse mailing list archives
from the Linux kernel's public-inbox service at lore.kernel.org.
"""


logger = logging.getLogger(__name__)


class LoreService:
    """Service for interacting with lore.kernel.org public-inbox archives."""

    GITCMD: str = "git"
    emlpolicy: EmailPolicy = EmailPolicy(utf8=True, cte_type='8bit', max_line_length=None,
                                         message_factory=EmailMessage)

    def __init__(self, datadir: Path) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': f'korgalore/{__version__}'
        })
        self.datadir = datadir

    def get_manifest(self, pi_url: str) -> Dict[str, Any]:
        response = self.session.get(f"{pi_url.rstrip('/')}/manifest.js.gz")
        response.raise_for_status()
        # ungzip and parse the manifest
        manifest: Dict[str, Any] = dict()
        with GzipFile(fileobj=io.BytesIO(response.content)) as f:
            mf = json.load(f)
            for key, vals in mf.items():
                manifest[key] = vals

        return manifest

    def run_git_command(self, topdir: Optional[str], args: List[str]) -> Tuple[int, bytes]:
        """Run a git command in the specified topdir and return (returncode, output)."""
        import subprocess

        cmd = [self.GITCMD]
        if topdir:
            cmd += ['-C', topdir]
        cmd += args

        result = subprocess.run(cmd, capture_output=True)
        return result.returncode, result.stdout.strip()

    def clone_epoch(self, repo_url: str, tgt_dir: Path) -> None:
        # does tgt_dir exist?
        if Path(tgt_dir).exists():
            raise RuntimeError(f"Destination directory {tgt_dir} already exists.")

        gitargs = ['clone', '--mirror', '--depth=1', repo_url, str(tgt_dir)]
        retcode, output = self.run_git_command(None, gitargs)
        if retcode != 0:
            raise RuntimeError(f"Git clone failed: {output.decode()}")

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

    def update_piper_info(self, gitdir: Path,
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

    def get_epochs(self, pi_url: str) -> List[Tuple[int, str, str]]:
        manifest = self.get_manifest(pi_url)
        # The keys are epoch paths, so we extract epoch numbers and paths
        epochs: List[Tuple[int, str, str]] = []
        # The key ends in #.git, so grab the final path component and remove .git
        for epoch_path in manifest.keys():
            epoch_str = epoch_path.split('/')[-1].replace('.git', '')
            try:
                epoch_num = int(epoch_str)
                fpr = str(manifest[epoch_path]['fingerprint'])
                epochs.append((epoch_num, epoch_path, fpr))
            except ValueError:
                logger.warning(f"Invalid epoch string: {epoch_str} in {pi_url}")
        # Sort epochs by their numeric value
        epochs.sort(key=lambda x: x[0])
        return epochs

    def store_epochs_info(self, list_dir: Path, epochs: List[Tuple[int, str, str]]) -> None:
        epochs_file = list_dir / 'epochs.json'
        epochs_info = []
        for enum, epath, fpr in epochs:
            epochs_info.append({
                'epoch': enum,
                'path': epath,
                'fpr': fpr
            })
        with open(epochs_file, 'w') as ef:
            json.dump(epochs_info, ef, indent=2)

    def load_epochs_info(self, list_dir: Path) -> List[Tuple[int, str, str]]:
        epochs_file = list_dir / 'epochs.json'
        if not epochs_file.exists():
            raise FileNotFoundError(f"Epochs file {epochs_file} does not exist.")
        with open(epochs_file, 'r') as ef:
            epochs_data = json.load(ef)
        epochs: List[Tuple[int, str, str]] = []
        for entry in epochs_data:
            epochs.append((entry['epoch'], entry['path'], entry['fpr']))
        return epochs

    def init_list(self, list_name: str, list_dir: Path, pi_url: str) -> None:
        if not list_dir.exists():
            list_dir.mkdir(parents=True, exist_ok=True)
        epochs = self.get_epochs(pi_url)
        enum, epath, _ = epochs[-1]
        tgt_dir = list_dir / 'git' / f'{enum}.git'
        repo_url = f"{pi_url.rstrip('/')}/git/{enum}.git"
        self.clone_epoch(repo_url=repo_url, tgt_dir=tgt_dir)
        self.update_piper_info(gitdir=tgt_dir)

    def load_korgalore_info(self, gitdir: Path) -> Dict[str, Any]:
        korgalore_file = Path(gitdir) / 'korgalore.info'
        if not korgalore_file.exists():
            raise FileNotFoundError(
                f"korgalore.info not found in {gitdir}. Run init_list() first."
            )

        with open(korgalore_file, 'r') as gf:
            info = json.load(gf)  # type: Dict[str, Any]

        return info

    def pull_highest_epoch(self, list_dir: Path) -> Tuple[int, Path, List[str]]:
        # What is our highest epoch?
        epochs_dir = list_dir / 'git'
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
        # Sort to find the highest
        highest_epoch = max(existing_epochs)
        logger.debug(f"Highest epoch found: {highest_epoch}")
        tgt_dir = epochs_dir / f'{highest_epoch}.git'
        # Pull the latest changes
        gitargs = ['remote', 'update', 'origin', '--prune']
        retcode, output = self.run_git_command(str(tgt_dir), gitargs)
        if retcode != 0:
            raise RuntimeError(f"Git remote update failed: {output.decode()}")
        # How many new commits since our latest_commit
        info = self.load_korgalore_info(tgt_dir)
        last_commit = info.get('last')
        gitargs = ['rev-list', '--reverse', '--ancestry-path', f'{last_commit}..master']
        retcode, output = self.run_git_command(str(tgt_dir), gitargs)
        if retcode != 0:
            raise RuntimeError(f"Git rev-list failed: {output.decode()}")
        if len(output):
            new_commits = output.decode().splitlines()
        else:
            new_commits = []
        return highest_epoch, tgt_dir, new_commits

    def reshallow(self, gitdir: Path, since_commit: str) -> None:
        # Trim the repository to remove anything we've handled already
        with open(gitdir / 'shallow', 'w') as sf:
            sf.write(since_commit + '\n')
        gitargs = ['gc', '--prune=now']
        retcode, output = self.run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise RuntimeError(f"Git gc failed: {output.decode()}")
