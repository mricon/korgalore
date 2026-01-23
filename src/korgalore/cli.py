"""Command-line interface for korgalore."""

import os
import re
import hashlib
import uuid
import click
import tomllib
import logging
import click_log
import requests

from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional, Union, Callable, Set
from korgalore.lore_feed import LoreFeed
from korgalore.lei_feed import LeiFeed
from korgalore.gmail_target import GmailTarget
from korgalore.maildir_target import MaildirTarget
from korgalore.jmap_target import JmapTarget
from korgalore.imap_target import ImapTarget
from korgalore.pipe_target import PipeTarget
from korgalore import (
    __version__, ConfigurationError, StateError, GitError,
    RemoteError, PublicInboxError, AuthenticationError, format_key_for_display,
    _init_git_user_agent, set_user_agent_id, get_requests_session, close_requests_session
)
from korgalore.tracking import (
    TrackingManifest, TrackStatus,
    create_lei_thread_search, create_lei_query_search, update_lei_search,
    forget_lei_search
)
from korgalore.maintainers import (
    get_subsystem, normalize_subsystem_name,
    build_mailinglist_query, build_patches_query,
    generate_subsystem_config, DEFAULT_CATCHALL_LISTS
)
from korgalore.bozofilter import (
    load_bozofilter, add_to_bozofilter, edit_bozofilter, is_bozofied
)

logger = logging.getLogger('korgalore')
click_log.basic_config(logger)

# Sentinel value for messages skipped due to bozofilter
SKIPPED_BOZOFILTER = '__SKIPPED_BOZOFILTER__'

# URL to fetch MAINTAINERS file from kernel.org
MAINTAINERS_URL = 'https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/plain/MAINTAINERS'

# Maximum age of cached MAINTAINERS file in seconds (24 hours)
MAINTAINERS_CACHE_MAX_AGE = 24 * 60 * 60


def get_maintainers_file(data_dir: Path) -> Path:
    """Get MAINTAINERS file, fetching from kernel.org if needed.

    Uses a cached copy if it exists and is less than 24 hours old.
    Otherwise fetches a fresh copy from kernel.org.

    Args:
        data_dir: The korgalore data directory for caching.

    Returns:
        Path to the MAINTAINERS file.

    Raises:
        click.ClickException: If fetching fails.
    """
    import time

    cache_path = data_dir / 'MAINTAINERS'

    # Check if we have a fresh cached copy
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < MAINTAINERS_CACHE_MAX_AGE:
            logger.debug('Using cached MAINTAINERS file (age: %.1f hours)', age / 3600)
            return cache_path
        logger.debug('Cached MAINTAINERS file is stale (age: %.1f hours)', age / 3600)

    # Fetch fresh copy
    logger.info('Fetching MAINTAINERS file from %s', MAINTAINERS_URL)
    try:
        session = get_requests_session()
        response = session.get(MAINTAINERS_URL, timeout=30)
        response.raise_for_status()
    except Exception as e:
        # If fetch fails but we have a stale cache, use it with a warning
        if cache_path.exists():
            logger.warning('Failed to fetch fresh MAINTAINERS file, using stale cache: %s', e)
            return cache_path
        raise click.ClickException(
            f"Failed to fetch MAINTAINERS file from {MAINTAINERS_URL}: {e}\n"
            "Use -m/--maintainers to specify a local copy."
        )

    # Cache the file
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(response.content)
    logger.debug('Cached MAINTAINERS file to %s', cache_path)

    return cache_path


def parse_labels(labels: Tuple[str, ...]) -> List[str]:
    """Parse labels from command line, supporting both repeated -l and comma-separated.

    Args:
        labels: Tuple of label strings from click's multiple=True option.

    Returns:
        Flattened list of individual labels.

    Examples:
        >>> parse_labels(('INBOX', 'UNREAD'))
        ['INBOX', 'UNREAD']
        >>> parse_labels(('INBOX,UNREAD,custom-label',))
        ['INBOX', 'UNREAD', 'custom-label']
        >>> parse_labels(('INBOX', 'UNREAD,CATEGORY_FORUMS'))
        ['INBOX', 'UNREAD', 'CATEGORY_FORUMS']
    """
    result: List[str] = []
    for label in labels:
        # Split by comma and strip whitespace
        result.extend(part.strip() for part in label.split(',') if part.strip())
    return result


def get_xdg_data_dir() -> Path:
    """Get or create the korgalore data directory following XDG specification."""
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
    """Get or create the korgalore config directory following XDG specification."""
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
    """Get or create a target service instance by identifier."""
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
    # In GUI mode, don't run interactive OAuth flows
    interactive = not ctx.obj.get('gui_mode', False)
    service: Any
    if target_type == 'gmail':
        service = get_gmail_target(
            identifier=identifier,
            credentials_file=details.get('credentials', ''),
            token_file=details.get('token', None),
            interactive=interactive
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
            timeout=details.get('timeout', 60),
            reqsession=get_requests_session()
        )
    elif target_type == 'imap':
        service = get_imap_target(
            identifier=identifier,
            server=details.get('server', ''),
            username=details.get('username', ''),
            folder=details.get('folder', 'INBOX'),
            password=details.get('password', None),
            password_file=details.get('password_file', None),
            timeout=details.get('timeout', 60),
            auth_type=details.get('auth_type', 'password'),
            client_id=details.get('client_id', None),
            tenant=details.get('tenant', 'common'),
            token=details.get('token', None),
            interactive=interactive
        )
    elif target_type == 'pipe':
        service = get_pipe_target(
            identifier=identifier,
            command=details.get('command', '')
        )
    else:
        logger.critical('Unknown target type "%s" for target "%s".', target_type, identifier)
        logger.critical('Supported types: gmail, maildir, jmap, imap, pipe')
        raise click.Abort()

    ctx.obj['targets'][identifier] = service

    # Check if Gmail target needs authentication (in non-interactive/GUI mode)
    # Note: IMAP OAuth2 targets handle this during connect() instead, which
    # allows the 'auth' command to work properly.
    if target_type == 'gmail' and service.needs_auth:
        raise AuthenticationError(
            f"Gmail target '{identifier}' requires authentication.",
            target_id=identifier,
            target_type='gmail'
        )

    return service


