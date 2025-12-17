"""Command-line interface for korgalore."""

import os
import re
import hashlib
import click
import tomllib
import logging
import click_log
import requests

from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional, Union
from korgalore.lore_feed import LoreFeed
from korgalore.lei_feed import LeiFeed
from korgalore.gmail_target import GmailTarget
from korgalore.maildir_target import MaildirTarget
from korgalore.jmap_target import JmapTarget
from korgalore.imap_target import ImapTarget
from korgalore import __version__, ConfigurationError, StateError, GitError, RemoteError

logger = logging.getLogger('korgalore')
click_log.basic_config(logger)

REQSESSION: Optional[requests.Session] = None

def get_reqsession() -> requests.Session:
    global REQSESSION
    if REQSESSION is None:
        REQSESSION = LoreFeed.get_reqsession()
    return REQSESSION

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
    target_type = details.get('type', '')

    # Instantiate based on type
    service: Any
    if target_type == 'gmail':
        service = get_gmail_target(
            identifier=identifier,
            credentials_file=details.get('credentials', ''),
            token_file=details.get('token', None)
        )
    elif target_type == 'maildir':
        service = get_maildir_target(
            identifier=identifier,
            maildir_path=details.get('path', '')
        )
    elif target_type == 'jmap':
        service = get_jmap_target(
            identifier=identifier,
            server=details.get('server', ''),
            username=details.get('username', ''),
            token=details.get('token', None),
            token_file=details.get('token_file', None),
            timeout=details.get('timeout', 60)
        )
    elif target_type == 'imap':
        service = get_imap_target(
            identifier=identifier,
            server=details.get('server', ''),
            username=details.get('username', ''),
            folder=details.get('folder', 'INBOX'),
            password=details.get('password', None),
            password_file=details.get('password_file', None),
            timeout=details.get('timeout', 60)
        )
    else:
        logger.critical('Unknown target type "%s" for target "%s".', target_type, identifier)
        logger.critical('Supported types: gmail, maildir, jmap, imap')
        raise click.Abort()

    ctx.obj['targets'][identifier] = service
    return service


def get_gmail_target(identifier: str, credentials_file: str,
                     token_file: Optional[str]) -> GmailTarget:
    if not credentials_file:
        logger.critical('No credentials file specified for Gmail target: %s', identifier)
        raise click.Abort()
    if not token_file:
        cfgdir = get_xdg_config_dir()
        token_file = str(cfgdir / f'gmail-{identifier}-token.json')
    try:
        gt = GmailTarget(identifier=identifier,
                         credentials_file=credentials_file,
                         token_file=token_file)
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort()

    return gt


def get_maildir_target(identifier: str, maildir_path: str) -> MaildirTarget:
    if not maildir_path:
        logger.critical('No maildir path specified for target: %s', identifier)
        raise click.Abort()

    try:
        mt = MaildirTarget(identifier=identifier, maildir_path=maildir_path)
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort()

    return mt


def get_jmap_target(identifier: str, server: str, username: str,
                    token: Optional[str], token_file: Optional[str],
                    timeout: int) -> JmapTarget:
    if not server:
        logger.critical('No server specified for JMAP target: %s', identifier)
        raise click.Abort()

    if not username:
        logger.critical('No username specified for JMAP target: %s', identifier)
        raise click.Abort()

    if not token and not token_file:
        logger.critical('No token or token_file specified for JMAP target: %s', identifier)
        logger.critical('Generate a token at your JMAP provider (e.g., Fastmail Settings → Integrations)')
        raise click.Abort()

    try:
        jt = JmapTarget(
            identifier=identifier,
            server=server,
            username=username,
            token=token,
            token_file=token_file,
            timeout=timeout
        )
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort()

    return jt


def get_imap_target(identifier: str, server: str, username: str,
                    folder: str, password: Optional[str],
                    password_file: Optional[str], timeout: int) -> ImapTarget:
    if not server:
        logger.critical('No server specified for IMAP target: %s', identifier)
        raise click.Abort()

    if not username:
        logger.critical('No username specified for IMAP target: %s', identifier)
        raise click.Abort()

    if not password and not password_file:
        logger.critical('No password or password_file specified for IMAP target: %s', identifier)
        logger.critical('Either provide password directly or use password_file for security')
        raise click.Abort()

    try:
        it = ImapTarget(
            identifier=identifier,
            server=server,
            username=username,
            folder=folder,
            password=password,
            password_file=password_file,
            timeout=timeout
        )
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort()

    return it


