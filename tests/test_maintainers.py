"""Tests for MAINTAINERS file parser and query builders."""

import pytest
from pathlib import Path

from korgalore.maintainers import (
    SubsystemEntry,
    normalize_subsystem_name,
    extract_email,
    is_field_line,
    is_subsystem_title,
    is_simple_pattern,
    email_to_list_id,
    parse_maintainers,
    get_subsystem,
    build_maintainers_query,
    build_mailinglist_query,
    build_patches_query,
    generate_subsystem_config,
)


class TestNormalizeSubsystemName:
    """Tests for normalize_subsystem_name function."""

    def test_basic_name(self) -> None:
        """Basic uppercase name converts to lowercase with underscores."""
        assert normalize_subsystem_name("AMD GPU") == "amd_gpu"

    def test_with_numbers(self) -> None:
        """Names starting with numbers are preserved."""
        assert normalize_subsystem_name("9P FILE SYSTEM") == "9p_file_system"

    def test_removes_parenthetical(self) -> None:
        """Parenthetical content is removed."""
        result = normalize_subsystem_name("3WARE SAS/SATA-RAID SCSI DRIVERS (3W-XXXX)")
        assert result == "3ware_sas_sata_raid_scsi_drivers"

    def test_special_chars_become_underscores(self) -> None:
        """Special characters become underscores."""
        assert normalize_subsystem_name("SAS/SATA-RAID") == "sas_sata_raid"

    def test_strips_leading_trailing_underscores(self) -> None:
        """Leading and trailing underscores are stripped."""
        assert normalize_subsystem_name("  SUBSYSTEM  ") == "subsystem"

    def test_multiple_spaces(self) -> None:
        """Multiple spaces collapse to single underscore."""
        assert normalize_subsystem_name("MULTIPLE   SPACES") == "multiple_spaces"


class TestExtractEmail:
    """Tests for extract_email function."""

    def test_angle_brackets(self) -> None:
        """Extracts email from angle brackets."""
        assert extract_email("John Doe <john@example.com>") == "john@example.com"

    def test_quoted_name(self) -> None:
        """Extracts email when name is quoted."""
        assert extract_email('"John Doe" <john@example.com>') == "john@example.com"

    def test_bare_email(self) -> None:
        """Handles bare email addresses without angle brackets."""
        assert extract_email("john@example.com") == "john@example.com"

    def test_empty_string(self) -> None:
        """Returns None for empty string."""
        assert extract_email("") is None

    def test_whitespace_only(self) -> None:
        """Returns None for whitespace-only string."""
        assert extract_email("   ") is None

    def test_empty_angle_brackets(self) -> None:
        """Returns None for empty angle brackets."""
        result = extract_email("Name <>")
        assert result is None

    def test_complex_email(self) -> None:
        """Handles complex email addresses."""
        assert extract_email("Name <user+tag@sub.domain.org>") == "user+tag@sub.domain.org"


class TestIsFieldLine:
    """Tests for is_field_line function."""

    def test_maintainer_field(self) -> None:
        """M: field line is recognized."""
        assert is_field_line("M:\tJohn Doe <john@example.com>") is True

    def test_reviewer_field(self) -> None:
        """R: field line is recognized."""
        assert is_field_line("R:\tReviewer <reviewer@example.com>") is True

    def test_list_field(self) -> None:
        """L: field line is recognized."""
        assert is_field_line("L:\tlist@lists.linux.dev") is True

    def test_file_field(self) -> None:
        """F: field line is recognized."""
        assert is_field_line("F:\tdrivers/gpu/") is True

    def test_status_field(self) -> None:
        """S: field line is recognized."""
        assert is_field_line("S:\tMaintained") is True

    def test_excluded_field(self) -> None:
        """X: field line is recognized."""
        assert is_field_line("X:\tdrivers/staging/") is True

    def test_not_field_lowercase_prefix(self) -> None:
        """Lowercase prefix is not a field line."""
        assert is_field_line("m:\tvalue") is False

    def test_not_field_no_tab(self) -> None:
        """Missing tab after colon is not a field line."""
        assert is_field_line("M: value") is False
        assert is_field_line("M:value") is False

    def test_not_field_multiple_chars(self) -> None:
        """Multi-char prefix is not a field line."""
        assert is_field_line("MM:\tvalue") is False

    def test_not_field_empty(self) -> None:
        """Empty line is not a field line."""
        assert is_field_line("") is False

    def test_not_field_short(self) -> None:
        """Line shorter than 3 chars is not a field line."""
        assert is_field_line("M:") is False
        assert is_field_line("M") is False


