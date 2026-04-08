"""Korgalore - A command-line tool to put public-inbox sources directly into Gmail."""
import logging
import os
import subprocess
from pathlib import Path

import liblore
from liblore import LoreNode
import requests
from typing import List, Optional, Tuple

__version__ = "0.7-dev"
__author__ = "Konstantin Ryabitsev"
__email__ = "konstantin@linuxfoundation.org"
__user_agent__ = f"korgalore/{__version__}"

GITCMD: str = "git"
LEICMD: str = "lei"

logger = logging.getLogger('korgalore')


# User-agent-plus from lore.useragentplus git config.
# Set at CLI startup via LoreNode.user_agent_plus; applied to git HTTP
# and lei user-agent strings only — NOT to the shared requests session
# (which serves JMAP, MAINTAINERS, etc.).
_user_agent_plus: Optional[str] = None


# Global requests session for HTTP calls
_REQSESSION: Optional[requests.Session] = None


def get_requests_session() -> requests.Session:
    """Get or create the global requests session with korgalore User-Agent."""
    global _REQSESSION
    if _REQSESSION is None:
        _REQSESSION = requests.Session()
        _REQSESSION.headers.update({
            'User-Agent': __user_agent__
        })
    return _REQSESSION


def close_requests_session() -> None:
    """Close the global requests session if open."""
    global _REQSESSION
    if _REQSESSION is not None:
        _REQSESSION.close()
        _REQSESSION = None


def make_lore_node(url: str = 'https://lore.kernel.org/all',
                   cache_dir: Optional[str] = None) -> LoreNode:
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
    pass

class ConfigurationError(KorgaloreError):
    """Raised when there is an error in configuration."""
    pass

class GitError(KorgaloreError):
    """Raised when there is an error with Git operations."""
    pass

class RemoteError(KorgaloreError, liblore.RemoteError):
    """Raised when there is an error communicating with remote services."""
    pass

class PublicInboxError(KorgaloreError, liblore.PublicInboxError):
    """Raised when something is wrong with Public-Inbox."""
    pass

class StateError(KorgaloreError):
    """Raised when there is an error with the internal state."""
    pass

class DeliveryError(KorgaloreError):
    """Raised when there is an error during message delivery."""
    pass

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
        result = subprocess.run([GITCMD, '--version'], capture_output=True)
    except FileNotFoundError:
        raise GitError(f"Git command '{GITCMD}' not found. Is it installed?")

    if result.returncode != 0:
        raise GitError(f"Git command failed: {result.stderr.decode().strip()}")

    # Parse "git version 2.52.0" -> "2.52.0"
    version_output = result.stdout.decode().strip()
    git_version = version_output.split()[-1]
    kgl_ua = f"{__user_agent__}+{_user_agent_plus}" if _user_agent_plus else __user_agent__
    user_agent = f"git/{git_version} ({kgl_ua})"
    os.environ['GIT_HTTP_USER_AGENT'] = user_agent
    logger.debug('Set GIT_HTTP_USER_AGENT to: %s', user_agent)

def run_git_command(gitdir: Optional[str], args: List[str],
                    stdin: Optional[bytes] = None) -> Tuple[int, bytes, bytes]:
    """Run a git command in the specified git directory and return (returncode, stdout, stderr).

    Uses --git-dir instead of -C to work with safe.bareRepository=explicit.
    """
    cmd = [GITCMD]
    if gitdir:
        cmd += ['--git-dir', gitdir]
    cmd += args
    logger.debug('Running git command: %s', ' '.join(cmd))

    try:
        result = subprocess.run(cmd, capture_output=True, input=stdin)
    except FileNotFoundError:
        raise GitError(f"Git command '{GITCMD}' not found. Is it installed?")
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def run_lei_command(args: List[str]) -> Tuple[int, bytes]:
    """Run a lei command and return (returncode, stdout).

    Args:
        args: Arguments to pass to lei command (first element is the subcommand).

    Returns:
        Tuple of (return_code, stdout_output).

    Raises:
        PublicInboxError: If the lei command is not found.
    """
    # --user-agent is only supported by 'q' and 'up' commands
    cmd = [LEICMD, args[0]]
    if args[0] in ('q', 'up'):
        lei_ua = f"{__user_agent__}+{_user_agent_plus}" if _user_agent_plus else __user_agent__
        cmd += ['--user-agent', lei_ua]
    cmd += args[1:]
    logger.debug('Running lei command: %s', ' '.join(cmd))

    try:
        result = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError:
        raise PublicInboxError(f"LEI command '{LEICMD}' not found. Is it installed?")
    return result.returncode, result.stdout.strip()


def format_key_for_display(key: Optional[str]) -> str:
    """Format a key (feed or delivery) for user-facing display by trimming lei paths."""
    if key is None:
        return ""
    if key.startswith('lei:'):
        try:
            return f"lei:{Path(key[4:]).name}"
        except Exception:
            return key
    return key