def resolve_feed_url(feed_value: str, config: Dict[str, Any]) -> str:
    # If it's already a URL, return as-is
    if feed_value.startswith('https:') or feed_value.startswith('lei:'):
        return feed_value

    # Otherwise, look it up in the feeds section
    feeds = config.get('feeds', {})
    if feed_value not in feeds:
        logger.critical('Feed "%s" not found in configuration.', feed_value)
        logger.critical('Known feeds: %s', ', '.join(feeds.keys()))
        raise ConfigurationError(f'Feed "{feed_value}" not found in configuration')

    feed_config = feeds[feed_value]
    feed_url: str = feed_config.get('url', '')

    if not feed_url:
        logger.critical('Feed "%s" has no URL configured.', feed_value)
        raise ConfigurationError(f'Feed "{feed_value}" has no URL configured')

    logger.debug('Resolved feed "%s" to URL: %s', feed_value, feed_url)
    return feed_url


def get_feed_identifier(feed_value: str, config: Dict[str, Any]) -> Optional[str]:
    """Get a stable identifier for a feed to use as directory name.

    Args:
        feed_value: The feed value from delivery config (name or URL)
        config: Full configuration dict

    Returns:
        Directory name to use for this feed, or None for LEI feeds (handled separately)
    """
    # Named feed: use the feed name as directory
    if not (feed_value.startswith('https:') or feed_value.startswith('http:') or feed_value.startswith('lei:')):
        return feed_value

    # LEI path: handled separately in process_lei_delivery
    if feed_value.startswith('lei:'):
        return None

    # Direct URL: sanitize for directory name
    # https://lore.kernel.org/lkml → lore.kernel.org-lkml
    url_without_scheme = feed_value.replace('https://', '').replace('http://', '')

    # Replace special characters with hyphens
    sanitized = re.sub(r'[^a-zA-Z0-9_.-]', '-', url_without_scheme)

    # Remove trailing slashes, dots, and hyphens
    sanitized = sanitized.strip('-./')

    # Handle very long URLs (filesystem limit ~255 chars)
    if len(sanitized) > 200:
        # Use hash-based name for very long URLs
        url_hash = hashlib.sha256(feed_value.encode()).hexdigest()[:16]
        sanitized = f'feed-{url_hash}'
        logger.debug('Feed URL too long, using hash-based directory name: %s', sanitized)

    return sanitized


def load_config(cfgfile: Path) -> Dict[str, Any]:
    config: Dict[str, Any] = dict()

    if not cfgfile.exists():
        logger.error('Config file not found: %s', str(cfgfile))
        click.Abort()

    try:
        logger.debug('Loading config from %s', str(cfgfile))

        with open(cfgfile, 'rb') as cf:
            config = tomllib.load(cf)

        # Backward compatibility: convert 'sources' to 'deliveries'
        if 'sources' in config and 'deliveries' not in config:
            logger.debug('Converting legacy "sources" to "deliveries" in config')
            config['deliveries'] = config['sources']
            del config['sources']

        logger.debug('Config loaded with %s targets, %s deliveries, and %s feeds',
                     len(config.get('targets', {})), len(config.get('deliveries', {})),
                     len(config.get('feeds', {})))

        return config

    except Exception as e:
        logger.error('Error loading config: %s', str(e))
        raise click.Abort()


def retry_failed_commits(feed_dir: Path, pi_feed: Union[LeiFeed, LoreFeed], target_service: Any,
                         labels: List[str], delivery_name: str) -> None:
    failed_commits = pi_feed.get_failed_commits_for_delivery(delivery_name)

    if not failed_commits:
        return

    logger.info('Retrying %d previously failed commits', len(failed_commits))

    for epoch, commit_hash in failed_commits:
        try:
            raw_message = pi_feed.get_message_at_commit(epoch, commit_hash)
        except (StateError, GitError) as e:
            # XXX: did the feed get rebased? Skip for now, but handle later.
            logger.debug('Skipping retry of commit %s: %s', commit_hash, str(e))
            continue

        try:
            target_service.import_message(raw_message, labels=labels)
            logger.debug('Successfully retried commit %s', commit_hash)
            pi_feed.mark_successful_delivery(delivery_name, epoch, commit_hash)
        except RemoteError:
            pi_feed.mark_failed_delivery(delivery_name, epoch, commit_hash)

    # Save updated tracking files
    pi_feed.feed_unlock()


