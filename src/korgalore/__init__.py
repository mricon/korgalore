"""Korgalore - A command-line tool to put public-inbox sources directly into Gmail."""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import requests

import liblore
from liblore import LoreNode

__version__ = '0.7.0'
__author__ = 'Konstantin Ryabitsev'
__email__ = 'konstantin@linuxfoundation.org'
__user_agent__ = f'korgalore/{__version__}'

GITCMD: str = 'git'
LEICMD: str = 'lei'

# How often to report that a lei command is still going, in seconds.
#
# A single query can run for many minutes -- importing six months of a busy
# list is the ordinary worst case, not an edge case -- and lei says nothing
# in the meantime, because its output is captured here rather than written
# straight to the terminal. To anything watching this process (a log being
# tailed, a progress view, a proxy with a read timeout) that silence is
# indistinguishable from a hang, so say periodically that work is still in
# progress.
LEI_HEARTBEAT_INTERVAL: float = 30.0

logger = logging.getLogger('korgalore')


# User-agent-plus from lore.useragentplus git config.
# Set at CLI startup via LoreNode.user_agent_plus; applied to git HTTP
# and lei user-agent strings only — NOT to the shared requests session
# (which serves JMAP, MAINTAINERS, etc.).
_user_agent_plus: str | None = None


# Global requests session for HTTP calls
_REQSESSION: requests.Session | None = None


def get_requests_session() -> requests.Session:
    """Get or create the global requests session with korgalore User-Agent."""
    global _REQSESSION
    if _REQSESSION is None:
        _REQSESSION = requests.Session()
        _REQSESSION.headers.update({'User-Agent': __user_agent__})
    return _REQSESSION


def close_requests_session() -> None:
    """Close the global requests session if open."""
    global _REQSESSION
    if _REQSESSION is not None:
        _REQSESSION.close()
        _REQSESSION = None


def make_lore_node(url: str = 'https://lore.kernel.org/all', cache_dir: str | None = None) -> LoreNode:
    """Create a LoreNode with failover/probing from git config.

    Reads the ``[lore]`` section from git config via
    :meth:`LoreNode.from_git_config`, picking up fallback mirror URLs,
    auto-probe settings, and ``lore.useragentplus``.

    The node creates and owns its own :class:`requests.Session` with
    the correct User-Agent.  Use as a context manager for automatic
    cleanup::

        with make_lore_node() as node:
            msgs = node.get_thread_by_msgid(msgid)
    """
    node = LoreNode.from_git_config(url, cache_dir=cache_dir)
    node.set_user_agent('korgalore', __version__)
    return node


# Custom exceptions
class KorgaloreError(Exception):
    """Base exception for all Korgalore errors."""


class ConfigurationError(KorgaloreError):
    """Raised when there is an error in configuration."""


class GitError(KorgaloreError):
    """Raised when there is an error with Git operations."""


class RemoteError(KorgaloreError, liblore.RemoteError):
    """Raised when there is an error communicating with remote services."""


class PublicInboxError(KorgaloreError, liblore.PublicInboxError):
    """Raised when something is wrong with Public-Inbox."""


class FeedLockedError(PublicInboxError):
    """Raised when another process is using a feed."""


class StateError(KorgaloreError):
    """Raised when there is an error with the internal state."""


class StateCorruptError(StateError):
    """Raised when a state file exists but cannot be used."""


class DeliveryError(KorgaloreError):
    """Raised when there is an error during message delivery."""


class AuthenticationError(KorgaloreError):
    """Raised when authentication fails and re-authentication is required."""

    def __init__(self, message: str, target_id: str, target_type: str = 'gmail') -> None:
        super().__init__(message)
        self.target_id = target_id
        self.target_type = target_type


