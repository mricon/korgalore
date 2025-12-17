import json
import logging
from pathlib import Path
from typing import List, Tuple

from korgalore.pi_feed import PIFeed
from korgalore import run_git_command, GitError, PublicInboxError, ConfigurationError, StateError

logger = logging.getLogger('korgalore')

class LeiFeed(PIFeed):
    """Feed class for interacting with lei (local email interface) searches."""
    LEICMD: str = "lei"

    def __init__(self, feed_key: str, lei_url: str) -> None:
        # do a parent init
        self.known_searches: List[str] = list()
        self._load_known_searches()
        feed_dir = Path(lei_url[4:])  # Strip 'lei:' prefix
        if str(feed_dir) not in self.known_searches:
            raise ConfigurationError(f"LEI search '{feed_dir}' is not known.")

        super().__init__(feed_key, feed_dir)
        self.feed_type = 'lei'
        self.feed_url = lei_url

    def run_lei_command(self, args: List[str]) -> Tuple[int, bytes]:
        import subprocess

        cmd = [self.LEICMD]
        cmd += args

        try:
            result = subprocess.run(cmd, capture_output=True)
        except FileNotFoundError:
            raise PublicInboxError(f"LEI command '{self.LEICMD}' not found. Is it installed?")
        return result.returncode, result.stdout.strip()

    def get_latest_epoch_info(self) -> List[Tuple[int, str]]:
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
        args = ['ls-search', '-l', '-f', 'json']
        retcode, output = self.run_lei_command(args)
        if retcode != 0:
            raise PublicInboxError(f"LEI list searches failed: {output.decode()}")
        json_output = output.decode()
        ls_data = json.loads(json_output)
        # Only return the names of v2 searches
        for entry in ls_data:
            output = entry.get('output', '')
            if output.startswith('v2:'):
                self.known_searches.append(output[3:])

    def init_feed(self) -> None:
        logger.debug('Initializing LEI feed: %s', self.feed_dir)
        # We just need to save the feed state with the latest existing epoch
        self.save_feed_state()

    def update_feed(self) -> bool:
        logger.debug('Updating lei search: %s', self.feed_dir)
        leiargs = ['up', str(self.feed_dir)]
        retcode, output = self.run_lei_command(leiargs)
        if retcode != 0:
            raise PublicInboxError(f"LEI update failed: {output.decode()}")

        try:
            finfo = self.load_feed_state()
        except StateError:
            logger.debug('No existing feed state found, initializing feed: %s', self.feed_dir)
            self.init_feed()
            return False

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
            return True

        return updated