def deliver_commit(delivery_name: str, target: Any, feed: Union[LeiFeed, LoreFeed], epoch: int, commit: str,
                   labels: List[str], was_failing: bool = False) -> bool:
    """Deliver a single message to the target."""
    raw_message: Optional[bytes] = None
    try:
        raw_message = feed.get_message_at_commit(epoch, commit)
        target.connect()
        if logger.isEnabledFor(logging.DEBUG):
            subject = feed.get_subject_at_commit(epoch, commit)
            logger.debug(' -> %s', subject)
        target.import_message(raw_message, labels=labels)
        feed.mark_successful_delivery(delivery_name, epoch, commit, was_failing=was_failing)
        return True
    except Exception as e:
        logger.debug('Failed to deliver commit %s from epoch %d: %s', commit, epoch, str(e))
        feed.mark_failed_delivery(delivery_name, epoch, commit)
        # Only save delivery info if we successfully retrieved the message
        if raw_message is not None:
            feed.save_delivery_info(delivery_name, epoch, latest_commit=commit, message=raw_message)
        return False


def normalize_feed_key(feed_url: str) -> str:
    if feed_url.startswith('https://lore.kernel.org/'):
        # Extract list name from URL
        return feed_url.replace('https://lore.kernel.org/', '').strip('/')
    elif feed_url.startswith('lei:'):
        # Keep full lei path as key
        return feed_url
    else:
        # For unknown types, use URL as-is
        return feed_url

def get_feed_for_delivery(delivery_details: Dict[str, Any], ctx: click.Context) -> Union[LeiFeed, LoreFeed]:
    config = ctx.obj.get('config', {})
    feed_value = delivery_details.get('feed', '')
    if not feed_value:
        raise ConfigurationError('No feed specified for delivery.')
    feed_url = resolve_feed_url(feed_value, config)
    feed_key = normalize_feed_key(feed_url)
    feeds = ctx.obj.get('feeds', {})  # type: Dict[str, Union[LeiFeed, LoreFeed]]
    if feed_key in feeds:
        return feeds[feed_key]

    if feed_url.startswith('https:'):
        # Lore feed
        data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())
        feed_dir = data_dir / feed_key
        lore_feed = LoreFeed(feed_key, feed_dir, feed_url, reqsession=get_reqsession())
        feeds[feed_key] = lore_feed
        return lore_feed
    elif feed_url.startswith('lei:'):
        # LEI feed
        lei_feed = LeiFeed(feed_key, feed_url)
        feeds[feed_key] = lei_feed
        return lei_feed
    else:
        logger.critical('Unknown feed type for delivery: %s', feed_url)
        raise ConfigurationError(f'Unknown feed type for delivery: {feed_url}')


def map_deliveries(ctx: click.Context, deliveries: Dict[str, Any]) -> None:
    # 'deliveries' is a mapping: delivery_name -> Tuple[feed_instance, target_instance, labels]
    dmap: Dict[str, Tuple[Union[LeiFeed, LoreFeed], Any, List[str]]] = dict()
    logger.debug('Mapping deliveries to their feeds and targets')
    # Pre-map deliveries to their feeds and targets for later use.
    for delivery_name, details in deliveries.items():
        # Map feed
        feed = get_feed_for_delivery(details, ctx)
        # Map target
        target_name = details.get('target', '')
        if not target_name:
            logger.critical('No target specified for delivery: %s', delivery_name)
            raise ConfigurationError(f'No target specified for delivery: {delivery_name}')
        target = get_target(ctx, target_name)
        # Lock for the entire duration
        dmap[delivery_name] = (feed, target, details.get('labels', []))
    ctx.obj['deliveries'] = dmap


def lock_all_feeds(ctx: click.Context) -> None:
    feeds = ctx.obj.get('feeds', {})  # type: Dict[str, Union[LeiFeed, LoreFeed]]
    for feed_key in feeds.keys():
        feed = feeds[feed_key]
        feed.feed_lock()


def unlock_all_feeds(ctx: click.Context) -> None:
    feeds = ctx.obj.get('feeds', {})  # type: Dict[str, Union[LeiFeed, LoreFeed]]
    for feed_key in feeds.keys():
        feed = feeds[feed_key]
        feed.feed_unlock()


