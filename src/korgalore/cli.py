"""Command-line interface for korgalore."""

import os
import click
import tomllib

from pathlib import Path
from typing import Dict, Any, List, Tuple
from korgalore.gmail_service import GmailService
from korgalore.lore_service import LoreService
from korgalore import __version__


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


def load_config(cfgdir: Path, verbose: bool = False) -> Dict[str, Any]:
    config_file = cfgdir / 'korgalore.toml'
    config: Dict[str, Any] = dict()

    if not config_file.exists():
        click.secho(f'No config file found at {config_file}', fg='red', err=True)
        click.Abort()

    try:
        if verbose:
            click.secho(f'Loading config from {config_file}', fg='cyan', err=True)

        with open(config_file, 'rb') as cf:
            config = tomllib.load(cf)

        if verbose:
            click.secho('Config loaded successfully', fg='cyan', err=True)

        return config

    except Exception as e:
        if verbose:
            click.secho(f'Error loading config: {str(e)}', fg='red', err=True)
        return config


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
    verbose = ctx.obj.get('verbose', False)
    ls = ctx.obj['lore']
    gs = ctx.obj['gmail']
    cfg = ctx.obj.get('config', {})
    details = cfg['sources'][listname]

    last_commit = ''

    with click.progressbar(commits,
                            label=f'Uploading {listname}',
                            show_pos=True,
                            hidden=verbose) as bar:
        for at_commit in bar:
            try:
                raw_message = ls.get_message_at_commit(gitdir, at_commit)
            except FileNotFoundError:
                # Assuming non-m commit
                continue
            try:
                gs.import_message(raw_message, label_ids=details.get('label_ids', None))
            except RuntimeError as re:
                click.secho(f'Failed to upload message at commit {at_commit}: {str(re)}', fg='red', err=True)
                click.Abort()
            ls.update_piper_info(gitdir=gitdir, latest_commit=at_commit, message=raw_message)
            last_commit = at_commit
            if verbose:
                msg = ls.parse_message(raw_message)
                click.secho(f"  Uploaded: {msg.get('Subject', '(no subject)')}", fg='cyan', err=True)

    return last_commit


@click.group()
@click.version_option(version=__version__)
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose output')
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """korgalore - Fetch lore.kernel.org mailing list messages into Gmail."""

    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose

    # Load configuration file
    cfgdir = get_xdg_config_dir()
    config = load_config(cfgdir, verbose=verbose)
    ctx.obj['config'] = config

    # Ensure XDG data directory exists
    data_dir = get_xdg_data_dir()
    ctx.obj['data_dir'] = data_dir

    if verbose:
        click.secho('Verbose mode enabled', fg='cyan', err=True)
        click.secho(f'Data directory: {data_dir}', fg='cyan', err=True)

    try:
        ctx.obj['gmail'] = GmailService(cfgdir)
    except FileNotFoundError as fe:
        click.secho(f'Error: {str(fe)}', fg='red', err=True)
        click.secho('Please run "korgalore auth" to authenticate first.', fg='red', err=True)
        raise click.Abort()
    ctx.obj['lore'] = LoreService(data_dir)


@main.command()
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Authenticate with Gmail API."""
    gmail = ctx.obj['gmail']
    verbose = ctx.obj.get('verbose', False)

    try:
        if verbose:
            click.secho('Initiating authentication...', fg='cyan', err=True)
        gmail.authenticate()
        click.echo("Authentication successful!")
    except Exception as e:
        if verbose:
            click.secho(f'Authentication failed: {str(e)}', fg='red', err=True)
        click.echo(f"Error: {str(e)}", err=True)
        raise click.Abort()


@main.command()
@click.pass_context
@click.option('--ids', '-i', is_flag=True, help='include id values')
def labels(ctx: click.Context, ids: bool = False) -> None:
    """List all available labels."""
    gmail = ctx.obj['gmail']
    verbose = ctx.obj.get('verbose', False)

    try:
        if verbose:
            click.secho('Fetching labels from Gmail...', fg='cyan', err=True)

        labels_list = gmail.list_labels()

        if not labels_list:
            click.echo("No labels found.")
            return

        if verbose:
            click.secho(f'Found {len(labels_list)} labels', fg='cyan', err=True)

        click.echo("Available labels:\n")
        for label in labels_list:
            if ids:
                click.echo(f"  - {label['name']} (ID: {label['id']})")
            else:
                click.echo(f"  - {label['name']}")

    except Exception as e:
        if verbose:
            click.secho(f'Failed to fetch labels: {str(e)}', fg='red', err=True)
        click.echo(f"Error: {str(e)}", err=True)
        raise click.Abort()


@main.command()
@click.pass_context
def pull(ctx: click.Context) -> None:
    """Pull updates from all subscribed mailing lists."""
    ls = ctx.obj['lore']
    gs = ctx.obj['gmail']
    verbose = ctx.obj.get('verbose', False)
    data_dir = ctx.obj['data_dir']

    cfg = ctx.obj.get('config', {})
    try:
        cfg = translate_labels(gs, cfg)
    except ValueError as ve:
        click.secho(f'Configuration error: {str(ve)}', fg='red', err=True)
        raise click.Abort()

    sources = cfg.get('sources', {})

    for listname, details in sources.items():
        if verbose:
            click.secho(f'Processing list: {listname}', fg='cyan', err=True)

        latest_epochs = ls.get_epochs(details['feed'])

        list_dir = data_dir / f'{listname}'
        if not list_dir.exists():
            click.secho(f'List directory {list_dir} does not exist. Initializing.', fg='cyan', err=True)
            ls.init_list(list_name=listname, list_dir=list_dir, pi_url=details['feed'])
            ls.store_epochs_info(list_dir=list_dir, epochs=latest_epochs)
            continue

        current_epochs: List[Tuple[int, str, str]] = list()
        try:
            current_epochs = ls.load_epochs_info(list_dir=list_dir)
        except FileNotFoundError:
            pass

        if current_epochs == latest_epochs:
            if verbose:
                click.secho(f'No updates for {listname}', fg='cyan', err=True)
            continue

        # Pull the highest epoch we have
        highest_epoch, gitdir, commits = ls.pull_highest_epoch(list_dir=list_dir)
        if commits:
            if verbose:
                click.secho(f'Found {len(commits)} new commits for list {listname}', fg='cyan', err=True)

            last_commit = process_commits(listname=listname, commits=commits, gitdir=gitdir, ctx=ctx)
        else:
            last_commit = ''
            if verbose:
                click.secho(f'No new commits for list {listname}', fg='cyan', err=True)

        # XXX: check for epoch rollover
        ls.store_epochs_info(list_dir=list_dir, epochs=latest_epochs)
        if last_commit:
            ls.reshallow(gitdir=gitdir, since_commit=last_commit)


if __name__ == '__main__':
    main()