def get_gmail_target(identifier: str, credentials_file: str,
                     token_file: Optional[str], interactive: bool = True) -> GmailTarget:
    """Create a Gmail target service instance."""
    if not credentials_file:
        logger.critical('No credentials file specified for Gmail target: %s', identifier)
        raise click.Abort()
    if not token_file:
        cfgdir = get_xdg_config_dir()
        token_file = str(cfgdir / f'gmail-{identifier}-token.json')
    try:
        gt = GmailTarget(identifier=identifier,
                         credentials_file=credentials_file,
                         token_file=token_file,
                         interactive=interactive)
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort()

    return gt


def get_maildir_target(identifier: str, maildir_path: str) -> MaildirTarget:
    """Create a Maildir target service instance."""
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
                    timeout: int,
                    reqsession: Optional[requests.Session] = None) -> JmapTarget:
    """Create a JMAP target service instance."""
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
            timeout=timeout,
            reqsession=reqsession
        )
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort()

    return jt


def get_imap_target(identifier: str, server: str, username: str,
                    folder: str, password: Optional[str],
                    password_file: Optional[str], timeout: int,
                    auth_type: str = 'password',
                    client_id: Optional[str] = None,
                    tenant: str = 'common',
                    token: Optional[str] = None,
                    interactive: bool = True) -> ImapTarget:
    """Create an IMAP target service instance."""
    if not server:
        logger.critical('No server specified for IMAP target: %s', identifier)
        raise click.Abort()

    if not username:
        logger.critical('No username specified for IMAP target: %s', identifier)
        raise click.Abort()

    if auth_type != 'oauth2':
        # Password authentication - requires password or password_file
        if not password and not password_file:
            logger.critical('No password or password_file specified for IMAP target: %s', identifier)
            logger.critical('Either provide password directly or use password_file for security')
            raise click.Abort()
    # OAuth2 uses a built-in default client_id if not specified

    try:
        it = ImapTarget(
            identifier=identifier,
            server=server,
            username=username,
            folder=folder,
            password=password,
            password_file=password_file,
            timeout=timeout,
            auth_type=auth_type,
            client_id=client_id,
            tenant=tenant,
            token=token,
            interactive=interactive
        )
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort()

    return it


def get_pipe_target(identifier: str, command: str) -> PipeTarget:
    """Create a Pipe target service instance."""
    if not command:
        logger.critical('No command specified for pipe target: %s', identifier)
        raise click.Abort()

    try:
        pt = PipeTarget(identifier=identifier, command=command)
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort()

    return pt


def resolve_feed_url(feed_value: str, config: Dict[str, Any]) -> str:
    """Resolve a feed name or URL to its full URL."""
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


def validate_config_file(cfgpath: Path) -> Tuple[bool, str]:
    """Validate a TOML configuration file.

    Args:
        cfgpath: Path to the configuration file.

    Returns:
        A tuple of (is_valid, error_message). If valid, error_message is empty.
    """
    if not cfgpath.exists():
        return False, f"Configuration file not found: {cfgpath}"

    try:
        with open(cfgpath, 'rb') as cf:
            tomllib.load(cf)
        return True, ""
    except tomllib.TOMLDecodeError as e:
        return False, f"TOML syntax error: {e}"
    except Exception as e:
        return False, f"Error reading config: {e}"


def merge_config(base: Dict[str, Any], extra: Dict[str, Any]) -> None:
    """Merge extra config into base config (modifies base in-place).

    Merges 'targets', 'feeds', 'deliveries', and 'gui' sections.
    """
    for section in ('targets', 'feeds', 'deliveries'):
        if section in extra:
            if section not in base:
                base[section] = {}
            base[section].update(extra[section])
    # gui section is replaced, not merged
    if 'gui' in extra:
        base['gui'] = extra['gui']


def load_config(cfgfile: Path) -> Dict[str, Any]:
    """Load and parse the TOML configuration file and conf.d/*.toml files."""
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

        # Load conf.d/*.toml files
        conf_d = cfgfile.parent / 'conf.d'
        if conf_d.is_dir():
            for toml_file in sorted(conf_d.glob('*.toml')):
                logger.debug('Loading additional config from %s', toml_file.name)
                with open(toml_file, 'rb') as cf:
                    extra = tomllib.load(cf)
                merge_config(config, extra)

        logger.debug('Config loaded with %s targets, %s deliveries, and %s feeds',
                     len(config.get('targets', {})), len(config.get('deliveries', {})),
                     len(config.get('feeds', {})))

        return config

    except Exception as e:
        logger.error('Error loading config: %s', str(e))
        raise click.Abort()


def retry_failed_commits(feed_dir: Path, pi_feed: Union[LeiFeed, LoreFeed], target_service: Any,
                         labels: List[str], delivery_name: str) -> None:
    """Retry previously failed message deliveries for a specific delivery."""
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
            pi_feed.mark_successful_delivery(delivery_name, epoch, commit_hash, message=raw_message)
        except RemoteError:
            pi_feed.mark_failed_delivery(delivery_name, epoch, commit_hash)

    # Save updated tracking files
    pi_feed.feed_unlock()


def deliver_commit(delivery_name: str, target: Any, feed: Union[LeiFeed, LoreFeed], epoch: int, commit: str,
                   labels: List[str], was_failing: bool = False,
                   bozofilter: Optional[Set[str]] = None) -> Optional[str]:
    """Deliver a single message to the target.

    Args:
        delivery_name: Name of the delivery configuration.
        target: Target service to deliver to.
        feed: Feed to get message from.
        epoch: Epoch number containing the message.
        commit: Git commit hash of the message.
        labels: Labels/folders to apply.
        was_failing: True if this is a retry of a previously failed delivery.
        bozofilter: Optional set of addresses to skip (from bozofilter).

    Returns:
        The Message-ID of the delivered message on success, None on failure or skip.
    """
    raw_message: Optional[bytes] = None
    try:
        raw_message = feed.get_message_at_commit(epoch, commit)
        target.connect()
        msg = feed.parse_message(raw_message)
        msgid = msg.get('Message-ID', '')

        # Check bozofilter before delivering
        if bozofilter:
            from_header = msg.get('From', '')
            if is_bozofied(from_header, bozofilter):
                logger.debug('Skipping bozofied sender: %s', from_header)
                # Mark as successful to avoid retrying
                feed.mark_successful_delivery(delivery_name, epoch, commit,
                                              message=raw_message, was_failing=was_failing)
                return SKIPPED_BOZOFILTER

        if logger.isEnabledFor(logging.DEBUG):
            subject = msg.get('Subject', '(no subject)')
            logger.debug(' -> %s', subject)
        target.import_message(raw_message, labels=labels)
        feed.mark_successful_delivery(delivery_name, epoch, commit, message=raw_message, was_failing=was_failing)
        return msgid
    except Exception as e:
        logger.debug('Failed to deliver commit %s from epoch %d: %s', commit, epoch, str(e))
        feed.mark_failed_delivery(delivery_name, epoch, commit)
        # Only save delivery info if we successfully retrieved the message
        # and this is not a retry of a previously failed delivery
        if raw_message is not None and not was_failing:
            feed.save_delivery_info(delivery_name, epoch, latest_commit=commit, message=raw_message)
        return None


