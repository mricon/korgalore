"""Command-line interface for korgalore."""

import os
import click
import tomllib
import logging
import click_log
import json

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


def load_config(cfgfile: Path) -> Dict[str, Any]:
    config: Dict[str, Any] = dict()

    if not cfgfile.exists():
        logger.error('Config file not found: %s', str(cfgfile))
        click.Abort()

    try:
        logger.debug('Loading config from %s', str(cfgfile))

        with open(cfgfile, 'rb') as cf:
            config = tomllib.load(cf)

        logger.debug('Config loaded with %s targets, %s sources, and %s feeds',
                     len(config.get('targets', {})), len(config.get('sources', {})),
                     len(config.get('feeds', {})))

        return config

    except Exception as e:
        logger.error('Error loading config: %s', str(e))
        raise click.Abort()


def load_failed_commits(gitdir: Path) -> Dict[str, int]:
    """Load the tracking file of failed commits.

    Returns a dict mapping commit hash to failure count.
    """
    failed_file = gitdir / 'korgalore.failed'
    if not failed_file.exists():
        return {}

    try:
        with open(failed_file, 'r') as f:
            data: Dict[str, int] = json.load(f)
            return data
    except Exception as e:
        logger.warning('Failed to load failed commits file: %s', str(e))
        return {}


def save_failed_commits(gitdir: Path, failed_commits: Dict[str, int]) -> None:
    """Save the tracking file of failed commits."""
    failed_file = gitdir / 'korgalore.failed'
    try:
        with open(failed_file, 'w') as f:
            json.dump(failed_commits, f, indent=2)
    except Exception as e:
        logger.error('Failed to save failed commits file: %s', str(e))


def load_rejected_commits(gitdir: Path) -> List[str]:
    rejected_file = gitdir / 'korgalore.rejected'
    if not rejected_file.exists():
        return []

    try:
        with open(rejected_file, 'r') as f:
            data: List[str] = json.load(f)
            return data
    except Exception as e:
        logger.warning('Failed to load rejected commits file: %s', str(e))
        return []


def save_rejected_commits(gitdir: Path, rejected_commits: List[str]) -> None:
    rejected_file = gitdir / 'korgalore.rejected'
    try:
        with open(rejected_file, 'w') as f:
            json.dump(rejected_commits, f, indent=2)
    except Exception as e:
        logger.error('Failed to save rejected commits file: %s', str(e))


def retry_failed_commits(gitdir: Path, ls: Any, gs: Any, labels: List[str]) -> Tuple[int, Dict[str, int], List[str]]:
    failed_commits = load_failed_commits(gitdir)
    rejected_commits = load_rejected_commits(gitdir)

    if not failed_commits:
        return 0, {}, []

    logger.info('Retrying %d previously failed commits', len(failed_commits))

    success_count = 0
    still_failed = {}
    newly_rejected = []

    for commit_hash, fail_count in failed_commits.items():
        try:
            raw_message = ls.get_message_at_commit(gitdir, commit_hash)
        except (StateError, GitError) as e:
            logger.debug('Skipping retry of commit %s: %s', commit_hash, str(e))
            # Still count this as failed
            still_failed[commit_hash] = fail_count
            continue

        try:
            gs.import_message(raw_message, labels=labels)
            logger.debug('Successfully retried commit %s', commit_hash)
            success_count += 1
            # Don't add to still_failed - it succeeded!
        except RemoteError:
            # Failed again
            new_fail_count = fail_count + 1
            if new_fail_count > 5:
                logger.warning('Commit %s failed %d times, moving to rejected', commit_hash, new_fail_count)
                newly_rejected.append(commit_hash)
                rejected_commits.append(commit_hash)
            else:
                logger.debug('Commit %s failed again (attempt %d)', commit_hash, new_fail_count)
                still_failed[commit_hash] = new_fail_count

    # Save updated tracking files
    if still_failed:
        save_failed_commits(gitdir, still_failed)
    else:
        # Remove the file if all retries succeeded
        failed_file = gitdir / 'korgalore.failed'
        if failed_file.exists():
            failed_file.unlink()

    if newly_rejected:
        save_rejected_commits(gitdir, rejected_commits)

    if success_count > 0:
        logger.info('Successfully retried %d commits', success_count)

    return success_count, still_failed, newly_rejected


