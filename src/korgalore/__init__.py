"""Korgalore - A command-line tool to put public-inbox sources directly into Gmail."""
import logging
import subprocess
from pathlib import Path

from typing import List, Optional, Tuple

__version__ = "0.3-dev"
__author__ = "Konstantin Ryabitsev"
__email__ = "konstantin@linuxfoundation.org"

GITCMD: str = "git"

logger = logging.getLogger('korgalore')

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

class RemoteError(KorgaloreError):
    """Raised when there is an error communicating with remote services."""
    pass

class PublicInboxError(KorgaloreError):
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

def run_git_command(gitdir: Optional[str], args: List[str],
                    stdin: Optional[bytes] = None) -> Tuple[int, bytes]:
    """Run a git command in the specified git directory and return (returncode, output).

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