class TestIsSubsystemTitle:
    """Tests for is_subsystem_title function."""

    def test_uppercase_title_after_empty(self) -> None:
        """Uppercase line after empty line is a title."""
        assert is_subsystem_title("AMD GPU DRIVER", prev_line_empty=True) is True

    def test_starts_with_digit_after_empty(self) -> None:
        """Line starting with digit after empty line is a title."""
        assert is_subsystem_title("9P FILE SYSTEM", prev_line_empty=True) is True

    def test_not_title_without_prev_empty(self) -> None:
        """Title candidate without preceding empty line is not a title."""
        assert is_subsystem_title("AMD GPU DRIVER", prev_line_empty=False) is False

    def test_line_with_tab_not_title(self) -> None:
        """Line containing tab is not a title."""
        assert is_subsystem_title("AMD\tGPU", prev_line_empty=True) is False
        assert is_subsystem_title("M:\tvalue", prev_line_empty=True) is False

    def test_lowercase_only_not_title(self) -> None:
        """Line with no uppercase letters is not a title."""
        assert is_subsystem_title("all lowercase words", prev_line_empty=True) is False

    def test_empty_line_not_title(self) -> None:
        """Empty line is not a title."""
        assert is_subsystem_title("", prev_line_empty=True) is False

    def test_whitespace_only_not_title(self) -> None:
        """Whitespace-only line is not a title."""
        assert is_subsystem_title("   ", prev_line_empty=True) is False

    def test_mixed_case_is_title(self) -> None:
        """Line with mixed case is a title (many subsystems use mixed case)."""
        assert is_subsystem_title("ARM/Allwinner SoC Clock Support", prev_line_empty=True) is True
        assert is_subsystem_title("AMD Gpu Driver", prev_line_empty=True) is True

    def test_special_chars_is_title(self) -> None:
        """Lines with special chars like slashes and parens are titles."""
        assert is_subsystem_title("3WARE SAS/SATA-RAID SCSI DRIVERS (3W-XXXX)", prev_line_empty=True) is True
        assert is_subsystem_title("ACPI FOR ARM64 (ACPI/arm64)", prev_line_empty=True) is True

    def test_field_line_not_title(self) -> None:
        """Field lines are not titles (contain tab)."""
        # Field lines have tabs, so they fail the no-tab check
        assert is_subsystem_title("M:\tJohn Doe", prev_line_empty=True) is False


class TestIsSimplePattern:
    """Tests for is_simple_pattern function."""

    def test_simple_word(self) -> None:
        """Simple word without metacharacters."""
        assert is_simple_pattern("csky") is True
        assert is_simple_pattern("driver") is True

    def test_with_path(self) -> None:
        """Path-like pattern without regex metacharacters."""
        assert is_simple_pattern("drivers/gpu/drm") is True

    def test_with_backslash(self) -> None:
        """Pattern with backslash is not simple."""
        assert is_simple_pattern(r"\bword\b") is False

    def test_with_brackets(self) -> None:
        """Pattern with brackets is not simple."""
        assert is_simple_pattern("[0-9]") is False
        assert is_simple_pattern("(group)") is False

    def test_with_quantifiers(self) -> None:
        """Pattern with quantifiers is not simple."""
        assert is_simple_pattern("a*") is False
        assert is_simple_pattern("a+") is False
        assert is_simple_pattern("a?") is False

    def test_with_anchors(self) -> None:
        """Pattern with anchors is not simple."""
        assert is_simple_pattern("^start") is False
        assert is_simple_pattern("end$") is False

    def test_with_alternation(self) -> None:
        """Pattern with alternation is not simple."""
        assert is_simple_pattern("a|b") is False

    def test_complex_regex(self) -> None:
        """Complex regex patterns are not simple."""
        assert is_simple_pattern(r"\b(?i:clang|llvm)\b") is False
        assert is_simple_pattern(r"[^\s]+\.rs$") is False


class TestEmailToListId:
    """Tests for email_to_list_id function."""

    def test_basic_conversion(self) -> None:
        """@ is replaced with dot."""
        assert email_to_list_id("v9fs@lists.linux.dev") == "v9fs.lists.linux.dev"

    def test_subdomain(self) -> None:
        """Works with subdomains."""
        assert email_to_list_id("linux-kernel@vger.kernel.org") == "linux-kernel.vger.kernel.org"