def process_commits(listname: str, commits: List[str], gitdir: Path,
                    ctx: click.Context, max_count: int = 0,
                    still_failed_dict: Optional[Dict[str, int]] = None) -> Tuple[int, str]:
    if max_count > 0 and len(commits) > max_count:
        # Take the last NN messages and discard the rest
        logger.info('Limiting to %d messages as requested', max_count)
        commits = commits[-max_count:]

    # This can be either a lore or a lei source, but it doesn't really
    # matter, as we use the underlying pi_service class for all actions
    ls = ctx.obj['lore']
    if ls is None:
        ls = ctx.obj['lei']
    cfg = ctx.obj.get('config', {})

    details = cfg['sources'][listname]
    target = details.get('target', '')
    labels = details.get('labels', [])

    try:
        gs = get_target(ctx, target)
    except click.Abort:
        logger.critical('Failed to process list "%s".', listname)
        raise ConfigurationError()

    # Use the passed-in still_failed_dict or create a new one
    if still_failed_dict is None:
        still_failed_dict = {}

    # Track failed commits from this run
    failed_commits_this_run: Dict[str, int] = {}

    last_commit = ''
    consecutive_failures = 0
    # If we hit this many consecutive failures, abort, because clearly
    # the remote is having issues.
    MAX_CONSECUTIVE_FAILURES = 5

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
                # Assuming non-m commit, don't count as failure
                continue

            try:
                gs.import_message(raw_message, labels=labels)
                count += 1
                consecutive_failures = 0  # Reset on success

                # Remove from still_failed_dict if it was there and save immediately
                if at_commit in still_failed_dict:
                    del still_failed_dict[at_commit]
                    logger.debug('Removed successfully imported commit %s from failed list', at_commit)
                    # Save immediately to persist the removal
                    merged_failed = {**still_failed_dict, **failed_commits_this_run}
                    save_failed_commits(gitdir, merged_failed)
            except RemoteError as err:
                logger.error('Failed to upload message at commit %s: %s', at_commit, str(err))

                # Track this failure
                failed_commits_this_run[at_commit] = 1
                consecutive_failures += 1

                # Save failed commits immediately to disk
                merged_failed = {**still_failed_dict, **failed_commits_this_run}
                save_failed_commits(gitdir, merged_failed)

                # Check if we should abort
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.critical('Aborting after %d consecutive failures', consecutive_failures)
                    return count, last_commit

                # Continue to next commit
                continue

            # Update tracking info on success
            ls.update_korgalore_info(gitdir=gitdir, latest_commit=at_commit, message=raw_message)
            last_commit = at_commit
            if logger.isEnabledFor(logging.DEBUG):
                msg = ls.parse_message(raw_message)
                logger.debug(' -> %s', msg.get('Subject', '(no subject)'))

    # Final check: if we have no failed commits at all, remove the failed commits file
    merged_failed = {**still_failed_dict, **failed_commits_this_run}
    if not merged_failed:
        failed_file = gitdir / 'korgalore.failed'
        if failed_file.exists():
            failed_file.unlink()
            logger.debug('Removed failed commits file as all commits succeeded')
    elif failed_commits_this_run:
        # Only log if we had new failures this run (already saved immediately)
        logger.info('%d commits failed and will be retried next run', len(merged_failed))

    return count, last_commit


