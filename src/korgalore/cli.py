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
from korgalore.lei_service import LeiService
from korgalore import __version__, ConfigurationError, StateError, GitError, RemoteError

logger = logging.getLogger('korgalore')
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


def get_target(ctx: click.Context, identifier: str) -> Any:
    if identifier in ctx.obj['targets']:
        return ctx.obj['targets'][identifier]

    config = ctx.obj.get('config', {})
    targets = config.get('targets', {})
    if identifier not in targets:
        logger.critical('Target "%s" not found in configuration.', identifier)
        logger.critical('Known targets: %s', ', '.join(targets.keys()))
        raise click.Abort()

    details = targets[identifier]
    if details.get('type') != 'gmail':
        logger.critical('Target "%s" is not a Gmail target.', identifier)
        raise click.Abort()

    gs = get_gmail_service(identifier=identifier,
                            credentials_file=details.get('credentials', ''),
                            token_file=details.get('token', None))
    ctx.obj['targets'][identifier] = gs
    return gs


def get_gmail_service(identifier: str, credentials_file: str,
                      token_file: Optional[str]) -> GmailService:
    if not credentials_file:
        logger.critical('No credentials file specified for Gmail target: %s', identifier)
        raise click.Abort()
    if not token_file:
        cfgdir = get_xdg_config_dir()
        token_file = str(cfgdir / f'gmail-{identifier}-token.json')
    try:
        gmail_service = GmailService(identifier=identifier,
                                     credentials_file=credentials_file,
                                     token_file=token_file)
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort()

    return gmail_service


def load_config(cfgfile: Path) -> Dict[str, Any]:
    config: Dict[str, Any] = dict()

    if not cfgfile.exists():
        logger.error('Config file not found: %s', str(cfgfile))
        click.Abort()

    try:
        logger.debug('Loading config from %s', str(cfgfile))

        with open(cfgfile, 'rb') as cf:
            config = tomllib.load(cf)

        logger.debug('Config loaded with %s targets and %s sources',
                     len(config.get('targets', {})), len(config.get('sources', {})))

        return config

    except Exception as e:
        logger.error('Error loading config: %s', str(e))
        raise click.Abort()


def process_commits(listname: str, commits: List[str], gitdir: Path,
                    ctx: click.Context, max_count: int = 0) -> Tuple[int, str]:
    if max_count > 0 and len(commits) > max_count:
        # Take the last NN messages and discard the rest
        logger.info('Limiting to %d messages as requested', max_count)
        commits = commits[-max_count:]

    ls = ctx.obj['lore']
    cfg = ctx.obj.get('config', {})

    details = cfg['sources'][listname]
    target = details.get('target', '')
    try:
        gs = get_target(ctx, target)
    except click.Abort:
        logger.critical('Failed to process list "%s".', listname)
        raise ConfigurationError()

    last_commit = ''

    if logger.isEnabledFor(logging.DEBUG):
        hidden = True
    elif logger.isEnabledFor(logging.INFO):
        hidden = False
    else:
        hidden = True

    count = 0
    with click.progressbar(commits,
                            label=f'Uploading {listname}',
                            show_pos=True,
                            hidden=hidden) as bar:
        for at_commit in bar:
            try:
                raw_message = ls.get_message_at_commit(gitdir, at_commit)
            except (StateError, GitError) as e:
                logger.debug('Skipping commit %s: %s', at_commit, str(e))
                # Assuming non-m commit
                continue
            try:
                gs.import_message(raw_message, labels=details.get('labels', []))
                count += 1
            except RemoteError as re:
                logger.critical('Failed to upload message at commit %s: %s', at_commit, str(re))
                return count, last_commit
            ls.update_korgalore_info(gitdir=gitdir, latest_commit=at_commit, message=raw_message)
            last_commit = at_commit
            if logger.isEnabledFor(logging.DEBUG):
                msg = ls.parse_message(raw_message)
                logger.debug(' -> %s', msg.get('Subject', '(no subject)'))

    return count, last_commit


