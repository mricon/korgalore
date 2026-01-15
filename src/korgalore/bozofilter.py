"""Bozofilter for blocking messages from unwanted addresses."""

import logging
import os
import subprocess
from datetime import date
from email.utils import parseaddr
from pathlib import Path
from typing import Set, Optional

logger = logging.getLogger('korgalore')


def get_bozofilter_path(config_dir: Path) -> Path:
    """Get the path to the bozofilter file."""
    return config_dir / 'bozofilter.txt'


def load_bozofilter(config_dir: Path) -> Set[str]:
    """Load and parse the bozofilter file.

    Args:
        config_dir: Path to the korgalore config directory.

    Returns:
        Set of lowercase email addresses in the filter.
    """
    bozofilter_path = get_bozofilter_path(config_dir)
    addresses: Set[str] = set()

    if not bozofilter_path.exists():
        return addresses

    with open(bozofilter_path, 'r') as f:
        for line in f:
            # Strip whitespace
            line = line.strip()

            # Skip empty lines and comment-only lines
            if not line or line.startswith('#'):
                continue

            # Remove trailing comment
            if '#' in line:
                line = line.split('#', 1)[0].strip()

            # Skip if nothing left after removing comment
            if not line:
                continue

            # Normalize to lowercase
            addresses.add(line.lower())

    return addresses


def add_to_bozofilter(config_dir: Path, addresses: list[str],
                      reason: Optional[str] = None) -> int:
    """Add addresses to the bozofilter.

    Args:
        config_dir: Path to the korgalore config directory.
        addresses: List of email addresses to add.
        reason: Optional reason for adding (included as comment).

    Returns:
        Number of new addresses added.
    """
    bozofilter_path = get_bozofilter_path(config_dir)

    # Load existing addresses
    existing = load_bozofilter(config_dir)

    # Prepare new entries
    added = 0
    new_lines: list[str] = []
    today = date.today().isoformat()

    for addr in addresses:
        addr_lower = addr.lower().strip()
        if not addr_lower:
            continue
        if addr_lower in existing:
            logger.info('Address already in bozofilter: %s', addr_lower)
            continue

        # Build the line with comment
        comment_parts = [f'added on {today}']
        if reason:
            comment_parts.append(reason)
        comment = ', '.join(comment_parts)

        new_lines.append(f'{addr_lower} # {comment}\n')
        added += 1

    if new_lines:
        # Ensure config dir exists
        config_dir.mkdir(parents=True, exist_ok=True)

        # Append to file
        with open(bozofilter_path, 'a') as f:
            f.writelines(new_lines)

    return added


def ensure_bozofilter_exists(config_dir: Path) -> Path:
    """Ensure the bozofilter file exists, creating with default content if needed.

    Args:
        config_dir: Path to the korgalore config directory.

    Returns:
        Path to the bozofilter file.
    """
    bozofilter_path = get_bozofilter_path(config_dir)

    config_dir.mkdir(parents=True, exist_ok=True)
    if not bozofilter_path.exists():
        with open(bozofilter_path, 'w') as f:
            f.write('# Korgalore bozofilter - one email address per line\n')
            f.write('# Lines starting with # are comments\n')
            f.write('# Trailing comments after # are also supported\n')
            f.write('#\n')
            f.write('# Example:\n')
            f.write('# spammer@example.com # added on 2026-01-15, sends junk\n')
            f.write('\n')

    return bozofilter_path


def edit_bozofilter(config_dir: Path) -> bool:
    """Open the bozofilter file in the user's editor.

    Args:
        config_dir: Path to the korgalore config directory.

    Returns:
        True if editor exited successfully, False otherwise.
    """
    ensure_bozofilter_exists(config_dir)
    bozofilter_path = get_bozofilter_path(config_dir)

    # Get editor from environment
    editor = os.environ.get('EDITOR', os.environ.get('VISUAL', 'vi'))

    try:
        result = subprocess.run([editor, str(bozofilter_path)])
        return result.returncode == 0
    except FileNotFoundError:
        logger.error('Editor not found: %s', editor)
        return False


def extract_email_address(from_header: str) -> Optional[str]:
    """Extract the email address from a From: header value.

    Args:
        from_header: The From: header value (e.g., "Name <addr@example.com>")

    Returns:
        The extracted email address in lowercase, or None if not found.
    """
    if not from_header:
        return None

    _, addr = parseaddr(from_header)
    if addr:
        return addr.lower()

    return None


def is_bozofied(from_header: str, bozofilter: Set[str]) -> bool:
    """Check if a From: header matches any address in the bozofilter.

    Args:
        from_header: The From: header value.
        bozofilter: Set of lowercase email addresses to filter.

    Returns:
        True if the address is in the bozofilter, False otherwise.
    """
    if not bozofilter:
        return False

    addr = extract_email_address(from_header)
    if addr and addr in bozofilter:
        return True

    return False
