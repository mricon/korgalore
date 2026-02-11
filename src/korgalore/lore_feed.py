import requests
from typing import List, Dict, Tuple, Any, Optional
from gzip import GzipFile
from pathlib import Path
from email import charset
import io
import json
import re
import urllib.parse

import logging

from korgalore import get_requests_session, run_git_command, StateError, RemoteError
from korgalore.pi_feed import PIFeed

charset.add_charset('utf-8', None)

logger = logging.getLogger('korgalore')


class LoreFeed(PIFeed):
    """Service for interacting with lore.kernel.org public-inbox archives."""

    def __init__(self, feed_key: str, feed_dir: Path, feed_url: str,
                 reqsession: Optional[requests.Session] = None) -> None:
        """Initialize a LoreFeed instance.

        Args:
            feed_key: Unique identifier for this feed.
            feed_dir: Local directory path for storing feed data.
            feed_url: Base URL of the lore.kernel.org archive.
            reqsession: Optional requests session for HTTP calls. If not provided,
                a new session with appropriate User-Agent header will be created.
        """
        super().__init__(feed_key, feed_dir)
        self.feed_type = 'lore'
        self.feed_url = feed_url
        if reqsession:
            self.session = reqsession
        else:
            self.session = get_requests_session()

    def get_manifest(self) -> Dict[str, Any]:
        """Fetch and parse the gzipped manifest from the Lore server."""
        try:
            response = self.session.get(f"{self.feed_url.rstrip('/')}/manifest.js.gz")
            response.raise_for_status()
        except Exception as e:
            raise RemoteError(
                f"Failed to fetch manifest from {self.feed_url}: {e}"
            ) from e
        # ungzip and parse the manifest
        manifest: Dict[str, Any] = dict()
        with GzipFile(fileobj=io.BytesIO(response.content)) as f:
            mf = json.load(f)
            for key, vals in mf.items():
                manifest[key] = vals

        return manifest

    def clone_epoch(self, epoch: int, shallow: bool = True) -> None:
        """Clone a git epoch repository from remote Lore server."""
        gitdir = self.get_gitdir(epoch)
        # does tgt_dir exist?
        if Path(gitdir).exists():
            logger.debug(f"Target directory {gitdir} already exists, skipping clone.")
            return

        gitargs = ['clone', '--mirror']
        if shallow:
            gitargs += ['--shallow-since=1.week.ago']
        repo_url = f"{self.feed_url.rstrip('/')}/git/{epoch}.git"
        gitargs += [repo_url, str(gitdir)]

        retcode, output, error = run_git_command(None, gitargs)
        if retcode != 0:
            raise RemoteError(f"Git clone failed (exit {retcode}): {error.decode()}")

    def get_manifest_epochs(self) -> List[Tuple[int, str, str]]:
        """Parse manifest to extract sorted list of (epoch, path, fingerprint) tuples."""
        manifest = self.get_manifest()
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
                logger.warning(f"Invalid epoch string: {epoch_str} in {self.feed_url}")
        # Sort epochs by their numeric value
        epochs.sort(key=lambda x: x[0])
        self.store_epochs_info(epochs)
        return epochs

    def store_epochs_info(self, epochs: List[Tuple[int, str, str]]) -> None:
        """Save epoch information to local JSON file."""
        epochs_file = self.feed_dir / 'epochs.json'
        epochs_info = []
        for enum, epath, fpr in epochs:
            epochs_info.append({
                'epoch': enum,
                'path': epath,
                'fpr': fpr
            })
        with open(epochs_file, 'w') as ef:
            json.dump(epochs_info, ef, indent=2)

    def load_epochs_info(self) -> List[Tuple[int, str, str]]:
        """Load epoch information from local JSON file."""
        epochs_file = self.feed_dir / 'epochs.json'
        if not epochs_file.exists():
            raise StateError(f"Epochs file {epochs_file} does not exist.")
        with open(epochs_file, 'r') as ef:
            epochs_data = json.load(ef)
        epochs: List[Tuple[int, str, str]] = []
        for entry in epochs_data:
            epochs.append((entry['epoch'], entry['path'], entry['fpr']))
        return epochs

    def init_feed(self) -> None:
        """Initialize a new Lore feed by fetching manifest and cloning latest epoch."""
        if not self.feed_dir.exists():
            self.feed_dir.mkdir(parents=True, exist_ok=True)
        epochs = self.get_manifest_epochs()
        epoch, _, _ = epochs[-1]
        self.clone_epoch(epoch)
        self.save_feed_state(epoch=epoch, success=True)

    def update_feed(self) -> int:
        """Update feed by fetching new epochs and commits.

        Returns:
            Status constant: STATUS_NOCHANGE, STATUS_UPDATED, or STATUS_INITIALIZED.
        """
        try:
            feed_state = self.load_feed_state()
        except StateError:
            self.init_feed()
            return self.STATUS_INITIALIZED
        local_epoch_info = self.load_epochs_info()
        remote_epoch_info = self.get_manifest_epochs()
        if local_epoch_info == remote_epoch_info:
            logger.debug('No new epochs found for feed: %s', self.feed_dir)
            return self.STATUS_NOCHANGE

        # What is our highest epoch?
        highest_local_epoch = max(int(e) for e in feed_state['epochs'].keys())
        logger.debug(f"Highest local epoch: {highest_local_epoch}")
        gitdir = self.get_gitdir(highest_local_epoch)
        # Pull the latest changes
        gitargs = ['fetch', 'origin', '--shallow-since=1.week.ago', '--update-shallow']
        retcode, output, error = run_git_command(str(gitdir), gitargs)
        if retcode != 0:
            raise RemoteError(f"Git fetch failed (exit {retcode}): {error.decode()}")

        updated = self.feed_updated(highest_local_epoch)

        self.save_feed_state(
            epoch=highest_local_epoch,
            success=True
        )

        # Now see if we have any new epochs on the remote
        highest_remote_epoch = max(e[0] for e in remote_epoch_info)
        logger.debug('Highest remote epoch: %d', highest_remote_epoch)
        if highest_local_epoch == highest_remote_epoch:
            logger.debug('No new epochs detected for feed %s', self.feed_dir)
            return self.STATUS_UPDATED if updated else self.STATUS_NOCHANGE

        # In theory, we could have more than one new epoch, for example if
        # someone hasn't run korgalore in a long time. This is almost certainly
        # not something anyone would want, because it would involve pulling a lot of data
        # that would take ages. So for now, we just pick the highest new epoch, which
        # will be correct in vast majority of cases.
        logger.debug('Cloning new epoch %d for feed %s', highest_remote_epoch, self.feed_dir)
        # We don't clone shallow for new epochs, since we want all the mail there for the initial run
        self.clone_epoch(epoch=highest_remote_epoch, shallow=False)
        self.save_feed_state(
            epoch=highest_remote_epoch,
            success=True
        )
        return self.STATUS_UPDATED

    @staticmethod
    def get_msgid_from_url(msgid_or_url: str) -> str:
        """Extract message ID from URL or return input if already a msgid."""
        if '://' in msgid_or_url:
            # Get anything that looks like a msgid
            matches = re.search(r'^https?://[^@]+/([^/]+@[^/]+)', msgid_or_url, re.IGNORECASE)
            if matches:
                chunks = matches.groups()
                msgid = urllib.parse.unquote(chunks[0])
                return msgid
        return msgid_or_url.strip('<>')

    @staticmethod
    def get_message_by_msgid(msgid_or_url: str) -> bytes:
        """Fetch a single raw email message from Lore by message ID or URL."""
        msgid = LoreFeed.get_msgid_from_url(msgid_or_url)
        raw_url = f"https://lore.kernel.org/all/{msgid}/raw"

        logger.debug(f"Fetching message from: {raw_url}")

        try:
            reqsession = get_requests_session()
            response = reqsession.get(raw_url)
            response.raise_for_status()
            return response.content
        except Exception as e:
            raise RemoteError(
                f"Failed to fetch message from {raw_url}: {e}"
            ) from e

    @staticmethod
    def get_thread_by_msgid(msgid_or_url: str) -> List[bytes]:
        """Fetch all messages in a thread from Lore by message ID or URL.

        Args:
            msgid_or_url: Either a message ID or a lore.kernel.org URL.

        Returns:
            List of raw email messages as bytes, one per message in the thread.

        Raises:
            RemoteError: If fetching or decompressing the thread mbox fails.
        """
        msgid = LoreFeed.get_msgid_from_url(msgid_or_url)
        mbox_url = f"https://lore.kernel.org/all/{msgid}/t.mbox.gz"
        logger.debug(f"Fetching thread from: {mbox_url}")

        try:
            reqsession = get_requests_session()
            response = reqsession.get(mbox_url)
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

        messages = PIFeed.mailsplit_bytes(mbox_content)
        logger.debug(f"Parsed {len(messages)} messages from thread")

        return messages
