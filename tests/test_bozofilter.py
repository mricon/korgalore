"""Tests for the bozofilter module."""

from pathlib import Path

from korgalore.bozofilter import (
    load_bozofilter,
    add_to_bozofilter,
    extract_email_address,
    is_bozofied,
    get_bozofilter_path,
)


class TestLoadBozofilter:
    """Tests for load_bozofilter function."""

    def test_empty_when_file_missing(self, tmp_path: Path) -> None:
        """Returns empty set when bozofilter file doesn't exist."""
        result = load_bozofilter(tmp_path)
        assert result == set()

    def test_empty_when_file_empty(self, tmp_path: Path) -> None:
        """Returns empty set when bozofilter file is empty."""
        (tmp_path / 'bozofilter.txt').touch()
        result = load_bozofilter(tmp_path)
        assert result == set()

    def test_parses_simple_addresses(self, tmp_path: Path) -> None:
        """Parses simple email addresses."""
        content = "spam@example.com\ntroll@example.org\n"
        (tmp_path / 'bozofilter.txt').write_text(content)
        result = load_bozofilter(tmp_path)
        assert result == {'spam@example.com', 'troll@example.org'}

    def test_skips_comment_lines(self, tmp_path: Path) -> None:
        """Skips lines that start with #."""
        content = "# This is a comment\nspam@example.com\n# Another comment\n"
        (tmp_path / 'bozofilter.txt').write_text(content)
        result = load_bozofilter(tmp_path)
        assert result == {'spam@example.com'}

    def test_strips_trailing_comments(self, tmp_path: Path) -> None:
        """Strips trailing comments from entries."""
        content = "spam@example.com # sends junk\ntroll@example.org # annoying\n"
        (tmp_path / 'bozofilter.txt').write_text(content)
        result = load_bozofilter(tmp_path)
        assert result == {'spam@example.com', 'troll@example.org'}

    def test_lowercases_addresses(self, tmp_path: Path) -> None:
        """Normalizes addresses to lowercase."""
        content = "SPAM@EXAMPLE.COM\nTroll@Example.Org\n"
        (tmp_path / 'bozofilter.txt').write_text(content)
        result = load_bozofilter(tmp_path)
        assert result == {'spam@example.com', 'troll@example.org'}

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        """Skips blank lines."""
        content = "spam@example.com\n\n\ntroll@example.org\n"
        (tmp_path / 'bozofilter.txt').write_text(content)
        result = load_bozofilter(tmp_path)
        assert result == {'spam@example.com', 'troll@example.org'}

    def test_mixed_content(self, tmp_path: Path) -> None:
        """Handles mixed comments, blank lines, and addresses."""
        content = """# Bozofilter
# Last updated: 2026-01-15

spam@example.com # sends junk patches
troll@example.org

# Bots
bot1@example.net # automated spam
bot2@example.net
"""
        (tmp_path / 'bozofilter.txt').write_text(content)
        result = load_bozofilter(tmp_path)
        assert result == {
            'spam@example.com',
            'troll@example.org',
            'bot1@example.net',
            'bot2@example.net',
        }


