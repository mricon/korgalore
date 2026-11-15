"""Service for delivering messages by piping to an external command."""

import logging
import subprocess
from typing import Any

from korgalore import ConfigurationError, DeliveryError, split_command
from korgalore.message import RawMessage

logger = logging.getLogger('korgalore')


class PipeTarget:
    """Service for delivering messages by piping to an external command."""

    DEFAULT_LABELS: list[str] = []
    LINE_ENDINGS = ('crlf', 'lf')

    def __init__(self, identifier: str, command: str, line_endings: str = 'crlf') -> None:
        """Initialize pipe target.

        Args:
            identifier: Target identifier for logging
            command: Command to pipe messages to (can include arguments)
            line_endings: 'crlf' (the wire format) or 'lf', which local MDAs
                and filters such as procmail and formail expect

        Raises:
            ConfigurationError: If command is empty or line_endings is unknown
        """
        self.identifier = identifier

        # Reject anything unknown, instead of falling back to the default,
        # so a typo can't quietly change the bytes a command receives
        if line_endings not in self.LINE_ENDINGS:
            allowed = ', '.join(repr(le) for le in self.LINE_ENDINGS)
            raise ConfigurationError(
                f"Pipe target '{identifier}' has invalid line_endings {line_endings!r}; allowed values: {allowed}"
            )
        self.crlf = line_endings == 'crlf'

        if not command:
            raise ConfigurationError(f"Pipe target '{identifier}' requires a command")

        self.command = command
        # Parse command for validation
        try:
            self.command_args = split_command(command)
        except ValueError as e:
            raise ConfigurationError(f"Invalid command for pipe target '{identifier}': {e}") from e

        if not self.command_args:
            raise ConfigurationError(f"Pipe target '{identifier}' requires a non-empty command")

    def connect(self) -> None:
        """Connect to pipe target (no-op for local command)."""
        logger.debug('Pipe target ready with command: %s', self.command)

    def import_message(
        self,
        raw_message: bytes,
        labels: list[str],
        feed_name: str | None = None,
        delivery_name: str | None = None,
        subfolder: str | None = None,
    ) -> Any:
        """Pipe message to the configured command.

        Args:
            raw_message: Raw email bytes to pipe to stdin
            labels: Additional command line arguments to append
            feed_name: Optional feed name for trace header
            delivery_name: Optional delivery name for trace header
            subfolder: Ignored for pipe target (no folder concept)

        Returns:
            Return code from the command

        Raises:
            DeliveryError: If command fails or cannot be executed
        """
        msg = RawMessage(raw_message)
        # Append labels as additional command line arguments
        command_with_args = self.command_args + labels

        try:
            result = subprocess.run(
                command_with_args,
                input=msg.as_bytes(feed_name, delivery_name, crlf=self.crlf),
                capture_output=True,
                check=False,
            )

            if result.returncode != 0:
                stderr = result.stderr.decode('utf-8', errors='replace').strip()
                raise DeliveryError(f'Pipe command failed with exit code {result.returncode}: {stderr}')

            logger.debug('Piped message to command: %s', self.command_args[0])
            return result.returncode

        except FileNotFoundError as e:
            raise DeliveryError(f'Pipe command not found: {self.command_args[0]}') from e
        except Exception as e:
            if isinstance(e, DeliveryError):
                raise
            raise DeliveryError(f'Failed to pipe message: {e}') from e