class TestBuildMaintainersQuery:
    """Tests for build_maintainers_query function."""

    def test_single_maintainer(self) -> None:
        """Query with single maintainer."""
        entry = SubsystemEntry(
            name="TEST",
            maintainers=["john@example.com"],
        )
        result = build_maintainers_query(entry, "30.days.ago")
        assert result == "a:john@example.com AND d:30.days.ago.."

    def test_multiple_maintainers(self) -> None:
        """Query with multiple maintainers uses OR and parentheses."""
        entry = SubsystemEntry(
            name="TEST",
            maintainers=["john@example.com", "jane@example.com"],
        )
        result = build_maintainers_query(entry, "30.days.ago")
        assert result == "(a:john@example.com OR a:jane@example.com) AND d:30.days.ago.."

    def test_maintainers_and_reviewers(self) -> None:
        """Query includes both maintainers and reviewers."""
        entry = SubsystemEntry(
            name="TEST",
            maintainers=["maintainer@example.com"],
            reviewers=["reviewer@example.com"],
        )
        result = build_maintainers_query(entry, "30.days.ago")
        assert result == "(a:maintainer@example.com OR a:reviewer@example.com) AND d:30.days.ago.."

    def test_no_maintainers(self) -> None:
        """Returns None when no maintainers or reviewers."""
        entry = SubsystemEntry(name="TEST")
        result = build_maintainers_query(entry, "30.days.ago")
        assert result is None

    def test_custom_since(self) -> None:
        """Custom since value is used."""
        entry = SubsystemEntry(
            name="TEST",
            maintainers=["john@example.com"],
        )
        result = build_maintainers_query(entry, "7.days.ago")
        assert result == "a:john@example.com AND d:7.days.ago.."


class TestBuildMailinglistQuery:
    """Tests for build_mailinglist_query function."""

    def test_single_list(self) -> None:
        """Query with single mailing list."""
        entry = SubsystemEntry(
            name="TEST",
            mailing_lists=["list@lists.linux.dev"],
        )
        query, excluded = build_mailinglist_query(entry, "30.days.ago")
        assert query == "l:list.lists.linux.dev AND d:30.days.ago.."
        assert excluded == []

    def test_multiple_lists(self) -> None:
        """Query with multiple mailing lists uses OR and parentheses."""
        entry = SubsystemEntry(
            name="TEST",
            mailing_lists=["list1@domain.org", "list2@domain.org"],
        )
        query, excluded = build_mailinglist_query(entry, "30.days.ago")
        assert query == "(l:list1.domain.org OR l:list2.domain.org) AND d:30.days.ago.."
        assert excluded == []

    def test_no_lists(self) -> None:
        """Returns None when no mailing lists."""
        entry = SubsystemEntry(name="TEST")
        query, excluded = build_mailinglist_query(entry, "30.days.ago")
        assert query is None
        assert excluded == []

    def test_excludes_default_catchall_lists(self) -> None:
        """Default catchall lists are excluded from query."""
        entry = SubsystemEntry(
            name="TEST",
            mailing_lists=[
                "subsystem@lists.linux.dev",
                "linux-kernel@vger.kernel.org",
                "patches@lists.linux.dev",
            ],
        )
        query, excluded = build_mailinglist_query(entry, "30.days.ago")
        assert query == "l:subsystem.lists.linux.dev AND d:30.days.ago.."
        assert "linux-kernel@vger.kernel.org" in excluded
        assert "patches@lists.linux.dev" in excluded

    def test_all_lists_excluded_returns_none(self) -> None:
        """Returns None when all lists are catchall lists."""
        entry = SubsystemEntry(
            name="TEST",
            mailing_lists=["linux-kernel@vger.kernel.org"],
        )
        query, excluded = build_mailinglist_query(entry, "30.days.ago")
        assert query is None
        assert excluded == ["linux-kernel@vger.kernel.org"]

    def test_custom_catchall_lists(self) -> None:
        """Custom catchall lists override defaults."""
        entry = SubsystemEntry(
            name="TEST",
            mailing_lists=[
                "subsystem@lists.linux.dev",
                "linux-kernel@vger.kernel.org",
            ],
        )
        # Custom catchall that doesn't include linux-kernel
        query, excluded = build_mailinglist_query(
            entry, "30.days.ago", catchall_lists={"custom@example.com"}
        )
        # linux-kernel should NOT be excluded with custom list
        assert query is not None
        assert "linux-kernel" in query
        assert excluded == []

    def test_empty_catchall_lists_includes_all(self) -> None:
        """Empty catchall set includes all mailing lists."""
        entry = SubsystemEntry(
            name="TEST",
            mailing_lists=[
                "subsystem@lists.linux.dev",
                "linux-kernel@vger.kernel.org",
            ],
        )
        query, excluded = build_mailinglist_query(
            entry, "30.days.ago", catchall_lists=set()
        )
        assert query is not None
        assert "linux-kernel" in query
        assert "subsystem" in query
        assert excluded == []