class TestAddToBozofilter:
    """Tests for add_to_bozofilter function."""

    def test_creates_file_if_missing(self, tmp_path: Path) -> None:
        """Creates bozofilter file if it doesn't exist."""
        added = add_to_bozofilter(tmp_path, ['spam@example.com'])
        assert added == 1
        assert (tmp_path / 'bozofilter.txt').exists()

    def test_adds_single_address(self, tmp_path: Path) -> None:
        """Adds a single address to empty filter."""
        added = add_to_bozofilter(tmp_path, ['spam@example.com'])
        assert added == 1
        result = load_bozofilter(tmp_path)
        assert 'spam@example.com' in result

    def test_adds_multiple_addresses(self, tmp_path: Path) -> None:
        """Adds multiple addresses."""
        added = add_to_bozofilter(tmp_path, ['a@example.com', 'b@example.com'])
        assert added == 2
        result = load_bozofilter(tmp_path)
        assert result == {'a@example.com', 'b@example.com'}

    def test_skips_existing_addresses(self, tmp_path: Path) -> None:
        """Doesn't add addresses that already exist."""
        (tmp_path / 'bozofilter.txt').write_text('existing@example.com\n')
        added = add_to_bozofilter(tmp_path, ['existing@example.com', 'new@example.com'])
        assert added == 1
        result = load_bozofilter(tmp_path)
        assert result == {'existing@example.com', 'new@example.com'}

    def test_includes_reason_in_comment(self, tmp_path: Path) -> None:
        """Includes reason in the trailing comment."""
        add_to_bozofilter(tmp_path, ['spam@example.com'], reason='sends junk')
        content = (tmp_path / 'bozofilter.txt').read_text()
        assert 'sends junk' in content

    def test_includes_date_in_comment(self, tmp_path: Path) -> None:
        """Includes date in the trailing comment."""
        add_to_bozofilter(tmp_path, ['spam@example.com'])
        content = (tmp_path / 'bozofilter.txt').read_text()
        assert 'added on' in content

    def test_lowercases_when_adding(self, tmp_path: Path) -> None:
        """Normalizes addresses to lowercase when adding."""
        add_to_bozofilter(tmp_path, ['SPAM@EXAMPLE.COM'])
        result = load_bozofilter(tmp_path)
        assert 'spam@example.com' in result

    def test_skips_empty_addresses(self, tmp_path: Path) -> None:
        """Skips empty or whitespace-only addresses."""
        added = add_to_bozofilter(tmp_path, ['', '  ', 'valid@example.com'])
        assert added == 1
        result = load_bozofilter(tmp_path)
        assert result == {'valid@example.com'}


class TestExtractEmailAddress:
    """Tests for extract_email_address function."""

    def test_extracts_from_angle_brackets(self) -> None:
        """Extracts address from angle bracket format."""
        result = extract_email_address('John Doe <john@example.com>')
        assert result == 'john@example.com'

    def test_extracts_bare_address(self) -> None:
        """Extracts bare email address."""
        result = extract_email_address('john@example.com')
        assert result == 'john@example.com'

    def test_lowercases_result(self) -> None:
        """Returns lowercase address."""
        result = extract_email_address('JOHN@EXAMPLE.COM')
        assert result == 'john@example.com'

    def test_returns_none_for_empty(self) -> None:
        """Returns None for empty input."""
        assert extract_email_address('') is None
        assert extract_email_address(None) is None  # type: ignore[arg-type]

    def test_handles_complex_names(self) -> None:
        """Handles names with special characters."""
        result = extract_email_address('"Doe, John" <john@example.com>')
        assert result == 'john@example.com'

    def test_handles_no_name(self) -> None:
        """Handles just angle brackets without name."""
        result = extract_email_address('<john@example.com>')
        assert result == 'john@example.com'


class TestIsBozofied:
    """Tests for is_bozofied function."""

    def test_returns_false_for_empty_filter(self) -> None:
        """Returns False when filter is empty."""
        assert is_bozofied('spam@example.com', set()) is False

    def test_matches_exact_address(self) -> None:
        """Matches exact email address."""
        bozo = {'spam@example.com'}
        assert is_bozofied('spam@example.com', bozo) is True

    def test_matches_with_display_name(self) -> None:
        """Matches when From header has display name."""
        bozo = {'spam@example.com'}
        assert is_bozofied('Spammer <spam@example.com>', bozo) is True

    def test_case_insensitive_match(self) -> None:
        """Matches regardless of case."""
        bozo = {'spam@example.com'}
        assert is_bozofied('SPAM@EXAMPLE.COM', bozo) is True
        assert is_bozofied('Spammer <SPAM@Example.Com>', bozo) is True

    def test_no_match_returns_false(self) -> None:
        """Returns False when address not in filter."""
        bozo = {'spam@example.com'}
        assert is_bozofied('good@example.com', bozo) is False
        assert is_bozofied('Good User <good@example.com>', bozo) is False

    def test_handles_empty_header(self) -> None:
        """Returns False for empty From header."""
        bozo = {'spam@example.com'}
        assert is_bozofied('', bozo) is False


class TestGetBozofilterPath:
    """Tests for get_bozofilter_path function."""

    def test_returns_correct_path(self, tmp_path: Path) -> None:
        """Returns correct path in config directory."""
        result = get_bozofilter_path(tmp_path)
        assert result == tmp_path / 'bozofilter.txt'
