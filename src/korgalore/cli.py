"""Command-line interface for korgalore."""

import hashlib
import io
import logging
import os
import re
import subprocess
import sys
import tomllib
import urllib.parse
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any, TextIO

import click
import click_log  # type: ignore[import-untyped]
import requests
from liblore.utils import clean_header, get_msgid_from_url, msg_get_subject, parse_message, split_and_dedupe_as_bytes

import liblore
from korgalore import (
    AuthenticationError,
    ConfigurationError,
    FeedLockedError,
    GitError,
    PublicInboxError,
    RemoteError,
    StateError,
    __version__,
    _init_git_user_agent,
    close_requests_session,
    format_key_for_display,
    get_requests_session,
    make_lore_node,
)
from korgalore.bozofilter import add_to_bozofilter, edit_bozofilter, is_bozofied, load_bozofilter
from korgalore.digest import (
    DIGEST_KEYS,
    DigestInfo,
    DigestJob,
    DigestSchedule,
    flocked,
    group_threads,
    render_digest_parts,
    roots_to_look_up,
    strip_reply_prefixes,
)
from korgalore.dummy_target import DummyTarget
from korgalore.gmail_target import GmailTarget, make_gmail_credentials
from korgalore.imap_target import ImapTarget
from korgalore.jmap_target import JmapTarget
from korgalore.lei_feed import LeiFeed
from korgalore.lore_feed import LoreFeed
from korgalore.maildir_target import MaildirTarget
from korgalore.maintainers import (
    DEFAULT_CATCHALL_LISTS,
    build_mailinglist_query,
    build_patches_query,
    generate_subsystem_config,
    get_subsystem,
    normalize_subsystem_name,
)
from korgalore.pipe_target import PipeTarget
from korgalore.summarizer import (
    Summarizer,
    SummaryCache,
    SummaryRun,
    estimate_summaries,
    estimate_tokens,
    make_summarizer,
)
from korgalore.tracking import (
    TrackingManifest,
    TrackStatus,
    create_lei_query_search,
    create_lei_thread_search,
    forget_lei_search,
    update_lei_search,
    write_archive_description,
)

logger = logging.getLogger('korgalore')
click_log.basic_config(logger)

# Sentinel value for messages skipped due to bozofilter
SKIPPED_BOZOFILTER = '__SKIPPED_BOZOFILTER__'
# Sentinel value for public-inbox 'rm' commits (message removals)
SKIPPED_NOOP_COMMIT = '__SKIPPED_NOOP_COMMIT__'

# URL to fetch MAINTAINERS file from kernel.org
MAINTAINERS_URL = 'https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/plain/MAINTAINERS'

# Maximum age of cached MAINTAINERS file in seconds (24 hours)
MAINTAINERS_CACHE_MAX_AGE = 24 * 60 * 60


def progress_file(hide_bar: bool) -> TextIO | None:
    """Pick the stream a progress bar should render to.

    Click grew a ``progressbar(hidden=...)`` argument in 8.3.0, but every
    release before that already suppresses the bar when its output stream is
    not a terminal -- it emits the label once and nothing else. Handing it a
    throwaway in-memory stream therefore hides the bar on every Click we
    support, old and new, with no version sniffing.

    Args:
        hide_bar: Whether the progress bar should be suppressed.

    Returns:
        A discard stream when hiding, or None to let Click use stdout.
    """
    return io.StringIO() if hide_bar else None


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
            f'Failed to fetch MAINTAINERS file from {MAINTAINERS_URL}: {e}\n'
            'Use -m/--maintainers to specify a local copy.'
        ) from e

    # Cache the file
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(response.content)
    logger.debug('Cached MAINTAINERS file to %s', cache_path)

    return cache_path


def parse_labels(labels: tuple[str, ...]) -> list[str]:
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
    result: list[str] = []
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


def resolve_target_name(target: str | None, targets: dict[str, Any]) -> str:
    """Return the target name to use, defaulting to the first one configured.

    Click passes None for an omitted --target, so every command taking one
    has to fall back to the first configured target. Returning a plain str
    saves each caller from re-proving the value is not None.

    Raises:
        click.Abort: if the named target is unknown, or if none are
            configured and there is therefore nothing to default to.
    """
    if target:
        if target not in targets:
            logger.critical('Target "%s" not found in configuration.', target)
            logger.critical('Known targets: %s', ', '.join(targets.keys()))
            raise click.Abort()
        return target

    if not targets:
        logger.critical('No targets are configured, so no default is available.')
        raise click.Abort()

    default = next(iter(targets))
    logger.info('Using default target: %s', default)
    return default


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
            details=details,
            interactive=interactive,
        )
    elif target_type == 'maildir':
        service = get_maildir_target(identifier=identifier, maildir_path=details.get('path', ''))
    elif target_type == 'jmap':
        service = get_jmap_target(
            identifier=identifier,
            server=details.get('server', ''),
            username=details.get('username', ''),
            token=details.get('token', None),
            token_file=details.get('token_file', None),
            timeout=details.get('timeout', 60),
            reqsession=get_requests_session(),
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
            interactive=interactive,
        )
    elif target_type == 'pipe':
        service = get_pipe_target(identifier=identifier, command=details.get('command', ''))
    elif target_type == 'dummy':
        service = get_dummy_target(identifier=identifier)
    else:
        logger.critical('Unknown target type "%s" for target "%s".', target_type, identifier)
        logger.critical('Supported types: gmail, maildir, jmap, imap, pipe, dummy')
        raise click.Abort()

    ctx.obj['targets'][identifier] = service

    # Check if Gmail target needs authentication (in non-interactive/GUI mode)
    # Note: IMAP OAuth2 targets handle this during connect() instead, which
    # allows the 'auth' command to work properly.
    if isinstance(service, GmailTarget) and service.needs_auth:
        raise AuthenticationError(
            f"Gmail target '{identifier}' requires authentication.", target_id=identifier, target_type='gmail'
        )

    return service


def get_gmail_target(
    identifier: str,
    details: Mapping[str, Any],
    interactive: bool = True,
) -> GmailTarget:
    """Create a Gmail target service instance."""
    try:
        creds = make_gmail_credentials(
            identifier=identifier,
            details=details,
            interactive=interactive,
            config_dir=get_xdg_config_dir(),
        )
        gt = GmailTarget(identifier=identifier, creds=creds)
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort() from fe

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
        raise click.Abort() from fe

    return mt


def get_dummy_target(identifier: str) -> DummyTarget:
    """Create a Dummy target service instance."""
    return DummyTarget(identifier=identifier)


def get_jmap_target(
    identifier: str,
    server: str,
    username: str,
    token: str | None,
    token_file: str | None,
    timeout: int,
    reqsession: requests.Session | None = None,
) -> JmapTarget:
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
            reqsession=reqsession,
        )
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort() from fe

    return jt


def get_imap_target(
    identifier: str,
    server: str,
    username: str,
    folder: str,
    password: str | None,
    password_file: str | None,
    timeout: int,
    auth_type: str = 'password',
    client_id: str | None = None,
    tenant: str = 'common',
    token: str | None = None,
    interactive: bool = True,
) -> ImapTarget:
    """Create an IMAP target service instance."""
    if not server:
        logger.critical('No server specified for IMAP target: %s', identifier)
        raise click.Abort()

    if not username:
        logger.critical('No username specified for IMAP target: %s', identifier)
        raise click.Abort()

    # Password authentication - requires password or password_file
    if auth_type != 'oauth2' and not password and not password_file:
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
            interactive=interactive,
        )
    except ConfigurationError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort() from fe

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
        raise click.Abort() from fe

    return pt


def resolve_feed_url(feed_value: str, config: dict[str, Any]) -> str:
    """Resolve a feed name or URL to its full URL."""
    # If it's already a URL, return as-is
    if feed_value.startswith(('https:', 'lei:')):
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


def get_feed_identifier(feed_value: str, config: dict[str, Any]) -> str | None:
    """Get a stable identifier for a feed to use as directory name.

    Args:
        feed_value: The feed value from delivery config (name or URL)
        config: Full configuration dict

    Returns:
        Directory name to use for this feed, or None for LEI feeds (handled separately)
    """
    # Named feed: use the feed name as directory
    if not feed_value.startswith(('https:', 'http:', 'lei:')):
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


def validate_config_file(cfgpath: Path) -> tuple[bool, str]:
    """Validate a TOML configuration file.

    Args:
        cfgpath: Path to the configuration file.

    Returns:
        A tuple of (is_valid, error_message). If valid, error_message is empty.
    """
    if not cfgpath.exists():
        return False, f'Configuration file not found: {cfgpath}'

    try:
        with cfgpath.open('rb') as cf:
            tomllib.load(cf)
        return True, ''
    except tomllib.TOMLDecodeError as e:
        return False, f'TOML syntax error: {e}'
    except Exception as e:
        return False, f'Error reading config: {e}'


def merge_config(base: dict[str, Any], extra: dict[str, Any]) -> None:
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


def load_config(cfgfile: Path) -> dict[str, Any]:
    """Load and parse the TOML configuration file and conf.d/*.toml files."""
    config: dict[str, Any] = {}

    if not cfgfile.exists():
        logger.error('Config file not found: %s', str(cfgfile))
        click.Abort()

    try:
        logger.debug('Loading config from %s', str(cfgfile))

        with cfgfile.open('rb') as cf:
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
                with toml_file.open('rb') as cf:
                    extra = tomllib.load(cf)
                merge_config(config, extra)

        logger.debug(
            'Config loaded with %s targets, %s deliveries, and %s feeds',
            len(config.get('targets', {})),
            len(config.get('deliveries', {})),
            len(config.get('feeds', {})),
        )

        return config

    except Exception as e:
        logger.error('Error loading config: %s', str(e))
        logger.debug('Traceback:', exc_info=True)
        raise click.Abort() from e


def retry_failed_commits(
    feed_dir: Path,
    pi_feed: LeiFeed | LoreFeed,
    target_service: Any,
    labels: list[str],
    delivery_name: str,
    subfolder: str | None = None,
) -> None:
    """Retry previously failed message deliveries for a specific delivery."""
    failed_commits = pi_feed.get_failed_commits_for_delivery(delivery_name)

    if not failed_commits:
        return

    logger.info('Retrying %d previously failed commits', len(failed_commits))

    for epoch, commit_hash in failed_commits:
        if pi_feed.is_noop_commit(epoch, commit_hash):
            logger.debug('Skipping no-op commit %s on retry', commit_hash)
            pi_feed.mark_successful_delivery(delivery_name, epoch, commit_hash)
            continue
        try:
            raw_message = pi_feed.get_message_at_commit(epoch, commit_hash)
        except (StateError, GitError) as e:
            # XXX: did the feed get rebased? Skip for now, but handle later.
            logger.debug('Skipping retry of commit %s: %s', commit_hash, str(e))
            continue

        try:
            target_service.import_message(raw_message, labels=labels, subfolder=subfolder)
            logger.debug('Successfully retried commit %s', commit_hash)
            pi_feed.mark_successful_delivery(delivery_name, epoch, commit_hash, message=raw_message)
        except RemoteError:
            pi_feed.mark_failed_delivery(delivery_name, epoch, commit_hash)

    # Save updated tracking files
    pi_feed.feed_unlock()