def process_lei_list(ctx: click.Context, listname: str,
                     details: Dict[str, Any], max_mail: int) -> int:
    # Make sure lei knows about this list
    # Placeholder for future LEI feed processing logic
    lei = ctx.obj['lei']
    if lei is None:
        lei = LeiService()
        ctx.obj['lei'] = lei

    # Resolve feed URL from name or use direct URL
    cfg = ctx.obj.get('config', {})
    feed_url = resolve_feed_url(details.get('feed', ''), cfg)
    feed = feed_url[4:]  # Strip 'lei:' prefix
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

    # XXX: this doesn't do the right thing with epoch rollover yet
    gitdir = feedpath / 'git' / f'{latest_epoch}.git'

    # Get target and labels for retry logic
    ls = ctx.obj['lore']
    target = details.get('target', '')
    labels = details.get('labels', [])
    try:
        gs = get_target(ctx, target)
    except click.Abort:
        logger.critical('Failed to get target for list "%s".', listname)
        raise ConfigurationError()

    # Always retry failed commits, even if there are no new commits
    retry_success, still_failed_dict, newly_rejected = retry_failed_commits(gitdir, ls, gs, labels)
    count = retry_success if retry_success > 0 else 0

    if known_epochs == latest_epochs:
        logger.debug('No updates for LEI list: %s', listname)
        return count

    commits = lei.get_latest_commits_in_epoch(gitdir)
    if commits:
        logger.debug('Found %d new commits for list %s', len(commits), listname)
        new_count, last_commit = process_commits(listname=listname, commits=commits,
                                             gitdir=gitdir, ctx=ctx, max_count=max_mail,
                                             still_failed_dict=still_failed_dict)
        count += new_count
        lei.save_epoch_info(list_dir=feedpath, epochs=latest_epochs)
        return count
    else:
        logger.debug('No new commits to process for LEI list %s', listname)
        lei.save_epoch_info(list_dir=feedpath, epochs=latest_epochs)
        return count


