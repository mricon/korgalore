import io
import json
import logging
from datetime import UTC, datetime, timedelta
from gzip import GzipFile
from pathlib import Path
from typing import Any

from korgalore import RemoteError, StateCorruptError, StateError, run_git_command
from korgalore.pi_feed import PIFeed
from liblore import LoreNode

logger = logging.getLogger('korgalore')

# How much history a lore clone keeps on disk
HISTORY_WINDOW = timedelta(weeks=1)
# Digests can ask for more history, but never more than this. Older
# messages are left out, and the digest says that they are missing.
HISTORY_MAX = timedelta(days=30)


def _fetch_manifest(node: LoreNode, base_url: str) -> dict[str, Any]:
    """Fetch and parse a public-inbox manifest.

    Manifests describe the epochs a public-inbox archive is split into,
    which is what drives epoch mirroring. They are fetched gzipped from
    ``{base_url}/manifest.js.gz``.

    Args:
        node: LoreNode to make the request with, giving mirror failover.
        base_url: Base URL of the public-inbox archive.

    Returns:
        The parsed manifest, mapping epoch paths to their metadata.

    Raises:
        RemoteError: If the manifest cannot be fetched, is not valid
            gzipped JSON, or is empty.
    """
    manifest_url = f'{base_url.rstrip("/")}/manifest.js.gz'
    logger.debug('Fetching manifest from %s', manifest_url)

    try:
        response = node.request('GET', manifest_url)
        response.raise_for_status()
    except Exception as e:
        raise RemoteError(f'Failed to fetch manifest from {manifest_url}: {e}') from e

    try:
        with GzipFile(fileobj=io.BytesIO(response.content)) as f:
            manifest: dict[str, Any] = json.load(f)
    except Exception as e:
        raise RemoteError(f'Failed to parse manifest from {manifest_url}: {e}') from e

    # A server that answers at all but has nothing to mirror is a remote
    # problem, not an archive with zero epochs.
    if not manifest:
        raise RemoteError(f'Empty manifest from {manifest_url}')

    return manifest