def deliver_commit(
    delivery_name: str,
    target: Any,
    feed: LeiFeed | LoreFeed,
    epoch: int,
    commit: str,
    labels: list[str],
    was_failing: bool = False,
    bozofilter: set[str] | None = None,
    subfolder: str | None = None,
) -> str | None:
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
        subfolder: Optional subfolder for IMAP/Maildir targets.

    Returns:
        The Message-ID of the delivered message on success, None on failure or skip.
    """
    # Skip public-inbox commits that carry no message (rm, purged, etc.)
    # is_noop_commit raises GitError when the commit object is missing
    # locally (bad object), so the check must be inside the try/except
    # to avoid crashing the retry loop.
    raw_message: bytes | None = None
    try:
        if feed.is_noop_commit(epoch, commit):
            logger.debug('Skipping no-op commit %s in epoch %d', commit, epoch)
            feed.mark_successful_delivery(delivery_name, epoch, commit, was_failing=was_failing)
            return SKIPPED_NOOP_COMMIT
        raw_message = feed.get_message_at_commit(epoch, commit)
        target.connect()
        msg = parse_message(raw_message)
        msgid: str = msg.get('Message-ID', '')

        # Check bozofilter before delivering
        if bozofilter:
            from_header = msg.get('From', '')
            if is_bozofied(from_header, bozofilter):
                logger.debug('Skipping bozofied sender: %s', from_header)
                # Mark as successful to avoid retrying
                feed.mark_successful_delivery(
                    delivery_name, epoch, commit, message=raw_message, was_failing=was_failing
                )
                return SKIPPED_BOZOFILTER

        if logger.isEnabledFor(logging.DEBUG):
            subject = msg_get_subject(msg) or '(no subject)'
            logger.debug(' -> %s', subject)
        target.import_message(
            raw_message,
            labels=labels,
            feed_name=format_key_for_display(feed.feed_key),
            delivery_name=delivery_name,
            subfolder=subfolder,
        )
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


# Links for lei feeds point here, since lei results can come from any list
LEI_LINK_BASE = 'https://lore.kernel.org/all'
# How many thread roots to fetch from the archive for one digest
ROOT_LOOKUPS_MAX = 25


def collect_digest(
    delivery_name: str,
    feed: LeiFeed | LoreFeed,
    schedule: DigestSchedule,
    job: DigestJob,
    bozofilter: set[str] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> bool:
    """Start a digest job when a digest is due: the collect stage.

    The digest covers everything since the last digest. The very first one
    covers one period back (a day or a week), using the feed history.

    This is the only stage that reads the feed's git repositories, so the
    feed must be locked. The messages and the pointer to save after
    delivery are copied into the job, and the later stages use only those.
    The subjects from look_up_root_subjects() go into the job too.

    Returns:
        True when a job was created. False when nothing was due, or when
        the period was empty and send_empty is off; in that case the
        state is saved right away.
    """
    if now is None:
        now = datetime.now(UTC)
    last_sent = feed.load_digest_sent(delivery_name)
    if not force and not schedule.is_due(last_sent, now):
        logger.debug('Digest %s is not due yet', delivery_name)
        return False

    period = read_digest_period(delivery_name, feed, schedule, bozofilter, now, last_sent)
    if not period.messages and not schedule.send_empty and not force and period.history_start is None:
        logger.info('Digest %s: no activity, nothing to send', delivery_name)
        feed.save_delivery_entry(delivery_name, period.pointer, digest_sent=now)
        return False

    job.create(
        period.messages,
        {
            'period_start': period.start.isoformat(),
            'period_end': now.isoformat(),
            'history_start': period.history_start.isoformat() if period.history_start else None,
            'pointer': period.pointer,
            'root_subjects': look_up_root_subjects(delivery_name, feed, period.messages),
        },
    )
    return True


def look_up_root_subjects(delivery_name: str, feed: LeiFeed | LoreFeed, messages: Sequence[bytes]) -> dict[str, str]:
    """Fetch the subjects of the roots that roots_to_look_up() names.

    Only a lore feed can fetch messages. The lookups happen while the
    feed is locked, so at most ROOT_LOOKUPS_MAX are made, and the first
    failure stops them for this digest, so a busy server is not asked
    again and again; those threads keep the name of their oldest message. A root
    that is a reply itself, because the list never got the first message,
    has no better name and is left out.

    Returns:
        The subjects, keyed by the root's Message-ID.
    """
    if not isinstance(feed, LoreFeed):
        return {}
    subjects: dict[str, str] = {}
    roots = roots_to_look_up(group_threads([parse_message(raw) for raw in messages]))
    if len(roots) > ROOT_LOOKUPS_MAX:
        logger.debug('Digest %s: looking up %d of %d thread roots', delivery_name, ROOT_LOOKUPS_MAX, len(roots))
        roots = roots[:ROOT_LOOKUPS_MAX]
    for msgid in roots:
        try:
            raw = feed.get_message_by_msgid(msgid)
        except liblore.LibloreError as e:
            logger.warning('Digest %s: cannot look up the first messages of threads: %s', delivery_name, e)
            break
        subject = clean_header(parse_message(raw).get('Subject'))
        if subject and strip_reply_prefixes(subject) == subject:
            subjects[msgid] = subject
    return subjects


def _job_root_subjects(job: DigestJob, state: Mapping[str, Any]) -> dict[str, str]:
    """The root subjects saved in a job. Older jobs have none."""
    saved = state.get('root_subjects') or {}
    if not isinstance(saved, dict):
        raise StateError(f'Bad digest job in {job.path}: root_subjects is not a map of subjects')
    subjects: dict[str, str] = {}
    for msgid, subject in saved.items():
        if not isinstance(subject, str):
            raise StateError(f'Bad digest job in {job.path}: root_subjects has a subject that is not text')
        subjects[str(msgid)] = subject
    return subjects


@dataclass
class DigestPeriod:
    """The messages of one digest period, as read from the feed."""

    start: datetime
    messages: list[bytes]
    # Where the delivery pointer goes once the digest is sent
    pointer: dict[str, Any] | None
    # Set when the feed history has a gap: the oldest message we still have
    history_start: datetime | None = None


def read_digest_period(
    delivery_name: str,
    feed: LeiFeed | LoreFeed,
    schedule: DigestSchedule,
    bozofilter: set[str] | None,
    now: datetime,
    last_sent: datetime | None,
) -> DigestPeriod:
    """Read the messages for a digest that ends now, without saving anything.

    The feed must be locked. last_sent is the feed's load_digest_sent()
    for this delivery, which the caller has already read for the due check.
    """
    period_start = schedule.period_start(last_sent, now)
    history_start: datetime | None = None
    # Without a pointer (the first digest, or a feed that was empty last
    # time), collect by commit date instead
    if last_sent is None or not feed.has_delivery_pointer(delivery_name):
        logger.debug('Collecting digest %s since %s', delivery_name, period_start.isoformat())
        commits = feed.get_commits_since(period_start)
    else:
        commits = feed.get_latest_commits_for_delivery(delivery_name)
        history_start = feed.find_history_gap(delivery_name, commits)
        if history_start is not None:
            logger.warning(
                'Digest %s: the local feed history starts at %s, older messages are missing',
                delivery_name,
                history_start.isoformat(),
            )

    messages: list[bytes] = []
    for epoch, commit in commits:
        try:
            if feed.is_noop_commit(epoch, commit):
                continue
            raw_message = feed.get_message_at_commit(epoch, commit)
        except (GitError, StateError) as e:
            logger.warning('Digest %s: skipping commit %s: %s', delivery_name, commit, e)
            continue
        if bozofilter and is_bozofied(parse_message(raw_message).get('From', ''), bozofilter):
            continue
        messages.append(raw_message)

    # The pointer moves to the last commit we looked at, even when that one
    # was skipped. With no commits at all, it moves to the top of the feed.
    if commits:
        last_epoch, last_commit = commits[-1]
    else:
        last_epoch = feed.get_highest_epoch()
        last_commit = feed.get_top_commit(last_epoch)
    pointer: dict[str, Any] | None = None
    if last_commit:
        pointer = {'epoch': last_epoch, 'entry': feed.make_delivery_entry(last_epoch, last_commit)}
    return DigestPeriod(period_start, messages, pointer, history_start)


def summarize_digest_job(
    delivery_name: str,
    schedule: DigestSchedule,
    job: DigestJob,
    run: SummaryRun,
    now: datetime | None = None,
) -> None:
    """Summarize the threads of a collected job: the summarize stage.

    This is the slow stage, so it runs in the digest worker. Each summary
    goes into the cache as soon as it is made, so a worker that is
    stopped halfway does not have to start again.
    """
    if now is None:
        now = datetime.now(UTC)
    msgs = [parse_message(raw) for raw in job.messages()]
    threads = group_threads(msgs, _job_root_subjects(job, job.load()))
    summaries = run.summarize_threads(
        f'Digest {delivery_name}',
        threads,
        msgs,
        now,
        max_summaries=schedule.max_summaries,
        instructions=schedule.summary_instructions,
    )
    job.write_summaries(run.summarizer.model, summaries)


def render_digest_job(
    delivery_name: str,
    feed: LeiFeed | LoreFeed,
    schedule: DigestSchedule,
    job: DigestJob,
    now: datetime | None = None,
) -> None:
    """Turn a collected or summarized job into digest parts: the render stage.

    Only the job is read, never the feed's git repositories, so this does
    not need the feed lock. A job that skipped the summarize stage makes
    a plain digest.
    """
    state = job.load()
    try:
        period_start = datetime.fromisoformat(state['period_start'])
        period_end = datetime.fromisoformat(state['period_end'])
        history_start = datetime.fromisoformat(state['history_start']) if state.get('history_start') else None
    except (KeyError, TypeError, ValueError) as e:
        raise StateError(f'Bad digest job in {job.path}: {e}') from e
    # Only the summarize stage stores a model
    model = state.get('model')
    summaries = job.summaries(state)
    threads = group_threads([parse_message(raw) for raw in job.messages()], _job_root_subjects(job, state))
    info = DigestInfo(
        feed_name=format_key_for_display(feed.feed_key),
        delivery_name=delivery_name,
        link_base=feed.feed_url if isinstance(feed, LoreFeed) else LEI_LINK_BASE,
        period_start=period_start.astimezone(),
        period_end=period_end.astimezone(),
        from_addr=schedule.from_addr,
        body_format=schedule.body_format,
        model=str(model) if model else None,
        history_start=history_start.astimezone() if history_start else None,
    )
    job.write_parts(render_digest_parts(info, threads, summaries, now=now))


def deliver_digest_job(
    delivery_name: str,
    feed: LeiFeed | LoreFeed,
    target: Any,
    labels: list[str],
    subfolder: str | None,
    job: DigestJob,
) -> list[EmailMessage]:
    """Send the parts of a rendered job, in order: the deliver stage.

    Each part is removed from the job as soon as the target accepts it.
    The pointer collected with the job is saved after the last part, and
    then the job is removed. No git data is read.

    Returns:
        The parts that were sent in this call.
    """
    state = job.load()
    try:
        period_end = datetime.fromisoformat(state['period_end'])
    except (KeyError, TypeError, ValueError) as e:
        raise StateError(f'Bad digest job in {job.path}: {e}') from e

    sent: list[EmailMessage] = []
    pending = job.pending()
    if pending:
        target.connect()
    for part_file in pending:
        raw = part_file.read_bytes()
        target.import_message(
            raw,
            labels=labels,
            feed_name=format_key_for_display(feed.feed_key),
            delivery_name=delivery_name,
            subfolder=subfolder,
        )
        part_file.unlink()
        part = BytesParser(_class=EmailMessage, policy=policy.default).parsebytes(raw)
        assert isinstance(part, EmailMessage)
        sent.append(part)
    feed.save_delivery_entry(delivery_name, state.get('pointer'), digest_sent=period_end)
    job.clear()
    return sent


def finish_digest_job(
    delivery_name: str,
    feed: LeiFeed | LoreFeed,
    target: Any,
    labels: list[str],
    subfolder: str | None,
    schedule: DigestSchedule,
    job: DigestJob,
    now: datetime | None = None,
    run: SummaryRun | None = None,
) -> list[EmailMessage]:
    """Run the stages that are left in a job, from where it stopped.

    The caller must hold the job lock. The feed lock is not needed. The
    summarize stage needs run; without it, a collected job makes a plain
    digest.

    Returns:
        The digest parts that were sent.
    """
    state = job.load()
    try:
        period_end = datetime.fromisoformat(state['period_end'])
    except (KeyError, TypeError, ValueError) as e:
        raise StateError(f'Bad digest job in {job.path}: {e}') from e
    last_sent = feed.load_digest_sent(delivery_name)
    if last_sent is not None and last_sent >= period_end:
        # State was saved, but we stopped before removing the job
        logger.debug('Digest %s: job was already delivered, removing it', delivery_name)
        job.clear()
        return []

    if state['stage'] == DigestJob.COLLECTED and run is not None:
        summarize_digest_job(delivery_name, schedule, job, run, now=now)
    if state['stage'] != DigestJob.RENDERED:
        render_digest_job(delivery_name, feed, schedule, job, now=now)
    return deliver_digest_job(delivery_name, feed, target, labels, subfolder, job)


def send_digest(
    delivery_name: str,
    feed: LeiFeed | LoreFeed,
    target: Any,
    labels: list[str],
    subfolder: str | None,
    schedule: DigestSchedule,
    bozofilter: set[str] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> list[EmailMessage]:
    """Build and deliver a digest when one is due, running all job stages.

    A job left from an earlier run goes first and resumes from its last
    finished stage, even when no new digest is due. See DigestJob for the
    stages. A job that the digest worker is working on is left alone.

    With force, a digest is sent even when it is not due and even when
    there is no activity.

    Returns:
        The digest parts that were sent. The list is empty when nothing
        was due or the period was empty and send_empty is off.

    Raises:
        Whatever the feed or the target raises; the caller decides what to do.
    """
    job = DigestJob(feed.get_digest_job_dir(delivery_name))
    with job.locked() as have_lock:
        if not have_lock:
            logger.info('Digest %s: the digest worker is still working on it', delivery_name)
            return []
        if job.exists():
            logger.info('Digest %s: finishing the digest from the last run', delivery_name)
        else:
            # A job without a job file was never finished, so start again
            job.clear()
            if not collect_digest(delivery_name, feed, schedule, job, bozofilter, force=force, now=now):
                return []
        return finish_digest_job(delivery_name, feed, target, labels, subfolder, schedule, job, now=now)


def queue_digest(
    delivery_name: str,
    feed: LeiFeed | LoreFeed,
    schedule: DigestSchedule,
    bozofilter: set[str] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> bool:
    """Collect a digest for the digest worker, when one is due.

    Only the collect stage runs here, under the feed lock. A job that is
    already waiting, or that the worker is working on, is left alone, so a
    new digest is collected only after the last one was sent.

    Returns:
        True when a job is waiting for the worker.
    """
    job = DigestJob(feed.get_digest_job_dir(delivery_name))
    with job.locked() as have_lock:
        if not have_lock:
            return True
        if job.exists():
            return True
        job.clear()
        return collect_digest(delivery_name, feed, schedule, job, bozofilter, force=force, now=now)


# The digest worker holds this lock, in the data dir, while it runs
DIGEST_WORKER_LOCK = 'digest-worker.lock'
DIGEST_WORKER_LOG = 'digest-worker.log'
# kgl pull touches this file, in the data dir, when it has collected a
# digest, so a worker that is about to exit looks once more
DIGEST_WORKER_POKE = 'digest-worker.poke'
# Summaries are cached here, in the data dir, and shared by all feeds
SUMMARY_CACHE_DIR = 'summaries'
DIGEST_WORKER_MODES = ('spawn', 'external')
# Workers started by this process. The GUI runs for days, so finished
# workers must be reaped, or they stay around as zombies.
_digest_workers: list['subprocess.Popen[bytes]'] = []


def get_digest_worker_mode(config: dict[str, Any]) -> str:
    """Read how the digest worker is started from the [digests] section."""
    mode = config.get('digests', {}).get('worker', 'spawn')
    if mode not in DIGEST_WORKER_MODES:
        raise ConfigurationError(f'[digests] worker must be one of: {", ".join(DIGEST_WORKER_MODES)} (got {mode!r})')
    return str(mode)


def digest_worker_running(data_dir: Path) -> bool:
    """True when a digest worker holds the worker lock."""
    with flocked(data_dir / DIGEST_WORKER_LOCK) as have_lock:
        return not have_lock


def poke_digest_worker(data_dir: Path) -> None:
    """Tell a running worker that a new job is waiting.

    The worker looks for jobs once more before it exits when this file is
    there, so a job that kgl pull collects just as the worker finishes its
    last round is not left until the next pull. It is written before the
    worker lock is checked: a worker that is still running sees it, and a
    worker that has exited leaves it for the next one to consume.
    """
    (data_dir / DIGEST_WORKER_POKE).touch()


def spawn_digest_worker(data_dir: Path, cfgpath: Path | None) -> bool:
    """Start the digest worker in the background, unless one is running.

    The worker runs as "python -m korgalore digest --work" in its own
    session, so it keeps running after kgl pull exits, and it logs to
    digest-worker.log in the data dir.

    Returns:
        True when a worker was started.
    """
    _digest_workers[:] = [proc for proc in _digest_workers if proc.poll() is None]
    poke_digest_worker(data_dir)
    if digest_worker_running(data_dir):
        logger.debug('The digest worker is already running')
        return False
    log_path = data_dir / DIGEST_WORKER_LOG
    cmd = [sys.executable, '-m', 'korgalore']
    if cfgpath is not None:
        cmd += ['--cfgfile', str(cfgpath)]
    cmd += ['--logfile', str(log_path), 'digest', '--work']
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _digest_workers.append(proc)
    logger.info('Started the digest worker (pid %d), its log is %s', proc.pid, log_path)
    return True


def start_digest_worker(ctx: click.Context) -> None:
    """Make sure waiting digest jobs get a worker, the way the config says."""
    if ctx.obj.get('digest_worker', 'spawn') == 'external':
        poke_digest_worker(ctx.obj['data_dir'])
        logger.info('Digests are waiting for "kgl digest --work"')
        return
    spawn_digest_worker(ctx.obj['data_dir'], ctx.obj.get('cfgpath'))


def run_due_digests(
    ctx: click.Context,
    delivery_names: list[str],
    force: bool = False,
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, list[str]]:
    """Send the digests that are due, logging failures and moving on.

    Feeds must already be locked and updated. Digests that need the digest
    worker are only collected here, and the worker is started for them.

    Returns:
        A mapping of delivery name to the Message-IDs of the digest parts
        sent. Digests that sent nothing are left out.
    """
    schedules: dict[str, DigestSchedule] = ctx.obj.get('digest_schedules', {})
    bozo_set = ctx.obj.get('bozofilter', set())
    sent: dict[str, list[str]] = {}
    used_targets: dict[str, Any] = {}
    waiting = False
    for dname in delivery_names:
        feed, target, labels, subfolder = ctx.obj['deliveries'][dname]
        if status_callback:
            status_callback(f'Checking digest {format_key_for_display(dname)}...')
        schedule = schedules[dname]
        if schedule.needs_worker:
            try:
                waiting = queue_digest(dname, feed, schedule, bozo_set, force=force) or waiting
            except Exception as e:
                logger.error('Could not collect digest %s, will retry on the next run: %s', dname, e)
                logger.debug('Traceback:', exc_info=True)
            continue
        try:
            parts = send_digest(dname, feed, target, labels, subfolder, schedule, bozo_set, force=force)
        except AuthenticationError:
            # The GUI shows an auth button for this, so let it through
            raise
        except Exception as e:
            logger.error('Could not send digest %s, will retry on the next run: %s', dname, e)
            logger.debug('Traceback:', exc_info=True)
            continue
        finally:
            used_targets[target.identifier] = target
        for part in parts:
            logger.info('Sent digest %s: %s', dname, part['Subject'])
        if parts:
            sent[dname] = [str(part['Message-ID']) for part in parts]

    for target in used_targets.values():
        if hasattr(target, 'disconnect'):
            target.disconnect()
    if waiting:
        start_digest_worker(ctx)
    return sent


def run_digest_worker(ctx: click.Context, delivery_names: list[str]) -> dict[str, list[str]]:
    """Finish the waiting digest jobs, one after another, until none are left.

    This is the digest worker. It does not lock or update feeds: jobs
    already hold everything they need, so kgl pull keeps running while the
    worker takes its time. Only one worker runs at a time, because a local
    model handles one request at a time anyway. Jobs that kgl pull
    collects while the worker runs are picked up too: the worker waits for
    a job that pull is still collecting, and pull pokes the worker (see
    poke_digest_worker()) so a job collected during the worker's last
    round is not missed. A job that fails is left for the next worker, so
    a target that is down cannot keep this one busy forever.

    Returns:
        A mapping of delivery name to the Message-IDs of the digest parts
        sent. Nothing is sent when another worker is running.
    """
    schedules: dict[str, DigestSchedule] = ctx.obj.get('digest_schedules', {})
    sent: dict[str, list[str]] = {}
    with flocked(ctx.obj['data_dir'] / DIGEST_WORKER_LOCK) as have_lock:
        if not have_lock:
            logger.info('Another digest worker is running')
            return sent
        used_targets: dict[str, Any] = {}
        failed: set[str] = set()
        cache = SummaryCache(ctx.obj['data_dir'] / SUMMARY_CACHE_DIR)
        summarizers: dict[str, Summarizer] = ctx.obj.get('summarizers', {})
        # One run per summarizer, so failures in a row count across digests
        runs = {name: SummaryRun(summarizer, cache) for name, summarizer in summarizers.items()}
        poke = ctx.obj['data_dir'] / DIGEST_WORKER_POKE
        while True:
            poke.unlink(missing_ok=True)
            busy = False
            for dname in delivery_names:
                if dname in failed:
                    continue
                feed, target, labels, subfolder = ctx.obj['deliveries'][dname]
                job = DigestJob(feed.get_digest_job_dir(dname))
                # kgl pull holds the job lock only while it collects
                with job.locked(wait=True):
                    if not job.exists():
                        continue
                    busy = True
                    used_targets[target.identifier] = target
                    logger.info('Digest %s: finishing the digest', dname)
                    schedule = schedules[dname]
                    run = runs.get(schedule.summarizer) if schedule.summarizer else None
                    try:
                        parts = finish_digest_job(dname, feed, target, labels, subfolder, schedule, job, run=run)
                    except Exception:
                        # Nobody watches the worker, so keep the traceback in its log
                        logger.exception('Could not finish digest %s, will retry on the next run', dname)
                        failed.add(dname)
                        continue
                for part in parts:
                    logger.info('Sent digest %s: %s', dname, part['Subject'])
                if parts:
                    sent.setdefault(dname, []).extend(str(part['Message-ID']) for part in parts)
            if not busy and not poke.exists():
                break

        for target in used_targets.values():
            if hasattr(target, 'disconnect'):
                target.disconnect()
        removed = cache.prune(datetime.now(UTC))
        if removed:
            logger.debug('Removed %d old cached summaries', removed)
    return sent


def estimate_digest(
    delivery_name: str,
    feed: LeiFeed | LoreFeed,
    schedule: DigestSchedule,
    summarizer: Summarizer | None,
    cache: SummaryCache,
    bozofilter: set[str] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Describe what the next digest would send to its summarizer.

    A waiting job is what the worker summarizes next, so it is used when
    there is one. Otherwise the messages are read as if the digest ended
    now. The model is never called and nothing is saved, so this is safe
    to run at any time. The feed must be locked.

    Returns:
        The report, one line per item.
    """
    if now is None:
        now = datetime.now(UTC)
    job = DigestJob(feed.get_digest_job_dir(delivery_name))
    with job.locked() as have_lock:
        if not have_lock:
            return [f'Digest {delivery_name}: the digest worker is working on it now']
        if job.exists():
            stage = job.load()['stage']
            if stage != DigestJob.COLLECTED:
                return [f'Digest {delivery_name}: already summarized, waiting to be sent']
            raw_msgs = job.messages()
            when = 'waiting for the digest worker'
        else:
            last_sent = feed.load_digest_sent(delivery_name)
            raw_msgs = read_digest_period(delivery_name, feed, schedule, bozofilter, now, last_sent).messages
            due = schedule.is_due(last_sent, now)
            when = 'due now' if due else 'not due yet, counting up to now'

    msgs = [parse_message(raw) for raw in raw_msgs]
    threads = group_threads(msgs)
    lines = [
        f'Digest {delivery_name} ({when})',
        f'  {len(threads):,} threads, {len(msgs):,} messages',
    ]
    if summarizer is None:
        lines.append('  Plain digest, nothing to summarize')
        return lines

    where = 'on this machine' if summarizer.is_local else 'NOT on this machine'
    lines.append(f'  Summarizer {summarizer.name}, model {summarizer.model}, {where}')
    est = estimate_summaries(summarizer, cache, threads, msgs, schedule.max_summaries, schedule.summary_instructions)
    lines.append(
        f'  No summary needed: {est.not_needed:,}, cached: {est.cached:,}, over max_summaries: {est.over_limit:,}'
    )
    lines.append(f'  Model calls: {est.calls:,} ({est.incremental:,} build on an earlier summary)')
    if est.calls:
        lines.append(
            f'  Input: {est.input_chars:,} chars, about {estimate_tokens(est.input_chars):,} tokens; '
            f'largest prompt {est.largest_chars:,} chars'
        )
    if est.cut:
        lines.append(
            f'  Cut to max_input_chars ({summarizer.max_input_chars:,}): {est.cut:,} prompts, '
            f'{est.cut_chars:,} chars left out'
        )
    return lines