class TestBuildPatchesQuery:
    """Tests for build_patches_query function."""

    def test_file_patterns(self) -> None:
        """F: patterns become dfn: queries with trailing slash preserved."""
        entry = SubsystemEntry(
            name="TEST",
            files=["drivers/gpu/", "include/drm/"],
        )
        query, skipped = build_patches_query(entry, "30.days.ago")
        assert query is not None
        assert "dfn:drivers/gpu/" in query
        assert "dfn:include/drm/" in query
        assert skipped == []

    def test_excluded_patterns(self) -> None:
        """X: patterns become NOT dfn: clauses with trailing slash preserved."""
        entry = SubsystemEntry(
            name="TEST",
            files=["drivers/"],
            excluded=["drivers/staging/"],
        )
        query, skipped = build_patches_query(entry, "30.days.ago")
        assert query is not None
        assert "dfn:drivers/" in query
        assert "NOT dfn:drivers/staging/" in query

    def test_simple_file_regex(self) -> None:
        """Simple N: patterns are included."""
        entry = SubsystemEntry(
            name="TEST",
            file_regex=["csky"],
        )
        query, skipped = build_patches_query(entry, "30.days.ago")
        assert query is not None
        assert "dfn:csky" in query
        assert skipped == []

    def test_complex_file_regex_skipped(self) -> None:
        """Complex N: patterns are skipped."""
        entry = SubsystemEntry(
            name="TEST",
            file_regex=[r"\b(?i:clang)\b"],
        )
        query, skipped = build_patches_query(entry, "30.days.ago")
        assert query is None
        assert r"N:\b(?i:clang)\b" in skipped

    def test_simple_content_regex(self) -> None:
        """Simple K: patterns become dfb: queries."""
        entry = SubsystemEntry(
            name="TEST",
            content_regex=["CONFIG_DRM"],
        )
        query, skipped = build_patches_query(entry, "30.days.ago")
        assert query is not None
        assert "dfb:CONFIG_DRM" in query
        assert skipped == []

    def test_complex_content_regex_skipped(self) -> None:
        """Complex K: patterns are skipped."""
        entry = SubsystemEntry(
            name="TEST",
            content_regex=[r"[^\s]+\.rs$"],
        )
        query, skipped = build_patches_query(entry, "30.days.ago")
        assert query is None
        assert r"K:[^\s]+\.rs$" in skipped

    def test_mixed_patterns(self) -> None:
        """Mixed simple and complex patterns."""
        entry = SubsystemEntry(
            name="TEST",
            files=["drivers/test/"],
            file_regex=["simple", r"complex\b"],
            content_regex=["CONFIG_TEST"],
        )
        query, skipped = build_patches_query(entry, "30.days.ago")
        assert query is not None
        assert "dfn:drivers/test/" in query
        assert "dfn:simple" in query
        assert "dfb:CONFIG_TEST" in query
        assert r"N:complex\b" in skipped

    def test_no_patterns(self) -> None:
        """Returns None when no usable patterns."""
        entry = SubsystemEntry(name="TEST")
        query, skipped = build_patches_query(entry, "30.days.ago")
        assert query is None
        assert skipped == []

    def test_single_pattern_no_parens(self) -> None:
        """Single pattern doesn't get wrapped in parentheses."""
        entry = SubsystemEntry(
            name="TEST",
            files=["drivers/test/"],
        )
        query, skipped = build_patches_query(entry, "30.days.ago")
        assert query == "dfn:drivers/test/ AND d:30.days.ago.."

    def test_multiple_patterns_with_parens(self) -> None:
        """Multiple patterns get wrapped in parentheses with OR."""
        entry = SubsystemEntry(
            name="TEST",
            files=["drivers/a/", "drivers/b/"],
        )
        query, skipped = build_patches_query(entry, "30.days.ago")
        assert query is not None
        assert "(dfn:drivers/a/ OR dfn:drivers/b/)" in query