def update_all_feeds(ctx: click.Context) -> List[str]:
    updated_feeds: List[str] = []
    feeds = ctx.obj.get('feeds', {})  # type: Dict[str, Union[LeiFeed, LoreFeed]]

    with click.progressbar(feeds.keys(),
                           label='Updating feeds',
                           show_pos=True,
                           item_show_func=lambda x: x in feeds and str(feeds[x].feed_url) or x,
                           hidden=ctx.obj['hide_bar']) as bar:
        for feed_key in bar:
            feed = feeds[feed_key]
            updated = feed.update_feed()
            if updated:
                updated_feeds.append(feed_key)

    return updated_feeds


def retry_all_failed_deliveries(ctx: click.Context) -> None:
    # 'deliveries' is a mapping: delivery_name -> Tuple[feed_instance, target_instance, labels]
    deliveries = ctx.obj['deliveries']
    retry_list: List[Tuple[str, Any, Union[LeiFeed, LoreFeed], int, str, List[str]]] = list()
    for delivery_name, (feed, target, labels) in deliveries.items():
        to_retry = feed.get_failed_commits_for_delivery(delivery_name)
        if not to_retry:
            logger.debug('No failed commits to retry for delivery: %s', delivery_name)
            continue
        for epoch, commit in to_retry:
            retry_list.append((delivery_name, target, feed, epoch, commit, labels))
    if not retry_list:
        logger.debug('No failed commits to retry for any delivery.')
        return

    with click.progressbar(retry_list,
                           label='Reattempting delivery',
                           show_pos=True,
                           hidden=ctx.obj['hide_bar']) as bar:
        for (delivery_name, target, feed, epoch, commit, labels) in bar:
            deliver_commit(delivery_name, target, feed, epoch, commit, labels, was_failing=True)


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

    # We lazy-load these
    # 'targets' is a mapping: target identifier -> target instance
    ctx.obj['targets'] = dict()
    # 'feeds' is a mapping: feed_key -> feed instance
    ctx.obj['feeds'] = dict()
    # 'deliveries' is a mapping: delivery_name -> Tuple[feed_instance, target_instance, labels]
    ctx.obj['deliveries'] = dict()

    # Hide progress bar at the DEBUG level
    if logger.isEnabledFor(logging.DEBUG):
        ctx.obj['hide_bar'] = True
    else:
        ctx.obj['hide_bar'] = False


@main.command()
@click.argument('target', required=False)
@click.pass_context
def auth(ctx: click.Context, target: Optional[str]) -> None:
    """Authenticate with configured targets.

    If TARGET is specified, authenticate only that target.
    If TARGET is omitted, authenticate all targets that require authentication.
    """
    # Target types that don't require authentication
    NO_AUTH_TARGETS = {'maildir'}

    config = ctx.obj.get('config', {})
    targets = config.get('targets', {})
    if not targets:
        logger.critical('No targets defined in configuration.')
        raise click.Abort()

    # If specific target requested, validate it exists
    if target:
        if target not in targets:
            logger.critical('Target "%s" not found in configuration.', target)
            logger.critical('Known targets: %s', ', '.join(targets.keys()))
            raise click.Abort()

        # Check if target requires authentication
        target_type = targets[target].get('type', '')
        if target_type in NO_AUTH_TARGETS:
            logger.warning('Target "%s" (type: %s) does not require authentication.', target, target_type)
            return

        # Authenticate only the specified target
        auth_targets = [(target, targets[target])]
    else:
        # Authenticate all targets that require authentication
        auth_targets = []
        for identifier, details in targets.items():
            target_type = details.get('type', '')
            if target_type in NO_AUTH_TARGETS:
                logger.debug('Skipping target that does not require authentication: %s (type: %s)',
                            identifier, target_type)
                continue
            auth_targets.append((identifier, details))

    if not auth_targets:
        logger.warning('No targets requiring authentication found.')
        return

    for identifier, details in auth_targets:
        target_type = details.get('type', '')

        # Instantiate target to trigger authentication
        try:
            ts = get_target(ctx, identifier)
            ts.connect()
            logger.info('Authenticated target: %s (type: %s)', identifier, target_type)
        except click.Abort:
            logger.error('Failed to authenticate target: %s', identifier)
            raise

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

### Deliveries ###