def run_digest_estimates(ctx: click.Context, delivery_names: list[str], now: datetime | None = None) -> None:
    """Print estimate_digest() for each digest. Feeds must already be locked."""
    schedules: dict[str, DigestSchedule] = ctx.obj.get('digest_schedules', {})
    summarizers: dict[str, Summarizer] = ctx.obj.get('summarizers', {})
    bozo_set = ctx.obj.get('bozofilter', set())
    cache = SummaryCache(ctx.obj['data_dir'] / SUMMARY_CACHE_DIR)
    for dname in delivery_names:
        feed = ctx.obj['deliveries'][dname][0]
        schedule = schedules[dname]
        summarizer = summarizers[schedule.summarizer] if schedule.summarizer else None
        try:
            lines = estimate_digest(dname, feed, schedule, summarizer, cache, bozo_set, now=now)
        except Exception as e:
            logger.error('Could not estimate digest %s: %s', dname, e)
            logger.debug('Traceback:', exc_info=True)
            continue
        click.echo('\n'.join(lines))


def normalize_feed_key(feed_url: str) -> str:
    """Normalize a feed URL into a consistent key for internal tracking.

    The returned key must be safe to use as a directory name. For lore URLs
    the list name is extracted directly; for other URLs the scheme is
    stripped and special characters are replaced with hyphens.
    """
    if feed_url.startswith('https://lore.kernel.org/'):
        # Extract list name from URL
        return feed_url.replace('https://lore.kernel.org/', '').strip('/')
    if feed_url.startswith('lei:'):
        # Keep full lei path as key
        return feed_url
    # Sanitize URL for use as a directory name
    url_without_scheme = feed_url.replace('https://', '').replace('http://', '')
    sanitized = re.sub(r'[^a-zA-Z0-9_.-]', '-', url_without_scheme)
    sanitized = sanitized.strip('-./')
    if len(sanitized) > 200:
        url_hash = hashlib.sha256(feed_url.encode()).hexdigest()[:16]
        sanitized = f'feed-{url_hash}'
    return sanitized