class TestParseMaintainers:
    """Tests for parse_maintainers function."""

    def test_parse_single_subsystem(self, tmp_path: Path) -> None:
        """Parse a single subsystem entry."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text("""
9P FILE SYSTEM
M:\tEric Van Hensbergen <ericvh@kernel.org>
L:\tv9fs@lists.linux.dev
S:\tMaintained
F:\tfs/9p/
F:\tnet/9p/
""")
        entries = parse_maintainers(maintainers)
        assert "9P FILE SYSTEM" in entries
        entry = entries["9P FILE SYSTEM"]
        assert entry.maintainers == ["ericvh@kernel.org"]
        assert entry.mailing_lists == ["v9fs@lists.linux.dev"]
        assert entry.status == "Maintained"
        assert "fs/9p/" in entry.files
        assert "net/9p/" in entry.files

    def test_parse_multiple_subsystems(self, tmp_path: Path) -> None:
        """Parse multiple subsystem entries."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text("""
FIRST SUBSYSTEM
M:\tfirst@example.com
F:\tfirst/

SECOND SUBSYSTEM
M:\tsecond@example.com
F:\tsecond/
""")
        entries = parse_maintainers(maintainers)
        assert len(entries) == 2
        assert "FIRST SUBSYSTEM" in entries
        assert "SECOND SUBSYSTEM" in entries

    def test_parse_all_field_types(self, tmp_path: Path) -> None:
        """Parse all supported field types."""
        maintainers = tmp_path / "MAINTAINERS"
        # Note: M:/R: lines need angle brackets for email extraction
        maintainers.write_text(
            "TEST SUBSYSTEM\n"
            "M:\tMaintainer Name <maintainer@example.com>\n"
            "R:\tReviewer Name <reviewer@example.com>\n"
            "L:\tlist@lists.linux.dev\n"
            "S:\tMaintained\n"
            "F:\tpath/to/files/\n"
            "X:\tpath/to/excluded/\n"
            "N:\tsimple_pattern\n"
            "K:\tCONFIG_TEST\n"
        )
        entries = parse_maintainers(maintainers)
        entry = entries["TEST SUBSYSTEM"]
        assert entry.maintainers == ["maintainer@example.com"]
        assert entry.reviewers == ["reviewer@example.com"]
        assert entry.mailing_lists == ["list@lists.linux.dev"]
        assert entry.status == "Maintained"
        assert entry.files == ["path/to/files/"]
        assert entry.excluded == ["path/to/excluded/"]
        assert entry.file_regex == ["simple_pattern"]
        assert entry.content_regex == ["CONFIG_TEST"]

    def test_parse_multiple_maintainers(self, tmp_path: Path) -> None:
        """Parse subsystem with multiple maintainers."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text(
            "MULTI MAINTAINER\n"
            "M:\tFirst Person <first@example.com>\n"
            "M:\tSecond Person <second@example.com>\n"
            "M:\tThird Person <third@example.com>\n"
        )
        entries = parse_maintainers(maintainers)
        entry = entries["MULTI MAINTAINER"]
        assert len(entry.maintainers) == 3

    def test_parse_skips_empty_lines(self, tmp_path: Path) -> None:
        """Empty lines are skipped."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text("""
TEST SUBSYSTEM

M:\ttest@example.com

F:\tpath/
""")
        entries = parse_maintainers(maintainers)
        assert "TEST SUBSYSTEM" in entries

    def test_parse_handles_preamble(self, tmp_path: Path) -> None:
        """Handles preamble text before first subsystem."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text("""This is preamble text
that should be ignored
until we reach a subsystem title.

ACTUAL SUBSYSTEM
M:\ttest@example.com
""")
        entries = parse_maintainers(maintainers)
        # Preamble lines might be parsed as entries, but the actual subsystem should be there
        assert "ACTUAL SUBSYSTEM" in entries


class TestGetSubsystem:
    """Tests for get_subsystem function."""

    def test_exact_match(self, tmp_path: Path) -> None:
        """Exact name match works."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text("""