# [deliveries.lkml]
# feed = 'https://lore.kernel.org/lkml'
# target = 'personal'
# labels = ['INBOX', 'UNREAD']
"""
        cfgpath.parent.mkdir(parents=True, exist_ok=True)
        cfgpath.write_text(example_config)
    else:
        # Convert legacy 'sources' to 'deliveries' in existing config file
        content = cfgpath.read_text()
        if '[sources.' in content or '### Sources ###' in content:
            logger.debug('Converting legacy "sources" to "deliveries" in config file')
            content = content.replace('[sources.', '[deliveries.')
            content = content.replace('### Sources ###', '### Deliveries ###')
            cfgpath.write_text(content)
            logger.info('Converted legacy "sources" to "deliveries" in config file')

    # Open in editor
    logger.info('Editing configuration file: %s', cfgpath)
    click.edit(filename=str(cfgpath))
    logger.debug('Configuration file closed.')


@main.command()
@click.pass_context
@click.argument('target', type=str, nargs=1)
@click.option('--ids', '-i', is_flag=True, help='include id values')
def labels(ctx: click.Context, target: str, ids: bool = False) -> None:
    """List all available labels (Gmail targets only)."""
    gs = get_target(ctx, ctx.params['target'])

    # Check if target supports labels
    if not hasattr(gs, 'list_labels'):
        logger.warning('Target "%s" does not support labels (maildir targets ignore labels).',
                      target)
        return

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
@click.option('--no-update', '-n', is_flag=True, help='skip feed updates (useful with --force)')
@click.option('--force', '-f', is_flag=True, help='run deliveries even if no apparent updates')
@click.argument('delivery_name', type=str, nargs=1, default=None)
def pull(ctx: click.Context, max_mail: int, no_update: bool, force: bool, delivery_name: Optional[str]) -> None:
    """Pull messages from configured lore and LEI deliveries."""
    cfg = ctx.obj.get('config', {})

    # Load deliveries to process
    deliveries = cfg.get('deliveries', {})
    if delivery_name:
        if delivery_name not in deliveries:
            logger.critical('Delivery "%s" not found in configuration.', delivery_name)
            raise click.Abort()
        deliveries = {delivery_name: deliveries[delivery_name]}

    # Collect unique feeds from all deliveries
    map_deliveries(ctx, deliveries)
    lock_all_feeds(ctx)
    # Retry all previously failed deliveries, if any
    retry_all_failed_deliveries(ctx)
    if no_update:
        logger.debug('No-update flag set, skipping feed updates')
        updated_feeds = list()
    else:
        updated_feeds = update_all_feeds(ctx)
    run_deliveries: List[str] = list()
    if not force:
        logger.debug('Updated feeds: %s', ', '.join(updated_feeds))
        for feed_key in updated_feeds:
            # 'deliveries' is a mapping: delivery_name -> Tuple[feed_instance, target_instance, labels]
            for delivery_name in ctx.obj['deliveries'].keys():
                feed = ctx.obj['deliveries'][delivery_name][0]
                if feed.feed_key == feed_key:
                    run_deliveries.append(delivery_name)
    else:
        # If force is specified, treat all feeds as updated
        logger.debug('Force flag set, treating all feeds as updated')
        run_deliveries = list(deliveries.keys())

    logger.debug('Deliveries to run: %s', ', '.join(run_deliveries))

    if not run_deliveries:
        logger.info('No feed updates available.')
        unlock_all_feeds(ctx)
        return

    # Build a worklist of updates per target
    by_target: Dict[str, List[str]] = dict()
    for delivery_name in run_deliveries:
        target_name = ctx.obj['deliveries'][delivery_name][1].identifier
        if target_name not in by_target:
            by_target[target_name] = list()
        by_target[target_name].append(delivery_name)

    changes: Dict[str, int] = dict()

    # Process deliveries now
    for target_name, delivery_names in by_target.items():
        logger.debug('Processing deliveries for target: %s', target_name)
        run_list: List[Tuple[str, Any, Union[LeiFeed, LoreFeed], int, str, List[str]]] = list()
        for delivery_name in delivery_names:
            feed, target, labels = ctx.obj['deliveries'][delivery_name]
            commits = feed.get_latest_commits_for_delivery(delivery_name)
            if not commits:
                logger.debug('No new commits for delivery: %s', delivery_name)
                continue
            for epoch, commit in commits:
                run_list.append((delivery_name, target, feed, epoch, commit, labels))
        if not run_list:
            logger.debug('No deliveries with new commits for target: %s', target_name)
            continue
        logger.debug('Delivering %d messages to target: %s', len(run_list), target_name)

        with click.progressbar(run_list,
                              label='Delivering to ' + target_name,
                              show_pos=True,
                              item_show_func=lambda x: x is not None and x[0] or None,
                              hidden=ctx.obj['hide_bar']) as bar:
            # We bail on a target if we have more than 5 consecutive failures
            consecutive_failures = 0
            for delivery_name, target, feed, epoch, commit, labels in bar:
                if consecutive_failures >= 5:
                    logger.error('Aborting deliveries to target "%s" due to repeated failures.', target_name)
                    break
                success = deliver_commit(delivery_name, target, feed, epoch, commit, labels, was_failing=False)
                if not success:
                    consecutive_failures += 1
                    continue

                consecutive_failures = 0
                if delivery_name not in changes:
                    changes[delivery_name] = 0
                changes[delivery_name] += 1

    unlock_all_feeds(ctx)
    if changes:
        logger.info('Pull complete with updates:')
        for delivery_name, count in changes.items():
            logger.info('  %s: %d', delivery_name, count)
    else:
        logger.info('Pull complete with no updates.')


@main.command()
@click.pass_context
@click.option('--target', '-t', default=None, help='Target to upload the message to')
@click.option('--labels', '-l', multiple=True,
              default=['INBOX', 'UNREAD'],
              help='Labels to apply to the message (can be used multiple times)')
@click.option('--thread', '-T', is_flag=True, help='Fetch and upload the entire thread')
@click.argument('msgid_or_url', type=str, nargs=1)
def yank(ctx: click.Context, target: Optional[str],
         labels: Tuple[str, ...], thread: bool, msgid_or_url: str) -> None:
    """Yank a single message or entire thread to a target."""
    # Get the target service
    if not target:
        # Get the first target in the list
        config = ctx.obj.get('config', {})
        targets = config.get('targets', {})
        target = list(targets.keys())[0]
        logger.debug('No target specified, using first target: %s', target)

    try:
        ts = get_target(ctx, target)
    except click.Abort:
        logger.critical('Failed to get target "%s".', target)
        raise

    # Convert labels tuple to list
    labels_list = list(labels) if labels else []

    if thread:
        # Fetch the entire thread
        logger.debug('Fetching thread: %s', msgid_or_url)
        try:
            messages = LoreFeed.get_thread_by_msgid(msgid_or_url)
        except RemoteError as e:
            logger.critical('Failed to fetch thread: %s', str(e))
            raise click.Abort()

        logger.info('Found %d messages in thread', len(messages))

        # Upload each message in the thread
        uploaded = 0
        failed = 0

        ts.connect()
        with click.progressbar(messages,
                              label='Uploading thread',
                              show_pos=True,
                              hidden=ctx.obj['hide_bar']) as bar:
            for raw_message in bar:
                try:
                    msg = LoreFeed.parse_message(raw_message)
                    subject = msg.get('Subject', '(no subject)')
                    logger.debug('Uploading: %s', subject)
                    ts.import_message(raw_message, labels=labels_list)
                    uploaded += 1
                except RemoteError as e:
                    logger.error('Failed to upload message: %s', str(e))
                    failed += 1
                    continue

        if failed > 0:
            logger.warning('Uploaded %d messages, %d failed', uploaded, failed)
        else:
            logger.info('Successfully uploaded %d messages from thread', uploaded)
    else:
        # Fetch a single message
        logger.debug('Fetching message: %s', msgid_or_url)
        try:
            raw_message = LoreFeed.get_message_by_msgid(msgid_or_url)
        except RemoteError as e:
            logger.critical('Failed to fetch message: %s', str(e))
            raise click.Abort()

        # Parse to get the subject for logging
        msg = LoreFeed.parse_message(raw_message)
        subject = msg.get('Subject', '(no subject)')
        logger.debug('Message subject: %s', subject)

        # Upload the message
        logger.info('Uploading to target "%s"', target)
        logger.debug('Uploading: %s', subject)
        try:
            ts.connect()
            ts.import_message(raw_message, labels=labels_list)
            logger.info('Successfully uploaded message.')
        except RemoteError as e:
            logger.critical('Failed to upload message: %s', str(e))
            raise click.Abort()


if __name__ == '__main__':
    main()