def process_lei_list(ctx: click.Context, listname: str,
                     details: Dict[str, Any], max_mail: int) -> int:
    # Make sure lei knows about this list
    # Placeholder for future LEI feed processing logic
    lei = ctx.obj['lei']
    if lei is None:
        lei = LeiService()
        ctx.obj['lei'] = lei
    feed = details.get('feed', '')[4:]  # Strip 'lei:' prefix
    if feed not in lei.known_searches:
        logger.critical('LEI search "%s" not known. Please create it first.', listname)
        raise click.Abort()
    feedpath = Path(feed)
    latest_epochs = lei.get_latest_epoch_info(feedpath)
    latest_epoch = max(lei.find_epochs(feedpath))
    try:
        known_epochs = lei.load_known_epoch_info(feedpath)
    except StateError:
        lei.save_epoch_info(list_dir=feedpath, epochs=latest_epochs)
        lei.update_korgalore_info(gitdir=feedpath / 'git' / f'{latest_epoch}.git')
        logger.info('Initialized: %s.', listname)
        return 0
    logger.debug('Running lei-up on list: %s', listname)
    lei.up_search(lei_name=feed)
    latest_epochs = lei.get_latest_epoch_info(feedpath)
    if known_epochs == latest_epochs:
        logger.debug('No updates for LEI list: %s', listname)
        return 0
    # XXX: this doesn't do the right thing with epoch rollover yet
    gitdir = feedpath / 'git' / f'{latest_epoch}.git'
    commits = lei.get_latest_commits_in_epoch(gitdir)
    if commits:
        logger.debug('Found %d new commits for list %s', len(commits), listname)
        count, last_commit = process_commits(listname=listname, commits=commits,
                                             gitdir=gitdir, ctx=ctx, max_count=max_mail)
        lei.save_epoch_info(list_dir=feedpath, epochs=latest_epochs)
        return count
    else:
        logger.debug('No new commits to process for LEI list %s', listname)
        return 0


def process_lore_list(ctx: click.Context, listname: str,
                      details: Dict[str, Any], max_mail: int) -> int:
    ls = ctx.obj['lore']
    if ls is None:
        data_dir = ctx.obj['data_dir']
        ls = LoreService(data_dir)
        ctx.obj['lore'] = ls
    latest_epochs = ls.get_epochs(details['feed'])
    count = 0

    data_dir = ctx.obj['data_dir']

    list_dir = data_dir / f'{listname}'
    if not list_dir.exists():
        ls.init_list(list_name=listname, list_dir=list_dir, pi_url=details['feed'])
        ls.store_epochs_info(list_dir=list_dir, epochs=latest_epochs)
        logger.info('Initialized: %s.', listname)
        return 0

    current_epochs: List[Tuple[int, str, str]] = list()
    try:
        current_epochs = ls.load_epochs_info(list_dir=list_dir)
    except StateError:
        pass

    if current_epochs == latest_epochs:
        logger.debug('No updates for lore list: %s', listname)
        return 0

    # Pull the highest epoch we have
    logger.debug('Running git pull on list: %s', listname)
    highest_epoch, gitdir, commits = ls.pull_highest_epoch(list_dir=list_dir)
    if commits:
        logger.debug('Found %d new commits for list %s', len(commits), listname)
        count, last_commit = process_commits(listname=listname, commits=commits,
                                             gitdir=gitdir, ctx=ctx, max_count=max_mail)
    else:
        last_commit = ''
        logger.debug('No new commits to process for list %s', listname)

    local = set(e[0] for e in current_epochs)
    remote = set(e[0] for e in latest_epochs)

    new_epochs = remote - local
    if new_epochs:
        # In theory, we could have more than one new epoch, for example if
        # someone hasn't run korgalore in a long time. This is almost certainly
        # not something anyone would want, because it would involve pulling a lot of data
        # that would take ages. So for now, we just pick the highest new epoch, which
        # will be correct in vast majority of cases.
        next_epoch = max(new_epochs)
        repo_url = f"{details['feed'].rstrip('/')}/git/{next_epoch}.git"
        tgt_dir = list_dir / 'git' / f'{next_epoch}.git'
        logger.debug('Cloning new epoch %d for list %s', next_epoch, listname)
        ls.clone_epoch(repo_url=repo_url, tgt_dir=tgt_dir, shallow=False)
        commits = ls.get_all_commits_in_epoch(tgt_dir)
        # attempt to respect max_mail across epoch boundaries
        remaining_mail = max_mail - count if max_mail > 0 else 0
        if remaining_mail <= 0:
            # Not clear what to do in this case, so we're just going to do max_mail for
            # the new epoch as well
            remaining_mail = max_mail
        new_count, last_commit = process_commits(listname=listname, commits=commits,
                                                 gitdir=tgt_dir, ctx=ctx, max_count=remaining_mail)
        count += new_count

    ls.store_epochs_info(list_dir=list_dir, epochs=latest_epochs)

    return count