TEST SUBSYSTEM
M:\ttest@example.com
""")
        entry = get_subsystem(maintainers, "TEST SUBSYSTEM")
        assert entry.name == "TEST SUBSYSTEM"

    def test_case_insensitive_match(self, tmp_path: Path) -> None:
        """Case-insensitive match works."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text("""
TEST SUBSYSTEM
M:\ttest@example.com
""")
        entry = get_subsystem(maintainers, "test subsystem")
        assert entry.name == "TEST SUBSYSTEM"

    def test_not_found_raises_keyerror(self, tmp_path: Path) -> None:
        """KeyError raised when subsystem not found."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text("""
TEST SUBSYSTEM
M:\ttest@example.com
""")
        with pytest.raises(KeyError, match="not found"):
            get_subsystem(maintainers, "NONEXISTENT")

    def test_substring_match(self, tmp_path: Path) -> None:
        """Substring match works when unique."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text(
            "ARM/ALLWINNER SOC SUPPORT\n"
            "M:\tallwinner@example.com\n"
            "\n"
            "INTEL GPU DRIVER\n"
            "M:\tintel@example.com\n"
        )
        # "ALLWINNER" is unique substring
        entry = get_subsystem(maintainers, "ALLWINNER")
        assert entry.name == "ARM/ALLWINNER SOC SUPPORT"

    def test_substring_match_case_insensitive(self, tmp_path: Path) -> None:
        """Substring match is case-insensitive."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text(
            "ARM/ALLWINNER SOC SUPPORT\n"
            "M:\tallwinner@example.com\n"
        )
        entry = get_subsystem(maintainers, "allwinner")
        assert entry.name == "ARM/ALLWINNER SOC SUPPORT"

    def test_ambiguous_substring_raises_valueerror(self, tmp_path: Path) -> None:
        """ValueError raised when substring matches multiple entries."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text(
            "802.11 WIRELESS NETWORKING\n"
            "M:\twireless@example.com\n"
            "\n"
            "802.11 BLUETOOTH DRIVER\n"
            "M:\tbluetooth@example.com\n"
        )
        with pytest.raises(ValueError, match="Ambiguous.*matches 2 entries"):
            get_subsystem(maintainers, "802.11")

    def test_exact_match_preferred_over_substring(self, tmp_path: Path) -> None:
        """Exact match is preferred even if substrings would match."""
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text(
            "GPU\n"
            "M:\tgpu@example.com\n"
            "\n"
            "GPU MEMORY MANAGER\n"
            "M:\tgpumem@example.com\n"
        )
        # "GPU" exact match should be preferred over substring matching both
        entry = get_subsystem(maintainers, "GPU")
        assert entry.name == "GPU"

    def test_substring_resolves_to_canonical_name(self, tmp_path: Path) -> None:
        """Substring match resolves to full canonical name for consistent normalisation.

        Regression test: track-subsystem --forget must use the same normalised
        key as track-subsystem when creating config. The user may supply a
        substring (e.g., "REGISTER MAP") that matches a longer entry name
        (e.g., "REGISTER MAP ABSTRACTION LAYER"). The normalised key must come
        from the full entry name, not the user input.
        """
        maintainers = tmp_path / "MAINTAINERS"
        maintainers.write_text(
            "REGISTER MAP ABSTRACTION LAYER\n"
            "M:\tbroonie@kernel.org\n"
            "L:\tlinux-kernel@vger.kernel.org\n"
            "F:\tdrivers/base/regmap/\n"
        )
        # User supplies substring "REGISTER MAP"
        entry = get_subsystem(maintainers, "REGISTER MAP")
        assert entry.name == "REGISTER MAP ABSTRACTION LAYER"
        # The normalised key must match what the create path uses
        canonical_key = normalize_subsystem_name(entry.name)
        user_input_key = normalize_subsystem_name("REGISTER MAP")
        assert canonical_key == "register_map_abstraction_layer"
        assert user_input_key == "register_map"
        assert canonical_key != user_input_key  # confirms the bug scenario