def generate_subscription_config(feed_key: str, url: str, target: str, labels: list[str]) -> str:
    """Generate TOML config content for a feed subscription.

    Args:
        feed_key: The normalized feed key.
        url: The feed URL (https:// for lore, or raw path for lei).
        target: Target name for deliveries.
        labels: List of labels to apply.

    Returns:
        Formatted TOML string for writing to conf.d/sub-{feed_key}.toml.
    """
    from datetime import datetime

    labels_str = ', '.join(f"'{label}'" for label in labels)
    timestamp = datetime.now().astimezone().isoformat(timespec='seconds')

    # Determine the URL value to write
    if url.startswith(('https:', 'http:')):
        url_value = url
    else:
        url_value = f'lei:{url}'

    lines = [
        f"# Auto-generated by: kgl subscribe add '{url}'",
        f'# Generated: {timestamp}',
        '',
        f'[feeds.{feed_key}]',
        f"url = '{url_value}'",
        '',
        f'[deliveries.{feed_key}]',
        f"feed = '{feed_key}'",
        f"target = '{target}'",
        f'labels = [{labels_str}]',
        '',
    ]

    return '\n'.join(lines)


def find_subscription_file(conf_d: Path, feed_key: str) -> Path | None:
    """Find an existing subscription config file for a feed key.

    Looks for sub-{feed_key}.toml (active) or sub-{feed_key}.toml.paused.

    Args:
        conf_d: The conf.d directory path.
        feed_key: The feed key to search for.

    Returns:
        Path to the subscription file if found, None otherwise.
    """
    active = conf_d / f'sub-{feed_key}.toml'
    if active.exists():
        return active
    paused = conf_d / f'sub-{feed_key}.toml.paused'
    if paused.exists():
        return paused
    return None


def get_lore_node(ctx: click.Context, url: str = 'https://lore.kernel.org/all') -> 'liblore.LoreNode':
    """Get or create a LoreNode for the given URL's origin.

    Nodes are cached in ctx.obj['lore_nodes'] keyed by canonical origin
    (scheme://host). All URLs on the same host share one node, since
    failover origins are per-host, not per-path.
    """
    parsed = urllib.parse.urlparse(url)
    origin = f'{parsed.scheme}://{parsed.netloc}'
    nodes: dict[str, liblore.LoreNode] = ctx.obj['lore_nodes']
    if origin not in nodes:
        node = make_lore_node(url=url)
        nodes[origin] = node
        ctx.call_on_close(node.close)
    return nodes[origin]


def get_feed_for_delivery(delivery_details: dict[str, Any], ctx: click.Context) -> LeiFeed | LoreFeed:
    """Get or create a feed instance for a delivery configuration."""
    config = ctx.obj.get('config', {})
    feed_value = delivery_details.get('feed', '')
    if not feed_value:
        raise ConfigurationError('No feed specified for delivery.')
    feed_url = resolve_feed_url(feed_value, config)
    feed_key = normalize_feed_key(feed_url)
    feeds: dict[str, LeiFeed | LoreFeed] = ctx.obj.get('feeds', {})
    if feed_key in feeds:
        return feeds[feed_key]

    if feed_url.startswith('https:'):
        # Lore feed
        data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())
        feed_dir = data_dir / feed_key
        lore_feed = LoreFeed(feed_key, feed_dir, feed_url, lore_node=get_lore_node(ctx, feed_url))
        feeds[feed_key] = lore_feed
        return lore_feed
    if feed_url.startswith('lei:'):
        # LEI feed
        lei_feed = LeiFeed(feed_key, feed_url)
        feeds[feed_key] = lei_feed
        return lei_feed
    logger.critical('Unknown feed type for delivery: %s', feed_url)
    raise ConfigurationError(f'Unknown feed type for delivery: {feed_url}')


def map_deliveries(ctx: click.Context, deliveries: dict[str, Any]) -> None:
    """Map delivery configurations to their feed and target instances."""
    from datetime import datetime

    # 'deliveries' is a mapping: delivery_name -> tuple[feed, target, labels, subfolder]
    dmap: dict[str, tuple[LeiFeed | LoreFeed, Any, list[str], str | None]] = {}
    # Store original strftime templates for refresh (used by GUI for long-running processes)
    templates: dict[str, str] = {}
    # Deliveries with mode = 'digest', and when they send
    schedules: dict[str, DigestSchedule] = {}
    # The [summarizers] entries that digests use
    summarizers: dict[str, Summarizer] = {}
    summarizer_cfg: dict[str, Any] = ctx.obj.get('config', {}).get('summarizers', {})
    logger.debug('Mapping deliveries to their feeds and targets')
    # Pre-map deliveries to their feeds and targets for later use.
    for delivery_name, details in deliveries.items():
        mode = details.get('mode', 'message')
        if mode == 'digest':
            schedules[delivery_name] = DigestSchedule.from_config(delivery_name, details)
        elif mode == 'message':
            digest_keys = [key for key in DIGEST_KEYS if key in details]
            if digest_keys:
                raise ConfigurationError(
                    f"Delivery '{delivery_name}' sets {', '.join(digest_keys)}, which only work with mode = 'digest'"
                )
        else:
            raise ConfigurationError(f"Delivery '{delivery_name}': mode must be 'message' or 'digest' (got {mode!r})")
        # Map feed
        feed = get_feed_for_delivery(details, ctx)
        sname = schedules[delivery_name].summarizer if delivery_name in schedules else None
        if sname is not None:
            if sname not in summarizers:
                if not isinstance(summarizer_cfg.get(sname), dict):
                    raise ConfigurationError(
                        f"Delivery '{delivery_name}': summarizer '{sname}' is not defined in [summarizers]"
                    )
                summarizers[sname] = make_summarizer(sname, summarizer_cfg[sname])
            summarizer = summarizers[sname]
            # lei searches can find private mail, which must not leave the machine
            if isinstance(feed, LeiFeed) and not summarizer.is_local and not summarizer.allow_private_feeds:
                raise ConfigurationError(
                    f"Delivery '{delivery_name}': summarizer '{sname}' may send mail off this machine, "
                    'and lei feeds can hold private mail. Set allow_private_feeds = true in '
                    f'[summarizers.{sname}] if that is fine.'
                )
        # Map target
        target_name = details.get('target', '')
        if not target_name:
            logger.critical('No target specified for delivery: %s', delivery_name)
            raise ConfigurationError(f'No target specified for delivery: {delivery_name}')
        target = get_target(ctx, target_name)
        # Extract and validate subfolder
        subfolder = details.get('subfolder')
        if subfolder is not None:
            if isinstance(subfolder, list):
                raise ConfigurationError(
                    f"subfolder for delivery '{delivery_name}' must be a string, not a list. "
                    'Use labels for multiple folders (JMAP only).'
                )
            if not isinstance(subfolder, str):
                raise ConfigurationError(f"subfolder for delivery '{delivery_name}' must be a string")
            # Treat empty string as None
            if not subfolder:
                subfolder = None
            elif '%' in subfolder:
                # Only Maildir targets support strftime templates in subfolder
                if isinstance(target, MaildirTarget):
                    try:
                        # Validate the strftime template and store original for refresh
                        templates[delivery_name] = subfolder
                        subfolder = datetime.now().astimezone().strftime(subfolder)
                        logger.debug('Expanded subfolder template to: %s', subfolder)
                    except ValueError as e:
                        raise ConfigurationError(
                            f"Invalid strftime format in subfolder for delivery '{delivery_name}': {e}"
                        ) from e
                else:
                    raise ConfigurationError(
                        f'strftime templates in subfolder are only supported for Maildir targets '
                        f"(delivery '{delivery_name}' uses {type(target).__name__})"
                    )
        # Validate labels don't contain strftime templates
        labels = details.get('labels', [])
        for label in labels:
            if '%' in label:
                raise ConfigurationError(
                    f"strftime templates in labels are not supported (delivery '{delivery_name}' has label '{label}')"
                )
        # Lock for the entire duration
        dmap[delivery_name] = (feed, target, labels, subfolder)
    ctx.obj['deliveries'] = dmap
    ctx.obj['subfolder_templates'] = templates
    ctx.obj['digest_schedules'] = schedules
    ctx.obj['summarizers'] = summarizers
    if schedules:
        ctx.obj['digest_worker'] = get_digest_worker_mode(ctx.obj.get('config', {}))


