import requests
from typing import List, Dict, Tuple, Any
from gzip import GzipFile
from pathlib import Path
from email import charset
import io
import json

import logging

from korgalore import __version__, StateError, RemoteError
from korgalore.pi_service import PIService

charset.add_charset('utf-8', None)

logger = logging.getLogger('korgalore')


class LoreService(PIService):
    """Service for interacting with lore.kernel.org public-inbox archives."""

    def __init__(self, datadir: Path) -> None:
        # do a parent init
        super().__init__()
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': f'korgalore/{__version__}'
        })
        self.datadir = datadir

    def get_manifest(self, pi_url: str) -> Dict[str, Any]:
        try:
            response = self.session.get(f"{pi_url.rstrip('/')}/manifest.js.gz")
            response.raise_for_status()
        except Exception as e:
            raise RemoteError(
                f"Failed to fetch manifest from {pi_url}: {e}"
            ) from e
        # ungzip and parse the manifest
        manifest: Dict[str, Any] = dict()
        with GzipFile(fileobj=io.BytesIO(response.content)) as f:
            mf = json.load(f)
            for key, vals in mf.items():
                manifest[key] = vals

        return manifest

    def clone_epoch(self, repo_url: str, tgt_dir: Path, shallow: bool = True) -> None:
        # does tgt_dir exist?
        if Path(tgt_dir).exists():
            logger.debug(f"Target directory {tgt_dir} already exists, skipping clone.")
            return

        gitargs = ['clone', '--mirror']
        if shallow:
            gitargs += ['--shallow-since=1.week.ago']
        gitargs += [repo_url, str(tgt_dir)]

        retcode, output = self.run_git_command(None, gitargs)
        if retcode != 0:
            raise RemoteError(f"Git clone failed: {output.decode()}")

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
            raise StateError(f"Epochs file {epochs_file} does not exist.")
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
        self.update_korgalore_info(gitdir=tgt_dir)

    def pull_highest_epoch(self, list_dir: Path) -> Tuple[int, Path, List[str]]:
        # What is our highest epoch?
        existing_epochs = self.find_epochs(list_dir)
        highest_epoch = max(existing_epochs)
        logger.debug(f"Highest epoch found: {highest_epoch}")
        epochs_dir = list_dir / 'git'
        tgt_dir = epochs_dir / f'{highest_epoch}.git'
        # Pull the latest changes
        gitargs = ['fetch', 'origin', '--shallow-since=1.week.ago', '--update-shallow']
        retcode, output = self.run_git_command(str(tgt_dir), gitargs)
        if retcode != 0:
            raise RemoteError(f"Git remote update failed: {output.decode()}")
        new_commits = self.get_latest_commits_in_epoch(tgt_dir)
        return highest_epoch, tgt_dir, new_commits