class TestGenerateSubsystemConfig:
    """Tests for generate_subsystem_config function."""

    def test_generates_valid_toml(self, tmp_path: Path) -> None:
        """Generated config is valid TOML."""
        import tomllib

        content = generate_subsystem_config(
            key="test_subsystem",
            target="personal",
            labels=["INBOX", "UNREAD"],
            lei_base_path=tmp_path / "lei",
            since="30.days.ago",
            subsystem_name="TEST SUBSYSTEM",
        )

        # Should parse without error
        config = tomllib.loads(content)

        # Check structure (TOML parses to nested dicts)
        assert "test_subsystem-mailinglist" in config["feeds"]
        assert "test_subsystem-patches" in config["feeds"]
        assert "test_subsystem-mailinglist" in config["deliveries"]
        assert "test_subsystem-patches" in config["deliveries"]

        # Check subsystem metadata
        assert config["subsystem"]["name"] == "TEST SUBSYSTEM"

    def test_subsystem_name_matches_input(self, tmp_path: Path) -> None:
        """Subsystem name in config matches the input parameter."""
        import tomllib

        content = generate_subsystem_config(
            key="9p_file_system",
            target="personal",
            labels=["INBOX"],
            lei_base_path=tmp_path / "lei",
            since="30.days.ago",
            subsystem_name="9P FILE SYSTEM",
        )

        config = tomllib.loads(content)
        assert config["subsystem"]["name"] == "9P FILE SYSTEM"

    def test_labels_formatted_correctly(self, tmp_path: Path) -> None:
        """Labels are formatted as TOML array."""
        content = generate_subsystem_config(
            key="test",
            target="personal",
            labels=["INBOX", "UNREAD", "IMPORTANT"],
            lei_base_path=tmp_path / "lei",
            since="30.days.ago",
            subsystem_name="TEST",
        )

        assert "labels = ['INBOX', 'UNREAD', 'IMPORTANT']" in content

    def test_includes_metadata_comments(self, tmp_path: Path) -> None:
        """Config includes metadata comments."""
        content = generate_subsystem_config(
            key="test",
            target="personal",
            labels=["INBOX"],
            lei_base_path=tmp_path / "lei",
            since="30.days.ago",
            subsystem_name="TEST SUBSYSTEM",
        )

        assert "# Auto-generated by: kgl track-subsystem 'TEST SUBSYSTEM'" in content
        assert "# Query date range: d:30.days.ago.." in content

    def test_target_in_deliveries(self, tmp_path: Path) -> None:
        """Target name appears in delivery configs."""
        content = generate_subsystem_config(
            key="test",
            target="my_target",
            labels=["INBOX"],
            lei_base_path=tmp_path / "lei",
            since="30.days.ago",
            subsystem_name="TEST",
        )

        assert "target = 'my_target'" in content

    def test_lei_paths_correct(self, tmp_path: Path) -> None:
        """Lei paths use correct base path and key."""
        lei_base = tmp_path / "lei"
        content = generate_subsystem_config(
            key="9p_file_system",
            target="personal",
            labels=["INBOX"],
            lei_base_path=lei_base,
            since="30.days.ago",
            subsystem_name="9P FILE SYSTEM",
        )

        assert f"url = 'lei:{lei_base}/9p_file_system-mailinglist'" in content
        assert f"url = 'lei:{lei_base}/9p_file_system-patches'" in content

    def test_exclude_mailinglist(self, tmp_path: Path) -> None:
        """Config excludes mailinglist when include_mailinglist=False."""
        import tomllib

        content = generate_subsystem_config(
            key="test",
            target="personal",
            labels=["INBOX"],
            lei_base_path=tmp_path / "lei",
            since="30.days.ago",
            subsystem_name="TEST",
            include_mailinglist=False,
            include_patches=True,
        )

        config = tomllib.loads(content)
        assert "test-mailinglist" not in config.get("feeds", {})
        assert "test-mailinglist" not in config.get("deliveries", {})
        assert "test-patches" in config["feeds"]
        assert "test-patches" in config["deliveries"]

    def test_exclude_patches(self, tmp_path: Path) -> None:
        """Config excludes patches when include_patches=False."""
        import tomllib

        content = generate_subsystem_config(
            key="test",
            target="personal",
            labels=["INBOX"],
            lei_base_path=tmp_path / "lei",
            since="30.days.ago",
            subsystem_name="TEST",
            include_mailinglist=True,
            include_patches=False,
        )

        config = tomllib.loads(content)
        assert "test-patches" not in config.get("feeds", {})
        assert "test-patches" not in config.get("deliveries", {})
        assert "test-mailinglist" in config["feeds"]
        assert "test-mailinglist" in config["deliveries"]