def refresh_subfolder_templates(ctx: click.Context) -> None:
    """Re-expand strftime templates in subfolder paths.

    For long-running processes (like the GUI), this should be called before
    each sync to ensure date-based subfolder paths are current.
    """
    from datetime import datetime

    templates = ctx.obj.get('subfolder_templates', {})
    if not templates:
        return

    deliveries = ctx.obj.get('deliveries', {})
    for delivery_name, template in templates.items():
        if delivery_name not in deliveries:
            continue
        feed, target, labels, _ = deliveries[delivery_name]
        new_subfolder = datetime.now().astimezone().strftime(template)
        deliveries[delivery_name] = (feed, target, labels, new_subfolder)
        logger.debug('Refreshed subfolder template for %s: %s', delivery_name, new_subfolder)


def lock_all_feeds(ctx: click.Context) -> None:
    """Acquire exclusive locks on all feeds in the context.

    Either all feeds are locked, or none: when one is busy, the feeds
    locked before it are released again.

    Raises:
        FeedLockedError: Another process is using one of the feeds.
    """
    feeds: dict[str, LeiFeed | LoreFeed] = ctx.obj.get('feeds', {})
    locked: list[LeiFeed | LoreFeed] = []
    try:
        for feed in feeds.values():
            feed.feed_lock()
            locked.append(feed)
    except FeedLockedError:
        for feed in locked:
            feed.feed_unlock()
        raise


@contextmanager
def abort_if_feed_locked() -> Generator[None, None, None]:
    """Turn a busy feed into a short error message instead of a traceback."""
    try:
        yield
    except FeedLockedError as fe:
        logger.critical('Error: %s', str(fe))
        raise click.Abort() from fe


def unlock_all_feeds(ctx: click.Context) -> None:
    """Release exclusive locks on all feeds in the context."""
    feeds: dict[str, LeiFeed | LoreFeed] = ctx.obj.get('feeds', {})
    for feed in feeds.values():
        feed.feed_unlock()


def digest_history_needs(ctx: click.Context) -> dict[str, datetime]:
    """How far back each lore feed must keep its history for its digests.

    Lore clones only keep one week of history, and a digest that was last
    sent longer ago than that would miss messages. Returns a mapping of
    feed key to the oldest commit date that any of its digests needs.
    """
    needs: dict[str, datetime] = {}
    for dname in ctx.obj.get('digest_schedules', {}):
        feed = ctx.obj['deliveries'][dname][0]
        if not isinstance(feed, LoreFeed):
            continue
        try:
            since = feed.get_digest_history_start(dname)
        except StateError as e:
            logger.warning('Digest %s: %s', dname, e)
            continue
        if since is not None and (feed.feed_key not in needs or since < needs[feed.feed_key]):
            needs[feed.feed_key] = since
    return needs


def update_all_feeds(
    ctx: click.Context,
    status_callback: Callable[[str], None] | None = None,
) -> tuple[list[str], list[str]]:
    """Update all feeds and return (updated_feeds, initialized_feeds).

    Feeds that failed to update are recorded in ctx.obj['failed_feeds'],
    which is replaced on every call.
    """
    updated_feeds: list[str] = []
    initialized_feeds: list[str] = []
    failed_feeds: list[str] = []
    ctx.obj['failed_feeds'] = failed_feeds
    feeds: dict[str, LeiFeed | LoreFeed] = ctx.obj.get('feeds', {})
    history_needs = digest_history_needs(ctx)

    if status_callback:
        status_callback('Querying feeds...')

    with click.progressbar(
        feeds.keys(),
        label='Updating feeds',
        show_pos=True,
        item_show_func=lambda x: format_key_for_display((x in feeds and str(feeds[x].feed_url)) or x),
        file=progress_file(ctx.obj['hide_bar']),
    ) as bar:
        for feed_key in bar:
            if status_callback:
                status_callback(f'Querying {format_key_for_display(feed_key)}...')
            feed = feeds[feed_key]
            try:
                if feed_key in history_needs and isinstance(feed, LoreFeed):
                    status = feed.update_feed(keep_history_since=history_needs[feed_key])
                else:
                    status = feed.update_feed()
            except (RemoteError, PublicInboxError, GitError) as e:
                logger.warning('Failed to update %s: %s', feed_key, e)
                failed_feeds.append(feed_key)
                continue
            if status & feed.STATUS_UPDATED:
                updated_feeds.append(feed_key)
            if status & feed.STATUS_INITIALIZED:
                initialized_feeds.append(feed_key)

    # Log initialization messages after progressbar completes
    for feed_key in initialized_feeds:
        logger.info('Initialized new feed: %s', feed_key)

    return updated_feeds, initialized_feeds


def retry_all_failed_deliveries(ctx: click.Context) -> None:
    """Retry all previously failed deliveries across all feeds."""
    bozo_set = ctx.obj.get('bozofilter', set())

    # 'deliveries' is a mapping: delivery_name -> tuple[feed, target, labels, subfolder]
    deliveries = ctx.obj['deliveries']
    digest_names = ctx.obj.get('digest_schedules', {})
    retry_list: list[tuple[str, Any, LeiFeed | LoreFeed, int, str, list[str], str | None]] = []
    for delivery_name, (feed, target, labels, subfolder) in deliveries.items():
        if delivery_name in digest_names:
            # Digests never deliver single messages, not even ones left
            # over from before the delivery was switched to a digest
            continue
        to_retry = feed.get_failed_commits_for_delivery(delivery_name)
        if not to_retry:
            logger.debug('No failed commits to retry for delivery: %s', delivery_name)
            continue
        for epoch, commit in to_retry:
            retry_list.append((delivery_name, target, feed, epoch, commit, labels, subfolder))
    if not retry_list:
        logger.debug('No failed commits to retry for any delivery.')
        return

    with click.progressbar(
        retry_list, label='Reattempting delivery', show_pos=True, file=progress_file(ctx.obj['hide_bar'])
    ) as bar:
        for delivery_name, target, feed, epoch, commit, labels, subfolder in bar:
            deliver_commit(
                delivery_name,
                target,
                feed,
                epoch,
                commit,
                labels,
                was_failing=True,
                bozofilter=bozo_set,
                subfolder=subfolder,
            )


@click.group()
@click.version_option(version=__version__)
# click_log ships no type information, so mypy treats the decorated
# function as untyped. The import ignore above does not cover this.
@click_log.simple_verbosity_option(logger)  # type: ignore[untyped-decorator]
@click.option('--cfgfile', '-c', help='Path to configuration file.')
@click.option('-l', '--logfile', default=None, type=click.Path(), help='Path to log file.')
@click.pass_context
def main(ctx: click.Context, cfgfile: str, logfile: click.Path | None) -> None:
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
        ctx.obj['cfgpath'] = cfgpath

        if not config.get('targets'):
            logger.critical('No targets defined in configuration.')
            logger.critical('Please edit %s and define at least one target.', cfgpath)
            raise click.Abort()

    # LoreNode cache keyed by canonical origin (scheme://host).
    # Nodes are created on demand by get_lore_node() and closed on exit.
    ctx.obj['lore_nodes'] = {}

    # Seed with default lore.kernel.org node and read lore.useragentplus.
    import korgalore

    korgalore._user_agent_plus = get_lore_node(ctx).user_agent_plus

    # Check git is available and set GIT_HTTP_USER_AGENT
    try:
        _init_git_user_agent()
    except GitError as e:
        raise click.ClickException(str(e)) from e

    # Ensure XDG data directory exists
    data_dir = get_xdg_data_dir()
    ctx.obj['data_dir'] = data_dir

    logger.debug('Data directory: %s', data_dir)

    # We lazy-load these
    # 'targets' is a mapping: target identifier -> target instance
    ctx.obj['targets'] = {}
    # 'feeds' is a mapping: feed_key -> feed instance
    ctx.obj['feeds'] = {}
    # 'deliveries' is a mapping: delivery_name -> tuple[feed_instance, target_instance, labels, subfolder]
    ctx.obj['deliveries'] = {}

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
def auth(ctx: click.Context, target: str | None) -> None:
    """Authenticate with configured targets.

    If TARGET is specified, authenticate only that target.
    If TARGET is omitted, authenticate all targets that require authentication.
    """
    # Target types that don't require authentication
    no_auth_targets = {'maildir', 'pipe', 'dummy'}

    config = ctx.obj.get('config', {})
    targets = config.get('targets', {})

    # If specific target requested, validate it exists
    if target:
        if target not in targets:
            logger.critical('Target "%s" not found in configuration.', target)
            logger.critical('Known targets: %s', ', '.join(targets.keys()))
            raise click.Abort()

        # Check if target requires authentication
        target_type = targets[target].get('type', '')
        if target_type in no_auth_targets:
            logger.warning('Target "%s" (type: %s) does not require authentication.', target, target_type)
            return

        # Authenticate only the specified target
        auth_targets = [(target, targets[target])]
    else:
        # Authenticate all targets that require authentication
        auth_targets = []
        for identifier, details in targets.items():
            target_type = details.get('type', '')
            if target_type in no_auth_targets:
                logger.debug(
                    'Skipping target that does not require authentication: %s (type: %s)', identifier, target_type
                )
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
        except (ConfigurationError, AuthenticationError, RemoteError) as e:
            logger.error('Failed to authenticate target %s: %s', identifier, e)
            raise click.Abort() from e
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
        logger.warning('Target "%s" does not support labels (maildir targets ignore labels).', target)
        return

    try:
        gs.connect()
        logger.debug('Fetching labels from target')
        labels_list = gs.list_labels()

        if not labels_list:
            logger.info('No labels found.')
            return

        logger.debug('Found %d labels', len(labels_list))
        logger.info('Available labels:')
        for label in labels_list:
            if ids:
                logger.info('  - %s (ID: %s)', label['name'], label['id'])
            else:
                logger.info('  - %s', label['name'])

    except Exception as e:
        logger.critical('Failed to fetch labels: %s', str(e))
        raise click.Abort() from e


