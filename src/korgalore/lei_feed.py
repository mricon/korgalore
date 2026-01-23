import json
import logging
from pathlib import Path
from typing import List, Tuple

from korgalore.pi_feed import PIFeed
from korgalore import run_git_command, run_lei_command, GitError, PublicInboxError, ConfigurationError, StateError

logger = logging.getLogger('korgalore')

class LeiFeed(PIFeed):
    """Feed class for interacting with lei (local email interface) searches."""

    def __init__(self, feed_key: str, lei_url: str) -> None:
        """Initialize a LeiFeed instance.

        Args:
            feed_key: Unique identifier for this feed.
            lei_url: LEI URL in the format 'lei:/path/to/search'.

        Raises:
            ConfigurationError: If the LEI search is not known to lei.
        """
        self.known_searches: List[str] = list()
        self._load_known_searches()
        feed_dir = Path(lei_url[4:])  # Strip 'lei:' prefix
        if str(feed_dir) not in self.known_searches:
            raise ConfigurationError(f"LEI search '{feed_dir}' is not known.")

        super().__init__(feed_key, feed_dir)
        self.feed_type = 'lei'
        self.feed_url = lei_url

    def get_latest_epoch_info(self) -> List[Tuple[int, str]]:
        """Get current ref information for all epochs.

        Returns:
            List of (epoch_number, refdata_string) tuples for each epoch.

        Raises:
            GitError: If git show-ref fails on any epoch.
        """
        epochs = self.find_epochs()
        epoch_info: List[Tuple[int, str]] = list()
        for epoch in epochs:
            epoch_dir = self.get_gitdir(epoch)
            gitargs = ['show-ref']
            retcode, output = run_git_command(str(epoch_dir), gitargs)
            if retcode != 0:
                raise GitError(f"Git show-ref failed: {output.decode()}")
            # It's just one ref in lei repos
            refdata = output.decode()
            logger.debug('Epoch %d refdata: %s', epoch, refdata)
            epoch_info.append((epoch, refdata))
        return epoch_info

    def _load_known_searches(self) -> None:
        """Load the list of known lei searches into self.known_searches.

        Queries lei for all searches and filters to only include v2 format searches.

        Raises:
            PublicInboxError: If the lei ls-search command fails.
        """
        args = ['ls-search', '-l', '-f', 'json']
        retcode, output = run_lei_command(args)
        if retcode != 0:
            raise PublicInboxError(f"LEI list searches failed: {output.decode()}")
        json_output = output.decode()
        ls_data = json.loads(json_output)
        # Only return the names of v2 searches
        for entry in ls_data:
            output = entry.get('output', '')
            if output.startswith('v2:'):
                self.known_searches.append(output[3:])

    def init_feed(self, from_start: bool = False) -> None:
        """Initialize a new LEI feed by saving the current state.

        Args:
            from_start: If True, initialize from the first commit so all existing
                        messages will be delivered on the next pull. If False
                        (default), initialize from the latest commit so only new
                        messages will be delivered.
        """
        logger.debug('Initializing LEI feed: %s (from_start=%s)', self.feed_dir, from_start)
        epoch = self.get_highest_epoch()
        if from_start:
            # Use first commit so all messages are treated as "new"
            latest_commit = self.get_first_commit(epoch)
            logger.debug('Initializing from first commit: %s', latest_commit)
        else:
            latest_commit = None  # Will use get_top_commit() in save_feed_state()
        self.save_feed_state(epoch=epoch, latest_commit=latest_commit)

    def update_feed(self) -> int:
        """Update the LEI search and check for new messages.

        Runs 'lei up' to update the search, then checks for new epochs
        or updated refs.

        Returns:
            Status constant: STATUS_NOCHANGE, STATUS_UPDATED, or STATUS_INITIALIZED.

        Raises:
            PublicInboxError: If the lei update command fails.
        """
        logger.debug('Updating lei search: %s', self.feed_dir)
        leiargs = ['up', str(self.feed_dir)]
        retcode, output = run_lei_command(leiargs)
        if retcode != 0:
            raise PublicInboxError(f"LEI update failed: {output.decode()}")

        try:
            finfo = self.load_feed_state()
        except StateError:
            self.init_feed()
            return self.STATUS_INITIALIZED

        known_epochs = [int(e) for e in finfo['epochs'].keys()]
        highest_known_epoch = max(known_epochs)

        updated = self.feed_updated(highest_known_epoch)

        self.save_feed_state(
            epoch=highest_known_epoch,
            success=True
        )

        # Do we have a new epoch?
        highest_existing_epoch = self.get_highest_epoch()
        if highest_existing_epoch > highest_known_epoch:
            logger.debug('New epoch detected for LEI search %s: %d',
                        self.feed_dir, highest_existing_epoch)
            self.save_feed_state(
                epoch=highest_existing_epoch,
                success=True
            )
            return self.STATUS_UPDATED

        return self.STATUS_UPDATED if updated else self.STATUS_NOCHANGE
