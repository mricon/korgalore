"""Command-line interface for korgalore."""

import os
import click
import tomllib
import logging
import click_log

from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from korgalore.gmail_service import GmailService
from korgalore.lore_service import LoreService
from korgalore import __version__

logger = logging.getLogger(__name__)
click_log.basic_config(logger)

def get_xdg_data_dir() -> Path:
    # Get XDG_DATA_HOME or default to ~/.local/share
    xdg_data_home = os.environ.get('XDG_DATA_HOME')
    if xdg_data_home:
        data_home = Path(xdg_data_home)
    else:
        data_home = Path.home() / '.local' / 'share'

    # Create korgalore subdirectory
    korgalore_data_dir = data_home / 'korgalore'

    # Create directory if it doesn't exist
    korgalore_data_dir.mkdir(parents=True, exist_ok=True)

    return korgalore_data_dir


def get_xdg_config_dir() -> Path:
    # Get XDG_CONFIG_HOME or default to ~/.config
    xdg_config_home = os.environ.get('XDG_CONFIG_HOME')
    if xdg_config_home:
        config_home = Path(xdg_config_home)
    else:
        config_home = Path.home() / '.config'

    # Create korgalore subdirectory
    korgalore_config_dir = config_home / 'korgalore'

    # Create directory if it doesn't exist
    korgalore_config_dir.mkdir(parents=True, exist_ok=True)

    return korgalore_config_dir


def load_config(cfgfile: Path) -> Dict[str, Any]:
    config: Dict[str, Any] = dict()

    if not cfgfile.exists():
        logger.error('Config file not found: %s', str(cfgfile))
        click.Abort()

    try:
        logger.debug('Loading config from %s', str(cfgfile))

        with open(cfgfile, 'rb') as cf:
            config = tomllib.load(cf)

        logger.debug('Config loaded with %s keys', len(config.keys()))

        return config

    except Exception as e:
        logger.error('Error loading config: %s', str(e))
        raise click.Abort()


def translate_labels(gs: GmailService, cfg: Dict[str, Any]) -> Dict[str, Any]:
    # Translate label names to their corresponding IDs
    # Get all labels from Gmail
    label_map = {label['name']: label['id'] for label in gs.list_labels()}
    for listname, details in cfg.get('sources', {}).items():
        details['label_ids'] = list()
        for label in details.get('labels', []):
            label_id = label_map.get(label, None)
            if label_id is None:
                raise ValueError(f"Label '{label}' for list '{listname}' not found in Gmail.")
            details['label_ids'].append(label_id)
    return cfg


def process_commits(listname: str, commits: List[str], gitdir: Path, ctx: click.Context) -> str:
    ls = ctx.obj['lore']
    gs = ctx.obj['gmail']
    cfg = ctx.obj.get('config', {})
    details = cfg['sources'][listname]

    last_commit = ''

    if logger.isEnabledFor(logging.DEBUG):
        hidden = True
    elif logger.isEnabledFor(logging.INFO):
        hidden = False
    else:
        hidden = True

    with click.progressbar(commits,
                            label=f'Uploading {listname}',
                            show_pos=True,
                            hidden=hidden) as bar:
        for at_commit in bar:
            try:
                raw_message = ls.get_message_at_commit(gitdir, at_commit)
            except FileNotFoundError:
                # Assuming non-m commit
                continue
            try:
                gs.import_message(raw_message, label_ids=details.get('label_ids', None))
            except RuntimeError as re:
                logger.critical('Failed to upload message at commit %s: %s', at_commit, str(re))
                raise click.Abort()
            ls.update_piper_info(gitdir=gitdir, latest_commit=at_commit, message=raw_message)
            last_commit = at_commit
            if logger.isEnabledFor(logging.DEBUG):
                msg = ls.parse_message(raw_message)
                logger.debug(' -> %s', msg.get('Subject', '(no subject)'))

    return last_commit