def perform_pull(
    ctx: click.Context,
    no_update: bool,
    force: bool,
    delivery_name: str | None,
    status_callback: Callable[[str], None] | None = None,
) -> tuple[dict[str, int], set[str]]:
    """Execute the pull logic and return changes.

    Returns:
        A tuple of (per-delivery counts dict, set of unique message-ids delivered).
        Feeds that failed to update are left in ctx.obj['failed_feeds'].
    """
    # Reset on every pull: the GUI calls us repeatedly with the same ctx
    ctx.obj['failed_feeds'] = []
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
        updated_feeds: list[str] = []
        initialized_feeds: list[str] = []
    else:
        updated_feeds, initialized_feeds = update_all_feeds(ctx, status_callback=status_callback)

    # Build reverse index once: feed_key -> delivery names
    feed_to_deliveries: dict[str, list[str]] = {}
    for dname, (feed, _, _, _) in ctx.obj['deliveries'].items():
        feed_to_deliveries.setdefault(feed.feed_key, []).append(dname)

    # Initialise delivery state for newly cloned feeds so the next update
    # delivers new commits without wasting an extra pull cycle.
    if not no_update:
        for feed_key in initialized_feeds:
            feed = ctx.obj['feeds'][feed_key]
            for dname in feed_to_deliveries.get(feed_key, []):
                try:
                    feed.load_delivery_info(dname)
                except StateError:
                    logger.info('Initializing delivery state: %s', dname)
                    feed.save_delivery_info(dname)

    run_deliveries: list[str] = []
    if not force:
        logger.debug('Updated feeds: %s', ', '.join(updated_feeds))
        for feed_key in updated_feeds:
            run_deliveries.extend(feed_to_deliveries.get(feed_key, []))
    else:
        # If force is specified, treat all feeds as updated
        logger.debug('Force flag set, treating all feeds as updated')
        run_deliveries = list(ctx.obj['deliveries'].keys())

    # Digests are sent on their own schedule, not message by message. They
    # are checked on every pull, because a digest can be due even when its
    # feed has nothing new.
    digest_names = list(ctx.obj.get('digest_schedules', {}))
    run_deliveries = [dname for dname in run_deliveries if dname not in digest_names]

    logger.debug('Deliveries to run: %s', ', '.join(run_deliveries))

    if not run_deliveries and not digest_names:
        unlock_all_feeds(ctx)
        return {}, set()

    # Build a worklist of updates per target
    by_target: dict[str, list[str]] = {}
    for dname in run_deliveries:
        target_name = ctx.obj['deliveries'][dname][1].identifier
        if target_name not in by_target:
            by_target[target_name] = []
        by_target[target_name].append(dname)

    changes: dict[str, int] = {}
    unique_msgids: set[str] = set()

    # Process deliveries now
    for target_name, delivery_names in by_target.items():
        logger.debug('Processing deliveries for target: %s', target_name)
        run_list: list[tuple[str, Any, LeiFeed | LoreFeed, int, str, list[str], str | None]] = []
        for dname in delivery_names:
            feed, target, labels, subfolder = ctx.obj['deliveries'][dname]
            commits = feed.get_latest_commits_for_delivery(dname)
            if not commits:
                logger.debug('No new commits for delivery: %s', dname)
                continue
            for epoch, commit in commits:
                run_list.append((dname, target, feed, epoch, commit, labels, subfolder))
        if not run_list:
            logger.debug('No deliveries with new commits for target: %s', target_name)
            continue
        logger.debug('Delivering %d messages to target: %s', len(run_list), target_name)

        with click.progressbar(
            run_list,
            label='Delivering to ' + target_name,
            show_pos=True,
            item_show_func=lambda x: (x is not None and format_key_for_display(x[0])) or None,
            file=progress_file(ctx.obj['hide_bar']),
        ) as bar:
            # We bail on a target if we have more than 5 consecutive failures
            consecutive_failures = 0
            prev_dname: str | None = None
            for dname, target, feed, epoch, commit, labels, subfolder in bar:
                if status_callback and dname != prev_dname:
                    status_callback(f'Delivering {format_key_for_display(dname)}...')
                    prev_dname = dname
                if consecutive_failures >= 5:
                    logger.error('Aborting deliveries to target "%s" due to repeated failures.', target_name)
                    break
                msgid = deliver_commit(
                    dname,
                    target,
                    feed,
                    epoch,
                    commit,
                    labels,
                    was_failing=False,
                    bozofilter=bozo_set,
                    subfolder=subfolder,
                )
                if msgid is None:
                    consecutive_failures += 1
                    continue
                if msgid in (SKIPPED_BOZOFILTER, SKIPPED_NOOP_COMMIT):
                    # Filtered or no-op commit - not a failure, just skip
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

    if digest_names:
        try:
            sent = run_due_digests(ctx, digest_names, status_callback=status_callback)
        except AuthenticationError:
            unlock_all_feeds(ctx)
            raise
        for dname, digest_msgids in sent.items():
            changes[dname] = len(digest_msgids)
            unique_msgids.update(digest_msgids)

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
@click.option(
    '--fail-on-feed-error',
    is_flag=True,
    help='exit with status 3 if any feed failed to update (all feeds and deliveries still run)',
)
@click.argument('delivery_name', type=str, required=False)
def pull(
    ctx: click.Context,
    max_mail: int,
    no_update: bool,
    force: bool,
    fail_on_feed_error: bool,
    delivery_name: str | None,
) -> None:
    """Pull messages from configured lore and LEI deliveries.

    With DELIVERY_NAME, only the feeds of that delivery are updated, so
    --fail-on-feed-error only covers those feeds.
    """
    with abort_if_feed_locked():
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

    failed_feeds: list[str] = ctx.obj.get('failed_feeds', [])
    if fail_on_feed_error and failed_feeds:
        logger.error('Feeds that failed to update:')
        for feed_key in failed_feeds:
            logger.error('  %s', feed_key)
        ctx.exit(3)


@main.command('digest')
@click.pass_context
@click.option('--force', '-f', is_flag=True, help='send now, even if not due and even if there is no activity')
@click.option('--no-update', '-n', is_flag=True, help='skip feed updates')
@click.option(
    '--fail-on-feed-error',
    is_flag=True,
    help='exit with status 3 if any feed failed to update (digests are still sent)',
)
@click.option('--work', is_flag=True, help='run the digest worker: finish the waiting digests and exit')
@click.option(
    '--estimate',
    is_flag=True,
    help='show what the next digests would send to their summarizer, without calling it or sending anything',
)
@click.argument('delivery_names', type=str, nargs=-1)
def digest_cmd(
    ctx: click.Context,
    force: bool,
    no_update: bool,
    fail_on_feed_error: bool,
    work: bool,
    estimate: bool,
    delivery_names: tuple[str, ...],
) -> None:
    """Send the digests that are due.

    Digests are also sent by "kgl pull", so you only need this command to
    send them without delivering anything else, or to send one right away
    with --force. With DELIVERY_NAMES, only those digests are checked.
    """
    if work and force:
        raise click.UsageError('--work finishes waiting digests, it cannot be used with --force')
    if estimate and (work or force):
        raise click.UsageError('--estimate only reports, it cannot be used with --work or --force')
    ctx.obj['failed_feeds'] = []
    cfg = ctx.obj.get('config', {})
    all_deliveries: dict[str, Any] = cfg.get('deliveries', {})
    digests = {name: details for name, details in all_deliveries.items() if details.get('mode') == 'digest'}
    if delivery_names:
        for name in delivery_names:
            if name not in all_deliveries:
                logger.critical('Delivery "%s" not found in configuration.', name)
                raise click.Abort()
            if name not in digests:
                logger.critical('Delivery "%s" is not a digest (set mode = "digest").', name)
                raise click.Abort()
        digests = {name: digests[name] for name in delivery_names}
    if not digests:
        logger.info('No digest deliveries configured.')
        return

    map_deliveries(ctx, digests)
    if work:
        # Jobs hold everything they need, so feeds are not locked or updated
        try:
            run_digest_worker(ctx, list(digests))
        finally:
            ctx.obj['targets'] = {}
        return

    with abort_if_feed_locked():
        lock_all_feeds(ctx)
    try:
        if no_update:
            logger.debug('No-update flag set, skipping feed updates')
        else:
            update_all_feeds(ctx)
        if estimate:
            run_digest_estimates(ctx, list(digests))
        else:
            sent = run_due_digests(ctx, list(digests), force=force)
            if not sent:
                logger.info('No digests were due.')
    finally:
        unlock_all_feeds(ctx)
        close_requests_session()
        ctx.obj['targets'] = {}

    failed_feeds: list[str] = ctx.obj.get('failed_feeds', [])
    if fail_on_feed_error and failed_feeds:
        logger.error('Feeds that failed to update:')
        for feed_key in failed_feeds:
            logger.error('  %s', feed_key)
        ctx.exit(3)


def perform_yank(
    ctx: click.Context,
    target_name: str,
    msgid_or_url: str,
    thread: bool = False,
    labels_list: list[str] | None = None,
) -> tuple[int, int]:
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

    node = get_lore_node(ctx)
    try:
        if thread:
            msgid = get_msgid_from_url(msgid_or_url)
            mbox = node.get_mbox_by_msgid(msgid)
            messages = split_and_dedupe_as_bytes(mbox)
            logger.info('Found %d unique messages in thread', len(messages))

            uploaded = 0
            failed = 0

            for raw_message in messages:
                try:
                    msg = parse_message(raw_message)
                    subject = msg_get_subject(msg) or '(no subject)'
                    logger.debug('Uploading: %s', subject)
                    ts.import_message(raw_message, labels=labels_list)
                    uploaded += 1
                except liblore.RemoteError as e:
                    logger.error('Failed to upload message: %s', str(e))
                    failed += 1

            return uploaded, failed
        msgid = get_msgid_from_url(msgid_or_url)
        raw_message = node.get_message_by_msgid(msgid)
        msg = parse_message(raw_message)
        subject = msg_get_subject(msg) or '(no subject)'
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
@click.option('--target', '-t', default=None, help='Target to upload the message to (default: first configured)')
@click.option('--labels', '-l', multiple=True, help='Labels to apply (repeatable or comma-separated)')
@click.option('--thread', '-T', is_flag=True, help='Fetch and upload the entire thread')
@click.argument('msgid_or_url', type=str, nargs=1)
def yank(ctx: click.Context, target: str | None, labels: tuple[str, ...], thread: bool, msgid_or_url: str) -> None:
    """Yank a single message or entire thread to a target."""
    # Get the target service
    config = ctx.obj.get('config', {})
    targets = config.get('targets', {})

    target = resolve_target_name(target, targets)

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

    node = get_lore_node(ctx)
    msgid = get_msgid_from_url(msgid_or_url)

    if thread:
        # Fetch the entire thread
        logger.debug('Fetching thread: %s', msgid)
        try:
            mbox = node.get_mbox_by_msgid(msgid)
            messages = split_and_dedupe_as_bytes(mbox)
        except liblore.RemoteError as e:
            logger.critical('Failed to fetch thread: %s', str(e))
            raise click.Abort() from e

        logger.info('Found %d unique messages in thread', len(messages))

        # Upload each message in the thread
        uploaded = 0
        failed = 0

        try:
            ts.connect()
        except (ConfigurationError, AuthenticationError, liblore.RemoteError) as e:
            logger.critical('Failed to connect to target "%s": %s', target, e)
            raise click.Abort() from e

        with click.progressbar(
            messages, label='Uploading thread', show_pos=True, file=progress_file(ctx.obj['hide_bar'])
        ) as bar:
            for raw_message in bar:
                try:
                    msg = parse_message(raw_message)
                    subject = msg_get_subject(msg) or '(no subject)'
                    logger.debug('Uploading: %s', subject)
                    ts.import_message(raw_message, labels=labels_list)
                    uploaded += 1
                except liblore.RemoteError as e:
                    logger.error('Failed to upload message: %s', str(e))
                    failed += 1
                    continue

        if failed > 0:
            logger.warning('Uploaded %d messages, %d failed', uploaded, failed)
        else:
            logger.info('Successfully uploaded %d messages from thread', uploaded)
    else:
        # Fetch a single message
        logger.debug('Fetching message: %s', msgid)
        try:
            raw_message = node.get_message_by_msgid(msgid)
        except liblore.RemoteError as e:
            logger.critical('Failed to fetch message: %s', str(e))
            raise click.Abort() from e

        # Parse to get the subject for logging
        msg = parse_message(raw_message)
        subject = msg_get_subject(msg) or '(no subject)'
        logger.debug('Message subject: %s', subject)

        # Upload the message
        logger.info('Uploading to target "%s"', target)
        logger.debug('Uploading: %s', subject)
        try:
            ts.connect()
            ts.import_message(raw_message, labels=labels_list)
            logger.info('Successfully uploaded message.')
        except (ConfigurationError, AuthenticationError, liblore.RemoteError) as e:
            logger.critical('Failed to upload message: %s', str(e))
            raise click.Abort() from e


def get_tracking_manifest(ctx: click.Context) -> TrackingManifest:
    """Get or create the tracking manifest."""
    if 'tracking_manifest' not in ctx.obj:
        data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())
        ctx.obj['tracking_manifest'] = TrackingManifest(data_dir)
    manifest: TrackingManifest = ctx.obj['tracking_manifest']
    return manifest


def map_tracked_threads(ctx: click.Context) -> list[str]:
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
    mapped: list[str] = []

    for tracked in active:
        lei_url = f'lei:{tracked.lei_path}'
        try:
            lei_feed = LeiFeed(tracked.track_id, lei_url)
        except ConfigurationError as e:
            logger.warning('Tracked thread %s not recognized by lei: %s', tracked.track_id, str(e))
            continue

        try:
            target = get_target(ctx, tracked.target)
        except click.Abort:
            logger.warning('Target "%s" not available for tracked thread %s', tracked.target, tracked.track_id)
            continue

        # Add to feeds and deliveries
        feeds[tracked.track_id] = lei_feed
        deliveries[tracked.track_id] = (lei_feed, target, tracked.labels, None)
        mapped.append(tracked.track_id)

    return mapped