def normalize_feed_key(feed_url: str) -> str:
    """Normalize a feed URL into a consistent key for internal tracking."""
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
    """Get or create a feed instance for a delivery configuration."""
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
        lore_feed = LoreFeed(feed_key, feed_dir, feed_url, reqsession=get_requests_session())
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
    """Map delivery configurations to their feed and target instances."""
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
    """Acquire exclusive locks on all feeds in the context."""
    feeds = ctx.obj.get('feeds', {})  # type: Dict[str, Union[LeiFeed, LoreFeed]]
    for feed_key in feeds.keys():
        feed = feeds[feed_key]
        feed.feed_lock()


def unlock_all_feeds(ctx: click.Context) -> None:
    """Release exclusive locks on all feeds in the context."""
    feeds = ctx.obj.get('feeds', {})  # type: Dict[str, Union[LeiFeed, LoreFeed]]
    for feed_key in feeds.keys():
        feed = feeds[feed_key]
        feed.feed_unlock()


def update_all_feeds(ctx: click.Context, status_callback: Optional[Callable[[str], None]] = None) -> List[str]:
    """Update all feeds and return list of feed keys that had updates."""
    updated_feeds: List[str] = []
    initialized_feeds: List[str] = []
    feeds = ctx.obj.get('feeds', {})  # type: Dict[str, Union[LeiFeed, LoreFeed]]

    if status_callback:
        status_callback("Querying feeds...")

    with click.progressbar(feeds.keys(),
                           label='Updating feeds',
                           show_pos=True,
                           item_show_func=lambda x: format_key_for_display(x in feeds and str(feeds[x].feed_url) or x),
                           hidden=ctx.obj['hide_bar']) as bar:
        for feed_key in bar:
            if status_callback:
                status_callback(f"Querying {format_key_for_display(feed_key)}...")
            feed = feeds[feed_key]
            status = feed.update_feed()
            if status & feed.STATUS_UPDATED:
                updated_feeds.append(feed_key)
            if status & feed.STATUS_INITIALIZED:
                initialized_feeds.append(feed_key)

    # Log initialization messages after progressbar completes
    for feed_key in initialized_feeds:
        logger.info('Initialized new feed: %s', feed_key)

    return updated_feeds


def retry_all_failed_deliveries(ctx: click.Context) -> None:
    """Retry all previously failed deliveries across all feeds."""
    bozo_set = ctx.obj.get('bozofilter', set())

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
            deliver_commit(delivery_name, target, feed, epoch, commit, labels,
                           was_failing=True, bozofilter=bozo_set)


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

        # Check for user_agent_plus in config
        main_config = config.get('main', {})
        user_agent_plus = main_config.get('user_agent_plus')
        if user_agent_plus:
            set_user_agent_id(user_agent_plus)

    # Check git is available and set GIT_HTTP_USER_AGENT
    try:
        _init_git_user_agent()
    except GitError as e:
        raise click.ClickException(str(e))

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

    # Load bozofilter for filtering unwanted senders
    config_dir = get_xdg_config_dir()
    ctx.obj['bozofilter'] = load_bozofilter(config_dir)


