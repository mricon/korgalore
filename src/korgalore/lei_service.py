import json
import logging
from pathlib import Path
from typing import List, Tuple

from korgalore.pi_service import PIService
from korgalore import GitError, PublicInboxError, StateError

logger = logging.getLogger('korgalore')

class LeiService(PIService):
    """Service for interacting with lore.kernel.org public-inbox archives."""
    LEICMD: str = "lei"

    def __init__(self) -> None:
        # do a parent init
        super().__init__()
        self.known_searches: List[str] = list()
        self._load_known_searches()

    def run_lei_command(self, args: List[str]) -> Tuple[int, bytes]:
        import subprocess

        cmd = [self.LEICMD]
        cmd += args

        try:
            result = subprocess.run(cmd, capture_output=True)
        except FileNotFoundError:
            raise PublicInboxError(f"LEI command '{self.LEICMD}' not found. Is it installed?")
        return result.returncode, result.stdout.strip()

    def get_latest_epoch_info(self, list_dir: Path) -> List[Tuple[int, str]]:
        epochs = self.find_epochs(list_dir)
        epoch_info: List[Tuple[int, str]] = list()
        for epoch in epochs:
            epoch_dir = list_dir / 'git' / f'{epoch}.git'
            gitargs = ['show-ref']
            retcode, output = self.run_git_command(str(epoch_dir), gitargs)
            if retcode != 0:
                raise GitError(f"Git show-ref failed: {output.decode()}")
            # It's just one ref in lei repos
            refdata = output.decode()
            logger.debug('Epoch %d refdata: %s', epoch, refdata)
            epoch_info.append((epoch, refdata))
        return epoch_info

    def load_known_epoch_info(self, list_dir: Path) -> List[Tuple[int, str]]:
        epochs_file = list_dir / 'epochs.json'
        if not epochs_file.exists():
            raise StateError(f"Epochs file {epochs_file} does not exist.")
        with open(epochs_file, 'r') as ef:
            epochs_data = json.load(ef)
        epochs: List[Tuple[int, str]] = list()
        for entry in epochs_data:
            epochs.append((entry['epoch'], entry['refdata']))
        return epochs

    def save_epoch_info(self, list_dir: Path, epochs: List[Tuple[int, str]]) -> None:
        epochs_file = list_dir / 'epochs.json'
        epochs_info = list()
        for enum, refdata in epochs:
            epochs_info.append({
                'epoch': enum,
                'refdata': refdata
            })
        with open(epochs_file, 'w') as ef:
            json.dump(epochs_info, ef, indent=2)

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

    def up_search(self, lei_name: str) -> None:
        leiargs = ['up', lei_name]
        retcode, output = self.run_lei_command(leiargs)
        if retcode != 0:
            raise PublicInboxError(f"LEI update failed: {output.decode()}")