@click.group()
@click.version_option(version=__version__)
@click_log.simple_verbosity_option(logger)
@click.option('--cfgfile', '-c', help='Path to configuration file.')
@click.option('-l', '--logfile', default=None, type=click.Path(), help='Path to log file.')
@click.pass_context
def main(ctx: click.Context, cfgfile: str, logfile: Optional[click.Path]) -> None:
    ctx.ensure_object(dict)

    # Load configuration file
    if not cfgfile:
        cfgdir = get_xdg_config_dir()
        cfgpath = cfgdir / 'korgalore.toml'
    else:
        cfgpath = Path(cfgfile)

    if logfile:
        file_handler = logging.FileHandler(str(logfile))
        file_handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # Only load config if we're not in edit-config mode
    if ctx.invoked_subcommand != 'edit-config':
        config = load_config(cfgpath)
        ctx.obj['config'] = config

    # Ensure XDG data directory exists
    data_dir = get_xdg_data_dir()
    ctx.obj['data_dir'] = data_dir

    logger.debug('Data directory: %s', data_dir)

    # We lazy-load these services as needed
    ctx.obj['targets'] = dict()

    ctx.obj['lore'] = None
    ctx.obj['lei'] = None


@main.command()
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Authenticate with Gmail."""
    config = ctx.obj.get('config', {})
    targets = config.get('targets', {})
    if not targets:
        logger.critical('No targets defined in configuration.')
        raise click.Abort()
    for identifier, details in targets.items():
        if details.get('type') != 'gmail':
            continue
        get_gmail_service(identifier=identifier,
                          credentials_file=details.get('credentials', ''),
                          token_file=details.get('token', None))
    logger.info('Authentication complete.')


@main.command()
@click.pass_context
def edit_config(ctx: click.Context) -> None:
    """Open the configuration file in the default editor."""
    # Get config file path
    cfgfile = ctx.parent.params.get('cfgfile') if ctx.parent else None
    if not cfgfile:
        cfgdir = get_xdg_config_dir()
        cfgpath = cfgdir / 'korgalore.toml'
    else:
        cfgpath = Path(cfgfile)

    # Create config file with example if it doesn't exist
    if not cfgpath.exists():
        logger.info('Configuration file does not exist. Creating example configuration at: %s', cfgpath)
        example_config = """### Targets ###

[targets.personal]
type = 'gmail'
credentials = '~/.config/korgalore/credentials.json'
# token = '~/.config/korgalore/token.json'

### Sources ###

# [sources.lkml]
# feed = 'https://lore.kernel.org/lkml'
# target = 'personal'
# labels = ['INBOX', 'UNREAD']
"""
        cfgpath.parent.mkdir(parents=True, exist_ok=True)
        cfgpath.write_text(example_config)

    # Open in editor
    logger.info('Editing configuration file: %s', cfgpath)
    click.edit(filename=str(cfgpath))
    logger.debug('Configuration file closed.')


@main.command()
@click.pass_context
@click.argument('target', type=str, nargs=1)
@click.option('--ids', '-i', is_flag=True, help='include id values')
def labels(ctx: click.Context, target: str, ids: bool = False) -> None:
    """List all available labels."""
    gs = get_target(ctx, ctx.params['target'])

    try:
        logger.debug('Fetching labels from Gmail')
        labels_list = gs.list_labels()

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
@click.option('--max-mail', '-m', default=0, help='maximum number of messages to pull (0 for all)')
@click.argument('listname', type=str, nargs=1, default=None)
def pull(ctx: click.Context, max_mail: int, listname: Optional[str]) -> None:
    cfg = ctx.obj.get('config', {})

    sources = cfg.get('sources', {})
    if listname:
        if listname not in sources:
            logger.critical('List "%s" not found in configuration.', listname)
            raise click.Abort()
        sources = {listname: sources[listname]}

    changes: List[Tuple[str, int]] = list()
    for listname, details in sources.items():
        logger.debug('Processing list: %s', listname)
        if details.get('feed', '').startswith('https:'):
            try:
                count = process_lore_list(ctx=ctx, listname=listname, details=details, max_mail=max_mail)
            except Exception as e:
                logger.critical('Failed to process lore list "%s": %s', listname, str(e))
                continue
            if count > 0:
                changes.append((listname, count))
        elif details.get('feed', '').startswith('lei:'):
            try:
                count = process_lei_list(ctx=ctx, listname=listname, details=details, max_mail=max_mail)
            except Exception as e:
                logger.critical('Failed to process LEI list "%s": %s', listname, str(e))
                continue
            if count > 0:
                changes.append((listname, count))
        else:
            logger.warning('Unknown feed type for list %s: %s', listname, details.get('feed'))
            continue
    if changes:
        logger.info('Pull complete with updates:')
        for listname, count in changes:
            logger.info('  %s: %d', listname, count)
    else:
        logger.info('Pull complete with no updates.')


if __name__ == '__main__':
    main()