def update_tracked_thread_activity(ctx: click.Context, changes: dict[str, int]) -> None:
    """Update tracking manifest activity for tracked threads that had deliveries."""
    manifest = get_tracking_manifest(ctx)

    for delivery_name, count in changes.items():
        if delivery_name.startswith('track-'):
            # Not a tracked thread, or already removed
            with suppress(KeyError):
                manifest.update_activity(delivery_name, count)


@main.group()
@click.pass_context
def track(ctx: click.Context) -> None:
    """Track email threads for updates via lei queries."""


@track.command('add')
@click.argument('msgid_or_url', type=str)
@click.option('--target', '-t', default=None, help='Target for deliveries (default: first configured)')
@click.option('--labels', '-l', multiple=True, help='Labels to apply (repeatable or comma-separated)')
@click.pass_context
def track_add(ctx: click.Context, msgid_or_url: str, target: str | None, labels: tuple[str, ...]) -> None:
    """Start tracking a thread by message ID or lore URL."""
    config = ctx.obj.get('config', {})
    targets = config.get('targets', {})

    target = resolve_target_name(target, targets)

    # Extract message ID from URL if needed
    msgid = get_msgid_from_url(msgid_or_url)
    logger.debug('Extracted message ID: %s', msgid)

    # Check if already tracking this message
    manifest = get_tracking_manifest(ctx)
    existing = manifest.get_thread_by_msgid(msgid)
    if existing:
        if existing.status == TrackStatus.ACTIVE:
            logger.warning('Already tracking this thread as %s', existing.track_id)
            return
        # Offer to resume
        logger.info('Thread previously tracked as %s (status: %s)', existing.track_id, existing.status.value)
        if click.confirm('Resume tracking?'):
            manifest.resume_thread(existing.track_id)
            logger.info('Resumed tracking thread %s', existing.track_id)
            return
        logger.info('Aborted.')
        return

    # Create lei search directory
    data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())
    import secrets

    track_id = f'track-{secrets.token_hex(6)}'
    lei_path = data_dir / 'lei' / track_id

    logger.info('Creating lei search for thread: %s', msgid)

    # Create the lei search
    try:
        retcode, output = create_lei_thread_search(msgid, lei_path)
    except PublicInboxError as e:
        logger.critical('Failed to create lei search: %s', str(e))
        raise click.Abort() from e

    if retcode != 0:
        logger.critical('Lei query failed: %s', output.decode())
        raise click.Abort()

    # Populate the git repository with lei up
    logger.info('Populating lei search repository...')
    try:
        retcode, output = update_lei_search(lei_path)
    except PublicInboxError as e:
        logger.critical('Failed to update lei search: %s', str(e))
        raise click.Abort() from e

    if retcode != 0:
        logger.critical('Lei update failed: %s', output.decode())
        raise click.Abort()

    # Get the subject from the first message
    subject = '(unknown subject)'
    try:
        node = get_lore_node(ctx)
        raw_message = node.get_message_by_msgid(msgid)
        msg = parse_message(raw_message)
        subject = msg_get_subject(msg) or '(no subject)'
    except liblore.RemoteError:
        logger.warning('Could not fetch message to get subject')

    # Thread archives are named after a random track id, so the subject is
    # the only thing that identifies them in a public-inbox listing.
    write_archive_description(lei_path, subject)

    # Get target instance for delivery and default labels
    target_service = get_target(ctx, target)

    # Add to manifest with target-specific default labels if none specified
    if labels:
        labels_list = parse_labels(labels)
    else:
        labels_list = target_service.DEFAULT_LABELS

    thread = manifest.add_thread(
        track_id=track_id, msgid=msgid, subject=subject, target=target, labels=labels_list, lei_path=lei_path
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
        result = deliver_commit(
            thread.track_id, target_service, lei_feed, 0, commit, labels_list, was_failing=False, bozofilter=bozo_set
        )
        if result and result not in (SKIPPED_BOZOFILTER, SKIPPED_NOOP_COMMIT):
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
        logger.info(
            '    Messages: %d, Last activity: %s', thread.message_count, thread.last_new_message.strftime('%Y-%m-%d')
        )


@track.command('stop')
@click.argument('track_id', type=str)
@click.option('--delete', is_flag=True, help='Also delete lei search data')
@click.pass_context
def track_stop(ctx: click.Context, track_id: str, delete: bool) -> None:
    """Stop tracking a thread."""
    manifest = get_tracking_manifest(ctx)

    try:
        thread = manifest.get_thread(track_id)
    except KeyError as e:
        logger.critical('Tracked thread "%s" not found.', track_id)
        raise click.Abort() from e

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
    except KeyError as e:
        logger.critical('Tracked thread "%s" not found.', track_id)
        raise click.Abort() from e

    logger.info('Paused tracking for %s', track_id)


@track.command('resume')
@click.argument('track_id', type=str)
@click.pass_context
def track_resume(ctx: click.Context, track_id: str) -> None:
    """Resume tracking for a paused or expired thread."""
    manifest = get_tracking_manifest(ctx)

    try:
        thread = manifest.get_thread(track_id)
    except KeyError as e:
        logger.critical('Tracked thread "%s" not found.', track_id)
        raise click.Abort() from e

    if thread.status == TrackStatus.ACTIVE:
        logger.warning('Thread %s is already active.', track_id)
        return

    manifest.resume_thread(track_id)
    logger.info('Resumed tracking for %s', track_id)


class DefaultCommandGroup(click.Group):
    """A click Group that falls back to a default subcommand."""

    def __init__(self, *args: Any, default_cmd_name: str = 'add', **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.default_cmd_name = default_cmd_name

    def resolve_command(self, ctx: click.Context, args: list[str]) -> Any:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            args = [self.default_cmd_name] + args
            return super().resolve_command(ctx, args)


@main.group(cls=DefaultCommandGroup)
@click.pass_context
def subscribe(ctx: click.Context) -> None:
    """Manage mailing list subscriptions."""


@subscribe.command('add')
@click.argument('url', type=str)
@click.option('--target', '-t', default=None, help='Target for deliveries (default: first configured)')
@click.option('--labels', '-l', multiple=True, help='Labels to apply (repeatable or comma-separated)')
@click.pass_context
def subscribe_add(ctx: click.Context, url: str, target: str | None, labels: tuple[str, ...]) -> None:
    """Add a new mailing list subscription.

    URL can be a lore.kernel.org URL (e.g. https://lore.kernel.org/lkml/)
    or a local lei search path.
    """
    config = ctx.obj.get('config', {})
    targets = config.get('targets', {})

    target = resolve_target_name(target, targets)

    # Determine feed type and validate
    try:
        if url.startswith(('https://', 'http://')):
            LoreFeed.validate_public_inbox_url(url)
            feed_key = normalize_feed_key(url)
        else:
            LeiFeed.validate_lei_path(url)
            # Use the directory basename as feed key for lei paths
            feed_key = Path(url).name
    except (RemoteError, PublicInboxError) as e:
        logger.critical('%s', str(e))
        raise click.Abort() from e

    # Check for duplicate feed or delivery in the merged config
    feeds = config.get('feeds', {})
    deliveries = config.get('deliveries', {})
    if feed_key in feeds:
        logger.critical('Feed "%s" already exists in configuration.', feed_key)
        raise click.Abort()
    if feed_key in deliveries:
        logger.critical('Delivery "%s" already exists in configuration.', feed_key)
        raise click.Abort()

    # Check for duplicate subscription file in conf.d
    config_dir = ctx.obj['cfgpath'].parent
    conf_d = config_dir / 'conf.d'
    existing = find_subscription_file(conf_d, feed_key)
    if existing:
        logger.critical('Subscription already exists: %s', existing)
        raise click.Abort()

    # Resolve labels
    target_service = get_target(ctx, target)
    if labels:
        labels_list = parse_labels(labels)
    else:
        labels_list = target_service.DEFAULT_LABELS

    # Generate and write config
    conf_d.mkdir(parents=True, exist_ok=True)
    config_content = generate_subscription_config(feed_key, url, target, labels_list)
    config_file = conf_d / f'sub-{feed_key}.toml'
    config_file.write_text(config_content)

    logger.info('Subscribed to %s', url)
    logger.info('Configuration written to: %s', config_file)
    logger.info('Target: %s, Labels: %s', target, ', '.join(labels_list))


@subscribe.command('list')
@click.option('--paused', '-p', is_flag=True, help='Show only paused subscriptions')
@click.pass_context
def subscribe_list(ctx: click.Context, paused: bool) -> None:
    """List current subscriptions."""
    config_dir = ctx.obj['cfgpath'].parent
    conf_d = config_dir / 'conf.d'

    if not conf_d.is_dir():
        logger.info('No subscriptions found.')
        return

    active_files = sorted(conf_d.glob('sub-*.toml'))
    paused_files = sorted(conf_d.glob('sub-*.toml.paused'))

    if paused:
        files = [(f, 'paused') for f in paused_files]
    else:
        files = [(f, 'active') for f in active_files] + [(f, 'paused') for f in paused_files]

    if not files:
        if paused:
            logger.info('No paused subscriptions.')
        else:
            logger.info('No subscriptions found.')
        return

    logger.info('Subscriptions:')
    for filepath, status in files:
        # Extract feed key from filename
        name = filepath.name
        if name.endswith('.toml.paused'):
            feed_key = name[len('sub-') : -len('.toml.paused')]
        else:
            feed_key = name[len('sub-') : -len('.toml')]

        # Parse the TOML to get details
        try:
            with filepath.open('rb') as f:
                sub_config = tomllib.load(f)
            feeds = sub_config.get('feeds', {})
            deliveries = sub_config.get('deliveries', {})
            feed_url = ''
            sub_target = ''
            sub_labels: list[str] = []
            for fval in feeds.values():
                feed_url = fval.get('url', '')
            for dval in deliveries.values():
                sub_target = dval.get('target', '')
                sub_labels = dval.get('labels', [])
        except Exception:
            feed_url = '(error reading config)'
            sub_target = ''
            sub_labels = []

        status_str = f' [{status}]' if status == 'paused' else ''
        logger.info('')
        logger.info('  %s%s', feed_key, status_str)
        logger.info('    URL: %s', feed_url)
        logger.info('    Target: %s, Labels: %s', sub_target, ', '.join(sub_labels))


@subscribe.command('stop')
@click.argument('feed_key', type=str)
@click.option('--delete', is_flag=True, help='Also delete feed data')
@click.pass_context
def subscribe_stop(ctx: click.Context, feed_key: str, delete: bool) -> None:
    """Stop a subscription and remove its configuration."""
    config_dir = ctx.obj['cfgpath'].parent
    conf_d = config_dir / 'conf.d'

    sub_file = find_subscription_file(conf_d, feed_key)
    if not sub_file:
        logger.critical('Subscription "%s" not found.', feed_key)
        raise click.Abort()

    sub_file.unlink()
    logger.info('Removed subscription: %s', feed_key)

    if delete:
        data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())
        feed_dir = data_dir / feed_key
        if feed_dir.is_dir():
            import shutil

            shutil.rmtree(feed_dir)
            logger.info('Deleted feed data: %s', feed_dir)
        else:
            logger.info('No feed data found at %s', feed_dir)


@subscribe.command('pause')
@click.argument('feed_key', type=str)
@click.pass_context
def subscribe_pause(ctx: click.Context, feed_key: str) -> None:
    """Pause a subscription (skip updates but keep data)."""
    config_dir = ctx.obj['cfgpath'].parent
    conf_d = config_dir / 'conf.d'

    active_file = conf_d / f'sub-{feed_key}.toml'
    if not active_file.exists():
        paused_file = conf_d / f'sub-{feed_key}.toml.paused'
        if paused_file.exists():
            logger.warning('Subscription "%s" is already paused.', feed_key)
        else:
            logger.critical('Subscription "%s" not found.', feed_key)
            raise click.Abort()
        return

    paused_file = conf_d / f'sub-{feed_key}.toml.paused'
    active_file.rename(paused_file)
    logger.info('Paused subscription: %s', feed_key)


@subscribe.command('resume')
@click.argument('feed_key', type=str)
@click.option('--skip', is_flag=True, help='Skip messages received while paused')
@click.pass_context
def subscribe_resume(ctx: click.Context, feed_key: str, skip: bool) -> None:
    """Resume a paused subscription.

    Use --skip to discard messages that arrived while paused.
    On the next pull, delivery state will be re-created from the
    current feed tip.
    """
    config_dir = ctx.obj['cfgpath'].parent
    conf_d = config_dir / 'conf.d'

    paused_file = conf_d / f'sub-{feed_key}.toml.paused'
    if not paused_file.exists():
        active_file = conf_d / f'sub-{feed_key}.toml'
        if active_file.exists():
            logger.warning('Subscription "%s" is already active.', feed_key)
        else:
            logger.critical('Subscription "%s" not found.', feed_key)
            raise click.Abort()
        return

    active_file = conf_d / f'sub-{feed_key}.toml'
    paused_file.rename(active_file)
    logger.info('Resumed subscription: %s', feed_key)

    if skip:
        data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())
        feed_dir = data_dir / feed_key
        if feed_dir.is_dir():
            removed = 0
            for info_file in feed_dir.glob('korgalore.*.info'):
                info_file.unlink()
                removed += 1
            if removed:
                logger.info('Deleted %d delivery info file(s) — will skip to current tip on next pull', removed)
            else:
                logger.debug('No delivery info files found in %s', feed_dir)
        else:
            logger.debug('No feed data directory found at %s', feed_dir)