def _init_git_user_agent() -> None:
    """Check git is available and set GIT_HTTP_USER_AGENT environment variable.

    Raises:
        GitError: If git is not installed or fails to run.
    """
    try:
        result = subprocess.run([GITCMD, '--version'], capture_output=True, check=False)
    except FileNotFoundError as e:
        raise GitError(f"Git command '{GITCMD}' not found. Is it installed?") from e

    if result.returncode != 0:
        raise GitError(f'Git command failed: {result.stderr.decode().strip()}')

    # Parse "git version 2.52.0" -> "2.52.0"
    version_output = result.stdout.decode().strip()
    git_version = version_output.split()[-1]
    kgl_ua = f'{__user_agent__}+{_user_agent_plus}' if _user_agent_plus else __user_agent__
    user_agent = f'git/{git_version} ({kgl_ua})'
    os.environ['GIT_HTTP_USER_AGENT'] = user_agent
    logger.debug('Set GIT_HTTP_USER_AGENT to: %s', user_agent)


def run_git_command(
    gitdir: str | None,
    args: list[str],
    stdin: bytes | None = None,
    git_config: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    """Run a git command in the specified git directory and return (returncode, stdout, stderr).

    Uses --git-dir instead of -C to work with safe.bareRepository=explicit.
    Optional *git_config* dict adds ``-c key=value`` flags before the
    subcommand (useful for per-invocation url.<base>.insteadOf).
    """
    cmd = [GITCMD]
    if git_config:
        for key, value in git_config.items():
            cmd += ['-c', f'{key}={value}']
    if gitdir:
        cmd += ['--git-dir', gitdir]
    cmd += args
    logger.debug('Running git command: %s', ' '.join(cmd))

    try:
        result = subprocess.run(cmd, capture_output=True, input=stdin, check=False)
    except FileNotFoundError as e:
        raise GitError(f"Git command '{GITCMD}' not found. Is it installed?") from e
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _report_still_running(what: str, finished: threading.Event, interval: float) -> None:
    """Log that *what* is still going, every *interval* seconds until *finished*.

    Meant to run in a daemon thread for the length of a subprocess call.
    Waiting on the event rather than sleeping means this stops as soon as
    the command returns, so a quick command logs nothing at all.

    *interval* is passed in rather than defaulting to LEI_HEARTBEAT_INTERVAL:
    a default is bound once at import, so the module value could be adjusted
    afterwards and silently make no difference.
    """
    started = time.monotonic()
    while not finished.wait(interval):
        logger.info('Still running lei %s (%d seconds so far)...', what, round(time.monotonic() - started))


def run_lei_command(args: list[str], stdin: bytes | None = None) -> tuple[int, bytes]:
    """Run a lei command and return (returncode, stdout).

    Reports progress every LEI_HEARTBEAT_INTERVAL seconds while the command
    runs, so a long query is distinguishable from a stuck one.

    Args:
        args: Arguments to pass to lei command (first element is the subcommand).
        stdin: Data to feed to the command's standard input, e.g. a query
               for 'lei q --stdin'.

    Returns:
        Tuple of (return_code, stdout_output).

    Raises:
        PublicInboxError: If the lei command is not found.
    """
    # --user-agent is only supported by 'q' and 'up' commands
    cmd = [LEICMD, args[0]]
    if args[0] in ('q', 'up'):
        lei_ua = f'{__user_agent__}+{_user_agent_plus}' if _user_agent_plus else __user_agent__
        cmd += ['--user-agent', lei_ua]
    cmd += args[1:]
    logger.debug('Running lei command: %s', ' '.join(cmd))

    # A daemon thread reports progress rather than a loop around
    # proc.communicate(timeout=...), which would mean giving up
    # subprocess.run() and hand-rolling the output draining it does for us.
    finished = threading.Event()
    threading.Thread(
        target=_report_still_running,
        args=(args[0], finished, LEI_HEARTBEAT_INTERVAL),
        daemon=True,
        name='lei-heartbeat',
    ).start()
    try:
        result = subprocess.run(cmd, capture_output=True, input=stdin, check=False)
    except FileNotFoundError as e:
        raise PublicInboxError(f"LEI command '{LEICMD}' not found. Is it installed?") from e
    finally:
        finished.set()
    return result.returncode, result.stdout.strip()


def format_key_for_display(key: str | None) -> str:
    """Format a key (feed or delivery) for user-facing display by trimming lei paths."""
    if key is None:
        return ''
    if key.startswith('lei:'):
        try:
            return f'lei:{Path(key[4:]).name}'
        except Exception:
            return key
    return key