@main.command()
@click.argument('target', required=False)
@click.pass_context
def auth(ctx: click.Context, target: Optional[str]) -> None:
    """Authenticate with configured targets.

    If TARGET is specified, authenticate only that target.
    If TARGET is omitted, authenticate all targets that require authentication.
    """
    # Target types that don't require authentication
    NO_AUTH_TARGETS = {'maildir', 'pipe'}

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
        example_config = f"""[main]
# Uncomment to add a unique identifier to your User-Agent string.
# This may be used to help prioritize your requests.
# user_agent_plus = '{uuid.uuid4()}'

### Targets ###

[targets.personal]
type = 'gmail'
credentials = '~/.config/korgalore/credentials.json'
# token = '~/.config/korgalore/token.json'

### Deliveries ###

# [deliveries.lkml]
# feed = 'https://lore.kernel.org/lkml'
# target = 'personal'
# labels = ['INBOX', 'UNREAD']

### GUI ###

[gui]
# sync_interval = 300
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

    # Validate the config file after editing
    is_valid, error_msg = validate_config_file(cfgpath)
    if is_valid:
        logger.info('Configuration file is valid.')
    else:
        logger.error('Configuration file has errors: %s', error_msg)


@main.command()
@click.pass_context
@click.argument('target', type=str, nargs=1)
@click.option('--ids', '-i', is_flag=True, help='include id values')
def labels(ctx: click.Context, target: str, ids: bool = False) -> None:
    """List all available labels/folders for a target."""
    gs = get_target(ctx, ctx.params['target'])

    # Check if target supports labels
    if not hasattr(gs, 'list_labels'):
        logger.warning('Target "%s" does not support labels (maildir targets ignore labels).',
                      target)
        return

    try:
        gs.connect()
        logger.debug('Fetching labels from target')
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


def perform_pull(ctx: click.Context, no_update: bool, force: bool,
                 delivery_name: Optional[str],
                 status_callback: Optional[Callable[[str], None]] = None) -> Tuple[Dict[str, int], Set[str]]:
    """Execute the pull logic and return changes.

    Returns:
        A tuple of (per-delivery counts dict, set of unique message-ids delivered).
    """
    cfg = ctx.obj.get('config', {})
    bozo_set = ctx.obj.get('bozofilter', set())

    # Load deliveries to process
    deliveries = cfg.get('deliveries', {})
    if delivery_name:
        if delivery_name not in deliveries:
            logger.critical('Delivery "%s" not found in configuration.', delivery_name)
            raise click.Abort()
        deliveries = {delivery_name: deliveries[delivery_name]}

    # Collect unique feeds from all deliveries
    map_deliveries(ctx, deliveries)

    # Map tracked threads as ephemeral deliveries (unless specific delivery requested)
    if not delivery_name:
        map_tracked_threads(ctx)

    lock_all_feeds(ctx)
    # Retry all previously failed deliveries, if any
    retry_all_failed_deliveries(ctx)
    if no_update:
        logger.debug('No-update flag set, skipping feed updates')
        updated_feeds = list()
    else:
        updated_feeds = update_all_feeds(ctx, status_callback=status_callback)
    run_deliveries: List[str] = list()
    if not force:
        logger.debug('Updated feeds: %s', ', '.join(updated_feeds))
        # Build reverse index: feed_key -> delivery names (O(m) once, instead of O(n*m) nested loop)
        feed_to_deliveries: Dict[str, List[str]] = dict()
        for dname, (feed, _, _) in ctx.obj['deliveries'].items():
            feed_to_deliveries.setdefault(feed.feed_key, []).append(dname)
        # O(1) lookup per updated feed
        for feed_key in updated_feeds:
            run_deliveries.extend(feed_to_deliveries.get(feed_key, []))
    else:
        # If force is specified, treat all feeds as updated
        logger.debug('Force flag set, treating all feeds as updated')
        run_deliveries = list(ctx.obj['deliveries'].keys())

    logger.debug('Deliveries to run: %s', ', '.join(run_deliveries))

    if not run_deliveries:
        unlock_all_feeds(ctx)
        return {}, set()

    # Build a worklist of updates per target
    by_target: Dict[str, List[str]] = dict()
    for dname in run_deliveries:
        target_name = ctx.obj['deliveries'][dname][1].identifier
        if target_name not in by_target:
            by_target[target_name] = list()
        by_target[target_name].append(dname)

    changes: Dict[str, int] = dict()
    unique_msgids: Set[str] = set()

    # Process deliveries now
    for target_name, delivery_names in by_target.items():
        logger.debug('Processing deliveries for target: %s', target_name)
        run_list: List[Tuple[str, Any, Union[LeiFeed, LoreFeed], int, str, List[str]]] = list()
        for dname in delivery_names:
            feed, target, labels = ctx.obj['deliveries'][dname]
            commits = feed.get_latest_commits_for_delivery(dname)
            if not commits:
                logger.debug('No new commits for delivery: %s', dname)
                continue
            for epoch, commit in commits:
                run_list.append((dname, target, feed, epoch, commit, labels))
        if not run_list:
            logger.debug('No deliveries with new commits for target: %s', target_name)
            continue
        logger.debug('Delivering %d messages to target: %s', len(run_list), target_name)

        with click.progressbar(run_list,
                              label='Delivering to ' + target_name,
                              show_pos=True,
                              item_show_func=lambda x: x is not None and format_key_for_display(x[0]) or None,
                              hidden=ctx.obj['hide_bar']) as bar:
            # We bail on a target if we have more than 5 consecutive failures
            consecutive_failures = 0
            prev_dname: Optional[str] = None
            for dname, target, feed, epoch, commit, labels in bar:
                if status_callback and dname != prev_dname:
                    status_callback(f"Delivering {format_key_for_display(dname)}...")
                    prev_dname = dname
                if consecutive_failures >= 5:
                    logger.error('Aborting deliveries to target "%s" due to repeated failures.', target_name)
                    break
                msgid = deliver_commit(dname, target, feed, epoch, commit, labels,
                                       was_failing=False, bozofilter=bozo_set)
                if msgid is None:
                    consecutive_failures += 1
                    continue
                if msgid == SKIPPED_BOZOFILTER:
                    # Bozofied message - not a failure, just skip
                    continue

                consecutive_failures = 0
                if dname not in changes:
                    changes[dname] = 0
                changes[dname] += 1
                if msgid:
                    unique_msgids.add(msgid)

        # Disconnect target if it supports it (e.g., IMAP)
        target_service = ctx.obj['targets'].get(target_name)
        if target_service is not None and hasattr(target_service, 'disconnect'):
            target_service.disconnect()

    unlock_all_feeds(ctx)

    # Update tracking manifest activity for any tracked threads that had deliveries
    update_tracked_thread_activity(ctx, changes)

    # Close HTTP session and clear cached targets to avoid stale session references
    close_requests_session()
    ctx.obj['targets'] = {}

    return changes, unique_msgids


@main.command()
@click.pass_context
@click.option('--max-mail', '-m', default=0, help='maximum number of messages to pull (0 for all)')
@click.option('--no-update', '-n', is_flag=True, help='skip feed updates (useful with --force)')
@click.option('--force', '-f', is_flag=True, help='run deliveries even if no apparent updates')
@click.argument('delivery_name', type=str, nargs=1, default=None)
def pull(ctx: click.Context, max_mail: int, no_update: bool, force: bool, delivery_name: Optional[str]) -> None:
    """Pull messages from configured lore and LEI deliveries."""
    changes, _ = perform_pull(ctx, no_update, force, delivery_name)

    if changes:
        logger.info('Pull complete with updates:')
        tracked_ids = []
        if not delivery_name:
             # We need to re-fetch tracked IDs to identify them in output
             # This is a bit inefficient but safe
             manifest = get_tracking_manifest(ctx)
             tracked_ids = [t.track_id for t in manifest.get_active_threads()]

        for dname, count in changes.items():
            if dname in tracked_ids:
                logger.info('  %s (tracked): %d', dname, count)
            else:
                logger.info('  %s: %d', dname, count)
    else:
        logger.info('Pull complete with no updates.')


def perform_yank(ctx: click.Context, target_name: str, msgid_or_url: str,
                 thread: bool = False,
                 labels_list: Optional[List[str]] = None) -> Tuple[int, int]:
    """Perform yank operation (usable from CLI and GUI).

    Args:
        ctx: Click context with config and targets
        target_name: Name of the target to upload to
        msgid_or_url: Message-ID or lore.kernel.org URL
        thread: If True, fetch entire thread
        labels_list: Labels to apply (uses target defaults if None)

    Returns:
        Tuple of (uploaded_count, failed_count)

    Raises:
        ConfigurationError: If target not found
        RemoteError: If fetch or upload fails
    """
    ts = get_target(ctx, target_name)

    if labels_list is None:
        labels_list = ts.DEFAULT_LABELS

    ts.connect()

    try:
        if thread:
            messages = LoreFeed.get_thread_by_msgid(msgid_or_url)
            logger.info('Found %d messages in thread', len(messages))

            uploaded = 0
            failed = 0

            for raw_message in messages:
                try:
                    msg = LoreFeed.parse_message(raw_message)
                    subject = msg.get('Subject', '(no subject)')
                    logger.debug('Uploading: %s', subject)
                    ts.import_message(raw_message, labels=labels_list)
                    uploaded += 1
                except RemoteError as e:
                    logger.error('Failed to upload message: %s', str(e))
                    failed += 1

            return uploaded, failed
        else:
            raw_message = LoreFeed.get_message_by_msgid(msgid_or_url)
            msg = LoreFeed.parse_message(raw_message)
            subject = msg.get('Subject', '(no subject)')
            logger.debug('Uploading: %s', subject)
            ts.import_message(raw_message, labels=labels_list)
            return 1, 0
    finally:
        if hasattr(ts, 'disconnect'):
            ts.disconnect()
        close_requests_session()
        ctx.obj['targets'] = {}


@main.command()
@click.pass_context
@click.option('--target', '-t', default=None, help='Target to upload the message to')
@click.option('--labels', '-l', multiple=True,
              help='Labels to apply (repeatable or comma-separated)')
@click.option('--thread', '-T', is_flag=True, help='Fetch and upload the entire thread')
@click.argument('msgid_or_url', type=str, nargs=1)
def yank(ctx: click.Context, target: Optional[str],
         labels: Tuple[str, ...], thread: bool, msgid_or_url: str) -> None:
    """Yank a single message or entire thread to a target."""
    # Get the target service
    config = ctx.obj.get('config', {})
    targets = config.get('targets', {})

    # Auto-select target if only one exists
    if not target:
        if len(targets) == 1:
            target = list(targets.keys())[0]
            logger.debug('Using only configured target: %s', target)
        else:
            logger.critical('Multiple targets configured. Please specify one with -t.')
            logger.critical('Available targets: %s', ', '.join(targets.keys()))
            raise click.Abort()

    try:
        ts = get_target(ctx, target)
    except click.Abort:
        logger.critical('Failed to get target "%s".', target)
        raise

    # Use target-specific default labels if none specified
    if labels:
        labels_list = parse_labels(labels)
    else:
        labels_list = ts.DEFAULT_LABELS

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


def get_tracking_manifest(ctx: click.Context) -> TrackingManifest:
    """Get or create the tracking manifest."""
    if 'tracking_manifest' not in ctx.obj:
        data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())
        ctx.obj['tracking_manifest'] = TrackingManifest(data_dir)
    manifest: TrackingManifest = ctx.obj['tracking_manifest']
    return manifest


def map_tracked_threads(ctx: click.Context) -> List[str]:
    """Map active tracked threads as ephemeral deliveries.

    Adds tracked threads to ctx.obj['feeds'] and ctx.obj['deliveries']
    so they are processed alongside regular deliveries.

    Returns:
        List of track_ids that were mapped.
    """
    manifest = get_tracking_manifest(ctx)

    # Auto-expire inactive threads
    expired = manifest.check_and_expire_threads()
    if expired:
        logger.info('Auto-expired %d threads with no recent activity', len(expired))

    active = manifest.get_active_threads()
    if not active:
        logger.debug('No active tracked threads')
        return []

    logger.debug('Mapping %d tracked threads as ephemeral deliveries', len(active))

    feeds = ctx.obj.get('feeds', {})
    deliveries = ctx.obj.get('deliveries', {})
    mapped: List[str] = []

    for tracked in active:
        lei_url = f'lei:{tracked.lei_path}'
        try:
            lei_feed = LeiFeed(tracked.track_id, lei_url)
        except ConfigurationError as e:
            logger.warning('Tracked thread %s not recognized by lei: %s',
                          tracked.track_id, str(e))
            continue

        try:
            target = get_target(ctx, tracked.target)
        except click.Abort:
            logger.warning('Target "%s" not available for tracked thread %s',
                          tracked.target, tracked.track_id)
            continue

        # Add to feeds and deliveries
        feeds[tracked.track_id] = lei_feed
        deliveries[tracked.track_id] = (lei_feed, target, tracked.labels)
        mapped.append(tracked.track_id)

    return mapped


def update_tracked_thread_activity(ctx: click.Context, changes: Dict[str, int]) -> None:
    """Update tracking manifest activity for tracked threads that had deliveries."""
    manifest = get_tracking_manifest(ctx)

    for delivery_name, count in changes.items():
        if delivery_name.startswith('track-'):
            try:
                manifest.update_activity(delivery_name, count)
            except KeyError:
                pass  # Not a tracked thread or already removed


@main.group()
@click.pass_context
def track(ctx: click.Context) -> None:
    """Track email threads for updates via lei queries."""
    pass


@track.command('add')
@click.argument('msgid_or_url', type=str)
@click.option('--target', '-t', default=None, help='Target for deliveries')
@click.option('--labels', '-l', multiple=True,
              help='Labels to apply (repeatable or comma-separated)')
@click.pass_context
def track_add(ctx: click.Context, msgid_or_url: str, target: Optional[str],
              labels: Tuple[str, ...]) -> None:
    """Start tracking a thread by message ID or lore URL."""
    config = ctx.obj.get('config', {})
    targets = config.get('targets', {})

    # Auto-select target if only one exists
    if not target:
        if len(targets) == 1:
            target = list(targets.keys())[0]
            logger.debug('Using only configured target: %s', target)
        else:
            logger.critical('Multiple targets configured. Please specify one with -t.')
            logger.critical('Available targets: %s', ', '.join(targets.keys()))
            raise click.Abort()
    elif target not in targets:
        logger.critical('Target "%s" not found in configuration.', target)
        logger.critical('Known targets: %s', ', '.join(targets.keys()))
        raise click.Abort()

    # Extract message ID from URL if needed
    msgid = LoreFeed.get_msgid_from_url(msgid_or_url)
    logger.debug('Extracted message ID: %s', msgid)

    # Check if already tracking this message
    manifest = get_tracking_manifest(ctx)
    existing = manifest.get_thread_by_msgid(msgid)
    if existing:
        if existing.status == TrackStatus.ACTIVE:
            logger.warning('Already tracking this thread as %s', existing.track_id)
            return
        else:
            # Offer to resume
            logger.info('Thread previously tracked as %s (status: %s)',
                       existing.track_id, existing.status.value)
            if click.confirm('Resume tracking?'):
                manifest.resume_thread(existing.track_id)
                logger.info('Resumed tracking thread %s', existing.track_id)
                return
            else:
                logger.info('Aborted.')
                return

    # Create lei search directory
    data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())
    import secrets
    track_id = f"track-{secrets.token_hex(6)}"
    lei_path = data_dir / 'lei' / track_id

    logger.info('Creating lei search for thread: %s', msgid)

    # Create the lei search
    try:
        retcode, output = create_lei_thread_search(msgid, lei_path)
    except PublicInboxError as e:
        logger.critical('Failed to create lei search: %s', str(e))
        raise click.Abort()

    if retcode != 0:
        logger.critical('Lei query failed: %s', output.decode())
        raise click.Abort()

    # Populate the git repository with lei up
    logger.info('Populating lei search repository...')
    try:
        retcode, output = update_lei_search(lei_path)
    except PublicInboxError as e:
        logger.critical('Failed to update lei search: %s', str(e))
        raise click.Abort()

    if retcode != 0:
        logger.critical('Lei update failed: %s', output.decode())
        raise click.Abort()

    # Get the subject from the first message
    subject = '(unknown subject)'
    try:
        raw_message = LoreFeed.get_message_by_msgid(msgid)
        msg = LoreFeed.parse_message(raw_message)
        subject = msg.get('Subject', '(no subject)')
    except RemoteError:
        logger.warning('Could not fetch message to get subject')

    # Get target instance for delivery and default labels
    target_service = get_target(ctx, target)

    # Add to manifest with target-specific default labels if none specified
    if labels:
        labels_list = parse_labels(labels)
    else:
        labels_list = target_service.DEFAULT_LABELS

    thread = manifest.add_thread(
        track_id=track_id,
        msgid=msgid,
        subject=subject,
        target=target,
        labels=labels_list,
        lei_path=lei_path
    )

    logger.info('Now tracking thread %s: %s', thread.track_id, subject)
    logger.info('Target: %s, Labels: %s', target, ', '.join(labels_list))

    # Deliver initial messages to target
    lei_url = f'lei:{lei_path}'
    try:
        lei_feed = LeiFeed(thread.track_id, lei_url)
    except ConfigurationError as e:
        logger.warning('Could not initialize lei feed for delivery: %s', str(e))
        return

    # Get all commits in epoch 0 (lei thread searches won't exceed a single epoch)
    commits = lei_feed.get_all_commits_in_epoch(0)

    if not commits:
        logger.info('No messages found in thread yet.')
        return

    logger.info('Delivering %d messages to target...', len(commits))

    bozo_set = ctx.obj.get('bozofilter', set())
    delivered = 0
    for commit in commits:
        result = deliver_commit(thread.track_id, target_service, lei_feed, 0, commit,
                                labels_list, was_failing=False, bozofilter=bozo_set)
        if result and result != SKIPPED_BOZOFILTER:
            delivered += 1

    # Initialize feed state so subsequent pulls don't re-initialize
    lei_feed.init_feed()

    manifest.update_activity(thread.track_id, delivered)
    logger.info('Delivered %d messages.', delivered)


@track.command('list')
@click.option('--inactive', '-i', is_flag=True, help='Show only inactive/paused threads')
@click.pass_context
def track_list(ctx: click.Context, inactive: bool) -> None:
    """List tracked threads."""
    manifest = get_tracking_manifest(ctx)

    if inactive:
        threads = manifest.get_inactive_threads()
        if not threads:
            logger.info('No inactive or paused tracked threads.')
            return
        logger.info('Inactive/paused tracked threads:')
    else:
        threads = manifest.get_all_threads()
        if not threads:
            logger.info('No tracked threads.')
            return
        logger.info('Tracked threads:')

    for thread in threads:
        status_str = ''
        if thread.status != TrackStatus.ACTIVE:
            status_str = f' [{thread.status.value}]'

        logger.info('')
        logger.info('  %s%s', thread.track_id, status_str)
        logger.info('    Subject: %s', thread.subject)
        logger.info('    Message-ID: %s', thread.msgid)
        logger.info('    Target: %s, Labels: %s', thread.target, ', '.join(thread.labels))
        logger.info('    Messages: %d, Last activity: %s',
                   thread.message_count, thread.last_new_message.strftime('%Y-%m-%d'))


@track.command('stop')
@click.argument('track_id', type=str)
@click.option('--delete', is_flag=True, help='Also delete lei search data')
@click.pass_context
def track_stop(ctx: click.Context, track_id: str, delete: bool) -> None:
    """Stop tracking a thread."""
    manifest = get_tracking_manifest(ctx)

    try:
        thread = manifest.get_thread(track_id)
    except KeyError:
        logger.critical('Tracked thread "%s" not found.', track_id)
        raise click.Abort()

    manifest.remove_thread(track_id, delete_data=delete)

    if delete:
        logger.info('Stopped tracking and deleted data for %s', track_id)
    else:
        logger.info('Stopped tracking %s (data preserved at %s)', track_id, thread.lei_path)
        logger.info('To clean up lei data, run: lei forget-search %s', thread.lei_path)


@track.command('pause')
@click.argument('track_id', type=str)
@click.pass_context
def track_pause(ctx: click.Context, track_id: str) -> None:
    """Pause tracking for a thread (skip updates but keep data)."""
    manifest = get_tracking_manifest(ctx)

    try:
        manifest.pause_thread(track_id)
    except KeyError:
        logger.critical('Tracked thread "%s" not found.', track_id)
        raise click.Abort()

    logger.info('Paused tracking for %s', track_id)


@track.command('resume')
@click.argument('track_id', type=str)
@click.pass_context
def track_resume(ctx: click.Context, track_id: str) -> None:
    """Resume tracking for a paused or expired thread."""
    manifest = get_tracking_manifest(ctx)

    try:
        thread = manifest.get_thread(track_id)
    except KeyError:
        logger.critical('Tracked thread "%s" not found.', track_id)
        raise click.Abort()

    if thread.status == TrackStatus.ACTIVE:
        logger.warning('Thread %s is already active.', track_id)
        return

    manifest.resume_thread(track_id)
    logger.info('Resumed tracking for %s', track_id)


@main.command()
@click.pass_context
def gui(ctx: click.Context) -> None:
    """Launch the GNOME taskbar application."""
    try:
        from korgalore.gui import start_gui
    except ImportError as e:
        logger.critical('GUI dependencies not found: %s', str(e))
        logger.critical('Please install the "gui" extra: pip install ".[gui]"')
        logger.critical('You may also need system packages: '
                        'libgirepository1.0-dev, libcairo2-dev, gir1.2-appindicator3-0.1')
        raise click.Abort()

    # Set GUI mode to disable interactive OAuth flows
    ctx.obj['gui_mode'] = True
    start_gui(ctx)


@main.command('track-subsystem')
@click.argument('subsystem_name', type=str)
@click.option('--maintainers', '-m', default=None,
              type=click.Path(), help='Path to MAINTAINERS file (default: ./MAINTAINERS)')
@click.option('--target', '-t', default=None, help='Target for deliveries')
@click.option('--labels', '-l', multiple=True,
              help='Labels to apply (repeatable or comma-separated; default: target DEFAULT_LABELS)')
@click.option('--since', default='7.days.ago',
              help='Start date for query (default: 7.days.ago)')
@click.option('--threads/--no-threads', default=False,
              help='Include entire threads when any message matches (can produce many results)')
@click.option('--forget', is_flag=True, default=False,
              help='Remove tracking for the subsystem (deletes config and lei queries)')
@click.pass_context
def track_subsystem(ctx: click.Context, subsystem_name: str,
                    maintainers: Optional[str], target: Optional[str],
                    labels: Tuple[str, ...], since: str,
                    threads: bool, forget: bool) -> None:
    """Track a kernel subsystem from MAINTAINERS file.

    Creates lei queries for the subsystem:

    \b
    - {name}-mailinglist: Messages to the subsystem mailing list(s)
    - {name}-patches: Patches touching subsystem files

    The configuration is written to conf.d/{subsystem_key}.toml

    Use --forget to remove tracking for a previously tracked subsystem.
    """
    # Handle --forget mode
    if forget:
        # Normalize the subsystem name to find the config and lei paths
        key = normalize_subsystem_name(subsystem_name)
        config_dir = get_xdg_config_dir()
        data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())

        # Find and remove config file
        config_file = config_dir / 'conf.d' / f'{key}.toml'
        if config_file.exists():
            config_file.unlink()
            logger.info('Removed config file: %s', config_file)
        else:
            logger.warning('Config file not found: %s', config_file)

        # Forget lei searches
        lei_base_path = data_dir / 'lei'
        for suffix in ('mailinglist', 'patches'):
            lei_path = lei_base_path / f'{key}-{suffix}'
            if lei_path.exists():
                try:
                    retcode, output = forget_lei_search(lei_path)
                    if retcode == 0:
                        logger.info('Forgot lei search: %s', lei_path)
                    else:
                        logger.error('Failed to forget lei search %s: %s',
                                     lei_path, output.decode())
                except PublicInboxError as e:
                    logger.error('Failed to forget lei search %s: %s', lei_path, str(e))
            else:
                logger.debug('Lei search not found: %s', lei_path)

        logger.info('Removed tracking for subsystem: %s', subsystem_name)
        return

    # Find MAINTAINERS file: explicit path, ./MAINTAINERS, or fetch from kernel.org
    if maintainers:
        maintainers_path = Path(maintainers)
        if not maintainers_path.exists():
            raise click.ClickException(f"MAINTAINERS file not found: {maintainers}")
    else:
        maintainers_path = Path('MAINTAINERS')
        if maintainers_path.exists():
            logger.debug('Using MAINTAINERS file from current directory')
        else:
            # Fetch from kernel.org as fallback
            data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())
            maintainers_path = get_maintainers_file(data_dir)

    config = ctx.obj.get('config', {})

    # Get catchall lists from config or use defaults
    main_config = config.get('main', {})
    catchall_lists_config = main_config.get('catchall_lists')
    if catchall_lists_config is not None:
        catchall_lists: Set[str] = set(catchall_lists_config)
        logger.debug('Using catchall_lists from config: %s', catchall_lists)
    else:
        catchall_lists = set(DEFAULT_CATCHALL_LISTS)
    targets = config.get('targets', {})

    # Auto-select target if only one exists
    if not target:
        if len(targets) == 1:
            target = list(targets.keys())[0]
            logger.debug('Using only configured target: %s', target)
        else:
            logger.critical('Multiple targets configured. Please specify one with -t.')
            logger.critical('Available targets: %s', ', '.join(targets.keys()))
            raise click.Abort()
    elif target not in targets:
        logger.critical('Target "%s" not found in configuration.', target)
        logger.critical('Known targets: %s', ', '.join(targets.keys()))
        raise click.Abort()

    # Get target instance for default labels
    target_service = get_target(ctx, target)

    # Parse MAINTAINERS file and get subsystem entry
    try:
        entry = get_subsystem(maintainers_path, subsystem_name)
    except KeyError as e:
        logger.critical('%s', str(e))
        raise click.Abort()

    logger.info('Found subsystem: %s', entry.name)

    # Generate normalized key for directory and config names
    key = normalize_subsystem_name(entry.name)
    logger.debug('Normalized key: %s', key)

    # Determine labels to use (supports comma-separated values)
    if labels:
        labels_list = parse_labels(labels)
    else:
        labels_list = target_service.DEFAULT_LABELS

    # Create lei search directories
    data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())
    lei_base_path = data_dir / 'lei'

    # Build and create queries
    queries_created = 0
    skipped_patterns: List[str] = []

    # 1. Mailing list query
    mailinglist_query, excluded_lists = build_mailinglist_query(entry, since, catchall_lists)
    if excluded_lists:
        logger.info('Excluding catch-all lists: %s', ', '.join(excluded_lists))
    if mailinglist_query:
        lei_path = lei_base_path / f'{key}-mailinglist'
        logger.info('Creating mailinglist query: %s', mailinglist_query)
        try:
            retcode, output = create_lei_query_search(mailinglist_query, lei_path,
                                                      threads=threads)
            if retcode != 0:
                logger.error('Lei query failed for mailinglist: %s', output.decode())
            else:
                # Initialize feed from start so all existing messages are delivered
                feed = LeiFeed(f'{key}-mailinglist', f'lei:{lei_path}')
                epoch = feed.get_highest_epoch()
                first_commit = feed.get_first_commit(epoch)
                if first_commit:
                    feed.init_feed(from_start=True)
                    # Also initialize delivery state from the same starting point
                    delivery_name = f'{key}-mailinglist'
                    feed.save_delivery_info(delivery_name, epoch=epoch,
                                            latest_commit=first_commit)
                else:
                    # No messages matched the query, initialize normally
                    logger.warning('No messages found for mailinglist query')
                    feed.init_feed(from_start=False)
                queries_created += 1
        except PublicInboxError as e:
            logger.error('Failed to create mailinglist query: %s', str(e))
    elif excluded_lists:
        logger.warning('No mailing lists remain after excluding catch-all lists')
    else:
        logger.warning('No mailing lists found for subsystem')

    # 2. Patches query
    patches_query, skipped = build_patches_query(entry, since)
    skipped_patterns.extend(skipped)
    if patches_query:
        lei_path = lei_base_path / f'{key}-patches'
        logger.info('Creating patches query: %s', patches_query)
        try:
            retcode, output = create_lei_query_search(patches_query, lei_path,
                                                      threads=threads)
            if retcode != 0:
                logger.error('Lei query failed for patches: %s', output.decode())
            else:
                # Initialize feed from start so all existing messages are delivered
                feed = LeiFeed(f'{key}-patches', f'lei:{lei_path}')
                epoch = feed.get_highest_epoch()
                first_commit = feed.get_first_commit(epoch)
                if first_commit:
                    feed.init_feed(from_start=True)
                    # Also initialize delivery state from the same starting point
                    delivery_name = f'{key}-patches'
                    feed.save_delivery_info(delivery_name, epoch=epoch,
                                            latest_commit=first_commit)
                else:
                    # No messages matched the query, initialize normally
                    logger.warning('No messages found for patches query')
                    feed.init_feed(from_start=False)
                queries_created += 1
        except PublicInboxError as e:
            logger.error('Failed to create patches query: %s', str(e))
    else:
        logger.warning('No file patterns found for subsystem')

    if queries_created == 0:
        logger.critical('No queries could be created for subsystem.')
        raise click.Abort()

    # Report skipped patterns
    if skipped_patterns:
        logger.warning('Skipped %d regex patterns (not supported by Xapian):', len(skipped_patterns))
        for pattern in skipped_patterns:
            logger.warning('  %s', pattern)

    # Generate and write configuration file
    config_dir = get_xdg_config_dir()
    conf_d = config_dir / 'conf.d'
    conf_d.mkdir(parents=True, exist_ok=True)

    config_content = generate_subsystem_config(
        key=key,
        target=target,
        labels=labels_list,
        lei_base_path=lei_base_path,
        since=since,
        subsystem_name=entry.name
    )

    config_file = conf_d / f'{key}.toml'
    config_file.write_text(config_content)

    logger.info('Created %d lei queries for subsystem "%s"', queries_created, entry.name)
    logger.info('Configuration written to: %s', config_file)
    logger.info('Target: %s, Labels: %s', target, ', '.join(labels_list))


@main.command()
@click.option('--add', '-a', 'addresses', default=None,
              help='Add address(es) to the bozofilter (comma-separated)')
@click.option('--reason', '-r', default=None,
              help='Reason for adding (included as comment)')
@click.option('--edit', '-e', 'do_edit', is_flag=True,
              help='Edit the bozofilter file in $EDITOR')
@click.option('--list', '-l', 'do_list', is_flag=True,
              help='List all addresses in the bozofilter')
@click.pass_context
def bozofilter(ctx: click.Context, addresses: Optional[str], reason: Optional[str],
               do_edit: bool, do_list: bool) -> None:
    """Manage the bozofilter for blocking unwanted senders.

    The bozofilter is a simple list of email addresses that will be
    skipped during mail delivery. Useful for blocking trolls, spammers,
    or bots.

    Examples:

        kgl bozofilter --add spammer@example.com

        kgl bozofilter --add "addr1@example.com,addr2@example.com" --reason "sends junk"

        kgl bozofilter --edit

        kgl bozofilter --list
    """
    config_dir = get_xdg_config_dir()

    if do_edit:
        if not edit_bozofilter(config_dir):
            raise click.Abort()
        return

    if do_list:
        bozo_set = load_bozofilter(config_dir)
        if not bozo_set:
            click.echo('Bozofilter is empty.')
        else:
            click.echo(f'Bozofilter contains {len(bozo_set)} address(es):')
            for addr in sorted(bozo_set):
                click.echo(f'  {addr}')
        return

    if addresses:
        # Parse comma-separated addresses
        addr_list = [a.strip() for a in addresses.split(',') if a.strip()]
        if not addr_list:
            logger.error('No valid addresses provided')
            raise click.Abort()

        added = add_to_bozofilter(config_dir, addr_list, reason=reason)
        if added > 0:
            click.echo(f'Added {added} address(es) to bozofilter.')
        else:
            click.echo('No new addresses added (all already in filter).')
        return

    # No action specified - show help
    ctx.invoke(bozofilter, do_list=True)


if __name__ == '__main__':
    main()
