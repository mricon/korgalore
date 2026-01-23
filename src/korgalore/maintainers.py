"""Parser for Linux kernel MAINTAINERS file and lei query builders."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger('korgalore')

# Default catch-all mailing lists to exclude from subsystem queries.
# These lists receive copies of most/all kernel patches and would flood
# subsystem-specific queries with irrelevant messages.
DEFAULT_CATCHALL_LISTS: List[str] = [
    'linux-kernel@vger.kernel.org',
    'patches@lists.linux.dev',
]

# Regex metacharacters that indicate a pattern is not a simple word
REGEX_METACHARACTERS = r'\[](){}|^$?+*'


@dataclass
class SubsystemEntry:
    """Represents a subsystem entry from the MAINTAINERS file."""
    name: str
    maintainers: List[str] = field(default_factory=list)  # M: emails
    reviewers: List[str] = field(default_factory=list)    # R: emails
    mailing_lists: List[str] = field(default_factory=list)  # L: addresses
    files: List[str] = field(default_factory=list)        # F: patterns
    excluded: List[str] = field(default_factory=list)     # X: patterns
    file_regex: List[str] = field(default_factory=list)   # N: patterns
    content_regex: List[str] = field(default_factory=list)  # K: patterns
    status: Optional[str] = None                          # S: value


def normalize_subsystem_name(name: str) -> str:
    """Convert 'SUBSYSTEM NAME' to 'subsystem_name' for use as key.

    Examples:
        '9P FILE SYSTEM' -> '9p_file_system'
        'AMD GPU' -> 'amd_gpu'
        '3WARE SAS/SATA-RAID SCSI DRIVERS (3W-XXXX)' -> '3ware_sas_sata_raid_scsi_drivers'
    """
    # Convert to lowercase
    key = name.lower()
    # Remove parenthetical content
    key = re.sub(r'\s*\([^)]*\)', '', key)
    # Replace non-alphanumeric with underscore
    key = re.sub(r'[^a-z0-9]+', '_', key)
    # Remove leading/trailing underscores
    key = key.strip('_')
    return key


def extract_email(line: str) -> Optional[str]:
    """Extract email address from a maintainer/reviewer line.

    Handles formats like:
        'Full Name <email@domain.com>'
        '"Full Name" <email@domain.com>'
        'email@domain.com'
    """
    _, email = parseaddr(line)
    return email if email else None


def is_field_line(line: str) -> bool:
    """Check if line is a MAINTAINERS field line (e.g., 'M:\\t...').

    Field lines match pattern: single uppercase letter, colon, tab.
    """
    return len(line) >= 3 and line[0].isupper() and line[1] == ':' and line[2] == '\t'


def is_subsystem_title(line: str, prev_line_empty: bool) -> bool:
    """Check if line is a subsystem title.

    A subsystem title must:
    - Follow an empty line (or be at start of entries section)
    - Not contain a tab character
    - Contain at least one uppercase letter

    Note: This may match some preamble lines like "Maintainers List" which
    would result in entries with empty fields. This is harmless since such
    entries have no maintainers, files, or mailing lists to query.
    """
    if not prev_line_empty:
        return False
    if '\t' in line:
        return False
    if not line or not line.strip():
        return False
    # Must contain at least one uppercase letter
    return any(c.isupper() for c in line)


def has_fields(entry: SubsystemEntry) -> bool:
    """Check if entry has any populated fields.

    Entries without fields (like preamble lines) are considered bogus.
    """
    return bool(
        entry.maintainers or entry.reviewers or entry.mailing_lists or
        entry.files or entry.excluded or entry.file_regex or
        entry.content_regex or entry.status
    )


def parse_maintainers(path: Path) -> Dict[str, SubsystemEntry]:
    """Parse entire MAINTAINERS file into dict of entries.

    Args:
        path: Path to MAINTAINERS file

    Returns:
        Dict mapping subsystem names to SubsystemEntry objects.
        Entries without any fields are skipped.
    """
    entries: Dict[str, SubsystemEntry] = {}
    current_entry: Optional[SubsystemEntry] = None
    prev_line_empty = True  # Start as true to catch first entry

    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')

            # Track empty lines for subsystem title detection
            if not line.strip():
                prev_line_empty = True
                continue

            # Check for field lines first (M:, L:, F:, etc.)
            if is_field_line(line):
                if current_entry:
                    prefix = line[0]
                    value = line[3:].strip()

                    if prefix == 'M':
                        email = extract_email(value)
                        if email:
                            current_entry.maintainers.append(email)
                    elif prefix == 'R':
                        email = extract_email(value)
                        if email:
                            current_entry.reviewers.append(email)
                    elif prefix == 'L':
                        current_entry.mailing_lists.append(value)
                    elif prefix == 'F':
                        current_entry.files.append(value)
                    elif prefix == 'X':
                        current_entry.excluded.append(value)
                    elif prefix == 'N':
                        current_entry.file_regex.append(value)
                    elif prefix == 'K':
                        current_entry.content_regex.append(value)
                    elif prefix == 'S':
                        current_entry.status = value
                prev_line_empty = False
                continue

            # Check for new subsystem title
            if is_subsystem_title(line, prev_line_empty):
                # Save previous entry if it has fields
                if current_entry and has_fields(current_entry):
                    entries[current_entry.name] = current_entry
                # Start new entry
                current_entry = SubsystemEntry(name=line.strip())

            prev_line_empty = False

        # Don't forget the last entry
        if current_entry and has_fields(current_entry):
            entries[current_entry.name] = current_entry

    return entries


def get_subsystem(path: Path, name: str) -> SubsystemEntry:
    """Get a specific subsystem entry by name or substring.

    Args:
        path: Path to MAINTAINERS file
        name: Subsystem name or substring (case-insensitive)

    Returns:
        SubsystemEntry for the requested subsystem

    Raises:
        KeyError: If no subsystem matches
        ValueError: If multiple subsystems match (ambiguous)
    """
    entries = parse_maintainers(path)

    # Try exact match first
    if name in entries:
        return entries[name]

    # Try case-insensitive exact match
    name_upper = name.upper()
    for entry_name, entry in entries.items():
        if entry_name.upper() == name_upper:
            return entry

    # Try substring match (case-insensitive)
    matches: List[Tuple[str, SubsystemEntry]] = []
    for entry_name, entry in entries.items():
        if name_upper in entry_name.upper():
            matches.append((entry_name, entry))

    if len(matches) == 1:
        return matches[0][1]

    if len(matches) > 1:
        match_names = [m[0] for m in matches]
        raise ValueError(
            f"Ambiguous subsystem '{name}' matches {len(matches)} entries:\n"
            + "\n".join(f"  - {n}" for n in sorted(match_names))
        )

    raise KeyError(f"Subsystem '{name}' not found in MAINTAINERS file")


def is_simple_pattern(pattern: str) -> bool:
    """Check if pattern is a simple word (no regex metacharacters).

    Xapian does not support regex, so we can only use simple patterns.
    """
    return not any(c in pattern for c in REGEX_METACHARACTERS)


def email_to_list_id(email: str) -> str:
    """Convert mailing list email to list-id format.

    Example: 'v9fs@lists.linux.dev' -> 'v9fs.lists.linux.dev'
    """
    return email.replace('@', '.')


def build_maintainers_query(entry: SubsystemEntry, since: str) -> Optional[str]:
    """Build lei query for maintainer/reviewer messages.

    Args:
        entry: SubsystemEntry with maintainers and reviewers
        since: Date string for query (e.g., '30.days.ago')

    Returns:
        Query string like '(a:email1 OR a:email2) AND d:30.days.ago..'
        or None if no maintainers/reviewers

    Note: d: is placed last to work around a lei bug with d: as first param.
    """
    emails = entry.maintainers + entry.reviewers
    if not emails:
        return None

    email_parts = [f'a:{email}' for email in emails]
    email_query = ' OR '.join(email_parts)

    if len(emails) > 1:
        return f'({email_query}) AND d:{since}..'
    return f'{email_query} AND d:{since}..'


def build_mailinglist_query(entry: SubsystemEntry, since: str,
                            catchall_lists: Optional[Set[str]] = None
                            ) -> Tuple[Optional[str], List[str]]:
    """Build lei query for mailing list messages.

    Args:
        entry: SubsystemEntry with mailing lists
        since: Date string for query
        catchall_lists: Set of mailing list addresses to exclude from query.
            If None, uses DEFAULT_CATCHALL_LISTS.

    Returns:
        Tuple of (query_string, excluded_lists).
        Query is None if no usable mailing lists remain after filtering.

    Note: d: is placed last to work around a lei bug with d: as first param.
    """
    if catchall_lists is None:
        catchall_lists = set(DEFAULT_CATCHALL_LISTS)

    excluded: List[str] = []
    usable_lists: List[str] = []

    for ml in entry.mailing_lists:
        if ml in catchall_lists:
            excluded.append(ml)
        else:
            usable_lists.append(ml)

    if not usable_lists:
        return None, excluded

    list_parts = [f'l:{email_to_list_id(ml)}' for ml in usable_lists]
    list_query = ' OR '.join(list_parts)

    if len(usable_lists) > 1:
        return f'({list_query}) AND d:{since}..', excluded
    return f'{list_query} AND d:{since}..', excluded


def build_patches_query(entry: SubsystemEntry, since: str) -> Tuple[Optional[str], List[str]]:
    """Build lei query for patches touching subsystem files.

    Args:
        entry: SubsystemEntry with file patterns
        since: Date string for query

    Returns:
        Tuple of (query_string, list_of_skipped_patterns)
        Query is None if no usable patterns

    Note: d: is placed last to work around a lei bug with d: as first param.
    """
    skipped: List[str] = []
    include_parts: List[str] = []
    exclude_parts: List[str] = []

    # Process F: file patterns (preserve trailing slash for directory matching)
    for pattern in entry.files:
        if pattern:
            include_parts.append(f'dfn:{pattern}')

    # Process X: excluded patterns
    for pattern in entry.excluded:
        if pattern:
            exclude_parts.append(f'dfn:{pattern}')

    # Process N: file regex patterns (only simple ones)
    for pattern in entry.file_regex:
        if is_simple_pattern(pattern):
            include_parts.append(f'dfn:{pattern}')
        else:
            skipped.append(f'N:{pattern}')

    # Process K: content regex patterns (only simple ones)
    for pattern in entry.content_regex:
        if is_simple_pattern(pattern):
            include_parts.append(f'dfb:{pattern}')
        else:
            skipped.append(f'K:{pattern}')

    if not include_parts:
        return None, skipped

    # Build query with d: at end (lei bug workaround)
    if len(include_parts) == 1:
        include_query = include_parts[0]
    else:
        include_query = '(' + ' OR '.join(include_parts) + ')'

    query = include_query

    # Add exclusions if any
    if exclude_parts:
        for excl in exclude_parts:
            query = f'{query} NOT {excl}'

    # Add date filter last
    query = f'{query} AND d:{since}..'

    return query, skipped


def generate_subsystem_config(
    key: str,
    target: str,
    labels: List[str],
    lei_base_path: Path,
    since: str,
    subsystem_name: str
) -> str:
    """Generate TOML config content for a subsystem.

    Args:
        key: Normalized subsystem key (e.g., '9p_file_system')
        target: Target name for deliveries
        labels: List of labels to apply
        lei_base_path: Base path for lei search directories
        since: Date string used in queries
        subsystem_name: Original subsystem name for comments

    Returns:
        Formatted TOML string for writing to conf.d/{key}.toml
    """
    # Format labels as TOML array: ['INBOX', 'UNREAD']
    labels_str = ', '.join(f"'{label}'" for label in labels)
    timestamp = datetime.now().isoformat(timespec='seconds')

    return f'''# Auto-generated by: kgl track-subsystem '{subsystem_name}'
# Generated: {timestamp}
# Query date range: d:{since}..

[feeds.{key}-mailinglist]
url = 'lei:{lei_base_path}/{key}-mailinglist'

[feeds.{key}-patches]
url = 'lei:{lei_base_path}/{key}-patches'

[deliveries.{key}-mailinglist]
feed = '{key}-mailinglist'
target = '{target}'
labels = [{labels_str}]

[deliveries.{key}-patches]
feed = '{key}-patches'
target = '{target}'
labels = [{labels_str}]
'''
