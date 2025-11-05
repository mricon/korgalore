"""Korgalore - A command-line tool to put public-inbox sources directly into Gmail."""

__version__ = "0.1.1"
__author__ = "Konstantin Ryabitsev"
__email__ = "konstantin@linuxfoundation.org"


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