@click.group()
@click.version_option(version=__version__)
@click_log.simple_verbosity_option(logger)
@click.option('--cfgfile', '-c', help='Path to configuration file.')
@click.pass_context
def main(ctx: click.Context, cfgfile: str) -> None:
    ctx.ensure_object(dict)

    # Load configuration file
    if not cfgfile:
        cfgdir = get_xdg_config_dir()
        cfgpath = cfgdir / 'korgalore.toml'
    else:
        cfgpath = Path(cfgfile)

    config = load_config(cfgpath)
    ctx.obj['config'] = config

    # Ensure XDG data directory exists
    data_dir = get_xdg_data_dir()
    ctx.obj['data_dir'] = data_dir

    logger.debug('Verbose mode enabled')
    logger.debug('Data directory: %s', data_dir)

    try:
        ctx.obj['gmail'] = GmailService(cfgdir)
    except FileNotFoundError as fe:
        logger.critical('Error: %s', str(fe))
        logger.critical('Please run "korgalore auth" to authenticate first.')
        raise click.Abort()
    ctx.obj['lore'] = LoreService(data_dir)


@main.command()
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Authenticate with Gmail API."""
    gmail = ctx.obj['gmail']

    try:
        logger.info('Starting authentication process')
        gmail.authenticate()
        logger.info('Authentication successful')
    except Exception as e:
        logger.critical('Authentication failed: %s', str(e))
        raise click.Abort()


@main.command()
@click.pass_context
@click.option('--ids', '-i', is_flag=True, help='include id values')
def labels(ctx: click.Context, ids: bool = False) -> None:
    """List all available labels."""
    gmail = ctx.obj['gmail']

    try:
        logger.debug('Fetching labels from Gmail')
        labels_list = gmail.list_labels()

        if not labels_list:
            logger.info("No labels found.")
            return

        logger.debug('Found %d labels', len(labels_list))
        logger.info('Available labels:')
        for label in labels_list:
            if ids:
                logger.info(f"  - {label['name']} (ID: {label['id']})")
            else:
                logger.info(f"  - {label['name']}")

    except Exception as e:
        logger.critical('Failed to fetch labels: %s', str(e))
        raise click.Abort()


@main.command()
@click.pass_context
@click.option('--max', '-m', default=0, help='maximum number of messages to pull (0 for all)')
@click.argument('listname', type=str, nargs=1, default=None)
def pull(ctx: click.Context, max: int, listname: Optional[str]) -> None:
    """Pull updates from all subscribed mailing lists."""
    ls = ctx.obj['lore']
    gs = ctx.obj['gmail']
    data_dir = ctx.obj['data_dir']

    cfg = ctx.obj.get('config', {})
    try:
        cfg = translate_labels(gs, cfg)
    except ValueError as ve:
        logger.critical('Configuration error: %s', str(ve))
        raise click.Abort()

    sources = cfg.get('sources', {})
    if listname:
        if listname not in sources:
            logger.critical('List "%s" not found in configuration.', listname)
            raise click.Abort()
        sources = {listname: sources[listname]}

    for listname, details in sources.items():
        logger.debug('Processing list: %s', listname)
        latest_epochs = ls.get_epochs(details['feed'])

        list_dir = data_dir / f'{listname}'
        if not list_dir.exists():
            logger.info('List directory %s does not exist. Initializing.', list_dir)
            ls.init_list(list_name=listname, list_dir=list_dir, pi_url=details['feed'])
            ls.store_epochs_info(list_dir=list_dir, epochs=latest_epochs)
            continue

        current_epochs: List[Tuple[int, str, str]] = list()
        try:
            current_epochs = ls.load_epochs_info(list_dir=list_dir)
        except FileNotFoundError:
            pass

        if current_epochs == latest_epochs:
            logger.debug('No updates for list: %s', listname)
            continue

        # Pull the highest epoch we have
        highest_epoch, gitdir, commits = ls.pull_highest_epoch(list_dir=list_dir)
        if commits:
            logger.debug('Found %d new commits for list %s', len(commits), listname)

            if max > 0 and len(commits) > max:
                # Take the last NN messages and discard the rest
                logger.info('Limiting to %d messages as requested', max)
                commits = commits[:max]

            last_commit = process_commits(listname=listname, commits=commits, gitdir=gitdir, ctx=ctx)
        else:
            last_commit = ''
            logger.debug('No new commits to process for list %s', listname)

        # XXX: check for epoch rollover
        ls.store_epochs_info(list_dir=list_dir, epochs=latest_epochs)
        if last_commit:
            ls.reshallow(gitdir=gitdir, since_commit=last_commit)


if __name__ == '__main__':
    main()
