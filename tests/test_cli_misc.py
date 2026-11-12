"""Miscellaneous CLI helper tests: key display and progress bar suppression.

Progress bar suppression across Click versions:

korgalore hides its progress bars by handing Click a throwaway stream rather
than by passing ``hidden=True``, which only exists in Click 8.3.0 and newer.
These tests pin the behaviour we rely on, so that a Click release changing how
a non-terminal output stream is treated fails here instead of dumping bar
noise into somebody's debug log.
"""

import io

import click
import pytest

from korgalore import format_key_for_display
from korgalore.cli import progress_file


@pytest.mark.parametrize(
    ('key', 'expected'),
    [
        ('lei:/home/user/foo/bar/queryname', 'lei:queryname'),
        ('lei:queryname', 'lei:queryname'),
        ('lei:/', 'lei:'),
        ('lei:/path/to/query/', 'lei:query'),
        # Lore keys are already normalized to the list name
        ('lkml', 'lkml'),
        ('ksummit', 'ksummit'),
        ('my-delivery', 'my-delivery'),
        ('https://example.com/feed', 'https://example.com/feed'),
        (None, ''),
    ],
    ids=[
        'lei-path',
        'lei-short',
        'lei-empty-component',
        'lei-trailing-slash',
        'lore-lkml',
        'lore-ksummit',
        'other-name',
        'other-url',
        'none',
    ],
)
def test_format_key_for_display(key: str | None, expected: str) -> None:
    assert format_key_for_display(key) == expected


def test_progress_file_visible() -> None:
    # None means "use Click's default", i.e. stdout.
    assert progress_file(False) is None


def test_progress_file_hidden() -> None:
    stream = progress_file(True)
    assert isinstance(stream, io.StringIO)
    # A fresh stream every call: two concurrent bars must not share one.
    assert progress_file(True) is not stream


def test_hidden_bar_writes_nothing_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    items = list(range(5))
    stream = progress_file(True)
    with click.progressbar(items, label='Hiding', show_pos=True, file=stream) as bar:
        seen = list(bar)

    # The iteration must still work -- hiding the bar is not skipping the work.
    assert seen == items
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == ''
    # Click's non-terminal path echoes the label once. Directing it at our own
    # stream is what keeps it off the user's terminal, so check it landed here.
    assert isinstance(stream, io.StringIO)
    assert 'Hiding' in stream.getvalue()