def process_lore_list(ctx: click.Context, listname: str,
                      details: Dict[str, Any], max_mail: int) -> int:
    ls = ctx.obj['lore']
    if ls is None:
        data_dir = ctx.obj['data_dir']
        ls = LoreService(data_dir)
        ctx.obj['lore'] = ls

    # Resolve feed URL from name or use direct URL
    cfg = ctx.obj.get('config', {})
    feed_url = resolve_feed_url(details.get('feed', ''), cfg)

    latest_epochs = ls.get_epochs(feed_url)
    count = 0

    data_dir = ctx.obj['data_dir']

    list_dir = data_dir / f'{listname}'
    if not list_dir.exists():
        ls.init_list(list_name=listname, list_dir=list_dir, pi_url=feed_url)
        ls.store_epochs_info(list_dir=list_dir, epochs=latest_epochs)
        logger.info('Initialized: %s.', listname)
        return 0

    current_epochs: List[Tuple[int, str, str]] = list()
    try:
        current_epochs = ls.load_epochs_info(list_dir=list_dir)
    except StateError:
        pass

    # Pull the highest epoch we have
    logger.debug('Running git pull on list: %s', listname)
    highest_epoch, gitdir, commits = ls.pull_highest_epoch(list_dir=list_dir)

    # Get target and labels for retry logic
    target = details.get('target', '')
    labels = details.get('labels', [])
    try:
        gs = get_target(ctx, target)
    except click.Abort:
        logger.critical('Failed to get target for list "%s".', listname)
        raise ConfigurationError()

    # Always retry failed commits, even if there are no new commits
    retry_success, still_failed_dict, newly_rejected = retry_failed_commits(gitdir, ls, gs, labels)
    if retry_success > 0:
        count = retry_success
    else:
        count = 0

    if current_epochs == latest_epochs and not commits:
        logger.debug('No updates for lore list: %s', listname)
        return count

    if commits:
        logger.debug('Found %d new commits for list %s', len(commits), listname)
        new_count, last_commit = process_commits(listname=listname, commits=commits,
                                             gitdir=gitdir, ctx=ctx, max_count=max_mail,
                                             still_failed_dict=still_failed_dict)
        count += new_count
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
        repo_url = f"{feed_url.rstrip('/')}/git/{next_epoch}.git"
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
        # For new epochs, we need to get a fresh still_failed_dict since it's a different gitdir
        new_retry_success, new_still_failed_dict, new_newly_rejected = retry_failed_commits(tgt_dir, ls, gs, labels)
        count += new_retry_success
        new_count, last_commit = process_commits(listname=listname, commits=commits,
                                                 gitdir=tgt_dir, ctx=ctx, max_count=remaining_mail,
                                                 still_failed_dict=new_still_failed_dict)
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
    """Pull messages from configured lore and LEI lists."""
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

        # Resolve feed URL from name or use direct URL
        feed_url = resolve_feed_url(details.get('feed', ''), cfg)

        if feed_url.startswith('https:'):
            try:
                count = process_lore_list(ctx=ctx, listname=listname, details=details, max_mail=max_mail)
            except Exception as e:
                logger.critical('Failed to process lore list "%s": %s', listname, str(e))
                continue
            if count > 0:
                changes.append((listname, count))
        elif feed_url.startswith('lei:'):
            try:
                count = process_lei_list(ctx=ctx, listname=listname, details=details, max_mail=max_mail)
            except Exception as e:
                logger.critical('Failed to process LEI list "%s": %s', listname, str(e))
                continue
            if count > 0:
                changes.append((listname, count))
        else:
            logger.warning('Unknown feed type for list %s: %s', listname, feed_url)
            continue
    if changes:
        logger.info('Pull complete with updates:')
        for listname, count in changes:
            logger.info('  %s: %d', listname, count)
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
    """Yank a single message or entire thread to a Gmail target."""
    # Get the lore service
    ls = ctx.obj.get('lore')
    if ls is None:
        data_dir = ctx.obj['data_dir']
        ls = LoreService(data_dir)
        ctx.obj['lore'] = ls

    # Get the target Gmail service
    if not target:
        # Get the first target in the list
        config = ctx.obj.get('config', {})
        targets = config.get('targets', {})
        target = list(targets.keys())[0]
        logger.debug('No target specified, using first target: %s', target)

    try:
        gs = get_target(ctx, target)
    except click.Abort:
        logger.critical('Failed to get target "%s".', target)
        raise

    # Convert labels tuple to list
    labels_list = list(labels) if labels else []

    if thread:
        # Fetch the entire thread
        logger.debug('Fetching thread: %s', msgid_or_url)
        try:
            messages = ls.get_thread_by_msgid(msgid_or_url)
        except RemoteError as e:
            logger.critical('Failed to fetch thread: %s', str(e))
            raise click.Abort()

        logger.info('Found %d messages in thread', len(messages))

        # Upload each message in the thread
        uploaded = 0
        failed = 0

        if logger.isEnabledFor(logging.DEBUG):
            hidden = True
        elif logger.isEnabledFor(logging.INFO):
            hidden = False
        else:
            hidden = True

        with click.progressbar(messages,
                              label='Uploading thread',
                              show_pos=True,
                              hidden=hidden) as bar:
            for raw_message in bar:
                try:
                    msg = ls.parse_message(raw_message)
                    subject = msg.get('Subject', '(no subject)')
                    logger.debug('Uploading: %s', subject)
                    gs.import_message(raw_message, labels=labels_list)
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
            raw_message = ls.get_message_by_msgid(msgid_or_url)
        except RemoteError as e:
            logger.critical('Failed to fetch message: %s', str(e))
            raise click.Abort()

        # Parse to get the subject for logging
        msg = ls.parse_message(raw_message)
        subject = msg.get('Subject', '(no subject)')
        logger.debug('Message subject: %s', subject)

        # Upload the message
        logger.info('Uploading to target "%s"', target)
        logger.debug('Uploading: %s', subject)
        try:
            gs.import_message(raw_message, labels=labels_list)
            logger.info('Successfully uploaded message.')
        except RemoteError as e:
            logger.critical('Failed to upload message: %s', str(e))
            raise click.Abort()


if __name__ == '__main__':
    main()