class LoreFeed(PIFeed):
    """Service for interacting with lore.kernel.org public-inbox archives."""

    def __init__(self, feed_key: str, feed_dir: Path, feed_url: str, lore_node: LoreNode | None = None) -> None:
        """Initialize a LoreFeed instance.

        Args:
            feed_key: Unique identifier for this feed.
            feed_dir: Local directory path for storing feed data.
            feed_url: Base URL of the lore.kernel.org archive.
            lore_node: Optional LoreNode for HTTP calls with failover.
                If not provided, one is created via make_lore_node().
        """
        super().__init__(feed_key, feed_dir)
        self.feed_type = 'lore'
        self.feed_url = feed_url
        if lore_node is not None:
            self._node = lore_node
        else:
            from korgalore import make_lore_node

            self._node = make_lore_node(url=feed_url)

    @staticmethod
    def validate_public_inbox_url(url: str) -> str:
        """Validate a public-inbox URL by fetching its manifest.

        Fetches {url}/manifest.js.gz, parses the JSON, and verifies that
        all epoch keys share the same list name prefix.

        Works with any public-inbox server, not just lore.kernel.org.

        Args:
            url: The public-inbox base URL to validate.

        Returns:
            The list name extracted from the manifest keys.

        Raises:
            RemoteError: If the manifest cannot be fetched, parsed, or
                contains inconsistent list prefixes.
        """
        from korgalore import make_lore_node

        node = make_lore_node(url=url)
        try:
            manifest = _fetch_manifest(node, url)
        finally:
            node.close()

        # Extract list name prefixes (e.g. /lkml/git/0.git -> lkml)
        prefixes: set[str] = set()
        for key in manifest:
            parts = key.strip('/').split('/')
            if parts:
                prefixes.add(parts[0])

        if len(prefixes) != 1:
            raise RemoteError(f'Manifest entries have inconsistent list prefixes: {", ".join(sorted(prefixes))}')

        return prefixes.pop()

    def get_message_by_msgid(self, msgid: str) -> bytes:
        """Fetch one raw message from the archive.

        Raises:
            liblore.LibloreError: If the message cannot be fetched.
        """
        return self._node.get_message_by_msgid(msgid)

    def get_manifest(self) -> dict[str, Any]:
        """Fetch and parse the gzipped manifest from the Lore server."""
        return _fetch_manifest(self._node, self.feed_url)

    def _git_mirror_config(self) -> dict[str, str]:
        """Return git config dict to redirect operations to the preferred mirror.

        After auto-probe, the first entry in node.origins is the
        fastest responding mirror.  If it differs from the canonical
        origin, return a ``url.<mirror>/.insteadOf`` config entry so
        git transparently uses the mirror.
        """
        origins = self._node.origins
        canonical = self._node.canonical_origin
        if origins and origins[0] != canonical:
            preferred = origins[0]
            return {f'url.{preferred}/.insteadOf': f'{canonical}/'}
        return {}

    def clone_epoch(self, epoch: int, shallow: bool = True) -> None:
        """Clone a git epoch repository from remote Lore server."""
        gitdir = self.get_gitdir(epoch)
        # does tgt_dir exist?
        if Path(gitdir).exists():
            logger.debug('Target directory %s already exists, skipping clone.', gitdir)
            return

        repo_url = f'{self.feed_url.rstrip("/")}/git/{epoch}.git'
        gitargs = ['clone', '--mirror']
        if shallow:
            gitargs += ['--shallow-since=1.week.ago']
        gitargs += [repo_url, str(gitdir)]

        mirror_config = self._git_mirror_config()
        retcode, _output, error = run_git_command(None, gitargs, git_config=mirror_config)
        if retcode != 0 and shallow:
            # Shallow clone with --shallow-since can fail for dormant lists
            # that have no commits in the time window. Fall back to --depth=1
            # which always succeeds regardless of commit dates.
            logger.debug('Shallow clone failed, retrying with --depth=1: %s', error.decode())
            gitargs = ['clone', '--mirror', '--depth=1', repo_url, str(gitdir)]
            retcode, _output, error = run_git_command(None, gitargs, git_config=mirror_config)
        if retcode != 0:
            raise RemoteError(f'Git clone failed (exit {retcode}): {error.decode()}')

    def get_manifest_epochs(self) -> list[tuple[int, str, str]]:
        """Parse manifest to extract sorted list of (epoch, path, fingerprint) tuples."""
        manifest = self.get_manifest()
        # The keys are epoch paths, so we extract epoch numbers and paths
        epochs: list[tuple[int, str, str]] = []
        # The key ends in #.git, so grab the final path component and remove .git
        for epoch_path in manifest:
            epoch_str = epoch_path.split('/')[-1].replace('.git', '')
            try:
                epoch_num = int(epoch_str)
                fpr = str(manifest[epoch_path]['fingerprint'])
                epochs.append((epoch_num, epoch_path, fpr))
            except ValueError:
                logger.warning('Invalid epoch string: %s in %s', epoch_str, self.feed_url)
        # Sort epochs by their numeric value
        epochs.sort(key=lambda x: x[0])
        return epochs

    def store_epochs_info(self, epochs: list[tuple[int, str, str]]) -> None:
        """Record the manifest that the local epochs are now in sync with.

        Call this only after the fetch and the clone have succeeded, so
        that a failed update is retried on the next pull.
        """
        epochs_file = self.feed_dir / 'epochs.json'
        epochs_info = []
        for enum, epath, fpr in epochs:
            epochs_info.append({'epoch': enum, 'path': epath, 'fpr': fpr})
        self._atomic_write(epochs_file, json.dumps(epochs_info, indent=2))

    def load_epochs_info(self) -> list[tuple[int, str, str]]:
        """Load the manifest recorded by the last successful update.

        Returns an empty list when there is no usable record, so that the
        update fetches and records the manifest again.
        """
        epochs_file = self.feed_dir / 'epochs.json'
        if not epochs_file.exists():
            return []
        try:
            epochs_data = json.loads(epochs_file.read_text())
            return [(entry['epoch'], entry['path'], entry['fpr']) for entry in epochs_data]
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as e:
            logger.warning('Epochs file %s is corrupt, fetching to rebuild it: %s', epochs_file, e)
            return []

    @staticmethod
    def shallow_since(keep_history_since: datetime | None, now: datetime | None = None) -> str | None:
        """The --shallow-since value for a fetch, or None for the default window.

        keep_history_since is the oldest commit date a digest still needs.
        When it is inside the default one-week window, the default is used.
        Otherwise the history goes back to it, but never more than
        HISTORY_MAX.
        """
        if keep_history_since is None:
            return None
        if now is None:
            now = datetime.now(UTC)
        if keep_history_since >= now - HISTORY_WINDOW:
            return None
        since = max(keep_history_since, now - HISTORY_MAX)
        return since.strftime('%Y-%m-%d %H:%M:%S %z')

    def fetch_epoch(self, epoch: int, keep_history_since: datetime | None = None, now: datetime | None = None) -> None:
        """Fetch new commits for an epoch and move the shallow cut.

        Normally the cut moves to one week back on every fetch. When a
        digest needs older history, the cut moves to keep_history_since
        instead (see shallow_since()), and git fetches the older commits
        back if they are already gone.
        """
        gitdir = self.get_gitdir(epoch)
        mirror_config = self._git_mirror_config()
        since = self.shallow_since(keep_history_since, now)
        if since is None:
            gitargs = ['fetch', 'origin', '--shallow-since=1.week.ago', '--update-shallow']
            # Shallow fetch with --shallow-since can fail for dormant lists
            # that have no commits in the time window. Fall back to --depth=1
            # which always succeeds regardless of commit dates.
            fallback = ['fetch', 'origin', '--depth=1', '--update-shallow']
        else:
            logger.debug('Keeping history of %s since %s for digests', self.feed_key, since)
            gitargs = ['fetch', 'origin', f'--shallow-since={since}', '--update-shallow']
            # --depth=1 would cut off the history a digest needs, so fall
            # back to a plain fetch, which leaves the cut where it is
            fallback = ['fetch', 'origin']
        retcode, _output, error = run_git_command(str(gitdir), gitargs, git_config=mirror_config)
        if retcode != 0:
            logger.debug(
                'Shallow fetch failed, retrying with %s: %s', ' '.join(fallback[2:]) or 'a plain fetch', error.decode()
            )
            retcode, _output, error = run_git_command(str(gitdir), fallback, git_config=mirror_config)
        if retcode != 0:
            raise RemoteError(f'Git fetch failed (exit {retcode}): {error.decode()}')

    def init_feed(self) -> None:
        """Initialize a new Lore feed by fetching manifest and cloning latest epoch.

        The manifest is not recorded here, because clone_epoch() leaves an
        existing epoch as it is, and that epoch may be behind. The first
        update then fetches and records it.
        """
        if not self.feed_dir.exists():
            self.feed_dir.mkdir(parents=True, exist_ok=True)
        epochs = self.get_manifest_epochs()
        epoch, _, _ = epochs[-1]
        self.clone_epoch(epoch)
        self.save_feed_state(epoch=epoch, success=True)

    def update_feed(self, keep_history_since: datetime | None = None) -> int:
        """Update feed by fetching new epochs and commits.

        keep_history_since is the oldest commit date that a digest on this
        feed still needs (see fetch_epoch()).

        Returns:
            Status constant: STATUS_NOCHANGE, STATUS_UPDATED, or STATUS_INITIALIZED.
        """
        try:
            feed_state = self.load_feed_state()
        except StateCorruptError as e:
            logger.warning('%s, rebuilding it from the local epochs', e)
            # The deliveries of the pull that lost the state may never have
            # run, so let this pull run them
            return self._update_epochs(self.rebuild_feed_state(), keep_history_since) | self.STATUS_UPDATED
        except StateError:
            self.init_feed()
            return self.STATUS_INITIALIZED
        return self._update_epochs(feed_state, keep_history_since)

    def _update_epochs(self, feed_state: dict[str, Any], keep_history_since: datetime | None) -> int:
        """Fetch and clone what the remote manifest has that we lack."""
        local_epoch_info = self.load_epochs_info()
        remote_epoch_info = self.get_manifest_epochs()
        if local_epoch_info == remote_epoch_info:
            logger.debug('No new epochs found for feed: %s', self.feed_dir)
            return self.STATUS_NOCHANGE

        # What is our highest epoch?
        highest_local_epoch = max(int(e) for e in feed_state['epochs'])
        logger.debug('Highest local epoch: %s', highest_local_epoch)
        # Pull the latest changes
        self.fetch_epoch(highest_local_epoch, keep_history_since)

        updated = self.feed_updated(highest_local_epoch)

        self.save_feed_state(epoch=highest_local_epoch, success=True)

        # Now see if we have any new epochs on the remote
        highest_remote_epoch = max(e[0] for e in remote_epoch_info)
        logger.debug('Highest remote epoch: %d', highest_remote_epoch)
        if highest_local_epoch == highest_remote_epoch:
            logger.debug('No new epochs detected for feed %s', self.feed_dir)
            self.store_epochs_info(remote_epoch_info)
            return self.STATUS_UPDATED if updated else self.STATUS_NOCHANGE

        # In theory, we could have more than one new epoch, for example if
        # someone hasn't run korgalore in a long time. This is almost certainly
        # not something anyone would want, because it would involve pulling a lot of data
        # that would take ages. So for now, we just pick the highest new epoch, which
        # will be correct in vast majority of cases.
        logger.debug('Cloning new epoch %d for feed %s', highest_remote_epoch, self.feed_dir)
        # We don't clone shallow for new epochs, since we want all the mail there for the initial run
        self.clone_epoch(epoch=highest_remote_epoch, shallow=False)
        self.save_feed_state(epoch=highest_remote_epoch, success=True)
        self.store_epochs_info(remote_epoch_info)
        return self.STATUS_UPDATED
