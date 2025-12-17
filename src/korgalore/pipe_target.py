"""Service for delivering messages by piping to an external command."""

import logging
import subprocess
import shlex
from typing import Any, List
from korgalore import ConfigurationError, DeliveryError

logger = logging.getLogger('korgalore')


class PipeTarget:
    """Service for delivering messages by piping to an external command."""

    DEFAULT_LABELS: List[str] = []

    def __init__(self, identifier: str, command: str) -> None:
        """Initialize pipe target.

        Args:
            identifier: Target identifier for logging
            command: Command to pipe messages to (can include arguments)

        Raises:
            ConfigurationError: If command is empty
        """
        self.identifier = identifier

        if not command:
            raise ConfigurationError(
                f"Pipe target '{identifier}' requires a command"
            )

        self.command = command
        # Parse command for validation
        try:
            self.command_args = shlex.split(command)
        except ValueError as e:
            raise ConfigurationError(
                f"Invalid command for pipe target '{identifier}': {e}"
            ) from e

        if not self.command_args:
            raise ConfigurationError(
                f"Pipe target '{identifier}' requires a non-empty command"
            )

    def connect(self) -> None:
        """Connect to pipe target (no-op for local command)."""
        logger.debug('Pipe target ready with command: %s', self.command)

    def import_message(self, raw_message: bytes, labels: List[str]) -> Any:
        """Pipe message to the configured command.

        Args:
            raw_message: Raw email bytes to pipe to stdin
            labels: Additional command line arguments to append

        Returns:
            Return code from the command

        Raises:
            DeliveryError: If command fails or cannot be executed
        """
        # Append labels as additional command line arguments
        command_with_args = self.command_args + labels

        try:
            result = subprocess.run(
                command_with_args,
                input=raw_message,
                capture_output=True
            )

            if result.returncode != 0:
                stderr = result.stderr.decode('utf-8', errors='replace').strip()
                raise DeliveryError(
                    f"Pipe command failed with exit code {result.returncode}: {stderr}"
                )

            logger.debug('Piped message to command: %s', self.command_args[0])
            return result.returncode

        except FileNotFoundError:
            raise DeliveryError(
                f"Pipe command not found: {self.command_args[0]}"
            )
        except Exception as e:
            if isinstance(e, DeliveryError):
                raise
            raise DeliveryError(f"Failed to pipe message: {e}") from e
