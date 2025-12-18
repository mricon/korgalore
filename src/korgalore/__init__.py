"""Korgalore - A command-line tool to put public-inbox sources directly into Gmail."""
import logging
import subprocess

from typing import List, Optional, Tuple

__version__ = "0.2.1"
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

def run_git_command(gitdir: Optional[str], args: List[str],
                    stdin: Optional[bytes] = None) -> Tuple[int, bytes]:
    """Run a git command in the specified topdir and return (returncode, output)."""
    cmd = [GITCMD]
    if gitdir:
        cmd += ['-C', gitdir]
    cmd += args
    logger.debug('Running git command: %s', ' '.join(cmd))

    try:
        result = subprocess.run(cmd, capture_output=True, input=stdin)
    except FileNotFoundError:
        raise GitError(f"Git command '{GITCMD}' not found. Is it installed?")
    return result.returncode, result.stdout.strip()