@main.command()
@click.pass_context
def gui(ctx: click.Context) -> None:
    """Launch the GNOME taskbar application."""
    try:
        from korgalore.gui import start_gui
    except ImportError as e:
        logger.critical('GUI dependencies not found: %s', str(e))
        raise click.Abort() from e

    # Set GUI mode to disable interactive OAuth flows
    ctx.obj['gui_mode'] = True
    try:
        start_gui(ctx)
    except RuntimeError as e:
        logger.critical('%s', str(e))
        raise click.Abort() from e


def find_tracked_subsystem_config(conf_d: Path, subsystem_name: str) -> tuple[str, Path]:
    """Find the conf.d file that tracks a subsystem.

    The user may supply a substring (e.g., "REGISTER MAP") that was
    resolved to a longer canonical name during creation (e.g.,
    "register_map_abstraction_layer.toml"). Match by checking if the
    normalised key appears as a word-boundary-aligned substring of the
    config filename, without needing to re-parse MAINTAINERS.

    Returns the key and the path. The path may not exist when nothing
    matched. Raises ConfigurationError when more than one file matches.
    """
    key = normalize_subsystem_name(subsystem_name)
    config_file = conf_d / f'{key}.toml'
    if config_file.exists() or not conf_d.is_dir():
        return key, config_file
    candidates = sorted(p for p in conf_d.glob('*.toml') if f'_{key}_' in f'_{p.stem}_')
    if len(candidates) == 1:
        return candidates[0].stem, candidates[0]
    if len(candidates) > 1:
        names = '\n'.join(f'  {c.name}' for c in candidates)
        raise ConfigurationError(f'Ambiguous match for "{subsystem_name}". Matching config files:\n{names}')
    return key, config_file


@main.command('track-subsystem')
@click.argument('subsystem_name', type=str, required=False, default=None)
@click.option(
    '--maintainers', '-m', default=None, type=click.Path(), help='Path to MAINTAINERS file (default: ./MAINTAINERS)'
)
@click.option('--target', '-t', default=None, help='Target for deliveries (default: first configured)')
@click.option(
    '--labels',
    '-l',
    multiple=True,
    help='Labels to apply (repeatable or comma-separated; default: target DEFAULT_LABELS)',
)
@click.option('--since', default='7.days.ago', help='Start date for query (default: 7.days.ago)')
@click.option(
    '--threads/--no-threads',
    default=False,
    help='Include entire threads when any message matches (can produce many results)',
)
@click.option(
    '--forget', is_flag=True, default=False, help='Remove tracking for the subsystem (deletes config and lei queries)'
)
@click.option('--list', '-L', 'do_list', is_flag=True, default=False, help='List tracked subsystems')
@click.pass_context
def track_subsystem(
    ctx: click.Context,
    subsystem_name: str | None,
    maintainers: str | None,
    target: str | None,
    labels: tuple[str, ...],
    since: str,
    threads: bool,
    forget: bool,
    do_list: bool,
) -> None:
    """Track a kernel subsystem from MAINTAINERS file.

    Creates lei queries for the subsystem:

    \b
    - {name}-mailinglist: Messages to the subsystem mailing list(s)
    - {name}-patches: Patches touching subsystem files

    The configuration is written to conf.d/{subsystem_key}.toml

    Use --forget to remove tracking for a previously tracked subsystem.
    Use --list to display all currently tracked subsystems.
    """
    # Parameter validation
    if not do_list and not subsystem_name:
        raise click.UsageError('SUBSYSTEM_NAME is required unless --list is specified.')
    if do_list:
        config_dir = get_xdg_config_dir()
        conf_d = config_dir / 'conf.d'
        if not conf_d.is_dir():
            click.echo('No tracked subsystems found.')
            return
        toml_files = sorted(conf_d.glob('*.toml'))
        if not toml_files:
            click.echo('No tracked subsystems found.')
            return
        home = Path.home()
        for i, toml_file in enumerate(toml_files):
            try:
                config_data = tomllib.loads(toml_file.read_text())
            except Exception:
                logger.warning('Failed to parse %s, skipping', toml_file.name)
                continue
            subsystem_info = config_data.get('subsystem', {})
            display_name = subsystem_info.get('name')
            if not display_name:
                display_name = toml_file.stem.replace('_', ' ').upper()
            # Use ~ shorthand for home directory
            try:
                display_path = '~' / toml_file.relative_to(home)
            except ValueError:
                display_path = toml_file
            click.echo(display_name)
            click.echo(f'    config: {display_path}')
            deliveries = config_data.get('deliveries', {})
            for dname, dconf in deliveries.items():
                if dname.endswith('-patches'):
                    dtype = 'patches'
                elif dname.endswith('-mailinglist'):
                    dtype = 'mailing list'
                else:
                    dtype = dname
                dtarget = dconf.get('target', 'unknown')
                dlabels = dconf.get('labels', [])
                click.echo(f'    {dtype}:')
                click.echo(f'        target: {dtarget}')
                if dlabels:
                    click.echo(f'        labels: {", ".join(dlabels)}')
            if i < len(toml_files) - 1:
                click.echo('')
        return

    # Handle --forget mode
    if forget:
        assert subsystem_name is not None
        config_dir = get_xdg_config_dir()
        data_dir = ctx.obj.get('data_dir', get_xdg_data_dir())

        conf_d = config_dir / 'conf.d'
        try:
            key, config_file = find_tracked_subsystem_config(conf_d, subsystem_name)
        except ConfigurationError as fe:
            logger.critical('%s', fe)
            raise click.Abort() from fe

        # Read subsystem name from config before removing, for better log output
        forget_display_name = subsystem_name
        if config_file.exists():
            try:
                config_data = tomllib.loads(config_file.read_text())
                stored_name = config_data.get('subsystem', {}).get('name')
                if stored_name:
                    forget_display_name = stored_name
            except Exception:
                pass
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
                        logger.error('Failed to forget lei search %s: %s', lei_path, output.decode())
                except PublicInboxError as e:
                    logger.error('Failed to forget lei search %s: %s', lei_path, str(e))
            else:
                logger.debug('Lei search not found: %s', lei_path)

        logger.info('Removed tracking for subsystem: %s', forget_display_name)
        return

    assert subsystem_name is not None

    # Find MAINTAINERS file: explicit path, ./MAINTAINERS, or fetch from kernel.org
    if maintainers:
        maintainers_path = Path(maintainers)
        if not maintainers_path.exists():
            raise click.ClickException(f'MAINTAINERS file not found: {maintainers}')
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
        catchall_lists: set[str] = set(catchall_lists_config)
        logger.debug('Using catchall_lists from config: %s', catchall_lists)
    else:
        catchall_lists = set(DEFAULT_CATCHALL_LISTS)
    targets = config.get('targets', {})

    target = resolve_target_name(target, targets)

    # Get target instance for default labels
    target_service = get_target(ctx, target)

    # Parse MAINTAINERS file and get subsystem entry
    try:
        entry = get_subsystem(maintainers_path, subsystem_name)
    except KeyError as e:
        logger.critical('%s', str(e))
        raise click.Abort() from e

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
    skipped_patterns: list[str] = []
    mailinglist_created = False
    patches_created = False

    # 1. Mailing list query
    mailinglist_query, excluded_lists = build_mailinglist_query(entry, since, catchall_lists)
    if excluded_lists:
        logger.info('Excluding catch-all lists: %s', ', '.join(excluded_lists))
    if mailinglist_query:
        lei_path = lei_base_path / f'{key}-mailinglist'
        logger.info('Creating mailinglist query: %s', mailinglist_query)
        try:
            retcode, output = create_lei_query_search(mailinglist_query, lei_path, threads=threads)
            if retcode != 0:
                logger.error('Lei query failed for mailinglist: %s', output.decode())
            else:
                write_archive_description(lei_path, f'{entry.name} mailing list traffic')
                # Initialize feed from start so all existing messages are delivered
                feed = LeiFeed(f'{key}-mailinglist', f'lei:{lei_path}')
                epoch = feed.get_highest_epoch()
                first_commit = feed.get_first_commit(epoch)
                if first_commit:
                    feed.init_feed(from_start=True)
                    # Also initialize delivery state from the same starting point
                    delivery_name = f'{key}-mailinglist'
                    feed.save_delivery_info(delivery_name, epoch=epoch, latest_commit=first_commit)
                else:
                    # No messages matched the query; skip init_feed since the
                    # repo is empty and will be populated on the next lei up.
                    logger.warning('No messages found for mailinglist query')
                queries_created += 1
                mailinglist_created = True
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
            retcode, output = create_lei_query_search(patches_query, lei_path, threads=threads)
            if retcode != 0:
                logger.error('Lei query failed for patches: %s', output.decode())
            else:
                write_archive_description(lei_path, f'{entry.name} patches')
                # Initialize feed from start so all existing messages are delivered
                feed = LeiFeed(f'{key}-patches', f'lei:{lei_path}')
                epoch = feed.get_highest_epoch()
                first_commit = feed.get_first_commit(epoch)
                if first_commit:
                    feed.init_feed(from_start=True)
                    # Also initialize delivery state from the same starting point
                    delivery_name = f'{key}-patches'
                    feed.save_delivery_info(delivery_name, epoch=epoch, latest_commit=first_commit)
                else:
                    # No messages matched the query; skip init_feed since the
                    # repo is empty and will be populated on the next lei up.
                    logger.warning('No messages found for patches query')
                queries_created += 1
                patches_created = True
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
        subsystem_name=entry.name,
        include_mailinglist=mailinglist_created,
        include_patches=patches_created,
    )

    config_file = conf_d / f'{key}.toml'
    config_file.write_text(config_content)

    logger.info('Created %d lei queries for subsystem "%s"', queries_created, entry.name)
    logger.info('Configuration written to: %s', config_file)
    logger.info('Target: %s, Labels: %s', target, ', '.join(labels_list))


@main.command()
@click.option('--add', '-a', 'addresses', default=None, help='Add address(es) to the bozofilter (comma-separated)')
@click.option('--reason', '-r', default=None, help='Reason for adding (included as comment)')
@click.option('--edit', '-e', 'do_edit', is_flag=True, help='Edit the bozofilter file in $EDITOR')
@click.option('--list', '-l', 'do_list', is_flag=True, help='List all addresses in the bozofilter')
@click.pass_context
def bozofilter(ctx: click.Context, addresses: str | None, reason: str | None, do_edit: bool, do_list: bool) -> None:
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
