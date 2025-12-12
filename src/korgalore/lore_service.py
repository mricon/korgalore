import requests
from typing import List, Dict, Tuple, Any, Optional
from gzip import GzipFile
from pathlib import Path
from email import charset
import io
import json
import os
import tempfile
import re
import urllib.parse

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

    def store_epochs_info(self, feed_dir: Path, epochs: List[Tuple[int, str, str]]) -> None:
        epochs_file = feed_dir / 'epochs.json'
        epochs_info = []
        for enum, epath, fpr in epochs:
            epochs_info.append({
                'epoch': enum,
                'path': epath,
                'fpr': fpr
            })
        with open(epochs_file, 'w') as ef:
            json.dump(epochs_info, ef, indent=2)

    def load_epochs_info(self, feed_dir: Path) -> List[Tuple[int, str, str]]:
        epochs_file = feed_dir / 'epochs.json'
        if not epochs_file.exists():
            raise StateError(f"Epochs file {epochs_file} does not exist.")
        with open(epochs_file, 'r') as ef:
            epochs_data = json.load(ef)
        epochs: List[Tuple[int, str, str]] = []
        for entry in epochs_data:
            epochs.append((entry['epoch'], entry['path'], entry['fpr']))
        return epochs

    def init_feed(self, delivery_name: str, feed_dir: Path, pi_url: str) -> None:
        """Initialize a new feed.

        Args:
            delivery_name: Name of the delivery
            feed_dir: Directory to store feed data
            pi_url: Public inbox URL
        """
        if not feed_dir.exists():
            feed_dir.mkdir(parents=True, exist_ok=True)
        epochs = self.get_epochs(pi_url)
        enum, epath, _ = epochs[-1]
        tgt_dir = feed_dir / 'git' / f'{enum}.git'
        repo_url = f"{pi_url.rstrip('/')}/git/{enum}.git"
        self.clone_epoch(repo_url=repo_url, tgt_dir=tgt_dir)
        self.update_korgalore_info(gitdir=tgt_dir, delivery_name=delivery_name)

    def pull_highest_epoch(self, feed_dir: Path, delivery_name: Optional[str] = None) -> Tuple[int, Path, List[str]]:
        """Pull the highest epoch and return new commits.

        Args:
            feed_dir: Directory containing the feed data
            delivery_name: Name of the delivery (for per-delivery state files)

        Returns:
            Tuple of (highest_epoch, gitdir, new_commits)
        """
        # What is our highest epoch?
        existing_epochs = self.find_epochs(feed_dir)
        highest_epoch = max(existing_epochs)
        logger.debug(f"Highest epoch found: {highest_epoch}")
        epochs_dir = feed_dir / 'git'
        tgt_dir = epochs_dir / f'{highest_epoch}.git'
        # Pull the latest changes
        gitargs = ['fetch', 'origin', '--shallow-since=1.week.ago', '--update-shallow']
        retcode, output = self.run_git_command(str(tgt_dir), gitargs)
        if retcode != 0:
            raise RemoteError(f"Git remote update failed: {output.decode()}")
        new_commits = self.get_latest_commits_in_epoch(tgt_dir, delivery_name=delivery_name)
        return highest_epoch, tgt_dir, new_commits

    def get_msgid_from_url(self, msgid_or_url: str) -> str:
        # Parse the input to determine if it's a URL or a msgid
        if '://' in msgid_or_url:
            # Get anything that looks like a msgid
            matches = re.search(r'^https?://[^@]+/([^/]+@[^/]+)', msgid_or_url, re.IGNORECASE)
            if matches:
                chunks = matches.groups()
                msgid = urllib.parse.unquote(chunks[0])
                return msgid
        return msgid_or_url.strip('<>')

    def get_message_by_msgid(self, msgid_or_url: str) -> bytes:
        # Parse the input to determine if it's a URL or a msgid
        msgid = self.get_msgid_from_url(msgid_or_url)
        raw_url = f"https://lore.kernel.org/all/{msgid}/raw"

        logger.debug(f"Fetching message from: {raw_url}")

        try:
            response = self.session.get(raw_url)
            response.raise_for_status()
            return response.content
        except Exception as e:
            raise RemoteError(
                f"Failed to fetch message from {raw_url}: {e}"
            ) from e

    def get_thread_by_msgid(self, msgid_or_url: str) -> List[bytes]:
        msgid = self.get_msgid_from_url(msgid_or_url)
        mbox_url = f"https://lore.kernel.org/all/{msgid}/t.mbox.gz"
        logger.debug(f"Fetching thread from: {mbox_url}")

        try:
            response = self.session.get(mbox_url)
            response.raise_for_status()
        except Exception as e:
            raise RemoteError(
                f"Failed to fetch thread from {mbox_url}: {e}"
            ) from e

        # Decompress the gzipped mbox
        try:
            with GzipFile(fileobj=io.BytesIO(response.content)) as f:
                mbox_content = f.read()
        except Exception as e:
            raise RemoteError(
                f"Failed to decompress thread mbox: {e}"
            ) from e

        messages = self.mailsplit_bytes(mbox_content)
        logger.debug(f"Parsed {len(messages)} messages from thread")

        return messages

    def mailsplit_bytes(self, bmbox: bytes) -> List[bytes]:
        msgs: List[bytes] = list()
        # Use a safe temporary directory for mailsplit output
        with tempfile.TemporaryDirectory(suffix='-mailsplit') as tfd:
            logger.debug('Mailsplitting the mbox into %s', tfd)
            args = ['mailsplit', '--mboxrd', '-o%s' % tfd]
            ecode, out = self.run_git_command(None, args, stdin=bmbox)
            if ecode > 0:
                logger.critical('Unable to parse mbox received from the server')
                return msgs
            # Read in the files
            for msg in os.listdir(tfd):
                with open(os.path.join(tfd, msg), 'rb') as fh:
                    msgs.append(fh.read())
            return msgs